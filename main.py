"""
Daily Editorial Pipeline — BCS Mentor
=====================================
Runs on GitHub Actions (free tier) every day at 06:00 Bangladesh time.

Flow:
  1. Pull latest items from configured RSS feeds / Google News fallbacks.
  2. Filter to the BCS-relevant topics in sources.py, dedupe against Firestore.
  3. Extract full article text (trafilatura).
  4. Gemini (free tier, rate-limited): gate by article type, then produce a
     comprehensive summary, vocabulary, exam keywords, and punchlines.
  5. Write the document — including the FULL article text for in-app reading —
     to Firestore `(default)` database, `editorials` collection.

Environment variables (see README.md):
  GEMINI_API_KEY               required
  FIREBASE_SERVICE_ACCOUNT     required (raw JSON of the service-account key)
  FIREBASE_SERVICE_ACCOUNT_FILE  alternative: path to the key file (local dev)
  GEMINI_MODEL                 optional override; by default the script asks the
                               API which models this key can use and picks the
                               newest Flash model (survives model retirements)
  MAX_PER_SOURCE               default 2
  MAX_TOTAL                    default 15   (keeps Gemini free tier safe)
"""

import hashlib
import os
import re
import sys
import time
from urllib.parse import quote

import requests

from firebase_admin import firestore

from google import genai

from sources import FEEDS, TOPIC_KEYWORDS, GNEWS_OPINION_TERMS

# Fetching, extraction, encoding hygiene and Gemini plumbing are shared with
# the weekly current-affairs pipeline (updates.py) — see common.py.
from common import (
    log, UA_HEADERS, today_bst, init_firebase, fetch_feed, resolve_gnews_link,
    extract_article, plain_text, looks_mojibake, detect_language,
    entry_age_hours, pick_model, generate_json,
    ModelUnavailable, DailyQuotaExhausted,
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")  # optional override
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "2"))
MAX_TOTAL = int(os.environ.get("MAX_TOTAL", "15"))
# Hard cap on Gemini requests per run (saves + gate-rejections combined).
# Current Flash models get ~1,500 free requests/day (the "20" once observed
# was the leftover quota of the retired gemini-2.5-flash), so 40 is a safety
# rail, not the binding constraint — MAX_TOTAL is. If Google quietly lowers
# limits again, the DailyQuotaExhausted handler still stops the run cleanly.
MAX_GEMINI_CALLS = int(os.environ.get("MAX_GEMINI_CALLS", "40"))
RECENCY_HOURS = 36
MIN_ARTICLE_CHARS = 600
GEMINI_COOLDOWN_SECONDS = 8  # free tier ≈ 10 requests/min → stay under it

TODAY = today_bst()  # Bangladesh Standard Time — the app's notion of "today"

# Full article text stored on each doc for in-app reading. Firestore caps a
# document at 1 MiB; 45k chars is ~135 KB even in 3-byte Bengali UTF-8.
MAX_FULLTEXT_CHARS = 45000

SUMMARY_PROMPT = (
    "You are the strict content gatekeeper AND summarizer for a BCS exam-prep "
    "app that publishes ONLY opinion journalism.\n\n"
    "STEP 1 — CLASSIFY the article below as exactly one of:\n"
    '  "editorial" | "op-ed" | "column" | "analysis" | "news" | "other"\n'
    "STRICT GATE: you must REJECT hard news, breaking news, press releases, "
    "event coverage, and routine reporting. If the article merely reports what "
    "happened (e.g., \"Prime Minister arrives in Barishal\", accident reports, "
    "match results, announcements), it is \"news\". ONLY articles that are "
    "explicitly editorials, op-eds, columns, or analytical opinion pieces "
    "(argument, evaluation, or perspective by an author or the editorial "
    "board) may pass.\n\n"
    "If the type is \"news\" or \"other\", respond with EXACTLY this JSON and "
    'nothing else: {"article_type": "news"}\n\n'
    "STEP 2 — ONLY if the article passed the gate, produce ALL of the "
    "following for a BCS (Bangladesh Civil Service) exam candidate:\n"
    "1. SUMMARY: a comprehensive summary that captures the main theme and all "
    "vital points accurately. Do not make it too short; ensure no important "
    "information is missed. End the summary with the key learning takeaway(s) "
    "for the exam candidate.\n"
    "2. VOCABULARY: IF the original article is in English, extract 5-7 crucial "
    "advanced vocabulary words with their precise Bengali meanings. If the "
    "article is NOT in English, return an empty list.\n"
    "3. KEYWORDS: 5-8 exam-relevant key terms from the article (concepts, "
    "names, treaties, data points — in the article's own language) that a "
    "candidate should remember.\n"
    "4. PUNCHLINES: 2-4 powerful, quotable sentences (each under 30 words, in "
    "the article's own language) taken or distilled from the article, that a "
    "candidate could reuse to elevate the quality of BCS written answers.\n\n"
    "Respond ONLY with valid JSON in exactly this shape:\n"
    '{"article_type": "<editorial|op-ed|column|analysis>", '
    '"summary": "<the comprehensive summary>", '
    '"vocabulary": [{"word": "<english word>", "meaning_bn": "<precise Bengali meaning>"}], '
    '"keywords": ["<term>", "..."], '
    '"punchlines": ["<quotable sentence>", "..."]}\n\n'
    "ARTICLE (source: {source}, title: {title}):\n{text}"
)



def gnews_url(feed):
    """Google News RSS query for a feed. `domain` may include a URL path
    (site:samakal.com/opinion — verified working); `query` overrides the
    site: part entirely (multi-section sources); `terms` narrows to opinion
    pieces (None → language default, "" → no narrowing)."""
    lang = feed["lang"]
    hl, gl, ceid = ("bn", "BD", "BD:bn") if lang == "bn" else ("en-US", "US", "US:en")
    if feed.get("query"):
        q = f'{feed["query"]} when:1d'
    else:
        terms = feed.get("terms")
        if terms is None:
            terms = GNEWS_OPINION_TERMS[lang]
        q = f'site:{feed["domain"]} when:1d {terms}'.strip()
    return f"https://news.google.com/rss/search?q={quote(q)}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch_html_links(feed):
    """Scrape article links off a section page — last resort for sites with
    no RSS that Google News doesn't index (e.g. New Age)."""
    resp = requests.get(feed["page"], headers=UA_HEADERS, timeout=25)
    resp.raise_for_status()
    seen, links = set(), []
    for link in re.findall(feed["link_pattern"], resp.text):
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links



def is_recent(entry):
    """Many opinion feeds omit dates, and some send unusable ones (see
    entry_age_hours) — in both cases accept the item and rely on dedupe."""
    age = entry_age_hours(entry)
    return age is None or age <= RECENCY_HOURS



def _keyword_pattern(kw):
    """Word-boundary regex for a keyword. Plain substring matching misfired
    badly ("art" in "part", "culture" in "agriculture", "war" in "warning").
    English keywords get both boundaries plus an optional plural; Bengali
    keywords get a leading boundary only, so inflected forms still match
    (সংস্কৃতির, ভূ-রাজনীতিতে)."""
    esc = re.escape(kw.lower())
    if kw.isascii():
        return re.compile(r"\b" + esc + r"(?:e?s)?\b")
    return re.compile(r"\b" + esc)


_CATEGORY_PATTERNS = {
    category: [_keyword_pattern(kw) for kw in keywords]
    for category, keywords in TOPIC_KEYWORDS.items()
}


def match_category(text):
    """Return (category, score) for the best keyword match, or (None, 0)."""
    lowered = text.lower()
    best, best_score = None, 0
    for category, patterns in _CATEGORY_PATTERNS.items():
        score = sum(1 for p in patterns if p.search(lowered))
        if score > best_score:
            best, best_score = category, score
    return best, best_score


def collect_candidates():
    """Yield dicts: {url, title, source, lang, category} across all feeds."""
    candidates = []
    for feed in FEEDS:
        if feed["type"] == "html":
            try:
                links = fetch_html_links(feed)
            except Exception as exc:
                log.warning("Feed failed [%s]: %s", feed["name"], exc)
                continue
            for link in links[:MAX_PER_SOURCE]:
                candidates.append({
                    "url": link,
                    "title": "",  # filled from page metadata after extraction
                    "source": feed["name"],
                    "lang": feed["lang"],
                    "origin": feed["origin"],
                    "category": feed["default_category"],
                })
            log.info("Feed [%s]: kept %d item(s)", feed["name"],
                     min(len(links), MAX_PER_SOURCE))
            continue

        url = feed["url"] if feed["type"] == "rss" else gnews_url(feed)
        try:
            parsed = fetch_feed(url)
        except Exception as exc:
            log.warning("Feed failed [%s]: %s", feed["name"], exc)
            continue
        taken = 0
        for entry in parsed.entries:
            if taken >= MAX_PER_SOURCE:
                break
            if not is_recent(entry):
                continue
            title = plain_text(entry.get("title", ""))
            snippet = plain_text(entry.get("summary", ""))
            category, score = match_category(f"{title} {snippet}")
            if not category:
                if feed.get("always_include"):
                    category = feed["default_category"]
                else:
                    continue
            link = entry.get("link", "")
            if not link or not title:
                continue
            # e.g. Al Jazeera: only /opinions/ URLs are opinion pieces.
            if feed.get("path_filter") and feed["path_filter"] not in link:
                continue
            if feed["type"] == "gnews":
                link = resolve_gnews_link(link)
                if not link:
                    continue
            candidates.append({
                "url": link,
                "title": title,
                "source": feed["name"],
                "lang": feed["lang"],
                "origin": feed["origin"],
                "category": category,
            })
            taken += 1
        log.info("Feed [%s]: kept %d item(s)", feed["name"], taken)
    return candidates



def summarize(client, model, title, source, text):
    """Return the gate verdict plus the summary payload, or None when the model
    kept returning unusable output. Raises ModelUnavailable /
    DailyQuotaExhausted (via generate_json) — the caller must react to those."""
    prompt = SUMMARY_PROMPT.replace("{source}", source).replace("{title}", title) \
                           .replace("{text}", text[:30000])
    data = generate_json(client, model, prompt, temperature=0.4)
    if not isinstance(data, dict):
        return None

    article_type = str(data.get("article_type", "")).strip().lower()
    if article_type in ("news", "other"):
        return {"rejected": True, "article_type": article_type or "news"}
    if not isinstance(data.get("summary"), str) or not data["summary"]:
        return None

    vocab = [
        {"word": str(v.get("word", "")).strip(),
         "meaning_bn": str(v.get("meaning_bn", "")).strip()}
        for v in (data.get("vocabulary") or [])
        if isinstance(v, dict) and v.get("word") and v.get("meaning_bn")
    ]

    def str_list(key, cap):
        items = data.get(key) or []
        return [str(s).strip() for s in items if str(s).strip()][:cap]

    return {
        "rejected": False,
        "article_type": article_type or "analysis",
        "summary": data["summary"].strip(),
        "vocabulary": vocab,
        "keywords": str_list("keywords", 10),
        "punchlines": str_list("punchlines", 5),
    }


# ----------------------------------------------------------------------------
# Retention: keep only the current calendar month
# ----------------------------------------------------------------------------
def purge_previous_months(db, col):
    """Delete articles, archive-index entries, and PDFs from past months.
    Runs every day; only does real work on the first run of a new month."""
    month_start = f"{TODAY[:7]}-01"

    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        old_docs = list(col.where(filter=FieldFilter("date", "<", month_start)).stream())
    except ImportError:
        old_docs = list(col.where("date", "<", month_start).stream())
    if old_docs:
        batch = db.batch()
        for i, snap in enumerate(old_docs, 1):
            batch.delete(snap.reference)
            if i % 400 == 0:  # Firestore batch limit is 500 ops
                batch.commit()
                batch = db.batch()
        batch.commit()
        log.info("Purged %d article(s) from previous months", len(old_docs))

    # Trim the archive index (list of days the app shows in the archive UI).
    days_ref = db.collection("editorial_meta").document("days")
    snap = days_ref.get()
    if snap.exists:
        dates = (snap.to_dict() or {}).get("dates", [])
        kept = sorted(d for d in dates if d >= month_start)
        if kept != sorted(dates):
            days_ref.set({"dates": kept})


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY is not set.")
        sys.exit(1)

    db = init_firebase()
    gemini = genai.Client(api_key=GEMINI_API_KEY)
    col = db.collection("editorials")

    try:
        purge_previous_months(db, col)
    except Exception as exc:
        log.warning("Monthly purge failed (continuing): %s", exc)

    model = pick_model(gemini, override=GEMINI_MODEL)
    if not model:
        log.error("No usable Gemini model found for this API key.")
        sys.exit(1)
    log.info("Using Gemini model: %s", model)
    dead_models = set()

    candidates = collect_candidates()
    log.info("Collected %d candidate article(s)", len(candidates))

    processed = failed = skipped = rejected = 0
    gemini_calls = 0
    quota_gone = False
    for item in candidates:
        if processed >= MAX_TOTAL:
            log.info("Reached MAX_TOTAL=%d; stopping.", MAX_TOTAL)
            break
        if gemini_calls >= MAX_GEMINI_CALLS:
            log.info("Reached MAX_GEMINI_CALLS=%d; stopping.", MAX_GEMINI_CALLS)
            break

        doc_id = hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:20]
        if col.document(doc_id).get().exists:
            skipped += 1
            continue

        text, author, meta_title = extract_article(item["url"])
        # html-scraped feeds start title-less; a feed title that was nothing but
        # markup also cleans down to "" and falls back to the page's own title.
        if not item["title"]:
            item["title"] = meta_title
        if len(text) < MIN_ARTICLE_CHARS or not item["title"]:
            log.info("Too short / paywalled, skipping: %s",
                     (item["title"] or item["url"])[:60])
            skipped += 1
            continue
        # A mis-decoded page reads as gibberish to a student and would waste a
        # Gemini call producing a summary of nonsense. Drop it loudly instead —
        # a spike in these means a source changed how it declares its charset.
        if looks_mojibake(text) or looks_mojibake(item["title"]):
            log.warning("Garbled text (encoding), skipping: [%s] %s",
                        item["source"], item["url"][:80])
            skipped += 1
            continue

        language = detect_language(text)
        result = None
        while True:
            try:
                gemini_calls += 1
                result = summarize(gemini, model, item["title"], item["source"], text)
                break
            except ModelUnavailable:
                dead_models.add(model)
                log.warning("Model %s unavailable for this key; picking another", model)
                model = pick_model(gemini, exclude=dead_models, override=GEMINI_MODEL)
                if not model:
                    log.error("Ran out of usable Gemini models; stopping run.")
                    quota_gone = True
                    break
                log.info("Switched to Gemini model: %s", model)
            except DailyQuotaExhausted:
                log.warning(
                    "Gemini free-tier daily quota exhausted; stopping for today. "
                    "Already-saved articles are kept; quota resets at midnight "
                    "US Pacific time (~1 PM Bangladesh time)."
                )
                quota_gone = True
                break
        if quota_gone:
            break
        time.sleep(GEMINI_COOLDOWN_SECONDS)
        if not result:
            failed += 1
            continue
        if result.get("rejected"):
            rejected += 1
            log.info("Gatekeeper rejected (hard news): [%s] %s",
                     item["source"], item["title"][:70])
            continue

        col.document(doc_id).set({
            "title": item["title"],
            "articleUrl": item["url"],
            "source": item["source"],
            "origin": item["origin"],  # "national" | "international" toggle
            "author": author or None,
            "articleType": result["article_type"],  # editorial|op-ed|column|analysis
            "category": item["category"],
            "language": language,
            "summary": result["summary"],
            "vocabulary": result["vocabulary"],
            "keywords": result["keywords"],
            "punchlines": result["punchlines"],
            "fullText": text[:MAX_FULLTEXT_CHARS],
            "date": TODAY,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        processed += 1
        log.info("Saved [%s] %s", item["source"], item["title"][:70])

    if processed:
        # Archive index: one small doc listing every day that has articles,
        # so the app builds the archive UI with a single read.
        db.collection("editorial_meta").document("days").set(
            {"dates": firestore.ArrayUnion([TODAY])}, merge=True
        )

    log.info("Done. saved=%d rejected=%d skipped=%d failed=%d gemini_calls=%d",
             processed, rejected, skipped, failed, gemini_calls)
    if processed == 0 and (failed > 0 or quota_gone):
        sys.exit(1)  # nothing was saved despite trying → surface red in Actions


if __name__ == "__main__":
    main()

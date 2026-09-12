"""
Daily Editorial Pipeline — BCS Mentor
=====================================
Runs on GitHub Actions (free tier) every day at 05:00 Bangladesh time.

Flow:
  1. Pull latest items from configured RSS feeds / Google News fallbacks.
  2. Filter to the BCS-relevant topics in sources.py, dedupe against Firestore.
  3. Extract full article text (trafilatura).
  4. Gemini (free tier, rate-limited): gate by article type, then produce a
     comprehensive summary, vocabulary, exam keywords, and punchlines.
  5. Write the document — including the FULL article text for in-app reading —
     to Firestore `(default)` database, `editorials` collection.

Environment variables (see README.md):
  GEMINI_API_KEY               required; may hold several comma-separated keys
  GEMINI_API_KEY_FALLBACK      optional; tried when the keys above are blocked
  FIREBASE_SERVICE_ACCOUNT     required (raw JSON of the service-account key)
  FIREBASE_SERVICE_ACCOUNT_FILE  alternative: path to the key file (local dev)
  GEMINI_MODEL                 optional override; by default the script asks the
                               API which models this key can use and picks the
                               newest Flash model (survives model retirements)
  MAX_PER_SOURCE               default 2
  MAX_TOTAL                    default 15   (keeps Gemini free tier safe)
  MAX_GEMINI_CALLS             default 40   (real HTTP requests, not articles)
  MAX_CONSECUTIVE_FAILURES     default 5    (give up on a run going nowhere)
  GEMINI_SLEEP_BUDGET          default 240  (seconds of back-off per run)
  GEMINI_MAX_CONSECUTIVE_429   default 6    (429s that mean "no quota")

On Gemini quota, learned the hard way in September 2026: this key bills against
PREPAID CREDITS, not the free tier, and the balance ran out. Throughput fell
from ~20 requests/day to ~6 over the fortnight to 2026-08-27 and to zero from
2026-08-28, with demand flat throughout — a draining balance, not a shrinking
quota. The 429s carry "Your prepayment credits are depleted" and no "PerDay"
marker, so the old code read them as "slow down" and slept 3x40s per article:
about 40 minutes a run, which ate the workflow's timeout and reported red
without naming a cause, for 16 days.

So: a 429 can mean "too fast", "today's allowance is spent", or "this account
is out of money", and only the message body distinguishes them. Log it, and
never sleep out the last two. Hence QuotaBlocked, the back-off budget and
request-level counting below.
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
    entry_age_hours, pick_model, generate_json, gemini_keys, GeminiStats,
    ModelUnavailable, DailyQuotaExhausted, QuotaBlocked,
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")  # optional override
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "2"))
MAX_TOTAL = int(os.environ.get("MAX_TOTAL", "15"))
# Hard cap on real Gemini HTTP requests per run (saves, gate-rejections and
# in-call retries combined — GeminiStats counts requests, not articles).
#
# The old note here assumed ~1,500 free requests/day. This key was never on the
# free tier at all — it bills against prepaid credits, and that balance drained:
# ~20 calls/day until 2026-08-13, ~6/day by 2026-08-27, nothing from 2026-08-28,
# with demand flat throughout. So this cap cannot protect availability; only a
# funded account can. Treat it as a spend rail. The QuotaBlocked and
# DailyQuotaExhausted handlers end the run cleanly when money or allowance runs
# out, whichever it turns out to be.
MAX_GEMINI_CALLS = int(os.environ.get("MAX_GEMINI_CALLS", "40"))
# Give up after this many articles in a row produce nothing usable. Without it
# a key that 429s everything walked the entire candidate list, sleeping through
# the job's timeout-minutes and reporting red with no diagnosis.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "5"))
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



def summarize(client, model, title, source, text, stats=None):
    """Return the gate verdict plus the summary payload, or None when the model
    kept returning unusable output. Raises ModelUnavailable /
    DailyQuotaExhausted / QuotaBlocked (via generate_json) — the caller must
    react to those."""
    prompt = SUMMARY_PROMPT.replace("{source}", source).replace("{title}", title) \
                           .replace("{text}", text[:30000])
    data = generate_json(client, model, prompt, temperature=0.4, stats=stats)
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
# ----------------------------------------------------------------------------
# Dashboard showcase
# ----------------------------------------------------------------------------
# The dashboard's slider is a shop window, not a reader: a headline, two lines,
# and a way through to the real reading view. Letting it query `editorials`
# meant downloading whole documents — each carries the full article text, up to
# 45,000 characters, so the app can read offline — to render a preview. Six of
# those is hundreds of KB over mobile data for a strip nobody reads in place.
#
# So the pipeline publishes the strip itself: ONE small document the dashboard
# fetches in a single read, a few KB total, no article bodies anywhere near it.

SHOWCASE_NATIONAL = ["Prothom Alo", "The Daily Star"]   # in display order
SHOWCASE_INTERNATIONAL_MAX = 2
SHOWCASE_MAX = 4


def pick_showcase(saved):
    """Choose the day's showcase: the two flagship national papers, then one or
    two international pieces. Falls back to any other national source when a
    flagship published nothing worth keeping that day, so the strip is never
    short just because one feed was quiet."""
    by_source = {}
    for item in saved:
        by_source.setdefault(item["source"], []).append(item)

    chosen, used = [], set()

    def take(item):
        if item["id"] in used:
            return False
        used.add(item["id"])
        chosen.append(item)
        return True

    # 1 — the flagship national papers, in order.
    for name in SHOWCASE_NATIONAL:
        for item in by_source.get(name, [])[:1]:
            take(item)

    # 2 — top up the national half from any other national source.
    national_target = len(SHOWCASE_NATIONAL)
    if sum(1 for c in chosen if c["origin"] == "national") < national_target:
        for item in saved:
            if item["origin"] != "national":
                continue
            if sum(1 for c in chosen if c["origin"] == "national") >= national_target:
                break
            take(item)

    # 3 — one or two international pieces.
    intl = 0
    for item in saved:
        if intl >= SHOWCASE_INTERNATIONAL_MAX or len(chosen) >= SHOWCASE_MAX:
            break
        if item["origin"] == "international" and take(item):
            intl += 1

    return chosen[:SHOWCASE_MAX]


def write_showcase(db, saved):
    """Replace (not merge) the showcase document with today's strip."""
    items = pick_showcase(saved)
    if not items:
        return
    try:
        db.collection("editorial_meta").document("showcase").set({
            "date": TODAY,
            "items": items,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        })
        log.info("Showcase: %d item(s) — %s", len(items),
                 ", ".join(i["source"] for i in items))
    except Exception as exc:
        log.warning("Showcase write failed (continuing): %s", exc)


def main():
    # Keys are tried in order. Set GEMINI_API_KEY_FALLBACK (or list several
    # keys comma-separated in either variable) to keep the run producing when
    # one account stops serving — as this one did on 2026-08-28 when its
    # prepaid credits ran out, costing 16 consecutive empty days.
    keys = gemini_keys("GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK")
    if not keys:
        log.error("GEMINI_API_KEY is not set.")
        sys.exit(1)

    db = init_firebase()
    col = db.collection("editorials")

    try:
        purge_previous_months(db, col)
    except Exception as exc:
        log.warning("Monthly purge failed (continuing): %s", exc)

    stats = GeminiStats()
    key_index = -1
    gemini = None
    model = None
    dead_models = set()

    def rotate_key():
        """Advance to the next usable API key. False when none are left.

        Also performs the initial selection, so a first key that is dead on
        arrival falls through to the fallback instead of ending the run.

        The back-off budget resets per key — a fresh key deserves a fresh
        allowance — but the request count stays cumulative, so
        MAX_GEMINI_CALLS remains a run-wide ceiling.
        """
        nonlocal key_index, gemini, model, dead_models
        while key_index + 1 < len(keys):
            key_index += 1
            name, key = keys[key_index]
            gemini = genai.Client(api_key=key)
            dead_models = set()
            stats.reset_backoff()
            model = pick_model(gemini, override=GEMINI_MODEL)
            if model:
                if key_index == 0:
                    log.info("Using Gemini key %s, model %s", name, model)
                else:
                    log.warning("Rotated to Gemini key %s, model %s", name, model)
                return True
            log.error("No usable Gemini model for key %s; trying the next.", name)
        return False

    if not rotate_key():
        log.error("No usable Gemini model on any of the %d configured key(s).",
                  len(keys))
        sys.exit(1)
    log.info("%d Gemini key(s) available", len(keys))

    candidates = collect_candidates()
    log.info("Collected %d candidate article(s)", len(candidates))

    saved_today = []   # compact records for the dashboard showcase
    processed = failed = skipped = rejected = 0
    consecutive_failures = 0
    quota_gone = False
    for item in candidates:
        if processed >= MAX_TOTAL:
            log.info("Reached MAX_TOTAL=%d; stopping.", MAX_TOTAL)
            break
        if stats.requests >= MAX_GEMINI_CALLS:
            log.info("Reached MAX_GEMINI_CALLS=%d requests; stopping.",
                     MAX_GEMINI_CALLS)
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
                result = summarize(gemini, model, item["title"], item["source"],
                                   text, stats)
                break
            except ModelUnavailable:
                dead_models.add(model)
                log.warning("Model %s unavailable for this key; picking another", model)
                model = pick_model(gemini, exclude=dead_models, override=GEMINI_MODEL)
                if not model:
                    log.error("Ran out of usable Gemini models on key %s.",
                              keys[key_index][0])
                    if not rotate_key():
                        quota_gone = True
                        break
                    continue
                log.info("Switched to Gemini model: %s", model)
            except DailyQuotaExhausted:
                log.warning(
                    "Gemini free-tier daily quota exhausted on key %s. Quota "
                    "resets at midnight US Pacific time (~1 PM Bangladesh "
                    "time).", keys[key_index][0]
                )
                if not rotate_key():
                    log.warning("No key has quota left; stopping for today. "
                                "Already-saved articles are kept.")
                    quota_gone = True
                    break
            except QuotaBlocked as exc:
                # Persistent 429s with no per-day marker: the key's allowance
                # is effectively zero, not merely spent. Never sleep this out.
                log.error("Gemini key %s is blocked: %s", keys[key_index][0], exc)
                if not rotate_key():
                    log.error(
                        "No Gemini key can serve requests. This is an account "
                        "quota problem, not a code fault — check the free-tier "
                        "grant for the key in GEMINI_API_KEY, or set "
                        "GEMINI_API_KEY_FALLBACK to a key from another account."
                    )
                    quota_gone = True
                    break
        if quota_gone:
            break
        time.sleep(GEMINI_COOLDOWN_SECONDS)
        if not result:
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("%d articles in a row produced nothing usable; "
                          "stopping rather than walking the whole list.",
                          consecutive_failures)
                break
            continue
        consecutive_failures = 0
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
        saved_today.append({
            "id": doc_id,
            "title": item["title"],
            # Two lines' worth on a phone. The showcase never shows more, and
            # sending more would defeat the point of a lightweight document.
            "excerpt": " ".join(result["summary"].split())[:180],
            "source": item["source"],
            "origin": item["origin"],
            "category": item["category"],
        })
        log.info("Saved [%s] %s", item["source"], item["title"][:70])

    if processed:
        # Archive index: one small doc listing every day that has articles,
        # so the app builds the archive UI with a single read.
        db.collection("editorial_meta").document("days").set(
            {"dates": firestore.ArrayUnion([TODAY])}, merge=True
        )
        write_showcase(db, saved_today)

    log.info("Done. saved=%d rejected=%d skipped=%d failed=%d "
             "gemini_requests=%d backoff=%.0fs key=%s",
             processed, rejected, skipped, failed, stats.requests,
             stats.sleep_spent, keys[key_index][0])
    if processed == 0 and (failed > 0 or quota_gone):
        # Name the cause in the last line, so the Actions summary is readable
        # without unzipping logs.
        if quota_gone:
            log.error("FAILED: no editorials saved — Gemini quota. Last error: "
                      "%s", (stats.last_error or "n/a")[:300])
        else:
            log.error("FAILED: no editorials saved — %d article(s) failed.", failed)
        sys.exit(1)  # nothing was saved despite trying → surface red in Actions


if __name__ == "__main__":
    main()

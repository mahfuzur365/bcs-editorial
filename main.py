"""
Daily Editorial Pipeline — BCS Mentor
=====================================
Runs on GitHub Actions (free tier) every day at 06:00 Bangladesh time.

Flow:
  1. Pull latest items from configured RSS feeds / Google News fallbacks.
  2. Filter to the BCS-relevant topics in sources.py, dedupe against Firestore.
  3. Extract full article text (trafilatura).
  4. Summarize + extract vocabulary with Gemini API (free tier, rate-limited).
  5. Render a UTF-8 / Bengali-correct PDF (WeasyPrint → Pango/HarfBuzz shaping).
  6. Store the PDF (GitHub repo by default, Firebase Storage optional).
  7. Write the document to Firestore `(default)` database, `editorials` collection.

Environment variables (see README.md):
  GEMINI_API_KEY               required
  FIREBASE_SERVICE_ACCOUNT     required (raw JSON of the service-account key)
  FIREBASE_SERVICE_ACCOUNT_FILE  alternative: path to the key file (local dev)
  PDF_STORAGE                  "github" (default) | "firebase"
  FIREBASE_STORAGE_BUCKET      required only when PDF_STORAGE=firebase
  GITHUB_REPOSITORY / GITHUB_REF_NAME   set automatically by Actions
  GEMINI_MODEL                 optional override; by default the script asks the
                               API which models this key can use and picks the
                               newest Flash model (survives model retirements)
  MAX_PER_SOURCE               default 2
  MAX_TOTAL                    default 15   (keeps Gemini free tier safe)
"""

import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import time
from urllib.parse import quote
from uuid import uuid4

import feedparser
import requests
import trafilatura

import firebase_admin
from firebase_admin import credentials, firestore, storage as fb_storage

from google import genai

from sources import FEEDS, TOPIC_KEYWORDS, GNEWS_OPINION_TERMS

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("editorial")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")  # optional override
PDF_STORAGE = os.environ.get("PDF_STORAGE", "github").lower()
STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "")
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

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}

TODAY = dt.datetime.now(dt.timezone.utc).astimezone(
    dt.timezone(dt.timedelta(hours=6))  # Bangladesh Standard Time
).date().isoformat()

PDF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdfs", TODAY)

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
    "STEP 2 — ONLY if the article passed the gate:\n"
    "Write a comprehensive summary of this article that captures the main theme "
    "and all vital points accurately. Do not make it too short; ensure no "
    "important information is missed. IF the original article is in English, "
    "ALSO extract 5-7 crucial advanced vocabulary words and provide their "
    "precise Bengali meanings.\n\n"
    "Respond ONLY with valid JSON in exactly this shape:\n"
    '{"article_type": "<editorial|op-ed|column|analysis>", '
    '"summary": "<the comprehensive summary>", '
    '"vocabulary": [{"word": "<english word>", "meaning_bn": "<precise Bengali meaning>"}]}\n'
    "If the article is NOT in English, return an empty list for \"vocabulary\".\n\n"
    "ARTICLE (source: {source}, title: {title}):\n{text}"
)


# ----------------------------------------------------------------------------
# Firebase
# ----------------------------------------------------------------------------
def init_firebase():
    key_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    key_file = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "")
    if key_json:
        cred = credentials.Certificate(json.loads(key_json))
    elif key_file:
        cred = credentials.Certificate(key_file)
    else:
        log.error("No Firebase credentials. Set FIREBASE_SERVICE_ACCOUNT secret.")
        sys.exit(1)
    opts = {"storageBucket": STORAGE_BUCKET} if STORAGE_BUCKET else {}
    firebase_admin.initialize_app(cred, opts)
    return firestore.client()  # uses the (default) database ID


# ----------------------------------------------------------------------------
# Feed collection
# ----------------------------------------------------------------------------
def fetch_feed(url):
    """Fetch a feed with a browser UA (many BD sites block default clients)."""
    resp = requests.get(url, headers=UA_HEADERS, timeout=25)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


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


def resolve_gnews_link(link):
    """Google News RSS links are redirects; decode to the real article URL."""
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(link, interval=1)
        if result.get("status"):
            return result["decoded_url"]
    except Exception as exc:
        log.warning("Google News link decode failed: %s", exc)
    return None


def is_recent(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return True  # many opinion feeds omit dates; accept and rely on dedupe
    published = dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - published
    return age <= dt.timedelta(hours=RECENCY_HOURS)


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
            title = html.unescape(entry.get("title", "")).strip()
            snippet = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
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


# ----------------------------------------------------------------------------
# Article extraction
# ----------------------------------------------------------------------------
def extract_article(url):
    """Return (text, author, meta_title). Author/title come from page
    metadata; the title matters for html-scraped feeds whose section pages
    only give us bare links."""
    try:
        resp = requests.get(url, headers=UA_HEADERS, timeout=30)
        resp.raise_for_status()
        text = trafilatura.extract(
            resp.text, include_comments=False, include_tables=False,
            favor_recall=True,
        )
        author = meta_title = ""
        try:
            meta = trafilatura.extract_metadata(resp.text)
            author = (getattr(meta, "author", None) or "").strip()
            meta_title = (getattr(meta, "title", None) or "").strip()
        except Exception:
            pass
        return (text or "").strip(), author, meta_title
    except Exception as exc:
        log.warning("Extraction failed [%s]: %s", url, exc)
        return "", "", ""


def detect_language(text):
    """'bn' if a meaningful share of characters are in the Bengali block."""
    if not text:
        return "en"
    bengali = sum(1 for ch in text if "ঀ" <= ch <= "৿")
    letters = sum(1 for ch in text if ch.isalpha())
    return "bn" if letters and bengali / letters > 0.3 else "en"


# ----------------------------------------------------------------------------
# Gemini summarization
# ----------------------------------------------------------------------------
class ModelUnavailable(Exception):
    """The model returned 404 — retired/unavailable for this API key."""


class DailyQuotaExhausted(Exception):
    """Free-tier requests-per-day quota is gone; it won't reset mid-run."""


def list_usable_models(client):
    """Model names this API key can call generateContent on."""
    names = []
    try:
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").removeprefix("models/")
            actions = getattr(m, "supported_actions", None) or []
            if name and (not actions or "generateContent" in actions):
                names.append(name)
    except Exception as exc:
        log.warning("Could not list Gemini models: %s", exc)
    return names


def pick_model(client, exclude=()):
    """Pick the best available Flash model. Hardcoding a model name broke once
    already (gemini-2.5-flash was retired for new keys), so ask the API."""
    preferred = [p for p in (
        GEMINI_MODEL,               # explicit override wins if usable
        "gemini-flash-latest",      # rolling alias maintained by Google
        "gemini-flash-lite-latest",
    ) if p and p not in exclude]

    available = [n for n in list_usable_models(client) if n not in exclude]
    for p in preferred:
        if p in available:
            return p

    flashes = [
        n for n in available
        if n.startswith("gemini") and "flash" in n
        and not any(x in n for x in ("image", "tts", "live", "audio", "embed"))
    ]
    if flashes:
        def rank(n):
            m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            version = float(m.group(1)) if m else 0.0
            return (version, "lite" not in n, "preview" not in n)
        return max(flashes, key=rank)

    # Listing failed entirely → fall back to trying the rolling aliases blind.
    return preferred[0] if preferred else None


def summarize(client, model, title, source, text):
    """Return {"summary", "vocabulary"} or None (bad output after retries).
    Raises ModelUnavailable / DailyQuotaExhausted — the caller must react,
    retrying those here would only burn the daily quota for nothing."""
    prompt = SUMMARY_PROMPT.replace("{source}", source).replace("{title}", title) \
                           .replace("{text}", text[:30000])
    for attempt in range(1, 4):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config={"response_mime_type": "application/json", "temperature": 0.4},
            )
            raw = (resp.text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            data = json.loads(raw)
            article_type = str(data.get("article_type", "")).strip().lower()
            if article_type in ("news", "other"):
                return {"rejected": True, "article_type": article_type or "news"}
            if not isinstance(data.get("summary"), str) or not data["summary"]:
                raise ValueError("missing summary field")
            vocab = data.get("vocabulary") or []
            vocab = [
                {"word": str(v.get("word", "")).strip(),
                 "meaning_bn": str(v.get("meaning_bn", "")).strip()}
                for v in vocab
                if isinstance(v, dict) and v.get("word") and v.get("meaning_bn")
            ]
            return {
                "rejected": False,
                "article_type": article_type or "analysis",
                "summary": data["summary"].strip(),
                "vocabulary": vocab,
            }
        except Exception as exc:
            msg = str(exc)
            if "NOT_FOUND" in msg or "404" in msg:
                raise ModelUnavailable(model) from exc
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                if "PerDay" in msg or "per day" in msg.lower():
                    raise DailyQuotaExhausted(model) from exc
                log.warning("Gemini rate-limited (attempt %d); waiting 40s", attempt)
                time.sleep(40)
                continue
            log.warning("Gemini attempt %d failed (%s); retrying in 8s", attempt, exc)
            time.sleep(8)
    return None


# ----------------------------------------------------------------------------
# PDF generation (WeasyPrint — proper complex-script shaping for Bengali)
# ----------------------------------------------------------------------------
PDF_TEMPLATE = """
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 2cm 1.8cm; }}
  body {{
    font-family: "Noto Sans Bengali", "Noto Serif", "Noto Sans", sans-serif;
    font-size: 11.5pt; line-height: 1.65; color: #1a1a1a;
  }}
  .meta {{ color: #555; font-size: 9.5pt; margin-bottom: 4px; }}
  h1 {{ font-size: 17pt; line-height: 1.35; margin: 2px 0 10px; }}
  hr {{ border: none; border-top: 1px solid #ccc; margin: 10px 0 16px; }}
  p {{ margin: 0 0 10px; text-align: justify; }}
  .footer {{ color: #888; font-size: 8.5pt; margin-top: 24px; }}
</style>
<div class="meta">{source} &nbsp;•&nbsp; {date} &nbsp;•&nbsp; {category}</div>
<h1>{title}</h1>
<hr>
{paragraphs}
<div class="footer">Original: {url}<br>Generated by BCS Mentor daily editorial pipeline.</div>
"""


def build_pdf(item, text, out_path):
    from weasyprint import HTML  # imported lazily: needs system Pango libs
    paragraphs = "".join(
        f"<p>{html.escape(p.strip())}</p>"
        for p in text.split("\n") if p.strip()
    )
    doc_html = PDF_TEMPLATE.format(
        source=html.escape(item["source"]),
        date=TODAY,
        category=html.escape(item["category"]),
        title=html.escape(item["title"]),
        paragraphs=paragraphs,
        url=html.escape(item["url"]),
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    HTML(string=doc_html).write_pdf(out_path)


# ----------------------------------------------------------------------------
# PDF storage
# ----------------------------------------------------------------------------
def store_pdf(local_path, doc_id):
    """Return a public download URL, or None."""
    if PDF_STORAGE == "firebase" and STORAGE_BUCKET:
        try:
            bucket = fb_storage.bucket()
            blob = bucket.blob(f"editorial_pdfs/{TODAY}/{doc_id}.pdf")
            token = uuid4().hex
            blob.metadata = {"firebaseStorageDownloadTokens": token}
            blob.upload_from_filename(local_path, content_type="application/pdf")
            return (
                f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/"
                f"{quote(blob.name, safe='')}?alt=media&token={token}"
            )
        except Exception as exc:
            log.warning("Firebase Storage upload failed (%s); keeping GitHub copy", exc)
    # GitHub mode: the workflow commits pdfs/ back to the repo after this script.
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if repo:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/pdfs/{TODAY}/{doc_id}.pdf"
    return None


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

    # Old PDFs: Firebase Storage blobs in firebase mode; in github mode the
    # local folders are deleted here and the workflow's commit removes them
    # from the repo (raw.githubusercontent URLs die with them — their
    # Firestore docs are already gone, so nothing links to them).
    if PDF_STORAGE == "firebase" and STORAGE_BUCKET:
        try:
            bucket = fb_storage.bucket()
            for blob in bucket.list_blobs(prefix="editorial_pdfs/"):
                parts = blob.name.split("/")
                if len(parts) >= 2 and parts[1] < month_start:
                    blob.delete()
        except Exception as exc:
            log.warning("Storage purge failed: %s", exc)
    pdf_root = os.path.dirname(PDF_DIR)
    if os.path.isdir(pdf_root):
        for name in os.listdir(pdf_root):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name) and name < month_start:
                shutil.rmtree(os.path.join(pdf_root, name), ignore_errors=True)
                log.info("Removed old PDF folder pdfs/%s", name)


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

    model = pick_model(gemini)
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
        if not item["title"]:
            item["title"] = meta_title  # html-scraped feeds start title-less
        if len(text) < MIN_ARTICLE_CHARS or not item["title"]:
            log.info("Too short / paywalled, skipping: %s",
                     (item["title"] or item["url"])[:60])
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
                model = pick_model(gemini, exclude=dead_models)
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

        pdf_url = None
        try:
            local_pdf = os.path.join(PDF_DIR, f"{doc_id}.pdf")
            build_pdf(item, text, local_pdf)
            pdf_url = store_pdf(local_pdf, doc_id)
        except Exception as exc:
            log.warning("PDF step failed for %s: %s", item["title"][:60], exc)

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
            "pdfUrl": pdf_url,
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

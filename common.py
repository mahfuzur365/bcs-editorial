"""
Shared machinery for the two content pipelines.

  main.py     — daily editorials (opinion journalism, summarised for reading)
  updates.py  — weekly current affairs (hard facts, for memorising)

Both fetch feeds, resolve Google News redirects, pull article text and talk to
Gemini, so that code lives here rather than in two drifting copies. The
encoding fix in `extract_article` is the clearest reason: it was a bug in one
pipeline, and the second would have inherited it verbatim.
"""

import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
from urllib.parse import quote

import feedparser
import requests
import trafilatura

import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}

BST = dt.timezone(dt.timedelta(hours=6))  # Bangladesh Standard Time


def today_bst():
    """Today's date as Bangladesh sees it — the app's notion of 'today'."""
    return dt.datetime.now(dt.timezone.utc).astimezone(BST).date().isoformat()


# ----------------------------------------------------------------------------
# Calendar buckets: day → week → month → year
# ----------------------------------------------------------------------------
# The archive nests items by week inside month inside year. A "week" here is
# SATURDAY→FRIDAY, not the ISO Monday→Sunday one, because that is the app's own
# study week (see MEMORIZE_WEEK / weekIndex in the app's memorizeMeta.js) and
# শুক্রবার is its revision day. An ISO week would put Friday's revision in a
# different bucket from the শনি–বৃহস্পতি work it reviews.

def week_start(date_iso):
    """The Saturday that opens the study week containing `date_iso`."""
    d = dt.date.fromisoformat(date_iso)
    # date.weekday(): Mon=0 … Sat=5, Sun=6. Saturday is the week's first day.
    return (d - dt.timedelta(days=(d.weekday() - 5) % 7)).isoformat()


def week_end(date_iso):
    """The Friday that closes the study week containing `date_iso`."""
    start = dt.date.fromisoformat(week_start(date_iso))
    return (start + dt.timedelta(days=6)).isoformat()


def period_fields(date_iso):
    """Archive buckets for one item, all derivable from its own date.

    A week that straddles two months belongs to both: each item is filed under
    the month of ITS OWN date, so "August" contains exactly the items dated in
    August, and the straddling week appears in both months holding only its
    respective days. That is what a reader expects from "আগস্টের কারেন্ট
    অ্যাফেয়ার্স" and avoids arguing about which month owns a split week.
    """
    return {
        "date": date_iso,
        "year": int(date_iso[:4]),
        "month": date_iso[:7],          # "2026-08"
        "weekStart": week_start(date_iso),
        "weekEnd": week_end(date_iso),
    }


# ----------------------------------------------------------------------------
# Firebase
# ----------------------------------------------------------------------------
def init_firebase():
    """Initialise the Admin SDK once per process (safe to call from either
    pipeline; a second call in the same process would raise)."""
    if not firebase_admin._apps:
        key_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
        key_file = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "")
        if key_json:
            cred = credentials.Certificate(json.loads(key_json))
        elif key_file:
            cred = credentials.Certificate(key_file)
        else:
            log.error("No Firebase credentials. Set FIREBASE_SERVICE_ACCOUNT secret.")
            sys.exit(1)
        firebase_admin.initialize_app(cred)
    return firestore.client()  # uses the (default) database ID


# ----------------------------------------------------------------------------
# Text hygiene
# ----------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")

# A UTF-8 lead byte (0xC2-0xEF) followed by a continuation byte (0x80-0xBF),
# both surfacing as Latin-1 supplement characters.
_MOJIBAKE_RE = re.compile("[\u00C2-\u00EF][\u0080-\u00BF]")


def plain_text(raw):
    """Strip markup from a feed field and normalise its whitespace.

    Feed titles are supposed to be plain text, but some papers ship markup
    inside <title> — The Daily Star wraps every one in an anchor. Unescape
    first (feedparser may return the markup raw or entity-encoded), then strip
    tags, then collapse whitespace."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def looks_mojibake(text):
    """Detect UTF-8 that was decoded as Latin-1 somewhere upstream.

    Requires both a count and a density: a stray pair in otherwise clean text
    is a quotation-mark artefact, not a broken charset. Verified against real
    Bengali and Hindi output plus French, Spanish, German, Portuguese and
    English samples."""
    if not text:
        return False
    sample = text[:4000]
    hits = len(_MOJIBAKE_RE.findall(sample))
    return hits >= 12 and hits / len(sample) > 0.02


def detect_language(text):
    """'bn' if a meaningful share of characters are in the Bengali block."""
    if not text:
        return "en"
    bengali = sum(1 for ch in text if "ঀ" <= ch <= "৿")
    letters = sum(1 for ch in text if ch.isalpha())
    return "bn" if letters and bengali / letters > 0.3 else "en"


# ----------------------------------------------------------------------------
# Feeds
# ----------------------------------------------------------------------------
def fetch_feed(url):
    """Fetch a feed with a browser UA (many BD sites block default clients)."""
    resp = requests.get(url, headers=UA_HEADERS, timeout=25)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def gnews_search_url(query, lang="bn", window="1d"):
    """Google News RSS search URL for a raw query string."""
    hl, gl, ceid = ("bn", "BD", "BD:bn") if lang == "bn" else ("en-US", "US", "US:en")
    q = f"{query} when:{window}".strip()
    return f"https://news.google.com/rss/search?q={quote(q)}&hl={hl}&gl={gl}&ceid={ceid}"


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


def entry_age_hours(entry):
    """Hours since publication, or None when the feed omits a usable date.

    Guards against RFC-822 two-digit years ("Mon, 03 Aug 26"), which a parser
    can read as 1926 — that would make an entire feed look ancient and vanish.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed or parsed[0] < 2000:
        return None
    published = dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 3600


# ----------------------------------------------------------------------------
# Article extraction
# ----------------------------------------------------------------------------
def extract_article(url):
    """Return (text, author, meta_title) for an article URL.

    Content is handed to trafilatura as RAW BYTES, never `resp.text`. When a
    server sends `Content-Type: text/html` with no charset parameter, requests
    follows the HTTP spec and decodes as ISO-8859-1, so a UTF-8 Bengali page
    comes back as mojibake and every downstream field is garbage. Bytes let
    trafilatura honour the page's own <meta charset> declaration."""
    try:
        resp = requests.get(url, headers=UA_HEADERS, timeout=30)
        resp.raise_for_status()
        raw = resp.content
        text = trafilatura.extract(
            raw, include_comments=False, include_tables=False, favor_recall=True,
        )
        author = meta_title = ""
        try:
            meta = trafilatura.extract_metadata(raw)
            author = (getattr(meta, "author", None) or "").strip()
            meta_title = (getattr(meta, "title", None) or "").strip()
        except Exception:
            pass
        return (text or "").strip(), author, plain_text(meta_title)
    except Exception as exc:
        log.warning("Extraction failed [%s]: %s", url, exc)
        return "", "", ""


# ----------------------------------------------------------------------------
# Gemini
# ----------------------------------------------------------------------------
class ModelUnavailable(Exception):
    """The model returned 404 — retired/unavailable for this API key."""


class DailyQuotaExhausted(Exception):
    """Free-tier requests-per-day quota is gone; it won't reset mid-run."""


class QuotaBlocked(Exception):
    """Gemini 429'd persistently enough that this key plainly has no usable
    quota right now.

    Distinct from DailyQuotaExhausted, which is Google explicitly naming the
    per-day allowance as spent. An account with no money left (a depleted
    prepaid balance) 429s from its very first request with no "PerDay" marker
    in the message, so the old code read it as "slow down" and slept 40s at a
    time — roughly 40 minutes across a run. That silently consumed the job's
    whole timeout-minutes and reported red without ever naming the cause.
    """


# Total seconds one run may spend sleeping on Gemini back-off. Deliberately far
# below the workflows' timeout-minutes so a dead key fails fast and legibly
# instead of grinding out the entire job.
GEMINI_SLEEP_BUDGET_SECONDS = int(os.environ.get("GEMINI_SLEEP_BUDGET", "240"))

# Consecutive 429s that mean "this key has no allowance", not "too fast".
GEMINI_MAX_CONSECUTIVE_429 = int(os.environ.get("GEMINI_MAX_CONSECUTIVE_429", "6"))

GEMINI_RATE_LIMIT_SLEEP = 40


def _short(msg, limit=300):
    """One-line, truncated error text. Gemini errors are multi-line JSON, and
    the quota id buried in them is the only thing that separates "too fast"
    from "no allowance at all" — so it must reach the log, but not flood it."""
    return " ".join(str(msg).split())[:limit]


class GeminiStats:
    """Per-run Gemini accounting, shared across every generate_json call.

    `requests` counts real HTTP requests, not logical summarise calls. The old
    per-article counter incremented once per summarize() while generate_json
    retried up to 3 times inside, under-reporting by up to 3x — so a cap of 40
    quietly permitted ~120 requests against the free tier.
    """

    def __init__(self):
        self.requests = 0
        self.sleep_spent = 0.0
        self.consecutive_429 = 0
        self.last_error = ""

    def reset_backoff(self):
        """Fresh back-off budget — called after rotating to another API key."""
        self.sleep_spent = 0.0
        self.consecutive_429 = 0

    def nap(self, seconds, why):
        """Sleep against the run's shared back-off budget, or give up."""
        remaining = GEMINI_SLEEP_BUDGET_SECONDS - self.sleep_spent
        if remaining <= 0:
            raise QuotaBlocked(
                "back-off budget of %ds is spent; last error: %s"
                % (GEMINI_SLEEP_BUDGET_SECONDS, _short(self.last_error, 200))
            )
        secs = min(seconds, remaining)
        log.warning("Gemini %s; waiting %.0fs (back-off %.0fs/%ds used)",
                    why, secs, self.sleep_spent + secs,
                    GEMINI_SLEEP_BUDGET_SECONDS)
        time.sleep(secs)
        self.sleep_spent += secs


def gemini_keys(*env_names):
    """API keys to try, in order, de-duplicated, as (env_name, key) pairs.

    Each variable may hold several comma-separated keys, so a spare can be
    added as a secret without a code change. Returning the variable's name
    alongside lets logs say which key is in play without printing the key.
    """
    keys, seen = [], set()
    for name in env_names:
        for raw in os.environ.get(name, "").split(","):
            key = raw.strip()
            if key and key not in seen:
                seen.add(key)
                keys.append((name, key))
    return keys


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


def pick_model(client, exclude=(), override=""):
    """Pick the best available Flash model. Hardcoding a model name broke once
    already (gemini-2.5-flash was retired for new keys), so ask the API."""
    preferred = [p for p in (
        override,
        "gemini-flash-latest",
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

    return preferred[0] if preferred else None


def generate_json(client, model, prompt, temperature=0.3, attempts=3, stats=None):
    """Call Gemini and parse a JSON response, retrying transient failures.

    Raises ModelUnavailable / DailyQuotaExhausted / QuotaBlocked so the caller
    can switch models, rotate keys or stop the run — retrying those here only
    burns quota and wall-clock.

    `stats` carries the run-wide request count and back-off budget; pass the
    same instance on every call so the budget is actually shared.
    """
    stats = stats if stats is not None else GeminiStats()
    for attempt in range(1, attempts + 1):
        try:
            stats.requests += 1
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config={"response_mime_type": "application/json",
                        "temperature": temperature},
            )
            stats.consecutive_429 = 0  # the key is serving again
            raw = (resp.text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            return json.loads(raw)
        except Exception as exc:
            msg = str(exc)
            stats.last_error = msg
            if "NOT_FOUND" in msg or "404" in msg:
                raise ModelUnavailable(model) from exc
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                stats.consecutive_429 += 1
                # Log the body. What is inside it is the difference between
                # "slow down", "today's allowance is spent" and "this account
                # is out of money", and the old fixed log line discarded all
                # three — which is precisely why 16 dead runs looked like a
                # mystery timeout.
                log.warning("Gemini 429 (attempt %d, %d consecutive): %s",
                            attempt, stats.consecutive_429, _short(msg))
                # A depleted prepaid balance is billed, not windowed: it will
                # not recover in 40s, at midnight Pacific, or ever, until
                # someone tops the account up. Fail on the first one.
                low = msg.lower()
                if "credits are depleted" in low or "prepayment credit" in low:
                    raise QuotaBlocked(
                        "Gemini prepaid credits are depleted on this key — "
                        "top the project up at https://ai.studio/projects, or "
                        "configure a fallback key. Original error: %s"
                        % _short(msg, 200)
                    ) from exc
                if "PerDay" in msg or "per day" in msg.lower():
                    raise DailyQuotaExhausted(model) from exc
                if stats.consecutive_429 >= GEMINI_MAX_CONSECUTIVE_429:
                    raise QuotaBlocked(
                        "%d consecutive 429s on %s; last error: %s"
                        % (stats.consecutive_429, model, _short(msg, 200))
                    ) from exc
                stats.nap(GEMINI_RATE_LIMIT_SLEEP, "rate-limited")
                continue
            log.warning("Gemini attempt %d failed: %s", attempt, _short(msg))
            stats.nap(8, "transient error")
    return None

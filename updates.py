"""
Current-Affairs Pipeline — BCS Mentor
=====================================
Feeds the app's সাম্প্রতিক আপডেট section.

COLLECTS DAILY, PUBLISHES WEEKLY. The run gathers a few candidates each morning
and files them as drafts; you approve a week's worth at /admin/updates whenever
suits — typically Friday, the app's revision day, so the week's current affairs
reach students exactly as they sit down to review.

Why not one weekly run? Measured over all 18 queries on a real week: the week
produced 1,185 matching articles, and a single run keeping 3 per query would
sample 54 of them (4.6%). Worse, Google News caps its RSS response at 100 items
and 5 of these queries hit that ceiling at when:7d, so a weekly run cannot see
the rest of the week at all. Seven daily runs sample roughly seven times as
much, and a failed day costs one day instead of the entire week — a weekly run
that fails is unrecoverable, because the next window will not reach back.

Distinct from main.py in purpose: that pipeline collects OPINION to be read;
this one collects FACTS to be memorised. A student will commit these to memory
as true, so the bar for what gets through is deliberately high and everything
lands as a DRAFT for a human to approve.

Flow:
  1. Run question-shaped searches (see update_sources.py) + wire feeds.
  2. Drop anything matching BLOCK_PATTERNS on the title alone — no API cost.
  3. Dedupe against Firestore by URL hash.
  4. Gemini scores exam-relevance 0-10 against a rubric and, if it passes,
     writes a Bengali headline + 3-5 memorisable bullet points.
  5. Reject anything without a concrete fact (a number, date or proper noun).
  6. Write to `updates` with status="draft" for admin approval.

Environment variables:
  UPDATES_GEMINI_API_KEY       preferred — a key from a SEPARATE Google AI
                               Studio project, so this run cannot eat the daily
                               editorial pipeline's quota (see below)
  GEMINI_API_KEY               fallback when the above is unset
  FIREBASE_SERVICE_ACCOUNT     required (raw JSON of the service-account key)
  GEMINI_MODEL                 optional model override
  UPDATES_MAX_ITEMS            default 5    (drafts saved per DAILY run,
                                             so roughly 35 to choose from a week)
  UPDATES_MAX_GEMINI_CALLS     default 30   (per run, not per week)
  UPDATES_MIN_SCORE            default 6    (0-10 relevance bar)
  UPDATES_AUTO_PUBLISH         default "0"  ("1" skips the approval step)

On quota: Gemini's free tier is metered PER GOOGLE CLOUD PROJECT, not per key.
A second key minted inside the same project shares the same daily allowance and
buys nothing — it must come from a different project to give this pipeline
independent headroom. In practice this pipeline's key comes from a different
Google ACCOUNT, which is stronger still, and is why it kept working from
2026-08-28 while the editorial key's free-tier grant decayed to zero.

Treat the free tier as small and unstable rather than as a documented number.
On the editorial key it fell from ~20 requests/day to ~6 over a fortnight and
then to zero, with demand flat throughout. So: count real requests (GeminiStats
does, not logical calls), budget back-off (GEMINI_SLEEP_BUDGET), and stop on
QuotaBlocked instead of sleeping through the job's timeout.
"""

import datetime as dt
import hashlib
import os
import re
import sys
import time

from google import genai

from common import (
    log, init_firebase, fetch_feed, gnews_search_url, resolve_gnews_link,
    extract_article, plain_text, looks_mojibake, entry_age_hours, today_bst,
    period_fields, pick_model, generate_json, gemini_keys, GeminiStats,
    ModelUnavailable, DailyQuotaExhausted, QuotaBlocked,
)
from update_sources import (
    EXAM_QUERIES, NEWS_FEEDS, BLOCK_PATTERNS, SEARCH_WINDOW,
)

from firebase_admin import firestore

# Prefer this pipeline's own key; fall back to the shared one so the run still
# works before the second secret is added. Either variable may hold several
# comma-separated keys, and they are tried in order — a key whose free-tier
# grant has been zeroed 429s every request, and rotating past it beats
# returning an empty week.
API_KEYS = gemini_keys("UPDATES_GEMINI_API_KEY", "GEMINI_API_KEY")
OWN_KEY = bool(os.environ.get("UPDATES_GEMINI_API_KEY"))

MAX_ITEMS = int(os.environ.get("UPDATES_MAX_ITEMS", "5"))
MAX_GEMINI_CALLS = int(os.environ.get("UPDATES_MAX_GEMINI_CALLS", "30"))
MIN_SCORE = int(os.environ.get("UPDATES_MIN_SCORE", "6"))
AUTO_PUBLISH = os.environ.get("UPDATES_AUTO_PUBLISH", "0") == "1"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")

# Give up after this many articles in a row produce nothing usable, rather
# than walking the whole candidate list on a key that answers every request
# with a 429.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "5"))

MAX_PER_QUERY = 3          # candidates per search, per run
MAX_AGE_HOURS = 3 * 24     # wire-feed cutoff; matches the 2-day search window
MIN_ARTICLE_CHARS = 400    # news pieces are shorter than editorials
GEMINI_COOLDOWN_SECONDS = 8

# ----------------------------------------------------------------------------
# The relevance gate.
#
# Written as a rubric with worked examples rather than an instruction, because
# "is this important?" and "would this be an exam question?" are very different
# judgements and the model defaults to the first one. Every example below is a
# real shape of question that appears in BCS/bank preliminaries.
# ----------------------------------------------------------------------------
RELEVANCE_PROMPT = (
    "You prepare current-affairs notes for candidates sitting Bangladeshi "
    "competitive job exams (BCS preliminary, bank recruitment, NTRCA). Your "
    "readers MEMORISE what you write, so a fact that is vague, undated or "
    "unverifiable is worse than no fact at all.\n\n"

    "STEP 1 — SCORE this article 0-10 on: how likely is a fact from it to "
    "appear as a multiple-choice question in the next two years?\n\n"

    "SCORE 8-10 — the staple of current-affairs questions:\n"
    "  • Who now holds an office (chief justice, governor, UN secretary-general,\n"
    "    vice-chancellor, ambassador, chairman of a commission)\n"
    "  • A country's rank or score in a named global index or report\n"
    "  • A named award and its winner (Nobel, Ekushey Padak, Booker, Oscar)\n"
    "  • A treaty, MoU or summit: who signed it, where, when, about what\n"
    "  • A first/largest/longest achievement, or a broken record\n"
    "  • A named national project inaugurated (bridge, tunnel, metro, plant)\n"
    "  • A new law, ordinance, commission or institution established\n"
    "  • Headline economic figures: reserves, remittance, GDP growth, inflation\n"
    "  • A world championship or Olympic title and who won it\n\n"

    "SCORE 4-7 — real substance but weaker odds: policy decisions without "
    "numbers, bilateral visits, sector reports, notable deaths.\n\n"

    "SCORE 0-3 — REJECT. Never examinable, no matter how prominent the news:\n"
    "  • Crime, accidents, fires, court hearings, arrests\n"
    "  • Party politics, rallies, statements, allegations, resignation rumours\n"
    "  • Ordinary match reports, previews, transfers, team selections\n"
    "  • Weather, traffic, local grievances, human-interest stories\n"
    "  • Opinion and analysis with no verifiable fact of record\n"
    "  • Anything whose central claim is speculation about the future\n\n"

    "STEP 2 — ONLY if the score is {min_score} or above, write the note in "
    "BENGALI:\n"
    "  title  : one factual Bengali headline, under 15 words, naming the "
    "concrete development (not a question, not a teaser)\n"
    "  points : 3-5 Bengali bullet points, each a SINGLE self-contained fact a "
    "candidate could be asked about. Every point must carry something "
    "memorisable — a name, number, rank, date or place. Write out figures "
    "(\"১৭ কোটি\"), and give full names on first use. Do NOT write commentary, "
    "context-only sentences, or anything the article does not state.\n"
    "  category : exactly one of bangladesh | international | science | "
    "economy | environment | sports\n\n"

    "If the score is below {min_score}, respond with EXACTLY: "
    '{"score": <number>, "reason": "<max 10 words>"}\n\n'

    "Otherwise respond ONLY with valid JSON in exactly this shape:\n"
    '{"score": <number>, "category": "<category>", '
    '"title": "<bengali headline>", '
    '"points": ["<bengali fact>", "<bengali fact>", "<bengali fact>"]}\n\n'

    "ARTICLE (source: {source}, headline: {headline}):\n{text}"
)

# A note nobody can be examined on is a note with no number, no date and no
# proper noun. Bengali digits, Latin digits, or a capitalised/Bengali name.
_FACT_RE = re.compile(r"[0-9০-৯]|[A-Z][a-z]{2,}")


def has_concrete_fact(points):
    """At least half the bullets must contain something memorisable."""
    if not points:
        return False
    solid = sum(1 for p in points if _FACT_RE.search(p))
    return solid >= max(1, len(points) // 2)


def _block_pattern(p):
    """ASCII patterns match on word boundaries so short ones ("vs", "nba")
    cannot fire inside an unrelated word or a URL. Bengali has no word
    boundaries usable by `re`, so those match as substrings — which is correct
    for a language that glues suffixes onto stems (নিহতের, গ্রেপ্তারকৃত)."""
    if p.isascii():
        return re.compile(r"\b" + re.escape(p.lower()) + r"\b")
    return re.compile(re.escape(p))


_BLOCK_RES = [_block_pattern(p) for p in BLOCK_PATTERNS]


def blocked(title):
    """Cheap title-level rejection before any network or API cost."""
    low = title.lower()
    return any(r.search(low) for r in _BLOCK_RES)


def collect_candidates():
    """Yield {url, headline, source, category} from searches and wire feeds."""
    seen_urls = set()
    candidates = []

    def add(url, headline, source, category):
        if not url or not headline or url in seen_urls:
            return False
        if blocked(headline):
            log.info("Blocked by pattern: %s", headline[:70])
            return False
        seen_urls.add(url)
        candidates.append({"url": url, "headline": headline,
                           "source": source, "category": category})
        return True

    # 1 — question-shaped searches
    for q in EXAM_QUERIES:
        url = gnews_search_url(q["query"], lang=q["lang"], window=SEARCH_WINDOW)
        try:
            parsed = fetch_feed(url)
        except Exception as exc:
            log.warning("Query failed [%s]: %s", q["name"], exc)
            continue
        taken = 0
        for entry in parsed.entries:
            if taken >= MAX_PER_QUERY:
                break
            headline = plain_text(entry.get("title", ""))
            if not headline or blocked(headline):
                continue
            link = resolve_gnews_link(entry.get("link", ""))
            if add(link, headline, q["name"], q["category"]):
                taken += 1
        log.info("Query [%s]: %d candidate(s)", q["name"], taken)

    # 2 — wire feeds
    for feed in NEWS_FEEDS:
        try:
            parsed = fetch_feed(feed["url"])
        except Exception as exc:
            log.warning("Feed failed [%s]: %s", feed["name"], exc)
            continue
        taken = 0
        for entry in parsed.entries:
            if taken >= MAX_PER_QUERY:
                break
            age = entry_age_hours(entry)
            if age is not None and age > MAX_AGE_HOURS:
                continue
            headline = plain_text(entry.get("title", ""))
            if add(entry.get("link", ""), headline, feed["name"], feed["category"]):
                taken += 1
        log.info("Feed [%s]: %d candidate(s)", feed["name"], taken)

    return candidates


def build_note(client, model, item, text, stats=None):
    """Ask Gemini to score and, if it passes, write the note."""
    prompt = (RELEVANCE_PROMPT
              .replace("{min_score}", str(MIN_SCORE))
              .replace("{source}", item["source"])
              .replace("{headline}", item["headline"])
              .replace("{text}", text[:16000]))
    data = generate_json(client, model, prompt, temperature=0.2, stats=stats)
    if not isinstance(data, dict):
        return None

    try:
        score = int(float(data.get("score", 0)))
    except (TypeError, ValueError):
        score = 0
    if score < MIN_SCORE:
        return {"rejected": True, "score": score,
                "reason": str(data.get("reason", ""))[:60]}

    title = plain_text(str(data.get("title", "")))
    points = [plain_text(str(p)) for p in (data.get("points") or [])]
    points = [p for p in points if len(p) > 10][:5]
    category = str(data.get("category", "")).strip().lower()

    if not title or len(points) < 2:
        return None
    return {"rejected": False, "score": score, "title": title,
            "points": points, "category": category}


# ----------------------------------------------------------------------------
# Archive: index + one-year retention
# ----------------------------------------------------------------------------
# `updates_meta/index` is a single small document listing every month and week
# that holds published items. The archive UI renders its whole year → month →
# week tree from that ONE read, then fetches a month's items on demand — the
# same trick the editorial pipeline uses for its day list.

RETENTION_DAYS = 365


def touch_archive_index(db, fields):
    """Record this item's month and week in the archive index."""
    try:
        db.collection("updates_meta").document("index").set({
            "months": firestore.ArrayUnion([fields["month"]]),
            "weeks": firestore.ArrayUnion([fields["weekStart"]]),
        }, merge=True)
    except Exception as exc:
        log.warning("Archive index update failed (continuing): %s", exc)


def purge_expired(db, col):
    """Drop items older than a year, and prune the index to match.

    A year is the useful horizon: a candidate preparing for an exam revises the
    last twelve months of current affairs, and anything older belongs in the
    static syllabus rather than a news feed. Runs every day, but only does work
    once items actually age out.
    """
    cutoff = (dt.date.fromisoformat(today_bst()) - dt.timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            old = list(col.where(filter=FieldFilter("date", "<", cutoff)).stream())
        except ImportError:
            old = list(col.where("date", "<", cutoff).stream())
    except Exception as exc:
        log.warning("Retention query failed (continuing): %s", exc)
        return

    if old:
        batch = db.batch()
        for i, snap in enumerate(old, 1):
            batch.delete(snap.reference)
            if i % 400 == 0:      # Firestore batch limit is 500 ops
                batch.commit()
                batch = db.batch()
        batch.commit()
        log.info("Purged %d update(s) older than %d days", len(old), RETENTION_DAYS)

    # Prune index entries whose period is entirely past the cutoff.
    try:
        ref = db.collection("updates_meta").document("index")
        snap = ref.get()
        if not snap.exists:
            return
        data = snap.to_dict() or {}
        months = sorted(m for m in (data.get("months") or []) if m >= cutoff[:7])
        weeks = sorted(w for w in (data.get("weeks") or []) if w >= cutoff)
        if months != sorted(data.get("months") or []) or weeks != sorted(data.get("weeks") or []):
            ref.set({"months": months, "weeks": weeks}, merge=True)
    except Exception as exc:
        log.warning("Index prune failed (continuing): %s", exc)


def main():
    if not API_KEYS:
        log.error("No Gemini key. Set UPDATES_GEMINI_API_KEY (preferred) or GEMINI_API_KEY.")
        sys.exit(1)
    if OWN_KEY:
        log.info("Using this pipeline's own Gemini key.")
    else:
        log.warning(
            "Falling back to the shared GEMINI_API_KEY — this run competes with "
            "the daily editorial pipeline for the same quota. Add an "
            "UPDATES_GEMINI_API_KEY secret from a SEPARATE AI Studio project."
        )

    db = init_firebase()
    col = db.collection("updates")

    purge_expired(db, col)

    stats = GeminiStats()
    key_index = -1
    client = None
    model = None
    dead_models = set()

    def rotate_key():
        """Advance to the next usable API key. False when none are left.

        Also performs the initial selection, so a first key that is dead on
        arrival falls through to the fallback instead of ending the run.
        """
        nonlocal key_index, client, model, dead_models
        while key_index + 1 < len(API_KEYS):
            key_index += 1
            name, key = API_KEYS[key_index]
            client = genai.Client(api_key=key)
            dead_models = set()
            stats.reset_backoff()
            model = pick_model(client, override=GEMINI_MODEL)
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
                  len(API_KEYS))
        sys.exit(1)
    log.info("%d Gemini key(s) available", len(API_KEYS))

    candidates = collect_candidates()
    log.info("Collected %d candidate article(s)", len(candidates))

    saved = rejected = skipped = failed = 0
    consecutive_failures = 0
    stop = False

    for item in candidates:
        if saved >= MAX_ITEMS:
            log.info("Reached UPDATES_MAX_ITEMS=%d; stopping.", MAX_ITEMS)
            break
        if stats.requests >= MAX_GEMINI_CALLS:
            log.info("Reached UPDATES_MAX_GEMINI_CALLS=%d requests; stopping.",
                     MAX_GEMINI_CALLS)
            break

        doc_id = hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:20]
        if col.document(doc_id).get().exists:
            skipped += 1
            continue

        text, _author, meta_title = extract_article(item["url"])
        if not item["headline"]:
            item["headline"] = meta_title
        if len(text) < MIN_ARTICLE_CHARS:
            skipped += 1
            continue
        if looks_mojibake(text):
            log.warning("Garbled text (encoding), skipping: %s", item["url"][:80])
            skipped += 1
            continue

        note = None
        while True:
            try:
                note = build_note(client, model, item, text, stats)
                break
            except ModelUnavailable:
                dead_models.add(model)
                model = pick_model(client, exclude=dead_models, override=GEMINI_MODEL)
                if not model:
                    log.error("Ran out of usable Gemini models on key %s.",
                              API_KEYS[key_index][0])
                    if not rotate_key():
                        stop = True
                        break
                    continue
                log.info("Switched to Gemini model: %s", model)
            except DailyQuotaExhausted:
                log.warning("Gemini daily quota exhausted on key %s.",
                            API_KEYS[key_index][0])
                if not rotate_key():
                    log.warning("No key has quota left; stopping. "
                                "Saved items are kept.")
                    stop = True
                    break
            except QuotaBlocked as exc:
                # Persistent 429s with no per-day marker: this key's allowance
                # is effectively zero, not merely spent. Never sleep it out.
                log.error("Gemini key %s is blocked: %s",
                          API_KEYS[key_index][0], exc)
                if not rotate_key():
                    log.error("No Gemini key can serve requests — check the "
                              "free-tier grant on these accounts.")
                    stop = True
                    break
        if stop:
            break
        time.sleep(GEMINI_COOLDOWN_SECONDS)

        if not note:
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("%d articles in a row produced nothing usable; "
                          "stopping rather than walking the whole list.",
                          consecutive_failures)
                break
            continue
        consecutive_failures = 0
        if note.get("rejected"):
            rejected += 1
            log.info("Not exam-relevant (score %d, %s): %s",
                     note["score"], note.get("reason", ""), item["headline"][:60])
            continue
        if not has_concrete_fact(note["points"]):
            rejected += 1
            log.info("No memorisable fact, skipping: %s", note["title"][:60])
            continue

        col.document(doc_id).set({
            "title": note["title"],
            "points": note["points"],
            "category": note["category"] or item["category"],
            "source": item["source"],
            "sourceUrl": item["url"],
            "score": note["score"],
            # Drafts are invisible to students until an admin publishes them —
            # these become memorised facts, so a human sees them first.
            "status": "published" if AUTO_PUBLISH else "draft",
            # date / year / month / weekStart / weekEnd — the archive buckets.
            **period_fields(today_bst()),
            "publishedAt": firestore.SERVER_TIMESTAMP,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        saved += 1
        touch_archive_index(db, period_fields(today_bst()))
        log.info("Saved (score %d) [%s] %s", note["score"], note["category"], note["title"][:70])

    log.info("Done. saved=%d rejected=%d skipped=%d failed=%d "
             "gemini_requests=%d backoff=%.0fs key=%s status=%s",
             saved, rejected, skipped, failed, stats.requests,
             stats.sleep_spent, API_KEYS[key_index][0],
             "published" if AUTO_PUBLISH else "draft")
    if saved == 0 and (failed > 0 or stop):
        if stop:
            log.error("FAILED: no updates saved — Gemini quota. Last error: %s",
                      (stats.last_error or "n/a")[:300])
        else:
            log.error("FAILED: no updates saved — %d article(s) failed.", failed)
        sys.exit(1)  # nothing saved despite trying → surface red in Actions


if __name__ == "__main__":
    main()

# BCS Mentor — Daily Editorial Backend (100% free)

Automated pipeline: every day at **6:00 AM Bangladesh time**, GitHub Actions
fetches **editorials/opinion pieces only** (a Gemini gate rejects hard news)
from Bangladeshi + international sources, groups them into 8 BCS topics
(Economy, World Politics, Geopolitics, War & Peace, Environment, Agriculture,
Public Policy, Art/Literature/Culture), and writes everything — comprehensive
summary with learning takeaways, English vocabulary with Bengali meanings,
exam keywords, quotable punchlines, and the **full article text for in-app
reading** — to **Firestore (Spark plan, `(default)` database)**.

```
GitHub Actions (cron 06:00 BST)
  → opinion-section RSS + Google News fallback + HTML scrape (New Age)
  → full text + author/title extraction (trafilatura)
  → Gemini Flash (auto-selected newest model):
      type gate (editorial|op-ed|column|analysis vs news)
      → summary, vocabulary, keywords, punchlines (JSON)
  → Firestore doc: title, date, source, origin, category, articleType,
    author, summary, vocabulary, keywords, punchlines, fullText
```

**No PDFs.** The full article text is stored on the Firestore document itself
(45k-char cap ≈ 135 KB, well under Firestore's 1 MiB doc limit) and rendered
in-app. That avoids Firebase Storage (which needs the paid Blaze plan on new
projects) and keeps this repo lightweight.

## Setup (one time, ~15 minutes)

### 1. Create the GitHub repo
Push the **contents of this folder** as the root of a new **public** repo
(public = free unlimited Actions minutes):

```bash
cd editorial-backend
git init && git add -A && git commit -m "Editorial pipeline"
gh repo create bcs-editorials --public --source=. --push
```

The workflow is already at `.github/workflows/main.yml`.

### 2. Get the two keys

- **Gemini API key** — <https://aistudio.google.com/apikey> → "Create API key".
  The script asks the API which models your key can use and picks the newest
  Flash model automatically (hardcoded names break when Google retires models —
  `gemini-2.5-flash` already 404s for new keys). Current Flash models allow
  ~1,500 free requests/day; the pipeline uses ≤ 40 (saves + gate rejections).
  If the quota ever runs out mid-run, the run stops cleanly and keeps whatever
  was saved; quota resets at midnight US Pacific (~1 PM Bangladesh time).
- **Firebase service account** — Firebase console → ⚙️ Project settings →
  *Service accounts* → **Generate new private key**. This downloads a JSON
  file. Never commit it.

### 3. Add GitHub Secrets
Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | the AI Studio key |
| `FIREBASE_SERVICE_ACCOUNT` | paste the **entire JSON file content** |

### 4. Firestore rules
The pipeline writes with the admin SDK (bypasses rules). Your app only needs
read access:

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /editorials/{doc} {
      allow read: if true;    // or `if request.auth != null;` once you add auth
      allow write: if false;
    }
    match /editorial_meta/{doc} {   // archive index (list of days)
      allow read: if true;
      allow write: if false;
    }
  }
}
```

### 5. Test it
Repo → **Actions** → *daily-editorials* → **Run workflow**. Watch the logs:
`Using Gemini model: …`, `Saved [source] title…` lines,
`Gatekeeper rejected (hard news): …` for filtered items, and a final
`Done. saved=… rejected=… skipped=… failed=…` summary. Documents appear in
Firestore → `editorials`.

Local test (optional):
```bash
pip install -r requirements.txt
set FIREBASE_SERVICE_ACCOUNT_FILE=C:\path\to\key.json
set GEMINI_API_KEY=...
python main.py
```

## Firestore document shape

```json
{
  "title": "…",
  "articleUrl": "https://…",
  "source": "Al Jazeera",
  "origin": "international",
  "author": "Author Name or null",
  "articleType": "op-ed",
  "category": "War & Peace",
  "language": "en",
  "summary": "Comprehensive summary ending with learning takeaways…",
  "vocabulary": [{ "word": "belligerent", "meaning_bn": "যুদ্ধরত / আক্রমণাত্মক" }],
  "keywords": ["ceasefire diplomacy", "UN Resolution 2735"],
  "punchlines": ["A peace that is merely the absence of war is a pause, not a settlement."],
  "fullText": "Full extracted article text for in-app reading…",
  "date": "2026-07-14",
  "createdAt": "<server timestamp>"
}
```

Doc ID = SHA-1 of the article URL → automatic dedupe across days.

`origin` drives the app's National/International toggle. A single
`editorial_meta/days` document lists every day that has articles — the app's
archive UI (weekly folders → days) is built from that one read.

**Retention:** only the current calendar month is kept. On the first run of a
new month the pipeline deletes all previous-month Firestore docs and the old
entries in `editorial_meta/days`.

## Sources (opinion sections, verified July 2026)

- **Direct RSS:** Daily Star `/opinion`, TBS `/analysis` + `/thoughts`,
  TBS Bangla `/bangla/opinion`, Guardian `commentisfree`, Project Syndicate,
  Al Jazeera (all-feed + `/opinions/` path filter).
- **Google News, path-scoped** (no opinion RSS or runner-blocked RSS):
  Prothom Alo `/opinion`, Samakal `/opinion`, Naya Diganta `/opinions`,
  Bonik Barta (3 sections via custom query), Ittefaq, Jugantor, Amar Desh,
  Financial Express, AP, Reuters, WEF, SciDev, Down To Earth.
- **HTML scrape:** New Age (editorial + opinion section pages) — it has no
  RSS and Google News doesn't index the domain.

Feed config lives in [sources.py](sources.py); the Gemini type-gate in
[main.py](main.py) is the final arbiter regardless of source.

## Free-tier budget (per day, defaults)

| Service | Usage | Free limit |
|---|---|---|
| Gemini Flash (auto-picked) | ≤ 40 requests | ~1,500/day (check your AI Studio dashboard) |
| Firestore writes | ≤ 15 | 20,000/day |
| Firestore reads (dedupe) | ~60 | 50,000/day |
| GitHub Actions | ~5 min | unlimited (public repo) |

## Frontend

The production UI lives in the app repo (`app/app/editorial/` +
`app/lib/editorials.js`): National/International toggle, category grouping,
bottom-sheet reader (summary → vocabulary → keywords/punchlines → audio →
inline full text), monthly archive. [frontend/editorials.js](frontend/editorials.js)
here is a standalone reference copy of the data layer.
Bengali TTS (`bn-BD`) is built into Google TTS on Android; desktop browser
support varies.

---

# Weekly Current-Affairs Pipeline (`updates.py`)

Feeds the app's **সাম্প্রতিক আপডেট** section.

**Collects daily, published weekly.** The workflow runs every morning at 08:00
BST and files a few drafts; you approve a week's worth at `/admin/updates`
whenever suits — typically Friday, the app's revision day, so the week's current
affairs reach students as they sit down to review.

### Why daily collection, not one weekly run

Measured across all 18 queries on a real week:

| | one weekly run | daily runs |
|---|---|---|
| matching articles published that week | 1,185 | 1,185 |
| candidates actually examined | 54 (**4.6%**) | ~378 (**~7×**) |
| queries truncated by Google's 100-item RSS cap | 5 of 18 | 0 of 18 |
| cost of one failed run | the whole week, unrecoverable | one day |

The RSS cap is the decisive one: at `when:7d` five of these queries return
exactly 100 results, so a weekly run **cannot see** the rest of the week however
many items it takes. At `when:2d` the same queries return 13–75, well clear of
the cap.

The 2-day window overlaps by a day on purpose, so a failed run is covered by the
next one. Repeats cost a single Firestore read — the URL-hash dedupe check runs
before extraction and before any Gemini call.

To revert to weekly: set `SEARCH_WINDOW = "7d"` in `update_sources.py` and the
cron to `"0 1 * * 5"`.

`main.py` collects **opinion to be read**; this collects **facts to be
memorised**. That difference drives every design choice below.

## How "exam-relevant" is decided

General news is mostly not exam material: a week of headlines is dominated by
accidents, court dates and party statements, none of which a BCS or bank
question will ever ask about. Relevance is therefore filtered in four layers:

1. **Query shape** (`update_sources.py` → `EXAM_QUERIES`). We do not browse the
   news. We run 18 searches shaped like the questions exams actually ask —
   *who was appointed*, *what rank did Bangladesh get*, *which treaty was
   signed*, *who won what*. A question-shaped query returns question-shaped
   articles. Each entry describes a **category of fact**, not a topic:
   "অর্থনীতি" returns opinion columns, "রিজার্ভ কত বিলিয়ন" returns a number.
2. **Wire feeds** (`NEWS_FEEDS`) — BSS and UN News, whose routine output is
   disproportionately the record-keeping kind of fact exams draw from.
3. **Blocklist** (`BLOCK_PATTERNS`) — title-only rejection before any network
   or API cost. ASCII patterns match on word boundaries, so "vs" cannot fire
   inside "advisers". Cheap insurance, not the main filter.
4. **Gemini rubric** (`RELEVANCE_PROMPT`) — scores 0–10 on *"how likely is a
   fact from this to appear as an MCQ in the next two years?"*, with worked
   examples of what scores 8–10 and what is an automatic reject. Below
   `UPDATES_MIN_SCORE` (default 6) the article is dropped.

A final `has_concrete_fact()` check rejects notes whose bullets carry no number,
date or proper noun — a note nobody can be examined on.

## Drafts, not publications

Items are written with `status: "draft"` and are **invisible to students** until
an admin approves them at `/admin/updates`. Students memorise these as fact, so
a wrong rank or date is worse than an empty section. The review screen shows
every bullet point plus a link to the source for checking. Set
`UPDATES_AUTO_PUBLISH=1` to skip approval (not recommended).

`firestore.rules` enforces this: `allow read: if isAdmin() || resource.data.status == 'published'`.
Because that is a per-document rule, a client listing the collection **must**
filter `where("status", "==", "published")` or Firestore rejects the whole query
— which is what `getUpdates()` in the app does.

## Firestore shape (`updates` collection)

| field | example |
|---|---|
| `title` | `"বাংলাদেশ-দক্ষিণ কোরিয়া সেপা চুক্তি স্বাক্ষরিত"` |
| `points` | `["...", "...", "..."]` (3–5 Bengali facts) |
| `category` | `bangladesh` \| `international` \| `science` \| `economy` \| `environment` \| `sports` |
| `status` | `draft` \| `published` |
| `score` | `8` (Gemini's exam-relevance rating, shown in the admin queue) |
| `source`, `sourceUrl` | for verification before approving |
| `date`, `createdAt`, `publishedAt` | |

## Gemini key: use a second one

The two pipelines are separate GitHub workflows (`main.yml` daily,
`updates.yml` Friday) and should use **separate Gemini keys**.

On Fridays both run an hour apart. Sharing one key means the editorial run can
spend its quota first and leave this one starved — and the free-tier allowance
has shrunk without warning before (the pipeline once found itself with 20
requests, the leftover quota of a retired model).

> **The catch:** Gemini free-tier quota is metered **per Google Cloud project**,
> not per key. A second key created inside the *same* AI Studio project shares
> the same daily allowance and buys you nothing. Create a **new project** in AI
> Studio and mint the key there.

Setup:

1. AI Studio → new project → **Get API key**.
2. GitHub repo → Settings → Secrets and variables → Actions → **New secret**
   named `UPDATES_GEMINI_API_KEY`.

Until that secret exists the pipeline falls back to `GEMINI_API_KEY` and logs
a warning saying so, so nothing breaks in the meantime.

## Tuning

| variable | default | effect |
|---|---|---|
| `UPDATES_MAX_ITEMS` | 5 | drafts per daily run (~35/week to choose from) |
| `UPDATES_MIN_SCORE` | 6 | raise to 7–8 for a stricter section |
| `UPDATES_MAX_GEMINI_CALLS` | 30 | safety rail, per run |
| `UPDATES_AUTO_PUBLISH` | 0 | 1 = publish without review |

Both can be overridden from the workflow's **Run workflow** button.

## Shared code

`common.py` holds what both pipelines need — feed fetching, Google News link
decoding, article extraction, encoding hygiene, Gemini model selection and JSON
calls. It exists because the ISO-8859-1 decoding bug (Bengali pages served
without a charset header came out as mojibake) would otherwise have been fixed
in one pipeline and silently inherited by the other.

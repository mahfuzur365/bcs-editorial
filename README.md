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

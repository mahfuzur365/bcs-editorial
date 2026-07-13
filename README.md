# BCS Mentor — Daily Editorial Backend (100% free)

Automated pipeline: every day at **6:00 AM Bangladesh time**, GitHub Actions
fetches editorials/news from 19 Bangladeshi + international feeds, filters them
to 8 BCS topics (Economy, World Politics, Geopolitics, War & Peace, Environment,
Agriculture, Public Policy, Art/Literature/Culture), summarizes with
**Gemini (free tier)**, extracts English
vocabulary with Bengali meanings, renders a **Bengali-correct PDF**, and writes
everything to **Firestore (Spark plan, `(default)` database)**.

```
GitHub Actions (cron 06:00 BST)
  → RSS + Google News fallback (feedparser / googlenewsdecoder)
  → full text extraction (trafilatura)
  → Gemini Flash (auto-selected newest model): summary + vocabulary (JSON)
  → PDF (WeasyPrint + Noto Sans Bengali — correct conjunct/matra shaping)
  → PDF hosted in this repo (raw.githubusercontent.com)  ← default, free
  → Firestore doc: title, date, source, category, summary, vocabulary, pdfUrl
```

## Why these two design choices

- **PDFs are stored in this GitHub repo, not Firebase Storage, by default.**
  Since 30 Oct 2024 Firebase requires the Blaze (billed) plan to create a
  Storage bucket in new projects. A public GitHub repo serves the PDFs free via
  `raw.githubusercontent.com`. If your Firebase project already has a bucket,
  flip to Firebase mode (see below).
- **WeasyPrint instead of FPDF/ReportLab.** FPDF and ReportLab embed Unicode
  fonts but do **not** do OpenType shaping, so Bengali conjuncts (ক্ষ, ন্ত) and
  vowel signs (কি, কে) render broken. WeasyPrint uses Pango/HarfBuzz — the same
  shaping engine as a browser — so Bengali renders perfectly. Still free.

## Setup (one time, ~15 minutes)

### 1. Create the GitHub repo
Push the **contents of this folder** as the root of a new **public** repo
(public = free unlimited Actions minutes + free raw PDF hosting):

```bash
cd editorial-backend
git init && git add -A && git commit -m "Editorial pipeline"
gh repo create bcs-editorials --public --source=. --push
```

The workflow is already at `.github/workflows/main.yml`.

### 2. Get the two keys

- **Gemini API key** — <https://aistudio.google.com/apikey> → "Create API key".
  Free tier; the pipeline caps itself at 15 articles/day with 8-second gaps.
  The script asks the API which models your key can use and picks the newest
  Flash model automatically (hardcoded names break when Google retires models —
  `gemini-2.5-flash` already 404s for new keys). Set the `GEMINI_MODEL` env
  var only if you want to force a specific model. If the daily quota runs out
  mid-run, the run stops cleanly and keeps whatever was already saved; free
  quota resets at midnight US Pacific (~1 PM Bangladesh time).
- **Firebase service account** — Firebase console → ⚙️ Project settings →
  *Service accounts* → **Generate new private key**. This downloads a JSON
  file. Never commit it.

### 3. Add GitHub Secrets
Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | the AI Studio key |
| `FIREBASE_SERVICE_ACCOUNT` | paste the **entire JSON file content** |
| `FIREBASE_STORAGE_BUCKET` | *(only for Firebase Storage mode)* e.g. `myproject.appspot.com` |

Optional repository **Variable** (not secret): `PDF_STORAGE` = `firebase` to
upload PDFs to Firebase Storage instead of the repo. Default is `github`.

### 4. Firestore rules
The pipeline writes with the admin SDK (bypasses rules). Your app only needs
read access to the collection:

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /editorials/{doc} {
      allow read: if true;    // or `if request.auth != null;` once you add auth
      allow write: if false;
    }
  }
}
```

### 5. Test it
Repo → **Actions** → *daily-editorials* → **Run workflow**. Watch the logs;
you should see `Saved [source] title...` lines, new files under `pdfs/`, and
documents in Firestore → `editorials`.

Local test (optional):
```bash
pip install -r requirements.txt
set FIREBASE_SERVICE_ACCOUNT_FILE=C:\path\to\key.json
set GEMINI_API_KEY=...
python main.py
```
(WeasyPrint on Windows needs GTK runtime; easiest is to just test via the
Actions manual run.)

## Firestore document shape

```json
{
  "title": "…",
  "articleUrl": "https://…",
  "source": "Al Jazeera",
  "category": "War & Peace",
  "language": "en",
  "summary": "Comprehensive summary…",
  "vocabulary": [{ "word": "belligerent", "meaning_bn": "যুদ্ধরত / আক্রমণাত্মক" }],
  "pdfUrl": "https://raw.githubusercontent.com/…/pdfs/2026-07-13/ab12….pdf",
  "date": "2026-07-13",
  "createdAt": "<server timestamp>"
}
```

Doc ID = SHA-1 of the article URL → automatic dedupe across days.

## Source notes (verified July 2026)

Direct RSS (working from GitHub runners): Daily Star (opinion), Prothom Alo,
Amar Desh, Financial Express (`today.` subdomain), TBS, Guardian (world +
environment), Al Jazeera, Project Syndicate.

Via **Google News fallback** (site blocks bots/datacenter IPs or has no RSS):
Jugantor, Naya Diganta, Bonik Barta, Samakal, Ittefaq (their RSS 403s requests
from GitHub's IP ranges), **AP News**, **Reuters** (both discontinued public
RSS), WEF Agenda, SciDev.Net, Down To Earth. These use Google News RSS scoped
to the domain + `googlenewsdecoder`; if Google changes its link format some
items may be skipped for a while — the run never fails because of it. Feed
URLs live in [sources.py](sources.py); edit that file to add/remove sources.

## Free-tier budget (per day, defaults)

| Service | Usage | Free limit |
|---|---|---|
| Gemini Flash (auto-picked) | ≤ 15 requests | varies by model; script stops cleanly at the daily cap |
| Firestore writes | ≤ 15 | 20,000/day |
| Firestore reads (dedupe) | ~60 | 50,000/day |
| GitHub Actions | ~8 min | unlimited (public repo) |
| PDF hosting | ~2 MB/day in-repo | 1 GB soft repo limit ≈ years of PDFs |

## Frontend

[frontend/editorials.js](frontend/editorials.js) — drop into your web app:
`getTodayEditorials()`, `getLatestEditorials(n, category)`, `speakSummary()`
(Capacitor TTS on device, Web Speech API in the browser), `speakVocabulary()`.
For Capacitor: `npm i @capacitor-community/text-to-speech && npx cap sync`.
Bengali TTS voice (`bn-BD`) is built into Google TTS on Android; desktop
browser support varies.

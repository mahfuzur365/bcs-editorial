"""
Feed configuration for the daily editorial pipeline.

Two feed types:
  - "rss"   : a direct RSS/Atom feed URL (all URLs below verified working 2026-07).
  - "gnews" : sources that block bots or have no public RSS. We query Google News
              RSS scoped to their domain (site:domain when:1d) and decode the
              redirect links with `googlenewsdecoder`. Best-effort: if decoding
              fails for an item, that item is skipped, never the whole run.

`origin` ("national" | "international") drives the app's paper toggle.
`always_include=True` marks editorial/opinion feeds whose every item is relevant
for BCS prep; other feeds are filtered by TOPIC_KEYWORDS.

EDITORIAL-ONLY GATEKEEPING (three layers):
  1. Every feed below targets the paper's opinion/editorial section where one
     exists (verified 2026-07): Daily Star /opinion, Guardian /commentisfree,
     TBS /analysis + /thoughts, Project Syndicate (all op-eds), Al Jazeera via
     `path_filter` (its opinion URLs live under /opinions/).
  2. gnews feeds get opinion keywords injected into the Google News query
     (`terms`, defaulting per language; "" opts out for inherently analytical
     outlets like WEF where titles rarely say "opinion").
  3. Gemini classifies article TYPE before summarizing and rejects hard news
     (see SUMMARY_PROMPT in main.py) — the final arbiter.
"""

# Default opinion-section search terms for Google News queries.
GNEWS_OPINION_TERMS = {
    "bn": '(মতামত OR সম্পাদকীয় OR "উপ-সম্পাদকীয়" OR কলাম OR বিশ্লেষণ)',
    "en": "(opinion OR editorial OR op-ed OR column OR analysis OR commentary)",
}

FEEDS = [
    # ---------------- Bangladeshi sources ----------------
    {"name": "The Daily Star", "type": "rss", "lang": "en", "origin": "national",
     "url": "https://www.thedailystar.net/opinion/rss.xml",  # opinion section
     "default_category": "Public Policy", "always_include": True},

    # Prothom Alo has no opinion-section RSS (/opinion/feed 404s) → Google News
    # scoped to the domain with Bengali opinion keywords.
    {"name": "Prothom Alo", "type": "gnews", "lang": "bn", "origin": "national",
     "domain": "prothomalo.com",
     "default_category": "Public Policy"},

    {"name": "Naya Diganta", "type": "gnews", "lang": "bn", "origin": "national",
     "domain": "dailynayadiganta.com",
     "default_category": "Public Policy"},

    # No opinion RSS (/opinion/feed 404s) → gnews with opinion keywords.
    {"name": "Amar Desh", "type": "gnews", "lang": "bn", "origin": "national",
     "domain": "dailyamardesh.com",
     "default_category": "Public Policy"},

    # No editorial RSS on the today. subdomain → gnews with opinion keywords.
    {"name": "The Financial Express", "type": "gnews", "lang": "en", "origin": "national",
     "domain": "thefinancialexpress.com.bd",
     "default_category": "Economy"},

    {"name": "The Business Standard (Analysis)", "type": "rss", "lang": "en",
     "origin": "national",
     "url": "https://www.tbsnews.net/analysis/rss.xml",  # analysis section
     "default_category": "Economy", "always_include": True},

    {"name": "The Business Standard (Thoughts)", "type": "rss", "lang": "en",
     "origin": "national",
     "url": "https://www.tbsnews.net/thoughts/rss.xml",  # op-ed section
     "default_category": "Public Policy", "always_include": True},

    {"name": "Bonik Barta", "type": "gnews", "lang": "bn", "origin": "national",
     "domain": "bonikbarta.com",
     "default_category": "Economy"},

    # Samakal & Ittefaq serve their RSS fine to home IPs but 403 requests
    # coming from GitHub Actions datacenter IPs → Google News fallback.
    {"name": "Samakal", "type": "gnews", "lang": "bn", "origin": "national",
     "domain": "samakal.com",
     "default_category": "Public Policy"},

    {"name": "Ittefaq", "type": "gnews", "lang": "bn", "origin": "national",
     "domain": "ittefaq.com.bd",
     "default_category": "Public Policy"},

    {"name": "Jugantor", "type": "gnews", "lang": "bn", "origin": "national",
     "domain": "jugantor.com",
     "default_category": "Public Policy"},

    # ---------------- International sources ----------------
    # Guardian opinion section (replaces the world/environment news feeds —
    # those served hard news; categories still come from keyword matching).
    {"name": "The Guardian", "type": "rss", "lang": "en", "origin": "international",
     "url": "https://www.theguardian.com/commentisfree/rss",
     "default_category": "World Politics", "always_include": True},

    # No opinion-only feed exists; all.xml + path filter keeps only /opinions/.
    {"name": "Al Jazeera", "type": "rss", "lang": "en", "origin": "international",
     "url": "https://www.aljazeera.com/xml/rss/all.xml",
     "path_filter": "/opinions/",
     "default_category": "World Politics", "always_include": True},

    # AP is a hard-news wire; only its occasional analysis pieces can pass the
    # gate, so expect few or zero items from it.
    {"name": "AP News", "type": "gnews", "lang": "en", "origin": "international",
     "domain": "apnews.com",
     "default_category": "World Politics"},

    {"name": "Reuters", "type": "gnews", "lang": "en", "origin": "international",
     "domain": "reuters.com",
     "terms": "(breakingviews OR analysis OR opinion OR commentary)",
     "default_category": "World Politics"},

    {"name": "Project Syndicate", "type": "rss", "lang": "en", "origin": "international",
     "url": "https://www.project-syndicate.org/rss",  # op-eds by nature
     "default_category": "World Politics", "always_include": True},

    # WEF/SciDev/DTE publish analytical commentary whose titles rarely contain
    # "opinion" — injecting terms would starve them, so opt out ("") and let
    # the Gemini gate judge each piece.
    {"name": "WEF Agenda", "type": "gnews", "lang": "en", "origin": "international",
     "domain": "weforum.org", "terms": "",
     "default_category": "Economy"},

    {"name": "SciDev.Net", "type": "gnews", "lang": "en", "origin": "international",
     "domain": "scidev.net", "terms": "",
     "default_category": "Environment"},

    {"name": "Down To Earth", "type": "gnews", "lang": "en", "origin": "international",
     "domain": "downtoearth.org.in", "terms": "",
     "default_category": "Environment"},
]

# Keyword → category matching (checked against feed-item title + summary).
# English keywords match on word boundaries (plural allowed); Bengali keywords
# match prefix-wise so inflected forms still hit. See main.py:_keyword_pattern.
TOPIC_KEYWORDS = {
    "Economy": [
        "economy", "economic", "inflation", "gdp", "remittance", "export",
        "import", "trade", "tariff", "budget", "tax", "banking", "imf",
        "world bank", "investment", "stock market", "reserve", "currency",
        "অর্থনীতি", "মূল্যস্ফীতি", "রেমিট্যান্স", "রপ্তানি", "আমদানি",
        "বাজেট", "ব্যাংক", "বিনিয়োগ", "রিজার্ভ", "শেয়ারবাজার", "কর",
    ],
    "World Politics": [
        "election", "diplomacy", "diplomatic", "united nations",
        "summit", "sanction", "foreign policy", "bilateral", "parliament",
        "president", "prime minister", "government", "democracy",
        "নির্বাচন", "কূটনীতি", "জাতিসংঘ", "নিষেধাজ্ঞা", "পররাষ্ট্র",
        "সংসদ", "রাষ্ট্রপতি", "প্রধানমন্ত্রী", "গণতন্ত্র", "রাজনীতি",
    ],
    "Geopolitics": [
        "geopolitics", "geopolitical", "geostrategic", "geoeconomic",
        "indo-pacific", "great power", "superpower", "regional power",
        "sphere of influence", "balance of power", "strategic rivalry",
        "proxy war", "hegemony",
        "ভূ-রাজনীতি", "ভূরাজনীতি", "ভূ-রাজনৈতিক", "ভূরাজনৈতিক",
        "পরাশক্তি", "আধিপত্য",
    ],
    "War & Peace": [
        "war", "conflict", "ceasefire", "military", "missile", "invasion",
        "peace talks", "armed", "troops", "refugee", "genocide", "nato",
        "যুদ্ধ", "সংঘাত", "যুদ্ধবিরতি", "সামরিক", "ক্ষেপণাস্ত্র",
        "শরণার্থী", "শান্তি আলোচনা", "সেনা",
    ],
    "Environment": [
        "climate", "environment", "pollution", "emission", "biodiversity",
        "flood", "cyclone", "drought", "renewable", "solar", "deforestation",
        "river", "ecosystem", "global warming", "carbon",
        "জলবায়ু", "পরিবেশ", "দূষণ", "বন্যা", "ঘূর্ণিঝড়", "খরা",
        "নবায়নযোগ্য", "নদী", "বন উজাড়", "কার্বন",
    ],
    "Agriculture": [
        "agriculture", "farmer", "crop", "harvest", "food security",
        "fertilizer", "irrigation", "paddy", "rice production", "wheat",
        "livestock", "fisheries", "seed", "agri",
        "কৃষি", "কৃষক", "ফসল", "ধান", "খাদ্য নিরাপত্তা", "সার",
        "সেচ", "গম", "মৎস্য", "বীজ",
    ],
    "Public Policy": [
        "policy", "reform", "education", "health care", "healthcare",
        "corruption", "governance", "infrastructure", "constitution",
        "judiciary", "civil service", "public administration", "subsidy",
        "নীতি", "সংস্কার", "শিক্ষা", "স্বাস্থ্য", "দুর্নীতি", "সুশাসন",
        "অবকাঠামো", "সংবিধান", "বিচার বিভাগ", "জনপ্রশাসন", "ভর্তুকি",
    ],
    "Art, Literature & Culture": [
        "art", "arts", "artist", "artwork", "painting", "sculpture",
        "literature", "literary", "novelist", "poet", "poetry", "fiction",
        "culture", "cultural", "heritage", "museum", "theatre", "theater",
        "folklore", "exhibition", "archaeology", "manuscript",
        "শিল্প-সাহিত্য", "শিল্পকলা", "সাহিত্য", "সংস্কৃতি", "সাংস্কৃতিক",
        "কবিতা", "কবি", "উপন্যাস", "ঐতিহ্য", "জাদুঘর", "চিত্রকলা",
        "নাটক", "প্রত্নতত্ত্ব",
    ],
}

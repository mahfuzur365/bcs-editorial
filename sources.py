"""
Feed configuration for the daily editorial pipeline.

Two feed types:
  - "rss"   : a direct RSS/Atom feed URL (all URLs below verified working 2026-07).
  - "gnews" : sources that block bots or have no public RSS. We query Google News
              RSS scoped to their domain (site:domain when:1d) and decode the
              redirect links with `googlenewsdecoder`. Best-effort: if decoding
              fails for an item, that item is skipped, never the whole run.

`always_include=True` marks editorial/opinion feeds whose every item is relevant
for BCS prep; other feeds are filtered by TOPIC_KEYWORDS.
"""

FEEDS = [
    # ---------------- Bangladeshi sources ----------------
    {"name": "The Daily Star", "type": "rss", "lang": "en",
     "url": "https://www.thedailystar.net/opinion/rss.xml",
     "default_category": "Public Policy", "always_include": True},

    {"name": "Prothom Alo", "type": "rss", "lang": "bn",
     "url": "https://www.prothomalo.com/feed",
     "default_category": "Public Policy"},

    {"name": "Naya Diganta", "type": "gnews", "lang": "bn",
     "domain": "dailynayadiganta.com",
     "default_category": "Public Policy"},

    {"name": "Amar Desh", "type": "rss", "lang": "bn",
     "url": "https://www.dailyamardesh.com/feed",
     "default_category": "Public Policy"},

    {"name": "The Financial Express", "type": "rss", "lang": "en",
     "url": "https://today.thefinancialexpress.com.bd/feed",
     "default_category": "Economy"},

    {"name": "The Business Standard", "type": "rss", "lang": "en",
     "url": "https://www.tbsnews.net/rss.xml",
     "default_category": "Economy"},

    {"name": "Bonik Barta", "type": "gnews", "lang": "bn",
     "domain": "bonikbarta.com",
     "default_category": "Economy"},

    # Samakal & Ittefaq serve their RSS fine to home IPs but 403 requests
    # coming from GitHub Actions datacenter IPs → Google News fallback.
    {"name": "Samakal", "type": "gnews", "lang": "bn",
     "domain": "samakal.com",
     "default_category": "Public Policy"},

    {"name": "Ittefaq", "type": "gnews", "lang": "bn",
     "domain": "ittefaq.com.bd",
     "default_category": "Public Policy"},

    {"name": "Jugantor", "type": "gnews", "lang": "bn",
     "domain": "jugantor.com",
     "default_category": "Public Policy"},

    # ---------------- International sources ----------------
    {"name": "The Guardian", "type": "rss", "lang": "en",
     "url": "https://www.theguardian.com/world/rss",
     "default_category": "World Politics"},

    {"name": "The Guardian (Environment)", "type": "rss", "lang": "en",
     "url": "https://www.theguardian.com/environment/rss",
     "default_category": "Environment"},

    {"name": "Al Jazeera", "type": "rss", "lang": "en",
     "url": "https://www.aljazeera.com/xml/rss/all.xml",
     "default_category": "World Politics"},

    {"name": "AP News", "type": "gnews", "lang": "en",
     "domain": "apnews.com",
     "default_category": "World Politics"},

    {"name": "Reuters", "type": "gnews", "lang": "en",
     "domain": "reuters.com",
     "default_category": "World Politics"},

    {"name": "Project Syndicate", "type": "rss", "lang": "en",
     "url": "https://www.project-syndicate.org/rss",
     "default_category": "World Politics", "always_include": True},

    {"name": "WEF Agenda", "type": "gnews", "lang": "en",
     "domain": "weforum.org",
     "default_category": "Economy"},

    {"name": "SciDev.Net", "type": "gnews", "lang": "en",
     "domain": "scidev.net",
     "default_category": "Environment"},

    {"name": "Down To Earth", "type": "gnews", "lang": "en",
     "domain": "downtoearth.org.in",
     "default_category": "Environment"},
]

# Keyword → category matching (checked against feed-item title + summary).
# English keywords are matched case-insensitively; Bengali as-is.
TOPIC_KEYWORDS = {
    "Economy": [
        "economy", "economic", "inflation", "gdp", "remittance", "export",
        "import", "trade", "tariff", "budget", "tax", "banking", "imf",
        "world bank", "investment", "stock market", "reserve", "currency",
        "অর্থনীতি", "মূল্যস্ফীতি", "রেমিট্যান্স", "রপ্তানি", "আমদানি",
        "বাজেট", "ব্যাংক", "বিনিয়োগ", "রিজার্ভ", "শেয়ারবাজার", "কর",
    ],
    "World Politics": [
        "election", "diplomacy", "diplomatic", "geopolit", "united nations",
        "summit", "sanction", "foreign policy", "bilateral", "parliament",
        "president", "prime minister", "government", "democracy",
        "নির্বাচন", "কূটনীতি", "জাতিসংঘ", "নিষেধাজ্ঞা", "পররাষ্ট্র",
        "সংসদ", "রাষ্ট্রপতি", "প্রধানমন্ত্রী", "গণতন্ত্র", "রাজনীতি",
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
}

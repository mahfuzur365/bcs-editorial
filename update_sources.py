"""
Source configuration for the weekly current-affairs pipeline (updates.py).

The problem this file solves: general news is mostly NOT exam material. A week
of Bangladeshi headlines is dominated by road accidents, court dates, party
statements and match reports — none of which a BCS or bank question will ever
ask about. Feeding that to a model and hoping it picks well wastes quota and
produces a section full of noise.

So relevance is decided twice, before Gemini ever sees an article:

  1. SHAPE  — we do not browse "the news". We run queries shaped like the
              questions competitive exams actually ask: who was appointed, what
              rank did Bangladesh get, which treaty was signed, who won what.
              A question-shaped query returns question-shaped articles.
  2. SOURCE — plus a small set of wire/official feeds (BSS, UN News, PIB-style
              outlets) whose ordinary output is disproportionately the kind of
              record-keeping fact exams draw from.

Gemini is then the third filter, and its rubric lives in updates.py.

Editing guidance: each query should describe a CATEGORY OF FACT, not a topic.
"অর্থনীতি" is a topic and returns opinion columns; "রিজার্ভ কত বিলিয়ন" is a fact
shape and returns a number a candidate can memorise.
"""

# Categories must match UPDATE_CATEGORIES in the app (app/lib/updates.js).
CAT_BANGLADESH = "bangladesh"
CAT_INTERNATIONAL = "international"
CAT_SCIENCE = "science"
CAT_ECONOMY = "economy"
CAT_ENVIRONMENT = "environment"
CAT_SPORTS = "sports"

# How far back each run looks. Google News `when:` window.
#
# Deliberately 2 days for a DAILY run, not 7 for a weekly one. Measured over
# all 18 queries on a real week:
#   • the week produced 1,185 matching articles; one weekly run keeping 3 per
#     query would sample 54 of them — 4.6%
#   • Google News caps its RSS response at 100 items, and 5 of these queries
#     hit that ceiling at when:7d — so a weekly run cannot even see the rest of
#     the week, no matter how many it takes. At when:2d the same queries return
#     13-75, comfortably under the cap.
# Running daily with a 2-day window means every day is sampled while its news
# is still visible, and the one-day overlap lets the next run recover anything
# a failed run missed. Duplicates are dropped by URL hash before any extraction
# or Gemini call, so the overlap costs one Firestore read.
SEARCH_WINDOW = "2d"

# ---------------------------------------------------------------------------
# Question-shaped queries — the primary source.
# `bn` queries run against Bangladeshi Google News, `en` against international.
# ---------------------------------------------------------------------------
EXAM_QUERIES = [
    # ---- appointments & office holders -----------------------------------
    # Classic MCQ: "বর্তমান প্রধান বিচারপতি কে?"
    {"name": "নিয়োগ ও দায়িত্ব গ্রহণ", "lang": "bn", "category": CAT_BANGLADESH,
     "query": '(নিযুক্ত OR নিয়োগ OR শপথ OR "দায়িত্ব গ্রহণ") '
              '(প্রধান বিচারপতি OR গভর্নর OR সচিব OR উপাচার্য OR রাষ্ট্রদূত OR চেয়ারম্যান OR মহাপরিচালক)'},
    {"name": "New heads of state & global bodies", "lang": "en", "category": CAT_INTERNATIONAL,
     "query": '(sworn in OR appointed OR elected) (president OR prime minister OR '
              '"secretary-general" OR "chief executive") (UN OR IMF OR "World Bank" OR WHO OR WTO)'},

    # ---- awards & honours -------------------------------------------------
    {"name": "পুরস্কার ও পদক", "lang": "bn", "category": CAT_BANGLADESH,
     "query": '(পুরস্কার OR পদক OR সম্মাননা) (একুশে OR স্বাধীনতা OR বাংলা একাডেমি OR রাষ্ট্রীয়)'},
    {"name": "International prizes", "lang": "en", "category": CAT_INTERNATIONAL,
     "query": '(Nobel OR Pulitzer OR "Booker Prize" OR Oscar OR "Ramon Magsaysay") '
              '(awarded OR winner OR laureate)'},

    # ---- indices, rankings & reports -------------------------------------
    # Exams love a rank and a number: "দুর্নীতি ধারণা সূচকে বাংলাদেশের অবস্থান?"
    {"name": "সূচক ও র‍্যাংকিং", "lang": "bn", "category": CAT_BANGLADESH,
     "query": '(সূচক OR র‍্যাংকিং OR প্রতিবেদন) (বাংলাদেশ) (অবস্থান OR স্থান OR শীর্ষে)'},
    {"name": "Global indices & reports", "lang": "en", "category": CAT_INTERNATIONAL,
     "query": '("Human Development Index" OR "Corruption Perceptions Index" OR '
              '"Global Hunger Index" OR "Press Freedom Index" OR "Doing Business" OR '
              '"Happiness Report") (rank OR ranking OR report)'},

    # ---- treaties, summits, agreements -----------------------------------
    {"name": "চুক্তি ও সমঝোতা", "lang": "bn", "category": CAT_BANGLADESH,
     "query": '(চুক্তি OR সমঝোতা OR "সমঝোতা স্মারক" OR এমওইউ) (বাংলাদেশ) (স্বাক্ষর)'},
    {"name": "Summits & treaties", "lang": "en", "category": CAT_INTERNATIONAL,
     "query": '(summit OR treaty OR accord OR "peace deal") (signed OR concluded OR hosted) '
              '(G20 OR ASEAN OR SAARC OR BIMSTEC OR BRICS OR "United Nations" OR NATO)'},

    # ---- economy: figures a candidate must memorise -----------------------
    {"name": "অর্থনৈতিক পরিসংখ্যান", "lang": "bn", "category": CAT_ECONOMY,
     "query": '(রিজার্ভ OR রেমিট্যান্স OR মূল্যস্ফীতি OR "জিডিপি প্রবৃদ্ধি" OR বাজেট) '
              '(বিলিয়ন OR শতাংশ OR কোটি OR রেকর্ড)'},
    {"name": "Bangladesh economy figures", "lang": "en", "category": CAT_ECONOMY,
     "query": 'Bangladesh (GDP growth OR inflation OR remittance OR "foreign reserves" OR export) '
              '(percent OR billion OR record OR forecast)'},

    # ---- science, technology & space -------------------------------------
    {"name": "বিজ্ঞান ও প্রযুক্তি", "lang": "bn", "category": CAT_SCIENCE,
     "query": '(আবিষ্কার OR উদ্ভাবন OR উৎক্ষেপণ OR স্যাটেলাইট OR "মহাকাশ") (প্রথম OR সফল)'},
    {"name": "Science & space milestones", "lang": "en", "category": CAT_SCIENCE,
     "query": '(launched OR discovered OR "first ever" OR breakthrough) '
              '(satellite OR mission OR vaccine OR telescope OR "space agency" OR NASA OR ISRO)'},

    # ---- environment & climate -------------------------------------------
    {"name": "পরিবেশ ও জলবায়ু", "lang": "bn", "category": CAT_ENVIRONMENT,
     "query": '(জলবায়ু OR পরিবেশ OR বনভূমি OR "রামসার" OR "বিশ্ব ঐতিহ্য") '
              '(সম্মেলন OR ঘোষণা OR স্বীকৃতি OR তালিকাভুক্ত)'},
    {"name": "Climate & heritage designations", "lang": "en", "category": CAT_ENVIRONMENT,
     "query": '(UNESCO OR Ramsar OR COP OR IPCC) (World Heritage OR site OR summit OR report OR declared)'},

    # ---- national projects, institutions, laws ---------------------------
    {"name": "প্রকল্প ও অবকাঠামো", "lang": "bn", "category": CAT_BANGLADESH,
     "query": '(উদ্বোধন OR চালু OR নির্মাণ সম্পন্ন) (সেতু OR টানেল OR মেট্রোরেল OR বিদ্যুৎকেন্দ্র OR বিমানবন্দর)'},
    {"name": "নতুন আইন ও প্রতিষ্ঠান", "lang": "bn", "category": CAT_BANGLADESH,
     "query": '(অনুমোদন OR পাস OR গেজেট OR প্রতিষ্ঠা) (আইন OR অধ্যাদেশ OR নীতিমালা OR কমিশন)'},

    # ---- sport: titles and records only, never fixtures -------------------
    # A live run showed the English version pulling US college and local-league
    # results ("Championship won, standard unchanged for Indiana football"),
    # which no Bangladeshi exam will ever ask about. Naming the specific global
    # competitions keeps it to the handful of results that are examinable.
    {"name": "খেলাধুলার রেকর্ড ও শিরোপা", "lang": "bn", "category": CAT_SPORTS,
     "query": '(চ্যাম্পিয়ন OR শিরোপা OR "বিশ্ব রেকর্ড" OR স্বর্ণপদক) '
              '(বিশ্বকাপ OR অলিম্পিক OR "এশিয়ান গেমস" OR "সাফ চ্যাম্পিয়নশিপ" OR কমনওয়েলথ)'},
    {"name": "World titles & records", "lang": "en", "category": CAT_SPORTS,
     "query": '("world record" OR "gold medal" OR "world champion") '
              '("World Cup" OR Olympics OR "Asian Games" OR "Commonwealth Games" OR "World Championships")'},
]

# ---------------------------------------------------------------------------
# Wire / official feeds — steady supply of record-keeping facts.
# ---------------------------------------------------------------------------
NEWS_FEEDS = [
    {"name": "BSS (Bangladesh Sangbad Sangstha)", "lang": "en", "category": CAT_BANGLADESH,
     "url": "https://www.bssnews.net/rss/allNews.xml"},
    {"name": "UN News", "lang": "en", "category": CAT_INTERNATIONAL,
     "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml"},
]

# ---------------------------------------------------------------------------
# Hard exclusions — dropped before Gemini, on title alone. Cheap insurance, not
# the main filter: a live run over all 18 queries caught only 11 of ~1,170
# headlines, because question-shaped queries already avoid crime reporting.
# Its real job is the handful of noisy matches those queries do let through.
#
# ASCII entries match on word boundaries (see `blocked()` in updates.py), so
# "vs" cannot fire inside "advisers" and "NBA" cannot fire inside a URL.
# ---------------------------------------------------------------------------
BLOCK_PATTERNS = [
    # Bengali — crime, accidents, agitation
    "সড়ক দুর্ঘটনা", "নিহত", "আহত", "গ্রেপ্তার", "মামলা", "রিমান্ড", "আটক",
    "ধর্ষণ", "খুন", "লাশ", "অগ্নিকাণ্ড", "চুরি", "ডাকাতি", "সংঘর্ষ",
    "শোক", "জানাজা", "বিক্ষোভ", "হরতাল", "অবরোধ",
    # English — crime, accidents, agitation
    "killed", "injured", "arrested", "jailed", "remand", "murder",
    "rape", "robbery", "fire broke", "road crash", "accident",
    "protest", "clash", "strike called", "obituary",
    # Fixtures and local leagues — never examinable, and the sports queries
    # attract them ("Championship won ... for Indiana football").
    "match preview", "live score", "playing xi", "squad announced",
    "vs", "nfl", "nba", "mlb", "college football", "high school",
    "premier league", "transfer window",
    # Opinion/speculation shapes that slipped past the topical queries.
    "opinion", "reflections on", "what to expect", "could be", "may be set to",
]

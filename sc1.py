#!/usr/bin/env python3
"""
Golden Dome stance aggregator for ONE selected politician.

Pipeline
--------
1. Build NAME-SCOPED queries: every query carries the member's full name plus a
   topic term, because topic-only queries return the general discourse rather
   than this person's statements, and surname-only queries match nothing.
2. Collect candidate ARTICLES mentioning the selected person, preferring the
   official record:
       - Congressional Record floor statements  (govinfo full text)
       - Federal Register documents             (keyless API)
       - Bill text naming the person            (govinfo full text)
       - Senate committee press releases        (keyless WordPress REST API)
       - Web search via duckduckgo_search       (direct publisher URLs)
       - Defense & space publisher RSS feeds    (keyless)
       - Google News RSS                        (keyless, redirects resolved)
3. Download each article body (trafilatura, regex fallback).
4. Ask the local Ollama model ONCE per article to decide:
       related -> is this actually about the Golden Dome programme?
       stance  -> For | Against | Neutral for the selected person
   Items judged not related are dropped from the verdict tally.
5. Print ONE table of the sources found with their verdicts.

Only keyless endpoints are used by default. The govinfo searches need a free
api.data.gov key and are skipped silently when it is absent. The congress.gov
bill API is deliberately NOT used: it needs a separate key of its own.

DuckDuckGo throttles bursts aggressively, so queries are spaced out and
retried with exponential backoff. A full run therefore takes several minutes.

Usage
-----
Just run it (no arguments needed — double-click / bare run does the full
roster run and appends to the sheet):

    python3 sc1.py                 # ALL 535 members, push to sheet, asks start row

Optional flags (only if you want something different):
    python3 sc1.py --member "Ted Cruz"
        # just that one member (still pushes to sheet)
    python3 sc1.py --no-push-sheet
        # roster run without writing to the sheet
    python3 sc1.py --single
        # only MEMBER, no roster
    python3 sc1.py --no-model      # list every source found, no AI calls
    python3 sc1.py --official-only # official record only, skip press

Sheet output talks directly to the deployed Apps Script web app configured in
APPS_SCRIPT_URL below (no scraper.py import — this file is self-contained).
Each source occupies a 5-column block (date | source | stance | summary | url)
starting at column H. Sources already present in the sheet are skipped, so
re-running only appends new material.

Progress: single-member runs show collect/download/judge bars; roster runs add
an outer "roster progress [i/N]" bar plus a per-member line showing sources
found, sheet appends and verdict, and finish with a ROSTER COMPLETE table.

To report on someone else, use --member NAME (or edit MEMBER).
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import warnings
from urllib.parse import quote, urlparse

import requests

# ==================== CONFIGURATION ====================
# Default politician for single-member runs. `--member NAME` overrides it and
# `--all-members` ignores it (roster comes from the sheet instead).
MEMBER = "Maria Cantwell"

# Free api.data.gov key: https://api.data.gov/
# The same key serves govinfo full-text search.
#
# CI copy: supplied by the repo secret GOVINFO_API_KEY (also accepts the
# legacy DATA_GOV_API_KEY name). No default is compiled in, so this file is
# safe to commit to a public repository. Without the key the govinfo
# Congressional Record / bill-text searches are skipped and the official
# record contributes nothing -- run_batch.sh fails early if it is unset.
GOVINFO_API_KEY = os.environ.get(
    "GOVINFO_API_KEY",
    os.environ.get("DATA_GOV_API_KEY", ""),
)

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:1b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "120"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

GOVINFO_SEARCH_URL = "https://api.govinfo.gov/search"
GOVINFO_CREC = "CREC"   # Congressional Record: members' floor statements
GOVINFO_BILLS = "BILLS"  # Bill text

# Deployed Apps Script web app (exec URL). This is the ONLY sheet backend used
# by this file — no scraper.py import.
#
# CI copy: supplied by the repo secret APPS_SCRIPT_URL. No default is compiled
# in, so no deployment URL is committed to the public repository. With this
# empty, sheet_configured() is False, every sheet write is skipped, and the run
# produces no output — run_batch.sh fails early rather than reporting success.
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "")
# Each source occupies one 5-column block starting at column H:
#   H date | I source | J stance | K summary | L url, then repeats.
SOURCE_BLOCK_WIDTH = 5
FIRST_SOURCE_COLUMN = 8
# Be gentle with Apps Script quotas between appends.
SHEET_PUSH_DELAY = float(os.environ.get("SHEET_PUSH_DELAY", "0.5"))
# How long to wait for the sheet web app per call.
SHEET_TIMEOUT = int(os.environ.get("SHEET_TIMEOUT", "15"))

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

try:
    from duckduckgo_search import DDGS
except ImportError:  # pragma: no cover
    DDGS = None

log = logging.getLogger("goldendome")

# ---------------------------------------------------------------------------
# TOPIC KEYWORDS
# ---------------------------------------------------------------------------
TOPIC_NAME = "Golden Dome"

# Strong signals: at least one must hit before a model call is spent.
TOPIC_PHRASES = [
    "golden dome",
    "missile defense shield",
    "missile defence shield",
    "golden dome missile",
    "space-based missile defense",
    "space based missile defense",
    "golden dome",
]

# Terms that name the missile programme unambiguously, so they override the
# Samarra-shrine ambiguity below.
TOPIC_PROGRAMME_ONLY = (
    "missile defense shield",
    "missile defence shield",
    "golden dome missile",
    "space-based missile defense",
    "space based missile defense",
    "shield program",
    "shield programme",
)

# Loose prefilter applied to headlines/snippets to cut non-matches early.
TOPIC_KEYWORDS = [
    "golden dome",
    "missile defense",
    "missile defence",
    "shield program",
    "shield programme",
    "space based interceptor",
    "space-based interceptor",
]

# Domains treated as the official record when triaging a hit.
OFFICIAL_DOMAINS = (
    ".gov",
    "congress.gov",
    "senate.gov",
    "house.gov",
    "govinfo.gov",
    "federalregister.gov",
    "whitehouse.gov",
    "defense.gov",
    "sga.gov",
    "spaceforce.mil",
)

# Defense/space publisher feeds. These yield REAL article URLs, so the body can
# be extracted and the politician identified in the text (unlike Google News
# redirects, which carry no readable article).
PUBLISHER_FEEDS = [
    ("Breaking Defense", "https://breakingdefense.com/feed/"),
    ("Defense News", "https://www.defensenews.com/arc/outboundfeeds/rss/"),
    ("DefenseScoop", "https://defensescoop.com/feed/"),
    ("SpaceNews", "https://spacenews.com/feed/"),
    ("Air & Space Forces Magazine",
     "https://www.airandspaceforces.com/feed/"),
    ("The War Zone", "https://www.twz.com/feed"),
    ("USNI News", "https://news.usni.org/feed"),
    ("National Defense", "https://www.nationaldefense.org/feed/"),
    ("Arms Control Association", "https://armscontrol.org/rss.xml"),
    ("CSIS", "https://www.csis.org/rss/analysis"),
]

MAX_ARTICLES = 60           # hard cap: model calls are the slow part
MAX_PER_QUERY = 25
FEED_LIMIT = 40             # entries pulled per publisher feed
ARTICLE_MAX_CHARS = 6000    # excerpt handed to the model
# Congressional Record / bill documents are very long. They are fetched well
# beyond the model excerpt so the member-naming and topic gates see the whole
# record; judge_article() truncates again before prompting.
GOVINFO_MAX_CHARS = 120000

# The duckduckgo_search library manages its own session and anti-bot plumbing,
# so only a light shared cooldown between queries is kept here.
# DDG throttles bursts hard. Measured: the same query returned 0 hits, then 10
# hits once spaced out. A 1 s interval with a 2 s/4 s backoff abandoned queries
# that actually had results, which silently emptied the site: and press-release
# angles. These values trade runtime for recall.
DDG_MIN_INTERVAL = float(os.environ.get("DDG_MIN_INTERVAL", "5"))
DDG_MAX_ATTEMPTS = 6
DDG_BACKOFF_BASE = 4.0
_last_request = {}


def _throttle(label, min_interval):
    """Seconds to wait before the next call to `label` may go out."""
    now = time.time()
    last = _last_request.get(label, 0.0)
    _last_request[label] = now
    return max(0.0, last + min_interval - now)

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def strip_html(text):
    """Congress/Federal Register payloads arrive as HTML blobs.

    Whitespace is fully collapsed to single spaces. Callers rely on that: the
    report tables render one row per source, so an embedded newline would break
    column alignment. Use strip_html_keep_lines() where the line structure is
    itself the signal (Congressional Record speaker turns).
    """
    if not text:
        return ""
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def strip_html_keep_lines(text):
    """Strip tags but PRESERVE line breaks.

    strip_html() collapses every run of whitespace to a single space, which is
    right for prose but destroys the Congressional Record's one-turn-per-line
    layout. The speaker markers in crec_blocks() are detected with an anchored
    (?m)^ pattern, so the newlines have to survive the strip.
    """
    if not text:
        return ""
    import html as html_mod
    text = re.sub(r"(?i)<(br|/p|/div|/tr|/h[1-6])[^>]*>", "\n", str(text))
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Keep indentation (the Record indents amendment text) but cap the run so
    # deeply nested markup cannot produce enormous blank spans.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# "Golden Dome" is also a famous Shia shrine in Samarra, Iraq. Congressional
# Record text about Iraq therefore matches the topic phrase while having nothing
# to do with the missile programme. These co-occurring words identify that
# sense; the record is only about the shrine if the programme words are absent.
OFFTOPIC_CONTEXT = (
    "mosque",
    "shia",
    "samarra",
    "al-askariyah",
    "al askariyah",
    "imam",
    "sunni",
    "iraq",
    "iraqi",
)


def topic_hit(text):
    """True if the blob mentions the missile programme (not the Samarra shrine).

    A bare substring test on "golden dome" is not enough: Congressional Record
    passages about the bombing of the Golden Dome mosque in Iraq matched and
    were reported as programme sources. A match now needs either a programme
    synonym (missile defence shield, space-based defence) or a "Golden Dome"
    occurrence that is not surrounded by the shrine/Iraq vocabulary.
    """
    if not text:
        return False
    low = strip_html(text).lower()
    if not any(p in low for p in TOPIC_PHRASES):
        return False
    # A programme synonym anywhere is decisive.
    if any(p in low for p in TOPIC_PROGRAMME_ONLY):
        return True

    # Otherwise judge each "golden dome" occurrence by its surroundings.
    for m in re.finditer(r"golden dome", low):
        window = low[max(0, m.start() - 160): m.end() + 160]
        if not any(w in window for w in OFFTOPIC_CONTEXT):
            return True
    return False


def member_is_named(member_name, text):
    """True if the politician's name (or surname) appears in the evidence.

    Guards against attributing a stance from content that never mentions them.
    """
    if not member_name or not text:
        return False
    haystack = text.lower()
    parts = [p for p in re.split(r"\s+", member_name.strip()) if len(p) > 2]
    if not parts:
        return False
    if member_name.strip().lower() in haystack:
        return True
    # Surname alone is enough: news routinely drops the first name.
    return parts[-1].lower() in haystack


# ---------------------------------------------------------------------------
# CONGRESSIONAL RECORD SPEAKER ATTRIBUTION
# ---------------------------------------------------------------------------
# The Congressional Record is a verbatim transcript of an entire day of floor
# business: one granule routinely holds amendments and remarks from 100+
# members. So "the surname appears somewhere in the document" says nothing
# about who spoke -- it only proves they were present that day.
#
# The Record does, however, mark speaker turns explicitly at the head of a
# block, in the three-column print layout:
#
#     SA 5993. Mr. HICKENLOOPER (for himself and Mr. Crapo) submitted an
#     amendment ... the new facilities for the Golden Dome ... ______
#
#     SA 5994. Mr. MERKLEY submitted an amendment ...
#
# Each speaker's text runs from their own marker until the NEXT marker. So
# attribution is a containment test, not a substring test: take the member's
# own block(s) and require the topic to appear INSIDE them. A member who is
# merely listed as a cosponsor ("Mr. CRUZ (for himself, Ms. Cantwell, ...)")
# appears in someone ELSE's block and is correctly attributed nothing.

# A speaker turn opens a line, optionally after an amendment number (SA 6527.)
# and covers either a named Member or one of the presiding/managerial roles.
# The Record prints Member names in caps ("Mr. HICKENLOOPER") but is matched
# case-insensitively so both "Mr. Cruz" and "Mr. CRUZ" are found.
CREC_SPEAKER_RE = re.compile(
    r"(?m)^(?P<lead>[ \t]{0,12}(?:SA[ \t]+\d+\.[ \t]*)?)"
    r"(?P<name>"
    r"(?:Mr|Ms|Mrs|Miss)\.[ \t]+[A-Z][A-Za-z\-']*"
    r"|The[ \t]+(?:PRESIDING[ \t]+OFFICER|SPEAKER|CHAIRMAN|CHAIRWOMAN"
    r"|MINORITY[ \t]+LEADER|MAJORITY[ \t]+LEADER|MINORITY[ \t]+MANAGER"
    r"|MAJORITY[ \t]+MANAGER|MEMBER|SENATOR|REPRESENTATIVE)"
    r")"
)

# Roles never denote a specific member, so they must never satisfy a name match.
CREC_ROLE_NAMES = frozenset({
    "the presiding officer", "the speaker", "the chairman", "the chairwoman",
    "the minority leader", "the majority leader", "the minority whip",
    "the majority whip", "the minority manager", "the majority manager",
    "the member", "the senator", "the representative",
})


def crec_blocks(text):
    """Split a Congressional Record granule into (speaker, text) turns.

    Returns [(speaker_name, block_text), ...] in document order. Each block
    runs from one speaker marker to the next, which is exactly the span over
    which that member is actually speaking. Returns [] when no speaker marker
    is found, so callers can fail loudly instead of guessing.
    """
    if not text:
        return []
    marks = list(CREC_SPEAKER_RE.finditer(text))
    if not marks:
        return []
    blocks = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        speaker = re.sub(r"\s+", " ", m.group("name")).strip()
        blocks.append((speaker, text[m.end():end].strip()))
    return blocks


def crec_member_blocks(text, member_name):
    """Every block of the Record actually spoken by this member.

    Matching is on the surname appearing as its own token inside the speaker
    marker, so "Mr. HICKENLOOPER" matches Hickenlooper while "Mr. CRUZ (for
    himself, Ms. Cantwell, ...)" -- where Cantwell appears in the BODY, not in
    the speaker marker -- matches nobody. Roles are excluded outright.
    """
    surname = member_name.strip().split()[-1].strip(".,").lower()
    if not surname:
        return []
    out = []
    for speaker, block in crec_blocks(text):
        low = speaker.lower()
        if low in CREC_ROLE_NAMES:
            continue
        # Token-wise match so "CRUZ" does not match "CRUZAN".
        tokens = [t for t in re.split(r"[^a-z\-']+", low) if len(t) > 2]
        if surname in tokens:
            out.append(block)
    return out


def crec_member_sections(text, member_name):
    """The member's own Record text that actually discusses the programme.

    Applies the topic test INSIDE each of the member's own speaking turns --
    the keyword has to land before the next speaker takes over. Returns the
    concatenated matching turns (possibly empty), so an unrelated speech is
    dropped rather than recorded as a stance.
    """
    kept = []
    for block in crec_member_blocks(text, member_name):
        if topic_hit(block):
            kept.append(block)
    return "\n\n".join(kept)


def crec_has_speakers(text):
    """True when the Record's speaker turns could be recovered."""
    return bool(crec_blocks(text))


def normalise_title(title):
    """Lowercase, punctuation-free title, used to spot the same story twice.

    Publishers truncate and re-case headlines differently across feeds:
      "Pentagon spectrum sale could put Trump's Golden Dome plan at risk"
      "Pentagon Spectrum Sale Could Put Trump's Golden Dome Plan At R… - WQXC"
    must collapse to one entry. Because one copy is usually TRUNCATED, a plain
    equality test fails; callers pair this with title_is_duplicate() below.
    """
    text = strip_html(title).lower()
    text = re.sub(r"[‘’“”]", "'", text)
    # Ellipsis marks where the publisher cut the headline off.
    text = text.replace("…", " ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Drop the publisher suffix Google News appends ("... - The Hill").
    text = re.sub(r"\s+-\s+[^-]{2,40}$", "", text)
    return text


# Headlines shorter than this are too generic to compare safely.
_TITLE_DEDUPE_MIN = 30
# How many leading words must agree before two headlines count as the same.
_TITLE_DEDUPE_WORDS = 8


def title_is_duplicate(new_key, seen_keys):
    """True if new_key looks like a headline already in seen_keys.

    Compares leading words rather than the whole string, because the same story
    appears both complete and truncated with an ellipsis. The shorter headline
    must be a prefix of the longer one; requiring a long shared prefix stops
    unrelated headlines that merely begin alike from being merged.
    """
    if not new_key or len(new_key) < _TITLE_DEDUPE_MIN:
        return False
    words = new_key.split()
    if len(words) < 4:
        return False
    prefix = " ".join(words[:_TITLE_DEDUPE_WORDS])
    for old in seen_keys:
        if old == new_key:
            return True
        old_words = old.split()
        if len(old_words) < 4:
            continue
        old_prefix = " ".join(old_words[:_TITLE_DEDUPE_WORDS])
        # Either headline may be the truncated one.
        if old_prefix == prefix:
            return True
        short, long_ = sorted((prefix, old_prefix), key=len)
        if len(short) >= _TITLE_DEDUPE_MIN and long_.startswith(short):
            return True
    return False


def is_official(url):
    """True when the URL's HOST is an official government domain.

    Matching the raw string was wrong: any '.gov' appearing in a path or query
    (e.g. an article slug quoting a .gov page) promoted non-official sources to
    official. Only the hostname is considered now.
    """
    host = url_host(url)
    if not host:
        return False
    if host in OFFICIAL_DOMAINS or host.endswith(".gov"):
        return True
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)


def url_host(url):
    """Hostname of a URL, lowercased, without a leading www. '' when invalid."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# Reference/aggregator pages that match on the member's NAME but contain no
# reporting. They crowd out real articles because every bio page mentions the
# senator and often the topic in passing.
NON_ARTICLE_HOSTS = {
    "en.wikipedia.org", "ballotpedia.org", "www.britannica.com",
    "biography.com", "www.biography.com", "dbpedia.org",
    "www.wikidata.org", "simple.wikipedia.org",
}

# URL path fragments that mark an index/tag/author page rather than an article.
NON_ARTICLE_PATH_HINTS = (
    "/people/", "/person/", "/author/", "/authors/", "/tag/", "/tags/",
    "/category/", "/topics/", "/profile/", "/staff/", "/bio",
)


def is_article_url(url):
    """False for reference pages and section indexes that are not articles."""
    host = url_host(url)
    if not host:
        return False
    if host in NON_ARTICLE_HOSTS or any(host.endswith("." + h)
                                       for h in NON_ARTICLE_HOSTS):
        return False
    path = (urlparse(str(url)).path or "").lower()
    if any(hint in path for hint in NON_ARTICLE_PATH_HINTS):
        return False
    return True


def http_get_text(url, params=None, timeout=25):
    res = requests.get(url, params=params, timeout=timeout,
                        headers={"User-Agent": USER_AGENT})
    res.raise_for_status()
    return res.text


CREC_ID_RE = re.compile(
    r"^CREC-(\d{4})-(\d{2})-(\d{2})(?:-(pt\d+)-(Pg[A-Za-z0-9]+))?(?:-\d+)?$")


def parse_crec_package_id(pid):
    """Accept both govinfo CREC id shapes -> date / part / page.

    The search API returns two forms and BOTH must be handled:
      packageId : 'CREC-2026-09-14'                (whole issue)
      granuleId : 'CREC-26-09-14-pt1-PgE914-4'   (one page range, -N suffix)

    The old regex demanded a page component and no trailing counter, so it
    matched neither form and every Congressional Record result was silently
    discarded, emptying this entire source.
    """
    m = CREC_ID_RE.match(str(pid or ""))
    if not m:
        return None
    return {"date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
            "part": m.group(4) or "",
            "page": m.group(5) or ""}


def crec_granule_url(res):
    """Public, crawlable HTML URL for a CREC search hit.

    Prefers granuleId (a real page range) and falls back to the issue-level
    packageId. Both are served under /content/pkg/<id>/html/<id>.htm.
    """
    for key in ("granuleId", "packageId"):
        gid = res.get(key)
        if gid:
            return f"https://www.govinfo.gov/content/pkg/{gid}/html/{gid}.htm"
    return ""


def govinfo_fetch_granule(res, max_chars=ARTICLE_MAX_CHARS):
    """Download the actual document text for a govinfo search hit.

    Two things were wrong here originally:

    1. The search API returns metadata only (no 'text'/'html' key), so the old
       code built a title-only record that always failed the member-naming gate.
    2. The guessed public URL /content/pkg/<id>/html/<id>.htm 302-redirects to
       govinfo's /error page for CREC granules, yet still returns HTTP 200 with
       an HTML error body. That silently produced ~19 KB of boilerplate that
       looked like a document but matched neither the member nor the topic.

    The search response carries an authoritative download.txtLink for the
    granule, which serves the real text. That is tried first; the public URL is
    kept only as a last resort and its body is rejected when it is an error
    page.
    """
    candidates = []

    # 1. Authoritative per-granule text link from the search response.
    dl = res.get("download")
    if isinstance(dl, dict) and dl.get("txtLink"):
        candidates.append(dl["txtLink"])

    # 2. Same granule via the API, derived from the ids.
    pkg = res.get("packageId")
    gid = res.get("granuleId")
    if pkg and gid:
        candidates.append(f"https://api.govinfo.gov/packages/{pkg}"
                          f"/granules/{gid}/htm")

    # 3. Public content URL (last resort; may be an error page).
    for key in ("granuleId", "packageId"):
        if res.get(key):
            ident = res[key]
            candidates.append(
                f"https://www.govinfo.gov/content/pkg/{ident}/html/{ident}.htm")
            break

    for url in candidates:
        try:
            r = requests.get(url, timeout=25, headers={
                "X-Api-Key": GOVINFO_API_KEY,
                "User-Agent": USER_AGENT,
            })
        except requests.RequestException as exc:
            log.debug("govinfo fetch failed %s: %s", url, exc)
            continue
        if r.status_code != 200:
            log.debug("govinfo fetch HTTP %s %s", r.status_code, url)
            continue
        raw = r.text
        if re.search(r"the page you requested cannot be found", raw, re.I):
            log.debug("govinfo returned an error page for %s", url)
            continue
        # Line breaks are preserved for Congressional Record granules because
        # crec_blocks() anchors its speaker markers on line starts.
        text = strip_html_keep_lines(raw)
        if len(text) > 200:
            return text[:max_chars]
    return ""


def govinfo_search(collection, query, limit=20):
    """govinfo full-text search. Returns [] when no key is configured."""
    if not GOVINFO_API_KEY:
        log.warning("no govinfo key set (GOVINFO_API_KEY / DATA_GOV_API_KEY)")
        return []

    body = {
        "query": f'collection:{collection} ({query})',
        "pageSize": min(int(limit), 100),
        "offsetMark": "*",
        "sorts": [{"field": "publishdate", "sortOrder": "DESC"}],
    }
    try:
        res = requests.post(
            GOVINFO_SEARCH_URL, json=body, timeout=30,
            headers={"X-Api-Key": GOVINFO_API_KEY,
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        log.warning("govinfo search failed: %s", exc)
        return []

    if res.status_code != 200:
        log.warning("govinfo -> HTTP %s", res.status_code)
        return []
    try:
        return res.json().get("results", [])
    except ValueError:
        return []


def source_congressional_record(phrase, member_name, limit=20):
    """Members' own floor statements on the programme (official record).

    The search API is metadata-only, so each hit is expanded into its real
    document text via govinfo_fetch_granule(). Items whose text cannot be
    retrieved are dropped rather than passed on as a bare title, which would
    never survive the member-naming gate downstream.
    Attribution is decided per SPEAKING TURN, not per document. A Record
    granule is a day of floor business covering 100+ members, so a granule
    that merely contains the surname -- or lists the member as a cosponsor in
    someone else's amendment -- proves nothing. Only text inside this member's
    own speaker block, with the topic hit occurring BEFORE the next speaker
    takes over, is attributed to them; anything else is dropped outright
    instead of being recorded as a spurious "Neutral".
    """
    out = []
    seen = set()
    surname = member_name.strip().split()[-1]
    warn_once = True

    # Scope the full-text search to the member as well as the topic. A
    # topic-only query returns the whole day's amendments, almost none of which
    # mention this person, and the member gate then discards nearly all of
    # them. The surname is used because the Congressional Record indexes names
    # in ALL CAPS and often without the first name.
    queries = [f'"{phrase}" AND "{surname}"', f'"{phrase}"']
    for query in queries:
        if len(out) >= limit:
            break
        for res in govinfo_search(GOVINFO_CREC, query, limit=limit):
            pid = res.get("granuleId") or res.get("packageId")
            parsed = parse_crec_package_id(pid)
            if not parsed or pid in seen:
                continue
            title = (strip_html(res.get("title", "") or "")
                     or f"Congressional Record {parsed['date']}")
            # Long documents: fetch well past the model excerpt cap so the
            # member/topic gate sees the whole record, not just its first page.
            body = govinfo_fetch_granule(res, max_chars=GOVINFO_MAX_CHARS)
            if not body:
                log.debug("no document text for CREC hit %s", pid)
                continue
            # ---- speaker-level attribution -------------------------------
            # Not every CREC granule exposes speaker turns (committees lists,
            # bare amendment text). Rather than fall back to the old
            # document-wide surname match -- which is exactly the bug this
            # replaces -- refuse the hit and say so loudly once per run.
            if not crec_has_speakers(body):
                if warn_once:
                    warn_once = False
                    print("[!] CREC granule with no recoverable speaker "
                          "markers; skipped to avoid mis-attribution. "
                          "Check the CREC_SPEAKER_RE pattern if this is "
                          "unexpected.", flush=True)
                log.warning("CREC %s: no speaker markers; skipped", pid)
                continue

            # Only this member's OWN speaking turns, and only those in which
            # the topic appears BEFORE the next speaker takes over.
            mine = crec_member_sections(body, member_name)
            if not mine:
                log.debug("CREC %s: no on-topic turn spoken by %s",
                          pid, member_name)
                continue

            seen.add(pid)
            out.append({
                "title": title,
                # Title names the member so the sheet entry is self-describing,
                # and the text is ONLY their own words, so the model judges
                # their actual statement rather than the whole day's debate.
                "text": f"{title} ({member_name}). {mine}"[:GOVINFO_MAX_CHARS],
                "url": crec_granule_url(res),
                "date": parsed["date"],
                "source": "Congressional Record",
                "official": True,
                # Text came from the authoritative granule download, not from
                # the public URL; enrich_candidates() must not re-fetch it.
                "body_fetched": True,
            })
    return out


def source_federal_register(phrase, limit=10):
    """Federal Register notices/rules touching the programme (keyless)."""
    out = []
    try:
        res = requests.get(
            "https://www.federalregister.gov/api/v1/documents",
            params={"conditions[term]": phrase, "per_page": limit,
                    "order": "relevance", "format": "json"},
            timeout=25, headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        log.warning("Federal Register failed: %s", exc)
        return []
    if res.status_code != 200:
        return []
    try:
        results = res.json().get("results", [])
    except ValueError:
        return []

    for doc in results:
        out.append({
            "title": strip_html(doc.get("title", "") or ""),
            "text": strip_html(doc.get("abstract") or doc.get("excerpts") or ""),
            "url": doc.get("html_url", ""),
            "date": doc.get("publication_date", "") or "",
            "source": "Federal Register",
            "official": True,
        })
    return out


def source_bill_text(phrase, member_name, limit=15):
    """Bill text that names this member (official record).

    Bill packages are whole numbered documents (BILLS-119hr1234), served as
    .htm alongside the .txt that the full-text search actually indexed. The
    .txt is preferred here because it is plain text and needs no tag stripping.
    """
    surname = member_name.strip().split()[-1]
    out = []
    for res in govinfo_search(GOVINFO_BILLS,
                              f'"{phrase}" AND "{surname}"', limit=limit):
        pid = res.get("granuleId") or res.get("packageId", "")
        if not pid:
            continue
        title = strip_html(res.get("title", "") or "") or pid
        body = govinfo_fetch_granule(res, max_chars=GOVINFO_MAX_CHARS)
        if not body:
            log.debug("no document text for bill %s", pid)
            continue
        out.append({
            "title": title,
            "text": f"{title}. {body}",
            "url": crec_granule_url(res),
            "date": res.get("dateIssued", "") or "",
            "source": "Legislation (govinfo)",
            "official": True,
            "body_fetched": True,
        })
    return out


def resolve_gnews_url(redirect_url, timeout=15):
    """Follow a Google News RSS redirect to the publisher's real article URL.

    Google News links are news.google.com/rss/articles/CBMi... wrappers. They
    serve a JS/HTML interstitial rather than a redirect, so requests alone does
    not resolve them. The real destination is embedded in that page as a
    data-n-au / data-n-a-ts style attribute or a direct link, which is read out
    here. Returns '' when the target cannot be determined, in which case the
    item stays headline-only (the previous, lossy behaviour).
    """
    if not redirect_url or "news.google.com" not in redirect_url:
        return redirect_url
    try:
        res = requests.get(redirect_url, timeout=timeout,
                           headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        log.debug("gnews resolve failed: %s", exc)
        return ""
    if res.status_code != 200:
        return ""

    html = res.text
    # Modern interstitial: the publisher URL is in one of these attributes.
    for attr in ("data-n-au", "data-n-a-ts", "data-n-a-id"):
        m = re.search(rf'{attr}="(https?://[^"]+)"', html)
        if m and "news.google.com" not in m.group(1):
            return m.group(1)
    # Fallback: first outbound link that is not a Google property.
    for m in re.finditer(r'href="(https?://[^"]+)"', html):
        cand = m.group(1)
        if "news.google.com" not in cand and "google.com" not in cand:
            return cand
    return ""


def source_gnews(query, limit=MAX_PER_QUERY):
    """Google News RSS. Links are opaque redirects -> resolved when possible."""
    if feedparser is None:
        log.warning("feedparser not installed: pip install feedparser")
        return []
    url = ("https://news.google.com/rss/search?q="
           f"{quote(query)}&hl=en-US&gl=US&ceid=US:en")
    try:
        feed = feedparser.parse(url)
    except Exception as exc:
        log.warning("feedparser failed for %r: %s", query, exc)
        return []

    out = []
    for entry in feed.entries[:limit]:
        title = strip_html(entry.get("title", "") or "")
        link = (entry.get("link", "") or "").strip()
        if not title or not link:
            continue
        src = entry.get("source")
        publisher = (strip_html(src.get("title", "") or "")
                     if isinstance(src, dict) else "")
        resolved = resolve_gnews_url(link)
        out.append({
            "title": title,
            "text": title,
            # Prefer the publisher URL so the body can actually be fetched;
            # fall back to the redirect when resolution fails.
            "url": resolved or link,
            "date": entry.get("published", "") or "",
            "source": (f"Google News: {publisher}" if publisher
                       else "Google News"),
            # Official status must follow the resolved publisher, not the
            # news.google.com wrapper, which is never official.
            "official": is_official(resolved) if resolved else False,
        })
    return out


def source_senate_press_releases(member_name, phrase=TOPIC_PHRASES[0],
                                 max_pages=4, per_page=100):
    """Committee press releases from the WordPress REST API (keyless).

    commerce.senate.gov exposes /wp-json/wp/v2/dem_press_releases, which is the
    primary record of the member's own position. It is worth querying directly
    rather than waiting on DuckDuckGo, which rate-limits the site: queries.

    Note: the ?search= parameter returns HTTP 404 on this host (an WAF rule),
    so pages are fetched and filtered client-side instead. Relevance is checked
    against the release title AND body, and the member must be named so that
    releases by other committee members on the same subject are not misfiled.
    """
    out = []
    seen = set()
    base = "https://www.commerce.senate.gov/wp-json/wp/v2/dem_press_releases"

    for page in range(1, max_pages + 1):
        try:
            res = requests.get(f"{base}?per_page={per_page}&page={page}",
                               timeout=30,
                               headers={"User-Agent": USER_AGENT})
        except requests.RequestException as exc:
            log.debug("press release page %d failed: %s", page, exc)
            break
        if res.status_code != 200:
            log.debug("press release page %d -> HTTP %s",
                      page, res.status_code)
            break
        try:
            posts = res.json()
        except ValueError:
            break
        if not isinstance(posts, list) or not posts:
            break

        for post in posts:
            link = (post.get("link") or "").strip()
            if not link or link in seen:
                continue
            title = strip_html((post.get("title") or {}).get("rendered", "")
                               or "")
            body_html = ((post.get("content") or {}).get("rendered", "")
                         or (post.get("excerpt") or {}).get("rendered", ""))
            text = f"{title}. {strip_html(body_html)}".strip(". ")
            if not title or not text:
                continue
            if not topic_hit(text) or not member_is_named(member_name, text):
                continue
            seen.add(link)
            out.append({
                "title": title,
                "text": text,
                "url": link,
                "date": (post.get("date") or "")[:10],
                "source": "Senate Commerce Committee",
                "official": True,
                "body_fetched": True,
            })
    return out


def source_publisher_feeds(limit_per_feed=FEED_LIMIT):
    """Curated defense/space RSS feeds. Real URLs => readable article bodies."""
    if feedparser is None:
        log.warning("feedparser not installed: pip install feedparser")
        return []

    out = []
    for publisher, feed_url in PUBLISHER_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as exc:
            log.debug("feed failed %s: %s", publisher, exc)
            continue
        for entry in feed.entries[:limit_per_feed]:
            title = strip_html(entry.get("title", "") or "")
            link = (entry.get("link", "") or "").strip()
            if not title or not link:
                continue
            # Cheap prefilter on the headline before any page fetch.
            if not topic_hit(title):
                continue
            desc = entry.get("summary") or entry.get("description") or ""
            out.append({
                "title": title,
                "text": f"{title}. {strip_html(desc)}".strip(". "),
                "url": link,
                "date": entry.get("published", "") or "",
                "source": publisher,
                "official": is_official(link),
            })
    return out


def source_duckduckgo(query, limit=MAX_PER_QUERY):
    """Web search through the duckduckgo_search library (DDGS).

    This is the primary press discovery source: unlike Google News, the results
    are direct publisher URLs, so the article body can be downloaded and read.

    The library handles its own sessions, tokens and captcha detection, which
    the hand-rolled HTML scraper could not. Any throttle or transport error is
    caught here and degrades to [] so one blocked query never kills the run.
    """
    if DDGS is None:
        log.warning("duckduckgo_search not installed: "
                    "pip install duckduckgo_search")
        return []

    hits = []
    for attempt in range(DDG_MAX_ATTEMPTS):
        wait = _throttle("duckduckgo", DDG_MIN_INTERVAL)
        if wait:
            time.sleep(wait)
        try:
            # DDGS.__init__ force-enables the "always" filter right before its
            # "renamed to ddgs" nag, which defeats any surrounding ignore. The
            # library's simplefilter call is neutralised during construction so
            # the ignore below survives; the global filter list is restored by
            # catch_warnings on exit.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                real_simplefilter = warnings.simplefilter
                warnings.simplefilter = lambda *a, **kw: None
                try:
                    ddgs = DDGS()
                finally:
                    warnings.simplefilter = real_simplefilter
                with ddgs:
                    hits = ddgs.text(keywords=query, region="us-en",
                                     safesearch="moderate",
                                     max_results=limit)
        except Exception as exc:  # library raises Ratelimit/SSLError etc.
            log.warning("duckduckgo_search failed for %r: %s", query, exc)
            hits = []
        if hits:
            break
        # An empty answer here is usually throttling or transient backend
        # flakiness rather than a genuine "no matches" (measured: one query
        # returned 0 hits, then 10 on a spaced retry). Back off
        # exponentially with jitter so parallel-looking bursts do not
        # re-trigger the limiter.
        if attempt < DDG_MAX_ATTEMPTS - 1:
            delay = DDG_BACKOFF_BASE * (2 ** attempt)
            delay *= 0.75 + random.random() * 0.5
            log.debug("retrying %r in %.1fs (attempt %d/%d)",
                      query, delay, attempt + 1, DDG_MAX_ATTEMPTS)
            time.sleep(delay)
    if not hits:
        log.warning("duckduckgo_search no results for %r", query)

    out = []
    for hit in hits or []:
        url = (hit.get("href") or hit.get("url") or "").strip()
        title = strip_html(hit.get("title") or "")
        if not url.startswith("http") or not title:
            continue
        snippet = strip_html(hit.get("body") or "")
        out.append({
            "title": title,
            "text": f"{title}. {snippet}".strip(". "),
            "url": url,
            # DDGS returns no publication date for web results; the field is
            # kept for schema consistency with the RSS/API sources.
            "date": str(hit.get("date") or "").strip(),
            "source": f"Web search: {url_host(url)}",
            "official": is_official(url),
        })
        if len(out) >= limit:
            break
    return out


def extract_article_text(url, max_chars=ARTICLE_MAX_CHARS):
    """Fetch an article page and extract readable body text.

    trafilatura gives clean boilerplate-free text; the regex fallback covers
    pages where it returns nothing. None when paywalled / JS-only / not HTML.
    """
    try:
        res = requests.get(url, timeout=25, headers={"User-Agent": USER_AGENT},
                           allow_redirects=True)
    except requests.RequestException as exc:
        log.debug("article fetch failed %s: %s", url, exc)
        return None
    if res.status_code != 200:
        return None
    if "html" not in res.headers.get("Content-Type", "").lower():
        return None

    raw = res.text
    if trafilatura is not None:
        try:
            text = trafilatura.extract(raw, url=res.url,
                                        include_comments=False,
                                        include_tables=False)
            if text and len(text) > 200:
                return text[:max_chars]
        except Exception as exc:
            log.debug("trafilatura failed for %s: %s", res.url, exc)

    body = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|form)"
                  r"[^>]*>.*?</\1>", " ", raw)
    body = strip_html(body)
    return body[:max_chars] if len(body) > 200 else None


# ---------------------------------------------------------------------------
# AI ANALYSIS (local Ollama)
# ---------------------------------------------------------------------------
STANCE_VALUES = {
    "for": "For",
    "supports": "For",
    "support": "For",
    "in favor": "For",
    "against": "Against",
    "opposes": "Against",
    "oppose": "Against",
    "opposed": "Against",
    "against it": "Against",
    "neutral": "Neutral",
    "mixed": "Neutral",
    "no position": "Neutral",
    "unclear": "Neutral",
}


def ollama_available():
    """True if the local model server answers /api/tags."""
    try:
        res = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
    except requests.RequestException:
        return False
    return res.status_code == 200


def judge_article(member_name, source_name, date, text, url):
    """One model call per article: is it about the programme, and what stance?

    Returns a dict with: related (bool), stance (For/Against/Neutral),
    reason, summary. Never raises: transport/parse failures return related
    False so a flaky model cannot fabricate a verdict.
    """
    excerpt = strip_html(text)[:4000]

    prompt = f"""Read the article below and judge one politician's position.

POLITICIAN: {member_name}
PROGRAMME: the "Golden Dome" space-based missile defence programme
SOURCE: {source_name}
DATE: {date or "Unknown"}

CONTENT:
{excerpt}

Answer two questions using ONLY the content above. Do not invent facts.
1. RELATED: is this article genuinely about the Golden Dome programme
   (the missile defence shield initiative)? A passing mention, or coverage of
   some unrelated bill, agency or topic is NOT related.
2. STANCE: {member_name}'s position toward the Golden Dome programme.
   - "For": endorses, supports, sponsors or defends it.
   - "Against": opposes, criticises, blocks or seeks to cut it.
   - "Neutral": only reports on it, or mixes support and criticism, and shows
     no clear position.
   Use "Neutral" if the content does not show {member_name}'s position.

Respond with strictly one JSON object and nothing else:
{{
  "related": true or false,
  "stance": "For | Against | Neutral",
  "reason": "one short sentence justifying the verdict",
  "summary": "one or two sentences describing what the article says"
}}"""

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0, "num_predict": 400},
    }

    try:
        res = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload,
                            timeout=OLLAMA_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("ollama unreachable: %s", exc)
        return None
    if res.status_code != 200:
        log.warning("ollama HTTP %s", res.status_code)
        return None

    try:
        parsed = json.loads(res.json()["message"]["content"])
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("unexpected ollama payload: %s", exc)
        return None
    if not isinstance(parsed, dict):
        return None

    related = parsed.get("related")
    if isinstance(related, str):
        related = related.strip().lower() in ("true", "yes", "1")
    related = bool(related)

    # Small models drift outside the vocabulary: normalise, default Neutral.
    raw = str(parsed.get("stance", "")).strip().lower()
    stance = STANCE_VALUES.get(raw)
    if stance is None:
        stance = STANCE_VALUES.get(next(
            (k for k in STANCE_VALUES if k in raw), ""), "Neutral")

    # Hard guard: evidence that never names the politician proves no stance,
    # whatever the model concluded.
    if not member_is_named(member_name, excerpt):
        stance = "Neutral"

    return {
        "related": related,
        "stance": stance,
        "reason": str(parsed.get("reason", ""))[:220].strip(),
        "summary": str(parsed.get("summary", ""))[:400].strip(),
        "url": url,
    }


# ---------------------------------------------------------------------------
# COLLECTION
# ---------------------------------------------------------------------------
def build_queries(member_name):
    """Name-scoped query angles that widen coverage of what this person said.

    Every query carries the member's FULL NAME. Measured on DuckDuckGo:
        '"Maria Cantwell" "golden dome"'            -> 19 hits
        '"Maria Cantwell" "missile defense shield"' ->  3 hits
        'Cantwell "golden dome"'                     ->  0 hits
        '"golden dome"'            (topic only)      ->  4 hits, almost all
                                                          irrelevant pages

    Topic-only queries return the general discourse rather than this person's
    statements, and bare-surname queries match nothing. So the name stays in
    every query and only the topic term and the target site vary.

    Order matters: the broad, high-yield queries come first so that if the run
    is cut short the useful results are already collected.
    """
    name = member_name.strip()
    surname = name.split()[-1]
    topic = TOPIC_PHRASES[0]          # "golden dome"

    # Official self-authored statements first: press releases and floor
    # remarks are the primary record of her position.
    queries = [
        f'"{name}" "{topic}"',
        f'"{name}" "missile defense shield"',
        f'"{name}" "{topic}" site:senate.gov',
        f'"{name}" "{topic}" press release',
        f'"{name}" "{topic}" statement',
        f'"{name}" "{topic}" site:commerce.senate.gov',
        f'"{name}" "{topic}" site:house.gov',
        f'"{name}" "{topic}" site:defense.gov',
        f'"{name}" "space-based interceptor"',
        f'"{name}" "missile defense"',
        f'"{name}" "{topic}" letter',
    ]

    # Surname-scoped fallbacks: measured as weak on DuckDuckGo but they do
    # surface press copy that drops the first name.
    queries += [
        f'{surname} "{topic}"',
        f'{surname} "missile defense shield"',
    ]

    # Deduplicate while preserving order.
    seen = set()
    out = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


class Progress:
    """Single-line, self-updating progress indicator.

    DuckDuckGo throttling makes a run take minutes with long silent gaps, so
    the bar shows which stage is running and how far along it is. Output is
    written to stderr with a carriage return, leaving stdout clean for the
    final source table. Falls back to plain periodic lines when stderr is not
    a terminal (piped output, CI) so logs stay readable.
    """

    BAR_WIDTH = 28

    def __init__(self, total, label, enabled=True):
        self.total = max(int(total), 0)
        self.label = label
        self.n = 0
        self.enabled = enabled
        self.tty = sys.stderr.isatty()
        self._last_pct = -1
        self.finished = False
        self.started = time.time()

    def advance(self, step=1, note=""):
        self.n += step
        self.render(note)
        return self.n

    def render(self, note=""):
        if not self.enabled:
            return
        done = min(self.n, self.total) if self.total else 0
        pct = int(done * 100 / self.total) if self.total else 0
        elapsed = time.time() - self.started
        if self.tty:
            filled = int(self.BAR_WIDTH * done / self.total) if self.total else 0
            bar = "#" * filled + "." * (self.BAR_WIDTH - filled)
            line = (f"  [{bar}] {done}/{self.total} "
                    f"{pct:3d}%  {self.label}")
            if note:
                line += f"  {note}"
            # Pad to erase whatever the previous, longer line left behind.
            sys.stderr.write("\r" + line.ljust(100))
            sys.stderr.flush()
        elif pct >= self._last_pct + 10 or done == self.total:
            self._last_pct = pct
            sys.stderr.write(f"  {self.label}: {done}/{self.total} "
                             f"({pct}%) {int(elapsed)}s\n")
            sys.stderr.flush()

    def done(self, note=""):
        """Finish the bar and move to a fresh line. Safe to call twice."""
        if not self.enabled or self.finished:
            return
        self.finished = True
        self.n = self.total
        if self.tty:
            self.render(note)
            sys.stderr.write("\n")
            sys.stderr.flush()
        else:
            sys.stderr.write(f"  {self.label}: done"
                             f" ({int(time.time() - self.started)}s)\n")
            sys.stderr.flush()


def _tick(progress, note=""):
    """Advance the progress bar by one unit, if one is attached."""
    if progress:
        progress.advance(note=note)


def collect_candidates(member_name, official_only=False, per_query=MAX_PER_QUERY,
                       progress=None):
    """Gather deduplicated candidate articles naming this member.

    Attribution is verified against the article BODY, not just the headline:
    search snippets and body text name the politician far more often than
    headlines do, and the body is what the stance judgement reads.

    `progress`, when given, is advanced as each collector finishes so a long
    throttled run shows movement instead of appearing hung.
    """
    candidates = []
    seen = set()
    seen_titles = set()

    def add(items):
        kept = 0
        for it in items:
            url = it.get("url")
            if not url or url in seen:
                continue
            # Reference pages and section indexes (Wikipedia, /people/, /tag/)
            # match on the member's name but contain no reporting. They used to
            # flood the result set and crowd out genuine articles.
            if not is_article_url(url):
                log.debug("skipped non-article page: %s", url)
                continue
            # The same story routinely arrives from three collectors at once
            # (DuckDuckGo, Google News, a publisher feed) under slightly
            # different titles, so dedupe on a normalised title too.
            tkey = normalise_title(it.get("title", ""))
            if tkey and title_is_duplicate(tkey, seen_titles):
                log.debug("duplicate story, skipped: %s", it.get("title"))
                continue
            blob = f"{it.get('title', '')} {it.get('text', '')}"
            # The member must be named. The topic is deliberately NOT required
            # here: the most valuable official pieces are titled around the
            # member's action ("Cantwell Sounds the Alarm...") and only name
            # the programme in the body. topic_hit is enforced once the body
            # has been downloaded, in enrich_candidates().
            if not member_is_named(member_name, blob):
                continue
            seen.add(url)
            if tkey:
                seen_titles.add(tkey)
            candidates.append(it)
            kept += 1
        return kept

    # 1. Official record: floor statements and bill text.
    if GOVINFO_API_KEY:
        for phrase in ("golden dome", "missile defense shield"):
            add(source_congressional_record(phrase, member_name,
                                            limit=per_query))
            _tick(progress, "Congressional Record")
            add(source_bill_text(phrase, member_name, limit=per_query))
            _tick(progress, "bill text")
    else:
        print("[i] govinfo skipped (no DATA_GOV_API_KEY/GOVINFO_API_KEY set)",
              flush=True)
        if progress:
            progress.advance(2)

    # 2. Federal Register (keyless).
    add(source_federal_register("Golden Dome missile defense",
                                limit=per_query))
    _tick(progress, "Federal Register")

    # 3. Her own committee's press releases (keyless WordPress REST API).
    #    This is the primary self-authored record and is far more reliable than
    #    waiting on a throttled web search for site:senate.gov results.
    add(source_senate_press_releases(member_name))
    _tick(progress, "Senate press releases")

    if official_only:
        return candidates

    queries = build_queries(member_name)

    # 4. Web search (real URLs => readable bodies). Primary press discovery.
    for query in queries:
        add(source_duckduckgo(query, limit=per_query))
        if progress:
            progress.advance(note=f"web search: {truncate(query, 44)}")

    # 5. Publisher feeds: curated defense/space RSS.
    add(source_publisher_feeds())
    _tick(progress, "publisher feeds")

    # 6. Google News, for breadth. Redirects are resolved so the publisher
    #    article body can be fetched.
    for query in queries:
        add(source_gnews(query, limit=per_query))
        if progress:
            progress.advance(note=f"Google News: {truncate(query, 40)}")

    if progress:
        progress.done()

    return candidates


def enrich_candidates(member_name, candidates, progress=None):
    """Attach the best available body text and enforce the topic filter.

    The headline+summary is kept as the floor: Google News links that cannot be
    resolved are judged on the headline alone. Everything else must prove BOTH
    the member and the programme in its real body text before reaching the
    model. Each item is one page download, so this stage also reports progress.
    """
    enriched = []
    for item in candidates:
        body = None
        url = item.get("url", "")
        existing = item.get("text", "") or ""

        # Items whose collector already downloaded the authoritative document
        # (Congressional Record, bill text) must NOT be re-fetched here: their
        # public govinfo URL answers HTTP 200 with an error page, which would
        # replace good text with 370 characters of boilerplate and then fail the
        # member/topic gate. Those records are trusted as collected.
        already_full = item.get("body_fetched") or len(existing) > ARTICLE_MAX_CHARS

        if already_full:
            text = existing
            if not member_is_named(member_name, text) or not topic_hit(text):
                continue
        elif url and "news.google.com" not in url:
            body = extract_article_text(url)
            if body:
                text = f"{item.get('title', '')}. {body}"
                # Re-verify against the real body: the snippet may not have named
                # them, and the body must actually be about the programme.
                if not member_is_named(member_name, text) or not topic_hit(text):
                    continue
            else:
                text = existing
                if not topic_hit(text):
                    continue
        else:
            text = existing
            if not topic_hit(text):
                continue

        if progress:
            progress.advance(note=truncate(item.get("title", ""), 46))
        rec = dict(item)
        rec["text"] = strip_html(text)
        rec["body_fetched"] = bool(body) or already_full
        enriched.append(rec)
    if progress:
        progress.done()
    return enriched


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------
def truncate(text, width):
    text = strip_html(text) or "-"
    return text if len(text) <= width else text[:width - 1] + "…"


def render_table(rows):
    """Fixed-width table of the sources found and their verdicts."""
    headers = ["#", "Source", "Official", "Verdict", "Title", "URL"]
    widths = [3, 24, 4, 9, 62, 58]

    data = []
    for idx, r in enumerate(rows, 1):
        data.append([
            str(idx),
            truncate(r["source"], widths[1]),
            "Yes" if r.get("official") else "No",
            r.get("verdict", "-"),
            truncate(r.get("title", ""), widths[4]),
            truncate(r.get("url", ""), widths[5]),
        ])

    def line(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"
    out = [line(headers), sep]
    out.extend(line(c) for c in data)
    return "\n".join(out)


def tally(rows):
    counts = {"For": 0, "Against": 0, "Neutral": 0, "Unclear": 0}
    for r in rows:
        counts[r.get("verdict", "Unclear")] = counts.get(
            r.get("verdict", "Unclear"), 0) + 1
    return counts


def overall_verdict(counts):
    """Majority verdict across the related sources."""
    scored = {k: v for k, v in counts.items() if k != "Unclear" and v}
    if not scored:
        return "NO CLEAR POSITION"
    top = max(scored, key=lambda k: scored[k])
    if scored[top] == 1 and len(scored) == 1:
        return f"{top.upper()} (single source)"
    return top.upper()


# ---------------------------------------------------------------------------
# GOOGLE SHEET OUTPUT
# ---------------------------------------------------------------------------
# Talks directly to the deployed Apps Script web app (APPS_SCRIPT_URL above),
# so this file stays self-contained. The deployed script in the user's message
# exposes:
#   GET  ?action=getExistingUrls&memberName=<name>  -> {"urls": [...]}
#   POST {"memberName":..., "source":{publication_date, source_name, stance,
#                                    summary, url}} -> {"status":"success",...}
# addSourceToMember() finds the member's row, scans columns H, M, R, ... (every
# 5th column starting at H) for the first empty block, lays down
# "Source N Date/Name/Stance/Summary/URL" headers when needed, and writes the
# 5 values there. So each source found becomes one new 5-column set appended
# to that member's row — exactly what the roster-wide run needs.


def _sheet_get(path_params):
    """GET the Apps Script web app. Returns parsed JSON or None."""
    if not APPS_SCRIPT_URL or "YOUR_DEPLOYMENT_ID" in APPS_SCRIPT_URL:
        return None
    try:
        res = requests.get(APPS_SCRIPT_URL, params=path_params,
                           timeout=SHEET_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("sheet GET failed: %s", exc)
        return None
    if res.status_code != 200:
        log.warning("sheet GET HTTP %s", res.status_code)
        return None
    try:
        return res.json()
    except ValueError:
        log.warning("sheet GET returned non-JSON")
        return None


def sheet_configured():
    """True when a deployed Apps Script URL is available and real."""
    return bool(APPS_SCRIPT_URL) and "YOUR_DEPLOYMENT_ID" not in APPS_SCRIPT_URL


def sheet_existing_urls(member_name):
    """URLs the sheet already holds for this member (dedup on re-runs)."""
    data = _sheet_get({"action": "getExistingUrls", "memberName": member_name})
    if not data:
        return set(), FIRST_SOURCE_COLUMN
    urls = set()
    for u in data.get("urls", []) or []:
        u = str(u or "").strip()
        if u:
            urls.add(u)
    next_col = FIRST_SOURCE_COLUMN + SOURCE_BLOCK_WIDTH * len(urls)
    return urls, next_col


def sheet_append_source(member_name, source_data):
    """Append one 5-col source block via doPost. True on success."""
    if not sheet_configured():
        return None
    payload = {
        "memberName": member_name,
        "source": {
            "publication_date": source_data.get("publication_date", "Unknown"),
            "source_name": source_data.get("source_name", "Unknown Source"),
            "stance": source_data.get("stance", "Unclear"),
            "summary": source_data.get("summary", ""),
            "url": source_data.get("url", "N/A"),
        },
    }
    try:
        res = requests.post(APPS_SCRIPT_URL, json=payload,
                            timeout=SHEET_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("sheet POST failed: %s", exc)
        return None
    if res.status_code != 200:
        log.warning("sheet POST HTTP %s", res.status_code)
        return None
    try:
        data = res.json()
    except ValueError:
        return None
    if data.get("status") == "success" and data.get("added"):
        return True
    log.warning("sheet POST rejected: %s", data)
    return False


def fetch_roster_names():
    """Every member name listed on the sheet (all 535 voting members)."""
    if not sheet_configured():
        return []
    data = _sheet_get({"action": "listMembers"})
    if data and data.get("members"):
        return [str(n).strip() for n in data["members"] if str(n).strip()]
    try:
        res = requests.get(
            "https://unitedstates.github.io/congress-legislators/"
            "legislators-current.json", timeout=30,
            headers={"User-Agent": USER_AGENT})
        legislators = res.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("roster fallback failed: %s", exc)
        return []
    names = []
    voting = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
              "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
              "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
              "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
              "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
    for item in legislators:
        terms = item.get("terms") or []
        if not terms:
            continue
        if (terms[-1].get("state") or "") not in voting:
            continue  # skip non-voting delegates, like populateCongressRoster
        first = (item.get("name") or {}).get("first", "")
        last = (item.get("name") or {}).get("last", "")
        full = f"{first} {last}".strip()
        if full:
            names.append(full)
    return names


class SheetWriter:
    """Writes accepted sources to the Google Sheet one at a time.

    Each push appends ONE 5-column source block to this member's row via the
    deployed addSourceToMember() (which finds the first empty H/M/R/... block
    and lays down "Source N Date/Name/Stance/Summary/URL" headers itself).
    Pushes happen immediately per accepted source so an interrupted run keeps
    what it already recorded.
    """

    def __init__(self, member_name, dry_run=False):
        self.member = member_name
        self.dry_run = dry_run
        self.next_col = FIRST_SOURCE_COLUMN
        self.seen_urls = set()
        self.pushed = 0
        self.skipped = 0
        self.failed = 0
        self._enabled = False

    def open(self):
        """Read the sheet's current state. Returns self, or None if unusable."""
        if not sheet_configured():
            print("[!] APPS_SCRIPT_URL is not configured; "
                  "skipping sheet write.", flush=True)
            return None
        try:
            self.seen_urls, self.next_col = sheet_existing_urls(self.member)
        except Exception as exc:  # sheet unreachable: never break the report
            log.warning("could not read sheet state: %s", exc)
            return None
        self._enabled = True
        return self

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc_info):
        return False

    def already_present(self, url):
        return bool(url) and url in self.seen_urls

    def push(self, row):
        """Append one accepted source. Returns 'pushed' | 'skipped' | 'failed'."""
        if not self._enabled:
            return "skipped"

        url = row.get("url", "")
        if self.already_present(url):
            self.skipped += 1
            return "skipped"

        if self.dry_run:
            start_col = self.next_col
            self.next_col += SOURCE_BLOCK_WIDTH
            self.pushed += 1
            print(f"     [dry] col {start_col}: {row.get('verdict')} | "
                  f"{truncate(row.get('title', ''), 56)}", flush=True)
            return "pushed"

        source_data = {
            "publication_date": row.get("date", "") or "Unknown",
            "source_name": row.get("source", "") or "Unknown Source",
            "stance": row.get("verdict", "Unclear"),
            "summary": strip_html(row.get("summary") or row.get("reason") or "")[:500],
            "url": url,
        }

        start_col = self.next_col
        try:
            ok = sheet_append_source(self.member, source_data)
        except Exception as exc:
            log.warning("sheet append raised: %s", exc)
            ok = None

        if not ok:
            # Cursor is left unchanged so a later run retries this source.
            self.failed += 1
            print(f"     ! sheet append failed: {truncate(url, 56)}", flush=True)
            return "failed"

        self.next_col += SOURCE_BLOCK_WIDTH
        self.pushed += 1
        self.seen_urls.add(url)
        print(f"     + sheet col {start_col}: {row.get('verdict')} | "
              f"{truncate(row.get('title', ''), 52)}", flush=True)
        time.sleep(SHEET_PUSH_DELAY)  # be gentle with Apps Script quotas
        return "pushed"

    def summary(self):
        return {"pushed": self.pushed, "skipped": self.skipped,
                "failed": self.failed}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Golden Dome stance tracker: goes through EVERYONE on the "
                    "sheet (all 535 voting members) and appends one 5-column "
                    "source block per source found. Just run with no "
                    "arguments. Paste your Apps Script /exec URL into "
                    "APPS_SCRIPT_URL before running.")
    p.add_argument("--max-articles", type=int, default=MAX_ARTICLES,
                   help=f"max articles sent to the model (default {MAX_ARTICLES})")
    p.add_argument("--official-only", action="store_true",
                   help="only official record sources (skip press/news)")
    p.add_argument("--no-body", action="store_true",
                   help="skip article downloads, judge on headlines/snippets only")
    p.add_argument("--no-model", action="store_true",
                   help="list every source found and skip the AI verdicts "
                        "(fast: no local model needed)")
    p.add_argument("--per-query", type=int, default=MAX_PER_QUERY,
                   help=f"results per query (default {MAX_PER_QUERY})")
    p.add_argument("--json", metavar="FILE",
                   help="also write the full results to this JSON file")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="suppress the progress bar")
    # On by default so a bare run writes to the sheet. --no-push-sheet opts
    # out; --push-sheet / --dry-run-sheet kept as aliases people already use.
    p.add_argument("--push-sheet", dest="push_sheet", action="store_true",
                   default=True,
                   help="append every judged source to the Google Sheet "
                        "(default: on)")
    p.add_argument("--no-push-sheet", dest="push_sheet", action="store_false",
                   help="run without writing anything to the sheet")
    p.add_argument("--dry-run-sheet", action="store_true",
                   help="show which columns would be written, write nothing")
    p.add_argument("--all-members", dest="all_members", action="store_true",
                   default=True,
                   help="go through EVERYONE listed on the sheet (default: on)")
    p.add_argument("--single", dest="all_members", action="store_false",
                   help="only process MEMBER / --member instead of the roster")
    p.add_argument("--member", metavar="NAME", default=None,
                   help="run for one member by name (overrides MEMBER)")
    p.add_argument("--limit-members", type=int, default=0,
                   help="only process the first N members (0 = no limit)")
    p.add_argument("--start-at", metavar="NAME", default=None,
                   help="skip ahead to this member name and continue from "
                        "there (resume a long run)")
    p.add_argument("--start-row", type=int, default=None,
                   help="sheet row number to start at (row 1 = header, so "
                        "2 = first member). You are also asked this "
                        "interactively when the roster run starts.")
    return p.parse_args(argv)


def run_one_member(member, args, bar_enabled, model_ok, idx=None, total=None):
    """Full pipeline for ONE member. Returns a summary dict for the run."""
    tag = f"[{idx}/{total}] " if idx and total else ""
    print("=" * 100)
    print(f" {tag}{TOPIC_NAME.upper()} STANCE REPORT — {member}")
    print(f" keywords: {', '.join(TOPIC_PHRASES[:3])} …")
    print("=" * 100, flush=True)

    if args.no_model:
        print("[*] Source listing only (--no-model): no AI verdicts.",
              flush=True)
    elif not model_ok:
        print(f"[!] No local model at {OLLAMA_HOST}. Skipping {member} — "
              f"verdicts need it (use --no-model to list sources only).",
              flush=True)
        return {"member": member, "sources": 0, "pushed": 0,
                "skipped": 0, "failed": 0, "verdict": "SKIPPED"}

    # Total units of collection work, so the bar can be sized up front:
    # 2 govinfo phrases x (Congressional Record + bill text), Federal Register,
    # press releases, publisher feeds, and one unit per web/News query.
    n_queries = 0 if args.official_only else len(build_queries(member))
    collect_total = (6 if GOVINFO_API_KEY else 2) + 1 + 1 + 1 + 2 * n_queries

    collect_bar = Progress(collect_total, "collecting sources",
                           enabled=bar_enabled)
    print("[*] Collecting sources (official record + press)...", flush=True)
    candidates = collect_candidates(member,
                                    official_only=args.official_only,
                                    per_query=args.per_query,
                                    progress=collect_bar)
    collect_bar.done()
    print(f"[*] {len(candidates)} candidate(s) name {member} "
          f"and mention {TOPIC_NAME}.", flush=True)

    if not candidates:
        print(f"[!] No sources found for {member} on {TOPIC_NAME}.")
        return {"member": member, "sources": 0, "pushed": 0,
                "skipped": 0, "failed": 0, "verdict": "NO SOURCES"}

    if not args.no_body:
        print("[*] Downloading article bodies...", flush=True)
        body_bar = Progress(len(candidates), "downloading bodies",
                            enabled=bar_enabled)
        candidates = enrich_candidates(member, candidates, progress=body_bar)
        body_bar.done()
        print(f"[*] {len(candidates)} source(s) left after body verification.",
              flush=True)

    candidates = candidates[:args.max_articles]

    rows = []
    related_dropped = 0

    # Sheet writer is opened BEFORE judging so each accepted source can be
    # appended the moment it is accepted. A run interrupted half-way keeps
    # everything it had already written.
    sheet = None
    if args.push_sheet or args.dry_run_sheet:
        dry = bool(args.dry_run_sheet)
        sheet = SheetWriter(member, dry_run=dry).open()
        if sheet:
            verb = "would append to" if dry else "appending to"
            print(f"[*] {verb} the Google Sheet as sources are accepted.",
                  flush=True)

    if args.no_model:
        # Source inventory: keep every candidate so coverage can be reviewed
        # without waiting on the model.
        for item in candidates:
            row = {**item, "verdict": "-", "reason": "",
                   "summary": strip_html(item.get("text", ""))[:300]}
            rows.append(row)
            if sheet:
                sheet.push(row)
    else:
        print(f"[*] Judging {len(candidates)} article(s) with AI...", flush=True)
        judge_bar = Progress(len(candidates), "AI judging", enabled=bar_enabled)
        for _idx, item in enumerate(candidates, 1):
            verdict = judge_article(member, item.get("source", ""),
                                    item.get("date", ""), item.get("text", ""),
                                    item.get("url", ""))
            judge_bar.advance(note=truncate(item.get("title", ""), 44))

            if verdict is None:
                row = {**item, "verdict": "Unclear",
                       "reason": "AI judgement unavailable"}
                rows.append(row)
                if sheet:
                    sheet.push(row)
                continue
            if not verdict["related"]:
                related_dropped += 1
                log.info("dropped as unrelated: %s", item.get("title"))
                continue

            row = {**item,
                   "verdict": verdict["stance"],
                   "reason": verdict["reason"],
                   "summary": verdict["summary"]}
            rows.append(row)
            # Accepted: record it now rather than batching to the end.
            if sheet:
                sheet.push(row)
        judge_bar.done()

    sheet_stats = sheet.summary() if sheet else None

    print()
    print("=" * 100)
    print(f" {'SOURCES FOUND' if args.no_model else 'SOURCES AND VERDICTS'} "
          f"— {member} / {TOPIC_NAME}")
    print("=" * 100)
    if rows:
        print(render_table(rows))
    else:
        print("(no article was judged related to the programme)")

    counts = tally(rows)
    verdict_label = ("LISTED" if args.no_model else overall_verdict(counts))
    print("-" * 100)
    if args.no_model:
        print(f" Total sources listed: {len(rows)}   (AI verdicts skipped)")
        official = sum(1 for r in rows if r.get("official"))
        print(f" Official: {official}   Other: {len(rows) - official}")
        by_source = {}
        for r in rows:
            by_source[r.get("source", "-")] = by_source.get(
                r.get("source", "-"), 0) + 1
        for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f"   {n:3}  {src}")
    else:
        print(f" Related sources: {len(rows)}   "
              f"(dropped as unrelated: {related_dropped})")
        print(f" FOR: {counts['For']}   AGAINST: {counts['Against']}   "
              f"NEUTRAL: {counts['Neutral']}   UNKNOWN: {counts['Unclear']}")
        print(f" OVERALL VERDICT: {overall_verdict(counts)}")
    if sheet_stats:
        print(f" SHEET: {sheet_stats['pushed']} written   "
              f"{sheet_stats['skipped']} already present   "
              f"{sheet_stats['failed']} failed")
    print("=" * 100)

    if args.json and not (idx and total):
        try:
            with open(args.json, "w") as f:
                json.dump({"member": member, "topic": TOPIC_NAME,
                           "model": (None if args.no_model else OLLAMA_MODEL),
                           "verdicts_included": not args.no_model,
                           "overall_verdict": (None if args.no_model
                                               else overall_verdict(counts)),
                           "counts": counts, "sources": rows}, f, indent=2)
            print(f"[*] Full results written to {args.json}")
        except OSError as exc:
            print(f"[!] could not write {args.json}: {exc}")

    return {"member": member, "sources": len(rows),
            "pushed": sheet_stats["pushed"] if sheet_stats else 0,
            "skipped": sheet_stats["skipped"] if sheet_stats else 0,
            "failed": sheet_stats["failed"] if sheet_stats else 0,
            "verdict": verdict_label, "counts": counts, "rows": rows}


def ask_start_row(total):
    """Interactively ask which sheet row to start at. Defaults to 2.

    Row 1 is the header, so row 2 = first member. Typing nothing (or anything
    unparseable / out of range) starts from the top. EOF (piped input) also
    starts from the top instead of crashing.
    """
    print(f"[*] Roster has {total} member(s) on sheet rows 2..{total + 1}.",
          flush=True)
    try:
        raw = input(f"Start at sheet row [2-{total + 1}] (Enter = 2): ").strip()
    except EOFError:
        print("[*] No input available; starting at row 2.", flush=True)
        return 2
    if not raw:
        return 2
    try:
        row = int(raw)
    except ValueError:
        print(f"[!] {raw!r} is not a number; starting at row 2.", flush=True)
        return 2
    if row < 2:
        print("[!] Row 1 is the header; starting at row 2.", flush=True)
        return 2
    if row > total + 1:
        print(f"[!] Row {row} is past the last row ({total + 1}); "
              f"starting at row 2.", flush=True)
        return 2
    return row


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    bar_enabled = not args.quiet

    # A bare run (no flags at all) does the full roster run and pushes to the
    # sheet: --all-members and --push-sheet both default to True, and --member
    # alone implies a single-member run.
    if args.member and args.all_members and \
            not any(a in (sys.argv[1:] if argv is None else argv)
                    for a in ("--all-members", "--single")):
        args.all_members = False

    if args.no_model:
        model_ok = True
    else:
        model_ok = ollama_available()
        if model_ok:
            print(f"[*] AI judge: {OLLAMA_MODEL} @ {OLLAMA_HOST}", flush=True)
        else:
            print(f"[!] No local model at {OLLAMA_HOST}. Start it with: "
                  f"ollama serve  (start the model via: ollama pull {OLLAMA_MODEL})",
                  flush=True)
            print("[!] Verdicts cannot be produced without it — exiting. "
                  "(use --no-model to list sources only)", flush=True)
            return 1

    # ---- roster-wide run: everyone listed on the sheet --------------------
    if args.all_members:
        members = fetch_roster_names()
        if not members:
            print("[!] Could not get the member list from the sheet; "
                  "check APPS_SCRIPT_URL.", flush=True)
            return 1
        full_total = len(members)
        start_row = 2
        if args.start_row is not None:
            # Explicit flag wins over everything (non-interactive runs).
            if args.start_row < 2:
                print("[!] --start-row 1 is the header; starting at row 2.",
                      flush=True)
            elif args.start_row > full_total + 1:
                print(f"[!] --start-row {args.start_row} is past the last "
                      f"row ({full_total + 1}); starting at row 2.",
                      flush=True)
            else:
                start_row = args.start_row
        elif args.start_at:
            target = args.start_at.strip().lower()
            pos = next((i for i, n in enumerate(members)
                        if n.strip().lower() == target), None)
            if pos is None:
                print(f"[!] --start-at {args.start_at!r} not in roster; "
                      f"starting from the top.", flush=True)
            else:
                start_row = pos + 2  # member index -> sheet row
        elif sys.stdin.isatty():
            # Interactive: ask every time so a later run can pick up where
            # the last one stopped.
            start_row = ask_start_row(full_total)
        else:
            print("[*] Starting at row 2 (non-interactive; use --start-row "
                  "or --start-at to resume).", flush=True)
        if start_row > 2:
            skipped_name = members[start_row - 2]
            print(f"[*] Resuming at row {start_row}: {skipped_name} "
                  f"({start_row - 1}/{full_total}).", flush=True)
            members = members[start_row - 2:]
        if args.limit_members and args.limit_members > 0:
            members = members[:args.limit_members]
        total = len(members)
        print("=" * 100)
        print(f" ROSTER RUN — {total} member(s), sheet rows "
              f"{start_row}..{start_row + total - 1}, appending one 5-column "
              f"source block per source found")
        print("=" * 100, flush=True)

        results = []
        roster_bar = Progress(total, "roster progress", enabled=bar_enabled)
        for i, name in enumerate(members, 1):
            sheet_row = start_row + i - 1  # real sheet row for this member
            roster_bar.render(note=f"next: row {sheet_row} {truncate(name, 32)}")
            try:
                summary = run_one_member(name, args, bar_enabled, model_ok,
                                         idx=i, total=total)
            except KeyboardInterrupt:
                print(f"\n[!] Interrupted at row {sheet_row} ({name}, "
                      f"{i}/{total}). Re-run and enter {sheet_row} "
                      f"(or --start-row {sheet_row}) to resume.",
                      flush=True)
                roster_bar.done()
                break
            except Exception as exc:
                log.warning("member %s failed: %s", name, exc)
                print(f"[!] Row {sheet_row} ({name}) failed ({exc}); "
                      f"continuing.", flush=True)
                summary = {"member": name, "sources": 0, "pushed": 0,
                           "skipped": 0, "failed": 0, "verdict": "ERROR"}
            summary["sheet_row"] = sheet_row
            results.append(summary)
            roster_bar.advance(note=f"done: {truncate(name, 34)} | "
                               f"+{summary.get('pushed', 0)} sheet | "
                               f"{summary.get('sources', 0)} sources | "
                               f"{summary.get('verdict', '?')}")
            # Plain per-row completion line: survives piping/grep and tells
            # you exactly which row to resume from next time.
            print(f"[ROW DONE] row {sheet_row}/{start_row + total - 1}: "
                  f"{name} — {summary.get('sources', 0)} source(s), "
                  f"{summary.get('pushed', 0)} appended, "
                  f"{summary.get('skipped', 0)} already present, "
                  f"{summary.get('failed', 0)} failed, "
                  f"verdict={summary.get('verdict', '?')}. "
                  f"Next run: start at row {sheet_row + 1}.", flush=True)
        roster_bar.done()

        done = len(results)
        tot_sources = sum(r.get("sources", 0) for r in results)
        tot_pushed = sum(r.get("pushed", 0) for r in results)
        tot_skipped = sum(r.get("skipped", 0) for r in results)
        tot_failed = sum(r.get("failed", 0) for r in results)
        print()
        print("=" * 100)
        print(f" ROSTER COMPLETE — {done}/{total} member(s) processed")
        print(f" sources found: {tot_sources}   sheet appended: {tot_pushed}   "
              f"already present: {tot_skipped}   failed: {tot_failed}")
        print("-" * 100)
        last_row = start_row + len(results) - 1 if results else start_row - 1
        for r in results:
            print(f"  row {r.get('sheet_row', '?'):>4}  "
                  f"{truncate(r.get('member', '?'), 28).ljust(28)}  "
                  f"src={r.get('sources', 0):<3}  "
                  f"sheet+{r.get('pushed', 0):<3}  "
                  f"skip={r.get('skipped', 0):<3}  "
                  f"fail={r.get('failed', 0):<3}  "
                  f"{r.get('verdict', '?')}")
        print("=" * 100)
        if done < total:
            print(f"[!] Stopped early at row {last_row}. Next run: start at "
                  f"row {last_row + 1} (or --start-row {last_row + 1}).",
                  flush=True)
        else:
            print(f"[*] All rows through {last_row} done.", flush=True)
        if args.json:
            try:
                with open(args.json, "w") as f:
                    json.dump({"topic": TOPIC_NAME,
                               "model": (None if args.no_model else OLLAMA_MODEL),
                               "members": results}, f, indent=2)
                print(f"[*] Full roster results written to {args.json}")
            except OSError as exc:
                print(f"[!] could not write {args.json}: {exc}")
        return 0

    # ---- single member (only via --single or --member) ----------------------
    member = (args.member or MEMBER).strip()
    run_one_member(member, args, bar_enabled, model_ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
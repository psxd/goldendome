#!/usr/bin/env python3
"""
Congress Member Article Search & Local LLM Stance Evaluator
Features:
 - Accepts --start-row and --batch-size CLI arguments for GitHub Actions batching.
 - Runs Ollama (gemma3:1b) locally in the GitHub Actions runner environment.
 - Pre-checks existing URLs on the sheet per legislator to prevent duplicate columns.
 - Sends 5-column source blocks to the Google Apps Script Web App.
 - Implements 20s delays between queries and 1 min -> 5 min -> 10 min rate-limit backoffs.
"""

import os
import sys
import time
import re
import json
import argparse
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import requests
import ollama

# ==============================================================================
# CONFIGURATION
# ==============================================================================
WEB_APP_URL = os.getenv("WEB_APP_URL", "")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:1b")

# Default 20-second delay between member queries
DELAY_BETWEEN_MEMBERS = float(os.getenv("DELAY_BETWEEN_MEMBERS", "20.0"))

# Explicit rate-limit backoff schedule in seconds: 1 min, 5 mins, 10 mins
BACKOFF_SCHEDULE = [60, 300, 600]

TOPIC_PATTERNS = {
    "Golden Dome / Defense": [
        r"\bgolden dome\b",
        r"\bmissile defense\b",
        r"\bdefense shield\b"
    ],
    "Spectrum & Radars": [
        r"\b3\.1\s*-\s*3\.45\s*ghz\b",
        r"\blow 3 spectrum\b",
        r"\bspectrum auction\b",
        r"\bradar interference\b"
    ]
}

SEARCH_TOPICS = [
    "Golden Dome",
    "3.1-3.45 GHz",
    "spectrum auction",
    "missile defense",
    "defense shield",
    "low 3 spectrum",
    "radar interference"
]

LEGISLATORS_FEED = "https://unitedstates.github.io/congress-legislators/legislators-current.json"

VOTING_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"
}


# ==============================================================================
# OLLAMA LOCAL ANALYSIS (gemma3:1b)
# ==============================================================================
def analyze_article_with_ollama(member_name: str, title: str, description: str, regex_hits: dict) -> dict:
    matched_terms = []
    for cat, terms in regex_hits.items():
        matched_terms.extend(terms)

    regex_keywords_str = ", ".join(set(matched_terms)) if matched_terms else "None"

    prompt = f"""
You are an expert legislative analyst. Analyze the following news item mentioning Congress member "{member_name}".

Article Title: {title}
Article Snippet: {description}
Key Terms Found: {regex_keywords_str}

Tasks:
1. Determine the legislator's or article's stance on missile defense / spectrum topics: choose exactly one from ["Support", "Oppose", "Neutral"].
2. Provide a 1-2 sentence summary highlighting key trigger words, terms, and context that caused the match.

Respond ONLY with valid JSON in this exact structure:
{{
  "stance": "Support" | "Oppose" | "Neutral",
  "summary": "Keywords: [extracted terms]. <Short summary>"
}}
"""

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1}
        )
        content = response['message']['content'].strip()
        
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        
        parsed = json.loads(content.strip())
        stance = parsed.get("stance", "Neutral")
        if stance not in ["Support", "Oppose", "Neutral"]:
            stance = "Neutral"
        
        summary = parsed.get("summary", f"Keywords: {regex_keywords_str} - {title}")
        return {"stance": stance, "summary": summary[:250]}

    except Exception:
        return {
            "stance": "Neutral",
            "summary": f"Keywords: [{regex_keywords_str}] - {title[:150]}"
        }


# ==============================================================================
# DEDUPLICATION & SHEET INTERFACE
# ==============================================================================
def normalize_url(url: str) -> str:
    if not url:
        return ""
    return url.strip().lower().rstrip("/")


def get_existing_urls_for_member(member_name: str) -> set:
    try:
        params = urllib.parse.urlencode({
            "action": "getExistingUrls",
            "memberName": member_name
        })
        resp = requests.get(f"{WEB_APP_URL}?{params}", timeout=15)
        if resp.status_code == 200:
            urls = resp.json().get("urls", [])
            return {normalize_url(u) for u in urls if u}
    except Exception as e:
        print(f"  [!] Failed to retrieve existing URLs for {member_name}: {e}")
    return set()


def send_source_to_web_app(member_name: str, source_data: dict) -> bool:
    payload = {
        "action": "addSource",
        "memberName": member_name,
        "sourceData": source_data
    }
    try:
        response = requests.post(WEB_APP_URL, json=payload, timeout=15)
        if response.status_code == 200:
            res_json = response.json()
            return res_json.get("status") == "success" or res_json.get("added") is True
    except Exception as e:
        print(f"  [!] POST request failed for {member_name}: {e}")
    return False


# ==============================================================================
# RSS & SEARCH LOGIC
# ==============================================================================
def build_news_query_url(target_member: str, keyphrases: list) -> str:
    phrase_str = " OR ".join([f'"{kp}"' for kp in keyphrases])
    raw_query = f'"{target_member}" ({phrase_str})'
    encoded_query = urllib.parse.quote(raw_query)
    return f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"


def match_topics(text: str) -> dict:
    matched = {}
    for category, patterns in TOPIC_PATTERNS.items():
        for pattern in patterns:
            hits = re.findall(pattern, text, flags=re.IGNORECASE)
            if hits:
                if category not in matched:
                    matched[category] = set()
                matched[category].update([h.strip() for h in hits])
    return {k: sorted(list(v)) for k, v in matched.items()}


def fetch_articles_for_member(member_name: str) -> list:
    feed_url = build_news_query_url(member_name, SEARCH_TOPICS)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PublicNewsBot/1.0"}

    attempt = 0

    while True:
        try:
            req = urllib.request.Request(feed_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                xml_data = resp.read()

            root = ET.fromstring(xml_data)
            items = root.findall(".//item")

            articles = []
            for item in items:
                title = item.findtext("title", "Untitled")
                link = item.findtext("link", "")
                pub_date = item.findtext("pubDate", "N/A")
                source = item.findtext("source", "Media Outlet")
                description = item.findtext("description", "")

                combined_text = f"{title} {description}"
                hits = match_topics(combined_text)

                ollama_res = analyze_article_with_ollama(member_name, title, description, hits)

                articles.append({
                    "publication_date": pub_date,
                    "source_name": source,
                    "stance": ollama_res["stance"],
                    "summary": ollama_res["summary"],
                    "url": link
                })

            return articles

        except urllib.error.HTTPError as e:
            if e.code in [429, 500, 502, 503, 504]:
                if attempt < len(BACKOFF_SCHEDULE):
                    wait_time = BACKOFF_SCHEDULE[attempt]
                    wait_minutes = wait_time // 60
                    print(f"  [!] HTTP {e.code} (Rate Limit / Server Busy) for {member_name}.")
                    print(f"  [!] Waiting {wait_minutes} minute(s) ({wait_time}s) before retry (Attempt {attempt + 1}/{len(BACKOFF_SCHEDULE)})...")
                    time.sleep(wait_time)
                    attempt += 1
                else:
                    print(f"  [!] HTTP {e.code} persistent for {member_name} after 1, 5, and 10 min waits. Skipping.")
                    break
            else:
                print(f"  [!] HTTP {e.code} Error for {member_name}: {e.reason}")
                break
        except Exception as e:
            print(f"  [!] Exception fetching articles for {member_name}: {e}")
            break

    return []


def get_legislators_list() -> list:
    req = urllib.request.Request(LEGISLATORS_FEED, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    members = []
    for item in data:
        terms = item.get("terms", [])
        if not terms:
            continue
        last_term = terms[-1]
        state = last_term.get("state", "")
        if state not in VOTING_STATES:
            continue
        full_name = f"{item['name'].get('first', '')} {item['name'].get('last', '')}".strip()
        members.append(full_name)
    return members


# ==============================================================================
# MAIN BATCH EXECUTION
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Congress Member Article Scraper & Ollama Evaluator")
    parser.add_argument("--start-row", type=int, default=2, help="Row index to start from (2 = Row 2 / 1st member)")
    parser.add_argument("--batch-size", type=int, default=20, help="Number of rows to process in this run")
    args = parser.parse_args()

    if not WEB_APP_URL:
        print("[!] ERROR: WEB_APP_URL environment variable is not set.")
        sys.exit(1)

    legislators = get_legislators_list()
    total_members = len(legislators)

    start_idx = max(0, args.start_row - 2)
    end_idx = min(total_members, start_idx + args.batch_size)

    print(f"[+] Total voting members in roster: {total_members}")
    print(f"[+] Processing batch: Rows {start_idx + 2} to {end_idx + 1} ({end_idx - start_idx} members)")
    print(f"[+] Using Ollama Model: {OLLAMA_MODEL}")
    print(f"[+] Default pause between members: {DELAY_BETWEEN_MEMBERS}s")
    print(f"[+] Rate-limit backoff schedule: 1 min -> 5 min -> 10 min\n")

    for idx in range(start_idx, end_idx):
        row_num = idx + 2
        member_name = legislators[idx]
        print(f"[{row_num}/{total_members + 1}] Processing Row {row_num}: {member_name}")

        existing_urls = get_existing_urls_for_member(member_name)
        articles = fetch_articles_for_member(member_name)

        if articles:
            added_count = 0
            for article in articles:
                norm_url = normalize_url(article["url"])
                if norm_url in existing_urls:
                    print(f"  └─ Skipped duplicate URL: {article['url']}")
                    continue

                success = send_source_to_web_app(member_name, article)
                if success:
                    existing_urls.add(norm_url)
                    added_count += 1
                    print(f"  └─ Added source [{article['stance']}]: {article['url']}")

            if added_count == 0:
                print("  └─ All found articles were already present on the sheet.")
        else:
            print("  └─ No matching articles found.")

        # Default 20-second pause between member requests to prevent rate limits
        time.sleep(DELAY_BETWEEN_MEMBERS)


if __name__ == "__main__":
    main()
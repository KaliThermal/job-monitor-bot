#!/usr/bin/env python3
"""Student-focused job monitor for GitHub Actions + Serper + Telegram.

Drop-in replacement for job_monitor.py in a fork of dayaki/job-monitor-bot.
Uses existing google_search_sites.yaml, GitHub Secrets, and seen_jobs.json cache.
No secrets are stored in source code.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

CONFIG_FILE = Path(__file__).with_name("google_search_sites.yaml")
SEEN_FILE = Path(__file__).with_name("seen_jobs.json")
SERPER_API = "https://google.serper.dev/search"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# A title must contain a relevant technical role or clearly technical speciality.
ROLE_RE = re.compile(
    r"\b(?:soc|noc|siem|cybersecurity|cyber security|cybersäkerhet|it[- ]säkerhet|"
    r"security (?:analyst|engineer|operations|technician)|"
    r"säkerhetsanalytiker|säkerhetstekniker|network|networking|nätverk|"
    r"infrastructure|infrastruktur|cloud (?:engineer|operations|technician)|"
    r"devops|platform engineer|sre|site reliability|"
    r"systemadministratör|system administrator|sysadmin|linux administrator|"
    r"it[- ]drift|drifttekniker|it[- ]operations|"
    r"it[- ]support|it[- ]tekniker|it technician|it support|helpdesk|service desk|"
    r"supporttekniker|systemtekniker)\b",
    re.IGNORECASE,
)

# Require real evidence that the JOB, not just the search query, is for students/part-time.
SCHEDULE_RE = re.compile(
    r"\b(?:deltid|deltidsjobb|extrajobb|extraarbete|timanställning|"
    r"sommarjobb|sommarpraktik|sommarinternship|praktik(?:plats)?|"
    r"internship|intern|trainee|sommarjobb|"
    r"part[ -]?time|working student|student worker|"
    r"studentmedarbetare|studentjobb|student assistant|"
    r"studentanställning|20[ -]?(?:h|hours)[ /]?(?:vecka|week))\b",
    re.IGNORECASE,
)

FULL_TIME_RE = re.compile(r"\b(?:heltid|full[ -]?time|100\s*%|tillsvidareanställning på heltid)\b", re.IGNORECASE)
SENIOR_RE = re.compile(r"\b(?:senior|sr\.?|lead|principal|staff engineer|head of|director|manager|architect|chef)\b", re.IGNORECASE)

# Search results about searches/profiles are NOT individual job ads.
BAD_TITLE_RE = re.compile(
    r"(?:\b\d[\d,. ]*\+?\s+(?:lediga\s+)?jobb\b|"
    r"\bjobb som matchar\b|\bjobs in\b|\bjobs for\b|"
    r"\bjob openings\b|\bjob listings\b|"
    r"\byour dream startup job\b|\btech & startup jobs\b|"
    r"\bpopular searches\b)", re.IGNORECASE
)

STOCKHOLM_RE = re.compile(
    r"\b(?:stockholm|solna|kista|sundbyberg|bromma|sollentuna|"
    r"nacka|sickla|huddinge|täby|danderyd|j(?:ä|a)rfälla|"
    r"jakobsberg|lidingö|södertälje|upplands väsby|"
    r"stockholms län|stockholm county|stockholm area|stockholm region)\b",
    re.IGNORECASE,
)
REMOTE_SWEDEN_RE = re.compile(r"\b(?:remote (?:from|in) sweden|distans (?:i|från) sverige|remote[- ]sweden)\b", re.IGNORECASE)
OTHER_CITIES_RE = re.compile(
    r"\b(?:göteborg|gothenburg|malmö|malmo|uppsala|umeå|örebro|"
    r"linköping|köping|helsingborg|lund|borås|västerås|"
    r"copenhagen|köpenhamn|amsterdam|berlin|davis|folsom)\b", re.IGNORECASE
)


def valid_individual_job_url(raw_url):
    """Strict allowlist: accepting job DETAIL URLs only, not lists, profiles or spam."""
    try:
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/").lower()
        query = parsed.query.lower()
        if parsed.scheme not in ("https", "http") or not host:
            return False
        if any(x in path for x in ("/in/", "/company/", "/jobs/search", "/jobs/location", "/lediga-jobb/", "/q-")):
            # /lediga-jobb/ can be real detail on Academic Work: handle below.
            if "academicwork.se" not in host or "/lediga-jobb/j/" not in path:
                return False
        if host.endswith("linkedin.com"):
            return bool(re.match(r"^/jobs/view/[^/]+$", path))
        if host.endswith("indeed.com"):
            return path in ("/viewjob", "/rc/clk", "/pagead/clk") and "jk=" in query
        if host.endswith("academicwork.se"):
            return "/lediga-jobb/j/" in path
        if host.endswith("arbetsformedlingen.se"):
            return bool(re.search(r"/(?:annonser|annons)/[a-z0-9-]+$", path))
        if host.endswith("jobbsafari.se"):
            return bool(re.match(r"^/jobb/[^/]+$", path))
        if host.endswith("teamtailor.com"):
            return bool(re.search(r"/jobs/[0-9]+(?:-[a-z0-9-]+)?$", path))
        if host.endswith("thehub.io"):
            return bool(re.match(r"^/jobs/[a-z0-9-]+$", path)) and path.count("/") == 2
        if host.endswith("greenhouse.io"):
            return bool(re.search(r"/jobs/[0-9]+(?:-[a-z0-9-]+)?$", path))
        if host.endswith("lever.co"):
            return bool(re.match(r"^/[^/]+/[a-z0-9-]{16,}$", path))
        if host.endswith("ashbyhq.com"):
            return bool(re.match(r"^/[^/]+/[a-z0-9-]{8,}$", path))
        if host.endswith("workable.com"):
            return bool(re.search(r"/(?:j|view)/[a-z0-9-]{6,}$", path))
        if host.endswith("smartrecruiters.com"):
            return bool(re.match(r"^/[^/]+/[a-z0-9-]{5,}$", path))
        if host.endswith("recruitee.com"):
            return bool(re.match(r"^/o/[^/]+$", path))
        if host.endswith("applytojob.com"):
            return bool(re.search(r"/apply/[^/]+", path))
        if host.endswith("wellfound.com"):
            return bool(re.search(r"/(?:jobs|job-listings)/[a-z0-9-]+$", path))
        return False
    except Exception:
        return False


def job_rejection_reason(item):
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or "").strip()
    url = str(item.get("link") or "").strip()
    if not valid_individual_job_url(url):
        return "not_an_individual_job_ad"
    if BAD_TITLE_RE.search(title):
        return "search_or_listing_page"
    if not ROLE_RE.search(title):
        return "not_a_relevant_technical_role"
    if SENIOR_RE.search(title):
        return "senior_role"
    # This intentionally excludes unconfirmed schedules: fewer but more trustworthy alerts.
    if not SCHEDULE_RE.search(title + " " + snippet):
        return "part_time_or_internship_not_confirmed"
    if FULL_TIME_RE.search(title + " " + snippet):
        return "full_time_mentioned"
    if not (STOCKHOLM_RE.search(title + " " + snippet) or REMOTE_SWEDEN_RE.search(title + " " + snippet)):
        return "outside_stockholm_or_location_unknown"
    if OTHER_CITIES_RE.search(title):
        return "outside_stockholm"
    return None


def read_seen():
    if not SEEN_FILE.is_file():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(x) for x in data if isinstance(x, (str, int)))
        if isinstance(data, dict):
            # Old app may use a dict of job IDs -> metadata.
            for key in ("urls", "seen", "seen_jobs", "job_ids"):
                if isinstance(data.get(key), list):
                    return set(str(x) for x in data[key] if isinstance(x, (str, int)))
            return set(data.keys())
    except Exception as exc:
        print("Could not read previous seen-jobs cache:", exc)
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps({"urls": sorted(seen)}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def make_search_query(keyword, domains, negative_terms):
    sites = " OR ".join("site:" + d for d in domains)
    negative = " ".join("-" + str(t).strip().replace(" ", "-") for t in negative_terms if t)
    return f"{keyword} ({sites}) {negative}".strip()


def search_serper(api_key, keyword, domains, settings, country):
    payload = {
        "q": make_search_query(keyword, domains, settings.get("query_negative_terms", [])),
        "num": min(10, max(1, int(settings.get("max_results_per_query", 10)))),
        "gl": country,
        "hl": "sv",
    }
    if settings.get("serper_time_filter"):
        payload["tbs"] = settings["serper_time_filter"]
    response = requests.post(SERPER_API, json=payload, headers={"X-API-KEY": api_key, "Content-Type": "application/json"}, timeout=30)
    response.raise_for_status()
    return response.json().get("organic", [])


def telegram_send(bot_token, chat_id, text):
    response = requests.post(
        TELEGRAM_API.format(token=bot_token),
        json={"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True},
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected notification")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Search, but do not notify or persist")
    args, _ = parser.parse_known_args()
    if not CONFIG_FILE.exists():
        sys.exit("Missing google_search_sites.yaml")
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    settings = config.get("settings") or {}
    if not settings.get("enabled", True):
        print("Search disabled in settings; exiting.")
        return
    keywords = [str(k).strip() for k in config.get("keywords", []) if str(k).strip()]
    domains = [str(site.get("domain", "")).strip() for site in config.get("sites", []) if site.get("domain")]
    countries = settings.get("serper_countries") or ["se"]
    if not keywords or not domains:
        sys.exit("No keywords or job domains in YAML.")
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not api_key:
        sys.exit("Missing GitHub secret SERPER_API_KEY")
    if not args.dry_run and not (bot_token and chat_id):
        sys.exit("Missing GitHub secrets TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    seen = read_seen()
    matches = []
    found_urls = set()
    rejection_counts = Counter()
    searches_succeeded = 0
    for keyword in keywords:
        for country in countries:
            try:
                results = search_serper(api_key, keyword, domains, settings, country)
                searches_succeeded += 1
                print(f"Serper: {len(results)} results for {keyword[:80]} ({country})")
            except Exception as exc:
                print(f"Serper error for {keyword[:60]}: {exc}")
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                reason = job_rejection_reason(item)
                if reason:
                    rejection_counts[reason] += 1
                    continue
                url = item["link"]
                if url in seen or url in found_urls:
                    rejection_counts["duplicate"] += 1
                    continue
                found_urls.add(url)
                matches.append(item)
            time.sleep(max(0, float(settings.get("min_seconds_between_queries", 2.0))))

    print("Filtered out:", dict(rejection_counts))
    print("New student/part-time job candidates:", len(matches))
    if not searches_succeeded:
        sys.exit("All Serper searches failed. No state changed.")
    if args.dry_run:
        for item in matches[:15]:
            print("DRY RUN:", item.get("title"), item.get("link"))
        return

    max_alerts = 8  # Avoid a flood of notifications if the first run finds many.
    sent = 0
    for item in matches[:max_alerts]:
        title = str(item.get("title", ""))[:150]
        snippet = str(item.get("snippet", ""))[:400]
        url = str(item.get("link", ""))
        text = f"🔔 Möjligt student-/deltidsjobb\n\n{title}\n\n{snippet}\n\nAnsök/Granska:\n{url}\n\n⚠️ Verifiera anställningsgrad och ort i annonsen."
        try:
            telegram_send(bot_token, chat_id, text)
            seen.add(url)
            sent += 1
        except Exception as exc:
            print("Telegram send failed:", exc)
            break
    if sent:
        save_seen(seen)
    print(f"Sent {sent} candidate job alerts at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()

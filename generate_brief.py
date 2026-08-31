#!/usr/bin/env python3
"""
Daily Brief Generator
Searches for today's news, synthesises it via Claude, generates audio via Deepgram,
and produces an HTML brief + MP3 file.
"""

import os
import re
import json
import time
import datetime
import zoneinfo
import requests
from pathlib import Path
import anthropic
from anthropic import Anthropic
from jinja2 import Template
from icalendar import Calendar as ICal

from brief_quality import (
    compute_word_targets,
    enforce_word_budget,
    entries_since,
    filter_placeholders,
    filter_repeats,
    lint_brief,
    load_coverage_log,
    prompt_banned_examples,
    record_coverage,
    save_coverage_log,
    section_word_counts,
)

# ── Configuration ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_VOICE = "aura-helios-en"  # British male
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("RECIPIENT_EMAILS", "").split(",") if e.strip()]
RECIPIENT_NAME = os.environ.get("RECIPIENT_NAME", "you")
SENDER_FROM = os.environ.get("SENDER_FROM", "The Daily Brief <brief@example.com>")
CALENDAR_ICS_URL = os.environ.get("CALENDAR_ICS_URL", "")

# DRY_RUN=true: full pipeline through search, synthesis, validation, word
# budget, and prose lint — but no email, no audio, no state writes. Output
# files get a dryrun_ prefix and are gitignored. Only needs ANTHROPIC_API_KEY.
DRY_RUN = os.environ.get("DRY_RUN", "").lower() == "true"
FILE_PREFIX = "dryrun_" if DRY_RUN else ""

# Models by pipeline stage. Gathering needs Sonnet: Haiku gives up and returns
# apology-prose instead of best-effort JSON when it can't fully verify outlet/
# URL/date constraints (verified in dry runs 2026-08-03). Narration is purely
# mechanical TTS conversion and runs on Haiku at a third of the token price.
SEARCH_MODEL = "claude-sonnet-4-5-20250929"
NARRATION_MODEL = "claude-haiku-4-5"
SYNTHESIS_MODEL = "claude-sonnet-4-5-20250929"
SEARCH_MAX_USES = 4  # web searches per category; the brief keeps 3-5 stories per section

DEFAULT_TIMEZONE = "America/Los_Angeles"  # Pacific — fallback when no travel detected
TARGET_LOCAL_HOUR = 5                     # Target 5:00am — GitHub Actions delays ~40min so delivery lands ~5:40–6:00am

# City/country keywords → IANA timezone. Extend as needed.
LOCATION_TIMEZONE_MAP = {
    # Ireland / UK
    "dublin": "Europe/Dublin",
    "ireland": "Europe/Dublin",
    "cork": "Europe/Dublin",
    "london": "Europe/London",
    "edinburgh": "Europe/London",
    "uk": "Europe/London",
    "united kingdom": "Europe/London",
    "england": "Europe/London",
    "scotland": "Europe/London",
    # Western Europe
    "paris": "Europe/Paris",
    "france": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "munich": "Europe/Berlin",
    "germany": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "netherlands": "Europe/Amsterdam",
    "zurich": "Europe/Zurich",
    "switzerland": "Europe/Zurich",
    "rome": "Europe/Rome",
    "milan": "Europe/Rome",
    "italy": "Europe/Rome",
    "barcelona": "Europe/Madrid",
    "madrid": "Europe/Madrid",
    "spain": "Europe/Madrid",
    # US / Canada
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "boston": "America/New_York",
    "washington": "America/New_York",
    "miami": "America/New_York",
    "atlanta": "America/New_York",
    "chicago": "America/Chicago",
    "houston": "America/Chicago",
    "dallas": "America/Chicago",
    "austin": "America/Chicago",
    "denver": "America/Denver",
    "phoenix": "America/Phoenix",
    "seattle": "America/Los_Angeles",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    # India
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    # Asia / Pacific
    "tokyo": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "australia": "Australia/Sydney",
    "auckland": "Pacific/Auckland",
    "new zealand": "Pacific/Auckland",
    # Middle East
    "dubai": "Asia/Dubai",
    "uae": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
}

# All dates are computed in the delivery timezone, not the runner's clock.
# GitHub Actions runners are UTC, so datetime.date.today() rolls to "tomorrow"
# during the Pacific evening — which anchored searches to a date with almost
# no published news and stamped the wrong date on the masthead.
TODAY = datetime.datetime.now(zoneinfo.ZoneInfo(DEFAULT_TIMEZONE)).date()
YESTERDAY = TODAY - datetime.timedelta(days=1)
DATE_STR = TODAY.strftime("%B %d, %Y")         # February 28, 2026
YESTERDAY_STR = YESTERDAY.strftime("%B %d, %Y")
DATE_FILE = TODAY.strftime("%Y-%m-%d")          # 2026-02-28
DAY_NAME = TODAY.strftime("%A")                 # Friday

OUTPUT_DIR = Path("briefs")
OUTPUT_DIR.mkdir(exist_ok=True)

# A fresh fork has no data/ — nothing in it is tracked, since every file it
# holds is generated at runtime. Create it here, alongside briefs/, so the
# coverage log and regression report have somewhere to land on the first run.
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

TEMPLATE_PATH = Path("templates/brief_template.html")

# ── Load config ────────────────────────────────────────────────────────────
_config = json.loads(Path("config.json").read_text())
SOURCES = _config["sources"]
CATEGORIES = _config["categories"]
TRUSTED_SOURCES = SOURCES["trusted_sources"]

# Word budget: reading minutes × words-per-minute is a hard ceiling for the
# whole brief, allocated across sections by weight.
_budget_cfg = _config.get("word_budget", {})
READING_MINUTES = _budget_cfg.get("reading_minutes", 10)
WORDS_PER_MINUTE = _budget_cfg.get("words_per_minute", 240)
EXPLORE_WEIGHT = _budget_cfg.get("explore_weight", 2)
WORD_CEILING = READING_MINUTES * WORDS_PER_MINUTE
SECTION_WEIGHTS = {c["id"]: c.get("weight", 2) for c in CATEGORIES}
WORD_TARGETS = compute_word_targets(SECTION_WEIGHTS, EXPLORE_WEIGHT, WORD_CEILING)

COVERAGE_LOG_PATH = Path("data/coverage_log.json")

WEATHER_LOCATIONS = [
    {"label": "Home", "icon": "🏠", "query": os.environ.get("WEATHER_LOCATION_HOME", "")},
    {"label": "Work", "icon": "🏢", "query": os.environ.get("WEATHER_LOCATION_WORK", "")},
]

client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Weather ───────────────────────────────────────────────────────────────
def _wmo_icon(code: int) -> str:
    if code == 0: return "☀️"
    if code in (1, 2): return "⛅"
    if code == 3: return "☁️"
    if code in (45, 48): return "🌫️"
    if code in (95, 96, 99): return "⛈️"
    if code in (71, 73, 75, 77, 85, 86): return "❄️"
    return "🌧️"


def _location_to_timezone(location: str) -> str:
    """Map a location string to an IANA timezone using keyword matching."""
    loc = location.lower()
    for keyword, tz in LOCATION_TIMEZONE_MAP.items():
        if keyword in loc:
            return tz
    return DEFAULT_TIMEZONE


def detect_travel_timezone() -> str:
    """
    Fetch personal Google Calendar ICS and look for multi-day events happening today.
    Returns the IANA timezone of the travel destination, or DEFAULT_TIMEZONE if none found.
    """
    if not CALENDAR_ICS_URL:
        return DEFAULT_TIMEZONE
    try:
        r = requests.get(CALENDAR_ICS_URL, timeout=15)
        r.raise_for_status()
        cal = ICal.from_ical(r.content)
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            dtstart = component.get("DTSTART")
            dtend = component.get("DTEND")
            if not dtstart or not dtend:
                continue
            start_val = dtstart.dt
            end_val = dtend.dt
            # Normalize to date
            start_date = start_val.date() if isinstance(start_val, datetime.datetime) else start_val
            end_date = end_val.date() if isinstance(end_val, datetime.datetime) else end_val
            # Must include today and span at least 2 days
            if not (start_date <= TODAY < end_date):
                continue
            if (end_date - start_date).days < 2:
                continue
            # 1. Try TZID from a datetime DTSTART
            if isinstance(start_val, datetime.datetime) and start_val.tzinfo:
                tz_key = getattr(start_val.tzinfo, "key", str(start_val.tzinfo))
                if tz_key and tz_key not in ("UTC", DEFAULT_TIMEZONE, "US/Pacific", "America/Pacific"):
                    try:
                        zoneinfo.ZoneInfo(tz_key)  # validate
                        print(f"  🌍 Travel detected via event TZID: {tz_key}")
                        return tz_key
                    except zoneinfo.ZoneInfoNotFoundError:
                        pass
            # 2. Try LOCATION keyword match
            location = str(component.get("LOCATION", ""))
            if location:
                tz = _location_to_timezone(location)
                if tz != DEFAULT_TIMEZONE:
                    print(f"  🌍 Travel detected via location '{location}': {tz}")
                    return tz
        return DEFAULT_TIMEZONE
    except Exception as e:
        print(f"  ⚠️  Calendar check failed: {e} — defaulting to Pacific")
        return DEFAULT_TIMEZONE


def target_utc_hour_for(tz_name: str) -> int:
    """Return the UTC hour that corresponds to TARGET_LOCAL_HOUR in tz_name on today's date."""
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
        local_dt = datetime.datetime.combine(TODAY, datetime.time(TARGET_LOCAL_HOUR, 0), tzinfo=tz)
        return local_dt.astimezone(datetime.timezone.utc).hour
    except Exception:
        return 13  # Pacific PDT fallback


def _clothing_tip(high_f: int, weather_code: int, precip_pct: int) -> str:
    """Return a one-line clothing recommendation based on day's high and conditions."""
    rain = precip_pct >= 40 or weather_code in (51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99)
    snow = weather_code in (71, 73, 75, 77, 85, 86)
    if high_f >= 85:
        tip = "Hot day — shorts and a t-shirt."
    elif high_f >= 75:
        tip = "Warm — light clothing."
    elif high_f >= 65:
        tip = "Comfortable — a light layer in the morning."
    elif high_f >= 55:
        tip = "Cool — jacket needed."
    elif high_f >= 45:
        tip = "Cold — warm coat and layers."
    else:
        tip = "Very cold — heavy coat, hat, and gloves."
    if snow:
        tip += " Snow expected — boots and waterproofs."
    elif rain:
        tip += " Rain likely — bring an umbrella."
    return tip


def _wmo_description(code: int) -> str:
    descriptions = {
        0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Icy fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
        80: "Showers", 81: "Rain showers", 82: "Heavy showers",
        85: "Snow showers", 86: "Heavy snow showers",
        95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
    }
    return descriptions.get(code, "Unknown")


def _geocode(location_name: str) -> tuple[float, float, str]:
    """Geocode a location name to lat/lon using Open-Meteo's geocoding API."""
    r = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location_name, "count": 1, "language": "en", "format": "json"},
        timeout=10,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise RuntimeError(f"No geocoding results for '{location_name}'")
    result = results[0]
    return result["latitude"], result["longitude"], result.get("timezone", "auto")


def fetch_weather() -> list[dict]:
    """Fetch today's weather via Open-Meteo (free, no API key required)."""
    print("🌤️  Fetching weather...")
    results = []
    for loc in WEATHER_LOCATIONS:
        if not loc["query"]:
            results.append({"label": loc["label"], "location_icon": loc["icon"], "error": True})
            continue
        try:
            lat, lon, timezone = _geocode(loc["query"])
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,apparent_temperature,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "temperature_unit": "fahrenheit",
                    "timezone": timezone,
                    "forecast_days": 1,
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            current = data["current"]
            daily = data["daily"]
            code = current["weather_code"]
            high_f = round(daily["temperature_2m_max"][0])
            precip_pct = round(daily["precipitation_probability_max"][0] or 0)
            results.append({
                "label": loc["label"],
                "location_icon": loc["icon"],
                "weather_icon": _wmo_icon(code),
                "condition": _wmo_description(code),
                "temp_f": str(round(current["temperature_2m"])),
                "feels_like_f": str(round(current["apparent_temperature"])),
                "high_f": str(high_f),
                "low_f": str(round(daily["temperature_2m_min"][0])),
                "precip_pct": str(precip_pct),
                "clothing_tip": _clothing_tip(high_f, code, precip_pct),
            })
            print(f"  ✅ {loc['label']}: {round(current['temperature_2m'])}°F, {_wmo_description(code)}")
        except Exception as e:
            print(f"  ❌ Weather fetch failed for {loc['label']}: {e}")
            results.append({"label": loc["label"], "location_icon": loc["icon"], "error": True})
    return results


# ── NBA Scores ────────────────────────────────────────────────────────────
def fetch_nba_scores() -> list[dict]:
    """Fetch yesterday's completed NBA scores from ESPN's public API."""
    print("🏀 Fetching NBA scores...")
    yesterday = TODAY - datetime.timedelta(days=1)
    date_param = yesterday.strftime("%Y%m%d")
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            params={"dates": date_param},
            timeout=10,
        )
        r.raise_for_status()
        games = []
        for event in r.json().get("events", []):
            competition = event.get("competitions", [{}])[0]
            if not competition.get("status", {}).get("type", {}).get("completed"):
                continue
            competitors = competition.get("competitors", [])
            home = next((c for c in competitors if c["homeAway"] == "home"), None)
            away = next((c for c in competitors if c["homeAway"] == "away"), None)
            if not home or not away:
                continue
            away_score = int(away.get("score", 0))
            home_score = int(home.get("score", 0))
            games.append({
                "away_abbr": away["team"]["abbreviation"],
                "away_score": away_score,
                "home_abbr": home["team"]["abbreviation"],
                "home_score": home_score,
                "away_win": away_score > home_score,
            })
        if games:
            print(f"  ✅ {len(games)} NBA game(s) on {yesterday.strftime('%b %d')}")
        else:
            print(f"  ℹ️  No NBA games on {yesterday.strftime('%b %d')}")
        return games
    except Exception as e:
        print(f"  ❌ NBA scores fetch failed: {e}")
        return []


# ── Active Sports ──────────────────────────────────────────────────────────
def get_active_sports() -> str:
    """Return a string listing sports currently in season, based on the current month."""
    month = TODAY.month
    active = []
    if month in (10, 11, 12, 1, 2, 3, 4, 5, 6):
        active.append("NBA basketball")
    if month in (9, 10, 11, 12, 1, 2):
        active.append("NFL American football")
    if month in (8, 9, 10, 11, 12, 1, 2, 3, 4, 5):
        active.append("Premier League and European soccer")
    if month in (4, 5, 6, 7, 8, 9, 10):
        active.append("MLB baseball")
    if month in (10, 11, 12, 1, 2, 3, 4, 5, 6):
        active.append("NHL ice hockey")
    # Cricket: year-round; IPL runs April–May, WPL runs February–early March
    cricket = "international cricket (pay special attention to Team India Men and Women matches)"
    if month == 2:
        cricket += ", WPL (Women's Premier League)"
    if month in (4, 5):
        cricket += ", IPL (Indian Premier League)"
    active.append(cricket)
    return ", ".join(active)


# ── Deep Dive State ───────────────────────────────────────────────────────
# The queue is personal data and lives OUTSIDE this public repo, in a private
# store read by path via $PERSONAL_DATA_DIR. Tracking it in the repo would
# publish the recipient's reading and listening history to anyone who cloned it
# (this template's parent project learned that the hard way).
#
# Read-only by design: days_shown is recomputed from first_shown on every run (see
# the deep-dive enrichment below), so there is no state to write back. The old
# save_deep_dive_state() persisted only that derived value.
DEEP_DIVE_STATE_RELPATH = "private/daily-brief/deep_dive_state.json"


def load_deep_dive_state() -> dict:
    """Load the curated deep dive queue from an external private data store.

    Fails closed: a missing mount yields an empty Deep Dive section and a loud
    warning, never a guess. The rest of the brief is unaffected.
    """
    mount = os.environ.get("PERSONAL_DATA_DIR", "").strip()
    if not mount:
        print(
            "  ⚠️  PERSONAL_DATA_DIR is not set — Deep Dive section will be empty.\n"
            "      The queue is personal data and is no longer tracked in this repo."
        )
        return {}

    state_path = Path(mount) / DEEP_DIVE_STATE_RELPATH
    if not state_path.exists():
        print(f"  ⚠️  No deep dive state at {state_path} — Deep Dive section will be empty")
        return {}

    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠️  Could not read deep dive state ({e}) — Deep Dive section will be empty")
        return {}


def enrich_deep_dive_item(item: dict, item_type: str) -> dict:
    """On first appearance, generate a 1-2 sentence description and estimated time via web search."""
    if not item or item.get("description"):
        return item
    title = item.get("title", "Unknown")
    source = item.get("source", "Unknown")
    url = item.get("url", "")
    type_label = "longform article or essay" if item_type == "read" else "podcast episode"
    print(f"  ✨ Enriching {item_type}: {title}...")
    prompt = f"""Search for information about this {type_label} and return a brief description.

Title: {title}
Source: {source}
{"URL: " + url if url else ""}

Return JSON only:
{{
  "description": "1-2 sentence description of what this is about and why it is worth reading or listening to",
  "estimated_time": "e.g. '25 min read' or '2.5 hr listen'"
}}"""
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=SEARCH_MODEL,
                max_tokens=300,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except anthropic.RateLimitError as e:
            if attempt < 2:
                wait = int(e.response.headers.get("retry-after", 60))
                time.sleep(wait)
            else:
                raise
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text
    try:
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            enriched = json.loads(json_match.group())
            item["description"] = enriched.get("description", "")
            item["estimated_time"] = enriched.get("estimated_time", "")
    except Exception as e:
        print(f"  ⚠️  Enrichment parsing failed: {e}")
    return item


# ── Step 1: Search for news ───────────────────────────────────────────────
def search_news() -> dict:
    """Use Claude with web search to gather today's news across all categories."""
    print("📡 Searching for today's news...")

    def source_list(category: str) -> str:
        return ", ".join(TRUSTED_SOURCES.get(category, []))

    explore_sources = ", ".join(SOURCES["explore_pool"])
    active_sports = get_active_sports()

    # Rolling freshness windows per category (hours). The window is the
    # freshness criterion, but the queries stay date-anchored ("news today
    # {date}") because that phrasing pulls actual articles — outlet-name-heavy
    # queries return pages ABOUT the outlets instead. Dates are computed in
    # the delivery timezone, so the anchor is correct at any hour of the day.
    freshness = {c["id"]: c.get("freshness_hours", 24) for c in CATEGORIES}

    # Ground the model's clock explicitly: without this, a rolling window has
    # no anchor, and search results dated "in the future" relative to the
    # model's training data get dismissed as fictional.
    now_local = datetime.datetime.now(zoneinfo.ZoneInfo(DEFAULT_TIMEZONE))
    now_str = now_local.strftime("%A, %B %d, %Y at %I:%M %p (%Z)")

    def window_rule(hours: int) -> str:
        return (
            f"The current date and time is {now_str} — search results dated around now are "
            f"genuine current news, not speculative or fictional content. Focus on stories "
            f"from roughly the past {hours} hours. Publication timestamps in search results "
            f"are often missing or approximate — when a story clearly describes a current or "
            f"ongoing development, include it and estimate its date; when uncertain, include "
            f"rather than exclude. Skip stories that are clearly more than a few days old. "
            f"If you find fewer than 3 stories inside the window, fill the list with the most "
            f"important stories from the past 3 days rather than returning fewer than 5 stories."
        )

    search_queries = {
        "world": f"top world politics news today {DATE_STR}",
        "india": f"top India news politics business today {DATE_STR}",
        "tech": f"top technology AI news today {DATE_STR}",
        "business": f"top business finance news today {DATE_STR}",
        "science": f"top science health news this week {DATE_STR}",
        "sports": f"top sports news today {DATE_STR} — in season: {active_sports}",
        "explore": (
            f"interesting long-form feature stories published in the past 2 weeks "
            f"from {explore_sources}"
        ),
    }

    source_instructions = {
        "world": (
            f"Search for the most important world politics and international news of the past {freshness['world']} hours, "
            f"reported by these trusted outlets: {source_list('world')}. "
            f"Only return stories where the original reporting outlet is one of these sources. "
            f"{window_rule(freshness['world'])} "
            f"Do not use news aggregators, regional blogs, or unfamiliar outlets."
        ),
        "india": (
            f"Search for the most important news from India of the past {freshness['india']} hours — politics, economy, society, and foreign affairs — "
            f"reported by these trusted outlets: {source_list('india')}. "
            f"Only return stories where the original reporting outlet is one of these sources. "
            f"{window_rule(freshness['india'])} "
            f"Do not use news aggregators or unfamiliar regional outlets."
        ),
        "tech": (
            f"Search for the most important technology and AI news of the past {freshness['tech']} hours, "
            f"reported by these trusted outlets: {source_list('tech')}. "
            f"Only return stories where the original reporting outlet is one of these sources. "
            f"{window_rule(freshness['tech'])} "
            f"Do not use news aggregators or secondary tech blogs."
        ),
        "business": (
            f"Search for the most important business and finance news of the past {freshness['business']} hours, "
            f"reported by these trusted outlets: {source_list('business')}. "
            f"Only return stories where the original reporting outlet is one of these sources. "
            f"{window_rule(freshness['business'])} "
            f"Do not use news aggregators or investor blogs."
        ),
        "science": (
            f"Search for the most important science and health news of the past {freshness['science']} hours, "
            f"reported by these trusted outlets: {source_list('science')}. "
            f"Only return stories where the original reporting outlet is one of these sources. "
            f"{window_rule(freshness['science'])} "
            f"Do not use aggregator sites like ScienceDaily — find the primary journal or specialist outlet."
        ),
        "sports": (
            f"Search for the most important sports news of the past {freshness['sports']} hours — scores, results, transfers, and major stories — "
            f"reported by these trusted outlets: {source_list('sports')}. "
            f"Sports currently in season: {active_sports}. Prioritise coverage of these. "
            f"{window_rule(freshness['sports'])} "
            f"Only return stories where the original reporting outlet is one of these sources. "
            f"Additionally: search for any major knockout tournaments currently in progress (e.g. Champions League, Grand Slam tennis, World Cups, Copa America, etc.). "
            f"For each active tournament found, include one story summarising the state of play — who has advanced, current bracket or standings, and what fixtures are coming up. "
            f"This tournament summary should be based on current search results, not assumed from the calendar."
        ),
        "explore": (
            f"Search for interesting and thought-provoking long-form or feature stories published in the past 2 weeks "
            f"from these outlets: {explore_sources}. "
            f"Prioritise The Economist and New York Times — the reader subscribes to both and rarely has time to read long-form. "
            f"Surface their best essays, analysis, or features from the past 2 weeks first. "
            f"Fill remaining slots from the other outlets. "
            f"Also search for the top longform essays, blog posts, or papers trending on Hacker News in the past week — "
            f"surface the actual articles being linked, not Hacker News discussion pages. "
            f"These are the only acceptable sources for this section. "
            f"Every story MUST carry the direct article URL from the search results — a story "
            f"without a real URL will be discarded downstream, so prefer stories whose links you can confirm."
        ),
    }

    results = {}
    for category, query in search_queries.items():
        print(f"  🔍 Searching: {category}...")

        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=SEARCH_MODEL,
                    max_tokens=3000,
                    tools=[{
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": SEARCH_MAX_USES,
                    }],
                    messages=[{
                        "role": "user",
                        "content": f"""{source_instructions[category]}

Search query: {query}

Return the top 5 most important stories. For each story provide:
- headline (concise, informative)
- source_name (the specific outlet, e.g. "BBC", "Reuters" — not an aggregator)
- source_url (direct link to the specific article if you can confirm it — null if not available; do NOT use the outlet homepage as a fallback)
- published_date (exact date the article was published, in YYYY-MM-DD format — this is required; if uncertain, use your best estimate based on the search result)
- summary (2-3 sentences of substance, not just headline expansion)
- why_it_matters (1 sentence, only for the top 1-2 stories)
- approved (true if source is in the approved list, false if not)

Prefer approved outlets. If you find fewer than 3 stories from approved outlets, fill remaining slots with stories from other major reputable outlets (major newspapers, wire services, broadcasters) and mark approved as false. Do not use aggregators or blogs.

OUTPUT CONTRACT — absolute:
Return ONLY a JSON array, no prose, no apologies, no explanations. Best-effort beats
perfect: if you cannot confirm a story's exact URL or publication date, include the
story anyway with url set to null and your best date estimate — downstream validation
handles uncertainty; an apology helps nobody. Fewer than 5 stories is fine. Only if
you genuinely found no real stories at all, return []."""
                    }],
                )
                break
            except anthropic.RateLimitError as e:
                if attempt < 2:
                    wait = int(e.response.headers.get("retry-after", 60))
                    print(f"  ⚠️  Rate limit hit for {category} (attempt {attempt + 1}/3) — waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        # Extract text from response
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        # Tag each story with its source category so synthesis cannot cross-contaminate sections.
        # Models often wrap the array in a ```json fence despite instructions — strip it first.
        try:
            fenced = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
            stories = json.loads(fenced.group(1) if fenced else text)
            if isinstance(stories, list):
                for story in stories:
                    story["source_section"] = category
                text = json.dumps(stories)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass  # If parsing fails, pass raw text through unchanged

        results[category] = text
        print(f"  ✅ {category} done")
        time.sleep(10)  # Brief pause between categories to avoid rapid-fire token bursts

    return results


# ── Step 2: Synthesise into structured brief ──────────────────────────────
def synthesise_brief(raw_results: dict, coverage_entries: list[dict]) -> dict:
    """Have Claude synthesise raw search results into a polished brief."""
    print("✍️  Synthesising brief...")

    subscriptions = ", ".join(SOURCES["subscriptions"])
    explore_sources = ", ".join(SOURCES["explore_pool"])

    trusted_by_category = "\n".join(
        f"- {cat.title()}: {', '.join(outlets)}"
        for cat, outlets in TRUSTED_SOURCES.items()
    )

    # Suppression list: what recent briefs already shipped (last 7 days).
    recent = entries_since(coverage_entries, TODAY, days=7)
    covered_block = "\n".join(
        f"- [{e['date']}] ({e['section']}) {e['headline']}" for e in recent
    ) or "(no coverage history yet)"

    # Yesterday's items in full, so today's brief can correct them.
    yesterday_entries = [e for e in coverage_entries if e.get("date") == YESTERDAY.isoformat()]
    yesterday_block = "\n".join(
        f"- ({e['section']}) {e['headline']}: {e['summary']}" for e in yesterday_entries
    ) or "(no brief was delivered yesterday)"

    word_targets_block = "\n".join(
        f"- {c['name']}: ~{WORD_TARGETS[c['id']]} words" for c in CATEGORIES
    ) + f"\n- Explore: ~{WORD_TARGETS['explore']} words"

    banned_examples = prompt_banned_examples()

    prompt = f"""You are writing The Daily Brief for {DAY_NAME}, {DATE_STR}.

Here are raw news search results by category:

WORLD & POLITICS:
{raw_results.get('world', 'No results')}

INDIA:
{raw_results.get('india', 'No results')}

TECH & AI:
{raw_results.get('tech', 'No results')}

BUSINESS & FINANCE:
{raw_results.get('business', 'No results')}

SCIENCE & HEALTH:
{raw_results.get('science', 'No results')}

SPORTS:
{raw_results.get('sports', 'No results')}

EXPLORE (discovery sources):
{raw_results.get('explore', 'No results')}

Write a synthesised daily brief. Cross-reference stories across sources for accuracy.
The reader subscribes to: {subscriptions} — mark these with "subscriber": true.

SOURCE LAW — absolute:
Every fact in your output must come from the raw search results above. Do not add any
number, date, name, quote, or event from your own knowledge — synthesis means selecting,
cross-referencing, and rewriting what was found, never supplementing it. If a specific
number or detail in a story cannot be traced to the raw results, drop that detail and
keep the story, rather than shipping the unverifiable claim. A story you cannot source
at all gets cut entirely. Never soften an unverifiable quote into "attributed to" —
replace it or drop it.

ALREADY COVERED — recent briefs shipped these stories:
{covered_block}

Do not re-run a story from this list. A story on this list may return ONLY if there is a
genuinely new development in today's search results — and then the summary's first
sentence must say what changed since it was last covered. Continuing sagas are fine;
re-summarising the same event is not.

YESTERDAY'S BRIEF — for corrections:
{yesterday_block}

Compare yesterday's items against today's search results. If a fact yesterday's brief
reported is clearly contradicted by a sourced result today (a number was wrong, an event
did not happen as described, an outcome reversed), add an entry to the "corrections"
array in your output: what yesterday's brief said, what is actually the case, and the
source URL that shows it. A correction is the cheapest trust the brief will ever buy —
but only correct real contradictions backed by a URL, never minor wording. Most days
this array is empty.

WORD BUDGET — the ceiling is a hard limit:
The reader has {READING_MINUTES} minutes. The entire brief — headlines, summaries,
why-it-matters lines — must total no more than {WORD_CEILING} words. Per-section targets
(stay within about 25% of each):
{word_targets_block}

A big story in one section must not swallow another section's space. If the day's news
does not fill a section's target, let it run short — never pad. To make budget, cut
whole stories rather than compressing every story into mush.

PROSE RULES:
Write like a good newspaper wire editor: concrete subjects, active verbs, specific
numbers. Never use these words or patterns: {banned_examples}.
State facts directly — never announce that a fact is interesting.

SOURCE QUALITY RULES — strictly enforced:
Only include stories from these approved outlets per section:
{trusted_by_category}
- Explore: {explore_sources}

If a story in the raw results comes from a news aggregator (e.g. ScienceDaily, Crescendo AI, News9live, MarketScreener, or any site that republishes others' reporting), you must either:
  a) Identify and cite the actual primary source (the journal, university, or original outlet), or
  b) Omit the story entirely.
Do not include stories where you cannot identify a reputable primary source. Fewer high-quality stories is better than padding with aggregator content.
If a section has fewer than 2 stories from approved outlets, include the most important stories from any major reputable outlet (major newspapers, wire services, broadcasters) to bring each section to at least 3 stories. Mark these with a note in the source name like "Additional: [Outlet Name]".

FRESHNESS RULE:
Each section was searched over a rolling window (24–48 hours before this run). Prefer the
freshest stories. If published_date is missing or ambiguous, include the story.
The published_date in raw results is estimated by the search model and may be inaccurate — treat it as a guide, not ground truth. If a story clearly describes a current or ongoing event, include it even if the estimated date looks slightly old.
If a section has fewer than 3 fresh stories, extend the window to the past 5 days rather than leaving the section thin.
Do not re-cover a specific match result or concluded event (e.g. a tournament final) if it clearly belongs to a prior day's brief.

REAL STORIES ONLY — no placeholders:
Aim for at least 2 real stories per section, using the 5-day fallback and "Additional" outlets when the window is thin.
NEVER invent a story, and NEVER output a placeholder whose headline says "No Fresh Stories Available", "No Major Developments", or anything similar. If a section genuinely has nothing real to report even after the fallback, return an empty stories array for that section — the layout renders an honest "quiet day" note, which the reader trusts more than filler.

URL RULE — critical:
Every story MUST have at least one source with a real article URL. The raw search results above contain source_url fields — carry these through to the output. If the raw results have a URL for a story, you MUST include it.
For "Additional" outlets: construct the URL from the outlet's domain and the article slug if you found it via web search. Only set url to null as a last resort for subscriber-only sources where you confirmed the story but cannot link it directly.
Stories where ALL sources have null URLs will be automatically removed by the validation layer — an empty section is worse than a slightly uncertain URL.

CONTENT QUALITY RULES:
- Never include stories about a journal publishing an issue, a magazine releasing an edition, or other meta-publishing announcements. Focus on specific discoveries, findings, or events — not on the fact that a publication released content.
- Do not report future tournament schedules, semifinals, or event dates unless the story comes from a specific named outlet with a verifiable URL. If a sporting event's current status is unclear from the search results, omit it rather than risk fabrication.
- Each story must describe a specific, verifiable event or development. Generic roundups or summaries of "what's happening in [field]" are not stories.

SECTION ASSIGNMENT RULE — strictly enforced:
Each story in the raw results has a "source_section" field (e.g. "world", "india", "tech", "business", "science", "sports", "explore").
You MUST only place a story in the section whose id matches its source_section.
NEVER move a story from one section to another. A story with source_section "world" must not appear in Sports. A story with source_section "sports" must not appear in Business. No exceptions.
If a section is thin after applying this rule, use the fallback rules above (extend freshness window, add "Additional" outlets) — but only within that section's own source_section stories.

Return ONLY valid JSON with this exact structure:
{{
  "summary": "One-sentence overview of the day's top 3 stories",
  "sections": [
    {{
      "id": "world",
      "name": "World & Politics",
      "badge_class": "world",
      "number": "01",
      "stories": [
        {{
          "headline": "...",
          "sources": [
            {{"name": "BBC", "url": "https://bbc.com/news/article-slug", "subscriber": false}},
            {{"name": "The Economist", "url": null, "subscriber": true}}
          ],
          "summary": "2-3 sentences of real substance",
          "why_it_matters": "1 sentence or null"
        }}
      ]
    }},
    {{
      "id": "india",
      "name": "India",
      "badge_class": "india",
      "number": "02",
      "stories": [...]
    }},
    {{
      "id": "tech",
      "name": "Tech & AI",
      "badge_class": "tech",
      "number": "03",
      "stories": [...]
    }},
    {{
      "id": "business",
      "name": "Business & Finance",
      "badge_class": "business",
      "number": "04",
      "stories": [...]
    }},
    {{
      "id": "science",
      "name": "Science & Health",
      "badge_class": "science",
      "number": "05",
      "stories": [...]
    }},
    {{
      "id": "sports",
      "name": "Sports",
      "badge_class": "sports",
      "number": "06",
      "stories": [...]
    }}
  ],
  "explore": {{
    "source_name": "WIRED or MIT Technology Review etc.",
    "source_description": "One sentence about this source",
    "stories": [
      {{
        "headline": "...",
        "source_name": "...",
        "source_url": "...",
        "summary": "..."
      }}
    ]
  }},
  "corrections": [
    {{
      "what_we_said": "The fact as yesterday's brief reported it",
      "correction": "What is actually the case, per today's sources",
      "source_name": "...",
      "source_url": "https://..."
    }}
  ]
}}

Include 4-5 stories per news section and 2-3 explore stories.
Ensure all URLs are real and accurate. Do not invent URLs."""

    brief_data = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text if response.content else ""
            json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1)
            if not text.strip():
                raise ValueError("empty response")
            brief_data = json.loads(text)
            break
        except (anthropic.RateLimitError, ValueError, json.JSONDecodeError) as e:
            if attempt < 2:
                if isinstance(e, anthropic.RateLimitError):
                    wait = int(e.response.headers.get("retry-after", 60))
                    print(f"  ⚠️  Rate limit hit during synthesis (attempt {attempt + 1}/3) — waiting {wait}s...")
                else:
                    print(f"  ⚠️  Bad response during synthesis (attempt {attempt + 1}/3): {e} — retrying in 30s...")
                    wait = 30
                time.sleep(wait)
            else:
                raise RuntimeError(f"synthesise_brief failed after 3 attempts: {e}") from e

    print("  ✅ Brief synthesised")
    return brief_data


# ── Step 2b: Post-synthesis validation ────────────────────────────────────
# Structural guardrails that catch issues regardless of what the synthesis
# prompt produces. The prompt is advisory; this code is the safety net.

# Words too common to be meaningful for headline similarity.
_STOP_WORDS = frozenset(
    "a an the and or but in on for to of is are was were with from by at as "
    "its it that this be has have had not no been will set new says said after "
    "over into about up out may could how what when where who why".split()
)

# Patterns that indicate a meta-item (journal publication announcement, not news).
_META_PATTERNS = [
    re.compile(r"\b(?:publishes?|releases?|announces?)\s+(?:volume|issue|edition)\b", re.IGNORECASE),
    re.compile(r"\bvolume\s+\d+\s+issue\s+\d+\b", re.IGNORECASE),
    re.compile(r"\blatest\s+(?:issue|edition)\s+(?:of|featuring)\b", re.IGNORECASE),
    re.compile(r"\bjournal\s+(?:publishes?|releases?)\b", re.IGNORECASE),
]


def _headline_keywords(headline: str) -> set[str]:
    """Extract significant lowercase keywords from a headline."""
    words = set(re.findall(r"[a-z0-9]+", headline.lower()))
    return words - _STOP_WORDS


def _is_meta_item(headline: str, summary: str = "") -> bool:
    """Return True if the story is a meta-publishing announcement, not real news."""
    text = f"{headline} {summary}"
    return any(p.search(text) for p in _META_PATTERNS)


def _has_vague_source(story: dict) -> bool:
    """Return True if the story has no URL and only a generic 'Additional:' source."""
    sources = story.get("sources", [])
    if not sources:
        return True
    has_any_url = any(s.get("url") for s in sources)
    all_additional = all(
        s.get("name", "").startswith("Additional:") for s in sources
    )
    return not has_any_url and all_additional


def validate_brief(brief_data: dict, coverage_entries: list[dict] | None = None) -> dict:
    """Post-synthesis validation: dedup, meta-item filter, URL/source checks,
    repeat suppression against the coverage log, corrections sourcing.

    Modifies brief_data in place and returns it. Logs all removals.
    """
    print("🔍 Validating brief...")
    removed = []

    sections = brief_data.get("sections", [])

    # ── 1. Cross-section headline deduplication ──
    # Build index: keyword set → (section_id, story_index, headline)
    headline_index: list[tuple[str, int, set, str]] = []
    for section in sections:
        for i, story in enumerate(section.get("stories", [])):
            kw = _headline_keywords(story.get("headline", ""))
            if kw:
                headline_index.append((section["id"], i, kw, story["headline"]))

    # Find pairs with >50% keyword overlap (Jaccard similarity)
    duplicates_to_remove: set[tuple[str, int]] = set()
    for a in range(len(headline_index)):
        for b in range(a + 1, len(headline_index)):
            sec_a, idx_a, kw_a, hl_a = headline_index[a]
            sec_b, idx_b, kw_b, hl_b = headline_index[b]
            if sec_a == sec_b:
                continue  # only check cross-section
            intersection = kw_a & kw_b
            union = kw_a | kw_b
            if len(union) > 0 and len(intersection) / len(union) > 0.4:
                # Keep the one whose section is the more natural home.
                # Heuristic: sports stories stay in sports, india stories stay in india, etc.
                # If both have source_section tags, use those; otherwise keep the first.
                story_a = sections[next(j for j, s in enumerate(sections) if s["id"] == sec_a)]["stories"][idx_a]
                story_b = sections[next(j for j, s in enumerate(sections) if s["id"] == sec_b)]["stories"][idx_b]
                tag_a = story_a.get("source_section", sec_a)
                tag_b = story_b.get("source_section", sec_b)
                # Remove from the section that doesn't match the tag
                if tag_a == sec_a and tag_b != sec_b:
                    duplicates_to_remove.add((sec_b, idx_b))
                elif tag_b == sec_b and tag_a != sec_a:
                    duplicates_to_remove.add((sec_a, idx_a))
                else:
                    # Both match or neither does — remove the later one
                    duplicates_to_remove.add((sec_b, idx_b))

    for section in sections:
        original = section.get("stories", [])
        filtered = []
        for i, story in enumerate(original):
            if (section["id"], i) in duplicates_to_remove:
                removed.append(f"  DEDUP [{section['id']}] {story.get('headline', '?')}")
            else:
                filtered.append(story)
        section["stories"] = filtered

    # ── 1b. Placeholder filter — fake "no news available" story cards ──
    # Runs before the vague-source filter, whose keep-if-section-would-be-empty
    # fallback would otherwise preserve these. An empty section renders as an
    # honest "quiet day" note instead.
    for line in filter_placeholders(brief_data):
        removed.append(f"  {line}")

    # ── 2. Meta-item filter ──
    for section in sections:
        original = section.get("stories", [])
        filtered = []
        for story in original:
            if _is_meta_item(story.get("headline", ""), story.get("summary", "")):
                removed.append(f"  META  [{section['id']}] {story.get('headline', '?')}")
            else:
                filtered.append(story)
        section["stories"] = filtered

    # ── 3. Vague-source filter (no URL + generic "Additional:" source) ──
    # If removing all vague stories would empty a section, keep them rather than
    # showing a blank section — a story with a named source but no URL is better
    # than nothing.
    for section in sections:
        original = section.get("stories", [])
        filtered = []
        vague = []
        for story in original:
            if _has_vague_source(story):
                vague.append(story)
            else:
                filtered.append(story)
        if filtered:
            # Section has non-vague stories — safe to drop the vague ones
            for story in vague:
                removed.append(f"  VAGUE [{section['id']}] {story.get('headline', '?')}")
            section["stories"] = filtered
        elif vague:
            # ALL stories are vague — keep them to avoid an empty section
            for story in vague:
                removed.append(f"  VAGUE-KEPT [{section['id']}] {story.get('headline', '?')} (kept — section would be empty)")
            section["stories"] = vague
        # else: section was already empty, nothing to do

    # ── 4. Explore URL enforcement ──
    explore = brief_data.get("explore", {})
    if explore:
        original = explore.get("stories", [])
        filtered = []
        for story in original:
            url = story.get("source_url")
            if not url or url == "null":
                removed.append(f"  NOURL [explore] {story.get('headline', '?')}")
            else:
                filtered.append(story)
        explore["stories"] = filtered

    # ── 5. Repeat suppression against the coverage log ──
    # The prompt already asks Claude not to re-run covered stories; this is the
    # mechanical backstop for near-identical headlines from the last 2 days.
    if coverage_entries:
        recent = entries_since(coverage_entries, TODAY, days=2)
        recent = [e for e in recent if e.get("date") != DATE_FILE]
        for line in filter_repeats(brief_data, recent):
            removed.append(f"  {line}")

    # ── 6. Corrections must carry a source URL ──
    corrections = brief_data.get("corrections") or []
    sourced = []
    for corr in corrections:
        if corr.get("source_url"):
            sourced.append(corr)
        else:
            removed.append(f"  NOURL [corrections] {corr.get('correction', '?')}")
    brief_data["corrections"] = sourced

    # ── Report ──
    if removed:
        print(f"  ⚠️  Removed {len(removed)} stories:")
        for line in removed:
            print(line)
    else:
        print("  ✅ All stories passed validation")

    return brief_data


# ── Step 2b-ii: Word budget enforcement ───────────────────────────────────
def apply_word_budget(brief_data: dict) -> dict:
    """Enforce the reading-time word ceiling by dropping whole stories."""
    print(f"📏 Enforcing word budget ({READING_MINUTES} min × {WORDS_PER_MINUTE} wpm = {WORD_CEILING} words)...")
    log = enforce_word_budget(brief_data, WORD_TARGETS, WORD_CEILING)
    counts = section_word_counts(brief_data)
    total = sum(counts.values()) + len((brief_data.get("summary") or "").split())
    if log:
        for line in log:
            print(f"  ✂️  {line}")
    per_section = " · ".join(f"{sid} {counts[sid]}/{WORD_TARGETS.get(sid, '?')}" for sid in counts)
    print(f"  ✅ {total}/{WORD_CEILING} words ({per_section})")
    return brief_data


# ── Step 2b-iii: Prose linting ────────────────────────────────────────────
def polish_prose(brief_data: dict) -> dict:
    """Lint the brief for AI-sounding prose; one rewrite pass on violations.

    Never blocks delivery: if the rewrite fails or phrases survive it, the
    brief ships anyway and the remainder is logged.
    """
    print("🖋️  Linting prose...")
    violations = lint_brief(brief_data)
    if not violations:
        print("  ✅ No banned words or meta-frames")
        return brief_data

    for v in violations:
        print(f"  ⚠️  {v['label']}: {', '.join(v['phrases'])}")

    payload = [
        {"id": i, "text": v["obj"][v["key"]], "banned_phrases": v["phrases"]}
        for i, v in enumerate(violations)
    ]
    prompt = f"""These snippets from a news brief contain banned words or phrases.
Rewrite each snippet to remove ONLY the banned phrases, preserving every fact, number,
name, and roughly the same length. State facts directly instead of announcing them.
If a banned word is part of a proper noun (a company, product, or person's name), or
appears inside a verbatim quotation, return that snippet unchanged.

{json.dumps(payload, indent=2)}

Return ONLY a JSON object mapping each id (as a string) to the rewritten text:
{{"0": "rewritten...", "1": "rewritten..."}}"""

    try:
        response = client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text if response.content else ""
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        rewrites = json.loads(text)
        for i, v in enumerate(violations):
            new_text = rewrites.get(str(i))
            if new_text:
                v["obj"][v["key"]] = new_text
        remaining = lint_brief(brief_data)
        if remaining:
            for v in remaining:
                print(f"  ⚠️  Still flagged after rewrite (shipping anyway): {v['label']}: {', '.join(v['phrases'])}")
        else:
            print(f"  ✅ Rewrote {len(violations)} snippet(s), all clean")
    except Exception as e:
        print(f"  ⚠️  Prose rewrite failed (non-blocking): {e}")
    return brief_data


# ── Step 2c: Regression checks ────────────────────────────────────────────
def run_regression_checks(brief_data: dict) -> dict:
    """Check for recurrence of known bugs. Never blocks brief delivery."""
    print("🔬 Running regression checks...")

    bugs_path = Path("data/known_bugs.json")
    if not bugs_path.exists():
        print("  ℹ️  No known_bugs.json — skipping regression checks")
        return {"date": DATE_FILE, "regressions_detected": 0, "results": []}

    registry = json.loads(bugs_path.read_text())
    results = []
    regressions = 0

    for bug in registry.get("bugs", []):
        detected = False
        details = None
        bug_id = bug["id"]
        detection = bug.get("detection", {})
        det_type = detection.get("type")

        if det_type == "python_expr":
            try:
                eval_globals = {"brief_data": brief_data,
                     "_has_vague_source": _has_vague_source,
                     "len": len, "any": any}
                detected = eval(  # noqa: S307 — expressions from our own committed registry
                    detection["check"],
                    eval_globals,
                )
                if detected and "details_expr" in detection:
                    details = eval(detection["details_expr"], eval_globals)  # noqa: S307
            except Exception as e:
                details = f"Detection check failed: {e}"

        elif det_type == "url_domain_check":
            flagged = detection.get("flagged_domains", [])
            bad_urls = []
            for story in brief_data.get("explore", {}).get("stories", []):
                url = (story.get("source_url") or "").lower()
                for domain in flagged:
                    if domain in url:
                        bad_urls.append(url)
            if bad_urls:
                detected = True
                details = f"Found {len(bad_urls)} paywalled URL(s): {', '.join(bad_urls[:3])}"

        elif det_type == "section_tag_mismatch":
            mismatches = []
            for section in brief_data.get("sections", []):
                for story in section.get("stories", []):
                    tag = story.get("source_section")
                    if tag and tag != section["id"]:
                        mismatches.append(
                            f"{story.get('headline', '?')} (tagged {tag}, placed in {section['id']})"
                        )
            # This check used to live in the cross_section_headline_dedup branch
            # below, which meant two things: section_tag_mismatch could never fire
            # (cross-section bleed detection was dead), and the dedup check read a
            # `mismatches` left over from THIS iteration of the loop, attributing
            # bleed findings to the dedup bug id with its details, fix_recipe and
            # severity. Reordering known_bugs.json would also have raised NameError.
            if mismatches:
                detected = True
                details = f"{len(mismatches)} misplaced: {'; '.join(mismatches[:3])}"

        elif det_type == "cross_section_headline_dedup":
            # Check for semantically duplicate stories across sections
            all_stories = []
            for section in brief_data.get("sections", []):
                for story in section.get("stories", []):
                    kw = _headline_keywords(story.get("headline", ""))
                    all_stories.append((section["id"], story.get("headline", "?"), kw))
            dupes = []
            for i in range(len(all_stories)):
                for j in range(i + 1, len(all_stories)):
                    if all_stories[i][0] == all_stories[j][0]:
                        continue  # same section, skip
                    kw_a, kw_b = all_stories[i][2], all_stories[j][2]
                    if not kw_a or not kw_b:
                        continue
                    overlap = len(kw_a & kw_b) / len(kw_a | kw_b)
                    if overlap >= 0.35:
                        dupes.append(
                            f"[{all_stories[i][0]}] \"{all_stories[i][1]}\" ~ "
                            f"[{all_stories[j][0]}] \"{all_stories[j][1]}\" ({overlap:.0%})"
                        )
            if dupes:
                detected = True
                details = f"{len(dupes)} cross-section duplicate(s): {'; '.join(dupes[:3])}"

        result = {"bug_id": bug_id, "detected": detected, "details": details}
        if detected and bug.get("status") == "fixed":
            regressions += 1
            result["fix_recipe"] = bug.get("fix_recipe", [])
            result["severity"] = "high" if bug_id in ("explore-empty", "cross-section-bleed") else "medium"
            # Record recurrence in registry
            bug.setdefault("history", {}).setdefault("recurrences", []).append(DATE_FILE)

        results.append(result)

    report = {
        "date": DATE_FILE,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "regressions_detected": regressions,
        "total_checks": len(results),
        "results": results,
    }

    # Persist report (dry runs leave data/ untouched)
    if not DRY_RUN:
        report_path = Path("data/regression_report.json")
        report_path.write_text(json.dumps(report, indent=2))

        # Update registry with any new recurrence entries
        if regressions > 0:
            bugs_path.write_text(json.dumps(registry, indent=2))

    # Console output
    if regressions > 0:
        print(f"\n  🔴 REGRESSION DETECTED — {regressions} known bug(s) have recurred:")
        for r in results:
            if r.get("detected") and "fix_recipe" in r:
                print(f"     • {r['bug_id']}: {r.get('details', 'no details')}")
                for step in r["fix_recipe"]:
                    print(f"       → {step}")
    else:
        print(f"  ✅ Regression check passed ({len(results)} known bugs checked)")

    return report


# ── Step 3: Generate narration text ───────────────────────────────────────
def generate_narration(brief_data: dict) -> list[dict]:
    """Generate TTS-friendly narration text from brief data."""
    print("🎤 Generating narration text...")

    weather = brief_data.get("weather", [])
    weather_lines = []
    for w in weather:
        if not w.get("error"):
            weather_lines.append(
                f"{w['label']}: high {w['high_f']}°F, {w['condition']}. {w.get('clothing_tip', '')}"
            )
    weather_str = " / ".join(weather_lines) if weather_lines else ""

    # Only what the narration actually reads out — the full brief_data carries
    # weather/NBA/deep-dive payloads the prompt tells the model to ignore.
    narration_input = {
        "summary": brief_data.get("summary", ""),
        "corrections": brief_data.get("corrections") or [],
        "sections": [
            {
                "name": s.get("name"),
                "stories": [
                    {
                        "headline": st.get("headline"),
                        "summary": st.get("summary"),
                        "why_it_matters": st.get("why_it_matters"),
                    }
                    for st in s.get("stories", [])
                ],
            }
            for s in brief_data.get("sections", [])
        ],
        "explore": [
            {"headline": st.get("headline"), "source_name": st.get("source_name"), "summary": st.get("summary")}
            for st in (brief_data.get("explore") or {}).get("stories", [])
        ],
    }

    prompt = f"""Convert this daily brief into a spoken narration script for a British English newsreader.

Brief data:
{json.dumps(narration_input, indent=2)}

Rules:
- Write numbers as words ("sixty-eight billion" not "68B")
- No abbreviations ("United States" not "US", except well-known ones like "AI")
- No contractions ("do not" not "don't", "here is" not "here's")
- Natural speech phrasing with good rhythm
- Include brief transitions between sections
- Open with "The Daily Brief. {DAY_NAME}, {DATE_STR}. Good morning." then add one natural sentence covering today's weather for both locations: {weather_str}
- If the brief data contains a non-empty "corrections" array, add one sentence to the Introduction after the weather: "One correction from yesterday's brief: ..." stating the corrected fact plainly
- If a section has no stories, cover it with a single natural sentence noting it was a quiet day for that topic — do not invent news
- Close with "That is your Daily Brief for {DAY_NAME}. Have a great day."
- Do NOT narrate the Deep Dive section or the corrections array as their own sections — just the 6 news categories and Explore

Return a JSON array with exactly 8 objects:
[
  {{"label": "Introduction", "text": "..."}},
  {{"label": "World & Politics", "text": "..."}},
  {{"label": "India", "text": "..."}},
  {{"label": "Tech & AI", "text": "..."}},
  {{"label": "Business & Finance", "text": "..."}},
  {{"label": "Science & Health", "text": "..."}},
  {{"label": "Sports", "text": "..."}},
  {{"label": "Explore & Sign-off", "text": "..."}}
]

Return ONLY the JSON array."""

    sections = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=NARRATION_MODEL,
                max_tokens=6000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text if response.content else ""
            json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1)
            if not text.strip():
                raise ValueError("empty response")
            sections = json.loads(text)
            break
        except (anthropic.RateLimitError, ValueError, json.JSONDecodeError) as e:
            if attempt < 2:
                if isinstance(e, anthropic.RateLimitError):
                    wait = int(e.response.headers.get("retry-after", 60))
                    print(f"  ⚠️  Rate limit hit during narration (attempt {attempt + 1}/3) — waiting {wait}s...")
                else:
                    print(f"  ⚠️  Bad response during narration (attempt {attempt + 1}/3): {e} — retrying in 30s...")
                    wait = 30
                time.sleep(wait)
            else:
                raise RuntimeError(f"generate_narration failed after 3 attempts: {e}") from e
    print(f"  ✅ {len(sections)} narration sections generated")
    return sections


# ── Step 4: Generate audio via Deepgram ───────────────────────────────────
def _split_text(text: str, max_chars: int = 1900) -> list[str]:
    """Split text into chunks under max_chars, breaking at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_chars:
            chunks.append(text)
            break
        split_at = text.rfind(". ", 0, max_chars)
        if split_at == -1:
            split_at = text.rfind(" ", 0, max_chars)
        if split_at == -1:
            split_at = max_chars
        else:
            split_at += 1  # include the period
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return chunks


def _tts_request(text: str) -> bytes:
    """Send a single TTS request to Deepgram and return audio bytes."""
    response = requests.post(
        f"https://api.deepgram.com/v1/speak?model={DEEPGRAM_VOICE}",
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"text": text},
    )
    if response.status_code != 200:
        raise RuntimeError(f"Deepgram API error ({response.status_code}): {response.text[:200]}")
    return response.content


def generate_audio(narration_sections: list[dict]) -> Path:
    """Call Deepgram TTS API for each narration section, stitch into one MP3."""
    print("🔊 Generating audio via Deepgram...")

    audio_chunks = []
    for i, section in enumerate(narration_sections):
        print(f"  🎵 Section {i+1}/{len(narration_sections)}: {section['label']}...")

        chunks = _split_text(section["text"])
        section_bytes = b""
        for chunk in chunks:
            section_bytes += _tts_request(chunk)

        audio_chunks.append(section_bytes)
        print(f"  ✅ {section['label']} done ({len(section_bytes)} bytes)")

    # Stitch MP3 chunks together
    mp3_path = OUTPUT_DIR / f"daily_brief_{DATE_FILE}.mp3"
    with open(mp3_path, "wb") as f:
        for chunk in audio_chunks:
            f.write(chunk)

    total_size = mp3_path.stat().st_size
    print(f"  ✅ Audio saved: {mp3_path} ({total_size / 1024 / 1024:.1f} MB)")
    return mp3_path


# ── Step 5: Render HTML ───────────────────────────────────────────────────
def render_html(brief_data: dict, narration_sections: list[dict], mp3_filename: str) -> Path:
    """Render the HTML brief from the Jinja2 template."""
    print("📄 Rendering HTML...")

    template_str = TEMPLATE_PATH.read_text()
    template = Template(template_str)

    # This file is COMMITTED to a public repo (it is how the MP3 gets a public URL
    # for the email to link). The Deep Dive section is the recipient's reading and
    # listening queue — personal, and it would be published here purely as a side
    # effect of that hosting arrangement. Strip it from the archive copy; the email
    # still renders it, and the email is not public.
    #
    # Dropping the key rather than deleting the template block: the block is already
    # guarded by `{% if brief.deep_dive_state %}`, so it simply does not render, and
    # the markup survives for any future private-archive use.
    archive_data = {k: v for k, v in brief_data.items() if k != "deep_dive_state"}

    html = template.render(
        date_str=DATE_STR,
        day_name=DAY_NAME,
        date_file=DATE_FILE,
        brief=archive_data,
        narration=narration_sections,
        mp3_filename=mp3_filename,
        sources=SOURCES,
        elevenlabs_api_key=None,  # No browser-side key needed — MP3 is pre-generated
    )

    html_path = OUTPUT_DIR / f"{FILE_PREFIX}daily_brief_{DATE_FILE}.html"
    html_path.write_text(html)
    print(f"  ✅ HTML saved: {html_path}")
    return html_path


# ── Step 6: Generate email-safe HTML ──────────────────────────────────────
def generate_email_html(brief_data: dict, mp3_url: str) -> str:
    """Generate inline-styled email HTML programmatically from brief data."""
    print("📧 Generating email HTML...")

    ACCENT = "#1B4D3E"
    BADGE_COLORS = {
        "world": "#DC2626", "india": "#F97316", "tech": "#2563EB",
        "business": "#059669", "science": "#7C3AED",
        "sports": "#0EA5E9", "explore": "#D97706",
    }

    def badge(text, color):
        return (f'<span style="background:{color};color:#fff;padding:3px 10px;'
                f'border-radius:4px;font-size:11px;font-family:Arial,sans-serif;'
                f'font-weight:bold;text-transform:uppercase;letter-spacing:0.5px">{text}</span>')

    def render_story(story):
        sources_parts = []
        for s in story.get("sources", []):
            url = s.get("url") or ""
            if url:
                name_html = f'<a href="{url}" style="color:{ACCENT};text-decoration:none">{s["name"]}</a>'
            else:
                name_html = s["name"]
            if s.get("subscriber"):
                name_html += (' <span style="background:#059669;color:#fff;padding:1px 5px;'
                              'border-radius:3px;font-size:9px;font-family:Arial">Sub</span>')
            sources_parts.append(name_html)
        sources_html = " · ".join(sources_parts)

        why = ""
        if story.get("why_it_matters"):
            why = (f'<div style="background:#f0fdf4;border-left:3px solid {ACCENT};'
                   f'padding:8px 12px;margin:8px 0;font-size:13px;font-family:Georgia,serif;'
                   f'color:#374151"><strong>Why it matters:</strong> {story["why_it_matters"]}</div>')

        return (f'<div style="margin-bottom:20px">'
                f'<h3 style="margin:0 0 4px;font-family:Georgia,serif;font-size:16px;color:#111">{story["headline"]}</h3>'
                f'<div style="font-size:12px;color:#6b7280;margin-bottom:8px;font-family:Arial">{sources_html}</div>'
                f'<p style="margin:0;font-family:Georgia,serif;font-size:14px;color:#374151;line-height:1.6">{story["summary"]}</p>'
                f'{why}</div>')

    # NBA scoreboard (injected at top of Sports section)
    def nba_scoreboard_html(scores):
        if not scores:
            return ""
        games_html = ""
        for g in scores:
            away = f"<strong>{g['away_abbr']} {g['away_score']}</strong>" if g["away_win"] else f"{g['away_abbr']} {g['away_score']}"
            home = f"<strong>{g['home_abbr']} {g['home_score']}</strong>" if not g["away_win"] else f"{g['home_abbr']} {g['home_score']}"
            games_html += (f'<span style="background:#fff;border-radius:4px;padding:4px 8px;'
                           f'font-family:Arial;font-size:12px;color:#1e293b;white-space:nowrap">'
                           f'{away} <span style="color:#94a3b8">@</span> {home}</span> ')
        return (f'<div style="margin-bottom:16px;padding:10px 12px;background:#EFF6FF;'
                f'border-radius:6px;border:1px solid #BFDBFE">'
                f'<div style="font-family:Arial;font-size:10px;font-weight:bold;color:#1E40AF;'
                f'text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">🏀 NBA — Last Night</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:6px">{games_html}</div></div>')

    # Corrections block — sits above the news so it's never missed
    corrections_html = ""
    corrections = brief_data.get("corrections") or []
    if corrections:
        rows = ""
        for c in corrections:
            src = ""
            if c.get("source_url"):
                src = (f' (<a href="{c["source_url"]}" style="color:#92400E">'
                       f'{c.get("source_name", "source")}</a>)')
            rows += (f'<p style="margin:0 0 8px;font-family:Georgia,serif;font-size:13px;'
                     f'color:#374151;line-height:1.6"><strong>Yesterday we said:</strong> '
                     f'{c.get("what_we_said", "")} <strong>In fact:</strong> '
                     f'{c.get("correction", "")}{src}</p>')
        corrections_html = (f'<div style="margin-bottom:24px;padding:12px 16px;background:#FFFBEB;'
                            f'border-left:3px solid #D97706;border-radius:4px">'
                            f'<div style="font-family:Arial;font-size:10px;font-weight:bold;'
                            f'color:#92400E;text-transform:uppercase;letter-spacing:1px;'
                            f'margin-bottom:8px">Correction</div>{rows}</div>')

    # News sections
    sections_html = ""
    for section in brief_data.get("sections", []):
        color = BADGE_COLORS.get(section["id"], "#6b7280")
        stories = section.get("stories", [])
        if stories:
            stories_html = "".join(render_story(s) for s in stories)
        else:
            stories_html = (f'<p style="margin:0;font-family:Georgia,serif;font-size:13px;'
                            f'color:#9ca3af;font-style:italic">A quiet stretch — nothing new '
                            f'worth your time from your trusted {section["name"]} sources this run.</p>')
        scores_html = nba_scoreboard_html(brief_data.get("nba_scores", [])) if section["id"] == "sports" else ""
        sections_html += (f'<div style="margin-bottom:32px">'
                          f'<div style="margin-bottom:16px">{badge(section["name"], color)}</div>'
                          f'{scores_html}{stories_html}</div>')

    # Explore section
    explore = brief_data.get("explore", {})
    explore_html = ""
    if explore:
        explore_stories = ""
        for s in explore.get("stories", []):
            explore_stories += (f'<div style="margin-bottom:16px">'
                                f'<h3 style="margin:0 0 4px;font-family:Georgia,serif;font-size:15px;color:#111">'
                                f'<a href="{s.get("source_url","")}" style="color:#111;text-decoration:none">{s["headline"]}</a></h3>'
                                f'<p style="margin:0;font-family:Georgia,serif;font-size:13px;color:#374151;line-height:1.6">{s["summary"]}</p>'
                                f'</div>')
        explore_html = (f'<div style="margin-bottom:32px">'
                        f'<div style="margin-bottom:8px">{badge("Explore · " + explore.get("source_name",""), BADGE_COLORS["explore"])}</div>'
                        f'<p style="font-size:12px;color:#6b7280;font-family:Arial;margin:0 0 16px">{explore.get("source_description","")}</p>'
                        f'{explore_stories}</div>')

    # Deep Dive section
    deep_dive_html = ""
    dds = brief_data.get("deep_dive_state", {})
    if dds:
        def dd_email_item(icon, label, item, nudge=False):
            if not item:
                return ""
            days = item.get("days_shown", 1)
            day_str = f"Day {days}"
            if nudge and days >= 7:
                day_str += " — mark it done to get your next item"
            title_html = (f'<a href="{item["url"]}" style="color:{ACCENT};text-decoration:none">{item["title"]}</a>'
                          if item.get("url") else item.get("title", ""))
            desc = f'<p style="margin:4px 0 0;font-size:13px;font-family:Georgia,serif;color:#374151">{item["description"]}</p>' if item.get("description") else ""
            time_str = f' · {item["estimated_time"]}' if item.get("estimated_time") else ""
            return (f'<div style="margin-bottom:12px;padding:12px;background:#f8fafc;border-radius:6px">'
                    f'<div style="font-size:11px;color:#6b7280;font-family:Arial;margin-bottom:4px">'
                    f'{icon} {label} · {day_str}{time_str}</div>'
                    f'<div style="font-size:14px;font-family:Georgia,serif;font-weight:bold">{title_html}</div>'
                    f'<div style="font-size:11px;color:#9ca3af;font-family:Arial">{item.get("source","")}</div>'
                    f'{desc}</div>')

        book_r = dds.get("current_book_read")
        book_l = dds.get("current_book_listen")
        book_html = ""
        if book_r:
            if book_l and book_r.get("title") == book_l.get("title"):
                book_html = (f'<div style="margin-bottom:12px;padding:12px;background:#f8fafc;border-radius:6px">'
                             f'<div style="font-size:11px;color:#6b7280;font-family:Arial;margin-bottom:4px">📚 Book — reading + listening</div>'
                             f'<div style="font-size:14px;font-family:Georgia,serif;font-weight:bold">{book_r["title"]}</div>'
                             f'<div style="font-size:12px;color:#9ca3af;font-family:Arial">{book_r.get("author","")}</div></div>')
            else:
                book_html = (f'<div style="margin-bottom:12px;padding:12px;background:#f8fafc;border-radius:6px">'
                             f'<div style="font-size:11px;color:#6b7280;font-family:Arial;margin-bottom:4px">📚 Book</div>'
                             f'<div style="font-size:13px;font-family:Georgia,serif"><strong>Reading:</strong> {book_r["title"]} · {book_r.get("author","")}</div>'
                             + (f'<div style="font-size:13px;font-family:Georgia,serif"><strong>Listening:</strong> {book_l["title"]} · {book_l.get("author","")}</div>' if book_l else "")
                             + f'</div>')

        items_html = (dd_email_item("📖", "Read", dds.get("current_read"), nudge=True)
                      + dd_email_item("🎙️", "Listen", dds.get("current_listen"), nudge=True)
                      + book_html)
        if items_html:
            deep_dive_html = (f'<div style="margin-bottom:32px">'
                              f'<div style="margin-bottom:16px">{badge("Deep Dive", "#0891B2")}</div>'
                              f'{items_html}</div>')

    # Weather card
    weather_html = ""
    weather_data = brief_data.get("weather", [])
    if weather_data:
        cells = ""
        for w in weather_data:
            if w.get("error"):
                cells += (f'<td style="width:50%;text-align:center;padding:8px 4px;'
                          f'font-family:Arial;font-size:12px;color:#9ca3af">'
                          f'{w["location_icon"]} {w["label"]}<br>Unavailable</td>')
            else:
                cells += (f'<td style="width:50%;text-align:center;padding:12px 8px">'
                          f'<div style="font-family:Arial;font-size:11px;color:#6b7280;margin-bottom:4px">'
                          f'{w["location_icon"]} {w["label"]}</div>'
                          f'<div style="font-size:22px;margin-bottom:2px">{w["weather_icon"]}</div>'
                          f'<div style="font-family:Arial;font-size:20px;font-weight:bold;color:#111">High {w["high_f"]}°F</div>'
                          f'<div style="font-family:Arial;font-size:12px;color:#6b7280;margin-bottom:4px">'
                          f'{w["condition"]} · Now {w["temp_f"]}°F · Low {w["low_f"]}°F</div>'
                          f'<div style="font-family:Arial;font-size:12px;color:#1B4D3E;font-style:italic">'
                          f'{w["clothing_tip"]}</div>'
                          f'</td>')
        weather_html = (f'<table width="100%" style="margin-bottom:24px;background:#f8fafc;'
                        f'border-radius:8px;border-collapse:collapse">'
                        f'<tr>{cells}</tr></table>')

    # Audio link
    audio_html = ""
    if mp3_url:
        audio_html = (f'<div style="margin-bottom:32px;padding:16px;background:#f0fdf4;'
                      f'border-radius:8px;text-align:center">'
                      f'<p style="margin:0 0 10px;font-family:Arial;font-size:13px;color:#374151">🎧 Listen to today\'s brief</p>'
                      f'<a href="{mp3_url}" style="background:{ACCENT};color:#fff;padding:10px 24px;'
                      f'border-radius:6px;text-decoration:none;font-family:Arial;font-size:14px;font-weight:bold">'
                      f'Download MP3</a>'
                      f'<p style="margin:8px 0 0;font-size:11px;color:#9ca3af;font-family:Arial">'
                      f'Audio may take a moment to become available after delivery.</p></div>')

    html = (f'<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f9fafb">'
            f'<div style="max-width:600px;margin:0 auto;background:#fff;padding:32px 24px">'
            f'<div style="text-align:center;margin-bottom:24px;padding-bottom:24px;border-bottom:2px solid {ACCENT}">'
            f'<h1 style="margin:0 0 4px;font-family:Georgia,serif;font-size:28px;color:{ACCENT};letter-spacing:2px">THE DAILY BRIEF</h1>'
            f'<div style="font-family:Arial;font-size:13px;color:#6b7280">{DAY_NAME}, {DATE_STR}</div>'
            f'<p style="margin:12px 0 0;font-family:Georgia,serif;font-size:15px;color:#374151;font-style:italic">{brief_data.get("summary","")}</p>'
            f'</div>'
            f'{weather_html}{audio_html}{corrections_html}{sections_html}{explore_html}{deep_dive_html}'
            f'<div style="text-align:center;padding-top:24px;border-top:1px solid #e5e7eb;'
            f'font-family:Arial;font-size:11px;color:#9ca3af">Curated for {RECIPIENT_NAME} · {DAY_NAME}, {DATE_STR}</div>'
            f'</div></body></html>')

    print("  ✅ Email HTML generated")
    return html


# ── Step 7: Send email ────────────────────────────────────────────────────
def send_email(email_html: str) -> None:
    """Send the daily brief via Resend."""
    print("📬 Sending email...")

    if not RESEND_API_KEY or not RECIPIENT_EMAILS:
        raise RuntimeError("RESEND_API_KEY and RECIPIENT_EMAILS are required to send email")

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": SENDER_FROM,
            "to": RECIPIENT_EMAILS,
            "subject": f"The Daily Brief — {DAY_NAME}, {DATE_STR}",
            "html": email_html,
        },
    )

    if response.status_code != 200:
        raise RuntimeError(f"Resend API error ({response.status_code}): {response.text[:200]}")

    print(f"  ✅ Email sent to {', '.join(RECIPIENT_EMAILS)}")


# ── Cleanup: delete briefs older than 7 days ──────────────────────────────
def cleanup_old_briefs(days: int = 7) -> None:
    """Delete HTML and MP3 brief files older than `days` days."""
    print(f"🧹 Cleaning up briefs older than {days} days...")
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    removed = 0
    for ext in ("*.html", "*.mp3"):
        for f in OUTPUT_DIR.glob(ext):
            if datetime.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                print(f"  🗑️  Removed {f.name}")
                removed += 1
    if removed == 0:
        print("  ✅ Nothing to clean up")


# ── Main pipeline ─────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  THE DAILY BRIEF — {DAY_NAME}, {DATE_STR}")
    if DRY_RUN:
        print(f"  🧪 DRY RUN — no email, no audio, no state writes")
    print(f"{'='*60}\n")

    # Guard 1: skip if brief already generated today (prevents duplicate hourly runs)
    brief_html = OUTPUT_DIR / f"daily_brief_{DATE_FILE}.html"
    if brief_html.exists() and not DRY_RUN:
        print(f"  ✅ Brief already generated for {DATE_STR} — skipping.")
        return

    # Guard 2: check travel timezone from Google Calendar; skip if it's too early to deliver.
    # Uses >= not == so missed cron windows (GitHub Actions can skip hours) still trigger delivery.
    print("📅 Checking delivery schedule...")
    if DRY_RUN:
        print("  🧪 Dry run — bypassing scheduling guard.")
    elif os.environ.get("FORCE_GENERATE", "").lower() == "true":
        print("  ⚡ FORCE_GENERATE=true — bypassing scheduling guard.")
    else:
        travel_tz = detect_travel_timezone()
        target_hour = target_utc_hour_for(travel_tz)
        current_utc_hour = datetime.datetime.now(datetime.timezone.utc).hour
        if current_utc_hour < target_hour:
            tz_label = travel_tz.replace("_", " ")
            print(f"  ⏭  Too early — targeting UTC {target_hour:02d}:00 ({TARGET_LOCAL_HOUR}am {tz_label}). Current UTC hour: {current_utc_hour:02d}. Skipping.")
            return
        print(f"  ✅ Delivering — UTC {current_utc_hour:02d}:00 is on or past target UTC {target_hour:02d}:00 ({TARGET_LOCAL_HOUR}am {travel_tz.replace('_', ' ')})")

    # Step 0: Clean up old briefs, fetch weather and NBA scores
    if not DRY_RUN:
        cleanup_old_briefs()
    weather = fetch_weather()
    nba_scores = fetch_nba_scores()

    # Step 1: Search
    raw_results = search_news()
    if DRY_RUN:
        raw_path = OUTPUT_DIR / f"{FILE_PREFIX}raw_results_{DATE_FILE}.json"
        raw_path.write_text(json.dumps(raw_results, indent=2))
        print(f"  🧪 Raw search results saved for inspection: {raw_path}")

    # Step 2: Synthesise (coverage log feeds the suppression list + corrections)
    coverage_entries = load_coverage_log(COVERAGE_LOG_PATH)
    brief_data = synthesise_brief(raw_results, coverage_entries)
    brief_data["weather"] = weather
    brief_data["nba_scores"] = nba_scores

    # Step 2a: Post-synthesis validation (structural guardrails)
    brief_data = validate_brief(brief_data, coverage_entries)

    # Step 2a-ii: Enforce the reading-time word ceiling
    brief_data = apply_word_budget(brief_data)

    # Step 2a-iii: Prose lint + one rewrite pass (never blocks delivery)
    brief_data = polish_prose(brief_data)

    # Step 2a-iv: Record what shipped in the rolling coverage log
    if DRY_RUN:
        would_record = record_coverage(brief_data, [], TODAY)
        print(f"  🧪 Dry run — coverage log untouched ({len(would_record)} entries would be recorded)")
    else:
        coverage_entries = record_coverage(brief_data, coverage_entries, TODAY)
        save_coverage_log(COVERAGE_LOG_PATH, coverage_entries, TODAY)
        print(f"  🗂️  Coverage log updated ({len(coverage_entries)} entries, 30-day window)")

    # Step 2b: Load and enrich deep dive state
    print("📚 Loading deep dive state...")
    deep_dive_state = load_deep_dive_state()
    if deep_dive_state:
        if deep_dive_state.get("current_read") and not deep_dive_state["current_read"].get("description"):
            deep_dive_state["current_read"] = enrich_deep_dive_item(deep_dive_state["current_read"], "read")
        if deep_dive_state.get("current_listen") and not deep_dive_state["current_listen"].get("description"):
            deep_dive_state["current_listen"] = enrich_deep_dive_item(deep_dive_state["current_listen"], "listen")
        for key in ("current_read", "current_listen"):
            item = deep_dive_state.get(key)
            if item and item.get("first_shown"):
                first = datetime.date.fromisoformat(item["first_shown"])
                item["days_shown"] = (TODAY - first).days + 1
        # No write-back: the queue lives in an external store, read-only from here.
        # days_shown is recomputed above, so nothing is lost.
        #
        # Enrichment isn't cached, so it re-runs per item per brief (up to 2 extra
        # web searches). Better fix: persist descriptions upstream when the queue
        # is updated, rather than giving this public repo write access.
        print("  ✅ Deep dive state ready")
    brief_data["deep_dive_state"] = deep_dive_state

    # Save raw data for debugging
    data_path = OUTPUT_DIR / f"{FILE_PREFIX}daily_brief_{DATE_FILE}.json"
    data_path.write_text(json.dumps(brief_data, indent=2))
    print(f"  💾 Data saved: {data_path}")

    # Step 2c: Regression checks (never blocks delivery)
    try:
        run_regression_checks(brief_data)
    except Exception as e:
        print(f"  ⚠️  Regression check failed (non-blocking): {e}")

    # Step 3: Generate narration
    narration_sections = generate_narration(brief_data)

    # Step 4: Generate audio (skipped on dry runs — no Deepgram key needed)
    if DRY_RUN:
        print("🔊 Dry run — skipping audio generation")
        mp3_path = None
    else:
        mp3_path = generate_audio(narration_sections)

    # Step 5: Render HTML
    html_path = render_html(brief_data, narration_sections, mp3_path.name if mp3_path else "")

    # Step 6: Generate email HTML (programmatic, no API call)
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    mp3_url = f"https://raw.githubusercontent.com/{repo_slug}/main/briefs/{mp3_path.name}" if mp3_path else ""
    email_html = generate_email_html(brief_data, mp3_url)

    # Step 7: Send email (dry runs save a preview instead)
    if DRY_RUN:
        email_path = OUTPUT_DIR / f"{FILE_PREFIX}email_{DATE_FILE}.html"
        email_path.write_text(email_html)
        print(f"📬 Dry run — email NOT sent; preview saved: {email_path}")
    else:
        send_email(email_html)

    print(f"\n{'='*60}")
    print(f"  ✅ BRIEF COMPLETE" + (" (DRY RUN — nothing sent, no state written)" if DRY_RUN else ""))
    print(f"  📄 HTML:  {html_path}")
    if mp3_path:
        print(f"  🔊 Audio: {mp3_path}")
    print(f"  💾 Data:  {data_path}")
    print(f"{'='*60}\n")

    return {
        "html_path": str(html_path),
        "mp3_path": str(mp3_path) if mp3_path else None,
        "date": DATE_FILE,
    }


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Test script: detects travel timezone for a given date using Google Calendar ICS.
Usage: python test_schedule.py [YYYY-MM-DD]
"""
import sys
import datetime
import zoneinfo
import requests
from icalendar import Calendar as ICal
import os

# ── Config (mirrors generate_brief.py) ────────────────────────────────────
CALENDAR_ICS_URL = os.environ.get("CALENDAR_ICS_URL", "")
DEFAULT_TIMEZONE = "America/Los_Angeles"
TARGET_LOCAL_HOUR = 5

LOCATION_TIMEZONE_MAP = {
    "dublin": "Europe/Dublin", "ireland": "Europe/Dublin", "cork": "Europe/Dublin",
    "london": "Europe/London", "edinburgh": "Europe/London", "uk": "Europe/London",
    "united kingdom": "Europe/London", "england": "Europe/London", "scotland": "Europe/London",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "berlin": "Europe/Berlin", "munich": "Europe/Berlin", "germany": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "zurich": "Europe/Zurich", "switzerland": "Europe/Zurich",
    "rome": "Europe/Rome", "milan": "Europe/Rome", "italy": "Europe/Rome",
    "barcelona": "Europe/Madrid", "madrid": "Europe/Madrid", "spain": "Europe/Madrid",
    "new york": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York",
    "washington": "America/New_York", "miami": "America/New_York", "atlanta": "America/New_York",
    "chicago": "America/Chicago", "houston": "America/Chicago", "dallas": "America/Chicago",
    "austin": "America/Chicago", "denver": "America/Denver", "phoenix": "America/Phoenix",
    "seattle": "America/Los_Angeles", "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "hyderabad": "Asia/Kolkata", "chennai": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong", "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "china": "Asia/Shanghai", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "australia": "Australia/Sydney", "auckland": "Pacific/Auckland", "new zealand": "Pacific/Auckland",
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai", "abu dhabi": "Asia/Dubai",
}


def _location_to_timezone(location: str) -> str:
    loc = location.lower()
    for keyword, tz in LOCATION_TIMEZONE_MAP.items():
        if keyword in loc:
            return tz
    return DEFAULT_TIMEZONE


def detect_travel_timezone(test_date: datetime.date) -> tuple[str, list[str]]:
    """Returns (timezone_name, list of matching event summaries)."""
    matches = []
    if not CALENDAR_ICS_URL:
        return DEFAULT_TIMEZONE, ["⚠️  CALENDAR_ICS_URL not set"]
    try:
        r = requests.get(CALENDAR_ICS_URL, timeout=10)
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
            start_date = start_val.date() if isinstance(start_val, datetime.datetime) else start_val
            end_date = end_val.date() if isinstance(end_val, datetime.datetime) else end_val
            if not (start_date <= test_date < end_date):
                continue
            if (end_date - start_date).days < 2:
                continue
            summary = str(component.get("SUMMARY", "(no title)"))
            location = str(component.get("LOCATION", ""))
            matches.append(f"  Event: '{summary}' | {start_date} → {end_date} | Location: '{location}'")
            # TZID check
            if isinstance(start_val, datetime.datetime) and start_val.tzinfo:
                tz_key = getattr(start_val.tzinfo, "key", str(start_val.tzinfo))
                if tz_key and tz_key not in ("UTC", DEFAULT_TIMEZONE, "US/Pacific", "America/Pacific"):
                    try:
                        zoneinfo.ZoneInfo(tz_key)
                        return tz_key, matches
                    except zoneinfo.ZoneInfoNotFoundError:
                        pass
            # Location check
            if location:
                tz = _location_to_timezone(location)
                if tz != DEFAULT_TIMEZONE:
                    return tz, matches
        return DEFAULT_TIMEZONE, matches
    except Exception as e:
        return DEFAULT_TIMEZONE, [f"⚠️  Error: {e}"]


def target_utc_hour_for(tz_name: str, target_date: datetime.date) -> int:
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
        local_dt = datetime.datetime.combine(target_date, datetime.time(TARGET_LOCAL_HOUR, 0), tzinfo=tz)
        return local_dt.astimezone(datetime.timezone.utc).hour
    except Exception:
        return 13


# ── Run ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_date_str = sys.argv[1] if len(sys.argv) > 1 else str(datetime.date.today())
    test_date = datetime.date.fromisoformat(test_date_str)

    print(f"\nSchedule check for: {test_date.strftime('%A, %B %d, %Y')}")
    print("=" * 50)

    tz_name, events = detect_travel_timezone(test_date)
    utc_hour = target_utc_hour_for(tz_name, test_date)

    local_tz = zoneinfo.ZoneInfo(tz_name)
    local_dt = datetime.datetime.combine(test_date, datetime.time(TARGET_LOCAL_HOUR, 0), tzinfo=local_tz)
    utc_dt = local_dt.astimezone(datetime.timezone.utc)

    print(f"\nMulti-day events on this date:")
    if events:
        for e in events:
            print(e)
    else:
        print("  (none found)")

    print(f"\nResult:")
    print(f"  Detected timezone : {tz_name}")
    print(f"  Target delivery   : 6:00am {tz_name.replace('_', ' ')} = UTC {utc_hour:02d}:00")
    print(f"  Brief runs at     : {utc_dt.strftime('%H:%M')} UTC on {test_date}")
    print()

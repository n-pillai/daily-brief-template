#!/usr/bin/env python3
"""Unit tests for brief_quality.py. Run with: python test_quality.py"""

import datetime
import json
import tempfile
from pathlib import Path

from brief_quality import (
    compute_word_targets,
    count_story_words,
    enforce_word_budget,
    entries_since,
    filter_placeholders,
    filter_repeats,
    find_banned,
    headline_similarity,
    lint_brief,
    load_coverage_log,
    record_coverage,
    save_coverage_log,
)

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ {name} {detail}")


def make_story(headline, words=50):
    return {
        "headline": headline,
        "summary": " ".join(["word"] * words),
        "why_it_matters": None,
        "sources": [{"name": "BBC", "url": "https://bbc.com/x", "subscriber": False}],
    }


# ── Prose linter ───────────────────────────────────────────────────────────
print("Prose linter:")
check("flags 'delve'", find_banned("Researchers delve into the data.") == ["delve"])
check("flags lowercase 'leverage'", "leverage" in find_banned("Firms leverage new tools daily."))
check("skips proper noun mid-sentence", find_banned("Shares of Paramount rose 4% today.") == [])
check("flags sentence-initial banned proper-noun word",
      "paramount" in find_banned("It matters. Paramount to the deal is trust."))
check("skips quoted material", find_banned('The CEO said "we will delve into this".') == [])
check("flags meta-frame", any("here" in p or "thing" in p for p in
      find_banned("Here's the thing: rates went up.")))
check("clean prose passes", find_banned("The Fed held rates at 5.5% for a fifth meeting.") == [])

brief = {
    "summary": "A transformative day for markets.",
    "sections": [{"id": "tech", "stories": [make_story("Firm ships cutting-edge chip")]}],
    "explore": {"stories": []},
    "corrections": [],
}
violations = lint_brief(brief)
check("lint_brief finds both fields", {v["label"] for v in violations} ==
      {"summary", "tech/story-1/headline"}, str([v["label"] for v in violations]))
violations[0]["obj"][violations[0]["key"]] = "A big day for markets."
check("violations patch in place", brief["summary"] == "A big day for markets.")

# ── Word budget ────────────────────────────────────────────────────────────
print("Word budget:")
targets = compute_word_targets({"a": 3, "b": 1}, 1, 500)
check("targets proportional to weight", targets == {"a": 300, "b": 100, "explore": 100}, str(targets))
check("count_story_words", count_story_words(
    {"headline": "one two three", "summary": "four five", "why_it_matters": None}) == 5)

over_brief = {
    "summary": "short",
    "sections": [
        {"id": "a", "stories": [make_story(f"A{i}", 100) for i in range(4)]},
        {"id": "b", "stories": [make_story(f"B{i}", 30) for i in range(3)]},
    ],
    "explore": {"stories": [make_story("E0", 40), make_story("E1", 40)]},
}
log = enforce_word_budget(over_brief, {"a": 300, "b": 100, "explore": 100}, 500)
counts_after = sum(
    count_story_words(s) for sec in over_brief["sections"] for s in sec["stories"]
) + sum(count_story_words(s) for s in over_brief["explore"]["stories"])
check("trims under ceiling", counts_after + 1 <= 500, f"{counts_after}")
check("drops from most-over section first", "A3" not in
      [s["headline"] for s in over_brief["sections"][0]["stories"]])
check("news sections keep >= 2 stories",
      all(len(sec["stories"]) >= 2 for sec in over_brief["sections"]))
check("explore keeps >= 1 story", len(over_brief["explore"]["stories"]) >= 1)
check("drops are logged", len(log) > 0)

under_brief = {"summary": "s", "sections": [{"id": "a", "stories": [make_story("A", 20)]}],
               "explore": {"stories": []}}
check("under-budget brief untouched",
      enforce_word_budget(under_brief, {"a": 100, "explore": 0}, 500) == [])

# ── Coverage log ───────────────────────────────────────────────────────────
print("Coverage log:")
today = datetime.date(2026, 8, 4)
entries = [
    {"date": "2026-08-03", "section": "tech", "headline": "OpenAI ships new model", "summary": "s", "urls": []},
    {"date": "2026-07-01", "section": "world", "headline": "Old story", "summary": "s", "urls": []},
]
check("entries_since filters by window",
      [e["headline"] for e in entries_since(entries, today, 7)] == ["OpenAI ships new model"])

with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "coverage_log.json"
    save_coverage_log(path, entries, today, retention_days=30)
    reloaded = load_coverage_log(path)
    check("save prunes entries older than 30 days",
          [e["headline"] for e in reloaded] == ["OpenAI ships new model"])
    check("load of missing file returns empty", load_coverage_log(Path(td) / "nope.json") == [])

shipped = {
    "sections": [{"id": "tech", "stories": [make_story("Apple unveils headset")]}],
    "explore": {"stories": [{"headline": "Long read", "summary": "s", "source_url": "https://x.com/a"}]},
}
recorded = record_coverage(shipped, [], today)
check("records sections and explore", len(recorded) == 2 and
      {e["section"] for e in recorded} == {"tech", "explore"})
recorded = record_coverage(shipped, recorded, today)
check("recording is idempotent", len(recorded) == 2)

# ── Repeat filter ──────────────────────────────────────────────────────────
print("Repeat filter:")
check("similarity of identical headlines", headline_similarity("Fed holds rates", "Fed holds rates") == 1.0)
repeat_brief = {
    "sections": [{
        "id": "business",
        "stories": [
            make_story("Fed holds interest rates steady fifth meeting"),
            make_story("Oil prices jump on supply cut"),
            make_story("Airline merger approved by regulator"),
        ],
    }],
    "explore": {"stories": []},
}
yesterday_entries = [{"date": "2026-08-03", "section": "business",
                      "headline": "Fed holds interest rates steady for fifth meeting", "summary": "s", "urls": []}]
log = filter_repeats(repeat_brief, yesterday_entries)
headlines = [s["headline"] for s in repeat_brief["sections"][0]["stories"]]
check("near-identical repeat removed", "Fed holds interest rates steady fifth meeting" not in headlines, str(headlines))
check("fresh stories kept", len(headlines) == 2)
check("follow-up with new development survives",
      filter_repeats({"sections": [{"id": "b", "stories": [
          make_story("Fed cuts rates in surprise reversal"), make_story("X"), make_story("Y")]}],
          "explore": {"stories": []}}, yesterday_entries) == [])

floor_brief = {"sections": [{"id": "b", "stories": [
    make_story("Fed holds interest rates steady fifth meeting"),
    make_story("Oil prices jump")]}], "explore": {"stories": []}}
log = filter_repeats(floor_brief, yesterday_entries)
check("repeat kept when section at floor", len(floor_brief["sections"][0]["stories"]) == 2
      and any("REPEAT-KEPT" in line for line in log))

# ── Placeholder filter ─────────────────────────────────────────────────────
print("Placeholder filter:")
ph_brief = {
    "sections": [
        {"id": "india", "stories": [make_story("No Major Developments from Approved Indian Outlets")]},
        {"id": "science", "stories": [make_story("No Fresh Science Stories from Approved Outlets")]},
        {"id": "sports", "stories": [make_story("No Current Sports Coverage Available from Approved Sources")]},
        {"id": "world", "stories": [
            make_story("No survivors found after ferry capsizes off coast"),
            make_story("Summit produces draft accord"),
        ]},
    ],
    "explore": {"stories": [
        {"headline": "No Long-Form Features Available", "summary": "s", "source_url": None},
        {"headline": "A real essay on attention", "summary": "s", "source_url": "https://x.com/a"},
    ]},
}
log = filter_placeholders(ph_brief)
check("drops all placeholder cards even when section becomes empty",
      all(len(s["stories"]) == 0 for s in ph_brief["sections"][:3]), str(log))
check("keeps real 'No survivors' headline",
      len(ph_brief["sections"][3]["stories"]) == 2)
check("drops explore placeholder, keeps real explore story",
      [s["headline"] for s in ph_brief["explore"]["stories"]] == ["A real essay on attention"])
check("removals are logged", len(log) == 4, str(log))
check("nothing-to-report variant caught",
      filter_placeholders({"sections": [{"id": "x", "stories": [make_story("Nothing new to report in markets")]}],
                           "explore": {"stories": []}}) != [])

# ── Result ─────────────────────────────────────────────────────────────────
print(f"\n{PASSED} passed, {len(FAILED)} failed")
if FAILED:
    raise SystemExit(f"FAILED: {', '.join(FAILED)}")

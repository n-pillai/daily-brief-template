#!/usr/bin/env python3
"""Unit tests for run_regression_checks() in generate_brief.py.

Run with: python test_regressions.py

Covers the section_tag_mismatch / cross_section_headline_dedup defect: the
mismatch check lived in the *dedup* branch, so bleed detection never fired and
the dedup check read a `mismatches` left over from the previous loop iteration.

These tests are written to FAIL against the pre-fix code, not merely to pass
against the new code — a test that passes both ways proves nothing.

generate_brief.py imports the Anthropic SDK and reads a timezone at module
scope, so the heavy deps are stubbed before it is loaded. DRY_RUN is set so the
checks never write to data/.
"""

import datetime
import importlib.util
import json
import os
import sys
import tempfile
import types
import zoneinfo
from pathlib import Path

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def load_generate_brief():
    """Import generate_brief.py with its external dependencies stubbed out."""
    for name in ("anthropic", "requests", "jinja2", "icalendar"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["jinja2"].Template = type("Template", (), {"__init__": lambda s, *a, **k: None})
    sys.modules["anthropic"].Anthropic = type("Anthropic", (), {"__init__": lambda s, *a, **k: None})
    sys.modules["icalendar"].Calendar = type("Calendar", (), {})
    # Windows has no system tz database; the value is irrelevant to these tests.
    try:
        zoneinfo.ZoneInfo("America/Los_Angeles")
    except Exception:
        zoneinfo.ZoneInfo = lambda key: datetime.timezone.utc

    for key in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "RESEND_API_KEY"):
        os.environ.setdefault(key, "test")
    os.environ.setdefault("RECIPIENT_EMAILS", "test@example.com")
    os.environ["DRY_RUN"] = "true"  # never write to data/

    target = Path(__file__).parent / "generate_brief.py"
    spec = importlib.util.spec_from_file_location("gb_under_test", target)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass
    return module


REGISTRY = {
    "schema_version": 1,
    "bugs": [
        {
            "id": "cross-section-bleed",
            "status": "fixed",
            "detection": {"type": "section_tag_mismatch"},
            "fix_recipe": ["bleed recipe"],
        },
        {
            "id": "cross-section-duplicate",
            "status": "fixed",
            "detection": {"type": "cross_section_headline_dedup"},
            "fix_recipe": ["dupe recipe"],
        },
    ],
}


def run(gb, brief_data, registry=REGISTRY):
    """Run the checks against a throwaway data/ so nothing real is touched."""
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        os.chdir(tmp)
        Path("data").mkdir()
        Path("data/known_bugs.json").write_text(json.dumps(registry), encoding="utf-8")
        report = gb.run_regression_checks(brief_data)
    finally:
        os.chdir(cwd)
    return {r["bug_id"]: r for r in report["results"]}, report


def story(headline, tag):
    return {"headline": headline, "source_section": tag, "summary": ""}


# A story tagged `india` but placed in the `sports` section — textbook bleed.
BLED = {"sections": [
    {"id": "world", "stories": [story("Election result certified", "world")]},
    {"id": "sports", "stories": [story("Rupee hits record low", "india")]},
]}

# Correctly placed, and no two headlines resemble each other.
CLEAN = {"sections": [
    {"id": "world", "stories": [story("Election result certified", "world")]},
    {"id": "sports", "stories": [story("Rain delays the second test match", "sports")]},
]}

# Same story in two sections, correctly tagged to where each sits — dedup should
# fire, bleed should not.
DUPED = {"sections": [
    {"id": "tech", "stories": [story("Chipmaker unveils new accelerator platform", "tech")]},
    {"id": "business", "stories": [story("Chipmaker unveils new accelerator platform", "business")]},
]}


def main():
    gb = load_generate_brief()

    print("section_tag_mismatch fires on real bleed:")
    res, report = run(gb, BLED)
    check("bleed detected", res["cross-section-bleed"]["detected"],
          "pre-fix this was always False — the check lived in the dedup branch")
    check("bleed details name the misplaced story",
          "Rupee" in (res["cross-section-bleed"].get("details") or ""))
    check("bleed carries its own fix_recipe",
          res["cross-section-bleed"].get("fix_recipe") == ["bleed recipe"])
    check("bleed counted as a regression", report["regressions_detected"] >= 1)

    print("\ndedup does NOT inherit the mismatch result:")
    check("dedup not detected on a bleed-only brief",
          not res["cross-section-duplicate"]["detected"],
          "pre-fix `mismatches` leaked from the previous loop iteration")
    check("dedup has no details on a bleed-only brief",
          not res["cross-section-duplicate"].get("details"))
    check("only one regression counted, not two", report["regressions_detected"] == 1)

    print("\ndedup still catches genuine duplicates:")
    res, _ = run(gb, DUPED)
    check("duplicate detected", res["cross-section-duplicate"]["detected"])
    check("bleed not detected when tags are correct",
          not res["cross-section-bleed"]["detected"])

    print("\nclean brief trips nothing:")
    res, report = run(gb, CLEAN)
    check("no bleed", not res["cross-section-bleed"]["detected"])
    check("no duplicate", not res["cross-section-duplicate"]["detected"])
    check("zero regressions", report["regressions_detected"] == 0)

    print("\nregistry order must not matter:")
    reversed_registry = {"schema_version": 1, "bugs": list(reversed(REGISTRY["bugs"]))}
    res, _ = run(gb, BLED, reversed_registry)
    check("bleed still detected with the registry reversed",
          res["cross-section-bleed"]["detected"],
          "pre-fix this order raised NameError on an undefined `mismatches`")

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

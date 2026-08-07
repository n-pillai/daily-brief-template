#!/usr/bin/env python3
"""Quality layer for the Daily Brief: prose linting, word budgets, coverage log.

Kept separate from generate_brief.py so it imports without API keys and can be
unit-tested. Three parts:

1. Prose linter — banned-word and meta-frame detection. Words that double as
   proper nouns (Paramount, Harness, Beacon...) are only flagged lowercase or
   at a sentence start; quoted material is somebody else's words and is skipped.
2. Word budget — reading minutes × words-per-minute is a hard ceiling,
   allocated across sections by weight. Over budget means dropping whole
   stories from the most-over-target section, never compressing everything.
3. Coverage log — rolling 30-day record of what each brief shipped, used to
   suppress repeats and to let today's brief correct yesterday's.

Attribution: the prose linter's banned-word lists (SLOP_ANY, SLOP_LOWER),
meta-frame patterns, and the proper-noun/quoted-material exemption approach
are adapted from scripts/check.py in The Daily Brief skill by Tenex Labs
(https://github.com/tenex-labs/daily-brief). The word-budget, coverage-log,
and corrections designs are also modeled on that project's SKILL.md. Used
under the MIT License:

    MIT License

    Copyright (c) 2026 Tenex Labs

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to
    the following conditions:

    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
    CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
    TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
    SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import datetime
import json
import re
from pathlib import Path

# ── Prose linter ───────────────────────────────────────────────────────────

# Matched case-insensitively: these are never proper nouns.
SLOP_ANY = [
    r"delv(e|es|ing|ed)", r"utiliz(e|es|ing|ed)", r"facilitat(e|es|ing|ed)",
    r"cutting.edge", r"paradigm.shift", r"game.chang(er|ing)",
    r"this changes everything", r"multifaceted", r"meticulous(ly)?", r"intricate",
    r"transformative", r"supercharg(e|es|ing|ed)", r"ever.evolving",
    r"pivotal moment", r"vital role", r"underscor(e|es|ing|ed)",
    r"showcas(e|es|ing|ed)", r"it.s worth noting",
    r"it.s important to note", r"at the end of the day", r"in today.s world",
    r"in the age of", r"let.s dive in", r"in conclusion", r"when it comes to",
    r"at its core", r"going forward",
]
# Matched lowercase-only: capitalised, each of these is somebody's name.
SLOP_LOWER = [
    r"foster(s|ing|ed)?", r"leverag(e|es|ing|ed)", r"empower(s|ing|ed)?",
    r"streamlin(e|es|ing|ed)", r"robust", r"tapestry", r"realm", r"beacon",
    r"paramount", r"elevat(e|es|ing|ed)", r"embark(s|ing|ed)?",
    r"harness(es|ing|ed)?", r"testament",
]
# Meta-frames: announcing that a fact is interesting instead of writing the fact.
FRAMES = [
    r"the (part|thing|bit|detail) (worth (noticing|noting|watching)|to watch)",
    r"worth noticing", r"the interesting (part|bit|thing)",
    r"what.s (interesting|notable|striking) here", r"here.s the thing",
    r"it.s telling that",
]

SLOP_ANY_RE = re.compile(r"\b(?:" + "|".join(SLOP_ANY) + r")\b", re.I)
SLOP_LOWER_RE = re.compile(r"\b(?:" + "|".join(SLOP_LOWER) + r")\b", re.I)
FRAME_RE = re.compile(r"\b(?:" + "|".join(FRAMES) + r")\b", re.I)
SENTENCE_START = re.compile(r"(?:^|[.!?:]\s+|\n\s*)$")
# Straight and curly quote pairs — quoted material is exempt from linting.
QUOTE_SPAN = re.compile(r'[“"][^”"]{0,600}[”"]')


def find_banned(text: str) -> list[str]:
    """Return sorted banned words/frames in text, skipping quoted spans and
    proper-noun capitalisations."""
    if not text:
        return []
    prose = QUOTE_SPAN.sub(" ", text)
    hits = {m.group(0).lower() for m in SLOP_ANY_RE.finditer(prose)}
    hits |= {m.group(0).lower() for m in FRAME_RE.finditer(prose)}
    for m in SLOP_LOWER_RE.finditer(prose):
        w = m.group(0)
        if w[0].islower() or SENTENCE_START.search(prose[max(0, m.start() - 40):m.start()]):
            hits.add(w.lower())
    return sorted(hits)


def lint_brief(brief_data: dict) -> list[dict]:
    """Lint every prose field in the brief. Each violation carries a direct
    reference to the containing dict so the caller can patch it in place:
    {"label": "world/story-1/summary", "obj": story, "key": "summary", "phrases": [...]}
    """
    violations = []

    def check(obj, key, label):
        phrases = find_banned(obj.get(key) or "")
        if phrases:
            violations.append({"label": label, "obj": obj, "key": key, "phrases": phrases})

    check(brief_data, "summary", "summary")
    for section in brief_data.get("sections", []):
        for i, story in enumerate(section.get("stories", [])):
            prefix = f"{section.get('id', '?')}/story-{i + 1}"
            check(story, "headline", f"{prefix}/headline")
            check(story, "summary", f"{prefix}/summary")
            check(story, "why_it_matters", f"{prefix}/why_it_matters")
    for i, story in enumerate((brief_data.get("explore") or {}).get("stories", [])):
        check(story, "headline", f"explore/story-{i + 1}/headline")
        check(story, "summary", f"explore/story-{i + 1}/summary")
    for i, corr in enumerate(brief_data.get("corrections") or []):
        check(corr, "correction", f"corrections/{i + 1}")
    return violations


def prompt_banned_examples() -> str:
    """A plain-language rendering of the banned list for the synthesis prompt."""
    return (
        "delve, utilize, facilitate, leverage, foster, empower, streamline, "
        "robust, harness, elevate, embark, showcase, underscore, testament, "
        "cutting-edge, game-changing, transformative, multifaceted, meticulous, "
        "intricate, paradigm shift, pivotal moment, vital role, ever-evolving, "
        "\"it's worth noting\", \"it's important to note\", \"when it comes to\", "
        "\"at its core\", \"going forward\", \"here's the thing\", "
        "\"what's interesting here\", or any phrase that announces a fact is "
        "interesting instead of stating the fact"
    )


# ── Word budget ────────────────────────────────────────────────────────────

def count_story_words(story: dict) -> int:
    text = " ".join(
        story.get(k) or "" for k in ("headline", "summary", "why_it_matters")
    )
    return len(text.split())


def section_word_counts(brief_data: dict) -> dict[str, int]:
    counts = {}
    for section in brief_data.get("sections", []):
        counts[section["id"]] = sum(count_story_words(s) for s in section.get("stories", []))
    explore = brief_data.get("explore") or {}
    counts["explore"] = sum(count_story_words(s) for s in explore.get("stories", []))
    return counts


def compute_word_targets(weights: dict[str, int], explore_weight: int, ceiling: int) -> dict[str, int]:
    """Allocate the word ceiling across sections proportionally to weight."""
    total = sum(weights.values()) + explore_weight
    targets = {cid: round(w / total * ceiling) for cid, w in weights.items()}
    targets["explore"] = round(explore_weight / total * ceiling)
    return targets


def enforce_word_budget(brief_data: dict, targets: dict[str, int], ceiling: int,
                        min_stories: int = 2, min_explore: int = 1) -> list[str]:
    """Drop whole stories, most-over-target section first, until under ceiling.

    Stories arrive ranked most-important-first, so the last story in a section
    is always the one cut. Never takes a news section below min_stories or
    explore below min_explore. Returns log lines describing each drop.
    """
    log = []
    while True:
        counts = section_word_counts(brief_data)
        total = sum(counts.values()) + len((brief_data.get("summary") or "").split())
        if total <= ceiling:
            break
        candidates = []
        for section in brief_data.get("sections", []):
            if len(section.get("stories", [])) > min_stories:
                overage = counts[section["id"]] - targets.get(section["id"], 0)
                candidates.append((overage, section["id"], section))
        explore = brief_data.get("explore") or {}
        if len(explore.get("stories", [])) > min_explore:
            overage = counts["explore"] - targets.get("explore", 0)
            candidates.append((overage, "explore", explore))
        if not candidates:
            log.append(f"still {total - ceiling} words over ceiling but every section is at its story floor")
            break
        candidates.sort(key=lambda c: c[0], reverse=True)
        _, sec_id, container = candidates[0]
        dropped = container["stories"].pop()
        log.append(
            f"BUDGET [{sec_id}] dropped \"{dropped.get('headline', '?')}\" "
            f"({count_story_words(dropped)} words; page was {total}/{ceiling})"
        )
    return log


# ── Placeholder filter ─────────────────────────────────────────────────────

# Headlines that announce an absence of news instead of reporting news.
# Anchored to the start of the headline — real headlines occasionally begin
# with "No" ("No survivors found...") but not with these shapes.
PLACEHOLDER_RE = re.compile(
    r"^\s*no\s+(?:[\w-]+\s+){0,3}(?:stories|story|coverage|news|developments|updates|results?|features|items)\b"
    r"|^\s*no\s+[\w\s-]{0,40}\bavailable\b"
    r"|^\s*nothing\s+(?:new\s+)?to\s+report\b",
    re.I,
)


def filter_placeholders(brief_data: dict) -> list[str]:
    """Remove placeholder 'no news available' stories entirely.

    Unlike the vague-source filter, this never keeps a placeholder to avoid
    an empty section — an honestly empty section beats a fake story card.
    """
    log = []
    for section in brief_data.get("sections", []):
        kept = []
        for story in section.get("stories", []):
            if PLACEHOLDER_RE.search(story.get("headline", "")):
                log.append(f"PLACEHOLDER [{section['id']}] \"{story.get('headline', '?')}\"")
            else:
                kept.append(story)
        section["stories"] = kept
    explore = brief_data.get("explore") or {}
    kept = []
    for story in explore.get("stories", []):
        if PLACEHOLDER_RE.search(story.get("headline", "")):
            log.append(f"PLACEHOLDER [explore] \"{story.get('headline', '?')}\"")
        else:
            kept.append(story)
    if explore:
        explore["stories"] = kept
    return log


# ── Coverage log ───────────────────────────────────────────────────────────

STOP_WORDS = frozenset(
    "a an the and or but in on for to of is are was were with from by at as "
    "its it that this be has have had not no been will set new says said after "
    "over into about up out may could how what when where who why".split()
)


def headline_keywords(headline: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", headline.lower()))
    return words - STOP_WORDS


def headline_similarity(a: str, b: str) -> float:
    kw_a, kw_b = headline_keywords(a), headline_keywords(b)
    if not kw_a or not kw_b:
        return 0.0
    return len(kw_a & kw_b) / len(kw_a | kw_b)


def load_coverage_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("entries", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def save_coverage_log(path: Path, entries: list[dict], today: datetime.date,
                      retention_days: int = 30) -> None:
    cutoff = (today - datetime.timedelta(days=retention_days)).isoformat()
    kept = [e for e in entries if e.get("date", "") >= cutoff]
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"entries": kept}, indent=2))


def entries_since(entries: list[dict], today: datetime.date, days: int) -> list[dict]:
    cutoff = (today - datetime.timedelta(days=days)).isoformat()
    return [e for e in entries if e.get("date", "") >= cutoff]


def record_coverage(brief_data: dict, entries: list[dict], today: datetime.date) -> list[dict]:
    """Append every story the brief shipped today. Idempotent for re-runs."""
    date_iso = today.isoformat()
    seen = {(e["date"], e["headline"]) for e in entries}
    for section in brief_data.get("sections", []):
        for story in section.get("stories", []):
            headline = story.get("headline", "")
            if not headline or (date_iso, headline) in seen:
                continue
            entries.append({
                "date": date_iso,
                "section": section["id"],
                "headline": headline,
                "summary": story.get("summary", ""),
                "urls": [s["url"] for s in story.get("sources", []) if s.get("url")],
            })
    for story in (brief_data.get("explore") or {}).get("stories", []):
        headline = story.get("headline", "")
        if not headline or (date_iso, headline) in seen:
            continue
        entries.append({
            "date": date_iso,
            "section": "explore",
            "headline": headline,
            "summary": story.get("summary", ""),
            "urls": [story["source_url"]] if story.get("source_url") else [],
        })
    return entries


def filter_repeats(brief_data: dict, recent_entries: list[dict],
                   threshold: float = 0.65, min_stories: int = 2) -> list[str]:
    """Remove stories that near-duplicate a headline already shipped recently.

    The threshold is deliberately high: a genuine follow-up gets a headline
    about the new development and survives; only a re-run of the same story
    scores this close. Never takes a section below min_stories.
    """
    log = []
    for section in brief_data.get("sections", []):
        stories = section.get("stories", [])
        flagged = []
        for story in stories:
            for entry in recent_entries:
                sim = headline_similarity(story.get("headline", ""), entry.get("headline", ""))
                if sim >= threshold:
                    flagged.append((story, entry, sim))
                    break
        allowed = max(0, len(stories) - min_stories)
        for story, entry, sim in flagged[:allowed]:
            stories.remove(story)
            log.append(
                f"REPEAT [{section['id']}] \"{story.get('headline', '?')}\" "
                f"~ \"{entry['headline']}\" from {entry['date']} ({sim:.0%})"
            )
        for story, entry, sim in flagged[allowed:]:
            log.append(
                f"REPEAT-KEPT [{section['id']}] \"{story.get('headline', '?')}\" "
                f"(kept — section would fall below {min_stories} stories)"
            )
    return log

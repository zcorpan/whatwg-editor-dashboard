"""`dashboard.py evaluate`: how the rules do on real data, to set thresholds by.

Nothing here is published. It reports the distribution of whose move the open items
are, how routing spreads, how many items have any topic at all, and the precision of
the page -> topic-label suggestion, measured leave-one-out over labeled history: each
labeled merged PR is predicted from everybody else's.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .config import DashboardConfig, label_key
from .expertise import contributions
from .github import RepositoryData
from .models import HistoryRecord
from .spec_pages import SpecPages, split_location
from .topics import LabelSuggester, display_name, page_shares, top_page, topic_labels


def label_suggestion_precision(data: RepositoryData, spec: SpecPages, config: DashboardConfig) -> dict[str, Any]:
    """How often the `topics:` map's label for a merged PR's main page is one it carried."""
    by_commit = spec.by_commit or {}
    suggester = LabelSuggester.from_config(
        config, {label for record in data.history for label in (*record.labels, *record.issue_labels)}
    )
    labeled = suggested = correct = 0
    for record in data.history:
        labels = set(topic_labels((*record.labels, *record.issue_labels), config))
        pages = by_commit.get(record.merge_oid or "")
        if not labels or not pages:
            continue
        labeled += 1
        suggestion = suggester.from_pages(pages, config)
        if suggestion is not None:
            suggested += 1
            correct += label_key(suggestion[0]) in labels
    return {
        "labeled_with_pages": labeled,
        "suggested": suggested,
        "correct": correct,
        "precision_percent": round(100 * correct / suggested, 1) if suggested else None,
        "coverage_percent": round(100 * suggested / labeled, 1) if labeled else None,
    }


def unknown_sections(index: Any, config: DashboardConfig) -> list[str]:
    """`page#section` entries in `topics:` that match no heading in the current source."""
    known = set()
    for location in index.by_heading.values():
        page, sections = split_location(location)
        known.update(f"{page}#{section}" for section in sections)
    return sorted(entry for _label, entries in config.topic_pages for entry in entries if "#" in entry and entry not in known)


def uncovered_pages(data: RepositoryData, spec: SpecPages, config: DashboardConfig) -> list[tuple[str, int]]:
    """Pages merged PRs are mostly about that no label in `topics:` covers, busiest first."""
    counts: Counter[str] = Counter()
    for record in data.history:
        page = top_page(page_shares((spec.by_commit or {}).get(record.merge_oid or ""), config))
        if page and not config.labels_for_page(page):
            counts[page] += 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def report(payload: dict[str, Any], data: RepositoryData, spec: SpecPages, config: DashboardConfig) -> list[str]:
    items = payload["items"]
    lines = []
    parties = Counter((item.get("kind", "pr"), item["waiting_on"]["party"]) for item in items)
    lines.append("Whose move (published items): " + ", ".join(f"{kind} {party} {count}" for (kind, party), count in sorted(parties.items())))
    owners = Counter(item["routing"]["owner"] for item in items if item.get("routing"))
    lines.append("Routed now: " + (", ".join(f"@{login} {count}" for login, count in sorted(owners.items())) or "none"))
    topical = sum(bool(item.get("labels") and topic_labels(item["labels"], config)) for item in items)
    lines.append(f"Items with a topic label: {topical} of {len(items)}")
    if spec.by_pr is not None:
        open_prs = [item for item in items if item.get("kind") == "pr"]
        mapped = sum(bool(page_shares((spec.by_pr or {}).get(item["number"]), config)) for item in open_prs)
        lines.append(f"Open PRs with a spec page: {mapped} of {len(open_prs)}")
        mapped_history = sum(bool(page_shares((spec.by_commit or {}).get(record.merge_oid or ""), config)) for record in data.history)
        lines.append(f"Merged PRs with a spec page: {mapped_history} of {len(data.history)}")
        precision = label_suggestion_precision(data, spec, config)
        if spec.index is not None:
            missing = unknown_sections(spec.index, config)
            lines.append("Sections in topics: that no heading matches: " + (", ".join(missing) or "none"))
        gaps = uncovered_pages(data, spec, config)
        lines.append(
            "Pages no label covers (merged PRs mostly about them): "
            + (", ".join(f"{page} {count}" for page, count in gaps[:15]) or "none")
        )
        lines.append(
            "Topic label from the topics: map, against merged PRs' own labels: "
            f"{precision['correct']} of {precision['suggested']} right ({precision['precision_percent']}%), "
            f"suggested for {precision['coverage_percent']}% of {precision['labeled_with_pages']} labeled PRs"
        )
    suggestions = Counter(suggestion["label"] for item in items for suggestion in item.get("suggested_labels") or [])
    lines.append("Suggested labels: " + (", ".join(f"{label} {count}" for label, count in suggestions.most_common(12)) or "none"))
    teams = Counter(suggestion["team"] for item in items for suggestion in item.get("suggested_teams") or [])
    lines.append("Suggested teams: " + (", ".join(f"{team} {count}" for team, count in teams.most_common()) or "none"))
    return lines


def expertise_profiles(
    history: list[HistoryRecord],
    pages_by_oid: dict[str, dict[str, int]],
    config: DashboardConfig,
    *,
    as_of: datetime,
) -> dict[str, list[tuple[str, float, float]]]:
    """Each login's topics as (topic key, decayed score, share of everyone's), best first."""
    half_life = config.expertise.half_life_days * 86400
    scores: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    totals: dict[str, float] = defaultdict(float)
    for contribution in contributions(history, pages_by_oid, config):
        if contribution.at > as_of:
            continue
        decay = 0.5 ** ((as_of - contribution.at).total_seconds() / half_life)
        for topic, weight in contribution.topics.items():
            value = contribution.weight * weight * decay
            scores[contribution.login][topic] += value
            totals[topic] += value
    return {
        login: sorted(
            ((topic, value, value / totals[topic]) for topic, value in topics.items()),
            key=lambda entry: (-entry[1], entry[0]),
        )
        for login, topics in scores.items()
    }


def knows_entry(topic: str) -> str:
    """A topic key as it is written in `knows:`."""
    kind, _, name = topic.partition(":")
    return name


def suggested_knows(profile: list[tuple[str, float, float]], *, top: int, min_share: float) -> list[str]:
    return sorted(knows_entry(topic) for topic, _score, share in profile[:top] if share >= min_share)


def expertise_report(
    history: list[HistoryRecord],
    pages_by_oid: dict[str, dict[str, int]],
    config: DashboardConfig,
    *,
    as_of: datetime,
    top: int = 8,
    min_share: float = 0.1,
) -> list[str]:
    """Who knows what according to merged-PR history, against what `knows:` declares.

    An aid for editing the declarations, never read by the build: history misses
    design done elsewhere, and follows volume.
    """
    profiles = expertise_profiles(history, pages_by_oid, config, as_of=as_of)
    lines = [
        f"Merged PRs since {config.expertise.since}, credit halving every {config.expertise.half_life_days} days, "
        f"as of {as_of.date().isoformat()}. Share is of everyone's credit on the topic."
    ]
    snippet = []
    for login in sorted(config.people):
        profile = profiles.get(login, [])
        role = "helper" if login in config.helpers else "retired editor" if login in config.retired_editors else "editor"
        lines.append(f"\n@{login} ({role}), total {sum(value for _topic, value, _share in profile):.0f}")
        for topic, value, share in profile[:top]:
            lines.append(f"  {display_name(topic):40} {value:6.1f}  {100 * share:3.0f}%")
        suggestion = suggested_knows(profile, top=top, min_share=min_share)
        declared = sorted(knows_entry(topic) for topic in config.knows_of(login))
        missing = [entry for entry in suggestion if entry.lower() not in {value.lower() for value in declared}]
        unsupported = [entry for entry in declared if entry.lower() not in {value.lower() for value in suggestion}]
        if missing:
            lines.append("  History suggests, not declared: " + ", ".join(missing))
        if unsupported:
            lines.append("  Declared, not in the history's top topics: " + ", ".join(unsupported))
        # One entry per line, as in dashboard.yml, so edits diff line by line.
        snippet.append(
            f"  - login: {login}\n    knows:" + "".join(f"\n      - {json.dumps(entry)}" for entry in suggestion)
        )
    lines.append("\nSuggested knows: lists (merge into the editors: and helpers: entries):\n" + "\n".join(snippet))
    uncovered: Counter[str] = Counter()
    for record in history:
        page = top_page(page_shares(pages_by_oid.get(record.merge_oid or ""), config))
        if page and not config.labels_for_page(page):
            uncovered[page] += 1
    if uncovered:
        lines.append(
            "\nPages no label in topics: covers, by merged PRs mostly about them (a label for these makes them declarable):\n  "
            + ", ".join(f"{page} {count}" for page, count in sorted(uncovered.items(), key=lambda pair: (-pair[1], pair[0])))
        )
    return lines

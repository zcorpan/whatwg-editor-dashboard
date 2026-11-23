"""`dashboard.py evaluate`: how the rules do on real data, to set thresholds by.

Nothing here is published. It reports the distribution of whose move the open items
are, how routing spreads, how many items have any topic at all, and the precision of
the page -> topic-label suggestion, measured leave-one-out over labeled history: each
labeled merged PR is predicted from everybody else's.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .config import DashboardConfig
from .github import RepositoryData
from .spec_pages import SpecPages
from .topics import LabelSuggester, page_shares, topic_labels, _top_page


def label_suggestion_precision(data: RepositoryData, spec: SpecPages, config: DashboardConfig) -> dict[str, Any]:
    by_commit = spec.by_commit or {}
    examples = [
        (tuple(topic_labels((*record.labels, *record.issue_labels), config)), by_commit.get(record.merge_oid or ""))
        for record in data.history
    ]
    labeled = [(labels, pages) for labels, pages in examples if labels and pages]
    full = LabelSuggester.learn(labeled, config, ())
    suggested = correct = 0
    for labels, pages in labeled:
        top = _top_page(page_shares(pages, config))
        if top is None:
            continue
        # Leave this example out of the counts it is judged by.
        counts = Counter(full.by_page.get(top, {}))
        for label in labels:
            counts[label] -= 1
        held_out = LabelSuggester({top: +counts}, ())
        suggestion = held_out.from_pages(pages, config)
        if suggestion is not None:
            suggested += 1
            correct += suggestion[0] in labels
    return {
        "labeled_with_pages": len(labeled),
        "suggested": suggested,
        "correct": correct,
        "precision_percent": round(100 * correct / suggested, 1) if suggested else None,
        "coverage_percent": round(100 * suggested / len(labeled), 1) if labeled else None,
    }


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
        lines.append(
            "Topic label from spec page, leave-one-out: "
            f"{precision['correct']} of {precision['suggested']} right ({precision['precision_percent']}%), "
            f"suggested for {precision['coverage_percent']}% of {precision['labeled_with_pages']} labeled PRs"
        )
    suggestions = Counter(suggestion["label"] for item in items for suggestion in item.get("suggested_labels") or [])
    lines.append("Suggested labels: " + (", ".join(f"{label} {count}" for label, count in suggestions.most_common(12)) or "none"))
    teams = Counter(suggestion["team"] for item in items for suggestion in item.get("suggested_teams") or [])
    lines.append("Suggested teams: " + (", ".join(f"{team} {count}" for team, count in teams.most_common()) or "none"))
    return lines

"""Routing, topic-label and team suggestions, applied to the published items.

This runs after every PR and issue has been analysed, because routing is a walk over
all of them. It reads the analyses and the snapshots, and writes only into the public
item dictionaries and the per-perspective `routed` lane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from .analysis import ALL_EDITORS, PRAnalysis, is_bot
from .config import DashboardConfig
from .issue_analysis import IssueAnalysis
from .models import HistoryRecord
from .routing import Candidate, Routing, route, suggest_teams
from .spec_pages import SpecPages
from .topics import LabelSuggester, item_topics, topic_labels


_TEAM_MENTION_RE = re.compile(r"@whatwg/([a-z0-9-]+)", re.IGNORECASE)


def _mentioned_teams(texts: Iterable[str]) -> frozenset[str]:
    return frozenset(match.lower() for text in texts for match in _TEAM_MENTION_RE.findall(text or ""))


@dataclass(frozen=True)
class Assignments:
    decisions: dict[str, Routing]
    candidates: dict[str, Candidate]


def _contributor(author: str | None, config: DashboardConfig) -> bool:
    return bool(author) and author not in config.people and not is_bot(author)


def apply_assignments(
    *,
    items: dict[str, dict[str, Any]],
    open_prs: Iterable[PRAnalysis],
    closed_prs: Iterable[PRAnalysis],
    open_issues: Iterable[IssueAnalysis],
    closed_issues: Iterable[IssueAnalysis],
    history: Iterable[HistoryRecord],
    spec: SpecPages,
    perspective_lanes: dict[str, dict[str, list[str]]],
    config: DashboardConfig,
    now: datetime,
) -> Assignments:
    """Route every recent contributor item, then annotate the published ones.

    The walk covers open and recently closed items, published or not, so that a
    decision depends only on what came before it and never on what has been answered
    since. Only items that are still unanswered show their routing.
    """
    slug = config.repository.slug
    history = list(history)
    by_commit = spec.by_commit or {}
    by_pr = spec.by_pr or {}
    merge_oids = {record.number: record.merge_oid for record in history}
    recent = now - timedelta(days=config.sampling.history_days)

    known_labels = {label for record in history for label in (*record.labels, *record.issue_labels)}
    candidates: dict[str, Candidate] = {}
    pages_of: dict[str, dict[str, int] | None] = {}
    titles: dict[str, str] = {}

    def add(key: str, candidate: Candidate, pages: dict[str, int] | None, title: str) -> None:
        candidates[key] = candidate
        pages_of[key] = pages
        titles[key] = title
        known_labels.update(candidate.labels)

    for analysis in (*open_prs, *closed_prs):
        pr = analysis.pr
        key = f"{slug}#{pr.number}"
        linked_labels = [label for issue in analysis.linked_issues for label in issue.labels]
        pages = by_pr.get(pr.number) if pr.closed_at is None else by_commit.get(merge_oids.get(pr.number) or "")
        unowned = pr.closed_at is None and _unowned(analysis)
        add(
            key,
            Candidate(
                key=key,
                kind="pr",
                ready_at=pr.ready_for_review_at,
                topics=item_topics((*pr.labels, *linked_labels), config, pages=pages),
                labels=tuple(sorted({*pr.labels, *linked_labels})),
                mentioned_teams=_mentioned_teams([pr.body, *(activity.body for activity in pr.timeline)]),
                unowned=unowned,
                routable=_contributor(pr.author, config) and (pr.ready_for_review_at >= recent or unowned),
            ),
            pages,
            pr.title,
        )
    for analysis in (*open_issues, *closed_issues):
        issue = analysis.issue
        key = f"{slug}#{issue.number}"
        linked = spec.link_pages(f"{issue.title}\n{issue.body}")
        unowned = issue.is_open and _unowned(analysis)
        add(
            key,
            Candidate(
                key=key,
                kind="issue",
                ready_at=issue.created_at,
                topics=item_topics(issue.labels, config, linked_pages=linked),
                labels=issue.labels,
                mentioned_teams=_mentioned_teams([issue.body, *(activity.body for activity in issue.timeline)]),
                unowned=unowned,
                routable=_contributor(issue.author, config) and (issue.created_at >= recent or unowned),
            ),
            {page: 1 for page in linked} or None,
            issue.title,
        )

    suggester = LabelSuggester.from_config(config, known_labels)

    decisions = route(candidates.values(), config)
    for key, decision in decisions.items():
        item = items.get(key)
        if item is None or not candidates[key].unowned:
            continue
        item["routing"] = decision.to_public_dict()
        waiting = item["waiting_on"]
        waiting["who"] = [decision.owner]
        waiting["reason"] = {**waiting["reason"], "label": f"Waiting on @{decision.owner}", "detail": "routed"}
        for perspective in (decision.owner, ALL_EDITORS):
            perspective_lanes[perspective]["routed"].append(key)
            lanes = item["perspectives"][perspective]["lanes"]
            if "routed" not in lanes:
                lanes.append("routed")

    # Oldest first: these are first responses, and fairness to the waiting
    # contributor is what the response target measures.
    for lanes in perspective_lanes.values():
        lanes["routed"].sort(key=lambda key: (candidates[key].ready_at, key))

    for key, item in items.items():
        candidate = candidates.get(key)
        if candidate is None:
            continue
        if not topic_labels(candidate.labels, config):
            suggestion = suggester.from_pages(pages_of.get(key), config)
            if suggestion is None and candidate.kind == "issue":
                suggestion = suggester.from_title(titles[key])
            if suggestion is not None:
                label, reason = suggestion
                item.setdefault("suggested_labels", []).append({"label": label, "reason": reason})
        item["suggested_teams"] = [
            suggestion.to_public_dict() for suggestion in suggest_teams(candidate, config)
        ]
    return Assignments(decisions=decisions, candidates=candidates)


def _unowned(analysis: PRAnalysis | IssueAnalysis) -> bool:
    """Never answered, and nobody named: waiting for somebody to pick it up."""
    lanes = analysis.shared_lanes
    return (
        ("reply_window" in lanes or "overdue" in lanes)
        and analysis.waiting_on.party == "editors"
        and not analysis.waiting_on.who
    )

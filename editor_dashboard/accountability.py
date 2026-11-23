"""Who did what, week by week: the team section of the health view.

Every figure is a count of public events attributed to one login, so it describes
event order and never causation: "first replies" are the first response on an item,
not the reason it moved. Each row also states what is waiting on that person now,
and how the items routed to them fared against the response target.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from .analysis import PRAnalysis, is_bot
from .config import DashboardConfig
from .issue_analysis import IssueAnalysis
from .models import HistoryRecord, isoformat
from .routing import Candidate, Routing


_EDITORIAL_PREFIXES = ("editorial:", "meta:")


def _week_index(at: datetime, starts: list[datetime]) -> int | None:
    for index in range(len(starts) - 1, -1, -1):
        if at >= starts[index]:
            return index
    return None


def build_people(
    *,
    prs: Iterable[PRAnalysis],
    issues: Iterable[IssueAnalysis],
    history: Iterable[HistoryRecord],
    items: Iterable[dict[str, Any]],
    decisions: dict[str, Routing],
    candidates: dict[str, Candidate],
    config: DashboardConfig,
    now: datetime,
    window_days: int,
    bucket_days: int,
) -> dict[str, Any]:
    since = now - timedelta(days=window_days)
    starts = [since + timedelta(days=bucket_days * index) for index in range(window_days // bucket_days)]
    slug = config.repository.slug
    target = timedelta(days=config.response_targets.initial_editor_response_days)
    weekly: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0] * len(starts)))
    reviewed_prs: dict[str, set[int]] = defaultdict(set)

    def count(login: str | None, measure: str, at: datetime | None) -> None:
        if not login or is_bot(login) or at is None or at < since or at > now:
            return
        index = _week_index(at, starts)
        if index is not None:
            weekly[login][measure][index] += 1

    pr_values = list(prs)
    issue_values = list(issues)
    for analysis in pr_values:
        pr = analysis.pr
        for review in pr.reviews:
            if review.author and review.author != pr.author and review.state != "PENDING":
                count(review.author, "reviews", review.created_at)
                if review.created_at >= since:
                    reviewed_prs[review.author].add(pr.number)
        response = analysis.first_editor_response
        if response is not None and analysis.first_editor_response_known and pr.author not in config.editors:
            count(response.author, "first_replies", response.created_at)
    for analysis in issue_values:
        response = analysis.first_response
        if response is not None and analysis.first_response_known and analysis.issue.author not in config.editors:
            count(response.author, "first_replies", response.created_at)
            if response.kind == "label":
                count(response.author, "triaged", response.created_at)
        if analysis.issue.closed_at is not None and analysis.issue.closed_by != analysis.issue.author:
            count(analysis.issue.closed_by, "issues_closed", analysis.issue.closed_at)
    for record in history:
        count(record.merged_by, "merged", record.merged_at)
        if not record.title.lower().startswith(_EDITORIAL_PREFIXES):
            count(record.author, "authored_merged", record.merged_at)

    # Routed to each person, from the routing start, once the target has had time to pass.
    routed: dict[str, dict[str, int]] = defaultdict(lambda: {"routed": 0, "answered_in_time": 0, "answered_by_them": 0})
    start = datetime.fromisoformat(config.routing.start).replace(tzinfo=now.tzinfo)
    responses: dict[str, tuple[datetime | None, str | None]] = {}
    for analysis in pr_values:
        response = analysis.first_editor_response
        responses[f"{slug}#{analysis.number}"] = (
            (response.created_at, response.author) if response and analysis.first_editor_response_known else (None, None)
        )
    for analysis in issue_values:
        response = analysis.first_response
        responses[f"{slug}#{analysis.number}"] = (
            (response.created_at, response.author) if response and analysis.first_response_known else (None, None)
        )
    for key, decision in decisions.items():
        ready = candidates[key].ready_at
        if ready < start or ready > now - target:
            continue
        tally = routed[decision.owner]
        tally["routed"] += 1
        at, author = responses.get(key, (None, None))
        if at is not None and at - ready <= target:
            tally["answered_in_time"] += 1
            if author == decision.owner:
                tally["answered_by_them"] += 1

    # What is on each person's plate now: the published items whose next move names them.
    waiting: dict[str, list[datetime]] = defaultdict(list)
    for item in items:
        move = item.get("waiting_on") or {}
        if move.get("party") not in {"editors", "merge"}:
            continue
        since_at = move.get("since")
        for login in move.get("who") or []:
            if since_at:
                waiting[login].append(datetime.fromisoformat(since_at.replace("Z", "+00:00")))

    contributors = sorted(
        login
        for login, numbers in reviewed_prs.items()
        if login not in config.people and len(numbers) >= config.min_reviews_of_others
    )
    active_retired = sorted(login for login in config.retired_editors if any(sum(values) for values in weekly[login].values()))
    rows = []
    for login, role in (
        *((login, "editor") for login in sorted(config.editors - config.retired_editors)),
        *((login, "retired editor") for login in active_retired),
        *((login, "helper") for login in sorted(config.helpers)),
        *((login, "contributor") for login in contributors),
    ):
        measures = weekly[login]
        plate = sorted(waiting.get(login, []))
        rows.append(
            {
                "login": login,
                "role": role,
                "weekly": {
                    measure: list(measures[measure])
                    for measure in ("reviews", "first_replies", "triaged", "merged", "authored_merged", "issues_closed")
                },
                "totals": {
                    measure: sum(measures[measure])
                    for measure in ("reviews", "first_replies", "triaged", "merged", "authored_merged", "issues_closed")
                },
                "prs_reviewed": len(reviewed_prs.get(login, ())),
                "routed": dict(routed.get(login, {"routed": 0, "answered_in_time": 0, "answered_by_them": 0})),
                "waiting_now": len(plate),
                "waiting_oldest_since": isoformat(plate[0]) if plate else None,
                # Why a contributor is listed at all.
                "qualified_by": (
                    f"reviewed {len(reviewed_prs[login])} other people's PRs in {window_days // 7} weeks"
                    if role == "contributor"
                    else None
                ),
            }
        )
    return {
        "window_days": window_days,
        "bucket_days": bucket_days,
        "week_starts": [isoformat(value) for value in starts],
        "routing_start": config.routing.start,
        "min_reviews_of_others": config.min_reviews_of_others,
        "rows": rows,
    }

"""Issues, read with the same rules as pull requests wherever the meaning carries over.

whatwg/html has thousands of open issues, most of them dormant feature requests, so
the lanes that measure waiting only look at issues inside `issues.window_days`.
Without that bound the first-response pair and the longest waits would be a decade of
backlog. An issue an open PR says it fixes is folded into that PR's card instead of
getting one of its own: the PR is where the work is.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .analysis import (
    ALL_EDITORS,
    LinkedIssue,
    PerspectiveView,
    Reason,
    SuggestedLabel,
    WaitingOn,
    _PARTY_LABELS,
    _PARTY_TONES,
    _attention_fingerprint,
    _hash_payload,
    _hours_between,
    _mention_pattern,
    _response_target_hours,
    is_bot,
    perspective_keys,
)
from .config import DashboardConfig
from .models import IssueSnapshot, isoformat


ISSUE_PERSPECTIVE_LANES = ("active", "direct", "stale_direct")
ISSUE_SHARED_LANES = ("reply_window", "overdue", "oldest_wait", "triage", "all")

_ISSUE_PARTY_LABELS = {
    **_PARTY_LABELS,
    "author": "Waiting on the discussion",
    "proposal": "Waiting on a concrete proposal",
    "fix": "A fix is proposed",
}


@dataclass(frozen=True)
class FirstResponse:
    author: str
    created_at: datetime
    kind: str  # "comment", "label" or "close"


@dataclass(frozen=True)
class IssueAnalysis:
    issue: IssueSnapshot
    shared_lanes: tuple[str, ...]
    shared_reasons: tuple[Reason, ...]
    perspectives: dict[str, PerspectiveView]
    waiting_on: WaitingOn
    first_response: FirstResponse | None
    first_response_known: bool
    first_response_hours: float | None
    latest_contributor_activity_at: datetime | None
    current_wait_hours: float | None
    age_hours: float
    first_time_contributor: bool
    suggested_labels: tuple[SuggestedLabel, ...] = ()

    kind = "issue"

    @property
    def number(self) -> int:
        return self.issue.number

    @property
    def updated_at(self) -> datetime:
        return self.issue.updated_at

    @property
    def ready_at(self) -> datetime:
        return self.issue.created_at

    @property
    def claimed_by(self) -> tuple[int, ...]:
        return self.issue.fixed_by

    def lanes_for(self, perspective: str) -> tuple[str, ...]:
        return (*self.perspectives[perspective].lanes, *self.shared_lanes)

    def in_any_lane(self) -> bool:
        if self.claimed_by:
            return False
        if any(lane != "all" for lane in self.shared_lanes):
            return True
        return any(view.lanes for view in self.perspectives.values())

    def to_public_dict(self, repository_slug: str) -> dict[str, Any]:
        issue = self.issue
        return {
            "key": f"{repository_slug}#{issue.number}",
            "kind": "issue",
            "number": issue.number,
            "title": issue.title,
            "url": issue.url,
            "author": issue.author,
            "author_association": issue.author_association,
            "first_time_contributor": self.first_time_contributor,
            "created_at": isoformat(issue.created_at),
            "updated_at": isoformat(issue.updated_at),
            "age_hours": round(self.age_hours, 2),
            "labels": list(issue.labels),
            "assignees": list(issue.assignees),
            "comment_count": len(issue.timeline),
            "timeline_sample_complete": issue.timeline_sample_complete,
            "fixed_by": list(issue.fixed_by),
            "lanes": list(self.shared_lanes),
            "reasons": [reason.to_public_dict() for reason in self.shared_reasons],
            "perspectives": {key: view.to_public_dict() for key, view in self.perspectives.items()},
            "blockers": [],
            "latest_contributor_activity_at": isoformat(self.latest_contributor_activity_at),
            "current_wait_hours": round(self.current_wait_hours, 2) if self.current_wait_hours is not None else None,
            "first_editor_response_at": isoformat(self.first_response.created_at if self.first_response else None),
            "first_editor_response_by": self.first_response.author if self.first_response else None,
            "first_editor_response_kind": self.first_response.kind if self.first_response else None,
            "first_editor_response_known": self.first_response_known,
            "waiting_on": self.waiting_on.to_public_dict(),
            "suggested_labels": [suggestion.to_public_dict() for suggestion in self.suggested_labels],
        }


def _first_response(issue: IssueSnapshot, responders: frozenset[str]) -> tuple[FirstResponse | None, bool]:
    """The earliest comment, label or close by a responder other than the reporter.

    Applying a label is how most whatwg/html issues are triaged, so it is a response.
    """
    candidates: list[FirstResponse] = []
    first_comment = None
    for activity in issue.timeline:
        if activity.author in responders and activity.author != issue.author and not is_bot(activity.author):
            first_comment = activity
            candidates.append(FirstResponse(activity.author, activity.created_at, "comment"))
            break
    for event in issue.label_events:
        if event.actor in responders and event.actor != issue.author:
            candidates.append(FirstResponse(event.actor, event.created_at, "label"))
            break
    if issue.closed_at and issue.closed_by in responders and issue.closed_by != issue.author:
        candidates.append(FirstResponse(issue.closed_by, issue.closed_at, "close"))
    if not candidates:
        return None, issue.timeline_sample_complete
    first = min(candidates, key=lambda response: (response.created_at, response.kind))
    known = (
        first.kind != "comment"
        or issue.timeline_sample_complete
        or (first_comment is not None and first_comment.id in issue.timeline_first_ids)
    )
    return first, known


def _wait(party: str, who: Iterable[str] = (), since: datetime | None = None, detail: str | None = None) -> WaitingOn:
    who = tuple(who)
    label = _ISSUE_PARTY_LABELS[party]
    if party == "editors" and who:
        label = "Waiting on " + ", ".join(f"@{login}" for login in who)
    elif party == "fix" and who:
        label = "Fix proposed in " + ", ".join(f"#{number}" for number in who)
    tone = _PARTY_TONES.get(party, "muted")
    return WaitingOn(party, who, since, Reason(f"waiting-{party}", label, detail=detail, timestamp=since, tone=tone))


def _issue_waiting_on(issue: IssueSnapshot, config: DashboardConfig) -> WaitingOn:
    labels = {label.lower() for label in issue.labels}
    meeting = sorted(label for label in issue.labels if label.lower() in config.waiting_on.meeting_labels)
    if meeting:
        return _wait("meeting", detail=", ".join(meeting))
    for label, party in config.waiting_on.party_labels:
        if label in labels:
            return _wait(party, detail=label)
    if issue.fixed_by:
        return _wait("fix", [str(number) for number in issue.fixed_by])

    editors = config.editors
    editor_comments = [
        activity for activity in issue.timeline if activity.author in editors and not is_bot(activity.author)
    ]
    others = [
        activity
        for activity in issue.timeline
        if activity.author not in editors and not is_bot(activity.author)
    ]
    latest_other = max((activity.updated_at for activity in others), default=issue.created_at)
    if issue.author in editors and not others:
        latest_other = None
    if editor_comments:
        latest_editor = max(editor_comments, key=lambda activity: (activity.updated_at, activity.id))
        if latest_other is None or latest_editor.updated_at >= latest_other:
            return _wait("author", since=latest_editor.updated_at, detail=f"@{latest_editor.author} replied last")
    assigned = sorted(login for login in issue.assignees if login in editors)
    if assigned:
        who: Iterable[str] = assigned
    else:
        recent: dict[str, datetime] = {}
        for activity in editor_comments:
            if activity.updated_at > recent.get(activity.author, datetime.min.replace(tzinfo=timezone.utc)):
                recent[activity.author] = activity.updated_at
        who = sorted(
            (login for login in recent if login not in config.retired_editors),
            key=lambda login: (-recent[login].timestamp(), login),
        )
    return _wait("editors", who, since=latest_other)


def _issue_direct_reasons(
    issue: IssueSnapshot, config: DashboardConfig, *, editor: str, now: datetime
) -> tuple[list[Reason], list[Reason]]:
    """Assignment is current state and never expires; a mention is an event and does."""
    pattern = _mention_pattern(editor)
    cutoff = now - timedelta(days=config.attention.activity_window_days)
    # Older than the issue window, a mention on an issue is dropped rather than kept
    # as a stale one: on thousands of dormant issues, they were 400 cards of history.
    forgotten = now - timedelta(days=config.issues.window_days)
    active: list[Reason] = []
    expired: list[Reason] = []

    def record(reason: Reason, *, expires: bool) -> None:
        if expires and reason.timestamp is not None and reason.timestamp < forgotten:
            return
        if expires and reason.timestamp is not None and reason.timestamp < cutoff:
            expired.append(replace(reason, tone="muted"))
        else:
            active.append(reason)

    if editor in issue.assignees:
        record(Reason("assigned", f"Assigned to @{editor}", timestamp=issue.updated_at, tone="urgent"), expires=False)
    if issue.author != editor and pattern.search(issue.body):
        record(
            Reason(
                "mentioned-in-description",
                f"@{editor} mentioned in the description",
                timestamp=issue.last_edited_at or issue.created_at,
                tone="urgent",
            ),
            expires=True,
        )
    mentions = [
        activity
        for activity in issue.timeline
        if activity.author != editor and not is_bot(activity.author) and pattern.search(activity.body)
    ]
    if mentions:
        latest = max(mentions, key=lambda activity: (activity.updated_at, activity.id))
        record(
            Reason(
                "mentioned-in-discussion",
                f"@{editor} mentioned in the discussion",
                detail=f"by @{latest.author}" if latest.author else None,
                timestamp=latest.updated_at,
                tone="urgent",
            ),
            expires=True,
        )
    return active, expired


def _issue_content_fingerprint(issue: IssueSnapshot, editor: str) -> str:
    """As for PRs: the issue's public content, excluding ``editor``'s own footprint."""
    external = [activity for activity in issue.timeline if activity.author != editor]
    latest = external[-1] if external else None
    return _hash_payload(
        {
            "kind": "issue",
            "number": issue.number,
            "title": issue.title,
            "body_hash": hashlib.sha256(issue.body.encode("utf-8")).hexdigest(),
            "labels": issue.labels,
            "assignees": [login for login in issue.assignees if login != editor],
            "state": issue.state,
            "latest_activity": [latest.id, isoformat(latest.updated_at)] if latest else None,
            "fixed_by": issue.fixed_by,
        }
    )


def _issue_perspective(
    issue: IssueSnapshot,
    config: DashboardConfig,
    *,
    editor: str | None,
    direct: list[Reason],
    expired: list[Reason],
    involved: bool,
    waiting_on: WaitingOn,
    now: datetime,
) -> PerspectiveView:
    cutoff = now - timedelta(days=config.attention.activity_window_days)
    lanes: list[str] = []
    reasons: list[Reason] = []
    actionable = bool(direct) or waiting_on.party == "editors"
    if issue.updated_at >= cutoff and (involved or direct) and actionable and not issue.fixed_by:
        lanes.append("active")
        reasons.append(
            Reason(
                "recently-active",
                f"Active within {config.attention.activity_window_days} days",
                timestamp=issue.updated_at,
                tone="attention",
            )
        )
    if not issue.fixed_by:
        if direct:
            lanes.append("direct")
        elif expired and waiting_on.party == "editors":
            lanes.append("stale_direct")
    reasons.extend((*direct, *expired))

    def latest(values: list[Reason]) -> datetime | None:
        return max((reason.timestamp for reason in values if reason.timestamp), default=issue.updated_at) if values else None

    return PerspectiveView(
        lanes=tuple(lanes),
        reasons=tuple(reasons),
        direct_request_at=latest(direct),
        stale_direct_at=latest(expired),
        rereview_trigger_at=None,
        land_at=None,
        has_attention_signal=bool(direct),
        latest_review=None,
        content_fingerprint=_issue_content_fingerprint(issue, editor) if editor else None,
        attention_fingerprint=_attention_fingerprint((*direct, *expired)) if editor else None,
    )


def analyze_issue(
    issue: IssueSnapshot,
    config: DashboardConfig,
    *,
    now: datetime,
    open_pull_requests: frozenset[int] | None = None,
) -> IssueAnalysis:
    """``open_pull_requests`` limits which claimed fixes count: a draft is not on the
    dashboard, so an issue it claims keeps its own card."""
    now = now.astimezone(timezone.utc)
    if open_pull_requests is not None:
        issue = replace(issue, fixed_by=tuple(number for number in issue.fixed_by if number in open_pull_requests))
    editors = config.editors
    waiting_on = _issue_waiting_on(issue, config)

    signals: dict[str, tuple[list[Reason], list[Reason], bool]] = {}
    for editor in sorted(config.people):
        direct, expired = _issue_direct_reasons(issue, config, editor=editor, now=now)
        involved = any(activity.author == editor for activity in issue.timeline)
        signals[editor] = (direct, expired, involved)

    perspectives: dict[str, PerspectiveView] = {}
    for key in perspective_keys(config):
        if key == ALL_EDITORS:
            direct = [reason for editor in sorted(editors) for reason in signals[editor][0]]
            expired = [reason for editor in sorted(editors) for reason in signals[editor][1]]
            involved = any(signals[editor][2] for editor in editors)
        else:
            direct, expired, involved = signals[key]
        perspectives[key] = _issue_perspective(
            issue,
            config,
            editor=None if key == ALL_EDITORS else key,
            direct=direct,
            expired=expired,
            involved=involved,
            waiting_on=waiting_on,
            now=now,
        )

    # Helpers triage issues too, and that does answer the reporter; the response is
    # attributed to whoever gave it, so the metrics can still tell them apart.
    first, known = _first_response(issue, config.editors | config.helpers)
    first_hours = _hours_between(issue.created_at, first.created_at) if first and known else None
    age_hours = _hours_between(issue.created_at, now)
    window = timedelta(days=config.issues.window_days)
    contributor = issue.author not in editors and not is_bot(issue.author)

    contributor_times = [
        activity.updated_at
        for activity in issue.timeline
        if activity.author not in editors and not is_bot(activity.author)
    ]
    if contributor:
        contributor_times.append(issue.created_at)
    latest_contributor = max(contributor_times) if contributor_times else None
    waiting = waiting_on.party == "editors" and latest_contributor is not None
    current_wait = _hours_between(latest_contributor, now) if waiting else None

    never_answered = bool(issue.is_open and contributor and first is None and known and not issue.fixed_by)
    recent = now - issue.created_at <= window
    target_hours = _response_target_hours(config)

    shared_lanes: list[str] = []
    shared_reasons: list[Reason] = []
    if never_answered and recent and age_hours <= target_hours:
        shared_lanes.append("reply_window")
        shared_reasons.append(
            Reason(
                "new-untriaged",
                "Awaiting a first editor response",
                detail=f"{(target_hours - age_hours) / 24:.1f} days left of the "
                f"{config.response_targets.initial_editor_response_days}-day target",
                timestamp=issue.created_at,
                tone="positive" if age_hours <= config.response_targets.highlight_new_hours else "attention",
            )
        )
    elif never_answered and recent:
        shared_lanes.append("overdue")
        shared_reasons.append(
            Reason(
                "first-response-overdue",
                "First editor response overdue",
                detail=f"{age_hours / 24:.1f} days open",
                timestamp=issue.created_at,
                tone="urgent",
            )
        )
    if waiting and latest_contributor is not None and now - latest_contributor <= window and not issue.fixed_by:
        shared_lanes.append("oldest_wait")
        days = (current_wait or 0) / 24
        shared_reasons.append(
            Reason(
                "waiting-on-editor",
                "Contributor activity awaiting editor response",
                detail=f"{days:.1f} days",
                timestamp=latest_contributor,
                tone="attention" if days < config.response_targets.initial_editor_response_days else "urgent",
            )
        )
    has_topic = any(label.lower().startswith(config.issues.topic_label_prefix) for label in issue.labels)
    if issue.is_open and recent and not has_topic and not issue.fixed_by:
        shared_lanes.append("triage")
        shared_reasons.append(Reason("needs-topic", "No topic label", tone="attention"))
    shared_lanes.append("all")

    return IssueAnalysis(
        issue=issue,
        shared_lanes=tuple(shared_lanes),
        shared_reasons=tuple(shared_reasons),
        perspectives=perspectives,
        waiting_on=waiting_on,
        first_response=first,
        first_response_known=known,
        first_response_hours=first_hours,
        latest_contributor_activity_at=latest_contributor,
        current_wait_hours=current_wait,
        age_hours=age_hours,
        first_time_contributor=issue.author_association in {"FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE"},
    )


def analyze_issues(
    issues: Iterable[IssueSnapshot],
    config: DashboardConfig,
    *,
    now: datetime,
    open_pull_requests: frozenset[int] | None = None,
) -> list[IssueAnalysis]:
    return [analyze_issue(issue, config, now=now, open_pull_requests=open_pull_requests) for issue in issues]


def linked_issues_by_pr(
    analyses: Iterable[IssueAnalysis], config: DashboardConfig, *, now: datetime
) -> dict[int, tuple[LinkedIssue, ...]]:
    """Each claimed issue, restated for the card of every open PR that claims it."""
    by_pr: dict[int, list[LinkedIssue]] = {}
    for analysis in analyses:
        if not analysis.claimed_by:
            continue
        issue = analysis.issue
        direct: dict[str, tuple[Reason, ...]] = {}
        expired: dict[str, tuple[Reason, ...]] = {}
        for editor in sorted(config.people):
            active, stale = _issue_direct_reasons(issue, config, editor=editor, now=now)
            direct[editor] = tuple(_restated(reason, issue.number) for reason in active)
            expired[editor] = tuple(_restated(reason, issue.number) for reason in stale)
        linked = LinkedIssue(
            number=issue.number,
            title=issue.title,
            url=issue.url,
            labels=issue.labels,
            direct=direct,
            expired=expired,
            fingerprints={
                editor: view.content_fingerprint
                for editor, view in analysis.perspectives.items()
                if view.content_fingerprint is not None
            },
        )
        for number in analysis.claimed_by:
            by_pr.setdefault(number, []).append(linked)
    return {number: tuple(sorted(values, key=lambda value: value.number)) for number, values in by_pr.items()}


def _restated(reason: Reason, number: int) -> Reason:
    # A distinct code, so the attention fingerprint tells a mention on the issue from
    # one on the PR.
    return replace(reason, code=f"{reason.code}-on-linked-issue", label=f"{reason.label} on #{number}")

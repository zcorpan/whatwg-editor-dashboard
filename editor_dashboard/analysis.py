from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .checklist import Checklist, parse_checklist
from .config import DashboardConfig
from .models import Activity, PullRequestSnapshot, isoformat


@dataclass(frozen=True)
class Reason:
    code: str
    label: str
    detail: str | None = None
    timestamp: datetime | None = None
    tone: str = "neutral"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "detail": self.detail,
            "timestamp": isoformat(self.timestamp),
            "tone": self.tone,
        }


@dataclass(frozen=True)
class PRAnalysis:
    pr: PullRequestSnapshot
    checklist: Checklist
    lanes: tuple[str, ...]
    reasons: tuple[Reason, ...]
    blockers: tuple[Reason, ...]
    content_fingerprint: str
    attention_fingerprint: str
    has_attention_signal: bool
    direct_request_at: datetime | None
    stale_direct_at: datetime | None
    rereview_trigger_at: datetime | None
    latest_viewer_review: Activity | None
    latest_contributor_activity_at: datetime | None
    latest_editor_activity_at: datetime | None
    current_wait_hours: float | None
    first_editor_response: Activity | None
    first_editor_response_known: bool
    first_editor_response_hours: float | None
    waiting_reason: str
    first_time_contributor: bool
    age_hours: float

    def to_public_dict(self, repository_slug: str) -> dict[str, Any]:
        pr = self.pr
        return {
            "key": f"{repository_slug}#{pr.number}",
            "number": pr.number,
            "title": pr.title,
            "url": pr.url,
            "author": pr.author,
            "author_association": pr.author_association,
            "first_time_contributor": self.first_time_contributor,
            "created_at": isoformat(pr.created_at),
            "updated_at": isoformat(pr.updated_at),
            "age_hours": round(self.age_hours, 2),
            "is_draft": pr.is_draft,
            "labels": list(pr.labels),
            "additions": pr.additions,
            "deletions": pr.deletions,
            "changed_files": pr.changed_files,
            "changed_lines": pr.changed_lines,
            "mergeable": pr.mergeable,
            "merge_state_status": pr.merge_state_status,
            "review_decision": pr.review_decision,
            "status_state": pr.status_state,
            "review_requests": list(pr.review_requests),
            "assignees": list(pr.assignees),
            "unresolved_review_threads": pr.unresolved_review_threads,
            "review_threads_total_count": pr.review_threads_total_count,
            "review_threads_sample_complete": pr.review_threads_sample_complete,
            "timeline_sampled_count": pr.timeline_sampled_count,
            "timeline_sample_complete": pr.timeline_sample_complete,
            "viewer_reviews_total_count": pr.viewer_reviews_total_count,
            "checklist": self.checklist.to_public_dict(),
            "lanes": list(self.lanes),
            "reasons": [reason.to_public_dict() for reason in self.reasons],
            "blockers": [reason.to_public_dict() for reason in self.blockers],
            "fingerprint": self.content_fingerprint,
            "attention_fingerprint": self.attention_fingerprint,
            "has_attention_signal": self.has_attention_signal,
            "direct_request_at": isoformat(self.direct_request_at),
            "stale_direct_at": isoformat(self.stale_direct_at),
            "rereview_trigger_at": isoformat(self.rereview_trigger_at),
            "latest_viewer_review_at": isoformat(self.latest_viewer_review.created_at if self.latest_viewer_review else None),
            "latest_contributor_activity_at": isoformat(self.latest_contributor_activity_at),
            "latest_editor_activity_at": isoformat(self.latest_editor_activity_at),
            "current_wait_hours": round(self.current_wait_hours, 2) if self.current_wait_hours is not None else None,
            "first_editor_response_at": isoformat(self.first_editor_response.created_at if self.first_editor_response else None),
            "first_editor_response_by": self.first_editor_response.author if self.first_editor_response else None,
            "first_editor_response_known": self.first_editor_response_known,
            "first_editor_response_hours": (
                round(self.first_editor_response_hours, 2)
                if self.first_editor_response_hours is not None
                else None
            ),
            "waiting_reason": self.waiting_reason,
        }


_KNOWN_BOTS = {
    "github-actions",
    "github-actions[bot]",
    "dependabot[bot]",
    "renovate[bot]",
    "whatwg-bot",
}


def is_bot(login: str | None) -> bool:
    if not login:
        return False
    normalized = login.lower()
    return normalized in _KNOWN_BOTS or normalized.endswith("[bot]")


def _mention_pattern(viewer: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9-])@{re.escape(viewer)}(?![A-Za-z0-9-])", re.IGNORECASE)


def _hours_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600)


def _max_datetime(values: Iterable[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _latest_viewer_review(pr: PullRequestSnapshot) -> Activity | None:
    submitted = [review for review in pr.viewer_reviews if review.state != "PENDING"]
    return max(submitted, key=lambda review: (review.created_at, review.id)) if submitted else None


def _first_editor_response(
    pr: PullRequestSnapshot,
    editors: frozenset[str],
) -> tuple[Activity | None, bool]:
    editor_activities = [
        activity
        for activity in pr.timeline
        if activity.author in editors and not is_bot(activity.author)
    ]
    if not editor_activities:
        return None, pr.timeline_sample_complete

    first = min(editor_activities, key=lambda activity: (activity.created_at, activity.id))
    known = pr.timeline_sample_complete or first.id in pr.timeline_first_ids
    return first, known


def _contributor_activity_times(
    pr: PullRequestSnapshot,
    editors: frozenset[str],
) -> list[datetime]:
    times: list[datetime] = []
    author_is_contributor = pr.author not in editors and not is_bot(pr.author)
    if author_is_contributor:
        times.append(pr.created_at)
        if pr.last_edited_at:
            times.append(pr.last_edited_at)

    head_author = pr.head_author or pr.author
    if pr.head_committed_at and head_author not in editors and not is_bot(head_author):
        times.append(pr.head_committed_at)

    for activity in pr.timeline:
        if activity.author not in editors and not is_bot(activity.author):
            times.append(activity.updated_at)
    return times


def _editor_activity_times(pr: PullRequestSnapshot, editors: frozenset[str]) -> list[datetime]:
    times = [
        activity.updated_at
        for activity in pr.timeline
        if activity.author in editors and not is_bot(activity.author)
    ]
    if pr.head_committed_at and pr.head_author in editors and not is_bot(pr.head_author):
        times.append(pr.head_committed_at)
    return times


def _direct_reasons(
    pr: PullRequestSnapshot,
    config: DashboardConfig,
    *,
    now: datetime,
) -> tuple[list[Reason], list[Reason]]:
    """Split direct-attention signals into currently active and expired ones.

    A review request or assignment is *current state*: GitHub removes it once the
    editor reviews, so it keeps claiming attention for as long as it is set. A
    mention is a past *event* with no such clearing mechanism — without an expiry a
    single 2016 comment marks a PR as a direct request forever, which is what buried
    the live queue under a decade of archaeology.
    """
    viewer = config.viewer
    pattern = _mention_pattern(viewer)
    cutoff = now - timedelta(days=config.attention.activity_window_days)
    active: list[Reason] = []
    expired: list[Reason] = []

    def record(reason: Reason, *, expires: bool) -> None:
        if expires and reason.timestamp is not None and reason.timestamp < cutoff:
            expired.append(replace(reason, tone="muted"))
        else:
            active.append(reason)

    if viewer in pr.review_requests:
        record(
            Reason(
                "review-requested",
                f"Review requested from @{viewer}",
                timestamp=pr.updated_at,
                tone="urgent",
            ),
            expires=False,
        )
    if viewer in pr.assignees:
        record(
            Reason(
                "assigned",
                f"Assigned to @{viewer}",
                timestamp=pr.updated_at,
                tone="urgent",
            ),
            expires=False,
        )

    if pr.author != viewer and pattern.search(pr.body):
        record(
            Reason(
                "mentioned-in-description",
                f"@{viewer} mentioned in the description",
                timestamp=pr.last_edited_at or pr.created_at,
                tone="urgent",
            ),
            expires=True,
        )

    mention_activities = [
        activity
        for activity in pr.timeline
        if activity.author != viewer
        and not is_bot(activity.author)
        and pattern.search(activity.body)
    ]
    if mention_activities:
        latest = max(mention_activities, key=lambda activity: (activity.updated_at, activity.id))
        record(
            Reason(
                "mentioned-in-discussion",
                f"@{viewer} mentioned in the discussion",
                detail=f"by @{latest.author}" if latest.author else None,
                timestamp=latest.updated_at,
                tone="urgent",
            ),
            expires=True,
        )

    return active, expired


def _rereview_reasons(
    pr: PullRequestSnapshot,
    latest_review: Activity | None,
) -> list[Reason]:
    if latest_review is None:
        return []

    reasons: list[Reason] = []
    head_changed = False
    if latest_review.commit_oid and pr.head_oid:
        head_changed = latest_review.commit_oid != pr.head_oid
    elif pr.head_committed_at:
        head_changed = pr.head_committed_at > latest_review.created_at

    if head_changed:
        reasons.append(
            Reason(
                "head-changed-since-review",
                "Head commit changed since your review",
                timestamp=pr.head_committed_at or pr.updated_at,
                tone="attention",
            )
        )

    if pr.last_edited_at and pr.last_edited_at > latest_review.created_at:
        reasons.append(
            Reason(
                "description-edited-since-review",
                "Description edited since your review",
                timestamp=pr.last_edited_at,
                tone="attention",
            )
        )

    author_activity = [
        activity
        for activity in pr.timeline
        if activity.author == pr.author
        and not is_bot(activity.author)
        and activity.updated_at > latest_review.created_at
    ]
    if author_activity:
        latest = max(author_activity, key=lambda activity: (activity.updated_at, activity.id))
        reasons.append(
            Reason(
                "author-replied-since-review",
                "Author activity since your review",
                timestamp=latest.updated_at,
                tone="attention",
            )
        )

    return reasons


def _ready_and_blockers(
    pr: PullRequestSnapshot,
    checklist: Checklist,
    config: DashboardConfig,
) -> tuple[bool, list[Reason], list[Reason]]:
    positive: list[Reason] = []
    blockers: list[Reason] = []

    if pr.is_draft:
        blockers.append(Reason("draft", "Draft pull request", tone="muted"))

    matching_labels = sorted(label for label in pr.labels if label.lower() in config.blocking_labels)
    if matching_labels:
        blockers.append(
            Reason(
                "blocking-label",
                "Blocking label",
                detail=", ".join(matching_labels),
                tone="warning",
            )
        )

    if pr.mergeable == "CONFLICTING":
        blockers.append(Reason("merge-conflict", "Merge conflict", tone="warning"))
    elif pr.mergeable == "UNKNOWN":
        blockers.append(Reason("mergeability-unknown", "Mergeability unknown", tone="muted"))

    if pr.status_state in {"FAILURE", "ERROR"}:
        blockers.append(Reason("ci-failing", "Checks failing", tone="warning"))
    elif pr.status_state in {"PENDING"}:
        blockers.append(Reason("ci-pending", "Checks pending", tone="muted"))

    if pr.review_decision == "CHANGES_REQUESTED":
        blockers.append(Reason("changes-requested", "Changes requested", tone="warning"))

    if not pr.review_threads_sample_complete:
        blockers.append(
            Reason(
                "review-thread-sample-incomplete",
                "Review-thread sample incomplete",
                tone="muted",
            )
        )
    elif pr.unresolved_review_threads:
        blockers.append(
            Reason(
                "unresolved-review-threads",
                f"{pr.unresolved_review_threads} unresolved review thread"
                + ("s" if pr.unresolved_review_threads != 1 else ""),
                tone="warning",
            )
        )

    if pr.changed_files > config.ready_bounded.max_changed_files:
        blockers.append(
            Reason(
                "large-file-count",
                "Larger file count",
                detail=f"{pr.changed_files} files",
                tone="muted",
            )
        )
    else:
        positive.append(Reason("bounded-files", f"{pr.changed_files} changed files", tone="positive"))

    if pr.changed_lines > config.ready_bounded.max_changed_lines:
        blockers.append(
            Reason(
                "large-diff",
                "Larger diff",
                detail=f"{pr.changed_lines} changed lines",
                tone="muted",
            )
        )
    else:
        positive.append(Reason("bounded-diff", f"{pr.changed_lines} changed lines", tone="positive"))

    if checklist.total:
        if (checklist.ratio or 0) < config.ready_bounded.minimum_checklist_ratio:
            blockers.append(
                Reason(
                    "checklist-low",
                    "Description checklist incomplete",
                    detail=f"{checklist.checked}/{checklist.total}",
                    tone="muted",
                )
            )
        else:
            positive.append(
                Reason(
                    "checklist-high",
                    "Description checklist complete"
                    if checklist.checked == checklist.total
                    else "Description checklist meets the configured threshold",
                    detail=f"{checklist.checked}/{checklist.total}",
                    tone="positive",
                )
            )
    else:
        blockers.append(
            Reason(
                "checklist-missing",
                "Description checklist not found",
                tone="muted",
            )
        )

    if pr.status_state in {None, "SUCCESS", "EXPECTED"}:
        positive.append(
            Reason(
                "checks-clear",
                "Checks passing" if pr.status_state == "SUCCESS" else "No failing checks detected",
                tone="positive",
            )
        )

    hard_blocker_codes = {
        "draft",
        "blocking-label",
        "merge-conflict",
        "mergeability-unknown",
        "ci-failing",
        "ci-pending",
        "changes-requested",
        "review-thread-sample-incomplete",
        "unresolved-review-threads",
        "large-file-count",
        "large-diff",
        "checklist-low",
        "checklist-missing",
    }
    ready = pr.mergeable == "MERGEABLE" and not any(
        blocker.code in hard_blocker_codes for blocker in blockers
    )
    return ready, positive, blockers


def _waiting_reason(pr: PullRequestSnapshot, blockers: Iterable[Reason], waiting_on_editor: bool) -> str:
    blocker_codes = {blocker.code for blocker in blockers}
    precedence = [
        ("draft", "Draft"),
        ("blocking-label", "Blocking label"),
        ("merge-conflict", "Merge conflict"),
        ("ci-failing", "Checks failing"),
        ("ci-pending", "Checks pending"),
        ("changes-requested", "Changes requested"),
        ("unresolved-review-threads", "Unresolved review threads"),
        ("review-thread-sample-incomplete", "Review-thread status unknown"),
    ]
    for code, label in precedence:
        if code in blocker_codes:
            return label
    if waiting_on_editor:
        return "Waiting on editor"
    if pr.review_requests:
        return "Waiting on requested reviewer"
    return "No clear next action"


def analyze_pull_request(
    pr: PullRequestSnapshot,
    config: DashboardConfig,
    *,
    now: datetime,
) -> PRAnalysis:
    now = now.astimezone(timezone.utc)
    checklist = parse_checklist(pr.body)
    direct_reasons, expired_direct_reasons = _direct_reasons(pr, config, now=now)
    latest_review = _latest_viewer_review(pr)
    rereview_reasons = _rereview_reasons(pr, latest_review)

    contributor_times = _contributor_activity_times(pr, config.editors)
    editor_times = _editor_activity_times(pr, config.editors)
    latest_contributor = max(contributor_times) if contributor_times else None
    latest_editor = max(editor_times) if editor_times else None
    waiting_on_editor = bool(
        latest_contributor
        and (latest_editor is None or latest_contributor > latest_editor)
        and pr.author not in config.editors
        and not is_bot(pr.author)
    )
    current_wait_hours = _hours_between(latest_contributor, now) if waiting_on_editor and latest_contributor else None

    first_editor, first_response_known = _first_editor_response(pr, config.editors)
    first_response_hours = (
        _hours_between(pr.created_at, first_editor.created_at)
        if first_editor and first_response_known
        else None
    )

    ready_bounded, positive_reasons, blockers = _ready_and_blockers(pr, checklist, config)
    age_hours = _hours_between(pr.created_at, now)
    target_hours = config.response_targets.initial_editor_response_days * 24

    # "Never received a first editor response" — deliberately unbounded by age. The
    # previous seven-day cap removed a PR from this lane on the very day it missed
    # the response target, so the lane could only ever show successes in progress.
    is_new = bool(
        not pr.is_draft
        and pr.author not in config.editors
        and not is_bot(pr.author)
        and first_editor is None
        and first_response_known
    )
    oldest_wait = bool(waiting_on_editor and not pr.is_draft)

    # Recency is the primary axis: the top of the queue answers "which reviews am I
    # currently in the middle of", not "which claim on my attention is oldest".
    activity_cutoff = now - timedelta(days=config.attention.activity_window_days)
    editor_involved = bool(direct_reasons) or bool(rereview_reasons) or latest_review is not None
    is_active = bool(pr.updated_at >= activity_cutoff and editor_involved)

    lanes: list[str] = []
    if is_active:
        lanes.append("active")
    if direct_reasons:
        lanes.append("direct")
    elif expired_direct_reasons:
        lanes.append("stale_direct")
    if rereview_reasons:
        lanes.append("rereview")
    if is_new:
        lanes.append("new")
    if oldest_wait:
        lanes.append("oldest_wait")
    if ready_bounded:
        lanes.append("ready_bounded")
    lanes.append("all")

    reasons: list[Reason] = []
    if is_active:
        reasons.append(
            Reason(
                "recently-active",
                f"Active within {config.attention.activity_window_days} days",
                timestamp=pr.updated_at,
                tone="attention",
            )
        )
    reasons.extend((*direct_reasons, *expired_direct_reasons, *rereview_reasons))
    if is_new:
        if age_hours > target_hours:
            reasons.append(
                Reason(
                    "first-response-overdue",
                    "First editor response overdue",
                    detail=f"{age_hours / 24:.1f} days open",
                    timestamp=pr.created_at,
                    tone="urgent",
                )
            )
        else:
            tone = "positive" if age_hours <= config.response_targets.highlight_new_hours else "attention"
            reasons.append(
                Reason(
                    "new-untriaged",
                    "Awaiting a first editor response",
                    detail=f"target {config.response_targets.initial_editor_response_days} days",
                    timestamp=pr.created_at,
                    tone=tone,
                )
            )
    if oldest_wait and current_wait_hours is not None:
        days = current_wait_hours / 24
        reasons.append(
            Reason(
                "waiting-on-editor",
                "Contributor activity awaiting editor response",
                detail=f"{days:.1f} days",
                timestamp=latest_contributor,
                tone="attention" if days < config.response_targets.initial_editor_response_days else "urgent",
            )
        )
    if ready_bounded:
        reasons.append(Reason("ready-bounded", "Appears ready and bounded", tone="positive"))
        reasons.extend(positive_reasons)

    # The freshest signal represents the PR. Taking the oldest meant one stale
    # mention outranked a review request filed on the same PR the same week.
    def _latest(reasons_in: Iterable[Reason]) -> datetime | None:
        values = list(reasons_in)
        if not values:
            return None
        return max((reason.timestamp for reason in values if reason.timestamp), default=pr.updated_at)

    direct_at = _latest(direct_reasons)
    rereview_at = _latest(rereview_reasons)
    stale_direct_at = _latest(expired_direct_reasons)

    body_hash = hashlib.sha256(pr.body.encode("utf-8")).hexdigest()
    activity_fingerprint = [
        (activity.id, isoformat(activity.updated_at), activity.state, activity.commit_oid)
        for activity in pr.timeline
    ]
    content_payload = {
        "number": pr.number,
        "updated_at": isoformat(pr.updated_at),
        "head_oid": pr.head_oid,
        "body_hash": body_hash,
        "labels": pr.labels,
        "assignees": pr.assignees,
        "review_requests": pr.review_requests,
        "review_decision": pr.review_decision,
        "status_state": pr.status_state,
        "mergeable": pr.mergeable,
        "activities": activity_fingerprint,
        "threads": [pr.unresolved_review_threads, pr.review_threads_total_count],
    }
    content_fingerprint = _hash_payload(content_payload)

    # Expired mentions stay in the fingerprint so that a signal ageing out does not
    # by itself mark an already-seen item unseen again.
    attention_reasons = [*direct_reasons, *expired_direct_reasons, *rereview_reasons]
    attention_payload = [
        (reason.code, isoformat(reason.timestamp), reason.detail)
        for reason in attention_reasons
    ]
    attention_fingerprint = _hash_payload(attention_payload or [("none", None, None)])

    waiting_reason = _waiting_reason(pr, blockers, waiting_on_editor)
    # GitHub reports NONE for an author with no prior merged contribution to the
    # repository; whatwg/html never returns the FIRST_TIME* values in practice, so
    # checking only for those left this signal permanently false.
    first_time = pr.author_association in {"FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE"}

    return PRAnalysis(
        pr=pr,
        checklist=checklist,
        lanes=tuple(lanes),
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        content_fingerprint=content_fingerprint,
        attention_fingerprint=attention_fingerprint,
        has_attention_signal=bool(direct_reasons or rereview_reasons),
        direct_request_at=direct_at,
        stale_direct_at=stale_direct_at,
        rereview_trigger_at=rereview_at,
        latest_viewer_review=latest_review,
        latest_contributor_activity_at=latest_contributor,
        latest_editor_activity_at=latest_editor,
        current_wait_hours=current_wait_hours,
        first_editor_response=first_editor,
        first_editor_response_known=first_response_known,
        first_editor_response_hours=first_response_hours,
        waiting_reason=waiting_reason,
        first_time_contributor=first_time,
        age_hours=age_hours,
    )


def analyze_all(
    pull_requests: Iterable[PullRequestSnapshot],
    config: DashboardConfig,
    *,
    now: datetime,
) -> list[PRAnalysis]:
    return [analyze_pull_request(pr, config, now=now) for pr in pull_requests]


def build_lanes(analyses: Iterable[PRAnalysis], repository_slug: str) -> dict[str, list[str]]:
    values = list(analyses)

    def key(analysis: PRAnalysis) -> str:
        return f"{repository_slug}#{analysis.pr.number}"

    # Newest first for the attention lanes: on a decade-old backlog, age is a poor
    # proxy for actionability. `oldest_wait` and `new` keep oldest-first ordering
    # because fairness to waiting contributors is exactly what they measure.
    active = sorted(
        (analysis for analysis in values if "active" in analysis.lanes),
        key=lambda analysis: (analysis.pr.updated_at, analysis.pr.number),
        reverse=True,
    )
    direct = sorted(
        (analysis for analysis in values if "direct" in analysis.lanes),
        key=lambda analysis: (analysis.direct_request_at or analysis.pr.updated_at, analysis.pr.number),
        reverse=True,
    )
    stale_direct = sorted(
        (analysis for analysis in values if "stale_direct" in analysis.lanes),
        key=lambda analysis: (analysis.stale_direct_at or analysis.pr.updated_at, analysis.pr.number),
        reverse=True,
    )
    rereview = sorted(
        (analysis for analysis in values if "rereview" in analysis.lanes),
        key=lambda analysis: (analysis.rereview_trigger_at or analysis.pr.updated_at, analysis.pr.number),
        reverse=True,
    )
    new = sorted(
        (analysis for analysis in values if "new" in analysis.lanes),
        key=lambda analysis: (analysis.pr.created_at, analysis.pr.number),
    )
    oldest_wait = sorted(
        (analysis for analysis in values if "oldest_wait" in analysis.lanes),
        key=lambda analysis: (-(analysis.current_wait_hours or 0), analysis.pr.number),
    )
    ready = sorted(
        (analysis for analysis in values if "ready_bounded" in analysis.lanes),
        key=lambda analysis: (analysis.pr.changed_lines, analysis.pr.changed_files, analysis.pr.created_at),
    )
    all_items = sorted(values, key=lambda analysis: (analysis.pr.updated_at, analysis.pr.number), reverse=True)

    return {
        "active": [key(value) for value in active],
        "direct": [key(value) for value in direct],
        "stale_direct": [key(value) for value in stale_direct],
        "rereview": [key(value) for value in rereview],
        "new": [key(value) for value in new],
        "oldest_wait": [key(value) for value in oldest_wait],
        "ready_bounded": [key(value) for value in ready],
        "all": [key(value) for value in all_items],
    }

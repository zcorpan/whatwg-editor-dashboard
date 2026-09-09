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


# The perspective key that answers "what needs any editor", as opposed to one
# editor's login. Deliberately not spelled "all": that is already a lane name, and
# lanes and perspectives are indexed by two different dictionaries.
ALL_EDITORS = "all-editors"

# Lane membership that depends on who is asking, and lane membership that does not.
# `_direct_reasons` and `_rereview_reasons` are per-editor; everything else — the
# first-response pair, the contributor wait, the ready heuristic — is a property of
# the pull request, so it is computed once and shared by every perspective.
PERSPECTIVE_LANES = ("active", "direct", "stale_direct", "rereview")
SHARED_LANES = ("reply_window", "overdue", "oldest_wait", "ready_bounded", "all")


def perspective_keys(config: DashboardConfig) -> tuple[str, ...]:
    """Every queue perspective, in the order the browser offers them."""
    return (ALL_EDITORS, *sorted(config.editors))


@dataclass(frozen=True)
class PerspectiveView:
    """One PR's analysis as seen by one editor, or by all of them at once."""

    lanes: tuple[str, ...]
    reasons: tuple[Reason, ...]
    direct_request_at: datetime | None
    stale_direct_at: datetime | None
    rereview_trigger_at: datetime | None
    has_attention_signal: bool
    latest_review: Activity | None
    # Both are None for ALL_EDITORS. A fingerprint has to exclude exactly one
    # person's own footprint to mean anything, and the union is not a person; the
    # browser reads the fingerprints of whoever the user says they are, whichever
    # perspective is on screen.
    content_fingerprint: str | None
    attention_fingerprint: str | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "lanes": list(self.lanes),
            "reasons": [reason.to_public_dict() for reason in self.reasons],
            "has_attention_signal": self.has_attention_signal,
            "fingerprint": self.content_fingerprint,
            "attention_fingerprint": self.attention_fingerprint,
        }


@dataclass(frozen=True)
class PRAnalysis:
    pr: PullRequestSnapshot
    checklist: Checklist
    shared_lanes: tuple[str, ...]
    shared_reasons: tuple[Reason, ...]
    perspectives: dict[str, PerspectiveView]
    blockers: tuple[Reason, ...]
    latest_contributor_activity_at: datetime | None
    latest_editor_activity_at: datetime | None
    current_wait_hours: float | None
    first_editor_response: Activity | None
    first_editor_response_known: bool
    first_editor_response_hours: float | None
    waiting_reason: str
    first_time_contributor: bool
    age_hours: float

    def lanes_for(self, perspective: str) -> tuple[str, ...]:
        return (*self.perspectives[perspective].lanes, *self.shared_lanes)

    def reasons_for(self, perspective: str) -> tuple[Reason, ...]:
        return (*self.perspectives[perspective].reasons, *self.shared_reasons)

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
            "reviews_total_count": pr.reviews_total_count,
            "checklist": self.checklist.to_public_dict(),
            # The lanes and evidence every perspective agrees on, published once;
            # `perspectives` carries only what depends on which editor is asking, and
            # the browser concatenates the two rather than storing six near-copies.
            "lanes": list(self.shared_lanes),
            "reasons": [reason.to_public_dict() for reason in self.shared_reasons],
            "perspectives": {
                key: view.to_public_dict() for key, view in self.perspectives.items()
            },
            "blockers": [reason.to_public_dict() for reason in self.blockers],
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


# Direct-request reasons that describe current GitHub state rather than a dated
# event. Their timestamps are stand-ins, so they stay out of the attention
# fingerprint; see analyze_pull_request.
_STATE_ATTENTION_CODES = frozenset({"review-requested", "assigned"})


def _mention_pattern(login: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9-])@{re.escape(login)}(?![A-Za-z0-9-])", re.IGNORECASE)


def _hours_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600)


def _response_target_hours(config: DashboardConfig) -> float:
    # One source for the boundary that splits `reply_window` from `overdue` and
    # picks between the `new-untriaged` and `first-response-overdue` chips. The two
    # must never disagree about which side of the target a PR is on.
    return config.response_targets.initial_editor_response_days * 24


def _max_datetime(values: Iterable[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _latest_editor_review(pr: PullRequestSnapshot, editor: str) -> Activity | None:
    submitted = pr.submitted_reviews_by(editor)
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
    editor: str,
    now: datetime,
) -> tuple[list[Reason], list[Reason]]:
    """Split ``editor``'s direct-attention signals into currently active and expired ones.

    A review request or assignment is *current state*: GitHub removes it once the
    editor reviews, so it keeps claiming attention for as long as it is set. A
    mention is a past *event* with no such clearing mechanism — without an expiry a
    single 2016 comment marks a PR as a direct request forever, which is what buried
    the live queue under a decade of archaeology.
    """
    pattern = _mention_pattern(editor)
    cutoff = now - timedelta(days=config.attention.activity_window_days)
    active: list[Reason] = []
    expired: list[Reason] = []

    def record(reason: Reason, *, expires: bool) -> None:
        if expires and reason.timestamp is not None and reason.timestamp < cutoff:
            expired.append(replace(reason, tone="muted"))
        else:
            active.append(reason)

    if editor in pr.review_requests:
        record(
            Reason(
                "review-requested",
                f"Review requested from @{editor}",
                timestamp=pr.updated_at,
                tone="urgent",
            ),
            expires=False,
        )
    if editor in pr.assignees:
        record(
            Reason(
                "assigned",
                f"Assigned to @{editor}",
                timestamp=pr.updated_at,
                tone="urgent",
            ),
            expires=False,
        )

    if pr.author != editor and pattern.search(pr.body):
        record(
            Reason(
                "mentioned-in-description",
                f"@{editor} mentioned in the description",
                timestamp=pr.last_edited_at or pr.created_at,
                tone="urgent",
            ),
            expires=True,
        )

    mention_activities = [
        activity
        for activity in pr.timeline
        if activity.author != editor
        and not is_bot(activity.author)
        and pattern.search(activity.body)
    ]
    if mention_activities:
        latest = max(mention_activities, key=lambda activity: (activity.updated_at, activity.id))
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


def _rereview_reasons(
    pr: PullRequestSnapshot,
    latest_review: Activity | None,
    *,
    editor: str,
) -> list[Reason]:
    """What changed on ``pr`` after ``editor``'s latest sampled review."""
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
                f"Head commit changed since @{editor}'s review",
                timestamp=pr.head_committed_at or pr.updated_at,
                tone="attention",
            )
        )

    if pr.last_edited_at and pr.last_edited_at > latest_review.created_at:
        reasons.append(
            Reason(
                "description-edited-since-review",
                f"Description edited since @{editor}'s review",
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
                f"Author activity since @{editor}'s review",
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


def _content_fingerprint(pr: PullRequestSnapshot, editor: str, body_hash: str) -> str:
    """Hash ``pr``'s public content, excluding ``editor``'s own footprint.

    "Address until changed" means "until somebody other than me changes it", so
    every field below has to be independent of that editor's own actions.
    Reviewing a PR used to change five of them at once — pr.updated_at, the
    sampled activity list, the reviewer's own review request, review_decision and
    the review-thread counts — which brought every addressed item straight back
    the next morning.

    pr.updated_at is therefore left out entirely: it bumps on the editor's own
    comment and cannot be attributed. The public state it stood proxy for is
    hashed field by field instead, so a change by anyone else is still caught.
    """
    external_activity = [activity for activity in pr.timeline if activity.author != editor]
    # Only the newest foreign comment or review, not the whole sampled list: on a
    # PR with more than 2 * timeline_each_end of them, the editor's own comment
    # shifts the sampling window and drops an older foreign item out of it, which
    # would move a list-based hash for exactly the same reason. Comments other
    # than the newest can therefore be edited unnoticed.
    latest_external = external_activity[-1] if external_activity else None
    open_threads_by_others, threads_by_others = pr.review_threads_started_by_others(editor)
    return _hash_payload(
        {
            "number": pr.number,
            "title": pr.title,
            "body_hash": body_hash,
            "is_draft": pr.is_draft,
            "head_oid": pr.head_oid,
            "labels": pr.labels,
            # GitHub clears an editor's review request the moment they review, and
            # review_decision reflects their own verdict, so neither their own
            # request nor the decision can be hashed. A re-request from somebody
            # else no longer resurfaces an addressed item on its own; in practice
            # it arrives with a comment or a push, which does.
            "assignees": [login for login in pr.assignees if login != editor],
            "review_requests": [login for login in pr.review_requests if login != editor],
            "status_state": pr.status_state,
            # mergeable is deliberately absent. GitHub computes mergeability lazily: a
            # cold query returns UNKNOWN and only schedules the real answer, so a build
            # reads UNKNOWN for most PRs and something else for the rest, with nothing
            # about the PR having changed. Measured against one deployed build, 258 of
            # 277 open PRs reported a different value minutes later on identical
            # updatedAt and head commits, which brought back nearly every addressed
            # item on the next build. A conflict appearing is also not something the
            # PR's author did; the head commit and the base branch cover real changes,
            # and the merge-conflict blocker chip still shows the current state.
            "latest_activity": (
                [
                    latest_external.id,
                    isoformat(latest_external.updated_at),
                    latest_external.state,
                    latest_external.commit_oid,
                ]
                if latest_external is not None
                else None
            ),
            "threads": [open_threads_by_others, threads_by_others],
        }
    )


def _attention_fingerprint(reasons: Iterable[Reason]) -> str:
    """Hash the direct-request and re-review signals claiming one editor's attention."""
    payload = [
        (
            reason.code,
            # A review request and an assignment are current state; this query
            # cannot see when either was set, so the reason carries pr.updated_at
            # as a stand-in for ordering. Hashing that made an unrelated push —
            # or the editor's own comment — mark the item unseen again.
            None if reason.code in _STATE_ATTENTION_CODES else isoformat(reason.timestamp),
            reason.detail,
        )
        for reason in reasons
    ]
    return _hash_payload(payload or [("none", None, None)])


def _perspective_view(
    pr: PullRequestSnapshot,
    config: DashboardConfig,
    *,
    editor: str | None,
    direct: list[Reason],
    expired: list[Reason],
    rereview: list[Reason],
    latest_review: Activity | None,
    body_hash: str,
    now: datetime,
) -> PerspectiveView:
    """Turn one perspective's attention signals into its lanes, evidence and hashes.

    ``editor`` is None for the union of every editor, which gets no fingerprints.
    """
    # Recency is the primary axis: the top of the queue answers "which reviews is
    # this editor currently in the middle of", not "which claim on their attention
    # is oldest".
    activity_cutoff = now - timedelta(days=config.attention.activity_window_days)
    involved = bool(direct) or bool(rereview) or latest_review is not None
    is_active = bool(pr.updated_at >= activity_cutoff and involved)

    lanes: list[str] = []
    reasons: list[Reason] = []
    if is_active:
        lanes.append("active")
        reasons.append(
            Reason(
                "recently-active",
                f"Active within {config.attention.activity_window_days} days",
                timestamp=pr.updated_at,
                tone="attention",
            )
        )
    if direct:
        lanes.append("direct")
    elif expired:
        lanes.append("stale_direct")
    if rereview:
        lanes.append("rereview")
    reasons.extend((*direct, *expired, *rereview))

    # The freshest signal represents the PR. Taking the oldest meant one stale
    # mention outranked a review request filed on the same PR the same week.
    def latest(values: Iterable[Reason]) -> datetime | None:
        present = list(values)
        if not present:
            return None
        return max((reason.timestamp for reason in present if reason.timestamp), default=pr.updated_at)

    return PerspectiveView(
        lanes=tuple(lanes),
        reasons=tuple(reasons),
        direct_request_at=latest(direct),
        stale_direct_at=latest(expired),
        rereview_trigger_at=latest(rereview),
        has_attention_signal=bool(direct or rereview),
        latest_review=latest_review,
        content_fingerprint=_content_fingerprint(pr, editor, body_hash) if editor else None,
        # Expired mentions stay in the hash so that a signal ageing out does not by
        # itself mark an already-seen item unseen again.
        attention_fingerprint=_attention_fingerprint((*direct, *expired, *rereview)) if editor else None,
    )


def analyze_pull_request(
    pr: PullRequestSnapshot,
    config: DashboardConfig,
    *,
    now: datetime,
) -> PRAnalysis:
    now = now.astimezone(timezone.utc)
    checklist = parse_checklist(pr.body)
    body_hash = hashlib.sha256(pr.body.encode("utf-8")).hexdigest()

    # Every editor's direct and re-review signals, plus their own fingerprints, so
    # the browser can show the queue from any perspective and track seen/addressed
    # state for whoever the user says they are.
    editors = sorted(config.editors)
    signals: dict[str, tuple[list[Reason], list[Reason], list[Reason], Activity | None]] = {}
    for editor in editors:
        direct, expired = _direct_reasons(pr, config, editor=editor, now=now)
        editor_review = _latest_editor_review(pr, editor)
        signals[editor] = (direct, expired, _rereview_reasons(pr, editor_review, editor=editor), editor_review)

    def merged(index: int) -> list[Reason]:
        return [reason for editor in editors for reason in signals[editor][index]]

    perspectives: dict[str, PerspectiveView] = {}
    for key in perspective_keys(config):
        if key == ALL_EDITORS:
            # The union: a PR claims the team's attention if it claims any editor's.
            # Assembled from the merged signal lists rather than from the per-editor
            # lane sets, so the direct/stale-mention split stays a single decision.
            direct, expired, rereview = merged(0), merged(1), merged(2)
            reviews = [signals[editor][3] for editor in editors if signals[editor][3] is not None]
            latest_review = max(reviews, key=lambda review: (review.created_at, review.id)) if reviews else None
        else:
            direct, expired, rereview, latest_review = signals[key]
        perspectives[key] = _perspective_view(
            pr,
            config,
            editor=None if key == ALL_EDITORS else key,
            direct=direct,
            expired=expired,
            rereview=rereview,
            latest_review=latest_review,
            body_hash=body_hash,
            now=now,
        )

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
    target_hours = _response_target_hours(config)

    # "Never received a first editor response", split on the response target into two
    # lanes with opposite meanings: inside the target a reply can still meet it, past
    # it the outcome is already fixed. The pair is deliberately unbounded by age — an
    # earlier seven-day cap dropped a PR on the very day it missed the target, so the
    # lane could only ever show successes in progress. Now it moves rather than
    # disappears.
    never_answered = bool(
        not pr.is_draft
        and pr.author not in config.editors
        and not is_bot(pr.author)
        and first_editor is None
        and first_response_known
    )
    in_reply_window = never_answered and age_hours <= target_hours
    first_response_overdue = never_answered and age_hours > target_hours
    oldest_wait = bool(waiting_on_editor and not pr.is_draft)

    # These four lanes are properties of the pull request, not of whoever is asking,
    # so they are computed once and shared by every perspective.
    shared_lanes: list[str] = []
    if in_reply_window:
        shared_lanes.append("reply_window")
    if first_response_overdue:
        shared_lanes.append("overdue")
    if oldest_wait:
        shared_lanes.append("oldest_wait")
    if ready_bounded:
        shared_lanes.append("ready_bounded")
    shared_lanes.append("all")

    shared_reasons: list[Reason] = []
    if first_response_overdue:
        shared_reasons.append(
            Reason(
                "first-response-overdue",
                "First editor response overdue",
                detail=f"{age_hours / 24:.1f} days open",
                timestamp=pr.created_at,
                tone="urgent",
            )
        )
    elif in_reply_window:
        tone = "positive" if age_hours <= config.response_targets.highlight_new_hours else "attention"
        shared_reasons.append(
            Reason(
                "new-untriaged",
                "Awaiting a first editor response",
                detail=f"{(target_hours - age_hours) / 24:.1f} days left of the "
                f"{config.response_targets.initial_editor_response_days}-day target",
                timestamp=pr.created_at,
                tone=tone,
            )
        )
    if oldest_wait and current_wait_hours is not None:
        days = current_wait_hours / 24
        shared_reasons.append(
            Reason(
                "waiting-on-editor",
                "Contributor activity awaiting editor response",
                detail=f"{days:.1f} days",
                timestamp=latest_contributor,
                tone="attention" if days < config.response_targets.initial_editor_response_days else "urgent",
            )
        )
    if ready_bounded:
        shared_reasons.append(Reason("ready-bounded", "Appears ready and bounded", tone="positive"))
        shared_reasons.extend(positive_reasons)

    waiting_reason = _waiting_reason(pr, blockers, waiting_on_editor)
    # GitHub reports NONE for an author with no prior merged contribution to the
    # repository; whatwg/html never returns the FIRST_TIME* values in practice, so
    # checking only for those left this signal permanently false.
    first_time = pr.author_association in {"FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE"}

    return PRAnalysis(
        pr=pr,
        checklist=checklist,
        shared_lanes=tuple(shared_lanes),
        shared_reasons=tuple(shared_reasons),
        perspectives=perspectives,
        blockers=tuple(blockers),
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
    """The lanes every perspective shares, keyed by lane."""
    values = list(analyses)

    def key(analysis: PRAnalysis) -> str:
        return f"{repository_slug}#{analysis.pr.number}"

    # `oldest_wait`, `reply_window` and `overdue` sort oldest-first because fairness
    # to waiting contributors is exactly what they measure. For `reply_window` that
    # ordering is deadline proximity, which coincides with oldest-first only because
    # every member shares one target.
    reply_window = sorted(
        (analysis for analysis in values if "reply_window" in analysis.shared_lanes),
        key=lambda analysis: (analysis.pr.created_at, analysis.pr.number),
    )
    overdue = sorted(
        (analysis for analysis in values if "overdue" in analysis.shared_lanes),
        key=lambda analysis: (analysis.pr.created_at, analysis.pr.number),
    )
    oldest_wait = sorted(
        (analysis for analysis in values if "oldest_wait" in analysis.shared_lanes),
        key=lambda analysis: (-(analysis.current_wait_hours or 0), analysis.pr.number),
    )
    ready = sorted(
        (analysis for analysis in values if "ready_bounded" in analysis.shared_lanes),
        key=lambda analysis: (analysis.pr.changed_lines, analysis.pr.changed_files, analysis.pr.created_at),
    )
    all_items = sorted(values, key=lambda analysis: (analysis.pr.updated_at, analysis.pr.number), reverse=True)

    return {
        "reply_window": [key(value) for value in reply_window],
        "overdue": [key(value) for value in overdue],
        "oldest_wait": [key(value) for value in oldest_wait],
        "ready_bounded": [key(value) for value in ready],
        "all": [key(value) for value in all_items],
    }


def build_perspective_lanes(
    analyses: Iterable[PRAnalysis],
    config: DashboardConfig,
) -> dict[str, dict[str, list[str]]]:
    """The attention lanes, keyed by perspective and then by lane."""
    values = list(analyses)
    slug = config.repository.slug

    # Newest first for every attention lane: on a decade-old backlog, age is a poor
    # proxy for actionability. `active` has no signal timestamp of its own, so it
    # falls through to the PR's own update time, which is what defines the lane.
    def ordered(perspective: str, lane: str, timestamp) -> list[str]:
        selected = [
            analysis for analysis in values if lane in analysis.perspectives[perspective].lanes
        ]
        selected.sort(
            key=lambda analysis: (
                timestamp(analysis.perspectives[perspective]) or analysis.pr.updated_at,
                analysis.pr.number,
            ),
            reverse=True,
        )
        return [f"{slug}#{analysis.pr.number}" for analysis in selected]

    return {
        perspective: {
            "active": ordered(perspective, "active", lambda view: None),
            "direct": ordered(perspective, "direct", lambda view: view.direct_request_at),
            "stale_direct": ordered(perspective, "stale_direct", lambda view: view.stale_direct_at),
            "rereview": ordered(perspective, "rereview", lambda view: view.rereview_trigger_at),
        }
        for perspective in perspective_keys(config)
    }

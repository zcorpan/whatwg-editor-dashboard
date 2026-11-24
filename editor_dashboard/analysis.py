from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .checklist import Checklist, parse_checklist
from .config import DashboardConfig
from .models import Activity, DependencyState, PullRequestSnapshot, isoformat
from .references import dependency_references


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
PERSPECTIVE_LANES = ("active", "direct", "stale_direct", "rereview", "land", "routed")
SHARED_LANES = ("reply_window", "overdue", "oldest_wait", "ready_bounded", "stale_author", "triage", "all")


def perspective_keys(config: DashboardConfig) -> tuple[str, ...]:
    """Every queue perspective, in the order the browser offers them: the union of the
    editors, then every editor and helper."""
    return (ALL_EDITORS, *sorted(config.people))


@dataclass(frozen=True)
class PerspectiveView:
    """One PR's analysis as seen by one editor, or by all of them at once."""

    lanes: tuple[str, ...]
    reasons: tuple[Reason, ...]
    direct_request_at: datetime | None
    stale_direct_at: datetime | None
    rereview_trigger_at: datetime | None
    land_at: datetime | None
    has_attention_signal: bool
    latest_review: Activity | None
    # The item reads as the reviewers' move, but not this person's: it is their own,
    # they replied after the other side last moved, or a helper is iterating on it
    # with the author. Only the last applies to ALL_EDITORS.
    others_move: bool
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
            "others_move": self.others_move,
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
    # The first reply by an editor, which is what the editor impact figures count.
    first_editor_response: Activity | None
    first_editor_response_known: bool
    first_editor_response_hours: float | None
    waiting_on: WaitingOn
    outstanding: tuple[Outstanding, ...]
    suggested_labels: tuple[SuggestedLabel, ...]
    first_time_contributor: bool
    age_hours: float
    linked_issues: tuple[LinkedIssue, ...] = ()
    # The first reply by an editor or helper, which is what answers the contributor:
    # the reply window, the overdue lane and the health view's PR figures.
    first_response: Activity | None = None
    first_response_known: bool = False
    first_response_hours: float | None = None

    kind = "pr"

    @property
    def number(self) -> int:
        return self.pr.number

    @property
    def updated_at(self) -> datetime:
        return self.pr.updated_at

    @property
    def ready_at(self) -> datetime:
        return self.pr.ready_for_review_at

    def lanes_for(self, perspective: str) -> tuple[str, ...]:
        return (*self.perspectives[perspective].lanes, *self.shared_lanes)

    def reasons_for(self, perspective: str) -> tuple[Reason, ...]:
        return (*self.perspectives[perspective].reasons, *self.shared_reasons)

    def to_public_dict(self, repository_slug: str) -> dict[str, Any]:
        pr = self.pr
        return {
            "key": f"{repository_slug}#{pr.number}",
            "kind": "pr",
            "number": pr.number,
            "title": pr.title,
            "url": pr.url,
            "author": pr.author,
            "author_association": pr.author_association,
            "first_time_contributor": self.first_time_contributor,
            "created_at": isoformat(pr.created_at),
            "ready_for_review_at": isoformat(pr.ready_for_review_at),
            "updated_at": isoformat(pr.updated_at),
            "age_hours": round(self.age_hours, 2),
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
            "first_response_at": isoformat(self.first_response.created_at if self.first_response else None),
            "first_response_by": self.first_response.author if self.first_response else None,
            "first_response_known": self.first_response_known,
            "waiting_on": self.waiting_on.to_public_dict(),
            "outstanding": [{"party": item.party, "label": item.label} for item in self.outstanding],
            "suggested_labels": [suggestion.to_public_dict() for suggestion in self.suggested_labels],
            "linked_issues": [issue.to_public_dict() for issue in self.linked_issues],
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


def _responders(pr: PullRequestSnapshot, config: DashboardConfig) -> frozenset[str]:
    """Whose reply answers the author: the editors, and the helpers apart from the author.

    A helper's reply hands the item to its author as an editor's does, but a helper's
    own PR still waits on the editors. The response metrics stay the editors' own.
    """
    return config.editors | (config.helpers - {pr.author})


def _contributor_activity_times(
    pr: PullRequestSnapshot,
    editors: frozenset[str],
    responders: frozenset[str] | None = None,
) -> list[datetime]:
    responders = editors if responders is None else responders
    times: list[datetime] = []
    author_is_contributor = pr.author not in editors and not is_bot(pr.author)
    if author_is_contributor:
        times.append(pr.created_at)
        if pr.last_edited_at:
            times.append(pr.last_edited_at)

    # Marking a draft ready is a request for review, so it starts the wait again
    # even when an editor had commented on the draft.
    if _readied_by(pr, editors) == "contributor":
        times.append(pr.ready_for_review_at)

    head_author = pr.head_author or pr.author
    if pr.head_committed_at and head_author not in editors and not is_bot(head_author):
        times.append(pr.head_committed_at)

    for activity in pr.timeline:
        if activity.author not in responders and not is_bot(activity.author):
            times.append(activity.updated_at)
    return times


def _editor_activity_times(
    pr: PullRequestSnapshot,
    editors: frozenset[str],
    responders: frozenset[str] | None = None,
) -> list[datetime]:
    responders = editors if responders is None else responders
    times = [
        activity.updated_at
        for activity in pr.timeline
        if activity.author in responders and not is_bot(activity.author)
    ]
    if pr.head_committed_at and pr.head_author in editors and not is_bot(pr.head_author):
        times.append(pr.head_committed_at)
    if _readied_by(pr, editors) == "editor":
        times.append(pr.ready_for_review_at)
    return times


def _readied_by(pr: PullRequestSnapshot, editors: frozenset[str]) -> str | None:
    """Whose activity marking the PR ready for review was, if it was ever a draft."""
    if pr.ready_for_review_at == pr.created_at:
        return None
    actor = pr.ready_for_review_by
    if is_bot(actor):
        return None
    return "editor" if actor in editors else "contributor"


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

    if not is_normative(pr):
        # The PR template asks only normative changes to fill in its checklist.
        pass
    elif checklist.total:
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


@dataclass(frozen=True)
class WaitingOn:
    """Whose move a pull request is, and the evidence for saying so."""

    party: str
    # Logins for people; canonical URLs for a `dependency`. May be empty: "the
    # editors" with nobody in particular yet.
    who: tuple[str, ...]
    since: datetime | None
    reason: Reason
    # Helpers who took part and haven't approved since: until they do, the PR is
    # theirs and the author's to iterate on, and no editor's move.
    helpers_reviewing: tuple[str, ...] = ()
    # Helpers whose latest word is an approval, which hands the PR to the editors.
    helpers_approved: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "party": self.party,
            "who": list(self.who),
            "since": isoformat(self.since),
            "reason": self.reason.to_public_dict(),
            "helpers_reviewing": list(self.helpers_reviewing),
            "helpers_approved": list(self.helpers_approved),
        }


@dataclass(frozen=True)
class LinkedIssue:
    """An open issue a PR says it fixes, folded into the PR's card.

    Built by build.py from the issue's own analysis, so that analysis.py does not
    need to know how issues are read.
    """

    number: int
    title: str
    url: str
    labels: tuple[str, ...]
    # Per editor: the issue's direct signals, already worded for the PR card.
    direct: dict[str, tuple[Reason, ...]]
    expired: dict[str, tuple[Reason, ...]]
    # Per editor: the issue's own content fingerprint, so new discussion on the issue
    # resurfaces an addressed PR.
    fingerprints: dict[str, str]

    def to_public_dict(self) -> dict[str, Any]:
        return {"number": self.number, "title": self.title, "url": self.url, "labels": list(self.labels)}


@dataclass(frozen=True)
class Outstanding:
    """An unticked item of the whatwg/html PR template, and who it waits on."""

    party: str
    label: str


@dataclass(frozen=True)
class SuggestedLabel:
    label: str
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        return {"label": self.label, "reason": self.reason}


# The PR template's items, matched by wording in this order (the tests item is last,
# because "tests" is the vaguest pattern). Implementation bugs, the AAM/ARIA item, the
# MDN issue and the commit message are all things whoever is landing the PR can do,
# which is usually the author.
_TEMPLATE_ITEMS = (
    (re.compile(r"implementers? (?:are|is) interested", re.IGNORECASE), "implementers", "Implementer interest"),
    (re.compile(r"implementation bugs", re.IGNORECASE), "author", "Implementation bugs"),
    (re.compile(r"\bAAM\b|ARIA in HTML", re.IGNORECASE), "author", "HTML AAM and ARIA in HTML"),
    (re.compile(r"\bMDN\b", re.IGNORECASE), "author", "MDN issue"),
    (re.compile(r"commit message", re.IGNORECASE), "author", "Commit message"),
    (re.compile(r"\btests?\b", re.IGNORECASE), "tests", "Tests"),
)
_EDITORIAL_TITLE_RE = re.compile(r"^\s*(?:editorial|meta)\s*:", re.IGNORECASE)
_WPT_PR_RE = re.compile(r"github\.com/web-platform-tests/wpt/pull/\d+", re.IGNORECASE)
_REMOVAL_TITLE_RE = re.compile(r"^\s*(?:remove|deprecate|drop|disallow)\b", re.IGNORECASE)

_PARTY_LABELS = {
    "editors": "Waiting on editors",
    "author": "Waiting on the author",
    "implementers": "Waiting on implementer interest",
    "tests": "Waiting on tests",
    "compat": "Waiting on compat analysis",
    "meeting": "Waiting on the WHATNOT meeting",
    "dependency": "Waiting on a dependency",
    "merge": "Approved, ready to land",
    "proposal": "Waiting on a concrete proposal",
    "fix": "A fix is proposed",
}
_PARTY_TONES = {
    "editors": "attention",
    "merge": "positive",
    "dependency": "muted",
    "author": "muted",
    "implementers": "muted",
    "tests": "muted",
    "compat": "muted",
    "meeting": "muted",
    "proposal": "muted",
    "fix": "positive",
}


def is_normative(pr: PullRequestSnapshot) -> bool:
    """Not an `Editorial:` or `Meta:` change, which the PR template exempts from its checklist."""
    if _EDITORIAL_TITLE_RE.match(pr.title or ""):
        return False
    return not ({label.lower() for label in pr.labels} & {"editorial", "meta"})


def outstanding_items(pr: PullRequestSnapshot, checklist: Checklist) -> tuple[Outstanding, ...]:
    if not is_normative(pr):
        return ()
    result: list[Outstanding] = []
    for item in checklist.items:
        if item.checked:
            continue
        for pattern, party, label in _TEMPLATE_ITEMS:
            if pattern.search(item.label):
                result.append(Outstanding(party, label))
                break
    return tuple(result)


def _short_reference(url: str, repository_slug: str) -> str:
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/(?:pull|issues)/(\d+)", url)
    if not match:
        return url
    slug, number = match.groups()
    return f"#{number}" if slug.lower() == repository_slug.lower() else f"{slug}#{number}"


def _latest_author_move(pr: PullRequestSnapshot, editors: frozenset[str]) -> datetime:
    """When the author last moved: opened, readied, edited, pushed, or replied.

    A commit an editor pushed to the branch is not the author's move.
    """
    author = pr.author
    times: list[datetime] = [pr.ready_for_review_at, pr.created_at]
    if pr.last_edited_at:
        times.append(pr.last_edited_at)
    head_author = pr.head_author or author
    if pr.head_committed_at and (head_author == author or head_author not in editors):
        times.append(pr.head_committed_at)
    for activity in (*pr.timeline, *pr.reviews):
        if activity.author == author and not is_bot(activity.author) and activity.state != "PENDING":
            times.append(activity.updated_at)
    return max(times)


def latest_activity_by(activities: Iterable[Activity], login: str) -> datetime | None:
    """When ``login`` last commented or reviewed. A pending review is not public yet."""
    return max(
        (activity.updated_at for activity in activities if activity.author == login and activity.state != "PENDING"),
        default=None,
    )


def pending_direct(direct: Iterable[Reason], *, answered_at: datetime | None, answered: bool, own: bool) -> list[Reason]:
    """The direct signals the person has not answered yet.

    A mention is answered by any later reply. A review request or an assignment has no
    time of its own (it carries the item's update time), so it counts as answered once
    the person has replied since the other side last moved. Nobody is asking the
    author for their own item.
    """
    pending: list[Reason] = []
    for reason in direct:
        if reason.code.startswith("mentioned"):
            if answered_at is None or (reason.timestamp is not None and reason.timestamp > answered_at):
                pending.append(reason)
        elif not own and not answered:
            pending.append(reason)
    return pending


def _waiting_on(
    pr: PullRequestSnapshot,
    config: DashboardConfig,
    outstanding: tuple[Outstanding, ...],
    dependency_states: dict[str, DependencyState],
) -> tuple[WaitingOn, list[Reason]]:
    """Whose move it is, first matching rule wins, plus any notes the rules left.

    Labels come first because they are an editor's deliberate statement. A merge
    conflict or failing checks never moves the ball: they are common, often
    trivial, and fixable by whoever lands the PR, so they stay chips.
    """
    notes: list[Reason] = []
    labels = {label.lower() for label in pr.labels}
    slug = config.repository.slug
    editors = config.editors
    author = pr.author

    def wait(
        party: str,
        who: Iterable[str] = (),
        since: datetime | None = None,
        detail: str | None = None,
        *,
        helpers: tuple[tuple[str, ...], tuple[str, ...]] = ((), ()),
    ) -> WaitingOn:
        who = tuple(who)
        label = _PARTY_LABELS[party]
        if party == "editors" and who:
            label = "Waiting on " + ", ".join(f"@{login}" for login in who)
        elif party == "dependency" and who:
            label = "Waiting on " + ", ".join(_short_reference(url, slug) for url in who)
        reason = Reason(f"waiting-{party}", label, detail=detail, timestamp=since, tone=_PARTY_TONES[party])
        return WaitingOn(party, who, since, reason, helpers_reviewing=helpers[0], helpers_approved=helpers[1])

    meeting = sorted(label for label in pr.labels if label.lower() in config.waiting_on.meeting_labels)
    if meeting:
        return wait("meeting", detail=", ".join(meeting)), notes
    for label, party in config.waiting_on.party_labels:
        if label in labels:
            return wait(party, detail=label), notes

    held = sorted(label for label in pr.labels if label.lower() in config.waiting_on.hold_labels)
    if held:
        references = dependency_references(pr.body, slug, pr.number)
        known = [dependency_states[url] for url in references if url in dependency_states]
        pending = [state.url for state in known if state.pending]
        if pending:
            return wait("dependency", pending, detail=", ".join(held)), notes
        if known:
            notes.append(
                Reason(
                    "dependency-landed",
                    "Dependency landed",
                    detail=", ".join(_short_reference(state.url, slug) for state in known),
                    tone="positive",
                )
            )
        else:
            # Nothing says why it is held, so somebody has to look.
            notes.append(Reason("held-without-dependency", f"Held by {', '.join(held)}", detail="No dependency named", tone="warning"))

    # Who acted last, the author or an editor or helper other than the author. Other
    # people's comments are discussion, not a move.
    responders = _responders(pr, config)
    latest_author = _latest_author_move(pr, responders)
    reviewer_activity = [
        activity
        for activity in (*pr.timeline, *pr.reviews)
        if not is_bot(activity.author)
        and activity.state != "PENDING"
        and activity.author != author
        and activity.author in responders
    ]
    # A helper and the author iterate until the helper approves; then the editors
    # review and land it. A helper's latest word decides which: an approval hands
    # the PR to the editors and settles what the helper said before it.
    helper_latest: dict[str, Activity] = {}
    for activity in reviewer_activity:
        if activity.author in editors:
            continue
        current_latest = helper_latest.get(activity.author)
        if current_latest is None or (activity.updated_at, activity.id) > (current_latest.updated_at, current_latest.id):
            helper_latest[activity.author] = activity
    approved_by_helpers = sorted(login for login, activity in helper_latest.items() if activity.state == "APPROVED")
    # Only while no editor has spoken since: a PR the editors are reviewing too is
    # theirs, and one comment from a helper doesn't take it over.
    latest_editor_word = max(
        (activity.updated_at for activity in reviewer_activity if activity.author in editors),
        default=None,
    )
    reviewing = sorted(
        (
            login
            for login, activity in helper_latest.items()
            if login not in approved_by_helpers and (latest_editor_word is None or activity.updated_at > latest_editor_word)
        ),
        key=lambda login: (-helper_latest[login].updated_at.timestamp(), login),
    )
    helpers = (tuple(reviewing), tuple(approved_by_helpers))
    reviewer_activity = [activity for activity in reviewer_activity if activity.author not in approved_by_helpers]

    # Each editor's latest verdict: a plain comment neither grants nor revokes one.
    verdicts: dict[str, Activity] = {}
    for review in pr.reviews:
        if review.author in editors and review.author != author and review.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            current = verdicts.get(review.author)
            if current is None or (review.created_at, review.id) > (current.created_at, current.id):
                verdicts[review.author] = review

    blocking = [review for review in verdicts.values() if review.state == "CHANGES_REQUESTED"]
    if blocking:
        latest_block = max(review.created_at for review in blocking)
        if latest_author <= latest_block:
            requesters = sorted({review.author for review in blocking if review.author})
            return wait("author", since=latest_block, detail="changes requested by " + ", ".join(f"@{login}" for login in requesters)), notes

    def current(review: Activity) -> bool:
        if review.commit_oid and pr.head_oid:
            return review.commit_oid == pr.head_oid
        return pr.head_committed_at is None or review.created_at >= pr.head_committed_at

    approvers = sorted(
        login for login, review in verdicts.items() if review.state == "APPROVED" and current(review)
    )
    if approvers and not blocking:
        approved_at = max(verdicts[login].created_at for login in approvers)
        for party in ("implementers", "tests", "author"):
            missing = [item.label for item in outstanding if item.party == party]
            if missing:
                return wait(party, since=approved_at, detail="approved; not ticked: " + ", ".join(missing)), notes
        return wait("merge", approvers, since=approved_at, detail="approved by " + ", ".join(f"@{login}" for login in approvers)), notes

    if reviewer_activity:
        latest_reviewer = max(reviewer_activity, key=lambda activity: (activity.updated_at, activity.id))
        if latest_reviewer.updated_at > latest_author:
            return wait("author", since=latest_reviewer.updated_at, detail=f"since @{latest_reviewer.author} replied", helpers=helpers), notes

    approved_detail = "approved by " + ", ".join(f"@{login}" for login in approved_by_helpers) if approved_by_helpers else None
    explicit = sorted(
        {login for login in (*pr.review_requests, *pr.assignees) if login in responders and login != author and login not in approved_by_helpers}
    )
    if reviewing:
        who: Iterable[str] = reviewing
    elif explicit:
        who = explicit
    else:
        # The editors and helpers already in the conversation, most recent first.
        recent: dict[str, datetime] = {}
        for activity in reviewer_activity:
            if activity.author and activity.updated_at > recent.get(activity.author, datetime.min.replace(tzinfo=timezone.utc)):
                recent[activity.author] = activity.updated_at
        # A retired editor only takes what is asked of them directly.
        who = sorted(
            (login for login in recent if login not in config.retired_editors),
            key=lambda login: (-recent[login].timestamp(), login),
        )
    return wait("editors", who, since=latest_author, detail=approved_detail, helpers=helpers), notes


def suggested_labels(pr: PullRequestSnapshot, waiting_on: WaitingOn) -> tuple[SuggestedLabel, ...]:
    """Labels the public state argues for that the PR does not carry. Never applied.

    An unticked checklist item alone is not enough: on whatwg/html about half of all
    normative PRs leave implementer interest unticked, and editors label PRs for it
    rarely. The suggestion is made when it is what holds an approved PR up, which is
    when the label tells everyone it is no longer the editors' move.
    """
    if not is_normative(pr):
        return ()
    labels = {label.lower() for label in pr.labels}
    result: list[SuggestedLabel] = []
    if waiting_on.party == "tests" and "needs tests" not in labels and not _WPT_PR_RE.search(pr.body or ""):
        result.append(SuggestedLabel("needs tests", "Approved, but tests are not ticked and no web-platform-tests PR is linked"))
    if waiting_on.party == "implementers" and "needs implementer interest" not in labels:
        result.append(SuggestedLabel("needs implementer interest", "Approved, but implementer interest is not ticked"))
    removal = "removal/deprecation" in labels or _REMOVAL_TITLE_RE.match(pr.title or "")
    if removal and not labels & {"compat", "needs compat analysis"}:
        result.append(
            SuggestedLabel(
                "needs compat analysis",
                "Removes or restricts existing behavior"
                if "removal/deprecation" not in labels
                else "Labeled removal/deprecation",
            )
        )
    return tuple(result)


def _content_fingerprint(
    pr: PullRequestSnapshot, editor: str, body_hash: str, linked: tuple[LinkedIssue, ...] = ()
) -> str:
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
    payload = {
        "number": pr.number,
        "title": pr.title,
        "body_hash": body_hash,
        # Always false now that drafts are dropped before analysis, but kept so
        # that leaving drafts out did not change every stored fingerprint.
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
    # Only present when there is something to hash, so a PR that fixes no open issue
    # keeps the fingerprint it had before issues were read at all.
    if linked:
        payload["linked_issues"] = [[issue.number, issue.fingerprints.get(editor)] for issue in linked]
    return _hash_payload(payload)


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
    waiting_on: WaitingOn,
    now: datetime,
    linked: tuple[LinkedIssue, ...] = (),
) -> PerspectiveView:
    """Turn one perspective's attention signals into its lanes, evidence and hashes.

    ``editor`` is None for the union of every editor, which gets no fingerprints.
    """
    # Recency is the primary axis: the top of the queue answers "which reviews is
    # this editor currently in the middle of", not "which claim on their attention
    # is oldest".
    activity_cutoff = now - timedelta(days=config.attention.activity_window_days)
    # While a helper iterates with the author, the PR is theirs, not any editor's, and
    # theirs to follow even if they have only commented.
    with_helper = bool(waiting_on.helpers_reviewing) and editor not in waiting_on.helpers_reviewing
    involved = bool(direct) or bool(rereview) or latest_review is not None or editor in waiting_on.helpers_reviewing
    if editor is None:
        own = answered = False
        pending = direct
        editors_turn = waiting_on.party == "editors" and not with_helper
        merge_turn = waiting_on.party == "merge"
    else:
        # Read as one person, "waiting on the editors" is somebody else's move when the
        # PR is their own, or when they replied after the author last moved, which only
        # happens when the reply didn't hand the PR back: a helper's approval.
        own = pr.author == editor
        author_moved_at = _latest_author_move(pr, _responders(pr, config))
        answered_at = latest_activity_by((*pr.timeline, *pr.reviews), editor)
        if own:
            # Pushing, editing or opening the PR answers a mention as well as a reply
            # does, including one on the issue the PR fixes.
            answered_at = max(filter(None, (answered_at, author_moved_at)))
        answered = answered_at is not None and answered_at > author_moved_at
        pending = pending_direct(direct, answered_at=answered_at, answered=answered, own=own)
        editors_turn = waiting_on.party == "editors" and not own and not answered and not with_helper
        merge_turn = waiting_on.party == "merge" and editor in waiting_on.who
    # Recent, and something this editor can act on: a PR that recently moved to its
    # author or to implementers is not work in progress for the editor, so it no
    # longer leads the queue. Nor is one that waits on another editor's verdict or
    # merge. An unanswered direct request always is.
    actionable = bool(pending) or editors_turn or merge_turn
    is_active = bool(pr.updated_at >= activity_cutoff and involved and actionable)

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
    # A stale mention or a re-review only claims attention while the move is the
    # editors' (this editor's, read as one person). Once the PR waits on its author, an
    # implementer or a dependency, both used to keep it in the queue anyway, which is
    # most of what made those lanes long.
    if direct:
        lanes.append("direct")
    elif expired and editors_turn:
        lanes.append("stale_direct")
    if rereview and editors_turn:
        lanes.append("rereview")
    if merge_turn:
        lanes.append("land")
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
        land_at=waiting_on.since if "land" in lanes else None,
        has_attention_signal=bool(direct or rereview),
        latest_review=latest_review,
        others_move=(
            (own and waiting_on.party != "author")
            or (answered and waiting_on.party == "editors")
            or (with_helper and waiting_on.party in {"editors", "author"})
        ),
        content_fingerprint=_content_fingerprint(pr, editor, body_hash, linked) if editor else None,
        # Expired mentions stay in the hash so that a signal ageing out does not by
        # itself mark an already-seen item unseen again.
        attention_fingerprint=_attention_fingerprint((*direct, *expired, *rereview)) if editor else None,
    )


def analyze_pull_request(
    pr: PullRequestSnapshot,
    config: DashboardConfig,
    *,
    now: datetime,
    dependency_states: dict[str, DependencyState] | None = None,
    linked_issues: tuple[LinkedIssue, ...] = (),
) -> PRAnalysis:
    now = now.astimezone(timezone.utc)
    checklist = parse_checklist(pr.body)
    body_hash = hashlib.sha256(pr.body.encode("utf-8")).hexdigest()
    outstanding = outstanding_items(pr, checklist)
    waiting_on, waiting_notes = _waiting_on(pr, config, outstanding, dependency_states or {})

    # Every editor's direct and re-review signals, plus their own fingerprints, so
    # the browser can show the queue from any perspective and track seen/addressed
    # state for whoever the user says they are.
    # Helpers get their own perspective, but "all editors" is the union of the editors.
    editors = sorted(config.editors)
    signals: dict[str, tuple[list[Reason], list[Reason], list[Reason], Activity | None]] = {}
    for editor in sorted(config.people):
        direct, expired = _direct_reasons(pr, config, editor=editor, now=now)
        # A mention or assignment on an issue this PR fixes is a request about this PR.
        for issue in linked_issues:
            direct.extend(issue.direct.get(editor, ()))
            expired.extend(issue.expired.get(editor, ()))
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
            waiting_on=waiting_on,
            now=now,
            linked=linked_issues,
        )

    responders = _responders(pr, config)
    contributor_times = _contributor_activity_times(pr, config.editors, responders)
    editor_times = _editor_activity_times(pr, config.editors, responders)
    latest_contributor = max(contributor_times) if contributor_times else None
    latest_editor = max(editor_times) if editor_times else None
    waiting_on_editor = bool(
        latest_contributor
        and (latest_editor is None or latest_contributor > latest_editor)
        and pr.author not in config.editors
        and not is_bot(pr.author)
    )
    current_wait_hours = _hours_between(latest_contributor, now) if waiting_on_editor and latest_contributor else None

    first_editor, first_editor_known = _first_editor_response(pr, config.editors)
    # A helper's reply answers the contributor too; the editor figures keep their own.
    first_reply, first_reply_known = _first_editor_response(pr, responders)

    # Counted from when the PR became ready for review, like its age. An editor who
    # already replied to the draft has answered, so that clamps to zero.
    def hours_to(response: Activity | None, known: bool) -> float | None:
        return _hours_between(pr.ready_for_review_at, response.created_at) if response and known else None

    ready_bounded, positive_reasons, blockers = _ready_and_blockers(pr, checklist, config)
    # A PR that was a draft is only asking for review since it was last marked ready.
    age_hours = _hours_between(pr.ready_for_review_at, now)
    target_hours = _response_target_hours(config)

    # "Never received a first response", split on the response target into two
    # lanes with opposite meanings: inside the target a reply can still meet it, past
    # it the outcome is already fixed. The pair is deliberately unbounded by age — an
    # earlier seven-day cap dropped a PR on the very day it missed the target, so the
    # lane could only ever show successes in progress. Now it moves rather than
    # disappears.
    never_answered = bool(
        pr.author not in config.editors
        and not is_bot(pr.author)
        and first_reply is None
        and first_reply_known
    )
    in_reply_window = never_answered and age_hours <= target_hours
    first_response_overdue = never_answered and age_hours > target_hours
    # "Contributor activity no editor answered" is only an editor's wait if the move
    # is theirs: a contributor pinging a PR that needs implementer interest is not.
    oldest_wait = waiting_on_editor and waiting_on.party == "editors"
    stale_author = bool(
        waiting_on.party == "author"
        and waiting_on.since is not None
        and now - waiting_on.since > timedelta(days=config.waiting_on.stale_author_days)
    )

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
    if stale_author:
        shared_lanes.append("stale_author")
    shared_lanes.append("all")

    shared_reasons: list[Reason] = []
    if first_response_overdue:
        shared_reasons.append(
            Reason(
                "first-response-overdue",
                "First response overdue",
                detail=f"{age_hours / 24:.1f} days ready for review",
                timestamp=pr.ready_for_review_at,
                tone="urgent",
            )
        )
    elif in_reply_window:
        tone = "positive" if age_hours <= config.response_targets.highlight_new_hours else "attention"
        shared_reasons.append(
            Reason(
                "new-untriaged",
                "Awaiting a first response",
                detail=f"{(target_hours - age_hours) / 24:.1f} days left of the "
                f"{config.response_targets.initial_editor_response_days}-day target",
                timestamp=pr.ready_for_review_at,
                tone=tone,
            )
        )
    if oldest_wait and current_wait_hours is not None:
        days = current_wait_hours / 24
        shared_reasons.append(
            Reason(
                "waiting-on-editor",
                "Contributor activity awaiting a response",
                detail=f"{days:.1f} days",
                timestamp=latest_contributor,
                tone="attention" if days < config.response_targets.initial_editor_response_days else "urgent",
            )
        )
    if ready_bounded:
        shared_reasons.append(Reason("ready-bounded", "Appears ready and bounded", tone="positive"))
        shared_reasons.extend(positive_reasons)
    if stale_author:
        shared_reasons.append(
            Reason(
                "stale-author",
                "Waiting on the author for a long time",
                detail=f"{(now - waiting_on.since).days} days",
                timestamp=waiting_on.since,
                tone="muted",
            )
        )
    shared_reasons.extend(waiting_notes)
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
        first_editor_response_known=first_editor_known,
        first_editor_response_hours=hours_to(first_editor, first_editor_known),
        first_response=first_reply,
        first_response_known=first_reply_known,
        first_response_hours=hours_to(first_reply, first_reply_known),
        waiting_on=waiting_on,
        outstanding=outstanding,
        suggested_labels=suggested_labels(pr, waiting_on),
        linked_issues=linked_issues,
        first_time_contributor=first_time,
        age_hours=age_hours,
    )


def analyze_all(
    pull_requests: Iterable[PullRequestSnapshot],
    config: DashboardConfig,
    *,
    now: datetime,
    dependency_states: dict[str, DependencyState] | None = None,
    linked_issues: dict[int, tuple[LinkedIssue, ...]] | None = None,
) -> list[PRAnalysis]:
    linked_issues = linked_issues or {}
    return [
        analyze_pull_request(
            pr,
            config,
            now=now,
            dependency_states=dependency_states,
            linked_issues=linked_issues.get(pr.number, ()),
        )
        for pr in pull_requests
    ]


def build_lanes(analyses: Iterable[Any], repository_slug: str) -> dict[str, list[str]]:
    """The lanes every perspective shares, keyed by lane, over PRs and issues alike."""
    values = list(analyses)

    def key(analysis) -> str:
        return f"{repository_slug}#{analysis.number}"

    # `oldest_wait`, `reply_window` and `overdue` sort oldest-first because fairness
    # to waiting contributors is exactly what they measure. For `reply_window` that
    # ordering is deadline proximity, which coincides with oldest-first only because
    # every member shares one target.
    reply_window = sorted(
        (analysis for analysis in values if "reply_window" in analysis.shared_lanes),
        key=lambda analysis: (analysis.ready_at, analysis.number),
    )
    overdue = sorted(
        (analysis for analysis in values if "overdue" in analysis.shared_lanes),
        key=lambda analysis: (analysis.ready_at, analysis.number),
    )
    oldest_wait = sorted(
        (analysis for analysis in values if "oldest_wait" in analysis.shared_lanes),
        key=lambda analysis: (-(analysis.current_wait_hours or 0), analysis.number),
    )
    # Longest-abandoned first: the lane exists to clear out the backlog's oldest dead weight.
    stale_author = sorted(
        (analysis for analysis in values if "stale_author" in analysis.shared_lanes),
        key=lambda analysis: (analysis.waiting_on.since, analysis.number),
    )
    ready = sorted(
        (analysis for analysis in values if "ready_bounded" in analysis.shared_lanes),
        key=lambda analysis: (analysis.pr.changed_lines, analysis.pr.changed_files, analysis.pr.created_at),
    )
    # Oldest first, so that triage clears the window rather than chasing the newest.
    triage = sorted(
        (analysis for analysis in values if "triage" in analysis.shared_lanes),
        key=lambda analysis: (analysis.ready_at, analysis.number),
    )
    all_items = sorted(values, key=lambda analysis: (analysis.updated_at, analysis.number), reverse=True)

    return {
        "reply_window": [key(value) for value in reply_window],
        "overdue": [key(value) for value in overdue],
        "oldest_wait": [key(value) for value in oldest_wait],
        "ready_bounded": [key(value) for value in ready],
        "stale_author": [key(value) for value in stale_author],
        "triage": [key(value) for value in triage],
        "all": [key(value) for value in all_items],
    }


def build_perspective_lanes(
    analyses: Iterable[Any],
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
                timestamp(analysis.perspectives[perspective]) or analysis.updated_at,
                analysis.number,
            ),
            reverse=True,
        )
        return [f"{slug}#{analysis.number}" for analysis in selected]

    return {
        perspective: {
            "active": ordered(perspective, "active", lambda view: None),
            "direct": ordered(perspective, "direct", lambda view: view.direct_request_at),
            "stale_direct": ordered(perspective, "stale_direct", lambda view: view.stale_direct_at),
            "rereview": ordered(perspective, "rereview", lambda view: view.rereview_trigger_at),
            "land": ordered(perspective, "land", lambda view: view.land_at),
            # Filled in by build.py once routing has run over every item.
            "routed": [],
        }
        for perspective in perspective_keys(config)
    }

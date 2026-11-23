from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Activity:
    id: str
    kind: str
    author: str | None
    author_association: str | None
    created_at: datetime
    updated_at: datetime
    body: str
    url: str | None
    state: str | None = None
    commit_oid: str | None = None

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> "Activity | None":
        created_raw = node.get("submittedAt") or node.get("createdAt")
        created = parse_datetime(created_raw)
        if created is None:
            return None
        updated = parse_datetime(node.get("updatedAt")) or created
        typename = str(node.get("__typename") or "Activity")
        author_data = node.get("author") or {}
        commit_data = node.get("commit") or {}
        return cls(
            id=str(node.get("id") or f"{typename}:{created_raw}:{node.get('url', '')}"),
            kind=typename,
            author=(str(author_data.get("login")).lower() if author_data.get("login") else None),
            author_association=node.get("authorAssociation"),
            created_at=created,
            updated_at=updated,
            body=str(node.get("body") or ""),
            url=node.get("url"),
            state=node.get("state"),
            commit_oid=commit_data.get("oid"),
        )


@dataclass(frozen=True)
class ReviewThread:
    is_resolved: bool
    is_outdated: bool
    author: str | None = None

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> "ReviewThread":
        comments = [comment for comment in ((node.get("comments") or {}).get("nodes") or []) if comment]
        first = comments[0] if comments else {}
        login = (first.get("author") or {}).get("login")
        return cls(
            is_resolved=bool(node.get("isResolved")),
            is_outdated=bool(node.get("isOutdated")),
            author=(str(login).lower() if login else None),
        )

    @property
    def is_open(self) -> bool:
        return not self.is_resolved and not self.is_outdated


def _timeline_sample_complete(
    node: dict[str, Any],
    window_ids: dict[str, set[str]],
) -> bool:
    """Decide whether the sampled comment/review timeline holds every such item.

    ``timelineItems.totalCount`` reports every timeline item kind — commits, label
    changes, assignments — regardless of the ``itemTypes`` filter applied to
    ``nodes``. Comparing the filtered nodes against it therefore reports almost
    every PR as incompletely sampled, because virtually all of them have at least
    one commit. That silently degraded "no editor has responded" into "unknown".

    ``pageInfo.hasNextPage`` on the forward window answers the question directly.
    When it is absent (older fixtures), fall back on the fact that ``first: n`` and
    ``last: n`` over one connection return the same items exactly when the filtered
    total is at most ``n``.
    """
    page_info = (node.get("timelineFirst") or {}).get("pageInfo") or {}
    if "hasNextPage" in page_info:
        return not bool(page_info.get("hasNextPage"))
    return window_ids["timelineFirst"] == window_ids["timelineLast"]


def _ready_for_review(node: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """When, and by whom, the PR last switched from draft to ready for review.

    GitHub records no event for a PR opened ready for review, so (None, None) means
    it has been ready since it was created. Only the latest transition is fetched:
    a PR converted back to draft and readied again is waiting from the second time.
    """
    events = [
        event
        for event in ((node.get("readyForReview") or {}).get("nodes") or [])
        if event and parse_datetime(event.get("createdAt")) is not None
    ]
    if not events:
        return None, None
    latest = max(events, key=lambda event: parse_datetime(event.get("createdAt")))
    login = (latest.get("actor") or {}).get("login")
    return parse_datetime(latest.get("createdAt")), (str(login).lower() if login else None)


@dataclass(frozen=True)
class DependencyState:
    """The current state of something a held PR says it depends on."""

    url: str
    # OPEN, CLOSED or MERGED; MISSING when GitHub could not resolve the URL.
    state: str
    title: str | None = None

    @property
    def pending(self) -> bool:
        return self.state == "OPEN"

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "DependencyState":
        return cls(url=str(raw["url"]), state=str(raw.get("state") or "MISSING"), title=raw.get("title"))


@dataclass(frozen=True)
class PullRequestSnapshot:
    number: int
    title: str
    url: str
    author: str | None
    author_association: str | None
    body: str
    created_at: datetime
    updated_at: datetime
    last_edited_at: datetime | None
    closed_at: datetime | None
    merged_at: datetime | None
    is_draft: bool
    # When the PR last became ready for review: the latest ready-for-review event,
    # or creation for a PR that was never a draft. Its age counts from here.
    ready_for_review_at: datetime
    # Who marked it ready, or None when it was opened ready (or by a deleted user).
    ready_for_review_by: str | None
    additions: int
    deletions: int
    changed_files: int
    head_oid: str | None
    head_committed_at: datetime | None
    head_author: str | None
    mergeable: str | None
    merge_state_status: str | None
    review_decision: str | None
    status_state: str | None
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    review_requests: tuple[str, ...]
    timeline: tuple[Activity, ...]
    timeline_first_ids: frozenset[str]
    timeline_sampled_count: int
    timeline_total_count: int
    timeline_sample_complete: bool
    reviews: tuple[Activity, ...]
    reviews_total_count: int
    review_threads: tuple[ReviewThread, ...]
    unresolved_review_threads: int
    review_threads_total_count: int
    review_threads_sample_complete: bool

    @property
    def key(self) -> str:
        return f"#{self.number}"

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    def submitted_reviews_by(self, login: str) -> tuple[Activity, ...]:
        """Submitted (non-pending) reviews written by ``login``, oldest first."""
        return tuple(
            review
            for review in self.reviews
            if review.author == login and review.state != "PENDING"
        )

    def review_threads_started_by_others(self, login: str) -> tuple[int, int]:
        """Return (open, sampled) review-thread counts excluding ``login``'s own threads.

        The content fingerprint needs thread counts that the viewer's own inline
        comments cannot move; the published counts stay unfiltered.
        """
        others = [thread for thread in self.review_threads if thread.author != login]
        return sum(thread.is_open for thread in others), len(others)

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> "PullRequestSnapshot":
        created = parse_datetime(node.get("createdAt"))
        updated = parse_datetime(node.get("updatedAt"))
        if created is None or updated is None:
            raise ValueError(f"PR #{node.get('number')} is missing required timestamps")

        timeline_nodes: dict[str, dict[str, Any]] = {}
        window_ids: dict[str, set[str]] = {"timelineFirst": set(), "timelineLast": set()}
        timeline_total = 0
        for alias in ("timelineFirst", "timelineLast"):
            connection = node.get(alias) or {}
            timeline_total = max(timeline_total, int(connection.get("totalCount") or 0))
            for activity_node in connection.get("nodes") or []:
                if activity_node:
                    activity_id = str(activity_node.get("id") or repr(activity_node))
                    timeline_nodes[activity_id] = activity_node
                    window_ids[alias].add(activity_id)
        timeline_first_ids = window_ids["timelineFirst"]
        timeline = tuple(
            sorted(
                (activity for value in timeline_nodes.values() if (activity := Activity.from_graphql(value)) is not None),
                key=lambda activity: (activity.created_at, activity.id),
            )
        )

        timeline_sample_complete = _timeline_sample_complete(node, window_ids)

        reviews_connection = node.get("reviews") or {}
        reviews = tuple(
            sorted(
                (activity for value in (reviews_connection.get("nodes") or []) if value and (activity := Activity.from_graphql(value)) is not None),
                key=lambda activity: (activity.created_at, activity.id),
            )
        )

        labels = tuple(
            sorted(
                str(label.get("name"))
                for label in ((node.get("labels") or {}).get("nodes") or [])
                if label and label.get("name")
            )
        )
        assignees = tuple(
            sorted(
                str(user.get("login")).lower()
                for user in ((node.get("assignees") or {}).get("nodes") or [])
                if user and user.get("login")
            )
        )

        requested: list[str] = []
        for request in ((node.get("reviewRequests") or {}).get("nodes") or []):
            reviewer = (request or {}).get("requestedReviewer") or {}
            login = reviewer.get("login")
            if login:
                requested.append(str(login).lower())
            elif reviewer.get("slug"):
                organization = (reviewer.get("organization") or {}).get("login")
                team = str(reviewer.get("slug"))
                requested.append(f"{organization}/{team}" if organization else team)

        commits = node.get("commits") or {}
        commit_nodes = commits.get("nodes") or []
        commit = ((commit_nodes[-1] or {}).get("commit") if commit_nodes else None) or {}
        commit_author = ((commit.get("author") or {}).get("user") or {}).get("login")

        threads = node.get("reviewThreads") or {}
        thread_nodes = tuple(
            ReviewThread.from_graphql(thread) for thread in (threads.get("nodes") or []) if thread
        )
        unresolved = sum(thread.is_open for thread in thread_nodes)
        thread_total = int(threads.get("totalCount") or 0)

        status_rollup = node.get("statusCheckRollup") or {}
        author_data = node.get("author") or {}
        ready_at, ready_by = _ready_for_review(node)

        return cls(
            number=int(node["number"]),
            title=str(node.get("title") or ""),
            url=str(node.get("url") or ""),
            author=(str(author_data.get("login")).lower() if author_data.get("login") else None),
            author_association=node.get("authorAssociation"),
            body=str(node.get("body") or ""),
            created_at=created,
            updated_at=updated,
            last_edited_at=parse_datetime(node.get("lastEditedAt")),
            closed_at=parse_datetime(node.get("closedAt")),
            merged_at=parse_datetime(node.get("mergedAt")),
            is_draft=bool(node.get("isDraft")),
            ready_for_review_at=max(created, ready_at) if ready_at else created,
            ready_for_review_by=ready_by,
            additions=int(node.get("additions") or 0),
            deletions=int(node.get("deletions") or 0),
            changed_files=int(node.get("changedFiles") or 0),
            head_oid=node.get("headRefOid"),
            head_committed_at=parse_datetime(commit.get("committedDate")),
            head_author=(str(commit_author).lower() if commit_author else None),
            mergeable=node.get("mergeable"),
            merge_state_status=node.get("mergeStateStatus"),
            review_decision=node.get("reviewDecision"),
            status_state=status_rollup.get("state"),
            labels=labels,
            assignees=assignees,
            review_requests=tuple(sorted(set(requested))),
            timeline=timeline,
            timeline_first_ids=frozenset(timeline_first_ids),
            timeline_sampled_count=len(timeline),
            timeline_total_count=timeline_total,
            timeline_sample_complete=timeline_sample_complete,
            reviews=reviews,
            reviews_total_count=int(reviews_connection.get("totalCount") or 0),
            review_threads=thread_nodes,
            unresolved_review_threads=unresolved,
            review_threads_total_count=thread_total,
            review_threads_sample_complete=len(thread_nodes) >= thread_total,
        )


def snapshots_from_nodes(nodes: Iterable[dict[str, Any]]) -> list[PullRequestSnapshot]:
    return [PullRequestSnapshot.from_graphql(node) for node in nodes]


def _sampled_timeline(node: dict[str, Any]) -> tuple[tuple[Activity, ...], frozenset[str], int, bool]:
    """The merged first/last timeline windows: activities, first-window ids, total, completeness."""
    timeline_nodes: dict[str, dict[str, Any]] = {}
    window_ids: dict[str, set[str]] = {"timelineFirst": set(), "timelineLast": set()}
    total = 0
    for alias in ("timelineFirst", "timelineLast"):
        connection = node.get(alias) or {}
        total = max(total, int(connection.get("totalCount") or 0))
        for activity_node in connection.get("nodes") or []:
            if activity_node:
                activity_id = str(activity_node.get("id") or repr(activity_node))
                timeline_nodes[activity_id] = activity_node
                window_ids[alias].add(activity_id)
    timeline = tuple(
        sorted(
            (activity for value in timeline_nodes.values() if (activity := Activity.from_graphql(value)) is not None),
            key=lambda activity: (activity.created_at, activity.id),
        )
    )
    return timeline, frozenset(window_ids["timelineFirst"]), total, _timeline_sample_complete(node, window_ids)


@dataclass(frozen=True)
class LabelEvent:
    actor: str | None
    label: str
    created_at: datetime


@dataclass(frozen=True)
class IssueSnapshot:
    number: int
    title: str
    url: str
    state: str
    author: str | None
    author_association: str | None
    body: str
    created_at: datetime
    updated_at: datetime
    last_edited_at: datetime | None
    closed_at: datetime | None
    closed_by: str | None
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    timeline: tuple[Activity, ...]
    timeline_first_ids: frozenset[str]
    timeline_sample_complete: bool
    # The earliest label applications, oldest first: triage by labeling is a response.
    label_events: tuple[LabelEvent, ...]
    # Open pull requests that say they fix this issue.
    fixed_by: tuple[int, ...]

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> "IssueSnapshot":
        created = parse_datetime(node.get("createdAt"))
        updated = parse_datetime(node.get("updatedAt"))
        if created is None or updated is None:
            raise ValueError(f"Issue #{node.get('number')} is missing required timestamps")
        timeline, first_ids, _total, complete = _sampled_timeline(node)

        events: list[LabelEvent] = []
        for event in ((node.get("labeledEvents") or {}).get("nodes") or []):
            at = parse_datetime((event or {}).get("createdAt"))
            if at is None:
                continue
            actor = (event.get("actor") or {}).get("login")
            events.append(
                LabelEvent(
                    actor=str(actor).lower() if actor else None,
                    label=str((event.get("label") or {}).get("name") or ""),
                    created_at=at,
                )
            )
        closed_nodes = (node.get("closedEvents") or {}).get("nodes") or []
        closer = ((closed_nodes[-1] or {}).get("actor") or {}).get("login") if closed_nodes else None
        author = (node.get("author") or {}).get("login")
        return cls(
            number=int(node["number"]),
            title=str(node.get("title") or ""),
            url=str(node.get("url") or ""),
            state=str(node.get("state") or "OPEN"),
            author=str(author).lower() if author else None,
            author_association=node.get("authorAssociation"),
            body=str(node.get("body") or ""),
            created_at=created,
            updated_at=updated,
            last_edited_at=parse_datetime(node.get("lastEditedAt")),
            closed_at=parse_datetime(node.get("closedAt")),
            closed_by=str(closer).lower() if closer else None,
            labels=tuple(
                sorted(
                    str(label.get("name"))
                    for label in ((node.get("labels") or {}).get("nodes") or [])
                    if label and label.get("name")
                )
            ),
            assignees=tuple(
                sorted(
                    str(user.get("login")).lower()
                    for user in ((node.get("assignees") or {}).get("nodes") or [])
                    if user and user.get("login")
                )
            ),
            timeline=timeline,
            timeline_first_ids=first_ids,
            timeline_sample_complete=complete,
            label_events=tuple(sorted(events, key=lambda event: (event.created_at, event.label))),
            fixed_by=tuple(
                sorted(
                    int(pr["number"])
                    for pr in ((node.get("closedByPullRequestsReferences") or {}).get("nodes") or [])
                    if pr and pr.get("number") is not None
                )
            ),
        )


def issues_from_nodes(nodes: Iterable[dict[str, Any]]) -> list[IssueSnapshot]:
    return [IssueSnapshot.from_graphql(node) for node in nodes]


@dataclass(frozen=True)
class HistoryRecord:
    """One merged PR, reduced to who did what on it, for routing by expertise."""

    number: int
    author: str | None
    merged_at: datetime
    merged_by: str | None
    merge_oid: str | None
    labels: tuple[str, ...]
    # Labels of the issues it fixed: often the only topic a PR has.
    issue_labels: tuple[str, ...]
    # (login, submitted at) of each submitted review by somebody other than the author.
    reviews: tuple[tuple[str, datetime], ...]

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> "HistoryRecord | None":
        merged_at = parse_datetime(node.get("mergedAt"))
        if merged_at is None:
            return None

        def login(value: Any) -> str | None:
            name = (value or {}).get("login")
            return str(name).lower() if name else None

        author = login(node.get("author"))
        reviews = []
        for review in ((node.get("historyReviews") or {}).get("nodes") or []):
            reviewer = login((review or {}).get("author"))
            at = parse_datetime((review or {}).get("submittedAt"))
            if reviewer and at and reviewer != author and review.get("state") != "PENDING":
                reviews.append((reviewer, at))
        issue_labels = sorted(
            {
                str(label.get("name"))
                for issue in ((node.get("closingIssuesReferences") or {}).get("nodes") or [])
                for label in (((issue or {}).get("labels") or {}).get("nodes") or [])
                if label and label.get("name")
            }
        )
        return cls(
            number=int(node["number"]),
            author=author,
            merged_at=merged_at,
            merged_by=login(node.get("mergedBy")),
            merge_oid=(node.get("mergeCommit") or {}).get("oid"),
            labels=tuple(
                sorted(
                    str(label.get("name"))
                    for label in ((node.get("labels") or {}).get("nodes") or [])
                    if label and label.get("name")
                )
            ),
            issue_labels=tuple(issue_labels),
            reviews=tuple(sorted(reviews, key=lambda value: (value[1], value[0]))),
        )


def history_from_nodes(nodes: Iterable[dict[str, Any]]) -> list[HistoryRecord]:
    """One record per merged PR, in number order; later nodes for a number win."""
    records: dict[int, HistoryRecord] = {}
    for node in nodes:
        record = HistoryRecord.from_graphql(node)
        if record is not None:
            records[record.number] = record
    return [records[number] for number in sorted(records)]

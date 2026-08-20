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
    viewer_reviews: tuple[Activity, ...]
    viewer_reviews_total_count: int
    unresolved_review_threads: int
    review_threads_total_count: int
    review_threads_sample_complete: bool

    @property
    def key(self) -> str:
        return f"#{self.number}"

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

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

        reviews_connection = node.get("viewerReviews") or {}
        viewer_reviews = tuple(
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
        thread_nodes = [thread for thread in (threads.get("nodes") or []) if thread]
        unresolved = sum(not bool(thread.get("isResolved")) and not bool(thread.get("isOutdated")) for thread in thread_nodes)
        thread_total = int(threads.get("totalCount") or 0)

        status_rollup = node.get("statusCheckRollup") or {}
        author_data = node.get("author") or {}

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
            viewer_reviews=viewer_reviews,
            viewer_reviews_total_count=int(reviews_connection.get("totalCount") or 0),
            unresolved_review_threads=unresolved,
            review_threads_total_count=thread_total,
            review_threads_sample_complete=len(thread_nodes) >= thread_total,
        )


def snapshots_from_nodes(nodes: Iterable[dict[str, Any]]) -> list[PullRequestSnapshot]:
    return [PullRequestSnapshot.from_graphql(node) for node in nodes]

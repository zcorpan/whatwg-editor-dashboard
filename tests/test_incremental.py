from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from editor_dashboard.build import build_site
from editor_dashboard.config import load_config
from editor_dashboard.github import fetch_with_snapshot
from editor_dashboard.models import isoformat, parse_datetime
from editor_dashboard.state import (
    SNAPSHOT_FILENAME,
    Snapshot,
    fetch_fingerprint,
    full_fetch_reason,
    load_snapshot,
    save_snapshot,
)


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
RATE = {"cost": 1, "limit": 5000, "remaining": 4999, "resetAt": "2026-08-03T13:00:00Z"}


class FakeGitHub:
    """Answers every query the fetch layer makes from one mutable set of PRs.

    Each entry is the node a query would return plus its ``state``, so a full fetch
    and an incremental one read exactly the same repository.
    """

    def __init__(
        self,
        open_nodes: list[dict[str, Any]],
        closed_nodes: list[dict[str, Any]],
        issues: list[dict[str, Any]] = (),
    ) -> None:
        self.prs: dict[int, tuple[str, dict[str, Any]]] = {}
        for node in open_nodes:
            self.prs[int(node["number"])] = ("OPEN", node)
        for node in closed_nodes:
            self.prs[int(node["number"])] = ("MERGED" if node.get("mergedAt") else "CLOSED", node)
        # Issues carry their own state field, as the query asks for it.
        self.issues: dict[int, dict[str, Any]] = {int(node["number"]): node for node in issues}
        self.queries: list[str] = []

    def by_id(self) -> dict[str, tuple[str, dict[str, Any]]]:
        found = {node["id"]: (state, node) for state, node in self.prs.values()}
        found.update({node["id"]: (node["state"], node) for node in self.issues.values()})
        return found

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        name = re.search(r"query (\w+)", query).group(1)
        self.queries.append(name)
        if name == "OpenPullRequests":
            nodes = [copy.deepcopy(node) for state, node in self.prs.values() if state == "OPEN"]
            return {
                "repository": {
                    "pullRequests": {
                        "totalCount": len(nodes),
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                },
                "rateLimit": RATE,
            }
        if name == "RecentClosedPullRequests":
            cutoff = re.search(r"closed:>=(\S+)", variables["query"]).group(1)
            nodes = [
                copy.deepcopy(node)
                for state, node in self.prs.values()
                if state != "OPEN" and node["closedAt"][:10] >= cutoff
            ]
            return self._search(nodes)
        if name == "UpdatedSince":
            since = parse_datetime(re.search(r"updated:>=(\S+)", variables["query"]).group(1))
            everything = [("PullRequest", state, node) for state, node in self.prs.values()]
            everything += [("Issue", node["state"], node) for node in self.issues.values()]
            nodes = [
                {"__typename": kind, "id": node["id"], "number": node["number"], "state": state, "updatedAt": node["updatedAt"]}
                for kind, state, node in everything
                if parse_datetime(node["updatedAt"]) >= since
            ]
            return self._search(nodes)
        if name == "HistoryPullRequests":
            start, end = re.search(r"merged:(\S+)\.\.(\S+)", variables["query"]).groups()
            return self._search(
                [
                    copy.deepcopy(node)
                    for state, node in self.prs.values()
                    if state == "MERGED" and start <= node["mergedAt"][:10] <= end
                ]
            )
        if name == "OpenIssues":
            nodes = [copy.deepcopy(node) for node in self.issues.values() if node["state"] == "OPEN"]
            return {
                "repository": {
                    "issues": {
                        "totalCount": len(nodes),
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                },
                "rateLimit": RATE,
            }
        if name == "RecentClosedIssues":
            cutoff = re.search(r"closed:>=(\S+)", variables["query"]).group(1)
            return self._search(
                [
                    copy.deepcopy(node)
                    for node in self.issues.values()
                    if node["state"] != "OPEN" and node["closedAt"][:10] >= cutoff
                ]
            )
        if name == "DependencyStates":
            return {"rateLimit": RATE}
        lookup = self.by_id()
        if name == "PullRequestVolatileFields":
            nodes = []
            for identifier in variables["ids"]:
                if identifier not in lookup:
                    nodes.append(None)
                    continue
                state, node = lookup[identifier]
                nodes.append(
                    {
                        "id": identifier,
                        "number": node["number"],
                        "state": state,
                        "updatedAt": node["updatedAt"],
                        "mergeable": node.get("mergeable"),
                        "mergeStateStatus": node.get("mergeStateStatus"),
                        "reviewDecision": node.get("reviewDecision"),
                        "statusCheckRollup": copy.deepcopy(node.get("statusCheckRollup")),
                    }
                )
            return {"nodes": nodes, "rateLimit": RATE}
        if name == "IssueFreshness":
            return {
                "nodes": [
                    {key: lookup[identifier][1][key] for key in ("id", "number", "state", "updatedAt")}
                    if identifier in lookup
                    else None
                    for identifier in variables["ids"]
                ],
                "rateLimit": RATE,
            }
        if name in {"OpenPullRequestsById", "ClosedPullRequestsById", "PullRequestMergeability", "IssuesById"}:
            return {
                "nodes": [copy.deepcopy(lookup[identifier][1]) for identifier in variables["ids"]],
                "rateLimit": RATE,
            }
        raise AssertionError(f"Unexpected query {name}")

    @staticmethod
    def _search(nodes: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "search": {
                "issueCount": len(nodes),
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            "rateLimit": RATE,
        }


def published(config, data, now) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        payload = build_site(config, data, output_dir=directory, now=now)
    # How the data was fetched is allowed to differ; what it says is not.
    payload.pop("build")
    return json.loads(json.dumps(payload))


class IncrementalFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")
        cls.fixture = json.loads((ROOT / "fixtures" / "sample_api_data.json").read_text(encoding="utf-8"))

    def github(self) -> FakeGitHub:
        open_nodes = copy.deepcopy(self.fixture["open_pull_requests"])
        closed_nodes = copy.deepcopy(self.fixture["recently_closed_pull_requests"])
        for node in open_nodes + closed_nodes:
            node["id"] = f"PR_{node['number']}"
        return FakeGitHub(open_nodes, closed_nodes, copy.deepcopy(self.fixture["open_issues"]))

    def fetch(self, github: FakeGitHub, now: datetime, previous: Snapshot | None = None):
        reason = full_fetch_reason(previous, self.config, now=now) if previous else "test"
        return fetch_with_snapshot(
            self.config, "unused", now=now, previous=previous, full_reason=reason,
            client=github, sleep=lambda _: None,
        )

    def a_day_of_changes(self, github: FakeGitHub, later: datetime) -> None:
        stamp = isoformat(later - timedelta(hours=3))
        open_numbers = sorted(number for number, (state, _node) in github.prs.items() if state == "OPEN")

        # A new comment: the PR's updatedAt moves.
        _state, commented = github.prs[open_numbers[0]]
        commented["updatedAt"] = stamp
        commented["timelineLast"]["nodes"].append(
            {
                "__typename": "IssueComment",
                "id": "IC_new",
                "author": {"login": "contributor"},
                "authorAssociation": "NONE",
                "body": "Rebased.",
                "createdAt": stamp,
                "updatedAt": stamp,
            }
        )

        # A merge: the PR leaves the open set.
        _state, merged = github.prs[open_numbers[1]]
        merged.update({"updatedAt": stamp, "closedAt": stamp, "mergedAt": stamp})
        github.prs[open_numbers[1]] = ("MERGED", merged)

        # A new PR.
        new = copy.deepcopy(github.prs[open_numbers[2]][1])
        new.update({"id": "PR_13500", "number": 13500, "title": "Brand new", "createdAt": stamp, "updatedAt": stamp})
        new["readyForReview"] = {"nodes": []}
        github.prs[13500] = ("OPEN", new)

        # An issue gets a reply, another one closes, and a new one is opened.
        replied = github.issues[14001]
        replied["updatedAt"] = stamp
        replied["timelineLast"]["nodes"].append(
            {
                "__typename": "IssueComment",
                "id": "IC_reply",
                "author": {"login": "zcorpan"},
                "authorAssociation": "MEMBER",
                "body": "Good catch.",
                "createdAt": stamp,
                "updatedAt": stamp,
            }
        )
        github.issues[14005].update({"state": "CLOSED", "closedAt": stamp, "updatedAt": stamp})
        fresh = copy.deepcopy(github.issues[14001])
        fresh.update({"id": "I_14600", "number": 14600, "title": "New", "createdAt": stamp, "updatedAt": stamp})
        github.issues[14600] = fresh

        # The base branch moved: mergeability changed without touching updatedAt.
        _state, rebased = github.prs[open_numbers[3]]
        rebased["mergeable"] = "CONFLICTING"

    def test_an_incremental_build_publishes_what_a_full_build_would(self) -> None:
        github = self.github()
        _data, snapshot = self.fetch(github, NOW)
        later = NOW + timedelta(days=1)
        self.a_day_of_changes(github, later)

        incremental, next_snapshot = self.fetch(github, later, previous=snapshot)
        self.assertEqual(incremental.metadata.fetch_mode, "incremental")
        self.assertNotIn("OpenPullRequests", github.queries[github.queries.index("UpdatedSince"):])
        full, full_snapshot = self.fetch(github, later)

        self.assertEqual(published(self.config, incremental, later), published(self.config, full, later))
        self.assertEqual(next_snapshot.open_nodes, full_snapshot.open_nodes)
        self.assertEqual(next_snapshot.closed_nodes, full_snapshot.closed_nodes)
        self.assertEqual(next_snapshot.open_issue_nodes, full_snapshot.open_issue_nodes)
        self.assertEqual(next_snapshot.closed_issue_nodes, full_snapshot.closed_issue_nodes)
        # An incremental chain keeps the date of the last full fetch.
        self.assertEqual(next_snapshot.last_full_at, NOW)
        self.assertEqual(next_snapshot.fetched_at, later)

    def test_only_what_changed_is_refetched(self) -> None:
        github = self.github()
        _data, snapshot = self.fetch(github, NOW)
        later = NOW + timedelta(days=1)
        self.a_day_of_changes(github, later)

        incremental, _snapshot = self.fetch(github, later, previous=snapshot)

        # The commented PR, the merged PR and the new one; the rebased PR only has its
        # volatile fields patched.
        self.assertEqual(incremental.metadata.refetched_prs, 3)
        self.assertEqual(incremental.metadata.refetched_issues, 3)

    def test_closed_prs_age_out_like_the_full_search_drops_them(self) -> None:
        github = self.github()
        _data, snapshot = self.fetch(github, NOW)
        # Far enough on that some fixture PRs closed before the history window, but
        # still within one incremental chain.
        later = NOW + timedelta(days=5)
        snapshot.last_full_at = later - timedelta(days=1)
        oldest = min(
            (node for state, node in github.prs.values() if state != "OPEN"),
            key=lambda node: node["closedAt"],
        )
        oldest["closedAt"] = isoformat(later - timedelta(days=self.config.sampling.history_days + 1))
        snapshot.closed_nodes[int(oldest["number"])]["closedAt"] = oldest["closedAt"]

        incremental, next_snapshot = self.fetch(github, later, previous=snapshot)
        full, full_snapshot = self.fetch(github, later)

        self.assertNotIn(int(oldest["number"]), next_snapshot.closed_nodes)
        self.assertEqual(sorted(next_snapshot.closed_nodes), sorted(full_snapshot.closed_nodes))

    def test_too_many_changes_fall_back_to_a_full_fetch(self) -> None:
        github = self.github()
        _data, snapshot = self.fetch(github, NOW)
        original = github._search

        def capped(nodes):
            answer = original(nodes)
            answer["search"]["issueCount"] = 1001
            return answer

        github._search = capped
        github.queries.clear()
        data, _snapshot = self.fetch(github, NOW + timedelta(days=1), previous=snapshot)

        self.assertEqual(data.metadata.fetch_mode, "full")
        self.assertIn("1,000", data.metadata.full_fetch_reason)
        self.assertIn("OpenPullRequests", github.queries)


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")

    def snapshot(self, **changes) -> Snapshot:
        values = {
            "fingerprint": fetch_fingerprint(self.config),
            "fetched_at": NOW,
            "last_full_at": NOW,
            "open_nodes": {2: {"number": 2, "id": "b"}, 1: {"number": 1, "id": "a"}},
            "closed_nodes": {},
        }
        values.update(changes)
        return Snapshot(**values)

    def test_round_trips_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save_snapshot(directory, self.snapshot())
            first = (Path(directory) / SNAPSHOT_FILENAME).read_bytes()
            loaded = load_snapshot(directory)
            save_snapshot(directory, loaded)
            self.assertEqual((Path(directory) / SNAPSHOT_FILENAME).read_bytes(), first)
        self.assertEqual(loaded.open_nodes, self.snapshot().open_nodes)
        self.assertEqual(loaded.fetched_at, NOW)

    def test_missing_directory_has_no_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(load_snapshot(Path(directory) / "absent"))

    def test_reasons_for_a_full_fetch(self) -> None:
        later = NOW + timedelta(days=1)
        self.assertIsNone(full_fetch_reason(self.snapshot(), self.config, now=later))
        self.assertEqual(full_fetch_reason(None, self.config, now=later), "no stored snapshot")
        self.assertIn("changed", full_fetch_reason(self.snapshot(fingerprint="old"), self.config, now=later))
        self.assertIn("newer", full_fetch_reason(self.snapshot(), self.config, now=NOW - timedelta(hours=1)))
        stale = NOW + timedelta(days=self.config.incremental.full_every_days)
        self.assertIn("days old", full_fetch_reason(self.snapshot(), self.config, now=stale))

    def test_fingerprint_follows_the_queries_and_sampling(self) -> None:
        from dataclasses import replace

        changed = replace(self.config, sampling=replace(self.config.sampling, timeline_each_end=3))
        self.assertNotEqual(fetch_fingerprint(changed), fetch_fingerprint(self.config))
        # Thresholds only affect analysis, which reruns every build anyway.
        relaxed = replace(self.config, attention=replace(self.config.attention, activity_window_days=5))
        self.assertEqual(fetch_fingerprint(relaxed), fetch_fingerprint(self.config))


if __name__ == "__main__":
    unittest.main()

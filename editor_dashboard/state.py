"""The raw GitHub data one build leaves for the next, so that a daily build only has to
ask for what changed.

The snapshot holds GraphQL nodes exactly as the queries return them, drafts included,
and nothing derived: analysis reruns on every build, so a code change never needs a
full fetch. Only a change to what is *fetched* does, and the fingerprint catches that.

The file holds public PR descriptions and comments. It is build-time state kept in the
workflow's cache and is never copied into the published site (see PRIVACY.md).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DashboardConfig
from .models import isoformat, parse_datetime


SNAPSHOT_FILENAME = "snapshot.json.gz"
# Bump when the snapshot's own layout changes. Query changes are caught by the
# fingerprint without a bump.
SNAPSHOT_FORMAT = 2

_GRAPHQL_DIR = Path(__file__).resolve().parent.parent / "graphql"


def fetch_fingerprint(config: DashboardConfig) -> str:
    """What a stored node depends on: the queries, and the variables they are sent.

    Nodes fetched under a different query or sample size cannot be mixed with fresh
    ones, so any change here forces a full build.
    """
    queries = {path.name: path.read_text(encoding="utf-8") for path in sorted(_GRAPHQL_DIR.glob("*.graphql"))}
    payload = {
        "format": SNAPSHOT_FORMAT,
        "repository": config.repository.slug,
        "queries": queries,
        "timeline_each_end": config.sampling.timeline_each_end,
        "reviews_per_pr": config.sampling.reviews_per_pr,
        "review_threads": config.sampling.review_threads,
        "history_days": config.sampling.history_days,
        "issue_page_size": config.sampling.issue_page_size,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class Snapshot:
    fingerprint: str
    # When the fetch that produced this snapshot started. The next incremental build
    # asks for everything updated since then, less an overlap.
    fetched_at: datetime
    # When the last full fetch started. Incremental builds cannot see every change
    # (an edited comment does not move a PR's `updatedAt`), so a full one runs
    # periodically to put the snapshot right.
    last_full_at: datetime
    open_nodes: dict[int, dict[str, Any]] = field(default_factory=dict)
    closed_nodes: dict[int, dict[str, Any]] = field(default_factory=dict)
    open_issue_nodes: dict[int, dict[str, Any]] = field(default_factory=dict)
    closed_issue_nodes: dict[int, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "fetched_at": isoformat(self.fetched_at),
            "last_full_at": isoformat(self.last_full_at),
            "open_pull_requests": [self.open_nodes[number] for number in sorted(self.open_nodes)],
            "recently_closed_pull_requests": [self.closed_nodes[number] for number in sorted(self.closed_nodes)],
            "open_issues": [self.open_issue_nodes[number] for number in sorted(self.open_issue_nodes)],
            "recently_closed_issues": [self.closed_issue_nodes[number] for number in sorted(self.closed_issue_nodes)],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Snapshot":
        fetched_at = parse_datetime(raw.get("fetched_at"))
        last_full_at = parse_datetime(raw.get("last_full_at"))
        if fetched_at is None or last_full_at is None:
            raise ValueError("snapshot is missing its fetch times")
        return cls(
            fingerprint=str(raw.get("fingerprint") or ""),
            fetched_at=fetched_at,
            last_full_at=last_full_at,
            open_nodes={int(node["number"]): node for node in raw.get("open_pull_requests") or []},
            closed_nodes={int(node["number"]): node for node in raw.get("recently_closed_pull_requests") or []},
            open_issue_nodes={int(node["number"]): node for node in raw.get("open_issues") or []},
            closed_issue_nodes={int(node["number"]): node for node in raw.get("recently_closed_issues") or []},
        )


def load_snapshot(state_dir: str | Path) -> Snapshot | None:
    path = Path(state_dir) / SNAPSHOT_FILENAME
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return Snapshot.from_json(json.load(handle))


def save_snapshot(state_dir: str | Path, snapshot: Snapshot) -> Path:
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SNAPSHOT_FILENAME
    encoded = json.dumps(snapshot.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temporary = path.with_suffix(".tmp")
    # mtime=0 keeps the file byte-identical for identical content.
    with open(temporary, "wb") as raw_handle, gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as handle:
        handle.write(encoded.encode("utf-8"))
    temporary.replace(path)
    return path


def full_fetch_reason(
    snapshot: Snapshot | None,
    config: DashboardConfig,
    *,
    now: datetime,
) -> str | None:
    """Why this build cannot be incremental, or None when it can."""
    if snapshot is None:
        return "no stored snapshot"
    if snapshot.fingerprint != fetch_fingerprint(config):
        return "the queries or sampling changed since the stored snapshot"
    if snapshot.fetched_at > now:
        return "the stored snapshot is newer than this build"
    limit = timedelta(days=config.incremental.full_every_days)
    if now - snapshot.last_full_at >= limit:
        return f"the last full fetch is at least {config.incremental.full_every_days} days old"
    return None

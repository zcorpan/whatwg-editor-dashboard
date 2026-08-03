from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ALLOWED_SUGGESTED_LANES = {"rereview", "new", "oldest_wait", "ready_bounded"}

@dataclass(frozen=True)
class RepositoryConfig:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class ResponseTargets:
    initial_editor_response_days: int = 7
    highlight_new_hours: int = 48


@dataclass(frozen=True)
class ReadyBoundedConfig:
    max_changed_files: int = 8
    max_changed_lines: int = 800
    minimum_checklist_ratio: float = 1.0


@dataclass(frozen=True)
class SamplingConfig:
    # Keep outer pages small because each PR includes several nested connections.
    # GitHub terminates GraphQL requests that take too long, usually as HTTP 502/504.
    graphql_page_size: int = 10
    timeline_each_end: int = 25
    viewer_reviews: int = 25
    review_threads: int = 50
    history_days: int = 90


@dataclass(frozen=True)
class SuggestedNextConfig:
    cycle: tuple[str, ...] = ("rereview", "new", "oldest_wait", "ready_bounded")


@dataclass(frozen=True)
class DashboardConfig:
    repository: RepositoryConfig
    viewer: str
    editors: frozenset[str]
    response_targets: ResponseTargets = field(default_factory=ResponseTargets)
    ready_bounded: ReadyBoundedConfig = field(default_factory=ReadyBoundedConfig)
    blocking_labels: frozenset[str] = field(default_factory=frozenset)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    suggested_next: SuggestedNextConfig = field(default_factory=SuggestedNextConfig)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _positive_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def load_config(path: str | Path) -> DashboardConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw = _mapping(raw, "configuration")

    repo_raw = _mapping(raw.get("repository", {}), "repository")
    owner = str(repo_raw.get("owner", "")).strip()
    name = str(repo_raw.get("name", "")).strip()
    if not owner or not name:
        raise ValueError("repository.owner and repository.name are required")

    viewer = str(raw.get("viewer", "")).strip().lstrip("@").lower()
    if not viewer:
        raise ValueError("viewer is required")

    editors_raw = raw.get("editors", [])
    if not isinstance(editors_raw, list):
        raise ValueError("editors must be a list")
    editors = frozenset(str(value).strip().lstrip("@").lower() for value in editors_raw if str(value).strip())
    if viewer not in editors:
        editors = frozenset((*editors, viewer))

    targets_raw = _mapping(raw.get("response_targets", {}), "response_targets")
    targets = ResponseTargets(
        initial_editor_response_days=_positive_int(
            targets_raw.get("initial_editor_response_days", 7),
            "response_targets.initial_editor_response_days",
        ),
        highlight_new_hours=_positive_int(
            targets_raw.get("highlight_new_hours", 48),
            "response_targets.highlight_new_hours",
        ),
    )

    bounded_raw = _mapping(raw.get("ready_bounded", {}), "ready_bounded")
    ratio = float(bounded_raw.get("minimum_checklist_ratio", 1.0))
    if not 0 <= ratio <= 1:
        raise ValueError("ready_bounded.minimum_checklist_ratio must be between 0 and 1")
    bounded = ReadyBoundedConfig(
        max_changed_files=_positive_int(
            bounded_raw.get("max_changed_files", 8),
            "ready_bounded.max_changed_files",
        ),
        max_changed_lines=_positive_int(
            bounded_raw.get("max_changed_lines", 800),
            "ready_bounded.max_changed_lines",
        ),
        minimum_checklist_ratio=ratio,
    )

    sampling_raw = _mapping(raw.get("sampling", {}), "sampling")
    sampling = SamplingConfig(
        graphql_page_size=_positive_int(
            sampling_raw.get("graphql_page_size", 10),
            "sampling.graphql_page_size",
            maximum=100,
        ),
        timeline_each_end=_positive_int(
            sampling_raw.get("timeline_each_end", 25),
            "sampling.timeline_each_end",
            maximum=100,
        ),
        viewer_reviews=_positive_int(
            sampling_raw.get("viewer_reviews", 25),
            "sampling.viewer_reviews",
            maximum=100,
        ),
        review_threads=_positive_int(
            sampling_raw.get("review_threads", 50),
            "sampling.review_threads",
            maximum=100,
        ),
        history_days=_positive_int(
            sampling_raw.get("history_days", 90),
            "sampling.history_days",
        ),
    )

    suggested_raw = _mapping(raw.get("suggested_next", {}), "suggested_next")
    cycle_raw = suggested_raw.get("cycle", ["rereview", "new", "oldest_wait", "ready_bounded"])
    if not isinstance(cycle_raw, list) or not cycle_raw:
        raise ValueError("suggested_next.cycle must be a non-empty list")
    cycle = tuple(str(value).strip() for value in cycle_raw)
    unknown_lanes = sorted(set(cycle) - _ALLOWED_SUGGESTED_LANES)
    if unknown_lanes:
        raise ValueError(f"suggested_next.cycle contains unknown lanes: {', '.join(unknown_lanes)}")
    if len(cycle) != len(set(cycle)):
        raise ValueError("suggested_next.cycle must not contain duplicate lanes")

    blocking_labels_raw = raw.get("blocking_labels", [])
    if not isinstance(blocking_labels_raw, list):
        raise ValueError("blocking_labels must be a list")

    return DashboardConfig(
        repository=RepositoryConfig(owner=owner, name=name),
        viewer=viewer,
        editors=editors,
        response_targets=targets,
        ready_bounded=bounded,
        blocking_labels=frozenset(str(value).strip().lower() for value in blocking_labels_raw if str(value).strip()),
        sampling=sampling,
        suggested_next=SuggestedNextConfig(cycle),
    )

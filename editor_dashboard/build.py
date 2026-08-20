from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis import analyze_all, build_lanes
from .config import DashboardConfig
from .github import RepositoryData
from .metrics import build_metrics
from .models import isoformat


LANE_DESCRIPTIONS = {
    "active": {
        "title": "Active now",
        "description": "Recently changed PRs the editor is already involved in, newest activity first.",
    },
    "direct": {
        "title": "Direct requests",
        "description": "Current review requests and assignments, plus public mentions inside the activity window.",
    },
    "stale_direct": {
        "title": "Stale direct requests",
        "description": "Public mentions older than the activity window, kept findable but no longer claiming the queue.",
    },
    "rereview": {
        "title": "Re-review owed",
        "description": "The PR changed or the author replied after the editor's latest sampled review.",
    },
    "new": {
        "title": "Awaiting first response",
        "description": "Non-draft contributor PRs that have never received a sampled editor response, oldest first.",
    },
    "oldest_wait": {
        "title": "Oldest contributor waits",
        "description": "Time since the latest sampled non-editor human activity not followed by editor activity.",
    },
    "ready_bounded": {
        "title": "Ready and bounded",
        "description": "A deterministic quick-win heuristic: mergeable, no detected blocker, bounded diff, and sufficient description-checklist completion.",
    },
    "all": {
        "title": "All open PRs",
        "description": "Every open pull request ordered by latest public GitHub update.",
    },
}


def _methodology(config: DashboardConfig) -> dict[str, Any]:
    return {
        "principles": [
            "No LLM is used. Every classification is produced by deterministic, inspectable rules.",
            (
                "The queue leads with recent activity on PRs the editor is already involved in. "
                f"'Recent' means the latest {config.attention.activity_window_days} days."
            ),
            "The generated site contains public GitHub data only.",
            "Seen, addressed, pinned, snoozed, and opened state is stored only in the browser.",
            "Description checklist completion is descriptive and is not an assessment of test sufficiency or specification readiness.",
        ],
        "sampling": {
            "timeline": (
                f"The first and last {config.sampling.timeline_each_end} issue comments/reviews are sampled per PR. "
                "Metrics that require complete middle history are omitted when the sample is incomplete."
            ),
            "viewer_reviews": (
                f"The latest {config.sampling.viewer_reviews} reviews by @{config.viewer} are sampled per PR."
            ),
            "review_threads": (
                f"The first {config.sampling.review_threads} review threads are sampled for unresolved-thread detection."
            ),
            "history": f"Flow and impact metrics use the latest {config.sampling.history_days} days of closed PRs.",
        },
        "known_limitations": [
            "GitHub notification unread state is not fetched; the browser shows locally unseen public attention signals instead.",
            (
                "A public mention stops counting as a direct request once it falls outside the activity "
                "window; it moves to the stale lane rather than disappearing. Review requests and "
                "assignments do not expire because GitHub clears them on review."
            ),
            (
                "First-time contributor detection treats an author with no repository association as a "
                "first-time contributor, which also covers authors whose only prior PRs were closed unmerged."
            ),
            "Inline review-thread replies are not inspected for mentions or response-time metrics in this MVP.",
            "Review-request and assignment connections expose current state, not a complete timestamped history; their displayed timestamp uses the PR update time.",
            "The configured current editor list is applied to the full sampled history; historical editor-membership changes are not reconstructed.",
            "A PR description edit is attributed to the PR author because the sampled API data does not identify who edited the description.",
            "First-response clocks use PR creation time for non-draft PRs; this MVP does not reconstruct when a formerly draft PR became ready for review.",
            "A ready-and-bounded result is a prioritization hint, not a merge recommendation.",
            "No claim is made about actual test coverage, implementer interest, web compatibility, or consensus quality.",
        ],
    }


def build_site(
    config: DashboardConfig,
    repository_data: RepositoryData,
    *,
    output_dir: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    open_analyses = analyze_all(repository_data.open_pull_requests, config, now=now)
    closed_analyses = analyze_all(repository_data.recently_closed_pull_requests, config, now=now)
    lanes = build_lanes(open_analyses, config.repository.slug)
    metrics = build_metrics(open_analyses, closed_analyses, config, now=now)

    items = [
        analysis.to_public_dict(config.repository.slug)
        for analysis in sorted(open_analyses, key=lambda value: value.pr.number)
    ]

    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": isoformat(now),
        "repository": {
            "owner": config.repository.owner,
            "name": config.repository.name,
            "slug": config.repository.slug,
            "url": f"https://github.com/{config.repository.slug}/pulls",
        },
        "viewer": {
            "login": config.viewer,
            "display": f"@{config.viewer}",
        },
        "privacy": {
            "generated_data": "public-only",
            "local_state": ["seen", "addressed", "pinned", "snoozed", "opened", "queue_preferences"],
            "github_notifications_fetched": False,
        },
        "response_targets": {
            "initial_editor_response_days": config.response_targets.initial_editor_response_days,
            "highlight_new_hours": config.response_targets.highlight_new_hours,
        },
        "attention": {
            "activity_window_days": config.attention.activity_window_days,
        },
        "suggested_next": {
            "active": "all",
            "cycle": list(config.suggested_next.cycle),
        },
        "lane_descriptions": LANE_DESCRIPTIONS,
        "lanes": lanes,
        "items": items,
        "metrics": metrics,
        "methodology": _methodology(config),
        "build": repository_data.metadata.to_public_dict(),
    }

    web_dir = Path(__file__).resolve().parent.parent / "web"
    for source in web_dir.iterdir():
        if source.is_file():
            shutil.copy2(source, output / source.name)
    (output / ".nojekyll").write_text("", encoding="utf-8")
    (output / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return payload

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assignments import apply_assignments
from .issue_analysis import analyze_issues, linked_issues_by_pr
from .spec_pages import SpecPages
from .analysis import ALL_EDITORS, analyze_all, build_lanes, build_perspective_lanes, perspective_keys
from .config import DashboardConfig
from .github import RepositoryData
from .accountability import build_people
from .metrics import TREND_BUCKET_DAYS, TREND_WINDOW_DAYS, build_metrics
from .models import isoformat


LANE_DESCRIPTIONS = {
    "active": {
        "title": "Active now",
        "description": "Recently changed items you're involved in and whose move is yours. Newest first.",
    },
    "direct": {
        "title": "Direct requests",
        "description": "Review requests and assignments you hold, and mentions in the activity window.",
    },
    "stale_direct": {
        "title": "Stale mentions",
        "description": "Older mentions, findable without leading the queue.",
    },
    "rereview": {
        "title": "Re-review owed",
        "description": "Changed, or the author replied, since your latest review. Your move.",
    },
    "land": {
        "title": "Ready to land",
        "description": "You approved the current head, nobody's latest review requests changes, and the checklist is ticked. Merge conflicts are shown but don't hold it here.",
    },
    "reply_window": {
        "title": "Reply window open",
        "description": "Contributor items with no reply yet, still inside the target. Closest to the deadline first: a reply can still make it.",
    },
    "overdue": {
        "title": "First reply overdue",
        "description": "Contributor items with no reply yet, past the target. Oldest first.",
    },
    "oldest_wait": {
        "title": "Longest waits",
        "description": "Contributor activity nobody has answered since. Longest wait first.",
    },
    "ready_bounded": {
        "title": "Ready",
        "description": "Quick wins: mergeable, no blocker, a small diff and a ticked checklist.",
    },
    "stale_author": {
        "title": "Long wait on author",
        "description": "The author's move for a long time. Candidates to ping or close, longest first.",
    },
    "triage": {
        "title": "Needs triage",
        "description": "Recent issues without a topic label, oldest first. Labeling one is a first response.",
    },
    "routed": {
        "title": "Routed to you",
        "description": "Unanswered items nobody was asked about, routed to one person by topic and load. Oldest first; the card says why.",
    },
    "all": {
        "title": "All open",
        "description": "Every open PR ready for review, and every open issue in another lane. Latest update first.",
    },
}


def _queue_order_principle(config: DashboardConfig) -> str:
    lead = config.suggested_next.first_response_lead
    days = config.attention.activity_window_days
    if not lead:
        return f"Suggested next leads with recent activity (the last {days} days) on items you're involved in."
    return (
        f"Suggested next leads with up to {lead} unanswered contributor items still inside the "
        f"{config.response_targets.initial_editor_response_days}-day target, then recent activity "
        f"(the last {days} days) on items you're involved in."
    )


def _perspective_options(config: DashboardConfig) -> list[dict[str, Any]]:
    # `editor` marks the options that can also be an identity. The union is a way of
    # reading the queue, not a person, so it carries no fingerprints and cannot own
    # browser-local seen or addressed state.
    return [
        {
            "key": key,
            "label": (
                "All editors"
                if key == ALL_EDITORS
                else f"@{key} (helper)"
                if key in config.helpers
                else f"@{key}"
            ),
            # Whether the option can be an identity: every person, editor or helper.
            "editor": key != ALL_EDITORS,
            "role": None if key == ALL_EDITORS else "helper" if key in config.helpers else "editor",
        }
        for key in perspective_keys(config)
    ]


def _methodology(config: DashboardConfig) -> dict[str, Any]:
    sampling = config.sampling
    days = config.response_targets.initial_editor_response_days
    return {
        "how_it_works": [
            "Rebuilt daily from public GitHub data. No LLM: every classification is a rule, and each card shows its evidence.",
            _queue_order_principle(config),
            (
                "Each item says whose move it is: labels first, then an open change request, a current approval, "
                "then who acted last. Conflicts and failing checks never hand a PR back. A helper's reply counts "
                "as an editor's, and a helper and the author iterate until the helper approves."
            ),
            "Attention lanes are per person, and leave out your own items and ones you answered last.",
            "Unanswered items nobody was asked about are routed to one person by declared topics and load.",
            (
                f"A first response is a reply by an editor or helper, with a {days}-day target. Drafts are left out, "
                "and a PR's clocks start when it was last marked ready."
            ),
            "Suggested labels and teams are never applied automatically.",
        ],
        "privacy": [
            "Published data is public GitHub signals only, with no description, comment or review text.",
            "Your identity and seen, addressed, pinned and snoozed state stay in this browser.",
        ],
        "limitations": [
            (
                f"Long threads are sampled: the first and last {sampling.timeline_each_end} comments, the latest "
                f"{sampling.reviews_per_pr} reviews and the first {sampling.review_threads} review threads. "
                "Figures that need the middle are dropped when it's missing."
            ),
            "Inline review replies aren't read.",
            (
                f"Trends span twelve weeks (closed PRs: the last {sampling.history_days} days) and replay today's "
                "drafts, authors and editor list."
            ),
            f"Mentions expire after {config.attention.activity_window_days} days; review requests and assignments last until GitHub clears them.",
            "Address until changed ignores your own activity and checks only the newest comment by someone else.",
            "Mergeability GitHub hasn't computed shows as unknown.",
            "Description edits are attributed to the author.",
            "Changes under a renamed heading aren't attributed to a spec page.",
            "Figures count public events here only: not test coverage, implementer interest, consensus, or work elsewhere.",
            "Unseen means not opened in this browser, not GitHub's unread state.",
        ],
    }


def build_site(
    config: DashboardConfig,
    repository_data: RepositoryData,
    *,
    output_dir: str | Path,
    now: datetime | None = None,
    spec: SpecPages | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # Issues first: an issue an open PR claims to fix is folded into that PR's card,
    # so the PR's analysis needs the issue's.
    open_numbers = frozenset(pr.number for pr in repository_data.open_pull_requests)
    issue_analyses = analyze_issues(
        repository_data.open_issues, config, now=now, open_pull_requests=open_numbers
    )
    open_analyses = analyze_all(
        repository_data.open_pull_requests,
        config,
        now=now,
        dependency_states=repository_data.dependency_states,
        linked_issues=linked_issues_by_pr(issue_analyses, config, now=now),
    )
    closed_analyses = analyze_all(repository_data.recently_closed_pull_requests, config, now=now)
    closed_issue_analyses = analyze_issues(repository_data.recently_closed_issues, config, now=now)
    # Only issues something asks about are published; the rest of the open issues
    # would multiply data.json for nothing a lane shows.
    published_issues = [analysis for analysis in issue_analyses if analysis.in_any_lane()]
    queued = [*open_analyses, *published_issues]
    lanes = build_lanes(queued, config.repository.slug)
    perspective_lanes = build_perspective_lanes(queued, config)
    metrics = build_metrics(
        open_analyses,
        closed_analyses,
        config,
        now=now,
        issue_analyses=[*issue_analyses, *closed_issue_analyses],
    )

    items = [
        analysis.to_public_dict(config.repository.slug)
        for analysis in sorted(queued, key=lambda value: value.number)
    ]
    assignments = apply_assignments(
        items={item["key"]: item for item in items},
        open_prs=open_analyses,
        closed_prs=closed_analyses,
        open_issues=issue_analyses,
        closed_issues=closed_issue_analyses,
        history=repository_data.history,
        spec=spec or SpecPages(),
        perspective_lanes=perspective_lanes,
        config=config,
        now=now,
        repository_labels=repository_data.labels,
    )
    metrics["people"] = build_people(
        prs=[*open_analyses, *closed_analyses],
        issues=[*issue_analyses, *closed_issue_analyses],
        history=repository_data.history,
        items=items,
        decisions=assignments.decisions,
        candidates=assignments.candidates,
        config=config,
        now=now,
        window_days=TREND_WINDOW_DAYS,
        bucket_days=TREND_BUCKET_DAYS,
    )

    payload: dict[str, Any] = {
        "schema_version": 2,
        "generated_at": isoformat(now),
        "repository": {
            "owner": config.repository.owner,
            "name": config.repository.name,
            "slug": config.repository.slug,
            "url": f"https://github.com/{config.repository.slug}/pulls",
        },
        "privacy": {
            "generated_data": "public-only",
            "local_state": ["identity", "seen", "addressed", "pinned", "snoozed", "opened", "queue_preferences"],
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
            "first_response_lead": config.suggested_next.first_response_lead,
        },
        "lane_descriptions": LANE_DESCRIPTIONS,
        # Which editor's queue the browser is showing. The attention lanes and their
        # evidence differ per perspective; `lanes` holds the four that do not.
        "perspectives": {
            "default": ALL_EDITORS,
            "options": _perspective_options(config),
        },
        "lanes": lanes,
        "perspective_lanes": perspective_lanes,
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

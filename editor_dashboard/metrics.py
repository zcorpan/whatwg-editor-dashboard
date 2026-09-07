from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .analysis import PRAnalysis, is_bot
from .config import DashboardConfig
from .models import PullRequestSnapshot, isoformat


# The health view is a trend, not a table of numbers, so every published figure comes
# from the same window: twelve weekly buckets. It stops short of ``history_days`` (90)
# because the closed-PR search is bounded by that cutoff, and a backlog reconstruction
# is only exact for moments GitHub told us about every close.
TREND_BUCKET_DAYS = 7
TREND_BUCKET_COUNT = 12
TREND_WINDOW_DAYS = TREND_BUCKET_DAYS * TREND_BUCKET_COUNT
RECENT_WINDOW_DAYS = 28


@dataclass(frozen=True)
class Bucket:
    start: datetime
    end: datetime


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _percent(numerator: int, denominator: int) -> float | None:
    return _round(numerator / denominator * 100) if denominator else None


def _contributor_pr(analysis: PRAnalysis, config: DashboardConfig) -> bool:
    """Whether the PR is one an editor owes a contributor a first response on."""
    pr = analysis.pr
    return not pr.is_draft and pr.author not in config.editors and not is_bot(pr.author)


def _open_at(pr: PullRequestSnapshot, moment: datetime) -> bool:
    return pr.created_at <= moment and (pr.closed_at is None or pr.closed_at > moment)


def _viewer_reviewed_by(pr: PullRequestSnapshot, moment: datetime) -> bool:
    return any(
        review.state != "PENDING" and review.created_at <= moment
        for review in pr.viewer_reviews
    )


def _buckets(now: datetime) -> list[Bucket]:
    return [
        Bucket(
            start=now - timedelta(days=index * TREND_BUCKET_DAYS),
            end=now - timedelta(days=(index - 1) * TREND_BUCKET_DAYS),
        )
        for index in range(TREND_BUCKET_COUNT, 0, -1)
    ]


def _backlog_points(
    analyses: list[PRAnalysis],
    config: DashboardConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    """Reconstruct the open-PR backlog at weekly moments across the trend window.

    Every open PR and every PR closed inside ``history_days`` is in the sample, so
    "was this PR open at time t" is answerable exactly for any t in the window. The
    unanswered series is narrower: it needs the first editor response, which is only
    trustworthy when the beginning-of-timeline sample established it.
    """
    target_hours = config.response_targets.initial_editor_response_days * 24
    points: list[dict[str, Any]] = []
    for offset in range(TREND_WINDOW_DAYS, -1, -TREND_BUCKET_DAYS):
        moment = now - timedelta(days=offset)
        open_prs = 0
        awaiting = 0
        overdue = 0
        for analysis in analyses:
            if not _open_at(analysis.pr, moment):
                continue
            open_prs += 1
            if not _contributor_pr(analysis, config) or not analysis.first_editor_response_known:
                continue
            response = analysis.first_editor_response
            if response is not None and response.created_at <= moment:
                continue
            awaiting += 1
            if (moment - analysis.pr.created_at).total_seconds() / 3600 >= target_hours:
                overdue += 1
        points.append(
            {
                "at": isoformat(moment),
                "open_prs": open_prs,
                "awaiting_first_response": awaiting,
                "overdue_first_response": overdue,
            }
        )
    return points


def _bucket_metrics(
    analyses: list[PRAnalysis],
    config: DashboardConfig,
    now: datetime,
) -> list[dict[str, Any]]:
    target_hours = config.response_targets.initial_editor_response_days * 24
    viewer = config.viewer
    results: list[dict[str, Any]] = []
    for bucket in _buckets(now):
        merged = [
            analysis
            for analysis in analyses
            if analysis.pr.merged_at and bucket.start < analysis.pr.merged_at <= bucket.end
        ]
        merged_by_others = [analysis for analysis in merged if analysis.pr.author != viewer]
        opened = [
            analysis
            for analysis in analyses
            if bucket.start < analysis.pr.created_at <= bucket.end
        ]
        eligible = [
            analysis
            for analysis in opened
            if _contributor_pr(analysis, config) and analysis.first_editor_response_known
        ]
        within_target = sum(
            analysis.first_editor_response_hours is not None
            and analysis.first_editor_response_hours <= target_hours
            for analysis in eligible
        )
        results.append(
            {
                "start": isoformat(bucket.start),
                "end": isoformat(bucket.end),
                "opened": len(opened),
                "merged": len(merged),
                "merged_by_others": len(merged_by_others),
                "merged_with_viewer_review": sum(
                    _viewer_reviewed_by(analysis.pr, analysis.pr.merged_at)
                    for analysis in merged_by_others
                ),
                "first_response_eligible": len(eligible),
                "first_response_within_target": within_target,
                # A bucket whose PRs are not all older than the target cannot report a
                # rate yet: the misses have not had time to happen.
                "first_response_mature": bucket.end <= now - timedelta(hours=target_hours),
            }
        )
    return results


def _status(on_track: bool, watch: bool = False) -> str:
    if on_track:
        return "on_track"
    return "watch" if watch else "off_track"


def _health(
    points: list[dict[str, Any]],
    buckets: list[dict[str, Any]],
    config: DashboardConfig,
    now: datetime,
) -> dict[str, Any]:
    """Reduce the trend to three judgements, each with an unambiguous good direction."""
    first, last = points[0], points[-1]

    mature = [bucket for bucket in buckets if bucket["first_response_mature"]]
    eligible = sum(bucket["first_response_eligible"] for bucket in mature)
    within_target = sum(bucket["first_response_within_target"] for bucket in mature)
    percent = _percent(within_target, eligible)
    target_percent = config.response_targets.first_response_within_target_percent

    # The rate has no per-moment value to difference, so "improving" compares the most
    # recent buckets against the earlier ones inside the same window.
    recent_start = isoformat(now - timedelta(days=RECENT_WINDOW_DAYS))
    recent = [bucket for bucket in mature if bucket["end"] > recent_start]
    earlier = [bucket for bucket in mature if bucket["end"] <= recent_start]
    recent_percent = _percent(
        sum(bucket["first_response_within_target"] for bucket in recent),
        sum(bucket["first_response_eligible"] for bucket in recent),
    )
    earlier_percent = _percent(
        sum(bucket["first_response_within_target"] for bucket in earlier),
        sum(bucket["first_response_eligible"] for bucket in earlier),
    )
    rate_change = (
        _round(recent_percent - earlier_percent)
        if recent_percent is not None and earlier_percent is not None
        else None
    )

    if percent is None:
        response_status = "unknown"
    else:
        response_status = _status(percent >= target_percent, percent >= target_percent - 20)

    indicators = {
        "backlog": {
            "label": "Open pull requests",
            "value": last["open_prs"],
            "change": last["open_prs"] - first["open_prs"],
            "lower_is_better": True,
            "status": _status(last["open_prs"] <= first["open_prs"]),
            "series": "open_prs",
        },
        "overdue_first_response": {
            "label": f"Waiting over {config.response_targets.initial_editor_response_days} days for a first reply",
            "value": last["overdue_first_response"],
            "change": last["overdue_first_response"] - first["overdue_first_response"],
            "lower_is_better": True,
            "status": _status(
                last["overdue_first_response"] == 0
                or last["overdue_first_response"] <= first["overdue_first_response"]
            ),
            "series": "overdue_first_response",
        },
        "first_response_rate": {
            "label": f"First reply within {config.response_targets.initial_editor_response_days} days",
            "percent": percent,
            "within_target": within_target,
            "eligible": eligible,
            "target_percent": target_percent,
            "recent_days": RECENT_WINDOW_DAYS,
            "recent_percent": recent_percent,
            "earlier_percent": earlier_percent,
            "change": rate_change,
            "lower_is_better": False,
            "status": response_status,
        },
    }

    ranking = {"off_track": 0, "watch": 1, "unknown": 2, "on_track": 3}
    overall = min(
        (indicator["status"] for indicator in indicators.values()),
        key=lambda value: ranking[value],
    )
    return {"overall_status": overall, "indicators": indicators}


def _viewer_window(
    analyses: list[PRAnalysis],
    config: DashboardConfig,
    now: datetime,
    days: int,
) -> dict[str, Any]:
    """Public, order-describing counts of the viewer's participation in one window.

    Nothing here asserts causation: "merged after a review" is an ordering of two
    public events. Merged PRs the viewer authored are excluded from the review share,
    because an author cannot review their own PR, and reported on their own.
    """
    viewer = config.viewer
    cutoff = now - timedelta(days=days)

    merged_by_others = 0
    merged_with_viewer_review = 0
    authored_merged = 0
    reviews_submitted = 0
    first_responses_total = 0
    first_responses_by_viewer = 0
    contributors: set[str] = set()

    for analysis in analyses:
        pr = analysis.pr
        reviews_submitted += sum(
            review.state != "PENDING" and review.created_at >= cutoff
            for review in pr.viewer_reviews
        )

        if pr.merged_at and pr.merged_at >= cutoff:
            if pr.author == viewer:
                authored_merged += 1
            else:
                merged_by_others += 1
                if _viewer_reviewed_by(pr, pr.merged_at):
                    merged_with_viewer_review += 1

        response = analysis.first_editor_response
        if (
            response is not None
            and analysis.first_editor_response_known
            and response.created_at >= cutoff
        ):
            first_responses_total += 1
            if response.author == viewer:
                first_responses_by_viewer += 1

        if pr.author and pr.author != viewer and not is_bot(pr.author):
            engaged = any(
                activity.author == viewer and activity.created_at >= cutoff
                for activity in pr.timeline
            ) or any(
                review.state != "PENDING" and review.created_at >= cutoff
                for review in pr.viewer_reviews
            )
            if engaged:
                contributors.add(pr.author)

    return {
        "days": days,
        "since": isoformat(cutoff),
        "merged_by_others": merged_by_others,
        "merged_with_viewer_review": merged_with_viewer_review,
        "merged_with_viewer_review_percent": _percent(merged_with_viewer_review, merged_by_others),
        "first_responses_total": first_responses_total,
        "first_responses_by_viewer": first_responses_by_viewer,
        "first_responses_by_viewer_percent": _percent(first_responses_by_viewer, first_responses_total),
        "reviews_submitted": reviews_submitted,
        "contributors_engaged": len(contributors),
        "authored_prs_merged": authored_merged,
    }


def build_metrics(
    open_analyses: Iterable[PRAnalysis],
    closed_analyses: Iterable[PRAnalysis],
    config: DashboardConfig,
    *,
    now: datetime,
) -> dict[str, Any]:
    now = now.astimezone(timezone.utc)
    open_values = list(open_analyses)
    closed_values = list(closed_analyses)
    all_values = [*open_values, *closed_values]

    known_without_response = sum(
        analysis.first_editor_response is None and analysis.first_editor_response_known
        for analysis in open_values
        if _contributor_pr(analysis, config)
    )
    unknown_first_response = sum(
        not analysis.first_editor_response_known
        for analysis in open_values
        if _contributor_pr(analysis, config)
    )
    target_hours = config.response_targets.initial_editor_response_days * 24

    points = _backlog_points(all_values, config, now)
    buckets = _bucket_metrics(all_values, config, now)

    return {
        "generated_at": isoformat(now),
        "repository": {
            "current": {
                "open_prs": len(open_values),
                "draft_prs": sum(analysis.pr.is_draft for analysis in open_values),
                "ready_for_review_prs": sum(not analysis.pr.is_draft for analysis in open_values),
                "active_now": sum("active" in analysis.lanes for analysis in open_values),
                "direct_requests": sum("direct" in analysis.lanes for analysis in open_values),
                "stale_direct_requests": sum("stale_direct" in analysis.lanes for analysis in open_values),
                "rereview_owed": sum("rereview" in analysis.lanes for analysis in open_values),
                "waiting_on_editor": sum("oldest_wait" in analysis.lanes for analysis in open_values),
                "known_without_editor_response": known_without_response,
                "first_response_unknown_due_to_sampling": unknown_first_response,
                "over_response_target": sum(
                    "oldest_wait" in analysis.lanes
                    and (analysis.current_wait_hours or 0) >= target_hours
                    for analysis in open_values
                ),
                "ready_and_bounded": sum("ready_bounded" in analysis.lanes for analysis in open_values),
            },
        },
        "trends": {
            "window_days": TREND_WINDOW_DAYS,
            "bucket_days": TREND_BUCKET_DAYS,
            "points": points,
            "buckets": buckets,
        },
        "health": _health(points, buckets, config, now),
        "viewer": {
            "login": config.viewer,
            "window": _viewer_window(all_values, config, now, TREND_WINDOW_DAYS),
            "recent": _viewer_window(all_values, config, now, RECENT_WINDOW_DAYS),
        },
        "coverage": {
            "open_timeline_complete": sum(analysis.pr.timeline_sample_complete for analysis in open_values),
            "open_timeline_total": len(open_values),
            "closed_timeline_complete": sum(analysis.pr.timeline_sample_complete for analysis in closed_values),
            "closed_timeline_total": len(closed_values),
            "viewer_review_connections_truncated": sum(
                analysis.pr.viewer_reviews_total_count > len(analysis.pr.viewer_reviews)
                for analysis in all_values
            ),
            "first_response_unknown_due_to_sampling": unknown_first_response,
            "history_days": config.sampling.history_days,
        },
    }

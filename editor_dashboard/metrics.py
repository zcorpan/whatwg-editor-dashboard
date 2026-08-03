from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .analysis import PRAnalysis, is_bot
from .config import DashboardConfig
from .models import Activity, isoformat


@dataclass(frozen=True)
class ResponseInterval:
    started_at: datetime
    ended_at: datetime
    editor: str

    @property
    def hours(self) -> float:
        return max(0.0, (self.ended_at - self.started_at).total_seconds() / 3600)


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _median(values: Iterable[float]) -> float | None:
    numbers = list(values)
    return statistics.median(numbers) if numbers else None


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    numbers = sorted(values)
    if not numbers:
        return None
    if len(numbers) == 1:
        return numbers[0]
    position = (len(numbers) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return numbers[lower]
    weight = position - lower
    return numbers[lower] * (1 - weight) + numbers[upper] * weight


def _human_activity(activity: Activity) -> bool:
    return bool(activity.author) and not is_bot(activity.author)


def response_intervals(analysis: PRAnalysis, config: DashboardConfig) -> list[ResponseInterval]:
    """Return sampled contributor-to-editor response intervals.

    Later intervals are emitted only when the sampled comment/review timeline is complete.
    The first response can still be known from the beginning-of-timeline sample, but it is
    handled separately by the response-time metrics.
    """
    if not analysis.pr.timeline_sample_complete:
        return []
    if analysis.pr.author in config.editors or is_bot(analysis.pr.author):
        return []

    pending_since: datetime | None = analysis.pr.created_at
    intervals: list[ResponseInterval] = []
    for activity in analysis.pr.timeline:
        if not _human_activity(activity):
            continue
        if activity.author in config.editors:
            if pending_since is not None and activity.created_at >= pending_since:
                intervals.append(
                    ResponseInterval(
                        started_at=pending_since,
                        ended_at=activity.created_at,
                        editor=activity.author,
                    )
                )
                pending_since = None
        else:
            pending_since = activity.updated_at
    return intervals


def _wait_bucket(hours: float) -> str:
    days = hours / 24
    if days < 2:
        return "Under 2 days"
    if days <= 7:
        return "2–7 days"
    if days <= 30:
        return "8–30 days"
    if days <= 90:
        return "31–90 days"
    if days <= 180:
        return "91–180 days"
    return "Over 180 days"


def _checklist_metrics(analyses: Iterable[PRAnalysis]) -> dict[str, Any]:
    values = list(analyses)
    with_tasks = [analysis for analysis in values if analysis.checklist.total]
    complete = sum(
        analysis.checklist.checked == analysis.checklist.total
        for analysis in with_tasks
    )
    partial = sum(
        0 < analysis.checklist.checked < analysis.checklist.total
        for analysis in with_tasks
    )
    none_checked = sum(
        analysis.checklist.checked == 0
        for analysis in with_tasks
    )
    ratios = [analysis.checklist.ratio or 0 for analysis in with_tasks]
    return {
        "with_checklist": len(with_tasks),
        "without_checklist": len(values) - len(with_tasks),
        "complete": complete,
        "partial": partial,
        "none_checked": none_checked,
        "average_percent": _round(statistics.mean(ratios) * 100) if ratios else None,
    }


def _response_metrics_for_created(
    analyses: Iterable[PRAnalysis],
    config: DashboardConfig,
) -> dict[str, Any]:
    eligible = [
        analysis
        for analysis in analyses
        if not analysis.pr.is_draft
        and analysis.pr.author not in config.editors
        and not is_bot(analysis.pr.author)
    ]
    known_response_times = [
        analysis.first_editor_response_hours
        for analysis in eligible
        if analysis.first_editor_response_hours is not None
    ]
    known_no_response = sum(
        analysis.first_editor_response is None and analysis.first_editor_response_known
        for analysis in eligible
    )
    unknown = sum(not analysis.first_editor_response_known for analysis in eligible)

    first_time = [analysis for analysis in eligible if analysis.first_time_contributor]
    first_time_times = [
        analysis.first_editor_response_hours
        for analysis in first_time
        if analysis.first_editor_response_hours is not None
    ]
    return {
        "eligible_prs": len(eligible),
        "responded": len(known_response_times),
        "known_no_response": known_no_response,
        "unknown_due_to_sampling": unknown,
        "median_hours": _round(_median(known_response_times)),
        "p90_hours": _round(_percentile(known_response_times, 0.9)),
        "within_target": sum(
            value <= config.response_targets.initial_editor_response_days * 24
            for value in known_response_times
        ),
        "first_time_contributors": {
            "eligible_prs": len(first_time),
            "responded": len(first_time_times),
            "median_hours": _round(_median(first_time_times)),
            "p90_hours": _round(_percentile(first_time_times, 0.9)),
        },
    }


def _personal_metrics(
    analyses: list[PRAnalysis],
    config: DashboardConfig,
    cutoff: datetime,
) -> dict[str, Any]:
    viewer = config.viewer

    reviews = [
        review
        for analysis in analyses
        for review in analysis.pr.viewer_reviews
        if review.state != "PENDING" and review.created_at >= cutoff
    ]

    merged_after_review = 0
    authored_merged = 0
    first_responses = 0
    unique_pr_authors: set[str] = set()
    author_activity_after_review = 0
    long_waits_addressed = 0
    waiting_days_ended = 0.0
    response_hours: list[float] = []

    for analysis in analyses:
        pr = analysis.pr
        if pr.merged_at and pr.merged_at >= cutoff:
            if any(review.created_at <= pr.merged_at for review in pr.viewer_reviews if review.state != "PENDING"):
                merged_after_review += 1
            if pr.author == viewer:
                authored_merged += 1

        if (
            analysis.first_editor_response
            and analysis.first_editor_response_known
            and analysis.first_editor_response.author == viewer
            and analysis.first_editor_response.created_at >= cutoff
        ):
            first_responses += 1

        viewer_timeline_activities = [
            activity
            for activity in pr.timeline
            if activity.author == viewer and activity.created_at >= cutoff
        ]
        viewer_reviews = [
            review
            for review in pr.viewer_reviews
            if review.state != "PENDING" and review.created_at >= cutoff
        ]
        if (
            (viewer_timeline_activities or viewer_reviews)
            and pr.author
            and pr.author != viewer
            and not is_bot(pr.author)
        ):
            unique_pr_authors.add(pr.author)

        latest_review = analysis.latest_viewer_review
        if latest_review and latest_review.created_at >= cutoff:
            if any(
                activity.author == pr.author
                and activity.created_at > latest_review.created_at
                for activity in pr.timeline
            ):
                author_activity_after_review += 1

        for interval in response_intervals(analysis, config):
            if interval.editor != viewer or interval.ended_at < cutoff:
                continue
            response_hours.append(interval.hours)
            waiting_days_ended += interval.hours / 24
            if interval.hours >= config.response_targets.initial_editor_response_days * 24:
                long_waits_addressed += 1

    return {
        "reviews_submitted": len(reviews),
        "prs_merged_after_review": merged_after_review,
        "first_editor_responses": first_responses,
        "unique_pr_authors_engaged_with": len(unique_pr_authors),
        "authored_prs_merged": authored_merged,
        "prs_with_author_activity_after_review": author_activity_after_review,
        "long_waits_addressed": long_waits_addressed,
        "sampled_contributor_waiting_days_ended": round(waiting_days_ended),
        "median_sampled_response_hours": _round(_median(response_hours)),
    }


def _window_metrics(
    all_analyses: list[PRAnalysis],
    closed_analyses: list[PRAnalysis],
    config: DashboardConfig,
    now: datetime,
    days: int,
) -> dict[str, Any]:
    cutoff = now - timedelta(days=days)
    opened = [analysis for analysis in all_analyses if analysis.pr.created_at >= cutoff]
    closed = [
        analysis
        for analysis in closed_analyses
        if analysis.pr.closed_at and analysis.pr.closed_at >= cutoff
    ]
    merged = [
        analysis
        for analysis in closed_analyses
        if analysis.pr.merged_at and analysis.pr.merged_at >= cutoff
    ]
    return {
        "days": days,
        "since": isoformat(cutoff),
        "opened": len(opened),
        "closed": len(closed),
        "merged": len(merged),
        "net_backlog_change": len(opened) - len(closed),
        "first_editor_response": _response_metrics_for_created(opened, config),
        "viewer": _personal_metrics(all_analyses, config, cutoff),
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

    waits = [
        analysis.current_wait_hours
        for analysis in open_values
        if "oldest_wait" in analysis.lanes and analysis.current_wait_hours is not None
    ]
    wait_distribution = Counter(_wait_bucket(value) for value in waits)
    wait_bucket_order = [
        "Under 2 days",
        "2–7 days",
        "8–30 days",
        "31–90 days",
        "91–180 days",
        "Over 180 days",
    ]
    waiting_reasons = Counter(analysis.waiting_reason for analysis in open_values)

    known_without_response = sum(
        analysis.first_editor_response is None and analysis.first_editor_response_known
        for analysis in open_values
        if not analysis.pr.is_draft
        and analysis.pr.author not in config.editors
        and not is_bot(analysis.pr.author)
    )
    unknown_first_response = sum(
        not analysis.first_editor_response_known
        for analysis in open_values
        if not analysis.pr.is_draft
        and analysis.pr.author not in config.editors
        and not is_bot(analysis.pr.author)
    )
    target_hours = config.response_targets.initial_editor_response_days * 24

    windows = {
        str(days): _window_metrics(all_values, closed_values, config, now, days)
        for days in (7, 28, 90)
    }

    week_viewer = windows["7"]["viewer"]
    motivation = [
        f"{week_viewer['reviews_submitted']} public review"
        + ("" if week_viewer["reviews_submitted"] == 1 else "s")
        + " submitted by @" + config.viewer,
        f"{week_viewer['prs_merged_after_review']} PR"
        + ("" if week_viewer["prs_merged_after_review"] == 1 else "s")
        + " merged after an @" + config.viewer + " review",
        f"{week_viewer['first_editor_responses']} first editor response"
        + ("" if week_viewer["first_editor_responses"] == 1 else "s")
        + " from @" + config.viewer,
        f"{week_viewer['long_waits_addressed']} sampled wait"
        + ("" if week_viewer["long_waits_addressed"] == 1 else "s")
        + " over seven days addressed",
    ]

    return {
        "generated_at": isoformat(now),
        "repository": {
            "current": {
                "open_prs": len(open_values),
                "draft_prs": sum(analysis.pr.is_draft for analysis in open_values),
                "ready_for_review_prs": sum(not analysis.pr.is_draft for analysis in open_values),
                "direct_requests": sum("direct" in analysis.lanes for analysis in open_values),
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
            "wait_distribution": [
                {"label": label, "count": wait_distribution.get(label, 0)}
                for label in wait_bucket_order
            ],
            "waiting_reasons": [
                {"label": label, "count": count}
                for label, count in sorted(waiting_reasons.items(), key=lambda item: (-item[1], item[0]))
            ],
            "checklist": _checklist_metrics(open_values),
            "windows": windows,
        },
        "viewer": {
            "login": config.viewer,
            "windows": {key: value["viewer"] for key, value in windows.items()},
        },
        "motivation": motivation,
        "coverage": {
            "open_timeline_complete": sum(analysis.pr.timeline_sample_complete for analysis in open_values),
            "open_timeline_total": len(open_values),
            "closed_timeline_complete": sum(analysis.pr.timeline_sample_complete for analysis in closed_values),
            "closed_timeline_total": len(closed_values),
            "open_review_threads_complete": sum(
                analysis.pr.review_threads_sample_complete for analysis in open_values
            ),
            "open_review_threads_total": len(open_values),
            "viewer_review_connections_truncated": sum(
                analysis.pr.viewer_reviews_total_count > len(analysis.pr.viewer_reviews)
                for analysis in all_values
            ),
            "history_days": config.sampling.history_days,
        },
    }

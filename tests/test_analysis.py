from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from editor_dashboard.analysis import (
    ALL_EDITORS,
    PERSPECTIVE_LANES,
    analyze_all,
    analyze_pull_request,
    build_lanes,
    build_perspective_lanes,
)
from editor_dashboard.config import load_config
from editor_dashboard.github import load_fixture
from editor_dashboard.models import Activity, ReviewThread


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
# One of the configured editors, standing in for whoever the browser says it is.
VIEWER = "zcorpan"


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")
        cls.repository_data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        cls.analyses = analyze_all(cls.repository_data.open_pull_requests, cls.config, now=NOW)
        cls.by_number = {analysis.pr.number: analysis for analysis in cls.analyses}

    def viewer_lanes(self, number: int) -> tuple[str, ...]:
        return self.by_number[number].lanes_for(VIEWER)

    def view(self, number: int, editor: str = None):
        return self.by_number[number].perspectives[editor or VIEWER]

    def test_expected_mvp_lanes(self) -> None:
        self.assertIn("active", self.viewer_lanes(13001))
        self.assertIn("stale_direct", self.viewer_lanes(13011))
        self.assertIn("direct", self.viewer_lanes(13001))
        self.assertIn("direct", self.viewer_lanes(13008))
        self.assertIn("rereview", self.viewer_lanes(13002))
        self.assertIn("reply_window", self.viewer_lanes(13003))
        self.assertIn("oldest_wait", self.viewer_lanes(12900))
        self.assertIn("ready_bounded", self.viewer_lanes(13004))
        self.assertNotIn("ready_bounded", self.viewer_lanes(13006))

    def test_attention_lanes_sort_newest_first(self) -> None:
        lanes = build_perspective_lanes(self.analyses, self.config)[VIEWER]
        self.assertEqual(lanes["direct"], ["whatwg/html#13001", "whatwg/html#13008"])
        self.assertEqual(
            lanes["active"],
            ["whatwg/html#13001", "whatwg/html#13008", "whatwg/html#13002"],
        )

    def test_oldest_wait_still_sorts_longest_wait_first(self) -> None:
        lanes = build_lanes(self.analyses, self.config.repository.slug)
        waits = [
            self.by_number[int(key.split("#")[1])].current_wait_hours
            for key in lanes["oldest_wait"]
        ]
        self.assertEqual(waits, sorted(waits, reverse=True))

    def test_active_lane_needs_both_recent_activity_and_involvement(self) -> None:
        # #13002 has a prior viewer review and recent activity.
        self.assertIn("active", self.viewer_lanes(13002))
        # #12900 has an old contributor wait and no editor involvement.
        self.assertNotIn("active", self.viewer_lanes(12900))

        stale_activity = replace(self.by_number[13002].pr, updated_at=NOW - timedelta(days=90))
        analysis = analyze_pull_request(stale_activity, self.config, now=NOW)
        self.assertNotIn("active", analysis.lanes_for(VIEWER))

    def test_expired_mention_moves_out_of_direct_but_stays_findable(self) -> None:
        analysis = self.by_number[13011]
        self.assertNotIn("direct", self.viewer_lanes(13011))
        self.assertIn("stale_direct", self.viewer_lanes(13011))
        self.assertFalse(self.view(13011).has_attention_signal)
        self.assertIn(
            "mentioned-in-discussion",
            {reason.code for reason in analysis.reasons_for(VIEWER)},
        )

    def test_review_request_does_not_expire_and_wins_over_an_old_mention(self) -> None:
        # A review request is current GitHub state, so it keeps claiming attention
        # even on a PR whose only other signal is a years-old mention.
        requested = replace(self.by_number[13011].pr, review_requests=("zcorpan",))
        analysis = analyze_pull_request(requested, self.config, now=NOW)
        view = analysis.perspectives[VIEWER]
        self.assertIn("direct", view.lanes)
        self.assertNotIn("stale_direct", view.lanes)
        # The freshest signal represents the PR, not the 2026-01-10 mention.
        self.assertEqual(view.direct_request_at, requested.updated_at)

    def test_missing_the_response_target_moves_a_pr_rather_than_dropping_it(self) -> None:
        # #12900 opened 2026-03-01, far beyond the seven-day target, and no editor
        # has ever responded. An old seven-day cap dropped exactly these; now they
        # leave `reply_window` for `overdue` instead of leaving the pair entirely.
        analysis = self.by_number[12900]
        self.assertIn("overdue", analysis.shared_lanes)
        self.assertNotIn("reply_window", analysis.shared_lanes)
        self.assertGreater(analysis.age_hours, self.config.response_targets.initial_editor_response_days * 24)
        self.assertIn("first-response-overdue", {reason.code for reason in analysis.shared_reasons})

    def test_reply_window_holds_the_savable_prs_closest_to_the_deadline_first(self) -> None:
        # The only PRs where a first reply can still land inside the target, so these
        # lead the suggested queue. #13001 (5.1 days) is nearer its deadline than
        # #13003 (1.2 days), and is deliberately also in `active`.
        lanes = build_lanes(self.analyses, self.config.repository.slug)
        self.assertEqual(lanes["reply_window"], ["whatwg/html#13001", "whatwg/html#13003"])
        self.assertEqual(
            lanes["overdue"],
            [
                "whatwg/html#13011",
                "whatwg/html#12900",
                "whatwg/html#13006",
                "whatwg/html#13008",
            ],
        )
        active = build_perspective_lanes(self.analyses, self.config)[VIEWER]["active"]
        self.assertIn("whatwg/html#13001", active)

    def test_reply_window_and_overdue_partition_the_unanswered_prs(self) -> None:
        lanes = build_lanes(self.analyses, self.config.repository.slug)
        self.assertEqual(set(lanes["reply_window"]) & set(lanes["overdue"]), set())
        unanswered = {
            f"{self.config.repository.slug}#{analysis.pr.number}"
            for analysis in self.analyses
            if not analysis.pr.is_draft
            and analysis.pr.author not in self.config.editors
            and analysis.first_editor_response is None
            and analysis.first_editor_response_known
        }
        self.assertEqual(set(lanes["reply_window"]) | set(lanes["overdue"]), unanswered)

    def test_the_lane_split_and_the_overdue_chip_agree_on_the_boundary(self) -> None:
        # Both read _response_target_hours; this pins them to the same instant so a
        # PR can never sit in `reply_window` while showing the overdue chip.
        target = timedelta(days=self.config.response_targets.initial_editor_response_days)

        at_target = analyze_pull_request(
            replace(self.by_number[13003].pr, created_at=NOW - target),
            self.config,
            now=NOW,
        )
        self.assertIn("reply_window", at_target.shared_lanes)
        self.assertIn("new-untriaged", {reason.code for reason in at_target.shared_reasons})

        past_target = analyze_pull_request(
            replace(self.by_number[13003].pr, created_at=NOW - target - timedelta(hours=1)),
            self.config,
            now=NOW,
        )
        self.assertIn("overdue", past_target.shared_lanes)
        self.assertIn("first-response-overdue", {reason.code for reason in past_target.shared_reasons})

    def test_recent_drafts_are_in_neither_first_response_lane(self) -> None:
        # #13005 is four days old with no editor response, but a draft is not yet
        # asking for one, so it must not take a lead slot.
        analysis = self.by_number[13005]
        self.assertTrue(analysis.pr.is_draft)
        self.assertNotIn("reply_window", analysis.shared_lanes)
        self.assertNotIn("overdue", analysis.shared_lanes)

    def test_first_time_contributor_detected_without_association(self) -> None:
        self.assertTrue(self.by_number[13011].first_time_contributor)
        self.assertEqual(self.by_number[13011].pr.author_association, "NONE")

    def test_first_editor_response_is_known_from_initial_sample(self) -> None:
        analysis = self.by_number[13007]
        self.assertTrue(analysis.first_editor_response_known)
        self.assertEqual(analysis.first_editor_response.author, "annevk")
        self.assertEqual(analysis.first_editor_response_hours, 49.0)

    def test_incomplete_timeline_marks_absent_first_response_unknown(self) -> None:
        analysis = self.by_number[13010]
        self.assertFalse(analysis.first_editor_response_known)
        self.assertIsNone(analysis.first_editor_response)
        self.assertNotIn("reply_window", analysis.shared_lanes)
        self.assertNotIn("overdue", analysis.shared_lanes)

    def test_ready_bounded_requires_a_description_checklist(self) -> None:
        original = self.by_number[13004]
        without_checklist = replace(original.pr, body="No task list here.")
        analysis = analyze_pull_request(without_checklist, self.config, now=NOW)
        self.assertNotIn("ready_bounded", analysis.shared_lanes)
        self.assertIn("checklist-missing", {blocker.code for blocker in analysis.blockers})

    def test_address_fingerprint_changes_with_public_pr_change(self) -> None:
        original = self.by_number[13003]
        changed_pr = replace(
            original.pr,
            updated_at=original.pr.updated_at + timedelta(minutes=1),
            head_oid="different-head",
        )
        changed = analyze_pull_request(changed_pr, self.config, now=NOW)
        self.assertNotEqual(
            original.perspectives[VIEWER].content_fingerprint,
            changed.perspectives[VIEWER].content_fingerprint,
        )

    def _comment(self, author: str) -> Activity:
        at = NOW - timedelta(hours=1)
        return Activity(
            id=f"c-{author}-new",
            kind="IssueComment",
            author=author,
            author_association="MEMBER",
            created_at=at,
            updated_at=at,
            body="Looks good to me.",
            url=None,
        )

    def test_address_fingerprint_ignores_the_viewers_own_review(self) -> None:
        # Reviewing bumps updated_at, adds a timeline item, clears the viewer's
        # own review request, sets review_decision and opens review threads. None
        # of that may bring an addressed PR back.
        original = self.by_number[13001]
        self.assertIn("zcorpan", original.pr.review_requests)
        reviewed = replace(
            original.pr,
            updated_at=NOW - timedelta(hours=1),
            timeline=(*original.pr.timeline, self._comment("zcorpan")),
            review_requests=(),
            review_decision="CHANGES_REQUESTED",
            review_threads=(*original.pr.review_threads, ReviewThread(False, False, "zcorpan")),
            unresolved_review_threads=original.pr.unresolved_review_threads + 1,
            review_threads_total_count=original.pr.review_threads_total_count + 1,
        )
        analysis = analyze_pull_request(reviewed, self.config, now=NOW)
        self.assertEqual(
            original.perspectives[VIEWER].content_fingerprint,
            analysis.perspectives[VIEWER].content_fingerprint,
        )

    def test_address_fingerprint_changes_on_somebody_elses_comment(self) -> None:
        original = self.by_number[13001]
        commented = replace(original.pr, timeline=(*original.pr.timeline, self._comment("carol")))
        analysis = analyze_pull_request(commented, self.config, now=NOW)
        self.assertNotEqual(
            original.perspectives[VIEWER].content_fingerprint,
            analysis.perspectives[VIEWER].content_fingerprint,
        )

    def test_address_fingerprint_changes_on_somebody_elses_review_thread(self) -> None:
        original = self.by_number[13009]
        threaded = replace(
            original.pr,
            review_threads=(*original.pr.review_threads, ReviewThread(False, False, "annevk")),
        )
        analysis = analyze_pull_request(threaded, self.config, now=NOW)
        self.assertNotEqual(
            original.perspectives[VIEWER].content_fingerprint,
            analysis.perspectives[VIEWER].content_fingerprint,
        )

    def test_address_fingerprint_ignores_lazily_computed_mergeability(self) -> None:
        """Regression: `mergeable` is not a property of the PR at query time.

        GitHub computes it lazily, so a cold build reads UNKNOWN and a later one
        reads the real value with nothing about the PR having changed. Hashing it
        returned nearly every addressed PR at the next build.
        """
        original = self.by_number[13001]
        fingerprints = {
            analyze_pull_request(
                replace(original.pr, mergeable=value), self.config, now=NOW
            ).perspectives[VIEWER].content_fingerprint
            for value in ("MERGEABLE", "CONFLICTING", "UNKNOWN", None)
        }
        self.assertEqual(len(fingerprints), 1)

    def test_every_configured_editor_gets_a_perspective(self) -> None:
        self.assertEqual(
            tuple(self.by_number[13001].perspectives),
            (ALL_EDITORS, "annevk", "domenic", "domfarolino", "foolip", "zcorpan"),
        )

    def test_an_editor_only_sees_their_own_direct_signals(self) -> None:
        # @annevk is mentioned on #13009 and @zcorpan on #13008; neither PR may
        # appear in the other editor's attention lanes.
        self.assertIn("direct", self.by_number[13009].perspectives["annevk"].lanes)
        self.assertNotIn("direct", self.by_number[13009].perspectives["zcorpan"].lanes)
        self.assertIn("direct", self.by_number[13008].perspectives["zcorpan"].lanes)
        self.assertNotIn("direct", self.by_number[13008].perspectives["annevk"].lanes)
        self.assertEqual(
            self.by_number[13009].perspectives["annevk"].reasons[-1].label,
            "@annevk mentioned in the discussion",
        )

    def test_all_editors_is_the_union_of_the_per_editor_lanes(self) -> None:
        lanes = build_perspective_lanes(self.analyses, self.config)
        for lane in PERSPECTIVE_LANES:
            union = set()
            for editor in sorted(self.config.editors):
                union |= set(lanes[editor][lane])
            self.assertEqual(set(lanes[ALL_EDITORS][lane]), union, lane)
        self.assertEqual(
            lanes[ALL_EDITORS]["direct"],
            ["whatwg/html#13001", "whatwg/html#13008", "whatwg/html#13009"],
        )

    def test_a_current_direct_signal_keeps_a_pr_out_of_the_team_stale_lane(self) -> None:
        # The union is assembled from the merged signal lists, not from the
        # per-editor lane sets, so the direct/stale-mention split stays one
        # decision: #13011 is stale for @zcorpan, and a live request for @annevk
        # must move it into `direct` rather than leaving it in both lanes.
        requested = replace(self.by_number[13011].pr, review_requests=("annevk",))
        view = analyze_pull_request(requested, self.config, now=NOW).perspectives[ALL_EDITORS]
        self.assertIn("direct", view.lanes)
        self.assertNotIn("stale_direct", view.lanes)
        self.assertIn("stale_direct", self.by_number[13011].perspectives[ALL_EDITORS].lanes)

    def test_shared_lanes_are_the_same_from_every_perspective(self) -> None:
        # `reply_window`, `overdue`, `oldest_wait` and `ready_bounded` are properties
        # of the pull request, so they are computed once and never per editor.
        for analysis in self.analyses:
            for view in analysis.perspectives.values():
                self.assertEqual(set(view.lanes) & set(analysis.shared_lanes), set())

    def test_each_editor_gets_their_own_fingerprints(self) -> None:
        # A fingerprint has to leave exactly one person's footprint out to mean
        # anything, so there is one per editor and none for the union.
        analysis = self.by_number[13009]
        self.assertIsNone(analysis.perspectives[ALL_EDITORS].content_fingerprint)
        self.assertIsNone(analysis.perspectives[ALL_EDITORS].attention_fingerprint)

        # #13009 claims @annevk's attention and nobody else's, so only their
        # attention fingerprint differs from everyone else's.
        self.assertTrue(analysis.perspectives["annevk"].has_attention_signal)
        self.assertFalse(analysis.perspectives[VIEWER].has_attention_signal)
        self.assertNotEqual(
            analysis.perspectives["annevk"].attention_fingerprint,
            analysis.perspectives[VIEWER].attention_fingerprint,
        )

    def test_a_footprint_moves_only_its_own_authors_fingerprint(self) -> None:
        # @annevk started the only review thread on #13009, so their fingerprint is
        # the one that leaves it out. Everybody else hashes the same public content,
        # @foolip included: they commented, but only the *newest* foreign activity is
        # hashed and @judy's later reply is what that is.
        views = self.by_number[13009].perspectives
        self.assertEqual(
            len({views[editor].content_fingerprint for editor in ("domenic", "domfarolino", "foolip", VIEWER)}),
            1,
        )
        self.assertNotEqual(
            views["annevk"].content_fingerprint,
            views[VIEWER].content_fingerprint,
        )

    def test_attention_fingerprint_ignores_updates_that_are_not_new_signals(self) -> None:
        original = self.by_number[13001]
        self.assertIn(
            "review-requested",
            {reason.code for reason in original.reasons_for(VIEWER)},
        )
        touched = replace(
            original.pr,
            updated_at=NOW,
            timeline=(*original.pr.timeline, self._comment("zcorpan")),
        )
        analysis = analyze_pull_request(touched, self.config, now=NOW)
        self.assertEqual(
            original.perspectives[VIEWER].attention_fingerprint,
            analysis.perspectives[VIEWER].attention_fingerprint,
        )


if __name__ == "__main__":
    unittest.main()

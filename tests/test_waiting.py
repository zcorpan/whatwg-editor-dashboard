from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from editor_dashboard.analysis import analyze_pull_request
from editor_dashboard.config import load_config
from editor_dashboard.github import load_fixture
from editor_dashboard.models import Activity, DependencyState
from editor_dashboard.references import dependency_references


ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
COMPLETE_CHECKLIST = """Fix the thing.

- [x] At least two implementers are interested
- [x] Tests have been added
- [x] Implementation bugs are filed
- [x] HTML AAM impact considered
- [x] MDN issue filed
- [x] Commit message is clear
"""


def activity(author: str, hours_ago: float, *, state: str | None = None, oid: str | None = None, kind: str = "IssueComment") -> Activity:
    at = NOW - timedelta(hours=hours_ago)
    return Activity(
        id=f"{kind}-{author}-{hours_ago}",
        kind=kind,
        author=author,
        author_association="MEMBER",
        created_at=at,
        updated_at=at,
        body="",
        url=None,
        state=state,
        commit_oid=oid,
    )


def review(author: str, hours_ago: float, state: str, oid: str = "head") -> Activity:
    return activity(author, hours_ago, state=state, oid=oid, kind="PullRequestReview")


class PRVariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "dashboard.yml")
        data = load_fixture(ROOT / "fixtures" / "sample_api_data.json")
        base = {pr.number: pr for pr in data.open_pull_requests}[13007]
        # A contributor PR with a complete checklist, opened a week ago, no
        # conversation yet: the baseline every test edits one thing of.
        cls.base = replace(
            base,
            title="Handle an edge case in forms",
            body=COMPLETE_CHECKLIST,
            author="heidi",
            labels=(),
            assignees=(),
            review_requests=(),
            created_at=NOW - timedelta(days=7),
            ready_for_review_at=NOW - timedelta(days=7),
            last_edited_at=None,
            head_oid="head",
            head_author="heidi",
            head_committed_at=NOW - timedelta(days=7),
            timeline=(),
            reviews=(),
        )

    def waiting(self, dependency_states=None, **changes):
        pr = replace(self.base, **changes)
        return analyze_pull_request(pr, self.config, now=NOW, dependency_states=dependency_states)


class WaitingOnTests(PRVariants):
    def test_unanswered_is_the_editors_move(self) -> None:
        analysis = self.waiting()
        self.assertEqual(analysis.waiting_on.party, "editors")
        self.assertEqual(analysis.waiting_on.who, ())

    def test_a_review_request_names_who(self) -> None:
        analysis = self.waiting(review_requests=("annevk",))
        self.assertEqual(analysis.waiting_on.who, ("annevk",))
        self.assertEqual(analysis.waiting_on.reason.label, "Waiting on @annevk")

    def test_an_editor_replying_last_hands_it_to_the_author(self) -> None:
        analysis = self.waiting(timeline=(activity("zcorpan", 5),))
        self.assertEqual(analysis.waiting_on.party, "author")
        self.assertNotIn("oldest_wait", analysis.shared_lanes)

    def test_the_author_answering_hands_it_back_to_the_editor_who_replied(self) -> None:
        analysis = self.waiting(timeline=(activity("zcorpan", 5), activity("heidi", 2)))
        self.assertEqual(analysis.waiting_on.party, "editors")
        self.assertEqual(analysis.waiting_on.who, ("zcorpan",))

    def test_a_merge_conflict_does_not_move_the_ball(self) -> None:
        analysis = self.waiting(mergeable="CONFLICTING")
        self.assertEqual(analysis.waiting_on.party, "editors")
        self.assertIn("merge-conflict", {blocker.code for blocker in analysis.blockers})

    def test_changes_requested_waits_on_the_author_until_they_act(self) -> None:
        requested = review("annevk", 10, "CHANGES_REQUESTED")
        self.assertEqual(self.waiting(reviews=(requested,)).waiting_on.party, "author")
        answered = self.waiting(reviews=(requested,), timeline=(activity("heidi", 1),))
        self.assertEqual(answered.waiting_on.party, "editors")

    def test_a_current_approval_with_nothing_outstanding_is_ready_to_land(self) -> None:
        analysis = self.waiting(reviews=(review("zcorpan", 3, "APPROVED"),))
        self.assertEqual(analysis.waiting_on.party, "merge")
        self.assertEqual(analysis.waiting_on.who, ("zcorpan",))
        self.assertIn("land", analysis.perspectives["zcorpan"].lanes)
        self.assertIn("land", analysis.perspectives["all-editors"].lanes)
        self.assertNotIn("land", analysis.perspectives["annevk"].lanes)

    def test_a_conflict_does_not_keep_an_approved_pr_out_of_land(self) -> None:
        analysis = self.waiting(reviews=(review("zcorpan", 3, "APPROVED"),), mergeable="CONFLICTING")
        self.assertIn("land", analysis.perspectives["zcorpan"].lanes)

    def test_an_approval_of_an_older_head_is_not_current(self) -> None:
        analysis = self.waiting(
            reviews=(review("zcorpan", 30, "APPROVED", oid="older"),),
            head_committed_at=NOW - timedelta(hours=2),
        )
        self.assertEqual(analysis.waiting_on.party, "editors")

    def test_approved_but_unticked_waits_on_that_item(self) -> None:
        body = COMPLETE_CHECKLIST.replace("[x] At least two implementers", "[ ] At least two implementers")
        analysis = self.waiting(body=body, reviews=(review("zcorpan", 3, "APPROVED"),))
        self.assertEqual(analysis.waiting_on.party, "implementers")
        self.assertIn("Implementer interest", analysis.waiting_on.reason.detail)

    def test_editorial_prs_have_no_outstanding_items(self) -> None:
        body = COMPLETE_CHECKLIST.replace("[x]", "[ ]")
        analysis = self.waiting(title="Editorial: tidy", body=body, reviews=(review("zcorpan", 3, "APPROVED"),))
        self.assertEqual(analysis.outstanding, ())
        self.assertEqual(analysis.waiting_on.party, "merge")

    def test_labels_outrank_the_conversation(self) -> None:
        self.assertEqual(self.waiting(labels=("agenda+",)).waiting_on.party, "meeting")
        self.assertEqual(self.waiting(labels=("needs implementer interest",)).waiting_on.party, "implementers")
        self.assertEqual(self.waiting(labels=("needs compat analysis",)).waiting_on.party, "compat")

    def test_a_hold_with_an_open_dependency_waits_on_it(self) -> None:
        url = "https://github.com/web-platform-tests/wpt/pull/123"
        body = COMPLETE_CHECKLIST + f"\nTests: {url}\n"
        analysis = self.waiting(
            labels=("do not merge yet",),
            body=body,
            dependency_states={url: DependencyState(url, "OPEN")},
        )
        self.assertEqual(analysis.waiting_on.party, "dependency")
        self.assertEqual(analysis.waiting_on.reason.label, "Waiting on web-platform-tests/wpt#123")

    def test_a_hold_whose_dependency_landed_falls_through_to_the_conversation(self) -> None:
        url = "https://github.com/web-platform-tests/wpt/pull/123"
        analysis = self.waiting(
            labels=("do not merge yet",),
            body=COMPLETE_CHECKLIST + f"\n{url}\n",
            reviews=(review("zcorpan", 3, "APPROVED"),),
            dependency_states={url: DependencyState(url, "MERGED")},
        )
        self.assertEqual(analysis.waiting_on.party, "merge")
        self.assertIn("dependency-landed", {reason.code for reason in analysis.shared_reasons})

    def test_a_hold_without_a_named_dependency_is_flagged_not_decided(self) -> None:
        analysis = self.waiting(labels=("do not merge yet",))
        self.assertEqual(analysis.waiting_on.party, "editors")
        self.assertIn("held-without-dependency", {reason.code for reason in analysis.shared_reasons})

    def test_long_waits_on_the_author_are_stale(self) -> None:
        analysis = self.waiting(
            timeline=(activity("zcorpan", 24 * 200),),
            created_at=NOW - timedelta(days=400),
            ready_for_review_at=NOW - timedelta(days=400),
            head_committed_at=NOW - timedelta(days=400),
        )
        self.assertIn("stale_author", analysis.shared_lanes)

    def test_active_means_recent_and_the_editors_move(self) -> None:
        reviewed = (review("zcorpan", 30, "COMMENTED"),)
        mine = self.waiting(reviews=reviewed, timeline=(activity("heidi", 2),), updated_at=NOW - timedelta(hours=2))
        self.assertIn("active", mine.perspectives["zcorpan"].lanes)
        theirs = self.waiting(reviews=reviewed, timeline=(activity("zcorpan", 2),), updated_at=NOW - timedelta(hours=2))
        self.assertEqual(theirs.waiting_on.party, "author")
        self.assertNotIn("active", theirs.perspectives["zcorpan"].lanes)

    def test_a_rereview_only_counts_while_it_is_the_editors_move(self) -> None:
        # zcorpan reviewed an older head and the author pushed since, so a re-review
        # is owed; then annevk replied, so the author has the next move.
        older = review("zcorpan", 48, "COMMENTED", oid="older")
        owed = self.waiting(reviews=(older,), head_committed_at=NOW - timedelta(hours=20))
        self.assertIn("rereview", owed.perspectives["zcorpan"].lanes)
        handed_back = self.waiting(
            reviews=(older,),
            head_committed_at=NOW - timedelta(hours=20),
            timeline=(activity("annevk", 2),),
        )
        self.assertNotIn("rereview", handed_back.perspectives["zcorpan"].lanes)


class SuggestedLabelTests(PRVariants):
    def suggestions(self, **changes) -> set[str]:
        return {suggestion.label for suggestion in self.waiting(**changes).suggested_labels}

    def test_an_approval_held_up_by_tests_suggests_needs_tests(self) -> None:
        body = COMPLETE_CHECKLIST.replace("[x] Tests", "[ ] Tests")
        approved = (review("zcorpan", 3, "APPROVED"),)
        self.assertIn("needs tests", self.suggestions(body=body, reviews=approved))
        # Unticked alone is too common to be worth a suggestion.
        self.assertNotIn("needs tests", self.suggestions(body=body))
        linked = body + "\nhttps://github.com/web-platform-tests/wpt/pull/9\n"
        self.assertNotIn("needs tests", self.suggestions(body=linked, reviews=approved))
        self.assertNotIn("needs tests", self.suggestions(body=body, reviews=approved, labels=("needs tests",)))

    def test_removals_suggest_compat_analysis(self) -> None:
        self.assertIn("needs compat analysis", self.suggestions(title="Remove the frobnicator"))
        self.assertNotIn("needs compat analysis", self.suggestions(title="Remove the frobnicator", labels=("compat",)))
        self.assertNotIn("needs compat analysis", self.suggestions(title="Editorial: remove a stray word"))


class DependencyReferenceTests(unittest.TestCase):
    def test_pr_urls_and_phrased_references(self) -> None:
        body = (
            "Tests: https://github.com/web-platform-tests/wpt/pull/5\n"
            "Depends on #12 and fixes #13.\n"
            "See whatwg/dom#7, blocked on whatwg/dom#8.\n"
            "<!-- https://github.com/whatwg/html/pull/99 -->\n"
        )
        self.assertEqual(
            dependency_references(body, "whatwg/html", 100),
            (
                "https://github.com/web-platform-tests/wpt/pull/5",
                "https://github.com/whatwg/dom/issues/8",
                "https://github.com/whatwg/html/issues/12",
            ),
        )

    def test_never_itself(self) -> None:
        self.assertEqual(dependency_references("after #100", "whatwg/html", 100), ())


if __name__ == "__main__":
    unittest.main()

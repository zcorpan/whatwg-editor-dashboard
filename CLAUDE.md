# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

## What this is

A build-time static site generator. A Python CLI fetches public `whatwg/html` data from GitHub's GraphQL API, applies deterministic rules, and emits `site/`: `web/` copied verbatim plus one `data.json`. The page is a dependency-free ES module that renders the JSON and keeps personal state in `localStorage`. No server, no runtime API calls.

[README.md](README.md) defines the lanes for users; [PRIVACY.md](PRIVACY.md) the public/private boundary.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate      # Python 3.12+; only dependency PyYAML
python -m pip install --requirement requirements.txt

# Offline demo, no token
python dashboard.py build --fixture fixtures/sample_api_data.json --now 2026-08-03T12:00:00Z --output site
python -m http.server --directory site 8000

# Live data; --state: incremental after the first run (--full forces a full fetch);
# --spec-repo: spec pages for routing; --evaluate: rule report
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site --state .state --spec-repo .spec-repo --evaluate

python dashboard.py validate
python -m unittest discover -s tests -v
python -m unittest tests.test_analysis.AnalysisTests.test_expected_mvp_lanes   # one test
```

`--log-level DEBUG` shows GraphQL retry detail. No linter, formatter or JS toolchain. `site/` is gitignored build output; CI uses Python 3.12.

## Architecture

A strict one-way chain; keep the layers apart.

1. [config.py](editor_dashboard/config.py): validates [dashboard.yml](dashboard.yml) eagerly into frozen dataclasses, raising `ValueError` with the field name. Every threshold, login and sampling size lives here. `editors` must be non-empty: there is no configured viewer, so it alone says whose queue can be read.
2. [github.py](editor_dashboard/github.py): `GraphQLClient` (urllib) and `fetch_repository_data`, paginating open PRs (repository connection) and recently closed PRs (`search`) from [graphql/](graphql/). `load_fixture` builds the same `RepositoryData` from JSON for tests and the demo. Entry point `fetch_with_snapshot`: with a stored [state.py](editor_dashboard/state.py) `Snapshot`, it searches for PRs updated since the last build, re-asks every open PR for fields that change without moving `updatedAt` (`_VOLATILE_FIELDS`), and refetches what moved through `*_by_id.graphql`, which reuse the connection queries' fragments so both paths fetch the same fields. The snapshot keeps drafts; `_repository_data` drops them (`_drop_drafts`) and sorts by number before anything else sees them, publishing the count as `metadata.drafts_excluded`.
3. [models.py](editor_dashboard/models.py): `from_graphql` normalizes nodes (lowercased logins, UTC times, `timelineFirst`/`timelineLast` merged and deduplicated). Snapshots are frozen and carry sampling-completeness flags. `ready_for_review_at` (latest `ReadyForReviewEvent`, else `created_at`) is a PR's age, first-response clock, and backlog start.
4. [analysis.py](editor_dashboard/analysis.py): `analyze_pull_request` gives each PR a `PRAnalysis`: lanes, `Reason` chips, blockers, fingerprints. Per-person lanes go into `perspectives` (each editor and helper, plus `ALL_EDITORS`); the rest is shared. `build_lanes` and `build_perspective_lanes` order them as `owner/repo#number` keys.
5. [metrics.py](editor_dashboard/metrics.py): the health payload: weekly moments (`trends.points`, `trends.issue_points`) and flow (`trends.buckets`, `trends.issue_buckets`) over twelve weeks, four judged `health.indicators`, `editors.team`/`editors.members` counts, and coverage.
6. [build.py](editor_dashboard/build.py): assembles `data.json` (lanes, items, metrics, `methodology`, build metadata), copies `web/*`, writes `.nojekyll`.
7. [web/app.js](web/app.js): resolves lane keys against `items` via `laneKeys` (`perspective_lanes` for per-person lanes, `lanes` for shared), then layers local state. `checkForFreshData` re-renders in place once `generated_at` is over 24 h old, gated on visibility and a throttle, so the render path must stay re-runnable from `applyDashboard`.

[checklist.py](editor_dashboard/checklist.py) is a leaf of `analysis.py`: it parses task-list items from a PR description (skipping fenced code), drops the author's answer after `: `, and `clean_label` unwraps links, emphasis and code spans but leaves `_underscore_` alone, since the labels are full of URLs. Handle only constructs seen in real descriptions; check a live `data.json` first. Bodies stay in memory: only a mention match, a sha256 and the checklist's counts and short labels reach `data.json`.

Lane ids (`active`, `direct`, `stale_direct`, `rereview`, `land`, `routed`, `reply_window`, `overdue`, `oldest_wait`, `ready_bounded`, `stale_author`, `triage`, `all`) are shared by `analysis.py` (`PERSPECTIVE_LANES` or `SHARED_LANES`), `build.py` `LANE_DESCRIPTIONS`, `config.py` `_ALLOWED_SUGGESTED_LANES` and `web/app.js` `LANE_ORDER`. Adding or renaming one touches all four.

### Issues

[issue_analysis.py](editor_dashboard/issue_analysis.py) applies the PR rules where they carry over, reusing `Reason`, `PerspectiveView` and `WaitingOn`. `build.py` runs it first, because:

- **An issue an open PR fixes folds into that PR's card** (`closedByPullRequestsReferences`, limited to PRs on the dashboard so a draft's issue keeps its card). Its direct signals are restated with `-on-linked-issue` codes, and the PR's content fingerprint hashes the issue's, a key added only when there is a linked issue so other PRs' stored state is untouched.
- **Only issues in some lane are published.** Most of ~2,500 open issues are dormant, so `reply_window`, `overdue`, `oldest_wait` and `triage` (no `topic:` label) consider only issues inside `issues.window_days`.

An issue's first response is the earliest comment, label or close by an editor or helper; labeling is how most issues are triaged.

### Whose move it is

`_waiting_on` gives each PR one `WaitingOn` (`party`, `who`, `since`, a lead `Reason`). First match wins:

1. Meeting labels (`agenda+`), then `party_labels` (`needs implementer interest`, `needs compat analysis`, `needs tests`). A label is a deliberate statement, so it outranks the conversation.
2. `hold_labels` (`do not merge yet`): an open dependency named in the description ([references.py](editor_dashboard/references.py), resolved each build by `_fetch_dependency_states`) makes it a `dependency` wait. A landed or missing dependency only adds a note and falls through.
3. An editor's unaddressed `CHANGES_REQUESTED` → `author`.
4. A current approval of the head → `merge`, unless a normative PR has unticked template items (`outstanding_items`); then that item's party.
5. An editor or helper other than the author acted last → `author` (`_responders`; a helper's reply also resets the `oldest_wait` clock). A helper's approval doesn't count as a reply, and a helper's own PR or issue is still a contributor's.
6. Otherwise `editors`. While a helper has taken part, their latest word isn't an approval, and no editor has spoken since (`helpers_reviewing`), it names the helper and is no editor's move: a helper and the author iterate alone. A helper's approval (`helpers_approved`) settles what they said before, hands the PR to the editors, and keeps it from being routed back to the walk's choice. Otherwise it names review requests and assignees, else the editors in the conversation.

**Conflicts and failing checks never move the ball**: common, often trivial, fixable by whoever lands it, so they stay chips and `ready_bounded` blockers. `oldest_wait`, `rereview`, `stale_direct` and `active` hold a PR only while it's the editors' move, which cut the first two from 203/117 to 160/67 on live data. `Editorial:`/`Meta:` PRs skip the checklist, as the template says.

`suggested_labels` are never applied. `needs tests`/`needs implementer interest` are suggested only when that unticked item holds up an approved PR; merely unticked is half of all normative PRs. The browser copies a `gh issue edit` command, since the build can't write and the page holds no token.

### Routing, expertise and the team section

[assignments.py](editor_dashboard/assignments.py) runs after all analysis, since routing walks every item, and writes only into item dicts and the `routed` lane. Its leaves:

- [spec_pages.py](editor_dashboard/spec_pages.py) maps diff hunks to multipage pages: an `xfuncname` diff driver puts each hunk's preceding heading in its header, and a `PageIndex` of `source` at main says which `split-filename` page holds it. Load-bearing: the pattern needs a capture group (else git shows only `<h4`), and git truncates hunk headers to 80 bytes, so headings are keyed by their first 80 bytes. About 78% of hunks attribute; renamed headings are dropped, not guessed. Issues attribute through spec links. `--spec-repo` is a bare clone (CI caches one per ISO week); page maps are cached in the snapshot by head and merge oid, keyed with `_CACHE_VERSION`, bumped when a map's content changes.
- [topics.py](editor_dashboard/topics.py): lowercased `label:` and `page:` keys. `topics:` (`config.topic_pages`) says which pages each label covers; a page's share of a diff is split among its labels and stays a `page:` key only when none covers it, so `knows:` needs labels only. Any key of the map is a topic label even without `topic:` (`removal/deprecation`, `accessibility`) and may cover no pages; `ignore_pages` say nothing. Locations are `page` or `page#section/subsection` (a heading's `id`, else a slug of its text), and `config.labels_for_location` prefers the innermost listed section. That is how the dashboard-only `element semantics` catch-all (whatwg/html#13322 prefers per-category labels) leaves `<link>`, `<a>`, `<style>` and `<ruby>` to their own labels. `--evaluate` lists section entries no heading matches, since a typo would silently fall back to the page.
  Labels compare case-insensitively (`config.label_key`), with no aliases: when a label is renamed on GitHub, update `dashboard.yml` and dispatch a `full` build, because a rename doesn't move `updatedAt`. `LabelSuggester` suggests the one label covering an unlabeled change's main page, only from the repository's own list (`_fetch_labels`, every build), and the build warns in `metadata.warnings` about labels `dashboard.yml` names that GitHub lacks (`config.named_labels`), except deliberate non-labels listed in `dashboard_labels:`. `--evaluate` reports its precision against merged PRs' labels.
- [routing.py](editor_dashboard/routing.py): `(affinity + prior) × share / (1 + load)`, oldest ready first. Affinity is the share of the item's topics the person declares (`knows:`, `config.knows`). Items waiting on `routing.start` walk as one batch that day so load spreads the old backlog; ties go by a hash of item and login, since login order handed every tie to annevk.

**Expertise is declared, not computed, on purpose.** Routing by decayed history credit missed design done elsewhere (srcset was designed in another spec and landed by others) and followed volume, so annevk led nearly every topic. [expertise.py](editor_dashboard/expertise.py) and `evaluate.expertise_report` survive only as the manual `dashboard.py expertise` report. Full builds still fetch merged history since `expertise.since` for the label suggester and that report.

**The walk includes every recent contributor item, answered or not.** Only unanswered, unnamed items *show* routing, but dropping answered ones would move every later decision in their load window. Retired editors (share 0) are never routed to but appear in "also knows"; helpers take PRs only. A GitHub team is suggested when nobody in the pool declares the item's topics, or for implementer interest or a horizontal label.

[accountability.py](editor_dashboard/accountability.py) builds `metrics.people`: a row per active editor, helper, and anyone else who reviewed at least `contributors.min_reviews_of_others` others' PRs (retired editors only while active), with twelve weekly columns, routed items answered in time since `routing.start`, and what waits on them now. Counts of public events, never causes.

### Queue perspectives

Six lanes depend on who is asking; the rest belong to the item. `analyze_pull_request` computes `_direct_reasons` and `_rereview_reasons` per person into a `PerspectiveView`, beside the shared `shared_lanes`/`shared_reasons`. `ALL_EDITORS` (`"all-editors"`, not `"all"`, a lane name) is the union, built from the *merged* signal lists so the direct/stale-mention `elif` stays one decision. `data.json` mirrors the split (`lanes`/`items[].reasons` shared, `perspective_lanes[key]`/`items[].perspectives[key]` per person), and the browser concatenates, the person's reasons first.

**"The editors' move" isn't "this person's move".** Read as one person, an item waiting on the editors is somebody else's when it's their own PR (unless it waits on its author), or when their latest comment or review follows the other side's latest move (`_latest_author_move` for PRs; for issues, the latest comment by anyone not an editor, helper or them). Since replies already move `waiting_on`, this mostly covers a helper's approval and an own issue nobody else answered. It gates `active`, `rereview` and `stale_direct`; a direct signal keeps an item `active` only while unanswered (`pending_direct`: a mention until a later reply; a request or assignment, which have no time of their own, until that reply), and `merge` counts only for the approvers, as in `land`. Published as `perspectives[key].others_move`, which `suggestedItems` also uses to skip shared-lane items; the shared lanes stay whole.

**Identity and perspective are separate, and that's load-bearing.** Fingerprints are per person in `perspectives[login]`; the union gets `None`, since a fingerprint must leave out exactly one person's footprint. The browser reads the fingerprints of `settings.identity` (**You are**) whatever perspective is shown. One control for both was rejected: reading `@annevk`'s queue would compare your addressed hashes against theirs, resurfacing everything, and Address would overwrite your value; namespacing state per selection would split one person into six workspaces. Keeping them apart needed no state change, so `STATE_VERSION` stayed 1. Identity starts unset and `createPRCard` then omits Address. That's not a degraded mode: the site is public and "until somebody else changes it" needs a somebody. Pins and snoozes don't use fingerprints, so they always work.

Both `<select>`s are filled from `perspectives.options`. A `Reason`'s `tone` becomes a `chip <tone>` class and must be one of the `.chip.*` rules in [web/style.css](web/style.css) (`urgent`, `attention`, `positive`, `warning`, `muted`); `neutral`, the JS default, is deliberately unstyled.

Health statuses (`on_track`, `watch`, `off_track`, `unknown`) are decided in `metrics.py` `_status` and only styled by the browser, which never re-judges a number. The four indicators ask the same two things of PRs and issues: the share answered inside the target, and how many still wait past it. Both count a helper's reply (`PRAnalysis.first_response`, which also defines `reply_window` and `overdue`) and publish `within_target_by_editors`; editor impact keeps `first_editor_response`. The unanswered-issue count only looks at issues opened within `issues.window_days` of each moment, like the overdue lane, so its last point equals that lane's issue count.

### The two fingerprints

Both are computed in `analysis.py` and drive different behavior; don't conflate them.

- `content_fingerprint` hashes public content *without one person's footprint*: title, body hash, draft state, head OID, labels, other people's assignments and review requests, check rollup, the newest comment or review by anybody else, and counts of review threads others started. Address stores it; the item returns when it changes.
- `attention_fingerprint` hashes the direct-request and re-review codes and timestamps, minus the timestamps of `_STATE_ATTENTION_CODES`, which stand in with `pr.updated_at`. Seen stores it; a new signal makes the item unseen.

Changing either payload silently invalidates stored state: treat both as a compatibility surface.

**Self-independence is the point.** Address must mean "until somebody else changes it". Reviewing moves `updated_at`, adds a timeline item, clears your review request, sets `review_decision` and opens threads, and hashing any of those brought every addressed item back next build. So `updated_at` isn't hashed (it can't be attributed); the state it stood for is hashed field by field. Deliberately, a renewed review request alone doesn't resurface an item, and only the *newest* comment by somebody else is hashed, because your own comment on a long PR shifts the sampling window. Unattributable state (labels, body, head) stays in, so those changes bring an item back for everyone.

**`mergeable` isn't hashed: GitHub computes it lazily.** A cold query returns `UNKNOWN` and schedules the answer; minutes after one build, 258 of 277 open PRs reported a different value on identical heads. It also made `ready_bounded` (which needs `MERGEABLE`) show 15 of 133. The fetch layer fixes the lane: `_resolve_unknown_mergeability` re-asks for `UNKNOWN` PRs after the closed search, backing off for just under a minute, and warns about the rest. Both halves stay: the retry makes the lane right, and leaving it out of the hash keeps a later flip from resurfacing items.

### Browser-local state

`localStorage` is the only user-owned data and has no migration path, so its shape is a compatibility surface too. `settings.perspective` and `settings.identity` are validated against `perspectives.options` at read time, since the configured people can change between builds; a missing perspective falls back to the default, a missing identity to browsing. The key is `whatwg-editor-dashboard:v${STATE_VERSION}:${location.pathname}` (path-namespaced; see the README caveat). `loadLocalState` discards any other `version`, so bumping `STATE_VERSION` wipes everyone's state. Items are keyed `owner/repo#number`. Renaming a direct-request or re-review code makes the affected items unseen, since codes are hashed into `attention_fingerprint`.

### Determinism and sampling honesty

- **Deterministic output.** `now` is threaded through analysis, metrics and build, so a fixture build is byte-identical. Sorts break ties on number. Never call `datetime.now()` below `dashboard.py`.
- **Incomplete samples are visible.** Only the first and last `timeline_each_end` comments and the latest `reviews_per_pr` reviews are fetched (unfiltered, since the connection takes one `author:`; split by login in `metrics.py`; `coverage.review_connections_truncated` counts PRs with more). `timeline_sample_complete`, `first_response_known`/`first_editor_response_known` and `review_threads_sample_complete` gate metrics; `coverage` publishes the shortfall. Gate any new middle-history metric the same way.

Two GitHub traps each produced a plausible zero once, so tests assert real values:

- **`timelineItems.totalCount` ignores `itemTypes`**, counting commits, labels and assignments. Completeness comes from `pageInfo.hasNextPage` (`_timeline_sample_complete`). Comparing against `totalCount` marked all 283 open PRs incomplete and reported `known_without_editor_response: 0`.
- **`authorAssociation` never returns `FIRST_TIME*` here**, only `CONTRIBUTOR`, `MEMBER` or `NONE`, so first-time detection keys off `NONE`.

### GraphQL resilience

GitHub answers GraphQL timeouts with 502/504, or with "Something went wrong while executing your query" inside an HTTP 200. On those only, `GraphQLClient.execute` halves `pageSize` and keeps the cap for the build (`_page_size_cap`); other errors are just retried. 403 with `X-RateLimit-Remaining: 0` fails fast; secondary-rate-limit 403s wait at least 60 s. `fetch_repository_data` dedupes PRs seen twice and drops open nodes that also came back closed, noting it in the published `metadata.warnings`.

Retries are bounded by time, not count: delays double from 37.5 s to a 5 min cap, and one hour-long budget (`DEFAULT_RETRY_BUDGET_SECONDS`) is shared by the whole build, after which requests fail on their first error. `max_retries` only guards against zero delays. The budget and the workflow's `timeout-minutes` are one decision, pinned together in `test_github.py`. The `except` catches `OSError` and `http.client.HTTPException`, because `IncompleteRead` isn't a `URLError` and once escaped the loop.

## Design intent and deliberate non-goals

Several apparent gaps are decisions. Don't "fix" them without a deliberate change of direction.

**Lanes, not a score.** A single ranking was rejected: lanes plus visible `Reason` chips are the product. Scoring may only order items within a lane. A "sort current lane" select was removed: each lane already has its defining order, and the select couldn't move Suggested next, so it looked broken. `orderedVisibleItems` applies the lane's order with pins hoisted (stable sort). Wanting another order means a lane is missing.

**Recency leads "what needs me now".** `active` holds items active within `activity_window_days` (30) that you're involved in, newest first, and Suggested next emits all of it after only the bounded `reply_window` lead. The original design put direct requests first, oldest first; on real data a 2016 mention on a PR untouched for 953 days led the queue while only 39 of 283 PRs had moved in 30 days. Preserve: attention lanes (`active`, `direct`, `rereview`) are newest first; `oldest_wait`, `reply_window` and `overdue` oldest first, since fairness is what they measure (for `reply_window` that's deadline order). When a PR has several direct signals, the **freshest** represents it.

**Direct signals expire; current state doesn't.** Review requests and assignments are current state that GitHub clears on review, so they persist. A mention is an event with no clearing, so it counts only inside the activity window, then moves to `stale_direct` rather than vanishing.

**`oldest_wait` is the current wait, not age**: time since the latest contributor activity no editor or helper answered. Bots never reset it (`is_bot()`).

**The Suggested next interleave** is a bounded `reply_window` lead, then active work, then a cycle, so incoming work can't starve long waits while new PRs still get a fast reply. Rejected: the whole unanswered population first, making it the first cycle lane, and overdue PRs in the lead.

**`reply_window` and `overdue` split "never answered" on the target.** A single `new` lane capped at the target dropped a PR the day it missed, showing only successes. Missing the target now *moves* it, and the chip escalates from `new-untriaged` to `first-response-overdue` at the same instant (both read `_response_target_hours`; a test pins it). The newest bucket is `first_response_mature: False`, so `reply_window` is exactly what the rate will judge next week, where a reply turns a scheduled miss into a hit; an overdue verdict is fixed. That's why `first_response_lead` draws from `reply_window` only, and why it's the one exception to `active` leading.

**No GitHub unread state.** The notifications endpoint needs a classic PAT, too risky in a workflow that publishes. "Unseen" means not opened in this browser; real sync waits for an OAuth service.

**No LLM.** Any future summary stays advisory: it may never hide an item, make it ready, or outrank a direct request, and must be cached by content fingerprint.

**The health view is a trend, not a scoreboard.** It replaced counts and 7/28/90-day tables that couldn't say "is this good, and is it improving". Now: four indicators, each with a good direction, a change and a chart, then how work divides. Open and closed counts are left to foolip's spec-stats (linked) rather than shown twice with different draft rules. A new number must name the decision it changes. Weekly moments are *reconstructed*: every open item plus every one closed inside `history_days` is present, so counts at any moment in the window are exact, and the window stays inside `history_days` for that reason. The newest bucket never counts toward a rate.

**One colour per editor, by identity.** Five slots on `:root` (one set per scheme) serve the columns, bars and legend; `editorSeries` gives slot *n* to the *n*th editor in the alphabetical `metrics.editors.members`, so one legend serves the page. The set was checked with the dataviz `validate_palette.js` on **every pair**, both schemes, all three colour blindnesses (worst ΔE 8.5 protan/deutan, 12.3 tritan on white; 8.6/12.7 dark; 15.7 normal; every slot ≥ 3:1 on its surface). Lightness carries much of the separation, so don't re-step slots without re-running it. A sixth editor folds into "other editors"; `SERIES_SLOTS` grows only with a fresh run. Colour is never the only channel: keep the legend, fixed stack order and "Show the numbers" tables. Chart SVGs set `forced-color-adjust: none`, since the hues are data.

**A merge is credited to one editor**: `_first_editor_reviewer`, the first editor other than the author to review before the merge (`buckets[].merged_by_first_reviewer`), so columns add up to merges. Every review still counts in `reviews_submitted`, and the group share asks whether *anyone* reviewed.

Count line charts use a focused y-range (257 → 283 is flat against zero), with tick labels saying where the axis starts. The merge columns keep a zero baseline and label every column: twelve weekly integers have room, and the question is "how many that week".

**Wording is a requirement.** Impact figures describe order, never cause ("merged after a review"; `test_metrics.py` guards it). An editor's own merged PRs are left out of their own review share, not counted as misses; the group share keeps them. The task list is a *description checklist*, never "requirements complete". Its state is a `::marker` of U+2610/U+2611, safe because markers reach the accessibility tree (Chrome's `ListMarker`); `font-family: monospace` on it is load-bearing, since in `system-ui` U+2611's box is bigger than U+2610's.

**All GitHub-derived text is untrusted** and reaches the page only as text nodes.

**The workflow is read-only apart from the Pages deploy.** Metrics are recomputed each run, not stored. Daily aggregates, if added, belong on a data branch, the only thing that may need `contents: write`.

**Deferred:** other WHATWG repositories (`dashboard.yml` is single-repo, though the layers allow more), cross-device state, OAuth, and assessing WPT coverage, implementer interest, compat risk or consensus.

## Constraints enforced by tests

- `test_build.py`: no `body` or `comments` keys anywhere in the payload, and every perspective's lane keys resolve against `items`. Only derived signals and short checklist labels reach `data.json`.
- `test_build.py`: no `innerHTML` in `web/app.js`, and `web/index.html` keeps its `Content-Security-Policy`. Build DOM with `element()` and text nodes.
- 10 of the 13 test modules load the real [dashboard.yml](dashboard.yml) and [fixture](fixtures/sample_api_data.json) at `NOW = 2026-08-03T12:00:00Z` and assert named PRs and exact orderings, so a threshold or fixture change breaks unrelated-looking tests. Re-derive the expectations; don't loosen them.
- `web/` is vanilla ES modules: no build step, third-party scripts or fonts, and no network calls but same-origin `data.json`.

## Configuration notes

`dashboard.yml` is the only knob surface. `editors` applies retroactively to the 90-day sample, so editing it changes past metrics; it is also the editor-impact rows, the queue select options (with helpers), and who gets fingerprints. `suggested_next.cycle` may only use `_ALLOWED_SUGGESTED_LANES`, without duplicates (`active`, `direct`, `reply_window`, `routed` and `all` have fixed places). `suggested_next.first_response_lead` is 0–10; 0 disables the lead but keeps the lane.

## CI

[.github/workflows/dashboard.yml](.github/workflows/dashboard.yml) runs on push to `main`, daily, and on dispatch. It tests before building; restores the previous snapshot from the Actions cache into `.state/` and saves it under a per-run key (incremental without `contents: write`; a miss means a full fetch); uses only the job `GITHUB_TOKEN` (`contents: read` to build, `pages: write` + `id-token: write` only in deploy); and pins actions to SHAs. Never add `pull_request_target` or anything that runs code from a `whatwg/html` branch.

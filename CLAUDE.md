# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A build-time static site generator. A Python CLI fetches public `whatwg/html` PR data from GitHub's GraphQL API, applies deterministic prioritization rules, and emits `site/` (copied `web/` assets plus a single `data.json`). The browser page is a plain, dependency-free ES module that renders that JSON and keeps personal workflow state in `localStorage`. There is no server and no runtime API call.

Read [README.md](README.md) for the product-level lane definitions and [PRIVACY.md](PRIVACY.md) for the public/private boundary.

## Commands

Setup (Python 3.12+, only dependency is PyYAML):

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install --requirement requirements.txt
```

Build the deterministic offline demo (no token needed) and serve it:

```bash
python dashboard.py build --fixture fixtures/sample_api_data.json --now 2026-08-03T12:00:00Z --output site
python -m http.server --directory site 8000
```

Build from live public GitHub data:

```bash
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site
```

Validate `dashboard.yml`, and run tests:

```bash
python dashboard.py validate
python -m unittest discover -s tests -v
python -m unittest tests.test_analysis -v                                     # one module
python -m unittest tests.test_analysis.AnalysisTests.test_expected_mvp_lanes  # one test
```

`--log-level DEBUG` on `dashboard.py` surfaces GraphQL retry detail.

There is no linter, formatter, or JS toolchain. `site/` is build output and gitignored; CI builds on Python 3.12.

## Architecture

The pipeline is a strict one-way chain; keep the layer boundaries intact when extending.

1. [config.py](editor_dashboard/config.py) — parses and validates [dashboard.yml](dashboard.yml) into frozen dataclasses. All thresholds, logins, sampling sizes, and lane cycling live here; validation is eager and raises `ValueError` with a field-qualified message. `editors` must name at least one login: there is deliberately no configured viewer, so it is the only thing saying who the queue can be read as.
2. [github.py](editor_dashboard/github.py) — `GraphQLClient` (urllib, no HTTP library) plus `fetch_repository_data`, which paginates two queries from [graphql/](graphql/): open PRs via the repository connection, recently closed PRs via `search`. `load_fixture` produces the same `RepositoryData` from JSON, which is how every test and the demo build run.
3. [models.py](editor_dashboard/models.py) — `PullRequestSnapshot.from_graphql` / `Activity.from_graphql` normalize raw GraphQL nodes: logins lowercased, timestamps parsed to UTC, the `timelineFirst`/`timelineLast` aliases merged and deduplicated. Snapshots are frozen and carry their own sampling-completeness flags.
4. [analysis.py](editor_dashboard/analysis.py) — the heart. `analyze_pull_request` produces one `PRAnalysis` per PR: lane membership, `Reason` evidence chips, blockers, and the two fingerprints. The four attention lanes are computed once per *perspective* (each configured editor, plus `ALL_EDITORS` for the union) into `PRAnalysis.perspectives`; everything else is shared. `build_lanes` orders the shared lanes and `build_perspective_lanes` the attention ones, both as lists of `owner/repo#number` keys.
5. [metrics.py](editor_dashboard/metrics.py) — aggregates `PRAnalysis` values into the health view's payload: a twelve-week weekly trend (`trends.points` backlog moments, `trends.buckets` flow), three judged `health.indicators`, `editors.team` and a per-editor `editors.members` list of merge, first-reply and review counts, and sampling coverage.
6. [build.py](editor_dashboard/build.py) — assembles the `data.json` payload (`schema_version`, lanes, items, metrics, methodology, build metadata), copies `web/*` into the output, writes `.nojekyll`.
7. [web/app.js](web/app.js) — fetches `data.json`, resolves lane key lists against `items` through `laneKeys` (which reads `perspective_lanes` for an attention lane and `lanes` for a shared one), and layers browser-local state on top. `checkForFreshData` re-fetches and re-renders in place when a long-lived tab's `generated_at` is older than the 24 h build interval, gated on tab visibility and a throttle; keep the whole render path re-runnable from `applyDashboard`.

[checklist.py](editor_dashboard/checklist.py) sits outside the chain as a leaf called from `analysis.py`: it parses GitHub task-list items out of a PR description while skipping fenced code blocks. PR bodies stay on the in-memory snapshot; `analysis.py` reduces each one to a mention match, a sha256 for the content fingerprint, and a `Checklist`, and only the checklist's counts plus its short labels cross into `data.json`.

Lane identifiers (`active`, `direct`, `stale_direct`, `rereview`, `reply_window`, `overdue`, `oldest_wait`, `ready_bounded`, `all`) are a shared vocabulary across `analysis.py`, `build.py` `LANE_DESCRIPTIONS`, `config.py` `_ALLOWED_SUGGESTED_LANES`, and `web/app.js` `LANE_ORDER`. Adding or renaming one means touching all four. `analysis.py` also splits them into `PERSPECTIVE_LANES` and `SHARED_LANES`, and a new lane has to join one of the two.

### Queue perspectives

The four attention lanes depend on *which editor is asking*; the other five are properties of the pull request. `analyze_pull_request` therefore computes `_direct_reasons` and `_rereview_reasons` once per configured editor and stores each result as a `PerspectiveView` in `PRAnalysis.perspectives`, alongside `shared_lanes` and `shared_reasons` which every perspective agrees on. `ALL_EDITORS` (`"all-editors"`, deliberately not `"all"`, which is a lane name) is the union, built from the *merged* signal lists rather than from the per-editor lane sets so that the direct/stale-mention `elif` stays one decision.

`data.json` mirrors that split rather than publishing six near-copies: `lanes` and `items[].reasons` hold the shared half, `perspective_lanes[key]` and `items[].perspectives[key]` the per-editor half, and the browser concatenates. The perspective's own reasons come first, which is the order `analyze_pull_request` used to emit as one list.

**Identity and perspective are two different things, and the split is load-bearing.** Both fingerprints are computed per editor and published inside `perspectives[login]`; the union gets `None` for both, because a fingerprint has to leave exactly one person's footprint out to mean anything and the union is not a person. The browser reads the fingerprints of `settings.identity` — the **You are** control — whichever perspective's lanes are on screen.

Collapsing the two into one control was considered and rejected. If the perspective select were also the identity, reading `@annevk`'s queue would compare your stored addressed hash against theirs, so every addressed item would reappear, and pressing Address there would overwrite your own value. Namespacing `localStorage` per selection fixes the corruption but gives one person six separate workspaces, where pins set in their own view vanish in the team view. Keeping identity separate needs no state-shape change at all: only which hash the comparison reads changes, so `STATE_VERSION` stayed at 1.

The identity starts unset, and `createPRCard` omits the Address button while it is. That is not a degraded mode to be fixed: the site is public, most readers are not editors, and "addressed until somebody else changes it" has no meaning without a somebody. Pins and snoozes are not fingerprint-derived, so they keep working; only seen and addressed need to know whose footprint to exclude.

Health indicator statuses (`on_track`, `watch`, `off_track`, `unknown`) are decided in `metrics.py` `_status` and only styled in the browser; the page never re-judges a number it was handed.

Both queue `<select>`s are populated from `perspectives.options`, so no lane or option vocabulary has to be kept in step between the two languages. A `Reason`'s `tone` becomes a `chip <tone>` class, so it has to be one of the `.chip.*` rules in [web/style.css](web/style.css) (`urgent`, `attention`, `positive`, `warning`, `muted`) — `neutral` is the JS default and is deliberately unstyled.

### The two fingerprints

`analysis.py` computes both, and they drive distinct browser behaviours — do not conflate them:

- `content_fingerprint` hashes the PR's public content *excluding one editor's own footprint* (title, body hash, draft state, head OID, labels, other people's assignments and review requests, check-rollup state, the newest comment/review by anybody else, and the counts of review threads somebody else started). "Address until changed" stores it; the PR reappears when it changes.
- `attention_fingerprint` hashes only the direct-request and re-review reason codes/timestamps, and drops the timestamps of the two current-state codes in `_STATE_ATTENTION_CODES`, which carry `pr.updated_at` as a stand-in. "Seen" stores it; a new attention signal makes the item unseen again.

Changing what goes into either hash silently invalidates users' stored state, so treat the payloads as a compatibility surface.

**Self-independence is the point of the content fingerprint, not an optimization.** "Address until changed" has to mean "until somebody else changes it". Reviewing a PR moves `pr.updated_at`, appends a timeline item, clears the reviewer's own review request, sets `review_decision` and opens review threads, so hashing any of those made every addressed item reappear at the next build — the editor's own reply was read as a change. `pr.updated_at` is therefore not hashed at all (it cannot be attributed to anyone); the public state it stood proxy for is hashed field by field instead. Two consequences are deliberate: a renewed review request with no other change does not resurface an addressed PR, and only the *newest* comment by somebody else is fingerprinted, because that editor's own comment on a PR with more than `2 * timeline_each_end` comments shifts the sampling window and would move a list-based hash. What is still unattributable — labels, the PR body, the head commit — stays in the hash, so editing a description or adding a label does bring the PR back for everyone.

**`mergeable` is not hashed, because it is not a property of the PR at query time.** GitHub computes mergeability lazily: a cold query returns `UNKNOWN` and only schedules the real answer. Compared against one deployed build, 258 of 277 open PRs reported a different `mergeable` value minutes later on identical `updatedAt` and head commits — the build had read `UNKNOWN` for 259 of them — so hashing it returned nearly every addressed PR at the next build. The same lazy answer also made `ready_bounded`, which requires `mergeable == "MERGEABLE"`, report 15 candidates where 133 PRs were actually mergeable. That is fixed in the fetch layer, not the fingerprint: `github.py` `_resolve_unknown_mergeability` re-asks for the `UNKNOWN` PRs by node id after the closed-PR search, in backing-off rounds bounded at just under a minute, and warns about whatever is still unknown. Both halves of the fix have to stay — the retry pass makes the *lane* right, and leaving `mergeable` out of the hash keeps a later flip from resurfacing an addressed PR.

### Browser-local state

`localStorage` holds the only user-owned data in the system, and there is no migration path, so its shape is as much a compatibility surface as the fingerprints. `settings.perspective` and `settings.identity` are both validated against `perspectives.options` at read time rather than at load time, because the configured editor list can change between builds: a perspective that is no longer in it falls back to the default rather than resolving to an empty queue, and an identity that is no longer in it falls back to browsing anonymously rather than to a fingerprint that does not exist. The key is `whatwg-editor-dashboard:v${STATE_VERSION}:${location.pathname}` — path-namespaced against other projects on the same origin (see the caveat in [README.md](README.md)), and version-gated: `loadLocalState` discards any payload whose `version` differs, so bumping `STATE_VERSION` silently throws away every user's seen/addressed/pinned/snoozed state. Per-item state is keyed by `owner/repo#number`. Renaming a direct-request or re-review `Reason` code has the same effect on a smaller scale, because those codes are hashed into `attention_fingerprint` and every affected item becomes unseen again.

### Determinism and sampling honesty

Every classification is rule-based; no LLM, no heuristics that cannot be shown as evidence (see [Design intent](#design-intent-and-deliberate-non-goals)). Two properties the code deliberately maintains:

- **Deterministic output.** `now` is threaded explicitly through analysis, metrics, and build so a fixture build is byte-reproducible. Sorts always break ties on PR number. Never call `datetime.now()` below `dashboard.py`.
- **Incomplete samples are visible, not silent.** Only the first and last `timeline_each_end` comments/reviews are fetched, and only the latest `reviews_per_pr` reviews of any author (the connection takes one `author:` at a time, so it is fetched unfiltered and split by login in `metrics.py`; `coverage.review_connections_truncated` publishes how many PRs had more). `timeline_sample_complete`, `first_editor_response_known`, and `review_threads_sample_complete` gate which metrics are reported; the `coverage` block in `data.json` publishes the shortfall. When adding a metric that needs middle history, gate it the same way rather than assuming completeness.

Two GitHub API traps have already cost this codebase a silent wrong answer each. Both produced plausible zeros rather than errors, so assert on real values in tests instead of trusting that a number exists:

- **`timelineItems.totalCount` ignores the `itemTypes` filter.** It counts commits, labels and assignments too, so it can never be compared against the filtered `nodes`. Completeness comes from `pageInfo.hasNextPage` ([models.py](editor_dashboard/models.py) `_timeline_sample_complete`). Comparing against `totalCount` marked 283 of 283 open PRs incompletely sampled, which turned every "no editor has responded" into "unknown" and reported `known_without_editor_response: 0`.
- **`authorAssociation` never returns `FIRST_TIME*` on this repo.** It returns `CONTRIBUTOR`, `MEMBER`, or `NONE`, so first-time-contributor detection keys off `NONE`. Checking only for `FIRST_TIMER`/`FIRST_TIME_CONTRIBUTOR` left the flag false on all 283 PRs and every first-time-contributor metric at zero.

### GraphQL resilience

GitHub returns HTTP 502/504 for GraphQL request timeouts, and this query is nested-connection heavy. `GraphQLClient.execute` retries with exponential backoff and, on 502/504 only, halves `pageSize` and retains the reduced cap for the rest of the build (`_page_size_cap`). 403 with `X-RateLimit-Remaining: 0` fails fast; secondary-rate-limit 403s get at least a 60 s delay. `fetch_repository_data` also deduplicates PRs that appear twice across pages and drops open-PR nodes that also came back in the closed search, appending a human-readable note to `metadata.warnings` (which is published in `data.json`) rather than failing.

## Design intent and deliberate non-goals

This codebase is milestones 1–2 of a longer plan, and several apparent gaps are decisions rather than omissions. Don't "fix" these without a deliberate change of direction.

**Lanes, not a score.** A single numeric ranking across all open PRs was considered and rejected: buckets plus visible `Reason` chips are the product. Any scoring may only order items *within* a lane, and the reasons must always be shown. The `active` lane outranks everything else except the bounded `reply_window` lead.

A "sort current lane" select (checklist completion, unchecked boxes, contributor wait, update time, age) existed and was removed. Each lane already arrives ordered by the rule that defines it, so a second ordering control competed with the lane list for the same job, and it could not reorder "Suggested next" above it — which made it look broken, because the top of the page never moved. `orderedVisibleItems` therefore applies exactly one ordering: the lane's own, with this browser's pins hoisted, relying on `Array.prototype.sort` being stable. Wanting a different order is a sign a lane is missing, not that the lanes need a sort menu.

**Recency is the primary axis for "what needs me now".** The top of the queue answers "which reviews am I currently in the middle of", not "which claim on my attention is oldest". The `active` lane holds PRs with public activity inside `activity_window_days` (30) where the editor is involved — a direct signal, a re-review owed, or a review previously submitted. It sorts newest activity first, and `suggested_next` emits all of it before the cycle — behind only the bounded `reply_window` lead.

This inverts the original design, which put every direct request first, oldest first. On real `whatwg/html` data that buried the live work: of 283 open PRs only 39 had any activity within 30 days, yet 35 direct requests preceded the cycle, led by a mention from 2016 on a PR untouched for 953 days. Age is a poor proxy for actionability on a decade-old backlog.

Consequences to preserve:

- Newest-first is the default for the attention lanes (`active`, `direct`, `rereview`). `oldest_wait`, `reply_window` and `overdue` sort oldest-first, because fairness to waiting contributors is exactly what they exist to measure. For `reply_window` that is deadline proximity, which coincides with oldest-first only because every member shares one target.
- When a PR carries several direct signals, the **freshest** one represents it. Taking the oldest meant one stale mention outranked a review request filed the same week.

**Direct signals expire; current state does not.** A review request or assignment is current API state — GitHub clears it when the editor reviews — so it persists as a direct request indefinitely. A mention is a past *event* with no clearing mechanism; once `@zcorpan` appeared in a 2016 comment, that PR claimed a direct request forever. Mentions therefore count only inside the activity window. Expired ones move to a separate stale lane rather than being discarded, so an old mention stays findable without leading the queue.

**`oldest_wait` measures the current wait, not PR age.** It is time since the latest non-editor human activity that no editor answered. A five-year-old PR whose author replied yesterday has a one-day wait; a three-week-old PR with no editor response has a three-week wait. Bot activity must never reset this clock — hence `is_bot()` filtering throughout [analysis.py](editor_dashboard/analysis.py).

**The `suggested_next` interleave resolves a specific tension.** A bounded `reply_window` lead, then active work, then a cycle, so that incoming work cannot permanently starve long-waiting contributors while new PRs still get a fast turnaround. Changing the lead size or the cycle changes that balance. The cycle is what keeps the backlog from being abandoned now that the top of the queue is recency-driven. Three alternatives were considered and rejected: emitting the whole unanswered-PR population first, making it the first cycle lane, and including overdue PRs in the lead.

**`reply_window` and `overdue` split "never got a first response" on the target.** A single `new` lane once required `age <= initial_editor_response_days`, so a PR left it on the very day it missed the target — it could only show successes in progress, never failures. The pair is unbounded instead: missing the target *moves* a PR from `reply_window` to `overdue`, and the reason chip escalates from `new-untriaged` to `first-response-overdue` on the same boundary (both read `_response_target_hours`, and a test pins them to one instant). Both stay distinct from `oldest_wait`, which covers stalled conversations an editor *did* join at some point.

Why the split earns two lanes: `metrics.py` `_bucket_metrics` marks the newest weekly bucket `first_response_mature: False`, so `reply_window` is exactly the set of PRs the first-response rate has not yet judged and will judge next week. Replying converts a scheduled failure into a success. A PR in `overdue` already sits in a mature bucket and its verdict is fixed, so hoisting it up the queue cannot move the indicator. That is the whole justification for `suggested_next.first_response_lead` drawing from one lane and not the other — and for the lead being the one documented exception to "the `active` lane outranks everything else".

**No GitHub notification / unread state, on purpose.** The REST notifications endpoint requires a *classic* PAT (fine-grained PATs and GitHub App tokens are unsupported), and putting such a token in a workflow that publishes public output is an unacceptable leakage risk. "Unseen" therefore means "this browser has not opened this public attention signal". Proper unread sync is deferred to a future OAuth-backed service, not to a secret in this workflow.

**No LLM.** If advisory summaries are ever added, they must stay advisory: they may never decide whether an item disappears, becomes "ready", or outranks a direct request, and they must be cached by content fingerprint.

**The health view is a trend, not a scoreboard.** It replaced a page of counts, distributions and 7/28/90-day tables that could not answer "is this good, and is it improving". What it publishes now is deliberately small: three indicators, each with a direction that is good, a change across the window, and a line chart; then how the work divided among the editors. Adding a number back means answering what decision it changes.

Two properties hold it up. The weekly backlog is *reconstructed* from the current sample rather than stored between builds — every open PR plus every PR closed inside `history_days` is present, so "how many were open at time t" is exact for any t in the twelve-week window; the window stops short of `history_days` for exactly that reason. And the newest weekly bucket never counts toward the first-response rate (`first_response_mature`), because a PR opened three days ago cannot yet have missed a seven-day target, and counting it would report the calendar as a failure.

**One colour per editor, assigned by identity and never cycled.** `web/style.css` defines five categorical slots on `:root` (one stepped set per colour scheme, shared by the columns, the attribution bars and the legend swatches); `web/app.js` `editorSeries` hands slot *n* to the *n*th editor in `metrics.editors.members`, which is alphabetical and stable, so a colour means the same person in every mark and the page needs only one legend. The five were found by search and checked with the dataviz skill's `validate_palette.js` on **every pair, not just neighbours**, in both colour schemes, against **all three kinds of colour blindness**: worst ΔE 8.5 protan/deutan and 12.3 tritan on white, 8.6 and 12.7 on the dark surface; worst normal-vision ΔE 15.7; every slot at or above 3:1 contrast on its own surface, and inside the lightness band for that surface. Lightness carries as much of the separation as hue does — that is what a tritan-safe set costs, and it is why the slots cannot be re-stepped for looks without re-running the check. A sixth editor folds into one "other editors" series rather than taking an unvalidated hue, and `SERIES_SLOTS` may only grow with a fresh run of the validator. Colour is still never the only channel: the legend, the fixed stack order and the two "Show the numbers" tables all have to stay. The chart SVGs set `forced-color-adjust: none`, because the hues are data and forced-colours mode would collapse them all to one system colour.

**A merge is credited to exactly one editor.** `metrics.py` `_first_editor_reviewer` picks the first editor other than the author to have reviewed before the merge, and `buckets[].merged_by_first_reviewer` counts those. Several editors reviewing one PR would otherwise stack to more than the number of merges. The credit is a display rule for the columns only — every review still counts in its own editor's `reviews_submitted`, and the group share asks the weaker question of whether *anybody* reviewed.

Count line charts use a focused y-range rather than a zero baseline: a backlog moving 257 → 283 is a flat line against zero, which is the reading the view exists to correct. The tick labels always state where the axis starts. The columns chart, where length encodes the value, keeps its zero baseline.

**Wording is a requirement, not style.** Impact metrics describe event order, never causation — "PRs merged after a review", never "PRs the review caused to merge" (`test_metrics.py` guards this). Merged PRs an editor authored are excluded from *their own* review share rather than counted as misses, since an author cannot review their own PR; the group share keeps them and asks whether any editor other than the author reviewed first. Likewise the task list is a *description checklist* and never "requirements complete": it says the author ticked boxes, nothing about test sufficiency, implementer interest, or merge readiness.

**All GitHub-derived text is untrusted.** PR titles, bodies, logins, and comments can contain deliberate HTML or script-like content; they reach the page only as text nodes.

**The workflow is read-only apart from the Pages deploy.** Rolling 90-day metrics are recomputed from the API each run rather than kept in a database. If daily aggregates are added later they belong on a separate data branch, which is the only thing that should ever need `contents: write`.

**Deferred by plan:** issues (PRs only for now), other WHATWG repositories (`dashboard.yml` is single-repo, though the layering is meant to allow more), cross-device state, OAuth/multi-user, and assessment of actual WPT coverage, implementer interest, web-compat risk, or consensus quality.

## Constraints enforced by tests

- `test_build.py` asserts the published payload contains no `body` or `comments` keys anywhere, and that every perspective's lane keys resolve against `items`. Raw PR descriptions and comment/review bodies must never reach `data.json` — only derived signals and short quoted checklist labels.
- `test_build.py` also asserts `web/app.js` contains no `innerHTML` and `web/index.html` retains its `Content-Security-Policy`. Build DOM via the `element()` helper and text nodes.
- Four of the six test modules load the real [dashboard.yml](dashboard.yml) and [fixtures/sample_api_data.json](fixtures/sample_api_data.json) at `NOW = 2026-08-03T12:00:00Z` (the same `--now` as the demo build) and assert on named PR numbers and exact lane orderings. So a threshold change in `dashboard.yml`, or a new fixture PR, is expected to break tests in modules that look unrelated to the change; re-derive the expectations rather than loosening the assertions.
- `web/` is vanilla ES modules, no build step, no third-party scripts, fonts, or network calls other than same-origin `data.json`. Files are copied verbatim into `site/`.

## Configuration notes

`dashboard.yml` is the only knob surface. The `editors` list is applied retroactively to the whole 90-day sample, so editing it changes historical response-time and waiting-on-editor metrics, and it is also the row list of the editor-impact section, the option list of both queue selects, and the set of logins that get fingerprints. `suggested_next.cycle` may only contain the five lanes in `_ALLOWED_SUGGESTED_LANES`, with no duplicates; `active`, `direct`, `reply_window` and `all` are excluded because each already has a fixed position in the queue. `suggested_next.first_response_lead` is how many `reply_window` PRs lead the queue: non-negative, capped at 10, and 0 disables the lead while leaving the lane browsable.

## CI

[.github/workflows/dashboard.yml](.github/workflows/dashboard.yml) runs on push to `main`, once every 24 hours, or manual dispatch. It runs the test suite before building, uses only the job `GITHUB_TOKEN` (`contents: read` for build; `pages: write` + `id-token: write` isolated to the deploy job), and pins actions to full commit SHAs. Never add `pull_request_target` or anything that would execute code from a `whatwg/html` PR branch.

# WHATWG HTML editor dashboard

A static, deterministic dashboard for prioritizing reviews in `whatwg/html` and tracking public project-health and editor-impact indicators.

See [`PRIVACY.md`](PRIVACY.md) for the public/private boundary and threat model.

The dashboard is rebuilt by GitHub Actions once every 24 hours and deployed to GitHub Pages. It uses public GitHub data only. Personal workflow state—seen signals, addressed items, pins, snoozes, and opened timestamps—stays in the browser's `localStorage`.

## MVP scope

### Review queues

The review dashboard contains these explainable lanes:

1. **Active now** — PRs changed within the activity window that the selected editor is already involved in, through a direct request, an owed re-review, or a previously submitted review. Newest activity first.
2. **Direct requests** — review requests and assignments the selected editor currently holds, plus sampled public mentions of them inside the activity window.
3. **Stale mentions** — mentions older than the activity window, kept findable without leading the queue.
4. **Re-review owed** — a head commit, PR description, or sampled author activity changed after the selected editor's latest sampled review.
5. **Reply window open** — contributor PRs that have never received a sampled editor response and are still inside the seven-day target, closest to the deadline first. The only PRs where a first reply can still meet the target.
6. **First reply overdue** — the same population past the target, oldest first. A PR that misses the target moves here rather than leaving the pair.
7. **Longest waits** — time since the latest sampled non-editor human activity that was not followed by editor activity.
8. **Ready** — a quick-win heuristic based on mergeability, CI, labels, review state, review threads, diff size, and (for normative PRs) a complete description checklist.
9. **Ready to land** — the selected editor approved the current head and nothing in the template checklist is left unticked.
10. **Long wait on author** — the author has had the next move for more than `waiting_on.stale_author_days`; candidates to ping or close.

Open issues are queued too, with the same lanes where the meaning carries over: mentions and assignments, first response (a comment, a label or a close by an editor, within the 7-day target), and longest waits. There's also **Needs triage**, for issues without a `topic:` label. Only issues opened, or last answered by a contributor, inside `issues.window_days` are queued, because most open issues are dormant. An issue an open PR says it fixes is shown on that PR's card ("Fixes #N") instead of on its own. The **Show** control filters PRs and issues.

Unanswered contributor PRs and issues nobody was asked about directly are **routed** to one person, shown on the card with the evidence ("Declared in dashboard.yml: topic: img, the images page"). Routing weighs the topics each person declares in `dashboard.yml` (`knows:`, `topic:` labels and spec page names) against recent load. An item's topics are its labels, its linked issues' labels, and the spec pages its diff touches, found by mapping the diff onto the multipage spec. `python dashboard.py expertise --state .state --spec-repo .spec-repo` prints who authored, reviewed and merged what according to history, compared with the declarations, as an aid to editing them. Each person's routed items are in their **Routed to you** lane and lead their Suggested next. `dashboard.yml` gives each editor a `share` of routed work, marks retired editors (never routed, direct requests only), and lists non-editor helpers who take routed PR reviews. Cards can also suggest a `topic:` label from the spec pages, and a GitHub team to ask, with a copy button. Nothing is ever applied or mentioned automatically.

The health view's **Who did what** table has one row per active editor and helper, plus anyone else who reviewed at least ten other people's PRs in twelve weeks. Each row shows reviews, first replies, issues triaged, merges, own normative PRs merged, issues closed, a twelve-week activity line, how the items routed to them did against the response target, and what is waiting on them now.

Every card leads with whose move it is: the editors (named when a review is requested or an editor is already in the conversation), the author, implementers, tests, compat analysis, the WHATNOT meeting, a dependency, or "approved, ready to land". Labels decide first, then an unaddressed change request, a current approval, and finally who acted last. A merge conflict or failing checks never hands a PR back to its author. Re-review owed, Stale mentions, Longest waits and Active now only hold a PR while the move is the editors'. Cards can also suggest a label, with a button that copies the `gh` command to apply it.

The first four lanes are computed for every editor in `dashboard.yml`, and the **Editor** control in the queue controls picks whose queue is on screen. It defaults to **All editors** — the union, so a PR is in a lane if it is in that lane for anybody — and the other options are the individual editors. The remaining lanes are properties of the pull request rather than of any editor, so they read the same from every perspective. The choice is stored per browser, and somebody else’s login is repeated next to the “Suggested next” heading, because the controls panel starts collapsed.

There is no configured viewer. A separate **You are** control names which editor is using this browser, which is what decides whose signals count as seen and whose own footprint is left out of an addressed fingerprint. It starts at **Just browsing**, since the deployed site is public and most readers are not editors; pins and snoozes work either way, and seen and addressed appear once you say who you are. The two controls are independent, so reading a colleague’s queue never touches your own state.

“Suggested next” shows up to three **Reply window open** PRs, then the whole **Active now** lane, then interleaves re-review, overdue-first-reply, oldest-wait, ready/bounded, and stale-direct candidates. Overdue PRs are not in the lead because a missed target cannot be un-missed: replying still matters to the contributor, but it can no longer change the first-response rate. Both the lead size and the cycle are configurable in `dashboard.yml`.

The queue leads with recent activity rather than with the oldest claim on the editor's attention. On a backlog where most PRs have not moved in years, age is a poor proxy for actionability: sorting direct requests oldest-first put a mention from 2016 at the top of the list and pushed the week's live reviews to the bottom. A review request or assignment keeps claiming attention until GitHub clears it on review, but a mention expires out of the direct lane once it leaves the activity window.

The reply-window lead is the deliberate exception. It is not an age ordering but a deadline ordering, and it is bounded — three by default, zero to disable — precisely so it cannot recreate the problem that putting recent activity first solved.

Every card exposes the evidence and detected limitations behind its classification. No LLM is used.

### Browser-local workflow state

- Opening a GitHub link marks the current public attention signal as seen in that browser.
- Seen and addressed state is anchored to the **You are** identity, not to the **Editor** perspective. Switching the queue to another editor changes which lanes and evidence chips are shown, never which items this browser has already dealt with. Changing the identity does resurface addressed items, because they are measured against a fingerprint that leaves that editor's own footprint out; switching back restores them, since nothing is overwritten until you act.
- **Address until changed** records the current public content fingerprint. The PR automatically returns when that fingerprint changes. The fingerprint deliberately excludes the editor's own footprint — their comments and reviews, the review threads they started, their cleared review request, the review decision, and the PR's `updatedAt` — so replying to a PR and then addressing it does not bring it straight back. It also excludes mergeability, which GitHub computes lazily and reports as `UNKNOWN` to a cold query, so it moves between builds without the PR changing.
- Pin, snooze, lane-sort, and queue-controls preferences are local; the controls panel starts collapsed and stays as this browser left it.
- State can be exported and imported manually as JSON.
- Clearing site data clears the local state.
- A tab left open reloads `data.json` in place once a newer build is deployed, so it does not keep showing yesterday's queue. It only looks once the displayed data is at least 24 hours old — the scheduled build interval — and only while the tab is visible; local state, the selected lane, the search box, and the scroll position survive the swap.

`localStorage` is origin-scoped rather than path-scoped. The key is namespaced by the dashboard path to prevent accidental collisions, but scripts on other pages under the same origin could technically read it. Use a dedicated Pages origin or custom subdomain when other projects on that origin are not equally trusted. The stored values are workflow metadata, not credentials.

The generated site never fetches GitHub notification inbox data. “Unseen” means a public attention signal has not been opened in this browser; it is not GitHub's private unread state.

### Project-health metrics

The health view answers two questions and stops: is the review load getting better or worse, and how is the editing work distributed. Every figure on it comes from the same twelve-week window.

- Three trend cards — open pull requests, contributors waiting more than `initial_editor_response_days` for a first reply, and the share of contributor PRs answered inside that target. Each shows the current value, the change across the window, a line chart, and which direction is the good one.
- A verdict line naming whichever indicators are off target or moving the wrong way. “Off target” for the response rate means below `response_targets.first_response_within_target_percent`.
- Editor impact over the same window: the share of merged pull requests that had a review from an editor other than the author before they merged, first replies, reviews submitted, distinct contributors replied to, and PRs the editors authored. Each editor in `editors` gets one colour, fixed by alphabetical position and used everywhere: the weekly merge columns are split by editor, and each of the four figures carries a bar showing how it divides among them. One legend, above, covers both. The five hues are validated on every pair, in both light and dark, against protanopia, deuteranopia and tritanopia, and every chart also has a text alternative and a table of the same numbers, so nothing is carried by colour alone. A merge several editors reviewed is credited to whoever reviewed it first, so the columns still add up to the merges. The per-editor numbers are in tables behind the disclosures.
- The weekly numbers behind each chart, in a table behind a disclosure, and one line of sampling coverage.

Nothing is stored between builds. The weekly backlog is *reconstructed*: every open PR and every PR closed inside the history window is in the sample, so the number open at any past week in the window can be counted exactly.

The newest week never counts toward the first-response rate. A PR opened three days ago has not missed a seven-day target yet, so counting it would report the calendar as a failure.

An editor's own pull requests are excluded from their own review share, because an author cannot review their own; the group figure keeps them, counting a merge as reviewed once any editor other than the author reviewed it. The wording intentionally describes event order, not causation: “merged after a review” does not mean the review caused the merge.

## Deploying

1. Create a GitHub repository and copy this project into it. A private repository is a good default when your plan supports private-repository Pages: the generated Pages site can still be public, and public repositories can have scheduled workflows disabled after 60 days without repository activity.
2. Keep `dashboard.yml` as-is for `whatwg/html`, or edit the repository and editor list before the first run.
3. In the repository's **Settings → Pages**, select **GitHub Actions** as the build and deployment source.
4. Push to `main`, or run the workflow manually.

No PAT or repository secret is required. The workflow uses its short-lived `GITHUB_TOKEN` with `contents: read` for the build job. The deployment job alone receives `pages: write` and `id-token: write`.

The workflow:

- runs only on `main`, a daily schedule, or manual dispatch;
- never runs code from `whatwg/html` PR branches;
- does not use `pull_request_target`;
- pins every action to a full commit SHA;
- runs the test suite before generating or deploying the site.

For a public dashboard-source repository, monitor the workflow's enabled state or ensure the repository has occasional real activity; GitHub can automatically disable inactive public-repository schedules. This does not apply to the static site itself, only its refresh job.

## Running locally

Python 3.12 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --requirement requirements.txt
```

Build the included deterministic demo:

```bash
python dashboard.py build \
  --fixture fixtures/sample_api_data.json \
  --now 2026-08-03T12:00:00Z \
  --output site
python -m http.server --directory site 8000
```

Build from live public GitHub data using an existing local token:

```bash
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site
```

With `--state DIR`, the build stores the raw GitHub data it fetched in `DIR/snapshot.json.gz`, and the next build with the same directory only fetches what changed since (pass `--full` to fetch everything anyway):

```bash
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site --state .state
```

Routing by spec page needs a bare clone of whatwg/html: `--spec-repo DIR` clones one there on first use (about 850 MB) and only fetches into it afterwards. Without it, topics come from labels alone. `--evaluate` also prints how the rules did on the data: whose move, routing spread, and the measured precision of the topic-label suggestion.

```bash
GITHUB_TOKEN="$(gh auth token)" python dashboard.py build --output site --state .state --spec-repo .spec-repo --evaluate
```

Validate configuration and run tests:

```bash
python dashboard.py validate
python -m unittest discover -s tests -v
```

### GitHub 502 or 504 during a build

For GraphQL, GitHub uses HTTP 502 and 504 when a request exceeds its processing-time limit. The client automatically retries and reduces `sampling.graphql_page_size`; the checked-in default is already a conservative 10 rather than 50. Retry messages show the attempt number, request ID when GitHub supplies one, delay, any page-size reduction, and how much of the build's retry budget is left.

The delay starts at 37.5 seconds and doubles up to 5 minutes, and the build keeps retrying until it has waited an hour in total across all of its requests, so a GitHub incident of ordinary length is waited out rather than failing the day's build. The workflow's build job is allowed 120 minutes for that reason: the retry budget plus a weekly full build. A build that still fails after spending the budget, or a persistent failure at page size 1, is more likely to be a longer GitHub API incident or a pathological single item. Check GitHub Status and rerun the workflow. Do not add a PAT merely to address a 502/504; these status codes do not indicate missing permissions.

## Configuration

`dashboard.yml` controls:

- repository owner and name;
- the public editor logins (each gets its own queue perspective, fingerprints and impact row);
- the seven-day first-response target and 48-hour new-PR highlight;
- the activity window that defines “active now” and how long a mention keeps claiming attention;
- ready/bounded diff and checklist thresholds (the HTML MVP defaults to all boxes checked);
- labels treated as blockers;
- the outer GraphQL page size, nested sampling sizes, and historical window;
- suggested-next lead size and interleaving order.

The current editor list is deliberately explicit. Update it when the HTML editor group changes, because it affects response-time and waiting-on-editor metrics, and it is also the option list of the queue's **Editor** control.

## Data collection and API economy

The Python builder uses GitHub's GraphQL API and paginates open PRs and recently closed PRs. For each PR it samples:

- the first and last configured number of issue comments and submitted reviews;
- the latest configured number of submitted reviews, whoever wrote them, which is what the per-editor re-review lanes and impact figures are counted from;
- current review requests and assignees;
- current labels, mergeability, review decision, check-rollup state, and head commit;
- a bounded review-thread sample.

Only derived public data is written to `site/data.json`; raw PR bodies and comment/review bodies are not published by the dashboard build.

The scheduled build is incremental. It restores the previous run's snapshot from the Actions cache and searches for PRs updated since that run started, less `incremental.overlap_hours`. Then it refetches those PRs by node id with exactly the fields a full fetch asks for. Some fields change without moving a PR's `updatedAt`: mergeability, merge state, review decision and check rollup move when the base branch does. So every open PR is also re-asked for those, plus its `updatedAt` and state, in batches of 100, and any PR whose `updatedAt` or state moved is refetched too. Closed PRs age out of the snapshot on the same date boundary the full search uses. A full fetch runs instead when there is no snapshot, when the queries or sampling sizes changed (a fingerprint of them is stored), when more than 1,000 PRs changed, or every `incremental.full_every_days`. The last case catches changes no `updatedAt` reflects, such as an edited comment. A manual workflow run can also force one. Analysis always reruns over the whole snapshot, so ages and lanes are current whichever way the data was fetched. `data.json`'s `build` block says which it was.

GitHub computes mergeability lazily, answering `UNKNOWN` to a cold query and only scheduling the real value, so a build that trusts the first answer reports almost nothing as mergeable. After the closed-PR search — which gives GitHub several seconds of computing time — the builder re-asks for the PRs that answered `UNKNOWN`, by node id, in rounds that back off up to just under a minute in total. Anything still unknown keeps its “Mergeability unknown” marker and is counted in the build warnings rather than guessed at.

The outer GraphQL page defaults to 10 PRs because every PR includes several nested connections. GitHub documents HTTP 502 and 504 responses from the GraphQL endpoint as request timeouts. When either occurs, or when GitHub reports the same timeout inside an HTTP 200 `errors` entry, the client retries with exponential backoff, halves the outer page size, and retains that smaller size for the rest of the build. Other transient 5xx responses, and connection failures including a response cut off mid-body, are retried without changing the page size. Retries are bounded by a waiting budget shared across the build rather than by a fixed count per request.

The build logs successful GraphQL query count, total request attempts, retries, query cost, remaining quota, and the effective outer page size. Sampling limits are configurable, but increasing the outer page or nested connection sizes increases server work and makes timeouts more likely.

## Deterministic rules and limitations

This MVP intentionally does **not**:

- use an LLM;
- fetch private GitHub notification state;
- assess actual WPT or test coverage;
- verify implementer-interest claims;
- infer web-compatibility risk or consensus quality;
- inspect inline review-thread replies for mentions or response metrics;
- claim that a ready/bounded PR should be merged.

Description task lists are parsed while fenced code blocks are ignored. Their completion is shown as a descriptive signal only.

A first-time contributor is detected from an author with no GitHub association to the repository, which also covers an author whose only earlier pull requests were closed unmerged.

For long discussions, only the beginning and end of the comment/review timeline are sampled. First-response metrics are reported only when the beginning sample establishes the response or the sampled timeline is complete. Later response-interval metrics require a complete sampled timeline.
The configured current editor list is applied retroactively to the 90-day sample; historical editor-membership changes are not reconstructed. Description edits are attributed to the PR author because the sampled API fields do not identify the editor of the description.
Draft PRs are left out of the dashboard entirely, queue and health view alike. A PR that used to be a draft is dated from the latest time it was marked ready for review: that is where its age, its first-response clock and its place in the weekly backlog start, and marking it ready counts as contributor activity for the editor wait.

## Repository layout

```text
.
├── dashboard.py                     # CLI
├── dashboard.yml                    # product and threshold configuration
├── editor_dashboard/
│   ├── analysis.py                  # deterministic queue rules and fingerprints
│   ├── build.py                     # public data payload and static-site build
│   ├── checklist.py                 # Markdown task-list parser
│   ├── config.py                    # validated configuration
│   ├── github.py                    # GraphQL client, pagination, fixture loading
│   ├── metrics.py                   # project-health and public impact metrics
│   └── models.py                    # typed snapshots and activities
├── graphql/                         # GitHub GraphQL queries
├── web/                             # static HTML, CSS, and browser-local state UI
├── fixtures/                        # offline demo data
├── tests/                           # unit and build tests
└── .github/workflows/dashboard.yml  # daily Pages deployment
```

## Extending beyond the MVP

The internal model separates collection, analysis, metrics, and rendering so later versions can add issues, additional WHATWG repositories, cross-device state, OAuth, or optional cached summaries without rewriting the core prioritization rules.

## License

MIT.

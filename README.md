# WHATWG HTML editor dashboard

A static, deterministic dashboard for prioritizing reviews in `whatwg/html` and tracking public project-health and editor-impact indicators.

See [`PRIVACY.md`](PRIVACY.md) for the public/private boundary and threat model.

The dashboard is rebuilt by GitHub Actions every six hours and deployed to GitHub Pages. It uses public GitHub data only. Personal workflow state—seen signals, addressed items, pins, snoozes, and opened timestamps—stays in the browser's `localStorage`.

## MVP scope

### Review queues

The review dashboard contains these explainable lanes:

1. **Direct requests** — current review requests, assignments, and sampled public `@zcorpan` mentions.
2. **Re-review owed** — a head commit, PR description, or sampled author activity changed after the latest sampled `@zcorpan` review.
3. **New PR turnaround** — non-draft contributor PRs without a sampled editor response, targeting a first response within seven days.
4. **Oldest contributor waits** — time since the latest sampled non-editor human activity that was not followed by editor activity.
5. **Ready and bounded** — a quick-win heuristic based on mergeability, CI, labels, review state, review threads, diff size, and a complete description checklist.

“Suggested next” shows every active direct request first, then interleaves re-review, new, oldest-wait, and ready/bounded candidates. The cycle is configurable in `dashboard.yml`. The current lane can also be sorted by checklist completion, unchecked-box count, contributor wait, update time, or age.

Every card exposes the evidence and detected limitations behind its classification. No LLM is used.

### Browser-local workflow state

- Opening a GitHub link marks the current public attention signal as seen in that browser.
- **Address until changed** records the current public content fingerprint. The PR automatically returns when that fingerprint changes.
- Pin, snooze, and lane-sort preferences are local.
- State can be exported and imported manually as JSON.
- Clearing site data clears the local state.

`localStorage` is origin-scoped rather than path-scoped. The key is namespaced by the dashboard path to prevent accidental collisions, but scripts on other pages under the same origin could technically read it. Use a dedicated Pages origin or custom subdomain when other projects on that origin are not equally trusted. The stored values are workflow metadata, not credentials.

The generated site never fetches GitHub notification inbox data. “Unseen” means a public attention signal has not been opened in this browser; it is not GitHub's private unread state.

### Project-health metrics

The public health view includes:

- Current open, draft, waiting-on-editor, over-target, direct-attention, re-review, and ready/bounded counts.
- Contributor-wait distribution and deterministic waiting-reason breakdown.
- PRs opened, closed, merged, and net backlog change over 7, 28, and 90 days.
- Median and p90 sampled first-editor-response time.
- Description-checklist distribution.
- Public `@zcorpan` indicators such as reviews submitted, first editor responses, unique PR authors engaged with, PRs merged after a review, and sampled contributor-wait days ended.
- Sampling coverage, so incomplete history is visible rather than silently treated as complete.

The wording intentionally describes event order, not causation. For example, “merged after review” does not mean the review caused the merge.

## Deploying

1. Create a GitHub repository and copy this project into it. A private repository is a good default when your plan supports private-repository Pages: the generated Pages site can still be public, and public repositories can have scheduled workflows disabled after 60 days without repository activity.
2. Keep `dashboard.yml` as-is for `whatwg/html` and `@zcorpan`, or edit it before the first run.
3. In the repository's **Settings → Pages**, select **GitHub Actions** as the build and deployment source.
4. Push to `main`, or run the workflow manually.

No PAT or repository secret is required. The workflow uses its short-lived `GITHUB_TOKEN` with `contents: read` for the build job. The deployment job alone receives `pages: write` and `id-token: write`.

The workflow:

- runs only on `main`, a six-hour schedule, or manual dispatch;
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

Validate configuration and run tests:

```bash
python dashboard.py validate
python -m unittest discover -s tests -v
```

### GitHub 502 or 504 during a build

For GraphQL, GitHub uses HTTP 502 and 504 when a request exceeds its processing-time limit. The client automatically retries and reduces `sampling.graphql_page_size`; the checked-in default is already a conservative 10 rather than 50. Retry messages show the attempt number, request ID when GitHub supplies one, delay, and any page-size reduction.

A persistent failure at page size 1 is more likely to be a wider GitHub API incident or a pathological single item. Check GitHub Status and rerun the workflow. Do not add a PAT merely to address a 502/504; these status codes do not indicate missing permissions.

## Configuration

`dashboard.yml` controls:

- repository owner and name;
- viewer login and public editor logins;
- the seven-day first-response target and 48-hour new-PR highlight;
- ready/bounded diff and checklist thresholds (the HTML MVP defaults to all boxes checked);
- labels treated as blockers;
- the outer GraphQL page size, nested sampling sizes, and historical window;
- suggested-next interleaving order.

The current editor list is deliberately explicit. Update it when the HTML editor group changes, because it affects response-time and waiting-on-editor metrics.

## Data collection and API economy

The Python builder uses GitHub's GraphQL API and paginates open PRs and recently closed PRs. For each PR it samples:

- the first and last configured number of issue comments and submitted reviews;
- recent reviews authored by the configured viewer;
- current review requests and assignees;
- current labels, mergeability, review decision, check-rollup state, and head commit;
- a bounded review-thread sample.

Only derived public data is written to `site/data.json`; raw PR bodies and comment/review bodies are not published by the dashboard build.

The outer GraphQL page defaults to 10 PRs because every PR includes several nested connections. GitHub documents HTTP 502 and 504 responses from the GraphQL endpoint as request timeouts. When either occurs, the client retries with exponential backoff, halves the outer page size, and retains that smaller size for the rest of the build. Other transient 5xx and network failures are retried without changing the page size.

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

For long discussions, only the beginning and end of the comment/review timeline are sampled. First-response metrics are reported only when the beginning sample establishes the response or the sampled timeline is complete. Later response-interval metrics require a complete sampled timeline.
The configured current editor list is applied retroactively to the 90-day sample; historical editor-membership changes are not reconstructed. Description edits are attributed to the PR author because the sampled API fields do not identify the editor of the description.
Current drafts are excluded from response-target and contributor-wait counts. For a non-draft PR that used to be a draft, the MVP still uses PR creation time because it does not fetch the ready-for-review transition history.

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
└── .github/workflows/dashboard.yml  # six-hour Pages deployment
```

## Extending beyond the MVP

The internal model separates collection, analysis, metrics, and rendering so later versions can add issues, additional WHATWG repositories, cross-device state, OAuth, or optional cached summaries without rewriting the core prioritization rules.

## License

MIT.

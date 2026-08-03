# Privacy and security model

## Published data

The scheduled build requests public data from `whatwg/html` and publishes a derived static `data.json`. The public payload includes PR metadata, deterministic classifications, description task-list labels and completion, public activity timestamps, and aggregate health/impact metrics.

It does not publish complete PR descriptions, complete comment or review bodies, a GitHub notification inbox, authentication tokens, or browser-local workflow state. Task-list labels and classification labels can quote small pieces of public PR descriptions because they are part of the displayed public evidence.

## Browser-local state

The static page stores these values in `localStorage`:

- the public attention fingerprint last seen for an item;
- the public content fingerprint marked addressed;
- pin and snooze state;
- the last-opened timestamp;
- queue display preferences.

No client-side code sends this state to GitHub or another server. Export and import are explicit local file operations.

`localStorage` is scoped to an origin, not a URL path. The key is namespaced by the dashboard path to avoid accidental collisions, but another script running under the same origin could technically read it. Use a dedicated Pages origin or custom subdomain when other projects on the origin are not equally trusted. The state is workflow metadata and should not contain credentials or secrets.

## Credentials and automation

The MVP does not use a personal access token. The GitHub Actions build receives only the job-scoped `GITHUB_TOKEN` with `contents: read`; the separate deployment job receives `pages: write` and `id-token: write`.

For local live builds, the token is accepted only through the `GITHUB_TOKEN` environment variable, rather than a command-line option that could be exposed in shell history or process listings.

The workflow is triggered only by pushes to `main`, its schedule, and manual dispatch. It does not use `pull_request_target` and does not execute code from `whatwg/html` pull-request branches. GitHub-authored actions are pinned to full commit SHAs.

## Static-site hardening

The page has no third-party scripts, analytics, fonts, or runtime API calls. It fetches only its same-origin `data.json`. Dynamic GitHub-derived strings are inserted through DOM text nodes rather than interpreted as HTML, and the page includes a restrictive Content Security Policy.

## Deliberately excluded from the MVP

- GitHub's private notification/unread state
- PATs, OAuth, and cross-device persistence
- LLM calls or model-derived assessments
- automatic writes to `whatwg/html`

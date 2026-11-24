# Privacy and security model

## Published data

The build reads public `whatwg/html` data and publishes a derived `data.json`: PR and issue metadata, classifications, checklist labels and completion, public activity times, and health and impact figures.

It never publishes full descriptions, comment or review bodies, notification state, tokens, or browser state. Checklist and reason labels may quote short pieces of public descriptions as evidence.

## Build state between runs

To fetch only what changed, each build keeps the raw GraphQL responses in `.state/snapshot.json.gz`, stored in the repository's Actions cache. Unlike `data.json` it holds full public descriptions and bodies as GitHub returned them, but nothing non-public and no credentials, and it is never published. Only this repository's workflows can read the cache; entries expire after seven days unused, and the next build then fetches everything.

## Browser-local state

`localStorage` holds, per item, the attention fingerprint last seen, the content fingerprint marked addressed, pin and snooze state, and when it was last opened; plus display settings, the perspective shown, and who is using this browser. That identity exists only here and decides whose footprint fingerprints leave out; until it's set, nothing is marked seen or addressed.

No code sends this state anywhere; export and import are local files. `localStorage` is per origin, not per path: the key is namespaced by path, but other scripts on the origin could read it, so use a dedicated origin if they aren't trusted. It holds workflow metadata, never credentials.

## Credentials and automation

No personal access token. The build job gets only `GITHUB_TOKEN` with `contents: read`; the deploy job gets `pages: write` and `id-token: write`. Local live builds take the token only from the `GITHUB_TOKEN` environment variable, not an argument that could leak into shell history or process lists.

The workflow runs on push to `main`, its schedule, and manual dispatch. It never uses `pull_request_target` or runs code from `whatwg/html` branches, and actions are pinned to commit SHAs.

## Static-site hardening

No third-party scripts, analytics or fonts, and no network calls but same-origin `data.json`. GitHub-derived text is inserted as text nodes, never HTML, under a restrictive Content Security Policy.

## Deliberately excluded

- GitHub's private notification and unread state
- PATs, OAuth, and cross-device state
- LLM calls or model-derived assessments
- writes to `whatwg/html`

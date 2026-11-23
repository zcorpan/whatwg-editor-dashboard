"""What a held pull request is waiting for, as far as its description says.

A `do not merge yet` PR usually names what it waits on: the web-platform-tests PR the
maintainer guidelines say to land together, a PR in another standard, or another PR in
this repository ("depends on #123"). Those references are resolved to their current
state by the fetch layer, and analysis turns an open one into a `dependency` wait.

Only two forms count, because a description links to plenty that is not a
dependency (the issue it fixes, discussions, specifications):

- a full GitHub pull-request URL, in any repository;
- a same-repository `#N` or `owner/repo#N` right after a dependency phrase.
"""

from __future__ import annotations

import re


_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.IGNORECASE)
_PHRASE_REF_RE = re.compile(
    r"\b(?:depends\s+on|blocked\s+(?:on|by)|requires|on\s+top\s+of|waiting\s+(?:on|for)|after)"
    r"\s+(?:([\w.-]+)/([\w.-]+))?#(\d+)",
    re.IGNORECASE,
)
# A task-list line's HTML comments are template instructions, not references.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def dependency_references(body: str | None, repository_slug: str, own_number: int) -> tuple[str, ...]:
    """Canonical URLs of what ``body`` says the PR depends on, sorted, without itself.

    Same-repository references use the `/issues/N` form, which GitHub resolves for
    issues and pull requests alike; other repositories' PRs keep their `/pull/N` URL
    so that a merge is distinguishable from a close.
    """
    if not body:
        return ()
    text = _COMMENT_RE.sub("", body)
    own_owner, own_name = repository_slug.lower().split("/", 1)
    found: set[str] = set()

    def add(owner: str, name: str, number: str, kind: str) -> None:
        owner, name = owner.lower(), name.lower()
        if (owner, name) == (own_owner, own_name):
            if int(number) == own_number:
                return
            found.add(f"https://github.com/{owner}/{name}/issues/{int(number)}")
        else:
            found.add(f"https://github.com/{owner}/{name}/{kind}/{int(number)}")

    for owner, name, number in _PR_URL_RE.findall(text):
        add(owner, name, number, "pull")
    for owner, name, number in _PHRASE_REF_RE.findall(text):
        add(owner or own_owner, name or own_name, number, "issues")
    return tuple(sorted(found))

"""Which pages of the multipage HTML Standard a change touches, or an issue links to.

Topic labels are too sparse to route by on their own: about a fifth of open
whatwg/html PRs carry one. Every PR does touch `source`, though, and `source` is split
into the multipage pages by `split-filename` on its `<h2>`/`<h3>` headings. So a diff
hunk is attributed to the page whose heading precedes it:

- git finds each hunk's preceding heading itself, through a diff driver whose
  `xfuncname` matches heading lines, so the heading lands in the hunk header;
- the heading line is looked up in an index built from `source` at the main branch,
  which says which page it is on.

A heading renamed since the change was made is not in the index, so its hunks are
not attributed rather than guessed at (about a quarter of hunks, measured on three
years of main). Issues have no diff, so they are attributed through the spec links in
their title and description: `multipage/<page>.html` directly, and a single-page
`#fragment` through the page that defines that id.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


SPEC_FILE = "source"
_HEADING_RE = re.compile(r"\s*<h([1-6])([^>]*)>")
_SPLIT_RE = re.compile(r'split-filename="([^"]+)"')
_ID_RE = re.compile(r'\bid="([^"]+)"')
_HUNK_RE = re.compile(r"^@@ [^@]* @@(.*)$")
_SPEC_LINK_RE = re.compile(
    r"html\.spec\.whatwg\.org/(?:(?:multipage|dev)/([a-z0-9-]+)\.html)?(?:#([A-Za-z0-9_.:%-]+))?"
)
# The diff driver: every heading line is a "function" header, so git names the
# nearest preceding heading in each hunk header. Without the group git would show
# only the matched part, "<h4", rather than the heading.
_XFUNCNAME = r"^([ \t]*<h[1-6].*)$"
# git truncates the text in a hunk header to this many bytes.
_HUNK_HEADER_BYTES = 80


def _hunk_key(line: str) -> str:
    """A heading line as it appears in a hunk header: its first 80 bytes, trimmed."""
    return line.encode("utf-8")[:_HUNK_HEADER_BYTES].decode("utf-8", errors="ignore").strip()


_TAG_RE = re.compile(r"<[^>]+>")


def _section_slug(attributes: str, rest: str) -> str:
    """A heading's anchor: its explicit `id`, else a slug of its text, as the spec's
    generated ids mostly are ("The link element" -> "the-link-element")."""
    explicit = _ID_RE.search(attributes)
    if explicit:
        return explicit.group(1)
    text = _TAG_RE.sub("", rest.split("</h", 1)[0])
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def split_location(location: str) -> tuple[str, tuple[str, ...]]:
    """`page#a/b` -> ("page", ("a", "b")): the page and its enclosing sections, outermost first."""
    page, _, sections = location.partition("#")
    return page, tuple(sections.split("/")) if sections else ()


@dataclass(frozen=True)
class PageIndex:
    """Where each heading is: `page`, or `page#section/subsection` below the page's own heading.

    Changes and links are attributed to these locations, so the `topics:` map can name
    a section (`semantics#the-link-element`) as well as a whole page.
    """

    by_heading: dict[str, str]
    by_fragment: dict[str, str]

    @classmethod
    def from_source(cls, text: str) -> "PageIndex":
        by_heading: dict[str, str] = {}
        by_fragment: dict[str, str] = {}
        page: str | None = None
        # Enclosing headings below the page's own, as (level, slug).
        chain: list[tuple[int, str]] = []
        location: str | None = None
        for line in text.split("\n"):
            match = _HEADING_RE.match(line)
            if match:
                level = int(match.group(1))
                split = _SPLIT_RE.search(match.group(2))
                if split:
                    page = split.group(1)
                    chain = []
                elif page:
                    chain = [entry for entry in chain if entry[0] < level]
                    chain.append((level, _section_slug(match.group(2), line[match.end() :])))
                if page:
                    location = page + ("#" + "/".join(slug for _level, slug in chain) if chain else "")
                    by_heading.setdefault(_hunk_key(line), location)
            if location:
                for fragment in _ID_RE.findall(line):
                    by_fragment.setdefault(fragment, location)
        return cls(by_heading, by_fragment)

    def link_pages(self, text: str | None) -> frozenset[str]:
        """Locations a piece of Markdown links to on html.spec.whatwg.org."""
        locations: set[str] = set()
        for page, fragment in _SPEC_LINK_RE.findall(text or ""):
            resolved = self.by_fragment.get(fragment) if fragment else None
            if resolved is None or (page and split_location(resolved)[0] != page):
                resolved = page or None
            if resolved:
                locations.add(resolved)
        return frozenset(locations)

    def hunk_pages(self, diff: str) -> Counter[str]:
        """Count the hunks of a `git diff -U0` output by the location of their heading."""
        counts: Counter[str] = Counter()
        for line in diff.split("\n"):
            match = _HUNK_RE.match(line)
            if match:
                location = self.by_heading.get(_hunk_key(match.group(1)))
                if location:
                    counts[location] += 1
        return counts


class SpecRepository:
    """Read-only git queries against a whatwg/html clone."""

    def __init__(self, git_dir: str | Path) -> None:
        self.git_dir = Path(git_dir)
        self._attributes = tempfile.NamedTemporaryFile("w", suffix=".gitattributes", delete=False)
        self._attributes.write(f"{SPEC_FILE} diff=spec\n")
        self._attributes.close()

    def _git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            [
                "git",
                f"--git-dir={self.git_dir}",
                "-c",
                f"core.attributesFile={self._attributes.name}",
                "-c",
                f"diff.spec.xfuncname={_XFUNCNAME}",
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
        return result.stdout

    def has_commit(self, oid: str) -> bool:
        return bool(self._git("cat-file", "-t", oid, check=False).strip() == "commit")

    def source_at(self, ref: str) -> str:
        return self._git("show", f"{ref}:{SPEC_FILE}")

    def merge_base(self, base: str, head: str) -> str | None:
        value = self._git("merge-base", base, head, check=False).strip()
        return value or None

    def diff(self, base: str, head: str) -> str:
        return self._git("diff", "--no-color", "--no-ext-diff", "-U0", base, head, "--", SPEC_FILE)

    def commit_diff(self, oid: str) -> str:
        """A commit against its first parent; a root commit has no hunks worth counting."""
        return self._git("show", "--no-color", "--no-ext-diff", "-U0", "--format=", "--first-parent", oid, "--", SPEC_FILE)

    def close(self) -> None:
        Path(self._attributes.name).unlink(missing_ok=True)

    def fetch(self, refspecs: list[str]) -> None:
        """Fetch from `origin`, in batches so the command line stays short."""
        for start in range(0, len(refspecs), 100):
            self._git("fetch", "--quiet", "--no-tags", "origin", *refspecs[start : start + 100])

    def first_parent_commits(self, ref: str, since: str) -> list[str]:
        return self._git("log", "--first-parent", "--format=%H", f"--since={since}", ref, "--", SPEC_FILE).split()

    def commits_pages(self, index: "PageIndex", oids: list[str]) -> dict[str, dict[str, int]]:
        """Pages for many commits, in one `git show` per batch rather than one per commit."""
        result: dict[str, dict[str, int]] = {}
        for start in range(0, len(oids), 200):
            output = self._git(
                "show", "--no-color", "--no-ext-diff", "-U0", "--first-parent", "--format=COMMIT %H",
                *oids[start : start + 200], "--", SPEC_FILE,
            )
            for chunk in output.split("COMMIT ")[1:]:
                oid, _, diff = chunk.partition("\n")
                result[oid.strip()] = dict(index.hunk_pages(diff))
        return result


def pull_request_pages(
    repository: SpecRepository,
    index: PageIndex,
    *,
    head_oid: str,
    main_ref: str,
) -> dict[str, int] | None:
    """Hunks per page for a PR head against its merge base with main, or None if unknown."""
    if not repository.has_commit(head_oid):
        return None
    base = repository.merge_base(main_ref, head_oid)
    if base is None:
        return None
    return dict(index.hunk_pages(repository.diff(base, head_oid)))


def commit_pages(repository: SpecRepository, index: PageIndex, oid: str) -> dict[str, int] | None:
    if not repository.has_commit(oid):
        return None
    return dict(index.hunk_pages(repository.commit_diff(oid)))


@dataclass(frozen=True)
class SpecPages:
    """Everything the build knows about spec pages; empty without a spec repository."""

    index: PageIndex | None = None
    # Hunks per page for each open PR, by number, and for each merge commit, by oid.
    by_pr: dict[int, dict[str, int]] | None = None
    by_commit: dict[str, dict[str, int]] | None = None

    def link_pages(self, text: str | None) -> frozenset[str]:
        return self.index.link_pages(text) if self.index else frozenset()


MAIN_REF = "refs/heads/main"
# Bumped when what a cached page map holds changes, so older entries are not reused.
# v2: locations (`page#section/...`) rather than whole pages.
_CACHE_VERSION = "v2:"


def compute_spec_pages(
    repository: SpecRepository,
    *,
    pull_requests: list[tuple[int, str | None]],
    merge_oids: list[str],
    cache: dict[str, dict[str, int]],
    fetch: bool = True,
) -> SpecPages:
    """Pages for open PRs (number, head oid) and merge commits, reusing ``cache``.

    ``cache`` is the snapshot's: keys `pr:<head oid>` and `commit:<oid>`. It is
    updated in place, so an incremental build only runs git on what is new.
    """
    if fetch:
        missing = [number for number, head in pull_requests if head and not repository.has_commit(head)]
        repository.fetch([f"+{MAIN_REF}:{MAIN_REF}", *(f"+refs/pull/{number}/head:refs/pull/{number}/head" for number in missing)])
    index = PageIndex.from_source(repository.source_at(MAIN_REF))

    by_pr: dict[int, dict[str, int]] = {}
    for number, head in pull_requests:
        if not head:
            continue
        key = f"{_CACHE_VERSION}pr:{head}"
        if key not in cache:
            pages = pull_request_pages(repository, index, head_oid=head, main_ref=MAIN_REF)
            if pages is None:
                continue
            cache[key] = pages
        by_pr[number] = cache[key]

    wanted = sorted({oid for oid in merge_oids if oid})
    commit_key = f"{_CACHE_VERSION}commit:"
    uncached = [oid for oid in wanted if commit_key + oid not in cache and repository.has_commit(oid)]
    for oid, pages in repository.commits_pages(index, uncached).items():
        cache[commit_key + oid] = pages
    by_commit = {oid: cache[commit_key + oid] for oid in wanted if commit_key + oid in cache}
    return SpecPages(index=index, by_pr=by_pr, by_commit=by_commit)


def ensure_clone(path: str | Path, repository_slug: str) -> SpecRepository:
    """A bare clone of the spec repository at ``path``, cloned on first use."""
    target = Path(path)
    if not (target / "HEAD").exists():
        subprocess.run(
            ["git", "clone", "--quiet", "--bare", f"https://github.com/{repository_slug}.git", str(target)],
            check=True,
        )
    return SpecRepository(target)

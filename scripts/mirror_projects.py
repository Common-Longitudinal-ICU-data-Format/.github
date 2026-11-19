#!/usr/bin/env python3
"""Mirror closed CLIF project-run repos into the organization, once each.

Why: project code often lives in personal accounts. A one-time, independent copy
in this org makes it searchable with `org:Common-Longitudinal-ICU-data-Format`.
(Forks are left out of code search by default, so the copy is not a fork.)

What one run does:
  1. Reads the queue from the CLIF portal: project runs that are closed, have a
     repo link, and have no org copy recorded yet.
  2. For each one:
       - repo already in this org      -> nothing to copy
       - private / missing source      -> skipped
       - same name and same commit already in this org -> use that repo
       - name used by a different repo in this org      -> skipped
       - uses Git LFS / is empty       -> skipped
       - otherwise                     -> create an org repo with the same name,
                                          push all branches and tags, archive it
     A copy is a snapshot taken at project close. It is never re-synced.
  3. Adds the project to the Projects list in profile/README.md and opens (or
     updates) a pull request. The member-only README in `.github-private` gets
     the same change through its own pull request, so both views stay aligned.
  4. Reports the results to the portal, which records the org link on the run
     and emails the consortium inbox about anything skipped.

With BACKFILL=true it also mirrors the external repos already linked in the
Projects list and re-links those entries to the org copies.

With DRY_RUN=true nothing is created, pushed or reported.

Environment: GH_TOKEN (org token), PORTAL_MIRROR_TOKEN, PORTAL_URL, DRY_RUN,
BACKFILL. Needs `git` and `gh` on the PATH. Standard library only.
"""

from __future__ import annotations

import base64
import datetime
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

ORG = "Common-Longitudinal-ICU-data-Format"
README = "profile/README.md"
PUBLIC_REPO = f"{ORG}/.github"
MEMBER_REPO = f"{ORG}/.github-private"  # README shown to org members
LIST_BRANCH = "automation/project-list"
SNAPSHOT_PREFIX = "Snapshot of https://github.com/"

REPO_URL_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", re.I)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# The link may be empty: a project listed before its repo exists.
ENTRY_RE = re.compile(r"^(\d+)\. \[(.+)\]\((\S*?)\)(.*)$")
PROJECTS_HEADING = "**Projects**"


def flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

def parse_repo(url: str) -> tuple[str, str] | None:
    """`https://github.com/owner/repo(.git)(/tree/...)` -> (owner, repo)."""
    m = REPO_URL_RE.match((url or "").strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not SAFE_NAME_RE.match(owner) or not SAFE_NAME_RE.match(repo):
        return None
    return owner, repo


def same_repo(a: str, b: str) -> bool:
    pa, pb = parse_repo(a), parse_repo(b)
    return bool(pa and pb and (pa[0].lower(), pa[1].lower()) == (pb[0].lower(), pb[1].lower()))


def org_url(repo: str) -> str:
    return f"https://github.com/{ORG}/{repo}"


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

def gh_api(path: str, method: str = "GET", fields: dict | None = None) -> dict | None:
    """Call the GitHub API through `gh`. Returns None on any error (e.g. 404)."""
    cmd = ["gh", "api", "-X", method, path]
    for key, value in (fields or {}).items():
        if isinstance(value, bool):
            cmd += ["-F", f"{key}={'true' if value else 'false'}"]
        else:
            cmd += ["-f", f"{key}={value}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def git(*args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def auth_header() -> str:
    token = os.environ["GH_TOKEN"]
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {basic}"


def uses_lfs(bare_dir: str) -> bool:
    proc = git("grep", "-l", "filter=lfs", "HEAD", "--", ".gitattributes", "**/.gitattributes",
               cwd=bare_dir, check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def head_sha(owner: str, repo: str, branch: str | None) -> str | None:
    """Commit at the tip of a branch, or None if it cannot be read."""
    if not branch:
        return None
    commit = gh_api(f"repos/{owner}/{repo}/commits/{branch}")
    return commit.get("sha") if commit else None


def mirror_one(owner: str, repo: str, dry_run: bool) -> dict:
    """Decide what to do with one source repo and do it.

    Returns {"outcome": "in_org" | "mirrored" | "skipped", "mirror_url"?, "reason"?}.
    """
    if owner.lower() == ORG.lower():
        found = gh_api(f"repos/{ORG}/{repo}")
        if not found:
            return {"outcome": "skipped", "reason": "linked repo not found in the CLIF org"}
        return {"outcome": "in_org", "mirror_url": found["html_url"]}

    source = gh_api(f"repos/{owner}/{repo}")
    if not source or source.get("private"):
        return {"outcome": "skipped", "reason": "source repo is private or not found"}
    source_url = source["html_url"]
    name = source["name"]

    existing = gh_api(f"repos/{ORG}/{name}")
    resume = False
    if existing:
        description = (existing.get("description") or "").lower()
        ours = description.startswith(f"{SNAPSHOT_PREFIX}{owner}/{name} ".lower())
        if not ours:
            # Someone may have copied it into the org by hand. If the org repo is at
            # the same commit as the source, the code is already here: use it as is.
            if head_sha(ORG, name, existing.get("default_branch")) == head_sha(owner, name, source.get("default_branch")) != None:
                return {"outcome": "in_org", "mirror_url": existing["html_url"]}
            return {"outcome": "skipped",
                    "reason": f"a different repo named {name} already exists in the CLIF org"}
        if existing.get("archived"):
            return {"outcome": "mirrored", "mirror_url": existing["html_url"]}
        resume = True  # an earlier run created it but did not finish

    if source.get("size", 0) == 0:
        return {"outcome": "skipped", "reason": "source repo is empty"}
    if dry_run:
        return {"outcome": "mirrored", "mirror_url": org_url(name), "dry": True}

    with tempfile.TemporaryDirectory() as tmp:
        bare = os.path.join(tmp, "src.git")
        cloned = git("clone", "--bare", "--quiet", f"https://github.com/{owner}/{name}.git", bare, check=False)
        if cloned.returncode != 0:
            return {"outcome": "skipped", "reason": "could not clone the source repo"}
        if uses_lfs(bare):
            return {"outcome": "skipped", "reason": "source repo uses Git LFS"}

        if not resume:
            today = datetime.date.today().isoformat()
            created = gh_api(f"orgs/{ORG}/repos", "POST", {
                "name": name,
                "description": f"{SNAPSHOT_PREFIX}{owner}/{name} at project close ({today}). Read-only copy for org code search.",
                "private": False,
                "has_issues": False,
                "has_wiki": False,
                "has_projects": False,
            })
            if not created:
                return {"outcome": "skipped", "reason": "could not create the repo in the CLIF org"}
        # Copied workflow files must never run in the snapshot.
        gh_api(f"repos/{ORG}/{name}/actions/permissions", "PUT", {"enabled": False})

        pushed = git("-c", f"http.https://github.com/.extraheader={auth_header()}",
                     "push", "--quiet", f"https://github.com/{ORG}/{name}.git",
                     "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*",
                     cwd=bare, check=False)
        if pushed.returncode != 0:
            return {"outcome": "skipped",
                    "reason": f"push to the org copy failed (an unfinished {name} repo was left in the org and will be retried)"}

    default_branch = source.get("default_branch")
    if default_branch:
        gh_api(f"repos/{ORG}/{name}", "PATCH", {"default_branch": default_branch})
    if not gh_api(f"repos/{ORG}/{name}", "PATCH", {"archived": True}):
        return {"outcome": "skipped", "reason": f"copied, but could not archive {name} in the org (will be retried)"}
    return {"outcome": "mirrored", "mirror_url": org_url(name)}


# --------------------------------------------------------------------------
# Projects list in profile/README.md
# --------------------------------------------------------------------------

def read_entries(lines: list[str]) -> tuple[list[dict], int, int]:
    """Find the numbered project links in the two Projects columns.

    Returns (entries, first-column insert index, second-column insert index).
    Each entry is {"title", "url", "rest", "line"}.
    """
    headings = [i for i, line in enumerate(lines) if PROJECTS_HEADING in line]
    if len(headings) != 2:
        raise SystemExit(f"Expected two Projects headings in {README}, found {len(headings)}.")
    first, second = headings
    end = next((i for i in range(second, len(lines)) if lines[i].strip() == "</td>"), len(lines))
    entries = []
    for i in range(first, end):
        m = ENTRY_RE.match(lines[i])
        if m:
            entries.append({"title": m.group(2), "url": m.group(3), "rest": m.group(4), "line": i})
    left = [e for e in entries if e["line"] < second]
    right = [e for e in entries if e["line"] > second]
    if not left or not right:
        raise SystemExit(f"Could not find project entries in both columns of {README}.")
    return entries, left[0]["line"], right[0]["line"]


def md_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().replace("[", "(").replace("]", ")")


def rewrite_readme(text: str, relinks: dict[str, str], additions: list[dict]) -> str:
    """Re-link existing entries, add new projects, sort, renumber, rebalance.

    relinks:   {old url: new url}
    additions: [{"title", "url", "source_url"}]; skipped when an entry already
               points at the same repo (the org copy or its source).
    """
    lines = text.split("\n")
    entries, left_at, right_at = read_entries(lines)

    for e in entries:
        for old, new in relinks.items():
            if e["url"] == old or same_repo(e["url"], old):
                e["url"] = new
    for add in additions:
        if any(same_repo(e["url"], add["url"]) or same_repo(e["url"], add["source_url"]) for e in entries):
            continue
        entries.append({"title": md_title(add["title"]), "url": add["url"], "rest": "", "line": -1})

    entries.sort(key=lambda e: e["title"].casefold())
    rendered = [f"{n}. [{e['title']}]({e['url']}){e['rest']}" for n, e in enumerate(entries, 1)]
    half = math.ceil(len(rendered) / 2)

    old_lines = {e["line"] for e in entries if e["line"] >= 0}
    out: list[str] = []
    for i, line in enumerate(lines):
        if i == left_at:
            out.extend(rendered[:half])
        if i == right_at:
            out.extend(rendered[half:])
        if i not in old_lines:
            out.append(line)
    return "\n".join(out)


def authed_git(*args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return git("-c", f"http.https://github.com/.extraheader={auth_header()}", *args, cwd=cwd, check=check)


def checkout_list_branch(cwd: str | None = None) -> None:
    """Start from the open list branch if there is one, else from main.

    The branch is reused while its pull request is unmerged, so entries from an
    earlier week are not lost.
    """
    # The workflow checkout (cwd=None) already carries credentials. A second
    # auth header would be rejected, so only temporary clones get one added.
    remote_git = authed_git if cwd else git
    fetched = remote_git("fetch", "--quiet", "origin", LIST_BRANCH, cwd=cwd, check=False)
    if fetched.returncode == 0:
        git("checkout", "--quiet", "-B", LIST_BRANCH, "FETCH_HEAD", cwd=cwd)
        merged = git("merge", "--quiet", "--no-edit", "origin/main", cwd=cwd, check=False)
        if merged.returncode != 0:
            raise SystemExit(f"{LIST_BRANCH} conflicts with main. Merge or close its pull request, then re-run.")
    else:
        git("checkout", "--quiet", "-B", LIST_BRANCH, "origin/main", cwd=cwd)


def publish_readme(repo: str, new_text: str, summary: list[str], cwd: str | None = None) -> None:
    """Commit the README to the list branch of `repo` and make sure a pull request is open."""
    git("config", "user.name", "clif-mirror-bot", cwd=cwd)
    git("config", "user.email", "clif-mirror-bot@users.noreply.github.com", cwd=cwd)
    with open(os.path.join(cwd or ".", README), "w", encoding="utf-8") as fh:
        fh.write(new_text)
    git("add", README, cwd=cwd)
    git("commit", "--quiet", "-m", "Update Projects list with mirrored project repos", cwd=cwd)
    remote_git = authed_git if cwd else git
    remote_git("push", "--quiet", "--force-with-lease", "origin", f"HEAD:{LIST_BRANCH}", cwd=cwd)
    open_pr = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--head", LIST_BRANCH, "--state", "open",
         "--json", "number", "--jq", "length"],
        capture_output=True, text=True)
    if open_pr.stdout.strip() in ("", "0"):
        body = "Weekly update from `scripts/mirror_projects.py`.\n\n" + "\n".join(f"- {s}" for s in summary)
        subprocess.run(["gh", "pr", "create", "--repo", repo, "--head", LIST_BRANCH, "--base", "main",
                        "--title", "Update Projects list with mirrored project repos", "--body", body],
                       check=True)


def update_member_readme(relinks: dict[str, str], additions: list[dict], summary: list[str], dry_run: bool) -> None:
    """Apply the same list changes to the member-only profile README.

    Organization members see profile/README.md from the private `.github-private`
    repo instead of the public one, so it gets the same update through its own
    pull request. Skipped quietly if that repo does not exist.
    """
    if not gh_api(f"repos/{MEMBER_REPO}"):
        print(f"{MEMBER_REPO} not found; member README not updated.")
        return
    with tempfile.TemporaryDirectory() as tmp:
        cloned = authed_git("clone", "--quiet", f"https://github.com/{MEMBER_REPO}.git", tmp, check=False)
        if cloned.returncode != 0:
            print(f"Could not clone {MEMBER_REPO}; member README not updated.")
            return
        if not dry_run:
            checkout_list_branch(tmp)
        path = os.path.join(tmp, README)
        if not os.path.exists(path):
            print(f"{MEMBER_REPO} has no {README}; member README not updated.")
            return
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
        new = rewrite_readme(old, relinks, additions)
        print(f"member README {'changes' if new != old else 'unchanged'}")
        if new != old and not dry_run:
            publish_readme(MEMBER_REPO, new, summary, cwd=tmp)


# --------------------------------------------------------------------------
# Portal
# --------------------------------------------------------------------------

def portal(path: str, payload: dict | None = None) -> dict:
    base = os.environ.get("PORTAL_URL", "https://clif-icu.com").rstrip("/")
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {os.environ['PORTAL_MIRROR_TOKEN']}",
            "Content-Type": "application/json",
            "User-Agent": "clif-mirror-projects",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    dry_run, backfill = flag("DRY_RUN"), flag("BACKFILL")
    print(f"dry_run={dry_run} backfill={backfill}")

    if not dry_run:
        checkout_list_branch()
    with open(README, encoding="utf-8") as fh:
        readme = fh.read()

    cache: dict[tuple[str, str], dict] = {}

    def handle(url: str) -> dict:
        parsed = parse_repo(url)
        if not parsed:
            return {"outcome": "skipped", "reason": "repo link is not a GitHub repository URL"}
        key = (parsed[0].lower(), parsed[1].lower())
        if key not in cache:
            cache[key] = mirror_one(parsed[0], parsed[1], dry_run)
        return cache[key]

    results: list[dict] = []      # reported to the portal
    relinks: dict[str, str] = {}  # README: old url -> org url
    additions: list[dict] = []    # README: new entries
    summary: list[str] = []

    if backfill:
        entries, _, _ = read_entries(readme.split("\n"))
        for e in entries:
            parsed = parse_repo(e["url"])
            if parsed and parsed[0].lower() == ORG.lower():
                continue
            outcome = handle(e["url"])
            print(f"[backfill] {e['title']}: {outcome}")
            if outcome["outcome"] == "mirrored":
                relinks[e["url"]] = outcome["mirror_url"]
                summary.append(f"Re-linked **{e['title']}** to {outcome['mirror_url']} (copied from {e['url']})")
            else:
                results.append({"title": e["title"], "repo_url": e["url"],
                                "outcome": "skipped", "reason": outcome.get("reason", "not mirrored")})

    for run in portal("/api/project-runs/mirror-queue")["runs"]:
        outcome = handle(run["repo_url"])
        print(f"[queue] {run['title']}: {outcome}")
        result = {"id": run["id"], "title": run["title"], "repo_url": run["repo_url"],
                  "outcome": outcome["outcome"]}
        if outcome["outcome"] == "skipped":
            result["reason"] = outcome["reason"]
        else:
            result["mirror_url"] = outcome["mirror_url"]
            additions.append({"title": run["title"], "url": outcome["mirror_url"], "source_url": run["repo_url"]})
            if outcome["outcome"] == "mirrored":
                relinks[run["repo_url"]] = outcome["mirror_url"]
            summary.append(f"**{run['title']}**: {outcome['mirror_url']}")
        results.append(result)

    new_readme = rewrite_readme(readme, relinks, additions)
    changed = new_readme != readme
    print(f"README {'changes' if changed else 'unchanged'}; {len(results)} result(s)")

    update_member_readme(relinks, additions, summary, dry_run)

    if dry_run:
        if changed:
            import difflib
            sys.stdout.writelines(difflib.unified_diff(
                readme.splitlines(True), new_readme.splitlines(True), "README (now)", "README (after)", n=0))
        print("Dry run: nothing was created, pushed or reported.")
        return 0

    if changed:
        publish_readme(PUBLIC_REPO, new_readme, summary)
    if results:
        print("portal:", portal("/api/project-runs/mirror-results", {"results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

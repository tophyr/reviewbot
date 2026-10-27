"""Find repositories and open pull requests to review."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .github_auth import GitHubError

log = logging.getLogger(__name__)

SKIP_LABEL = "no-reviewbot"  # a pull request with this label is not reviewed

PR_REF_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)$")


def parse_pr_ref(ref: str) -> tuple[str, int]:
    m = PR_REF_RE.match(ref.strip())
    if not m:
        raise ValueError(f"expected owner/name#number, got {ref!r}")
    return f"{m.group(1)}/{m.group(2)}", int(m.group(3))


def pr_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def state_key(pr_json: dict) -> str:
    """The state key for a pull request: owner/name#number, where owner/name
    is GitHub's full_name of the base repository as written into pr.json.

    Every state lookup and every state write uses this, never the spelling
    from a command line, so a case difference or a renamed repository cannot
    make the lookup miss the record.
    """
    return pr_key(pr_json["repo"], pr_json["number"])


def list_repos(auth) -> tuple[list[str], list[str]]:
    """Repositories to poll, as owner/name, and a description of each
    installation whose repositories could not be listed.

    The repositories are every repository of every installation of the App.
    A suspended installation is skipped (its token request would fail), and
    an installation whose token or repository list cannot be fetched is
    logged and skipped, so the other installations are still polled.
    """
    repos: list[str] = []
    failures: list[str] = []
    for inst in auth.list_installations():
        inst_id = int(inst["id"])
        account = (inst.get("account") or {}).get("login", "?")
        if inst.get("suspended_at"):
            log.warning("installation %s (%s) is suspended since %s; skipping it", inst_id,
                        account, inst["suspended_at"])
            continue
        try:
            inst_repos = auth.installation_repos(inst_id)
        except GitHubError as exc:
            log.warning("listing repositories of installation %s (%s) failed; skipping it this "
                        "pass: %s", inst_id, account, exc)
            failures.append(f"installation {inst_id} ({account}): {exc}")
            continue
        for repo in inst_repos:
            full = repo["full_name"]
            if repo.get("archived") or repo.get("disabled"):
                continue
            auth.remember_repo(full, inst_id)
            repos.append(full)
    return repos, failures


def is_bot(pr: dict) -> bool:
    user = pr.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def label_names(pr: dict) -> list[str]:
    return [lbl.get("name", "") for lbl in pr.get("labels") or []]


def skip_reason(pr: dict, skip_label: str) -> str | None:
    if pr.get("state") != "open":
        return f"state is {pr.get('state')}"
    if pr.get("draft"):
        return "draft"
    if skip_label in label_names(pr):
        return f"has label {skip_label}"
    if is_bot(pr):
        return "bot author"
    return None


def list_open_prs(api, token: str, repo: str) -> list[dict]:
    return list(api.paginate(f"/repos/{repo}/pulls", token, params={"state": "open"}))


def get_pr(api, token: str, repo: str, number: int) -> dict:
    return api.get_json(f"/repos/{repo}/pulls/{number}", token)


def build_pr_json(pr: dict) -> dict:
    """The pr.json document from DESIGN.md."""
    return {
        "repo": pr["base"]["repo"]["full_name"],
        "number": int(pr["number"]),
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "author": (pr.get("user") or {}).get("login", ""),
        "base_ref": pr["base"]["ref"],
        "base_sha": pr["base"]["sha"],
        "head_ref": pr["head"]["ref"],
        "head_sha": pr["head"]["sha"],
        # null when the fork that held the head branch has been deleted
        "head_repo": (pr["head"]["repo"] or {}).get("full_name"),
        "html_url": pr.get("html_url", ""),
        "labels": label_names(pr),
    }


@dataclass
class Candidate:
    repo: str
    number: int
    head_sha: str
    pr_json: dict
    # skip_reason of GitHub's pull request record, which pr_json does not
    # keep all the fields of. pipeline.skip_reason adds the state checks.
    skip: str | None

    @property
    def key(self) -> str:
        return state_key(self.pr_json)


def make_candidate(pr: dict) -> Candidate:
    doc = build_pr_json(pr)
    return Candidate(doc["repo"], doc["number"], doc["head_sha"], doc,
                     skip_reason(pr, SKIP_LABEL))


@dataclass
class Listing:
    # Every open pull request of the repositories in listed.
    candidates: list[Candidate]
    # Repositories whose open pull requests were all listed.
    listed: list[str]
    # Repositories or installations whose pull requests could not be listed.
    failures: list[str]


def candidates(auth) -> Listing:
    out: list[Candidate] = []
    listed: list[str] = []
    repos, failures = list_repos(auth)
    for repo in repos:
        try:
            token = auth.token_for_repo(repo)
            prs = list_open_prs(auth.api, token, repo)
        except Exception as exc:  # one broken repo must not stop the pass
            log.error("listing pull requests for %s failed: %s", repo, exc)
            failures.append(f"{repo}: {exc}")
            continue
        out.extend(make_candidate(pr) for pr in prs)
        listed.append(repo)
    return Listing(out, listed, failures)

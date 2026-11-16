"""Skills people hand the hive, as pull requests and nothing else (10.18.8-9).

A person picks a skill folder on their own machine; the browser sends its
files here; this opens a pull request adding it to the hive's skills
(`REPOSITORY`, under `SKILLS_PATH`) and stops. The hive runs on a server and
cannot reach anybody's laptop, so the import starts on the person's side and
arrives as an upload — the hive never goes looking.

**A submission never lands in the hive by itself.** The only thing written
anywhere is a new branch named for the submission and the pull request from
it; the base branch is never written and nothing here merges. A person
merges every submission, on GitHub. Who submitted it changes nothing about
that: the owner's own ways of adding a skill — a Mind's write-back, a commit
— are untouched, and the owner using this page gets a pull request too.

**The record is written first.** The submission's row exists before any call
to GitHub, and its branch is named from the row's id, so a submission cut
off half way (branch made, pull request not) is retried as the same branch
and the same pull request rather than as a second one.

**Community submissions** (10.18.9) are skills a person other than the owner
submitted as a *new* skill — a name the repo did not already have. Once such
a pull request is merged, that name is a community skill: left out of the
console's skill lists unless asked for, and labelled when shown. The state
is refreshed from GitHub when the lists are read; when GitHub cannot say, a
submission is still counted as community, because showing a community skill
unlabelled is the failure and hiding one a moment longer is not. A pull
request closed without merging stops counting: that skill never reached
the hive.
"""

from __future__ import annotations

import base64
import os
import posixpath
import re
import time
from typing import Any

import httpx
from fastapi import HTTPException

import credential_registry
import store

#: The hive's repository, and the folder its shipped skills live in.
REPOSITORY = os.getenv("HIVE_SKILLS_REPOSITORY", "danielstewart77/hive-mind")
BASE_BRANCH = os.getenv("HIVE_SKILLS_BASE_BRANCH", "master")
SKILLS_PATH = "specs/skills"

GITHUB_API = "https://api.github.com"

#: What one submission may carry. A skill is markdown and a few helpers; a
#: folder past these is a virtualenv or a dataset, not a skill.
MAX_FILES = 200
MAX_BYTES = 2 * 1024 * 1024

#: How long a pull request's merge state is trusted before it is asked again.
CHECK_EVERY = 5 * 60

#: The most pull requests one read of the skill lists asks GitHub about, and
#: how long each may take. A list read waits on these, so they are bounded;
#: what is not asked this time is asked on the next read.
REFRESH_BATCH = 10
REFRESH_TIMEOUT = 5.0

_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

#: The transport GitHub is reached through; None is the real network. The
#: one place a test stands in for the outside world.
_transport: httpx.AsyncBaseTransport | None = None


def _token() -> str:
    for holder in ("gh_hosts", "git_credentials"):
        token = credential_registry._host_github_token(holder)
        if token:
            return token
    return ""


def _client() -> httpx.AsyncClient:
    token = _token()
    if not token:
        raise HTTPException(status_code=503,
                            detail="This hive has no GitHub token to open a pull request with")
    return httpx.AsyncClient(
        base_url=GITHUB_API, transport=_transport, timeout=20.0,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "hive-console-skill-submissions"})


# ------------------------------------------------------------- the upload

def checked_files(files: list[tuple[str, bytes]]) -> tuple[str, list[tuple[str, bytes]]]:
    """The skill's name and its files, each path inside the skill folder.

    Refused, before anything is recorded or sent, when the folder has no
    SKILL.md at its top, a path leaves it, two paths name one file, or it is
    larger than a skill is.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were sent")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"More than {MAX_FILES} files is not a skill")
    if sum(len(data) for _, data in files) > MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"Larger than {MAX_BYTES // (1024 * 1024)} MB is not a skill")
    tops = set()
    cleaned: list[tuple[str, bytes]] = []
    seen = set()
    for raw, data in files:
        path = str(raw or "").replace("\\", "/")
        normal = posixpath.normpath(path)
        if (not path or path.startswith("/") or normal.startswith("..")
                or "/../" in f"/{path}/" or normal in (".", "")):
            raise HTTPException(status_code=400, detail=f"Not a path inside the skill folder: {raw!r}")
        parts = normal.split("/")
        if len(parts) < 2:
            raise HTTPException(status_code=400, detail="Pick the skill's folder, not single files")
        tops.add(parts[0])
        inner = "/".join(parts[1:])
        if inner in seen:
            raise HTTPException(status_code=400, detail=f"Two files are both {inner}")
        seen.add(inner)
        cleaned.append((inner, data))
    if len(tops) != 1:
        raise HTTPException(status_code=400, detail="Submit one skill folder at a time")
    name = tops.pop().strip().lower()
    if not _SKILL_NAME.fullmatch(name):
        raise HTTPException(status_code=400,
                            detail="A skill folder is named in lower-case letters, digits and hyphens")
    if "SKILL.md" not in seen:
        raise HTTPException(status_code=400, detail="The folder has no SKILL.md at its top")
    return name, sorted(cleaned)


# ------------------------------------------------------------- submitting

async def submit(user: dict, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Record a submission and open its pull request. Never writes the base."""
    name, cleaned = checked_files(files)
    username = str(user.get("username") or "").strip().lower()
    if not username:
        raise HTTPException(status_code=401, detail="Sign in to submit a skill")
    # Recorded before GitHub is asked anything, as a new skill until GitHub
    # says otherwise: a submission whose kind is not known yet is counted as
    # a community one, which hides it a moment longer rather than showing it
    # unlabelled.
    submission_id = await store.add_skill_submission(
        skill=name, submitted_by=username, by_owner=bool(user.get("is_admin")),
        new_skill=True, repository=REPOSITORY,
        branch=f"pending-{time.time_ns()}")
    return await _open(submission_id, name, cleaned)


async def retry(submission_id: int, user: dict, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Finish a submission that was cut off, as the same branch and PR."""
    row = await store.skill_submission(submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such submission")
    if row["submitted_by"] != str(user.get("username") or "").lower() and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="That submission is not yours")
    if row["pr_number"]:
        # Its pull request exists. Moving the branch now would rewrite what a
        # reviewer is reading, or commit onto a branch already merged.
        raise HTTPException(status_code=409,
                            detail="This submission already has its pull request; submit again for a new one")
    name, cleaned = checked_files(files)
    if name != row["skill"]:
        raise HTTPException(status_code=400, detail=f"This submission is the skill {row['skill']}")
    return await _open(submission_id, name, cleaned)


def branch_for(submission_id: int, name: str) -> str:
    return f"skill-submission/{submission_id}-{name}"


async def _open(submission_id: int, name: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    row = await store.skill_submission(submission_id)
    branch = branch_for(submission_id, name)
    if row["branch"] != branch:
        await store.update_skill_submission(submission_id, branch=branch)
    try:
        async with _client() as client:
            if row["new_skill"]:
                exists = await _exists(client, f"{SKILLS_PATH}/{name}")
                if exists:
                    await store.update_skill_submission(submission_id, new_skill=False)
                    row = {**row, "new_skill": False}
            base_sha = (await _github(client, "GET", f"/repos/{REPOSITORY}/git/ref/heads/{BASE_BRANCH}"))["object"]["sha"]
            base_tree = (await _github(client, "GET", f"/repos/{REPOSITORY}/git/commits/{base_sha}"))["tree"]["sha"]
            entries = []
            for inner, data in files:
                blob = await _github(client, "POST", f"/repos/{REPOSITORY}/git/blobs",
                                     json={"content": base64.b64encode(data).decode(), "encoding": "base64"})
                # A browser upload carries no file mode; a script is known by
                # its first line, and lands executable as it was on disk.
                mode = "100755" if data.startswith(b"#!") else "100644"
                entries.append({"path": f"{SKILLS_PATH}/{name}/{inner}", "mode": mode,
                                "type": "blob", "sha": blob["sha"]})
            tree = await _github(client, "POST", f"/repos/{REPOSITORY}/git/trees",
                                 json={"base_tree": base_tree, "tree": entries})
            commit = await _github(client, "POST", f"/repos/{REPOSITORY}/git/commits", json={
                "message": f"skill submission: {name} (from {row['submitted_by']})",
                "tree": tree["sha"], "parents": [base_sha]})
            await _point_branch(client, branch, commit["sha"])
            pull = await _pull_for(client, branch)
            if pull is not None and pull.get("state") != "open":
                pull = None
            if pull is None:
                pull = await _github(client, "POST", f"/repos/{REPOSITORY}/pulls", json={
                    "title": f"Skill submission: {name}",
                    "head": branch, "base": BASE_BRANCH,
                    "body": (f"Submitted on the hive console by {row['submitted_by']}"
                             f"{' (the owner)' if row['by_owner'] else ''}. "
                             f"{'A new skill.' if row['new_skill'] else 'A change to an existing skill.'} "
                             "A person reviews and merges every submission; nothing lands until then.")})
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code,
                            detail=f"Submission {submission_id} is recorded but its pull request "
                                   f"is not open yet: {exc.detail}. Retry it from the submissions list.") from exc
    await store.update_skill_submission(submission_id, pr_number=int(pull["number"]),
                                        pr_url=str(pull.get("html_url") or ""))
    return {"ok": True, "id": submission_id, "skill": name, "branch": branch,
            "pr_number": int(pull["number"]), "pr_url": pull.get("html_url")}


async def _github(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> dict:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub is unreachable: {exc}") from exc
    if response.status_code >= 400:
        try:
            message = response.json().get("message") or response.text
        except ValueError:
            message = response.text
        raise HTTPException(status_code=502, detail=f"GitHub refused {method} {path}: {message}")
    body = response.json()
    return body if isinstance(body, dict) else {"items": body}


async def _exists(client: httpx.AsyncClient, path: str) -> bool:
    try:
        response = await client.get(f"/repos/{REPOSITORY}/contents/{path}",
                                     params={"ref": BASE_BRANCH})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub is unreachable: {exc}") from exc
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GitHub refused reading {path}: {response.status_code}")
    return True


async def _point_branch(client: httpx.AsyncClient, branch: str, sha: str) -> None:
    """Make the submission's branch, or move it if a cut-off try made it."""
    response = await client.post(f"/repos/{REPOSITORY}/git/refs",
                                 json={"ref": f"refs/heads/{branch}", "sha": sha})
    if response.status_code == 422:
        await _github(client, "PATCH", f"/repos/{REPOSITORY}/git/refs/heads/{branch}",
                      json={"sha": sha, "force": True})
        return
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GitHub refused the branch: {response.status_code}")


async def _pull_for(client: httpx.AsyncClient, branch: str) -> dict | None:
    owner = REPOSITORY.split("/")[0]
    found = await _github(client, "GET", f"/repos/{REPOSITORY}/pulls",
                          params={"head": f"{owner}:{branch}", "state": "all"})
    items = found.get("items") or []
    return items[0] if items else None


# ------------------------------------------------------------- community

async def refresh() -> None:
    """Learn whether open pull requests were merged or closed. Best effort."""
    now = time.time()
    due = [r for r in await store.skill_submissions()
           if r["pr_number"] and r["merged_at"] is None and r["closed_at"] is None
           and (r["checked_at"] is None or now - r["checked_at"] >= CHECK_EVERY)]
    if not due:
        return
    try:
        client = _client()
    except HTTPException:
        return
    client.timeout = httpx.Timeout(REFRESH_TIMEOUT)
    async with client:
        for row in due[:REFRESH_BATCH]:
            try:
                pull = await _github(client, "GET", f"/repos/{row['repository']}/pulls/{row['pr_number']}")
            except HTTPException:
                continue
            fields: dict[str, Any] = {"checked_at": now}
            if pull.get("merged_at"):
                fields["merged_at"] = now
            elif pull.get("state") == "closed":
                fields["closed_at"] = now
            await store.update_skill_submission(row["id"], **fields)


def community_from(rows: list[dict]) -> set[str]:
    """The skill names that are community submissions, given the records.

    A new skill from somebody other than the owner, once its pull request is
    merged. One whose state GitHub has never told us is counted too, so a
    community skill is never shown unlabelled for want of an answer; one
    known to be open, or closed unmerged, has not reached the hive and is
    not counted, so it cannot hide a skill of the same name somebody else
    already has.
    """
    names = set()
    for row in rows:
        if row["by_owner"] or not row["new_skill"] or not row["pr_number"]:
            continue
        if row["merged_at"] is not None or row["checked_at"] is None:
            names.add(row["skill"])
    return names


async def community_names() -> set[str]:
    await refresh()
    return community_from(await store.skill_submissions())


async def filter_skills(payload: dict, *, include_community: bool) -> dict:
    """A Mind's skill listing with community submissions left out or labelled."""
    names = await community_names()
    rows = payload.get("skills") or []
    kept, hidden = [], 0
    for row in rows:
        community = str(row.get("name") or "") in names
        if community and not include_community:
            hidden += 1
            continue
        kept.append({**row, "community": community})
    return {**payload, "skills": kept, "community_hidden": hidden}

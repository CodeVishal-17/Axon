"""GitHub adapter — the MVP's belief source AND reality source.

Implementation choices (architecture §8):

* **Tarball over git clone** — one HTTP call, no git binary, no clone
  lifecycle. Streamed to a temp file so large repos never sit in memory.
* **Issues endpoint yields PRs too** — ``/issues?state=all`` returns both
  (PRs carry a ``pull_request`` key), so one paginated endpoint covers both
  belief kinds.
* **Ownership from commit-detail calls, capped** — the commits list endpoint
  doesn't include touched files, so we fetch details for the most recent N
  commits. Recent-N is exactly the signal bus-factor needs; uncapped history
  is cost without signal.
"""

from __future__ import annotations

import logging
import tarfile
import tempfile
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from axon.adapters.base import (
    AdapterError,
    AuthenticationError,
    RateLimitError,
    RepositoryNotFoundError,
    CommitInfo,
    KnowledgeDoc,
    NormalizedEvent,
    RepoFile,
    RepoInfo,
    sha256_text,
)
from axon.config import get_settings

logger = logging.getLogger("axon.adapters.github")

_API = "https://api.github.com"
_PER_PAGE = 100
# Issue/PR bodies can be enormous; cap what we carry into the graph.
_MAX_BODY_CHARS = 20_000


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubAdapter:
    """Implements both BeliefSource and RealitySource for one repository."""

    def __init__(
        self,
        full_name: str,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.full_name = full_name
        settings = get_settings()
        resolved_token = token or settings.github_token

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "axon-ingest",
        }
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
        # `client` injection exists for tests (no network).
        self._client = client or httpx.Client(
            headers=headers, 
            timeout=httpx.Timeout(60.0, connect=10.0), 
            follow_redirects=True
        )

    # --- internals -------------------------------------------------------

    def _request_with_retry(self, method: str, path: str, allow_statuses: tuple[int, ...] = (), **kwargs: Any) -> httpx.Response:
        import time
        max_attempts = 3
        for attempt in range(max_attempts):
            response = self._client.request(method, f"{_API}{path}", **kwargs)
            if response.status_code in (502, 503, 504) and attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue
            
            if response.status_code in allow_statuses:
                return response

            if response.status_code == 401:
                raise AuthenticationError("GitHub token is invalid or expired.")
            if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
                reset_str = response.headers.get("x-ratelimit-reset")
                reset_at = datetime.fromtimestamp(int(reset_str)) if reset_str else None
                raise RateLimitError(
                    f"GitHub rate limit exhausted (resets at {reset_str}).", 
                    reset_at=reset_at
                )
            if response.status_code == 404:
                raise RepositoryNotFoundError(
                    f"GitHub returned 404 for {path} — repo missing or token lacks access"
                )
            if response.is_error:
                raise AdapterError(
                    f"GitHub API error {response.status_code} on {path}: "
                    f"{response.text[:200]}"
                )
            return response
        raise AdapterError("Unreachable")

    def _get(self, path: str, **params: Any) -> httpx.Response:
        return self._request_with_retry("GET", path, params=params or None)

    def _paginate(self, path: str, limit: int, **params: Any) -> Iterator[dict]:
        """Yield items across pages until `limit` items or a short page."""
        yielded = 0
        page = 1
        while yielded < limit:
            batch = self._get(
                path, per_page=min(_PER_PAGE, limit - yielded), page=page, **params
            ).json()
            if not batch:
                return
            for item in batch:
                yield item
                yielded += 1
                if yielded >= limit:
                    return
            if len(batch) < _PER_PAGE:
                return
            page += 1

    # --- RealitySource ---------------------------------------------------

    def fetch_repo_info(self) -> RepoInfo:
        repo = self._get(f"/repos/{self.full_name}").json()
        branch = repo["default_branch"]
        commits = self._get(
            f"/repos/{self.full_name}/commits", sha=branch, per_page=1
        ).json()
        head_sha = commits[0]["sha"] if commits else None
        return RepoInfo(
            external_id=str(repo["id"]),
            full_name=repo["full_name"],
            default_branch=branch,
            head_sha=head_sha,
        )

    def iter_files(self, max_file_bytes: int) -> Iterator[RepoFile]:
        """Stream the default-branch tarball and yield regular files.

        Oversized members are skipped here (never read into memory);
        content-based filtering (binary sniffing, ignore rules) is the
        ingestion service's job — the adapter only normalizes.
        """
        url = f"{_API}/repos/{self.full_name}/tarball"
        with tempfile.TemporaryFile() as spool:
            with self._client.stream("GET", url) as response:
                if response.is_error:
                    raise AdapterError(
                        f"tarball download failed: {response.status_code}"
                    )
                for chunk in response.iter_bytes():
                    spool.write(chunk)
            spool.seek(0)

            with tarfile.open(fileobj=spool, mode="r:gz") as tar:
                for member in tar:
                    if not member.isreg() or member.size > max_file_bytes:
                        continue
                    # First path component is the "{repo}-{sha}/" prefix.
                    _, _, path = member.name.partition("/")
                    if not path:
                        continue
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    yield RepoFile(path=path, content=extracted.read())

    def fetch_file(self, path: str, max_bytes: int = 500_000) -> bytes | None:
        """Current content of one file at the default branch, or None.

        Used by drift verification: the DB stores only content hashes for
        code, so 'what does the code say TODAY' is always fetched fresh.
        Contents API returns base64 for files up to 1MB.
        """
        import base64  # noqa: PLC0415

        try:
            payload = self._get(f"/repos/{self.full_name}/contents/{path}").json()
        except AdapterError:
            return None  # deleted/renamed files are a normal outcome here
        if payload.get("type") != "file" or payload.get("size", 0) > max_bytes:
            return None
        if payload.get("encoding") == "base64" and payload.get("content"):
            return base64.b64decode(payload["content"])
        return None

    def iter_commits(self, limit: int) -> Iterator[CommitInfo]:
        for item in self._paginate(f"/repos/{self.full_name}/commits", limit):
            sha = item["sha"]
            detail = self._get(f"/repos/{self.full_name}/commits/{sha}").json()
            author = item.get("author") or {}
            commit_meta = item.get("commit", {}).get("author", {})
            yield CommitInfo(
                sha=sha,
                author_login=author.get("login") or commit_meta.get("name"),
                authored_at=_parse_dt(commit_meta.get("date")),
                files=tuple(
                    f["filename"] for f in detail.get("files", []) if "filename" in f
                ),
            )

    # --- Write side (PR generation) --------------------------------------
    # Five primitives; a future GitLab adapter implements the same five and
    # GitHubPRService is unchanged.

    def _post(self, path: str, json: dict[str, Any]) -> httpx.Response:
        return self._request_with_retry("POST", path, json=json)

    def get_branch_head(self, branch: str) -> str:
        ref = self._get(f"/repos/{self.full_name}/git/ref/heads/{branch}").json()
        return ref["object"]["sha"]

    def branch_exists(self, branch: str) -> bool:
        response = self._request_with_retry(
            "GET",
            f"/repos/{self.full_name}/git/ref/heads/{branch}",
            allow_statuses=(404,)
        )
        return response.status_code != 404

    def create_branch(self, branch: str, from_sha: str) -> None:
        self._post(
            f"/repos/{self.full_name}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": from_sha},
        )

    def fetch_file_with_sha(
        self, path: str, ref: str | None = None
    ) -> tuple[bytes, str] | None:
        """(content, blob sha) at ref, or None when the file is missing.
        The sha is the contents-API write precondition."""
        import base64  # noqa: PLC0415

        params = {"ref": ref} if ref else {}
        response = self._request_with_retry(
            "GET",
            f"/repos/{self.full_name}/contents/{path}",
            allow_statuses=(404,),
            params=params
        )
        if response.status_code == 404:
            return None
        payload = response.json()
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            return None
        return base64.b64decode(payload["content"]), payload["sha"]

    def put_file(
        self, path: str, content: bytes, message: str, branch: str, sha: str
    ) -> None:
        """One commit updating one file on a branch (contents API)."""
        import base64  # noqa: PLC0415

        self._request_with_retry(
            "PUT",
            f"/repos/{self.full_name}/contents/{path}",
            json={
                "message": message,
                "content": base64.b64encode(content).decode(),
                "branch": branch,
                "sha": sha,
            },
        )

    def find_pull(self, head_branch: str) -> str | None:
        owner = self.full_name.split("/")[0]
        pulls = self._get(
            f"/repos/{self.full_name}/pulls",
            head=f"{owner}:{head_branch}",
            state="open",
        ).json()
        return pulls[0]["html_url"] if pulls else None

    def create_pull(self, title: str, body: str, head: str, base: str) -> str:
        response = self._request_with_retry(
            "POST",
            f"/repos/{self.full_name}/pulls",
            allow_statuses=(422,),
            json={"title": title, "body": body, "head": head, "base": base},
        )
        if response.status_code == 422:
            if "already exist" in response.text:
                existing = self.find_pull(head)
                if existing:
                    return existing
            raise AdapterError(
                f"GitHub API error 422 creating PR: {response.text[:300]}"
            )
        return response.json()["html_url"]

    def fetch_pr_files(self, number: int, limit: int = 300) -> tuple[str, ...]:
        """Changed file paths of a pull request (used by the scoped
        verification planner — merged-PR webhook payloads carry no file
        list, so the worker fetches it here, never the request path)."""
        paths: list[str] = []
        for item in self._paginate(
            f"/repos/{self.full_name}/pulls/{number}/files", limit
        ):
            if "filename" in item:
                paths.append(item["filename"])
        return tuple(paths)

    # --- Event normalization ---------------------------------------------

    @staticmethod
    def normalize_webhook(
        event_name: str,
        delivery_id: str,
        payload: dict[str, Any],
        default_branch: str,
    ) -> NormalizedEvent | None:
        """GitHub webhook → NormalizedEvent, or None when reality is
        unchanged. Pure function, no I/O — safe in the request path.

        Semantics (events are REALITY changes):
        - push to the default branch          → "push" event
        - pull_request closed with merged     → "pr_merged" event
        - pull_request opened/synchronize     → None (a proposal; the code
          on the default branch did not change — verifying against it
          would be wrong)
        - issues closed                       → "issue_closed" event
        - issues edited/reopened              → reingest_only (a BELIEF
          changed; re-extraction handles it, no Event row)
        - anything else (discussions, stars…) → None
        """
        if event_name == "push":
            ref = payload.get("ref", "")
            if ref != f"refs/heads/{default_branch}":
                return None  # non-default-branch pushes don't change reality
            changed: set[str] = set()
            for commit in payload.get("commits", []):
                for key in ("added", "modified", "removed"):
                    changed.update(commit.get(key, []))
            return NormalizedEvent(
                provider="github",
                external_id=delivery_id,
                kind="push",
                action="push",
                changed_paths=tuple(sorted(changed)),
                head_sha=payload.get("after"),
                title=(payload.get("head_commit") or {}).get("message", "")[:200],
            )

        if event_name == "pull_request":
            action = payload.get("action")
            pr = payload.get("pull_request") or {}
            if action == "closed" and pr.get("merged"):
                return NormalizedEvent(
                    provider="github",
                    external_id=delivery_id,
                    kind="pr_merged",
                    action="merged",
                    pr_number=pr.get("number"),
                    head_sha=pr.get("merge_commit_sha"),
                    title=(pr.get("title") or "")[:200],
                )
            return None  # opened/synchronize/unmerged-close: reality unchanged

        if event_name == "issues":
            action = payload.get("action")
            issue = payload.get("issue") or {}
            if action == "closed":
                return NormalizedEvent(
                    provider="github",
                    external_id=delivery_id,
                    kind="issue_closed",
                    action="closed",
                    issue_number=issue.get("number"),
                    title=(issue.get("title") or "")[:200],
                )
            if action in ("edited", "reopened"):
                return NormalizedEvent(
                    provider="github",
                    external_id=delivery_id,
                    kind="push",  # unused: reingest_only events write no row
                    action=action,
                    issue_number=issue.get("number"),
                    title=(issue.get("title") or "")[:200],
                    reingest_only=True,
                )
            return None

        return None  # discussions, labels, stars, …

    # --- BeliefSource ----------------------------------------------------

    def iter_knowledge_docs(self, limit: int) -> Iterator[KnowledgeDoc]:
        """Issues and PRs, most recently updated first."""
        for item in self._paginate(
            f"/repos/{self.full_name}/issues",
            limit,
            state="all",
            sort="updated",
            direction="desc",
        ):
            kind = "pull_request" if "pull_request" in item else "issue"
            title = item.get("title") or ""
            body = (item.get("body") or "")[:_MAX_BODY_CHARS]
            state = item.get("state")
            yield KnowledgeDoc(
                external_id=str(item["number"]),
                kind=kind,
                title=title,
                body=body,
                url=item.get("html_url", ""),
                author=(item.get("user") or {}).get("login"),
                state=state,
                updated_at=_parse_dt(item.get("updated_at")),
                # State is part of the hash: closing an issue must trigger
                # re-processing even when title/body are unchanged.
                content_hash=sha256_text(f"{title}\n{body}\n{state}"),
            )

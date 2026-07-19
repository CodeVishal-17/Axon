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
    CommitInfo,
    KnowledgeDoc,
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
            headers=headers, timeout=60.0, follow_redirects=True
        )

    # --- internals -------------------------------------------------------

    def _get(self, path: str, **params: Any) -> httpx.Response:
        response = self._client.get(f"{_API}{path}", params=params or None)
        if response.status_code == 403 and response.headers.get(
            "x-ratelimit-remaining"
        ) == "0":
            raise AdapterError(
                f"GitHub rate limit exhausted (resets at "
                f"{response.headers.get('x-ratelimit-reset')}). "
                "Configure GITHUB_TOKEN for a 5000/hr limit."
            )
        if response.status_code == 404:
            raise AdapterError(
                f"GitHub returned 404 for {path} — repo missing or token lacks access"
            )
        if response.is_error:
            raise AdapterError(
                f"GitHub API error {response.status_code} on {path}: "
                f"{response.text[:200]}"
            )
        return response

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

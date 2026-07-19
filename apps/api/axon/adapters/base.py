"""Normalized adapter types — the boundary no provider detail may cross.

Two adapter roles (architecture §14):

* **Belief sources** yield :class:`KnowledgeDoc` — normalized documents that
  contain claims (docs, issues, PRs; later Notion pages, Slack threads).
* **Reality sources** expose the code itself: file tree, contents, commit
  history. GitHub is currently the only one, and implements both roles.

Everything downstream (ingestion, extraction, verification, the feed)
consumes ONLY these types. Adding Notion later = a new module implementing
:class:`BeliefSource`; nothing below the adapter layer changes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


class AdapterError(RuntimeError):
    """Raised for provider-side failures (auth, rate limit, missing repo)."""


@dataclass(frozen=True)
class RepoInfo:
    """Provider-side repository identity."""

    external_id: str
    full_name: str
    default_branch: str
    head_sha: str | None


@dataclass(frozen=True)
class RepoFile:
    """One regular file from the repository snapshot."""

    path: str  # repo-relative, forward slashes
    content: bytes


@dataclass(frozen=True)
class KnowledgeDoc:
    """A normalized belief-source document.

    ``content_hash`` is computed by the adapter over the fields whose change
    should trigger re-processing — unchanged hash means ingestion (and later
    claim extraction) may skip the document entirely.
    """

    external_id: str
    kind: str  # "issue" | "pull_request" (repo markdown docs derive from files)
    title: str
    body: str
    url: str
    author: str | None
    state: str | None
    updated_at: datetime | None
    content_hash: str


@dataclass(frozen=True)
class CommitInfo:
    """One commit with the files it touched — raw material for ownership."""

    sha: str
    author_login: str | None
    authored_at: datetime | None
    files: tuple[str, ...]


class BeliefSource(Protocol):
    """Enumerate the provider's claim-bearing documents."""

    def iter_knowledge_docs(self, limit: int) -> Iterator[KnowledgeDoc]: ...


class RealitySource(Protocol):
    """Expose the code and its history."""

    def fetch_repo_info(self) -> RepoInfo: ...

    def iter_files(self, max_file_bytes: int) -> Iterator[RepoFile]: ...

    def iter_commits(self, limit: int) -> Iterator[CommitInfo]: ...

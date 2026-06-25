"""Thin interface over Qdrant. The ONLY module that imports qdrant_client."""



from __future__ import annotations
import tempfile
from pathlib import Path

from .config import settings
from .vectorstore import VectorStore

def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    """Split a document into chunks of roughly `max_chars`, on paragraph breaks.

    We split on blank lines (paragraphs) and then greedily pack paragraphs
    together until adding the next one would exceed max_chars. This keeps
    related sentences together while bounding chunk size.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def ingest_git(store: VectorStore, repo_url: str) -> int:
    from git import Repo
    if not settings.git_runbook_url:
        print("No git runbook URL configured, skipping git ingestion.")
        return

def ingest_notion():
    """Ingests all pages in the configured Notion workspace. Requires `notion_token`"""

def ingest_files(store: VectorStore, directory: Path | None = None) -> int:
    """Ingests all .md files in the configured local runbook path (and subdirs)."""
    if not settings.local_runbook_path:
        print("No local path is set, skipping local file ingestion.")
        return 0
    directory = directory or Path(settings.local_runbook_path)
    total = 0
    for file in directory.glob("**/*.md"):
        text = file.read_text(encoding="utf-8", errors="ignore")
        chunks = chunk_text(text)
        total += store.upsert(chunks, source=str(file))
    return total

def ingest_all() -> int: 

    store = VectorStore()
    store.ensure_collection()

    total = 0
    if settings.git_runbook_url:
        total += ingest_git(store, settings.git_runbook_url)
    if settings.notion_token:
        total += ingest_notion(store)
    if settings.local_runbook_path:
        total += ingest_files(store)
    
    print(f"Ingestion complete: {total} chunks upserted.")
    return total
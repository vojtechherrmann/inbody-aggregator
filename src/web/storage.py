"""Raw-file storage: Azure Blob via managed identity, or a local directory (dev + tests).

Blob holds the uploaded raw files only, under `raw/<user_id>/<yyyy-mm>/<uuid>-<name>` in a
PRIVATE container, one blob per user + SHA-256. Nothing is ever hard-deleted: when no live upload
references a file it MOVES to `archive/...` (same layout), and back to `raw/` when the same file is
uploaded again (pipeline._sync_blob). `list_raw` / rebuild-index ignore `archive/`, so deleted
data never comes back by itself.
Nothing is ever exposed by blob URL: files are streamed through the app after
the `can_view` check (see app.py `/files/{upload_id}`).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import config

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_name(name: str) -> str:
    base = Path(name or "file").name  # drops any client-supplied directories
    cleaned = _SAFE.sub("_", base).strip("._") or "file"
    return cleaned[-80:]


def blob_path(user_id: str, upload_id: str, original_name: str, now: datetime) -> str:
    return f"raw/{user_id}/{now:%Y-%m}/{upload_id}-{sanitize_name(original_name)}"


def archive_path(path: str) -> str:
    """raw/<user>/<yyyy-mm>/<file> -> archive/<user>/<yyyy-mm>/<file> (and nothing else)."""
    if path.startswith("archive/"):
        return path
    if not path.startswith("raw/"):
        raise ValueError(f"not a raw path: {path}")
    return "archive/" + path[len("raw/") :]


def raw_path(path: str) -> str:
    return path if path.startswith("raw/") else "raw/" + path[len("archive/") :]


def move(store, src: str, dst: str) -> None:
    """Copy -> verify (SHA-256 of what landed) -> delete the source. Idempotent: a move that was
    interrupted after the copy, or already done, completes / does nothing. Never a hard delete of
    the only copy: the source goes only after the destination is verified."""
    import hashlib

    if src == dst:
        return
    if not store.exists(src):
        if store.exists(dst):
            return  # already moved
        raise FileNotFoundError(src)
    data = store.get(src)
    store.put(dst, data, "application/octet-stream", overwrite=True)
    if hashlib.sha256(store.get(dst)).digest() != hashlib.sha256(data).digest():
        raise OSError(f"verification of {dst} failed; {src} kept")
    store.delete(src)


class LocalStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _path(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if self.root not in full.parents:
            raise ValueError("path escapes the storage root")
        return full

    def put(self, path: str, data: bytes, content_type: str, overwrite: bool = False) -> None:
        full = self._path(path)
        if full.exists() and not overwrite:
            raise FileExistsError(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def get(self, path: str) -> bytes:
        return self._path(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._path(path).is_file()

    def delete(self, path: str) -> None:
        self._path(path).unlink(missing_ok=True)

    def list_raw(self):
        """(path, created naive-UTC) of every raw file -- for rebuilding the index."""
        from datetime import UTC, datetime

        base = self.root / "raw"
        if not base.exists():
            return
        for f in sorted(base.rglob("*")):
            if f.is_file():
                created = datetime.fromtimestamp(f.stat().st_mtime, UTC).replace(tzinfo=None)
                yield f.relative_to(self.root).as_posix(), created


class AzureStore:
    def __init__(self, account: str, container: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(
            f"https://{account}.blob.core.windows.net",
            credential=DefaultAzureCredential(exclude_interactive_browser_credential=True),
        )
        self.container = service.get_container_client(container)

    def put(self, path: str, data: bytes, content_type: str, overwrite: bool = False) -> None:
        from azure.storage.blob import ContentSettings

        self.container.upload_blob(
            path,
            data,
            overwrite=overwrite,
            content_settings=ContentSettings(content_type=content_type),
        )

    def get(self, path: str) -> bytes:
        return self.container.download_blob(path).readall()

    def exists(self, path: str) -> bool:
        return self.container.get_blob_client(path).exists()

    def delete(self, path: str) -> None:
        self.container.delete_blob(path)

    def list_raw(self):
        for blob in self.container.list_blobs(name_starts_with="raw/"):
            created = blob.creation_time or blob.last_modified
            yield blob.name, created.replace(tzinfo=None)


_store = None


def store():
    global _store
    if _store is None:
        if config.STORAGE_BACKEND == "azure":
            _store = AzureStore(config.AZURE_STORAGE_ACCOUNT, config.AZURE_CONTAINER_NAME)
        else:
            _store = LocalStore(config.LOCAL_STORAGE_DIR)
    return _store


def set_store(new) -> None:
    global _store
    _store = new

"""Private, content-agnostic storage for original uploaded documents.

The RAG index stores extracted text, which is the right representation for
retrieval but the wrong representation for a faithful document preview.  This
module keeps the original bytes under opaque, hash-derived paths.  Callers must
perform authorization before exposing a returned path.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .config import RAGConfig, get_config


@dataclass(frozen=True)
class StoredOriginal:
    path: Path
    filename: str
    content_type: str
    size: int


class OriginalStore:
    def __init__(self, config: RAGConfig | None = None) -> None:
        self._root = (config or get_config()).paths.originals_dir

    @staticmethod
    def _opaque(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _path(self, tenant_id: str, doc_id: str) -> Path:
        return self._root / self._opaque(tenant_id) / self._opaque(doc_id) / "source.bin"

    def save(
        self,
        tenant_id: str,
        doc_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> StoredOriginal:
        path = self._path(tenant_id, doc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return StoredOriginal(
            path=path,
            filename=Path(filename).name or doc_id,
            content_type=content_type or "application/octet-stream",
            size=len(data),
        )

    def get(
        self,
        tenant_id: str,
        doc_id: str,
        filename: str,
        content_type: str,
        size: int = 0,
    ) -> StoredOriginal | None:
        path = self._path(tenant_id, doc_id)
        if not path.is_file():
            return None
        return StoredOriginal(
            path=path,
            filename=Path(filename).name or doc_id,
            content_type=content_type or "application/octet-stream",
            size=size or path.stat().st_size,
        )

    def delete(self, tenant_id: str, doc_id: str) -> None:
        """Remove the binary for a tombstoned document.

        The indexed tombstone and audit record remain; only the potentially
        sensitive source bytes are removed.
        """
        path = self._path(tenant_id, doc_id)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
            path.parent.parent.rmdir()
        except OSError:
            # Shared tenant directories and non-empty directories are expected.
            pass

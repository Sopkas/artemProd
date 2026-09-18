"""Uploaded files on the local disk: ``<root>/<run_id>/<uuid>.xlsx`` (S2-03).

The relative path is what the repository stores. Every resolved path is checked to stay
inside the root even through symlinks, as agreed in docs/analysis-storage.md.
"""

import re
from pathlib import Path
from uuid import uuid4

from claims_assistant.application.check_package import StorageError

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def save(self, run_id: str, data: bytes) -> str:
        if not _RUN_ID.fullmatch(run_id):
            raise StorageError("Unsafe run identifier")
        relative = f"{run_id}/{uuid4().hex}.xlsx"
        target = self._inside_root(relative)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._inside_root(relative)  # re-check after the directory exists (symlinks)
            target.write_bytes(data)
        except OSError as error:
            raise StorageError("Не удалось сохранить файл.") from error
        return relative

    def read(self, stored_path: str) -> bytes:
        target = self._inside_root(stored_path)
        try:
            return target.read_bytes()
        except OSError as error:
            raise StorageError("Файл пакета не найден в хранилище.") from error

    def remove(self, stored_path: str) -> None:
        target = self._inside_root(stored_path)
        try:
            target.unlink(missing_ok=True)
        except OSError as error:
            raise StorageError("Не удалось удалить файл.") from error

    def _inside_root(self, relative: str) -> Path:
        root = self._root.resolve()
        candidate = (self._root / relative).resolve()
        if candidate == root or root not in candidate.parents:
            raise StorageError("Path escapes the storage root")
        return candidate

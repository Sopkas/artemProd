import os

import pytest

from claims_assistant.application.check_package import StorageError
from claims_assistant.infrastructure.storage.local import LocalFileStorage


def test_save_writes_under_run_directory_and_returns_relative_path(tmp_path):
    storage = LocalFileStorage(tmp_path / "uploads")
    stored_path = storage.save("run1", b"abc")
    assert stored_path.startswith("run1/") and stored_path.endswith(".xlsx")
    assert (tmp_path / "uploads" / stored_path).read_bytes() == b"abc"


def test_two_saves_never_collide(tmp_path):
    storage = LocalFileStorage(tmp_path / "uploads")
    assert storage.save("run1", b"a") != storage.save("run1", b"a")


def test_remove_deletes_only_inside_the_root(tmp_path):
    storage = LocalFileStorage(tmp_path / "uploads")
    stored_path = storage.save("run1", b"abc")
    storage.remove(stored_path)
    assert not (tmp_path / "uploads" / stored_path).exists()
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    with pytest.raises(StorageError):
        storage.remove("../secret.txt")
    assert outside.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlinked_run_directory_escaping_the_root_is_rejected(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "run1").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(StorageError):
        LocalFileStorage(root).save("run1", b"abc")


def test_run_id_is_validated(tmp_path):
    storage = LocalFileStorage(tmp_path / "uploads")
    for bad in ("", "..", "a/b", "a\\b", "C:"):
        with pytest.raises(StorageError):
            storage.save(bad, b"abc")

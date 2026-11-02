import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from vela.storage import Storage, StorageError


def test_mount_marker_missing_fails_without_creating_target(tmp_path):
    target = tmp_path / "absent-nas"
    source = tmp_path / "media.mp4"
    source.write_bytes(b"video")
    with pytest.raises(StorageError) as error:
        Storage(target, reserve_bytes=0).publish(str(uuid4()), source)
    assert error.value.category == "storage_offline"
    assert not target.exists()


def make_storage(tmp_path):
    target = tmp_path / "nas"
    target.mkdir()
    (target / ".vela-volume").write_text("volume-123")
    return Storage(target, marker=".vela-volume", marker_value="volume-123", reserve_bytes=0)


def test_publication_is_verified_and_repeatable(tmp_path):
    store = make_storage(tmp_path)
    source = tmp_path / "media.mp4"
    content = b"test media\0" * 1024
    source.write_bytes(content)
    job_id = str(uuid4())
    first = store.publish(job_id, source, title="A/B: Video")
    assert first == store.publish(job_id, source, title="A/B: Video")
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert (store.root / first.relative_path).read_bytes() == content
    assert not list(store.root.rglob("*.tmp"))
    assert source.exists()  # DB commit owns eventual local cleanup.
    source.write_bytes(b"different")
    with pytest.raises(StorageError) as error:
        store.publish(job_id, source, title="A/B: Video")
    assert error.value.category == "conflict"


def test_crash_after_rename_can_reconcile(tmp_path, monkeypatch):
    store = make_storage(tmp_path)
    source = tmp_path / "media.mkv"
    source.write_bytes(b"complete file")
    import vela.storage as storage_module
    replace = storage_module.os.replace

    def crash_after_replace(a, b):
        replace(a, b)
        raise OSError("simulated crash after rename")

    monkeypatch.setattr(storage_module.os, "replace", crash_after_replace)
    job_id = str(uuid4())
    with pytest.raises(StorageError):
        store.publish(job_id, source, title="Video")
    monkeypatch.setattr(storage_module.os, "replace", replace)
    result = store.publish(job_id, source, title="Video")
    assert (store.root / result.relative_path).read_bytes() == b"complete file"


def test_wrong_mount_identity_prevents_copy(tmp_path):
    store = make_storage(tmp_path)
    (store.root / ".vela-volume").write_text("different-volume")
    source = tmp_path / "media.mp4"
    source.write_bytes(b"video")
    with pytest.raises(StorageError):
        store.publish(str(uuid4()), source)
    assert [item.name for item in store.root.iterdir()] == [".vela-volume"]

from __future__ import annotations

import hashlib
import json
from io import BytesIO

import pytest

import updater


def test_version_key_accepts_release_tag() -> None:
    assert updater._version_key("v1.12.3") > updater._version_key("0.9.9")


def test_download_latest_update_verifies_sha256(tmp_path, monkeypatch) -> None:
    archive_bytes = b"portable-update-content"
    release = {
        "tag_name": "v0.1.3",
        "assets": [
            {"name": updater.UPDATE_MANIFEST, "browser_download_url": "manifest"},
            {
                "name": updater.UPDATE_ARCHIVE,
                "browser_download_url": "archive",
                "size": len(archive_bytes),
            },
        ],
    }
    manifest = {
        "version": "0.1.3",
        "archive": updater.UPDATE_ARCHIVE,
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
    }
    responses = {
        updater.RELEASES_API: json.dumps(release).encode(),
        "manifest": json.dumps(manifest).encode(),
        "archive": archive_bytes,
    }
    monkeypatch.setattr(
        updater,
        "_request",
        lambda url, **kwargs: BytesIO(responses[url]),
    )
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))

    archive_path, version = updater.download_latest_update()

    assert version == "0.1.3"
    assert (tmp_path / updater.UPDATE_ARCHIVE).read_bytes() == archive_bytes


def test_download_latest_update_rejects_bad_checksum(tmp_path, monkeypatch) -> None:
    archive_bytes = b"not-the-published-update"
    release = {
        "tag_name": "v0.1.3",
        "assets": [
            {"name": updater.UPDATE_MANIFEST, "browser_download_url": "manifest"},
            {
                "name": updater.UPDATE_ARCHIVE,
                "browser_download_url": "archive",
                "size": len(archive_bytes),
            },
        ],
    }
    manifest = {
        "version": "0.1.3",
        "archive": updater.UPDATE_ARCHIVE,
        "sha256": "0" * 64,
    }
    responses = {
        updater.RELEASES_API: json.dumps(release).encode(),
        "manifest": json.dumps(manifest).encode(),
        "archive": archive_bytes,
    }
    monkeypatch.setattr(updater, "_request", lambda url, **kwargs: BytesIO(responses[url]))
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))

    with pytest.raises(ValueError, match="SHA-256"):
        updater.download_latest_update()

    assert not (tmp_path / updater.UPDATE_ARCHIVE).exists()

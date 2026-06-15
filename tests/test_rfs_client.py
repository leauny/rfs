"""Tests for the rfs.py CLI client streaming upload/download against a real server."""

import hashlib
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import rfs  # noqa: E402


def _md5_bytes(b):
    return hashlib.md5(b).hexdigest()


def _md5_file(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def test_upload_round_trip(server, tmp_path):
    src = tmp_path / "src.bin"
    payload = bytes(range(256)) * 4096       # 1 MiB, full byte spectrum
    payload += b"\r\n--fakebound--\r\n" * 200
    src.write_bytes(payload)

    progress_log = []

    def on_progress(uploaded, total):
        progress_log.append((uploaded, total))

    fname, local_md5, server_md5 = rfs._upload_with_progress(
        server.url, None, str(src), "/", on_progress
    )

    assert fname == "src.bin"
    assert local_md5 == _md5_bytes(payload)
    assert server_md5 == local_md5
    # Progress callback fires at least once per 1 MiB chunk + final flush
    assert progress_log, "progress callback was never invoked"
    assert progress_log[-1][0] == len(payload)
    assert progress_log[-1][1] == len(payload)
    # Server-side file matches
    saved = server.server_path("src.bin").read_bytes()
    assert saved == payload


def test_upload_remote_name_override(server, tmp_path):
    src = tmp_path / "local-name.bin"
    src.write_bytes(b"hello")
    fname, *_ = rfs._upload_with_progress(
        server.url, None, str(src), "/", lambda *_: None,
        remote_name="renamed-on-server.bin",
    )
    assert fname == "renamed-on-server.bin"
    assert server.server_path("renamed-on-server.bin").read_bytes() == b"hello"
    assert not server.server_path("local-name.bin").exists()


def test_upload_md5_mismatch_detection(server, tmp_path, monkeypatch):
    """If server reports a different MD5 we must raise ValueError."""
    src = tmp_path / "mm.bin"
    src.write_bytes(b"data" * 100)

    real_post = rfs.requests.post

    def lying_post(*args, **kwargs):
        resp = real_post(*args, **kwargs)
        # Force a fake header by wrapping the headers dict
        resp.headers["X-Content-MD5"] = "deadbeef" * 4
        return resp

    monkeypatch.setattr(rfs.requests, "post", lying_post)
    with pytest.raises(ValueError, match="MD5 mismatch"):
        rfs._upload_with_progress(
            server.url, None, str(src), "/", lambda *_: None
        )


def test_download_round_trip(server, tmp_path):
    payload = b"download me " * 4096
    server.server_path("remote.bin").write_bytes(payload)

    dest = tmp_path / "got.bin"
    rfs._download_with_progress(
        server.url, None, "/remote.bin", str(dest), lambda *_: None
    )
    assert dest.read_bytes() == payload
    assert _md5_file(dest) == _md5_bytes(payload)


def test_list_remote_dir_via_json(server, tmp_path):
    server.server_path("a.txt").write_bytes(b"x")
    server.server_path("sub").mkdir()
    entries = rfs._list_remote_dir(server.url, None, "")
    names = {href for href, _ in entries}
    assert "a.txt" in names
    assert "sub/" in names


def test_list_remote_dir_html_fallback_handles_parent_crumb(server, tmp_path, monkeypatch):
    """Even if JSON fails, the HTML regex must skip the new ↑ Parent crumb."""
    server.server_path("sub").mkdir()
    server.server_path("sub", "x.bin").write_bytes(b"x")

    real_get = rfs.requests.get

    def fail_json(url, *args, **kwargs):
        # Make /_api/ls look like a 500 so the fallback kicks in.
        if "/_api/ls" in url:
            class FakeResp:
                status_code = 500
                text = "boom"
                def raise_for_status(self):
                    raise rfs.requests.RequestException("forced")
                def json(self):
                    raise ValueError("forced")
            return FakeResp()
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(rfs.requests, "get", fail_json)
    entries = rfs._list_remote_dir(server.url, None, "sub")
    names = [name for _, name in entries]
    # Must include the file but NOT the parent crumb text
    assert any(n == "x.bin" for n in names)
    assert not any("↑" in n for n in names)
    assert "Parent" not in names


def test_streaming_upload_keeps_client_memory_bounded(server, tmp_path):
    """Client peak memory must NOT scale with file size.

    Uses tracemalloc to measure allocations during the upload of a 32 MiB
    file. We expect peak well under the file size.
    """
    import tracemalloc

    size = 32 * 1024 * 1024
    src = tmp_path / "big.bin"
    with open(src, "wb") as f:
        chunk = bytes(range(256)) * 4096   # 1 MiB
        for _ in range(size // len(chunk)):
            f.write(chunk)

    tracemalloc.start()
    try:
        rfs._upload_with_progress(
            server.url, None, str(src), "/", lambda *_: None
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Allow up to 8 MiB peak Python allocations; comfortably below 32 MiB.
    # `requests` + urllib3 internal buffers are within this budget.
    assert peak < 8 * 1024 * 1024, (
        "client tracemalloc peak %d B exceeds 8 MiB — body may not be streaming" % peak
    )

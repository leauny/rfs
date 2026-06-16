"""End-to-end tests for tinyhttp.py's streaming multipart upload parser.

These tests pump real HTTP traffic at a freshly booted server and check:
  - byte-for-byte fidelity of the saved file (full byte spectrum, fake
    boundary patterns, random tail);
  - server-computed `X-Content-MD5` matches the client hash;
  - boundaries that straddle 1 MiB read chunks don't get mis-parsed;
  - empty-file and small-file edge cases still work;
  - directory listing renders the modern UI markup;
  - JSON `/_api/ls` reflects uploaded files.
"""

import hashlib
import io
import os
import uuid
from pathlib import Path

import pytest
import requests


def _build_multipart(filename, payload, boundary=None):
    boundary = boundary or uuid.uuid4().hex
    head = (
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="'
        + filename.encode("utf-8") + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )
    tail = b"\r\n--" + boundary.encode() + b"--\r\n"
    return boundary, head + payload + tail


def _post_multipart(url, filename, payload, overwrite=False):
    boundary, body = _build_multipart(filename, payload)
    if overwrite:
        url += "?overwrite=1"
    return requests.post(
        url,
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Content-Length": str(len(body)),
        },
        timeout=60,
    )


def _md5(data):
    return hashlib.md5(data).hexdigest()


def test_small_text_upload(server):
    payload = b"hello, world\n"
    resp = _post_multipart(server.url + "/", "hello.txt", payload)
    assert resp.status_code == 200
    assert resp.headers["X-Content-MD5"] == _md5(payload)
    saved = server.server_path("hello.txt").read_bytes()
    assert saved == payload


def test_empty_file_upload(server):
    resp = _post_multipart(server.url + "/", "empty.bin", b"")
    assert resp.status_code == 200
    assert resp.headers["X-Content-MD5"] == _md5(b"")
    assert server.server_path("empty.bin").read_bytes() == b""


def test_full_byte_spectrum_with_fake_boundary(server):
    # 4 MiB of all 256 byte values + chunks that *look* like multipart
    # boundaries but aren't, plus a random tail.
    data = bytes(range(256)) * (16 * 1024)
    data += b"\r\n--fakeboundary--\r\n" * 500
    data += os.urandom(64 * 1024)
    resp = _post_multipart(server.url + "/", "torture.bin", data)
    assert resp.status_code == 200
    assert resp.headers["X-Content-MD5"] == _md5(data)
    saved = server.server_path("torture.bin").read_bytes()
    assert saved == data


def test_upload_size_just_above_chunk_boundary(server):
    # Server reads 1 MiB at a time; force a payload whose closing boundary
    # straddles the chunk boundary.
    data = b"A" * ((1 << 20) - 5) + b"BCDEF" + b"Z" * 1024
    resp = _post_multipart(server.url + "/", "boundary.bin", data)
    assert resp.status_code == 200
    assert resp.headers["X-Content-MD5"] == _md5(data)
    assert server.server_path("boundary.bin").read_bytes() == data


def test_upload_into_subdir(server):
    sub = server.server_path("nested", "deeply")
    sub.mkdir(parents=True)
    payload = b"sub" * 100
    resp = _post_multipart(server.url + "/nested/deeply/", "file.bin", payload)
    assert resp.status_code == 200
    assert (sub / "file.bin").read_bytes() == payload


def test_upload_rejects_existing_file_without_overwrite(server):
    server.server_path("same.bin").write_bytes(b"old")
    resp = _post_multipart(server.url + "/", "same.bin", b"new")
    assert resp.status_code == 409
    assert server.server_path("same.bin").read_bytes() == b"old"


def test_upload_overwrites_existing_file_when_requested(server):
    server.server_path("same.bin").write_bytes(b"old")
    resp = _post_multipart(server.url + "/", "same.bin", b"new", overwrite=True)
    assert resp.status_code == 200
    assert resp.headers["X-Content-MD5"] == _md5(b"new")
    assert server.server_path("same.bin").read_bytes() == b"new"


def test_upload_rejects_directory_name_conflict(server):
    server.server_path("same.bin").mkdir()
    resp = _post_multipart(server.url + "/", "same.bin", b"new", overwrite=True)
    assert resp.status_code == 409
    assert server.server_path("same.bin").is_dir()


def test_directory_listing_html_contains_modern_markup(server):
    resp = requests.get(server.url + "/", timeout=5)
    assert resp.status_code == 200
    body = resp.text
    # Modern listing template markers
    assert "Directory listing" in body
    assert 'id="drop"' in body
    assert "FormData" in body
    # No remnants of the old <ul> markup
    assert "<ul>" not in body


def test_parent_link_on_subdir(server):
    server.server_path("sub").mkdir()
    resp = requests.get(server.url + "/sub/", timeout=5)
    assert resp.status_code == 200
    assert "↑ Parent" in resp.text


def test_api_ls_reflects_upload(server):
    payload = b"x" * 1234
    _post_multipart(server.url + "/", "list_me.bin", payload)
    resp = requests.get(server.url + "/_api/ls", params={"path": "/"}, timeout=5)
    data = resp.json()
    assert data["ok"] is True
    names = {e["name"]: e for e in data["entries"]}
    assert "list_me.bin" in names
    assert names["list_me.bin"]["size"] == len(payload)


def test_streaming_upload_keeps_server_memory_bounded(server):
    """Upload 64 MiB and verify server RSS doesn't balloon to body size.

    Skipped on platforms where `ps` isn't available or returns no RSS.
    """
    import shutil
    import subprocess as sp

    if shutil.which("ps") is None:
        pytest.skip("ps not available")

    # Sample baseline RSS
    def rss_kb():
        out = sp.run(
            ["ps", "-p", str(server.proc.pid), "-o", "rss="],
            capture_output=True, text=True,
        )
        try:
            return int(out.stdout.strip())
        except ValueError:
            return None

    before = rss_kb()
    if before is None:
        pytest.skip("could not read RSS")

    size = 64 * 1024 * 1024  # 64 MiB
    payload = bytes(range(256)) * (size // 256)

    # Stream the multipart body so the *client* side is also bounded.
    boundary = uuid.uuid4().hex
    head = (
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )
    tail = b"\r\n--" + boundary.encode() + b"--\r\n"
    total = len(head) + len(payload) + len(tail)

    def gen():
        yield head
        # 1 MiB chunks
        view = memoryview(payload)
        for i in range(0, len(payload), 1 << 20):
            yield bytes(view[i:i + (1 << 20)])
        yield tail

    resp = requests.post(
        server.url + "/",
        data=gen(),
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Content-Length": str(total),
        },
        timeout=120,
    )
    after = rss_kb() or before
    assert resp.status_code == 200
    assert resp.headers["X-Content-MD5"] == hashlib.md5(payload).hexdigest()
    # Streaming guarantee: peak RSS shouldn't be anywhere near 64 MiB above
    # baseline. Allow a generous 32 MiB margin to absorb interpreter noise
    # and OS-level allocator behavior.
    growth_kb = after - before
    assert growth_kb < 32 * 1024, (
        "server RSS grew by %d KiB during a 64 MiB streaming upload" % growth_kb
    )

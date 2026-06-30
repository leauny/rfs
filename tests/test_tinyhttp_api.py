"""End-to-end tests for tinyhttp.py's API endpoints and ancillary handlers.

Covers JSON endpoints (ls, mkdir, rename, restore, stat, trash), DELETE
soft-delete behavior, and download integrity with `X-Content-MD5`.
"""

import hashlib
import threading
import time
from pathlib import Path

import requests


def _md5(b):
    return hashlib.md5(b).hexdigest()


def test_ls_root_empty(server):
    resp = requests.get(server.url + "/_api/ls", params={"path": "/"})
    data = resp.json()
    assert data["ok"] is True
    assert data["entries"] == []


def test_ls_hidden_filtering(server):
    server.server_path(".hidden").write_bytes(b"x")
    server.server_path("visible").write_bytes(b"y")
    server.server_path(".Trash").mkdir()      # always hidden

    default = requests.get(server.url + "/_api/ls", params={"path": "/"}).json()
    names = [e["name"] for e in default["entries"]]
    assert "visible" in names
    assert ".hidden" not in names
    assert ".Trash" not in names

    shown = requests.get(
        server.url + "/_api/ls", params={"path": "/", "show_hidden": "1"}
    ).json()
    names = [e["name"] for e in shown["entries"]]
    assert ".hidden" in names
    assert ".Trash" not in names              # never shown, regardless of flag


def test_mkdir_then_ls(server):
    resp = requests.post(
        server.url + "/_api/mkdir", json={"path": "/new-dir"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert (server.server_path("new-dir")).is_dir()


def test_mkdir_conflict(server):
    server.server_path("exists").mkdir()
    resp = requests.post(
        server.url + "/_api/mkdir", json={"path": "/exists"}
    )
    assert resp.status_code == 409
    assert resp.json()["ok"] is False


def test_mkdir_rejects_unsafe_path_segments(server):
    for path in ("/../escape", "/safe/./name", "/safe/../../escape", "\\escape"):
        resp = requests.post(server.url + "/_api/mkdir", json={"path": path})
        assert resp.status_code == 400
        data = resp.json()
        assert data["ok"] is False
        assert data["error"] == "Invalid directory path"
    assert not server.server_path("escape").exists()
    assert not server.server_path("safe").exists()


def test_rename(server):
    server.server_path("a.txt").write_bytes(b"A")
    resp = requests.post(
        server.url + "/_api/rename",
        json={"from": "/a.txt", "to": "/b.txt"},
    )
    assert resp.status_code == 200
    assert not server.server_path("a.txt").exists()
    assert server.server_path("b.txt").read_bytes() == b"A"


def test_rename_target_exists(server):
    server.server_path("a.txt").write_bytes(b"A")
    server.server_path("b.txt").write_bytes(b"B")
    resp = requests.post(
        server.url + "/_api/rename",
        json={"from": "/a.txt", "to": "/b.txt"},
    )
    assert resp.status_code == 409


def test_delete_then_restore(server):
    f = server.server_path("doomed.txt")
    f.write_bytes(b"will be trashed")
    resp = requests.delete(server.url + "/doomed.txt")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    trashed = payload["entry"]["trashed"]
    # File moved into .Trash/, original gone
    assert not f.exists()
    assert (server.root / ".Trash" / trashed).exists()
    # /_api/trash lists it
    listing = requests.get(server.url + "/_api/trash", params={"dir": "/"}).json()
    assert any(e["trashed"] == trashed for e in listing["entries"])
    # Restore returns OK and clears the entry from the manifest. (The exact
    # restored path depends on the realpath-vs-translate_path logic in the
    # server which we don't dictate here.)
    rest = requests.post(
        server.url + "/_api/restore",
        json={"dir": "/", "trashed": trashed},
    )
    assert rest.status_code == 200
    assert rest.json()["ok"] is True
    # Trash file should be gone from .Trash/ and from the manifest
    assert not (server.root / ".Trash" / trashed).exists()
    listing_after = requests.get(server.url + "/_api/trash", params={"dir": "/"}).json()
    assert not any(e["trashed"] == trashed for e in listing_after["entries"])


def test_stat_endpoint(server):
    server.server_path("info.bin").write_bytes(b"abc")
    resp = requests.get(server.url + "/_api/stat", params={"path": "/info.bin"})
    data = resp.json()
    assert data["ok"] is True
    assert data["stat"]["size"] == 3
    assert data["stat"]["type"] == "file"


def test_download_md5_header(server):
    payload = b"download me " * 1024  # 12 KiB
    server.server_path("dl.bin").write_bytes(payload)
    resp = requests.get(server.url + "/dl.bin")
    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["X-Content-MD5"] == _md5(payload)


def test_path_traversal_blocked(server):
    # ../ should not escape the serve root
    parent_marker = server.root.parent / "should_not_appear.txt"
    if parent_marker.exists():
        parent_marker.unlink()
    resp = requests.delete(server.url + "/../should_not_appear.txt")
    assert resp.status_code in (403, 404)
    assert not parent_marker.exists()


# ─── /_api/tasks: running-task indicator ────────────────────────────────────

def _tasks(server):
    return requests.get(server.url + "/_api/tasks", timeout=10).json()


def test_tasks_empty_when_idle(server):
    data = _tasks(server)
    assert data["ok"] is True
    assert data["tasks"] == []


def test_upload_appears_in_tasks_while_running(server):
    filename = "big.bin"
    chunk = b"x" * (256 * 1024)              # 256 KiB
    chunks = 16                              # ~4 MiB total payload
    boundary = "testboundary123"
    head = (
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="'
        + filename.encode() + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )
    tail = b"\r\n--" + boundary.encode() + b"--\r\n"

    def body():
        yield head
        for _ in range(chunks):
            time.sleep(0.08)                # slow producer → server stays mid-upload
            yield chunk
        yield tail

    total_len = len(head) + chunks * len(chunk) + len(tail)
    result = {}

    def do_upload():
        result["resp"] = requests.post(
            server.url + "/",
            data=body(),
            headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Content-Length": str(total_len),
            },
            timeout=60,
        )

    t = threading.Thread(target=do_upload)
    t.start()

    # Poll until the upload task surfaces (or give up).
    seen = None
    deadline = time.time() + 10
    while time.time() < deadline:
        tasks = _tasks(server).get("tasks", [])
        if tasks:
            seen = tasks
            break
        time.sleep(0.1)

    assert seen is not None, "upload task never appeared in /_api/tasks"
    assert any(
        tk.get("type") == "upload" and tk.get("filename") == filename for tk in seen
    ), seen

    t.join(timeout=60)
    assert not t.is_alive()
    assert result["resp"].status_code == 200

    # Once finished, the task must be gone (no history kept).
    assert _tasks(server)["tasks"] == []


def test_download_appears_in_tasks_while_running(server):
    payload = b"y" * (4 * 1024 * 1024)      # 4 MiB
    server.server_path("dl_big.bin").write_bytes(payload)

    result = {}
    t = threading.Thread(target=lambda: result.__setitem__(
        "resp", requests.get(server.url + "/dl_big.bin", stream=True, timeout=60)))
    t.start()

    seen = None
    deadline = time.time() + 10
    while time.time() < deadline:
        tasks = _tasks(server).get("tasks", [])
        if tasks:
            seen = tasks
            break
        time.sleep(0.1)

    assert seen is not None, "download task never appeared in /_api/tasks"
    assert any(
        tk.get("type") == "download" and tk.get("filename") == "dl_big.bin"
        for tk in seen
    ), seen

    # Drain and finish the download so the task is cleared.
    resp = result["resp"]
    assert resp.status_code == 200
    assert resp.content == payload
    t.join(timeout=30)
    assert not t.is_alive()
    assert _tasks(server)["tasks"] == []


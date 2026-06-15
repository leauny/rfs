"""Shared fixtures: spin up a real tinyhttp.py server in a temp dir per test.

The server is launched as a subprocess so we exercise the *actual* request
handling code (including the streaming multipart parser), not an in-process
mock. Tests get back a `ServerCtx` carrying the base URL and the serve root.
"""

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TINYHTTP = REPO / "tinyhttp.py"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


@dataclass
class ServerCtx:
    url: str
    root: Path
    proc: subprocess.Popen
    port: int

    def server_path(self, *parts):
        return self.root.joinpath(*parts)


@pytest.fixture
def server(tmp_path_factory):
    """Boot tinyhttp.py serving from a dedicated temp dir, tear down after.

    Uses `tmp_path_factory` (not `tmp_path`) so the serve root is separate
    from the per-test `tmp_path`, leaving that available for client-side
    fixtures (source files to upload, download targets, etc.).
    """
    serve_root = tmp_path_factory.mktemp("rfs_serve")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(TINYHTTP), "-p", str(port), "-b", "127.0.0.1"],
        cwd=str(serve_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )
    try:
        if not _wait_ready(port):
            proc.kill()
            raise RuntimeError("tinyhttp.py failed to start on port %d" % port)
        yield ServerCtx(
            url="http://127.0.0.1:%d" % port,
            root=serve_root,
            proc=proc,
            port=port,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

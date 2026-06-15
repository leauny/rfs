"""rfs - Remote File Server CLI client."""

import hashlib
import mimetypes
import os
import re
import uuid

import click
import requests

DEFAULT_SERVER = "http://0.0.0.0:8002"
DEFAULT_PROXY = None


@click.group()
@click.option("--server", default=DEFAULT_SERVER, help="Server base URL.")
@click.option("--proxy", default=DEFAULT_PROXY, help="HTTP proxy address.")
@click.option("--no-proxy", is_flag=True, help="Disable proxy.")
@click.pass_context
def cli(ctx, server, proxy, no_proxy):
    """Remote File Server CLI client.

    Use ':' prefix to denote remote paths (like scp).

    \b
    Examples:
      rfs cp search.png :            # upload to remote /
      rfs cp search.png :/docker/    # upload to remote /docker/
      rfs cp :search.png .           # download to current dir
      rfs cp :/docker/f.yaml ./f.yaml
      rfs ls                         # list remote /
      rfs ls docker/                 # list remote /docker/
      rfs rm :/path/file.txt         # soft-delete remote file
      rfs mkdir :/new/dir            # create remote directory
      rfs mv :/old/name :/new/name   # rename remote file/dir
    """
    ctx.ensure_object(dict)
    ctx.obj["server"] = server.rstrip("/")
    ctx.obj["proxies"] = None if no_proxy else ({"http": proxy, "https": proxy} if proxy else None)


def _server_url(server, path):
    """Join a server base URL with an absolute path without dropping its prefix."""
    if not path.startswith("/"):
        path = "/" + path
    return server.rstrip("/") + path


@cli.command()
@click.argument("path", default="")
@click.pass_context
def ls(ctx, path):
    """List remote directory contents."""
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    path = path.lstrip(":")
    entries = _list_remote_dir(server, proxies, path)

    if not entries:
        click.echo("(empty)")
        return

    for href, name in entries:
        entry_type = "DIR " if href.endswith("/") else "FILE"
        click.echo(f"  {entry_type}  {name}")


def _list_remote_dir(server, proxies, path, show_hidden=False):
    """Return list of (href, display_name) tuples in a remote directory.

    Uses JSON API (/_api/ls) if available, falls back to HTML parsing.
    """
    # Normalize path
    path = path.strip("/")
    rel_path = "/" + path + "/" if path else "/"

    # Try JSON API first
    try:
        url = _server_url(server, "/_api/ls")
        params = {"path": rel_path}
        if show_hidden:
            params["show_hidden"] = "1"
        resp = requests.get(url, params=params, proxies=proxies, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                results = []
                for entry in data["entries"]:
                    name = entry["name"]
                    if entry["type"] == "dir":
                        results.append((name + "/", name + "/"))
                    else:
                        results.append((name, name))
                return results
    except (requests.RequestException, ValueError):
        pass

    # Fallback: HTML parsing
    url = _server_url(server, path)
    if path and not url.endswith("/"):
        url += "/"
    elif not path:
        url += "/"
    try:
        resp = requests.get(url, proxies=proxies, timeout=30)
        resp.raise_for_status()
        # Match table-name links and legacy <li><a> links, skip the
        # `class="parent"` breadcrumb introduced by the new listing UI.
        links = re.findall(
            r'<a(?![^>]*\bclass="parent")[^>]+href="([^"]+)"[^>]*>([^<]*)</a>',
            resp.text,
        )
        results = []
        for href, name in links:
            if href == "../" or name.strip() == "..":
                continue
            display = name.strip() or href
            # Strip a trailing "@" symlink marker the legacy listing appended.
            if display.endswith("@"):
                display = display[:-1]
            # Skip the "↑ Parent" crumb just in case the class filter missed it.
            if display.startswith("↑"):
                continue
            # .Trash always hidden; other dotfiles filtered by show_hidden
            if display.rstrip("/") == ".Trash":
                continue
            if display.startswith(".") and not show_hidden:
                continue
            results.append((href, display))
        return results
    except requests.RequestException:
        return []


def _md5_file(filepath):
    """Compute MD5 hex digest of a local file."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _list_remote_dir_json(server, proxies, path):
    """Return full JSON entries (with size/mtime) for a remote directory."""
    path = path.strip("/")
    rel_path = "/" + path + "/" if path else "/"
    try:
        url = _server_url(server, "/_api/ls")
        resp = requests.get(url, params={"path": rel_path}, proxies=proxies, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return data["entries"]
    except (requests.RequestException, ValueError):
        pass
    return []


def _remote_delete(server, proxies, remote_path):
    """Soft-delete a remote file."""
    url = _server_url(server, remote_path)
    resp = requests.delete(url, proxies=proxies, timeout=30)
    return resp


def _remote_mkdir(server, proxies, remote_path):
    """Create a remote directory."""
    url = _server_url(server, "/_api/mkdir")
    resp = requests.post(url, json={"path": remote_path}, proxies=proxies, timeout=30)
    return resp


def _remote_rename(server, proxies, from_path, to_path):
    """Rename/move a remote file or directory."""
    url = _server_url(server, "/_api/rename")
    resp = requests.post(url, json={"from": from_path, "to": to_path}, proxies=proxies, timeout=30)
    return resp


def _upload_with_progress(server, proxies, local_file, remote_dir, on_progress, remote_name=None):
    """Upload a local file with progress callback.

    on_progress(uploaded_bytes, total_bytes) — `uploaded_bytes` counts the
    raw file bytes pushed so far (multipart framing overhead is excluded so
    the percentage matches the file, not the wire body). `total_bytes` is
    the file size.

    Streams the multipart body directly from disk: peak memory is one read
    chunk (~1 MiB), independent of file size. Computes the local MD5 in the
    same pass and verifies against the server's `X-Content-MD5` response.

    remote_name: optional override for the uploaded filename.
    Returns (filename, local_md5, server_md5).
    Raises ValueError on MD5 mismatch.
    """
    remote_dir = remote_dir.rstrip("/") + "/"
    url = _server_url(server, remote_dir)
    filename = remote_name or os.path.basename(local_file)
    file_size = os.path.getsize(local_file)

    boundary = uuid.uuid4().hex
    bnd = boundary.encode()
    ctype = (mimetypes.guess_type(filename)[0] or "application/octet-stream").encode()
    safe_name = filename.encode("utf-8")
    head = (
        b"--" + bnd + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="'
        + safe_name + b'"\r\n'
        b"Content-Type: " + ctype + b"\r\n\r\n"
    )
    tail = b"\r\n--" + bnd + b"--\r\n"
    total_body = len(head) + file_size + len(tail)

    md5_hasher = hashlib.md5()
    sent_file_bytes = [0]

    def body_iter():
        yield head
        with open(local_file, "rb") as fp:
            while True:
                chunk = fp.read(1 << 20)        # 1 MiB
                if not chunk:
                    break
                md5_hasher.update(chunk)
                sent_file_bytes[0] += len(chunk)
                on_progress(sent_file_bytes[0], file_size)
                yield chunk
        yield tail

    headers = {
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(total_body),
    }

    resp = requests.post(
        url, data=body_iter(), headers=headers,
        proxies=proxies, timeout=600,
    )
    resp.raise_for_status()

    local_md5 = md5_hasher.hexdigest()
    server_md5 = resp.headers.get("X-Content-MD5", "")

    if server_md5 and server_md5 != local_md5:
        raise ValueError(
            f"MD5 mismatch for '{filename}': local={local_md5}, server={server_md5}. File may be corrupted on server."
        )

    return filename, local_md5, server_md5


def _download_with_progress(server, proxies, remote_path, local_path, on_progress):
    """Download a remote file with progress callback.

    on_progress(downloaded_bytes, total_bytes) is called during transfer.
    Returns (local_path, local_md5, server_md5).
    Raises ValueError on MD5 mismatch.
    """
    url = _server_url(server, remote_path)
    filename = os.path.basename(remote_path)

    if os.path.isdir(local_path):
        local_path = os.path.join(local_path, filename)

    resp = requests.get(url, proxies=proxies, timeout=600, stream=True)
    resp.raise_for_status()

    server_md5 = resp.headers.get("X-Content-MD5", "")
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
            on_progress(downloaded, total)

    local_md5 = _md5_file(local_path)

    if server_md5 and server_md5 != local_md5:
        raise ValueError(f"MD5 mismatch for download '{filename}': server={server_md5}, local={local_md5}")

    return local_path, local_md5, server_md5


def _upload(server, proxies, local_file, remote_dir, force):
    """Upload a local file to a remote directory (CLI version)."""
    remote_dir = remote_dir.rstrip("/") + "/"
    filename = os.path.basename(local_file)

    if not force:
        entries = _list_remote_dir(server, proxies, remote_dir.strip("/"))
        remote_files = {href.rstrip("/") for href, _ in entries}
        if filename in remote_files:
            if not click.confirm(f"Remote '{remote_dir}{filename}' already exists. Overwrite?"):
                click.echo("Cancelled.")
                return

    file_size = os.path.getsize(local_file)
    with click.progressbar(length=file_size, label=f"Uploading {filename}") as bar:
        last_reported = [0]

        def on_progress(uploaded, total):
            bar.update(uploaded - last_reported[0])
            last_reported[0] = uploaded

        _, local_md5, server_md5 = _upload_with_progress(server, proxies, local_file, remote_dir, on_progress)

    click.echo(f"Uploaded: {filename} -> {remote_dir}{filename}")
    if server_md5:
        click.echo(f"  MD5 verified: {local_md5}")


def _download(server, proxies, remote_path, local_path):
    """Download a remote file to a local path (CLI version)."""
    filename = os.path.basename(remote_path)

    if os.path.isdir(local_path):
        actual_path = os.path.join(local_path, filename)
    else:
        actual_path = local_path

    url = _server_url(server, remote_path)
    resp = requests.head(url, proxies=proxies, timeout=30)
    total = int(resp.headers.get("content-length", 0)) if resp.ok else 0

    with click.progressbar(length=total or None, label=f"Downloading {filename}") as bar:
        last_reported = [0]

        def on_progress(downloaded, total_bytes):
            bar.update(downloaded - last_reported[0])
            last_reported[0] = downloaded

        _, local_md5, server_md5 = _download_with_progress(server, proxies, remote_path, local_path, on_progress)

    click.echo(f"Downloaded: {remote_path} -> {actual_path}")
    if server_md5:
        click.echo(f"  MD5 verified: {local_md5}")


@cli.command()
@click.argument("src")
@click.argument("dst")
@click.option("-f", "--force", is_flag=True, help="Overwrite without confirmation.")
@click.pass_context
def cp(ctx, src, dst, force):
    """Copy files between local and remote.

    \b
    Use ':' prefix for remote paths:
      rfs cp local.txt :          # upload to /
      rfs cp local.txt :/dir/     # upload to /dir/
      rfs cp :remote.txt .        # download to cwd
      rfs cp :remote.txt ./a.txt  # download and rename
    """
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    src_remote = src.startswith(":")
    dst_remote = dst.startswith(":")

    if src_remote and dst_remote:
        raise click.UsageError("Cannot copy between two remote paths.")
    if not src_remote and not dst_remote:
        raise click.UsageError("One of src/dst must be a remote path (prefixed with ':').")

    if dst_remote:
        local_file = src
        if not os.path.isfile(local_file):
            raise click.BadParameter(f"Local file not found: {local_file}", param_hint="src")
        remote_dir = dst[1:] or "/"
        if not remote_dir.startswith("/"):
            remote_dir = "/" + remote_dir
        _upload(server, proxies, local_file, remote_dir, force)
    else:
        remote_path = src[1:]
        if not remote_path:
            raise click.UsageError("Remote source path cannot be empty.")
        local_path = dst or os.path.basename(remote_path)
        _download(server, proxies, remote_path, local_path)


@cli.command()
@click.argument("path")
@click.option("-f", "--force", is_flag=True, help="Skip confirmation.")
@click.pass_context
def rm(ctx, path, force):
    """Soft-delete a remote file (moves to .Trash).

    \b
    Examples:
      rfs rm :/file.txt
      rfs rm :/docker/config.yaml
    """
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    if not path.startswith(":"):
        raise click.UsageError("Remote path must be prefixed with ':'.")

    remote_path = path[1:]
    if not remote_path:
        raise click.UsageError("Remote path cannot be empty.")
    if not remote_path.startswith("/"):
        remote_path = "/" + remote_path

    if not force:
        if not click.confirm(f"Delete remote file '{remote_path}'?"):
            click.echo("Cancelled.")
            return

    resp = _remote_delete(server, proxies, remote_path)
    data = resp.json()
    if data.get("ok"):
        click.echo(f"Deleted: {remote_path} (moved to .Trash)")
    else:
        click.echo(f"Error: {data.get('error', 'Unknown error')}", err=True)


@cli.command()
@click.argument("path")
@click.pass_context
def mkdir(ctx, path):
    """Create a remote directory.

    \b
    Examples:
      rfs mkdir :/new-dir
      rfs mkdir :/path/to/new-dir
    """
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    if not path.startswith(":"):
        raise click.UsageError("Remote path must be prefixed with ':'.")

    remote_path = path[1:]
    if not remote_path:
        raise click.UsageError("Remote path cannot be empty.")
    if not remote_path.startswith("/"):
        remote_path = "/" + remote_path

    resp = _remote_mkdir(server, proxies, remote_path)
    data = resp.json()
    if data.get("ok"):
        click.echo(f"Created: {remote_path}")
    else:
        click.echo(f"Error: {data.get('error', 'Unknown error')}", err=True)


@cli.command()
@click.argument("src")
@click.argument("dst")
@click.option("-f", "--force", is_flag=True, help="Skip confirmation.")
@click.pass_context
def mv(ctx, src, dst, force):
    """Rename or move a remote file/directory.

    \b
    Examples:
      rfs mv :/old-name.txt :/new-name.txt
      rfs mv :/dir/file.txt :/other-dir/file.txt
    """
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    if not src.startswith(":") or not dst.startswith(":"):
        raise click.UsageError("Both paths must be remote (prefixed with ':').")

    from_path = src[1:]
    to_path = dst[1:]
    if not from_path or not to_path:
        raise click.UsageError("Paths cannot be empty.")
    if not from_path.startswith("/"):
        from_path = "/" + from_path
    if not to_path.startswith("/"):
        to_path = "/" + to_path

    if not force:
        if not click.confirm(f"Rename '{from_path}' -> '{to_path}'?"):
            click.echo("Cancelled.")
            return

    resp = _remote_rename(server, proxies, from_path, to_path)
    data = resp.json()
    if data.get("ok"):
        click.echo(f"Renamed: {from_path} -> {to_path}")
    else:
        click.echo(f"Error: {data.get('error', 'Unknown error')}", err=True)


# ─── Textual TUI ───────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def ui(ctx):
    """Interactive TUI for file transfer."""
    from rfs_tui import RfsApp

    app = RfsApp(server=ctx.obj["server"], proxies=ctx.obj["proxies"])
    app.run()


if __name__ == "__main__":
    cli()

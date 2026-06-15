__version__ = "0.2"
__all__ = ["SimpleHTTPRequestHandler"]

import html
import hashlib
import http.server
import json
import mimetypes
import os
import posixpath
import re
import shutil
import socketserver
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO


# Python 3.7+ ships ThreadingHTTPServer; build an equivalent for 3.6.
ThreadingHTTPServer = getattr(http.server, "ThreadingHTTPServer", None)
if ThreadingHTTPServer is None:
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True


def normalize_url_prefix(prefix):
    prefix = (prefix or "").strip()
    if not prefix or prefix == "/":
        return ""
    if "://" in prefix or "?" in prefix or "#" in prefix:
        raise ValueError("url prefix must be a path like /rfs")
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix.rstrip("/")


class SimpleHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """简单的http文件服务器，支持上传下载、软删除、重命名、创建目录"""

    server_version = "SimpleHTTPWithUpload/" + __version__

    def do_GET(self):
        # API endpoints
        parsed = self._parse_prefixed_path()
        if parsed is None:
            return self.send_error(404, "File not found")
        if parsed.path == "/_api/ls":
            return self._api_ls(parsed)
        if parsed.path == "/_api/trash":
            return self._api_trash(parsed)
        if parsed.path == "/_api/stat":
            return self._api_stat(parsed)

        f = self.send_head()
        if f:
            self.copyfile(f, self.wfile)
            f.close()

    def do_HEAD(self):
        f = self.send_head()
        if f:
            f.close()

    def do_POST(self):
        parsed = self._parse_prefixed_path()
        if parsed is None:
            return self.send_error(404, "File not found")

        # API endpoints (JSON body)
        if parsed.path == "/_api/mkdir":
            return self._api_mkdir()
        if parsed.path == "/_api/rename":
            return self._api_rename()
        if parsed.path == "/_api/restore":
            return self._api_restore()

        # Legacy file upload (multipart form)
        r, info, saved_path = self.deal_post_data()
        print((r, info, "by: ", self.client_address))
        f = BytesIO()
        f.write(b'<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">')
        f.write(b"<html>\n<title>Upload Result Page</title>\n")
        f.write(b"<body>\n<h2>Upload Result Page</h2>\n")
        f.write(b"<hr>\n")
        if r:
            f.write(b"<strong>Success:</strong>")
        else:
            f.write(b"<strong>Failed:</strong>")
        f.write(info.encode())
        back_url = self.headers.get("referer") or self._add_url_prefix(parsed.path)
        f.write(("<br><a href=\"%s\">back</a>" % html.escape(back_url, quote=True)).encode())
        f.write(b"</body>\n</html>\n")
        length = f.tell()
        f.seek(0)
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.send_header("Content-Length", str(length))
        if r and saved_path:
            self.send_header("X-Content-MD5", self._md5_file(saved_path))
        self.end_headers()
        if f:
            self.copyfile(f, self.wfile)
            f.close()

    def do_DELETE(self):
        """Soft-delete: move file to .Trash/ with metadata."""
        parsed = self._parse_prefixed_path()
        if parsed is None:
            return self.send_error(404, "File not found")
        path = self.translate_path(parsed.path)
        if not self._check_path_safe(path):
            return self._send_json(403, {"ok": False, "error": "Access denied: path outside serve root"})
        if not os.path.exists(path):
            return self._send_json(404, {"ok": False, "error": "File not found"})
        if os.path.isdir(path):
            return self._send_json(400, {"ok": False, "error": "Cannot delete directories via DELETE"})

        filename = os.path.basename(path)
        parent_dir = os.path.dirname(path)
        trash_dir = os.path.join(parent_dir, ".Trash")
        os.makedirs(trash_dir, exist_ok=True)

        # Generate trash filename: timestamp_originalname
        ts = int(time.time())
        trash_name = f"{ts}_{filename}"
        trash_path = os.path.join(trash_dir, trash_name)

        # Build original_path as the real filesystem path
        original_path = os.path.realpath(path)

        # Read/update manifest
        manifest_path = os.path.join(trash_dir, ".manifest.json")
        manifest = self._read_manifest(manifest_path)

        entry = {
            "original": filename,
            "original_path": original_path,
            "trashed": trash_name,
            "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "deleted_by": self.client_address[0],
            "size": os.path.getsize(path),
        }

        # Move file to trash
        shutil.move(path, trash_path)
        manifest.append(entry)
        self._write_manifest(manifest_path, manifest)

        print(f"Trashed: {original_path} -> .Trash/{trash_name} by {self.client_address}")
        return self._send_json(200, {"ok": True, "entry": entry})

    # ─── API Endpoints ─────────────────────────────────────────────────────────

    def _api_ls(self, parsed):
        """GET /_api/ls?path=/dir/&show_hidden=1 — JSON directory listing."""
        params = urllib.parse.parse_qs(parsed.query)
        rel_path = params.get("path", ["/"])[0]
        show_hidden = params.get("show_hidden", ["0"])[0] == "1"
        path = self.translate_path(rel_path)

        if not os.path.isdir(path):
            return self._send_json(404, {"ok": False, "error": "Not a directory"})

        try:
            entries = []
            for name in sorted(os.listdir(path), key=str.lower):
                # .Trash is always hidden
                if name == ".Trash":
                    continue
                # Skip hidden files unless show_hidden is set
                if name.startswith(".") and not show_hidden:
                    continue
                fullname = os.path.join(path, name)
                entry = {"name": name}
                if os.path.isdir(fullname):
                    entry["type"] = "dir"
                else:
                    entry["type"] = "file"
                    try:
                        st = os.stat(fullname)
                        entry["size"] = st.st_size
                        entry["mtime"] = st.st_mtime
                    except OSError:
                        entry["size"] = 0
                        entry["mtime"] = 0
                entries.append(entry)
            return self._send_json(200, {"ok": True, "path": rel_path, "entries": entries})
        except OSError:
            return self._send_json(403, {"ok": False, "error": "Permission denied"})

    def _api_stat(self, parsed):
        """GET /_api/stat?path=/file — File metadata."""
        params = urllib.parse.parse_qs(parsed.query)
        rel_path = params.get("path", [""])[0]
        if not rel_path:
            return self._send_json(400, {"ok": False, "error": "path required"})

        path = self.translate_path(rel_path)
        if not os.path.exists(path):
            return self._send_json(404, {"ok": False, "error": "Not found"})

        st = os.stat(path)
        info = {
            "name": os.path.basename(path),
            "path": rel_path,
            "type": "dir" if os.path.isdir(path) else "file",
            "size": st.st_size,
            "mtime": st.st_mtime,
        }
        return self._send_json(200, {"ok": True, "stat": info})

    def _api_trash(self, parsed):
        """GET /_api/trash?dir=/path/ — List trashed files for a directory."""
        params = urllib.parse.parse_qs(parsed.query)
        rel_dir = params.get("dir", ["/"])[0]
        path = self.translate_path(rel_dir)
        trash_dir = os.path.join(path, ".Trash")
        manifest_path = os.path.join(trash_dir, ".manifest.json")

        manifest = self._read_manifest(manifest_path)
        return self._send_json(200, {"ok": True, "dir": rel_dir, "entries": manifest})

    def _api_mkdir(self):
        """POST /_api/mkdir — Create directory. Body: {"path": "/new/dir"}"""
        data = self._read_json_body()
        if data is None:
            return
        rel_path = data.get("path", "")
        if not rel_path:
            return self._send_json(400, {"ok": False, "error": "path required"})

        path = self.translate_path(rel_path)
        if not self._check_path_safe(path):
            return self._send_json(403, {"ok": False, "error": "Access denied: path outside serve root"})
        if os.path.exists(path):
            return self._send_json(409, {"ok": False, "error": "Already exists"})

        try:
            os.makedirs(path, exist_ok=True)
            print(f"Mkdir: {rel_path} by {self.client_address}")
            return self._send_json(200, {"ok": True, "path": rel_path})
        except OSError as e:
            return self._send_json(500, {"ok": False, "error": str(e)})

    def _api_rename(self):
        """POST /_api/rename — Rename/move. Body: {"from": "/old", "to": "/new"}"""
        data = self._read_json_body()
        if data is None:
            return
        from_rel = data.get("from", "")
        to_rel = data.get("to", "")
        if not from_rel or not to_rel:
            return self._send_json(400, {"ok": False, "error": "from and to required"})

        from_path = self.translate_path(from_rel)
        to_path = self.translate_path(to_rel)

        if not self._check_path_safe(from_path) or not self._check_path_safe(to_path):
            return self._send_json(403, {"ok": False, "error": "Access denied: path outside serve root"})

        if not os.path.exists(from_path):
            return self._send_json(404, {"ok": False, "error": "Source not found"})
        if os.path.exists(to_path):
            return self._send_json(409, {"ok": False, "error": "Target already exists"})

        try:
            # Ensure parent dir of target exists
            os.makedirs(os.path.dirname(to_path), exist_ok=True)
            os.rename(from_path, to_path)
            print(f"Renamed: {from_rel} -> {to_rel} by {self.client_address}")
            return self._send_json(200, {"ok": True, "from": from_rel, "to": to_rel})
        except OSError as e:
            return self._send_json(500, {"ok": False, "error": str(e)})

    def _api_restore(self):
        """POST /_api/restore — Restore from trash. Body: {"dir": "/path/", "trashed": "123_file"}"""
        data = self._read_json_body()
        if data is None:
            return
        rel_dir = data.get("dir", "")
        trashed_name = data.get("trashed", "")
        if not trashed_name:
            return self._send_json(400, {"ok": False, "error": "trashed required"})

        dir_path = self.translate_path(rel_dir) if rel_dir else os.getcwd()
        trash_dir = os.path.join(dir_path, ".Trash")
        trash_path = os.path.join(trash_dir, trashed_name)
        manifest_path = os.path.join(trash_dir, ".manifest.json")

        if not os.path.exists(trash_path):
            return self._send_json(404, {"ok": False, "error": "Trashed file not found"})

        manifest = self._read_manifest(manifest_path)

        # Find entry in manifest
        entry = None
        for e in manifest:
            if e["trashed"] == trashed_name:
                entry = e
                break

        if not entry:
            return self._send_json(404, {"ok": False, "error": "Entry not in manifest"})

        # Restore to original path
        original_path = self.translate_path(entry["original_path"])
        if not self._check_path_safe(original_path):
            return self._send_json(403, {"ok": False, "error": "Access denied: path outside serve root"})
        if os.path.exists(original_path):
            return self._send_json(409, {"ok": False, "error": "Original path already occupied"})

        try:
            os.makedirs(os.path.dirname(original_path), exist_ok=True)
            shutil.move(trash_path, original_path)
            manifest.remove(entry)
            self._write_manifest(manifest_path, manifest)
            print(f"Restored: {entry['original_path']} by {self.client_address}")
            return self._send_json(200, {"ok": True, "restored": entry["original_path"]})
        except OSError as e:
            return self._send_json(500, {"ok": False, "error": str(e)})

    # ─── Helpers ───────────────────────────────────────────────────────────────

    def _md5_file(self, filepath):
        """Compute MD5 hex digest of a file."""
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _check_path_safe(self, path):
        """Ensure resolved path is within serve root (cwd). Returns True if safe."""
        real = os.path.realpath(path)
        root = os.path.realpath(os.getcwd())
        # Allow root itself and anything under it
        return real == root or real.startswith(root + os.sep)

    def _url_prefix(self):
        return getattr(self.server, "url_prefix", "")

    def _strip_url_prefix(self, path):
        prefix = self._url_prefix()
        if not prefix:
            return path
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
        return None

    def _add_url_prefix(self, path):
        prefix = self._url_prefix()
        if not path.startswith("/"):
            path = "/" + path
        return prefix + path if prefix else path

    def _parse_prefixed_path(self):
        parsed = urllib.parse.urlparse(self.path)
        stripped_path = self._strip_url_prefix(parsed.path)
        if stripped_path is None:
            return None
        return parsed._replace(path=stripped_path)

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, {"ok": False, "error": "Empty body"})
            return None
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {e}"})
            return None

    def _read_manifest(self, manifest_path):
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return []
        return []

    def _write_manifest(self, manifest_path, manifest):
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    # ─── Original Methods ──────────────────────────────────────────────────────

    def deal_post_data(self):
        """Parse multipart upload. Returns (success, message, saved_path)."""
        parsed = self._parse_prefixed_path()
        if parsed is None:
            return (False, "URL prefix does not match", None)
        content_type = self.headers['content-type']
        if not content_type:
            return (False, "Content-Type header doesn't contain boundary", None)
        boundary = content_type.split("=")[1].encode()
        content_length = int(self.headers['content-length'])

        # Read entire body at once to avoid binary data corruption from readline
        body = self.rfile.read(content_length)

        # Find boundary markers
        bound = b"--" + boundary
        parts = body.split(bound)
        # parts[0] is empty (before first boundary)
        # parts[1] is the file part
        # parts[2] is the closing "--\r\n"
        if len(parts) < 2:
            return (False, "Content NOT begin with boundary", None)

        file_part = parts[1]
        # Strip leading \r\n
        if file_part.startswith(b"\r\n"):
            file_part = file_part[2:]

        # Split headers from content (separated by \r\n\r\n)
        header_end = file_part.find(b"\r\n\r\n")
        if header_end == -1:
            return (False, "Malformed multipart data", None)

        headers_section = file_part[:header_end].decode("utf-8", errors="replace")
        file_data = file_part[header_end + 4:]

        # Remove trailing \r\n before next boundary
        if file_data.endswith(b"\r\n"):
            file_data = file_data[:-2]

        # Extract filename
        fn = re.findall(r'Content-Disposition.*name="file"; filename="(.*)"', headers_section)
        if not fn:
            return (False, "Can't find out file name...", None)

        path = self.translate_path(parsed.path)
        filepath = os.path.join(path, fn[0])

        try:
            with open(filepath, 'wb') as out:
                out.write(file_data)
        except IOError:
            return (False, "Can't create file to write, do you have permission to write?", None)

        return (True, "File '%s' upload success!" % filepath, filepath)

    def send_head(self):
        parsed = self._parse_prefixed_path()
        if parsed is None:
            self.send_error(404, "File not found")
            return None
        path = self.translate_path(parsed.path)
        f = None
        if os.path.isdir(path):
            if not parsed.path.endswith('/'):
                self.send_response(301)
                self.send_header("Location", self._add_url_prefix(parsed.path + "/"))
                self.end_headers()
                return None
            for index in "index.html", "index.htm":
                index = os.path.join(path, index)
                if os.path.exists(index):
                    path = index
                    break
            else:
                return self.list_directory(path)
        ctype = self.guess_type(path)
        try:
            f = open(path, 'rb')
        except IOError:
            self.send_error(404, "File not found")
            return None
        self.send_response(200)
        self.send_header("Content-type", ctype)
        fs = os.fstat(f.fileno())
        self.send_header("Content-Length", str(fs[6]))
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.send_header("X-Content-MD5", self._md5_file(path))
        self.end_headers()
        return f

    def list_directory(self, path):
        try:
            list = os.listdir(path)
        except os.error:
            self.send_error(404, "No permission to list directory")
            return None
        list.sort(key=lambda a: a.lower())
        f = BytesIO()
        parsed = self._parse_prefixed_path()
        display_path = self._add_url_prefix(parsed.path if parsed else self.path)
        displaypath = html.escape(urllib.parse.unquote(display_path))
        f.write(b'<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">')
        f.write(("<html>\n<title>Directory listing for %s</title>\n" % displaypath).encode())
        f.write(("<body>\n<h2>Directory listing for %s</h2>\n" % displaypath).encode())
        f.write(b"<hr>\n")
        f.write(b"<form ENCTYPE=\"multipart/form-data\" method=\"post\">")
        f.write(b"<input name=\"file\" type=\"file\"/>")
        f.write(b"<input type=\"submit\" value=\"upload\"/></form>\n")
        f.write(b"<hr>\n<ul>\n")
        for name in list:
            if name == ".Trash":
                continue
            fullname = os.path.join(path, name)
            displayname = linkname = name
            if os.path.isdir(fullname):
                displayname = name + "/"
                linkname = name + "/"
            if os.path.islink(fullname):
                displayname = name + "@"
            f.write(('<li><a href="%s">%s</a>\n'
                     % (urllib.parse.quote(linkname), html.escape(displayname))).encode())
        f.write(b"</ul>\n<hr>\n</body>\n</html>\n")
        length = f.tell()
        f.seek(0)
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        return f

    def translate_path(self, path):
        path = path.split('?', 1)[0]
        path = path.split('#', 1)[0]
        path = posixpath.normpath(urllib.parse.unquote(path))
        words = path.split('/')
        words = [_f for _f in words if _f]
        path = os.getcwd()
        for word in words:
            drive, word = os.path.splitdrive(word)
            head, word = os.path.split(word)
            if word in (os.curdir, os.pardir):
                continue
            path = os.path.join(path, word)
        return path

    def copyfile(self, source, outputfile):
        shutil.copyfileobj(source, outputfile)

    def guess_type(self, path):
        base, ext = posixpath.splitext(path)
        if ext in self.extensions_map:
            return self.extensions_map[ext]
        ext = ext.lower()
        if ext in self.extensions_map:
            return self.extensions_map[ext]
        else:
            return self.extensions_map['']

    if not mimetypes.inited:
        mimetypes.init()
    extensions_map = mimetypes.types_map.copy()
    extensions_map.update({
        '': 'application/octet-stream',
        '.py': 'text/plain',
        '.c': 'text/plain',
        '.h': 'text/plain',
    })


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', '-b', default='0.0.0.0', metavar='ADDRESS',
                        help='Specify alternate bind address '
                             '[default: all interfaces]')
    parser.add_argument('--port', '-p', default=8002, type=int,
                        help='Specify alternate port')
    parser.add_argument('--url-prefix', default='',
                        help='URL path prefix when served behind a reverse proxy, e.g. /rfs')
    args = parser.parse_args()
    try:
        url_prefix = normalize_url_prefix(args.url_prefix)
    except ValueError as e:
        parser.error(str(e))

    # Use ThreadingHTTPServer for concurrent request handling
    server = ThreadingHTTPServer(
        (args.bind, args.port), SimpleHTTPRequestHandler
    )
    server.url_prefix = url_prefix
    prefix_text = f" with URL prefix {url_prefix}" if url_prefix else ""
    print(f"Serving on {args.bind}:{args.port}{prefix_text} (threaded) ...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()

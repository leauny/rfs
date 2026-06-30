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
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import BytesIO


# Python 3.7+ ships ThreadingHTTPServer; build an equivalent for 3.6.
ThreadingHTTPServer = getattr(http.server, "ThreadingHTTPServer", None)
if ThreadingHTTPServer is None:
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True


class TaskRegistry:
    """Thread-safe in-memory registry of in-flight upload/download tasks.

    Purely in-memory: nothing is persisted, so a process restart clears all
    state. Only running tasks are kept; finish() removes them. Used to answer
    "is anything transferring right now?" (e.g. before restarting the server).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}  # id -> task dict (running only)

    def add(self, task):
        """Register a running task. `task` is taken by reference; returns its id."""
        tid = task.get("id") or uuid.uuid4().hex
        task["id"] = tid
        with self._lock:
            self._tasks[tid] = task
        return tid

    def finish(self, tid):
        """Remove a finished/failed task. No-op if already gone."""
        if not tid:
            return
        with self._lock:
            self._tasks.pop(tid, None)

    def list(self):
        """Snapshot of currently running tasks."""
        with self._lock:
            return list(self._tasks.values())


# Directory listing template. Uses string.Template to keep it readable while
# avoiding accidental %-formatting collisions with the inlined CSS/JS.
# `$$` in the JS escapes a literal `$` for string.Template.
_DIR_TEMPLATE = string.Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>$title</title>
<style>
  body { font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", sans-serif; max-width: 960px; margin: 24px auto; padding: 0 16px; color: #222; }
  h2 { font-weight: 600; margin: 0 0 4px; word-break: break-all; }
  .crumbs { color: #888; font-size: 13px; margin-bottom: 16px; }
  .crumbs a.parent { color: #06c; text-decoration: none; margin-left: 6px; }
  .crumbs a.parent:hover { text-decoration: underline; }
  .indicator { font-size: 13px; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; display: flex; align-items: center; gap: 6px; }
  .indicator.idle { background: #f3fbf6; color: #2a7; border: 1px solid #cdeedd; }
  .indicator.busy { background: #fff7ed; color: #b3591a; border: 1px solid #f3d7b3; }
  .indicator.unknown { background: #f6f8fa; color: #888; border: 1px solid #e1e4e8; }
  .indicator #task-label { min-width: 0; }
  .indicator .files { margin: 4px 0 0; padding: 0; color: inherit; }
  .indicator .files li { list-style: none; }
  .indicator .task-refresh { margin-left: auto; border: 1px solid currentColor; background: transparent; color: inherit; border-radius: 4px; padding: 1px 7px; cursor: pointer; font-size: 13px; line-height: 1.4; opacity: .7; }
  .indicator .task-refresh:hover { opacity: 1; }
  .toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .toolbar .spacer { flex: 1; }
  .toolbar button { border: 1px solid #ccd2d8; border-radius: 6px; background: #fff; padding: 6px 10px; cursor: pointer; }
  .toolbar button:hover { background: #f6f8fa; }
  .toolbar input[type="search"] { border: 1px solid #ccd2d8; border-radius: 6px; padding: 6px 10px; outline: none; width: 180px; font-size: 13px; }
  .toolbar input[type="search"]:focus { border-color: #2a7; box-shadow: 0 0 0 2px rgba(42,170,119,.15); }
  .toolbar input[type="search"]::placeholder { color: #aaa; }
  tr.hidden { display: none; }
  .drop { border: 2px dashed #cfd4d9; border-radius: 8px; padding: 24px; text-align: center; color: #666; transition: border-color .15s, background .15s; }
  .drop.hover { border-color: #2a7; background: #f3fbf6; color: #2a7; }
  .drop label { color: #06c; cursor: pointer; }
  .drop.disabled { border-color: #e0e0e0; background: #f9f9f9; color: #aaa; pointer-events: none; }
  .drop.disabled label { color: #aaa; cursor: default; }
  .bar { height: 6px; background: #eee; border-radius: 3px; overflow: hidden; margin: 12px auto 0; max-width: 480px; display: none; }
  .bar > div { height: 100%; width: 0; background: #2a7; transition: width .1s linear; }
  .meta { font-size: 12px; color: #666; margin-top: 6px; min-height: 16px; font-variant-numeric: tabular-nums; }
  dialog { border: 1px solid #ccd2d8; border-radius: 8px; padding: 18px; box-shadow: 0 8px 30px rgba(0,0,0,.18); min-width: 320px; }
  dialog::backdrop { background: rgba(0,0,0,.25); }
  dialog p { margin: 0 0 12px; }
  dialog input { box-sizing: border-box; width: 100%; padding: 6px 8px; margin-bottom: 12px; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; }
  table { width: 100%; border-collapse: collapse; margin-top: 20px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }
  th { font-weight: 500; color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  td.name a { color: #06c; text-decoration: none; }
  td.name a:hover { text-decoration: underline; }
  td.size, td.mtime { color: #888; font-variant-numeric: tabular-nums; white-space: nowrap; width: 1%; }
  td.empty { color: #aaa; text-align: center; padding: 24px; }
  tr:hover td { background: #fafafa; }
</style>
</head>
<body>
  <h2>$display_path</h2>
  <div class="crumbs">Directory listing $parent_link</div>
  <div id="task-indicator" class="indicator unknown"><div id="task-label">检查任务状态…</div><button id="task-refresh" type="button" class="task-refresh" title="立即刷新">↻</button></div>
  <div class="toolbar"><input id="search" type="search" placeholder="搜索文件…"><div class="spacer"></div><button id="refresh" type="button">刷新</button><button id="mkdir" type="button">新建文件夹</button></div>

  <div id="drop" class="drop">
    <span id="drop-idle">拖拽文件到此处，或 <label>点击选择<input id="file" type="file" multiple hidden></label></span>
    <span id="drop-busy" style="display:none">上传中，请等待…</span>
    <div class="bar"><div id="pb"></div></div>
    <div id="meta" class="meta"></div>
  </div>

  <dialog id="conflict">
    <p id="conflict-msg"></p>
    <input id="rename" autocomplete="off">
    <div class="actions">
      <button id="overwrite" type="button">Overwrite</button>
      <button id="rename-btn" type="button">Rename</button>
      <button id="cancel" type="button">Cancel</button>
    </div>
  </dialog>

  <table>
    <thead><tr><th>名称</th><th class="size">大小</th><th class="mtime">修改时间</th></tr></thead>
    <tbody>
      $rows
    </tbody>
  </table>

<script>
(function () {
  var drop = document.getElementById('drop');
  var input = document.getElementById('file');
  var mkdirBtn = document.getElementById('mkdir');
  var bar = drop.querySelector('.bar');
  var pb = document.getElementById('pb');
  var meta = document.getElementById('meta');
  var dialog = document.getElementById('conflict');
  var conflictMsg = document.getElementById('conflict-msg');
  var renameInput = document.getElementById('rename');
  var overwriteBtn = document.getElementById('overwrite');
  var renameBtn = document.getElementById('rename-btn');
  var cancelBtn = document.getElementById('cancel');
  var dropIdle = document.getElementById('drop-idle');
  var dropBusy = document.getElementById('drop-busy');
  var queue = [], busy = false, remoteEntries = null;

  function setDropState(uploading) {
    if (uploading) {
      drop.classList.add('disabled');
      dropIdle.style.display = 'none';
      dropBusy.style.display = '';
      input.disabled = true;
    } else {
      drop.classList.remove('disabled');
      dropIdle.style.display = '';
      dropBusy.style.display = 'none';
      input.disabled = false;
    }
  }

  function fmt(n) {
    var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + ' ' + u[i];
  }

  function fmtEta(s) {
    s = Math.ceil(s);
    if (s < 0) s = 0;
    var d = Math.floor(s / 86400); s %= 86400;
    var h = Math.floor(s / 3600);  s %= 3600;
    var m = Math.floor(s / 60);   s %= 60;
    var r = '';
    if (d) r += d + 'd';
    if (h || d) r += h + 'h';
    if (m || h || d) r += m + 'm';
    r += s + 's';
    return r;
  }

  function apiUrl(path) {
    var prefix = '$url_prefix';
    return (prefix || '') + path;
  }

  function remotePath() {
    var prefix = '$url_prefix';
    var path = location.pathname;
    if (prefix && path.indexOf(prefix + '/') === 0) return path.slice(prefix.length) || '/';
    if (prefix && path === prefix) return '/';
    return path;
  }

  function pump() {
    if (busy || !queue.length) return;
    busy = true;
    setDropState(true);
    var item = queue.shift();
    var file = item.file;
    var uploadName = item.name || file.name;
    var fd = new FormData();
    fd.append('file', file, uploadName);

    var xhr = new XMLHttpRequest();
    xhr.open('POST', location.pathname + (item.overwrite ? '?overwrite=1' : ''), true);

    var keepAliveTimer = setInterval(function () {
      fetch(apiUrl('/_api/ping'), { method: 'GET', keepalive: true }).catch(function () {});
    }, 30000);

    var t0 = Date.now(), lastT = t0, lastL = 0;
    bar.style.display = 'block';
    pb.style.width = '0';
    meta.textContent = uploadName + ' · 0 / ' + fmt(file.size);

    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var pct = e.loaded * 100 / e.total;
      pb.style.width = pct.toFixed(1) + '%';
      var now = Date.now();
      if (now - lastT > 200) {
        var spd = (e.loaded - lastL) * 1000 / (now - lastT);
        var eta = spd > 0 ? (e.total - e.loaded) / spd : 0;
        meta.textContent = uploadName + ' · ' + fmt(e.loaded) + ' / ' + fmt(e.total)
          + ' · ' + fmt(spd) + '/s · 剩余 ' + fmtEta(eta);
        lastT = now; lastL = e.loaded;
      }
    };
    xhr.onload = function () {
      clearInterval(keepAliveTimer);
      busy = false;
      if (xhr.status >= 200 && xhr.status < 300) {
        var avg = file.size * 1000 / Math.max(1, Date.now() - t0);
        var md5 = xhr.getResponseHeader('X-Content-MD5');
        pb.style.width = '100%';
        meta.textContent = uploadName + ' · 完成 · 平均 ' + fmt(avg) + '/s' + (md5 ? ' · MD5 ' + md5 : '');
        if (queue.length) pump();
        else { setDropState(false); setTimeout(refreshListing, 500); }
      } else {
        meta.textContent = '上传失败 (' + xhr.status + '): ' + uploadName;
        if (queue.length) pump(); else setDropState(false);
      }
    };
    xhr.onerror = function () {
      clearInterval(keepAliveTimer);
      busy = false;
      meta.textContent = '网络错误：' + uploadName;
      if (queue.length) pump(); else setDropState(false);
    };
    xhr.send(fd);
  }

  function entryMapFromList(list) {
    var entries = {};
    list.forEach(function (entry) { entries[entry.name] = entry.type === 'dir'; });
    return entries;
  }

  function fetchRemoteEntries(cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', apiUrl('/_api/ls') + '?path=' + encodeURIComponent(remotePath()), true);
    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          var data = JSON.parse(xhr.responseText);
          if (data.ok) {
            remoteEntries = entryMapFromList(data.entries);
            cb(data.entries);
            return;
          }
        } catch (e) {}
      }
      remoteEntries = remoteEntries || {};
      cb(null);
    };
    xhr.onerror = function () { remoteEntries = remoteEntries || {}; cb(null); };
    xhr.send();
  }

  function loadRemoteEntries(cb) {
    if (remoteEntries) { cb(remoteEntries); return; }
    fetchRemoteEntries(function () { cb(remoteEntries || {}); });
  }

  function renderListing(entries) {
    clearNode(tbody);
    if (!entries || !entries.length) {
      var emptyRow = document.createElement('tr');
      var emptyCell = document.createElement('td');
      emptyCell.className = 'empty';
      emptyCell.colSpan = 3;
      emptyCell.textContent = '— 空目录 —';
      emptyRow.appendChild(emptyCell);
      tbody.appendChild(emptyRow);
      return;
    }
    entries.forEach(function (entry) {
      var row = document.createElement('tr');
      var nameCell = document.createElement('td');
      var sizeCell = document.createElement('td');
      var mtimeCell = document.createElement('td');
      var link = document.createElement('a');
      var isDir = entry.type === 'dir';
      nameCell.className = 'name';
      sizeCell.className = 'size';
      mtimeCell.className = 'mtime';
      link.href = encodeURIComponent(entry.name) + (isDir ? '/' : '');
      link.textContent = entry.name + (isDir ? '/' : '');
      nameCell.appendChild(link);
      sizeCell.textContent = isDir ? '—' : fmt(entry.size || 0);
      mtimeCell.textContent = isDir || !entry.mtime
        ? '—'
        : new Date(entry.mtime * 1000).toISOString().slice(0, 16).replace('T', ' ');
      row.appendChild(nameCell);
      row.appendChild(sizeCell);
      row.appendChild(mtimeCell);
      tbody.appendChild(row);
    });
  }

  function refreshListing() {
    fetchRemoteEntries(function (entries) {
      if (!entries) {
        meta.textContent = '刷新列表失败';
        return;
      }
      renderListing(entries);
      meta.textContent = '列表已刷新';
      searchInput.dispatchEvent(new Event('input'));
    });
  }

  function joinRemotePath(name) {
    var base = remotePath();
    if (base.charAt(base.length - 1) !== '/') base += '/';
    return base + name;
  }

  function createDirectory() {
    var name = prompt('新建文件夹名称：');
    if (name === null) return;
    name = name.trim();
    if (!name) {
      meta.textContent = '请输入文件夹名称';
      alert('请输入文件夹名称');
      return;
    }
    if (name === '.' || name === '..') {
      meta.textContent = '文件夹名称不能是 . 或 ..';
      alert('文件夹名称不能是 . 或 ..');
      return;
    }
    if (name.indexOf('/') !== -1 || name.indexOf('\\\\') !== -1) {
      meta.textContent = '文件夹名称不能包含路径分隔符';
      alert('文件夹名称不能包含路径分隔符');
      return;
    }
    if (Object.prototype.hasOwnProperty.call(remoteEntries || {}, name)) {
      meta.textContent = "Remote '" + name + "' already exists";
      alert("Remote '" + name + "' already exists");
      return;
    }
    var xhr = new XMLHttpRequest();
    xhr.open('POST', apiUrl('/_api/mkdir'), true);
    xhr.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 300) {
        if (remoteEntries) remoteEntries[name] = true;
        meta.textContent = '已创建文件夹：' + name;
        setTimeout(refreshListing, 300);
      } else {
        var err = '创建文件夹失败 (' + xhr.status + ')';
        try { err += ': ' + (JSON.parse(xhr.responseText).error || ''); } catch (e) {}
        meta.textContent = err;
      }
    };
    xhr.onerror = function () { meta.textContent = '创建文件夹网络错误：' + name; };
    xhr.send(JSON.stringify({ path: joinRemotePath(name) }));
  }

  function resolveConflict(file, entries, cb) {
    if (!Object.prototype.hasOwnProperty.call(entries, file.name)) {
      cb({ file: file, name: file.name, overwrite: false });
      return;
    }
    var isDir = entries[file.name];
    conflictMsg.textContent = isDir
      ? "Remote directory '" + file.name + "' has the same name. Cannot overwrite."
      : "Remote file '" + file.name + "' already exists.";
    renameInput.value = file.name;
    overwriteBtn.style.display = isDir ? 'none' : '';
    function done(result) {
      overwriteBtn.onclick = renameBtn.onclick = cancelBtn.onclick = dialog.oncancel = null;
      dialog.close();
      cb(result);
    }
    overwriteBtn.onclick = function () { done({ file: file, name: file.name, overwrite: true }); };
    renameBtn.onclick = function () {
      var name = renameInput.value.trim();
      if (!name || name === file.name) {
        meta.textContent = '请输入不同的文件名';
        return;
      }
      if (Object.prototype.hasOwnProperty.call(remoteEntries || {}, name)) {
        meta.textContent = "Remote '" + name + "' already exists";
        return;
      }
      done({ file: file, name: name, overwrite: false });
    };
    cancelBtn.onclick = function () { done(null); };
    dialog.oncancel = function (e) { e.preventDefault(); done(null); };
    dialog.showModal();
  }

  function add(files) {
    var list = Array.prototype.slice.call(files);
    if (!list.length) return;
    loadRemoteEntries(function (entries) {
      function next() {
        if (!list.length) { pump(); return; }
        resolveConflict(list.shift(), entries, function (item) {
          if (item) {
            queue.push(item);
            entries[item.name] = false;
          }
          next();
        });
      }
      next();
    });
  }

  mkdirBtn.addEventListener('click', createDirectory);
  document.getElementById('refresh').addEventListener('click', refreshListing);
  var searchInput = document.getElementById('search');
  var tbody = document.querySelector('table tbody');
  searchInput.addEventListener('input', function () {
    var q = searchInput.value.trim().toLowerCase();
    var rows = tbody.querySelectorAll('tr');
    for (var i = 0; i < rows.length; i++) {
      var link = rows[i].querySelector('td.name a');
      if (!link) continue;
      rows[i].classList.toggle('hidden', q && link.textContent.toLowerCase().indexOf(q) === -1);
    }
  });
  input.addEventListener('change', function () {
    add(input.files);
    input.value = '';
  });
  ['dragenter', 'dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('hover'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('hover'); });
  });
  drop.addEventListener('drop', function (e) {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      add(e.dataTransfer.files);
    }
  });

  // ── Running-task indicator (polls /_api/tasks every 5s) ──
  var indicator = document.getElementById('task-indicator');
  var taskLabel = document.getElementById('task-label');
  var pollTimer = null;

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function renderTasks(tasks) {
    if (!tasks.length) {
      indicator.className = 'indicator idle';
      taskLabel.textContent = '✓ 空闲，可安全重启';
      return;
    }
    indicator.className = 'indicator busy';
    clearNode(taskLabel);
    taskLabel.appendChild(document.createTextNode('⚠ ' + tasks.length + ' 个任务进行中，请勿重启'));
    var list = document.createElement('ul');
    list.className = 'files';
    tasks.forEach(function (t) {
      var item = document.createElement('li');
      var arrow = t.type === 'upload' ? '↑' : '↓';
      item.textContent = arrow + ' ' + (t.filename || t.path || '(unknown)');
      list.appendChild(item);
    });
    taskLabel.appendChild(list);
  }

  function pollTasks() {
    return fetch(apiUrl('/_api/tasks'), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.ok) renderTasks(data.tasks || []);
        else { indicator.className = 'indicator unknown'; taskLabel.textContent = '任务状态不可用'; }
      })
      .catch(function () {
        indicator.className = 'indicator unknown';
        taskLabel.textContent = '无法连接服务器';
      });
  }

  document.getElementById('task-refresh').addEventListener('click', function () {
    this.blur();   // drop focus so the button matches the auto-refresh appearance
    pollTasks();
  });

  function startPolling() {
    if (pollTimer) return;
    pollTasks();
    pollTimer = setInterval(pollTasks, 5000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopPolling(); else startPolling();
  });
  startPolling();
})();
</script>
</body>
</html>
""")


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
        if parsed.path == "/_api/ping":
            return self._send_json(200, {"ok": True})
        if parsed.path == "/_api/ls":
            return self._api_ls(parsed)
        if parsed.path == "/_api/trash":
            return self._api_trash(parsed)
        if parsed.path == "/_api/stat":
            return self._api_stat(parsed)
        if parsed.path == "/_api/tasks":
            return self._api_tasks()

        # Register the download task *before* send_head: send_head computes
        # X-Content-MD5 by reading the whole file, which for large files blocks
        # for a while. That phase must be covered by the "busy" indicator too,
        # otherwise the panel would falsely show "safe to restart" mid-download.
        reg = getattr(self.server, "task_registry", None)
        tid = None
        if reg:
            pre_path = self.translate_path(parsed.path)
            if os.path.isfile(pre_path):
                tid = reg.add({
                    "type": "download",
                    "path": parsed.path,
                    "filename": os.path.basename(parsed.path.rstrip("/")) or parsed.path,
                    "client_ip": self.client_address[0],
                    "started_at": time.time(),
                })
        try:
            f = self.send_head()
            if f:
                try:
                    self.copyfile(f, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    # Client disconnected mid-download; task is over either way.
                    pass
                finally:
                    f.close()
        finally:
            if reg and tid:
                reg.finish(tid)

    def do_HEAD(self):
        parsed = self._parse_prefixed_path()
        if parsed is None:
            return self.send_error(404, "File not found")
        if parsed.path == "/_api/ping":
            return self._send_json(200, {"ok": True})
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
        r, info, saved_path, md5_hex = self.deal_post_data()
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
        self.send_response(200 if r else (409 if info in ("File already exists", "A directory with this name already exists") else 400))
        self.send_header("Content-type", "text/html")
        self.send_header("Content-Length", str(length))
        if r and md5_hex:
            self.send_header("X-Content-MD5", md5_hex)
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

    def _api_tasks(self):
        """GET /_api/tasks — Snapshot of currently running upload/download tasks."""
        reg = getattr(self.server, "task_registry", None)
        tasks = reg.list() if reg else []
        return self._send_json(200, {"ok": True, "tasks": tasks})

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
        if self._path_has_unsafe_segments(rel_path):
            return self._send_json(400, {"ok": False, "error": "Invalid directory path"})

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

    def _path_has_unsafe_segments(self, rel_path):
        if "\\" in rel_path:
            return True
        return any(part in (".", "..") for part in rel_path.split("/") if part)

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
        """Streaming multipart upload parser.

        Reads the request body in fixed-size chunks, locating the part
        headers and the closing `\\r\\n--boundary` marker without ever
        holding the full body in memory. Writes the file content directly
        and computes MD5 on the fly. Returns
        `(ok, message, saved_path, md5_hex)`; `md5_hex` is None on failure.
        """
        parsed = self._parse_prefixed_path()
        if parsed is None:
            return (False, "URL prefix does not match", None, None)
        content_type = self.headers.get('content-type', '')
        if not content_type or 'boundary=' not in content_type:
            return (False, "Content-Type header doesn't contain boundary", None, None)
        boundary = content_type.split('boundary=', 1)[1].strip().encode()
        if boundary.startswith(b'"') and boundary.endswith(b'"'):
            boundary = boundary[1:-1]
        sep = b'--' + boundary
        end_sep = b'\r\n' + sep             # marker that closes the file part
        try:
            content_length = int(self.headers.get('content-length', 0))
        except ValueError:
            return (False, "Invalid Content-Length", None, None)

        READ = 1 << 20                      # 1 MiB per recv (safe on macOS)
        remaining = [content_length]        # mutable so the helper can update

        def read_some(n):
            n = min(n, remaining[0])
            if n <= 0:
                return b''
            data = self.rfile.read(n)
            if data:
                remaining[0] -= len(data)
            return data

        # ─── Phase 1: locate the first boundary and the part-header terminator
        buf = b''
        after_sep_len = 0
        header_end = -1
        HEADER_LIMIT = 65536
        while True:
            chunk = read_some(READ)
            if not chunk:
                return (False, "Client disconnected before headers", None, None)
            buf += chunk
            if buf.startswith(sep):
                after_sep_len = len(sep)
            elif buf.startswith(b'\r\n' + sep):
                after_sep_len = len(sep) + 2
            else:
                idx = buf.find(sep)
                if idx == -1:
                    if len(buf) > HEADER_LIMIT:
                        return (False, "Boundary not found in preamble", None, None)
                    continue
                buf = buf[idx:]
                after_sep_len = len(sep)
            hdr_idx = buf.find(b'\r\n\r\n', after_sep_len)
            if hdr_idx != -1:
                header_end = hdr_idx
                break
            if len(buf) > HEADER_LIMIT:
                return (False, "Multipart headers too large", None, None)

        headers_section = buf[after_sep_len:header_end].decode('utf-8', errors='replace')
        headers_section = headers_section.lstrip('\r\n')
        fn = re.findall(r'filename="([^"]*)"', headers_section)
        if not fn or not fn[0]:
            return (False, "Can't find out file name...", None, None)
        filename = os.path.basename(fn[0])
        if not filename:
            return (False, "Can't find out file name...", None, None)

        path = self.translate_path(parsed.path)
        filepath = os.path.join(path, filename)

        reg = getattr(self.server, "task_registry", None)
        tid = reg.add({
            "type": "upload",
            "path": parsed.path,
            "filename": filename,
            "client_ip": self.client_address[0],
            "started_at": time.time(),
        }) if reg else None
        try:
            params = urllib.parse.parse_qs(parsed.query)
            overwrite = params.get("overwrite", ["0"])[0] == "1"
            if os.path.isdir(filepath):
                return (False, "A directory with this name already exists", None, None)
            if os.path.exists(filepath) and not overwrite:
                return (False, "File already exists", None, None)

            # ─── Phase 2: stream the body to disk, hashing on the fly
            leftover = buf[header_end + 4:]
            del buf
            h = hashlib.md5()
            # Always keep at least len(end_sep)+4 bytes back so a boundary that
            # straddles two reads is never accidentally written to the file.
            tail_keep = len(end_sep) + 4

            try:
                out = open(filepath, 'wb')
            except IOError:
                return (False, "Can't create file to write, do you have permission to write?", None, None)

            try:
                with out:
                    window = leftover
                    while True:
                        idx = window.find(end_sep)
                        if idx != -1:
                            out.write(window[:idx])
                            h.update(window[:idx])
                            break
                        if len(window) > tail_keep:
                            flush = window[:-tail_keep]
                            out.write(flush)
                            h.update(flush)
                            window = window[-tail_keep:]
                        if remaining[0] <= 0:
                            # Stream exhausted without finding the closing boundary;
                            # flush whatever's left (best-effort, rare for valid clients).
                            if window:
                                out.write(window)
                                h.update(window)
                            break
                        chunk = read_some(READ)
                        if not chunk:
                            if window:
                                out.write(window)
                                h.update(window)
                            break
                        window += chunk
            except IOError as e:
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                return (False, "Write failed: %s" % e, None, None)

            return (True, "File '%s' upload success!" % filepath, filepath, h.hexdigest())
        finally:
            if reg and tid:
                reg.finish(tid)

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
            names = os.listdir(path)
        except os.error:
            self.send_error(404, "No permission to list directory")
            return None
        names.sort(key=lambda a: a.lower())

        parsed = self._parse_prefixed_path()
        rel_path = parsed.path if parsed else "/"
        display_path = self._add_url_prefix(rel_path)
        displaypath = html.escape(urllib.parse.unquote(display_path))

        # Parent link (None at root)
        parent_href = None
        if rel_path not in ("/", ""):
            parent_rel = posixpath.normpath(rel_path.rstrip("/") + "/..")
            if not parent_rel.endswith("/"):
                parent_rel += "/"
            parent_href = html.escape(self._add_url_prefix(parent_rel), quote=True)

        rows = []
        for name in names:
            if name == ".Trash":
                continue
            fullname = os.path.join(path, name)
            is_dir = os.path.isdir(fullname)
            is_link = os.path.islink(fullname)
            linkname = name + "/" if is_dir else name
            displayname = linkname + ("@" if is_link else "")
            try:
                st = os.stat(fullname)
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                size_str = "—" if is_dir else self._format_size(st.st_size)
            except OSError:
                mtime = "—"
                size_str = "—"
            href = html.escape(urllib.parse.quote(linkname), quote=True)
            rows.append(
                '<tr><td class="name"><a href="%s">%s</a></td>'
                '<td class="size">%s</td><td class="mtime">%s</td></tr>'
                % (href, html.escape(displayname), size_str, mtime)
            )

        parent_html = (
            '<a class="parent" href="%s">↑ Parent</a>' % parent_href
            if parent_href else ""
        )

        body = _DIR_TEMPLATE.substitute(
            title=displaypath,
            display_path=displaypath,
            parent_link=parent_html,
            url_prefix=self._url_prefix(),
            rows="\n      ".join(rows) if rows
                 else '<tr><td colspan="3" class="empty">— 空目录 —</td></tr>',
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        f = BytesIO(body)
        return f

    @staticmethod
    def _format_size(n):
        units = ("B", "KB", "MB", "GB", "TB")
        i = 0
        size = float(n)
        while size >= 1024 and i < len(units) - 1:
            size /= 1024
            i += 1
        return ("%.1f %s" if i else "%d %s") % (size, units[i])

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
    server.task_registry = TaskRegistry()
    prefix_text = f" with URL prefix {url_prefix}" if url_prefix else ""
    print(f"Serving on {args.bind}:{args.port}{prefix_text} (threaded) ...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()

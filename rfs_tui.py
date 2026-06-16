"""Textual TUI application for rfs file transfer."""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    Static,
)

from rfs import (
    _download_with_progress,
    _list_remote_dir,
    _remote_delete,
    _remote_mkdir,
    _remote_rename,
    _upload_with_progress,
)


class _CancelledError(Exception):
    """Raised inside progress callbacks when a task is cancelled."""
    pass


def _fmt_size(n: int) -> str:
    """Format byte count to human-readable string."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    else:
        return f"{n / 1024 / 1024 / 1024:.2f} GB"


def _fmt_speed(bps: float) -> str:
    """Format bytes/sec to human-readable speed."""
    if bps < 1024:
        return f"{bps:.0f} B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    else:
        return f"{bps / 1024 / 1024:.1f} MB/s"


class FileItem(ListItem):
    """A list item representing a remote file or directory."""

    def __init__(self, href: str, label_text: str, is_dir: bool) -> None:
        super().__init__()
        self.href = href
        self.label_text = label_text
        self.is_dir = is_dir

    def compose(self) -> ComposeResult:
        icon = "\U0001f4c1" if self.is_dir else "\U0001f4c4"
        yield Label(f"{icon} {self.label_text}")


class TaskItem(ListItem):
    """A list item showing a transfer task with progress."""

    def __init__(self, name: str, direction: str, task_id: str, display_id: int) -> None:
        super().__init__(id=task_id)
        self.task_name = name
        self.direction = direction
        self.task_id_str = task_id
        self.display_id = display_id
        self.is_active = True

    def compose(self) -> ComposeResult:
        arrow = "\u2191" if self.direction == "upload" else "\u2193"
        yield Label(
            f"#{self.display_id} {arrow} {self.task_name}  --",
            id=f"{self.task_id_str}-label",
        )
        yield ProgressBar(total=100, show_eta=False, id=f"{self.task_id_str}-bar")
        yield Label("", id=f"{self.task_id_str}-md5")

    def update_progress(
        self, percent: int, speed: str, size_text: str, finished: bool = False
    ) -> None:
        bar = self.query_one(f"#{self.task_id_str}-bar", ProgressBar)
        bar.update(progress=percent)
        label = self.query_one(f"#{self.task_id_str}-label", Label)
        arrow = "\u2191" if self.direction == "upload" else "\u2193"
        if finished:
            self.is_active = False
            label.update(f"#{self.display_id} {arrow} {self.task_name}  {size_text} \u2714")
        else:
            label.update(f"#{self.display_id} {arrow} {self.task_name}  {size_text}  {speed}")

    def set_md5_result(self, local_md5: str, server_md5: str) -> None:
        """Display MD5 verification result."""
        md5_label = self.query_one(f"#{self.task_id_str}-md5", Label)
        if server_md5:
            if local_md5 == server_md5:
                md5_label.update(f"  MD5 \u2714 {local_md5}")
            else:
                md5_label.update(f"  MD5 \u2718 local:{local_md5} remote:{server_md5}")
        else:
            md5_label.update(f"  MD5: {local_md5} (server did not provide)")

    def mark_cancelled(self) -> None:
        """Mark this task as cancelled in the UI."""
        self.is_active = False
        label = self.query_one(f"#{self.task_id_str}-label", Label)
        arrow = "\u2191" if self.direction == "upload" else "\u2193"
        label.update(f"#{self.display_id} {arrow} {self.task_name}  CANCELLED \u2718")


def _pick_files() -> list[str]:
    """Open system file picker dialog, return list of selected file paths."""
    if platform.system() == "Darwin":
        script = (
            'set theFiles to choose file with multiple selections allowed\n'
            'set output to ""\n'
            'repeat with f in theFiles\n'
            '  set output to output & POSIX path of f & linefeed\n'
            'end repeat\n'
            'return output'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().split("\n") if f]
        return []
    # Fallback: tkinter for Linux/Windows
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    files = filedialog.askopenfilenames(title="Select files to upload")
    root.destroy()
    return list(files)


# ─── Confirmation / Input Dialogs ──────────────────────────────────────────────


class ConfirmDialog(ModalScreen[bool]):
    """Modal confirmation dialog for destructive operations."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", variant="error", id="btn-confirm")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class InputDialog(ModalScreen[str | None]):
    """Modal input dialog."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str, default: str = "") -> None:
        super().__init__()
        self._message = message
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="input-dialog"):
            yield Label(self._message)
            yield Input(value=self._default, id="dialog-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class UploadConflictDialog(ModalScreen[str]):
    """Modal dialog for upload name conflict: overwrite, rename, or cancel.

    Dismisses with:
      "overwrite" - proceed with overwrite
      "cancel" - abort upload
      or a new filename string - rename and upload
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, filename: str, is_dir: bool = False) -> None:
        super().__init__()
        self._filename = filename
        self._is_dir = is_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            if self._is_dir:
                yield Label(
                    f"Remote directory '{self._filename}' has the same name. Cannot overwrite.",
                    id="confirm-message",
                )
            else:
                yield Label(
                    f"Remote file '{self._filename}' already exists.",
                    id="confirm-message",
                )
            yield Input(value=self._filename, id="rename-input")
            with Horizontal(id="confirm-buttons"):
                if not self._is_dir:
                    yield Button("Overwrite", variant="error", id="btn-overwrite")
                yield Button("Rename", variant="primary", id="btn-rename")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-overwrite":
            self.dismiss("overwrite")
        elif event.button.id == "btn-rename":
            new_name = self.query_one("#rename-input", Input).value.strip()
            if new_name and new_name != self._filename:
                self.dismiss(new_name)
            else:
                self.notify("Please enter a different name", severity="warning")
        else:
            self.dismiss("cancel")

    def action_cancel(self) -> None:
        self.dismiss("cancel")


# ─── Main App ──────────────────────────────────────────────────────────────────


class RfsApp(App):
    """Textual TUI for rfs file transfer."""

    CSS = """
    #main-container {
        height: 1fr;
    }
    #file-panel {
        width: 1fr;
        border: solid $primary;
    }
    #task-panel {
        width: 2fr;
        min-width: 30;
        border: solid $secondary;
    }
    #file-panel-title, #task-panel-title {
        text-style: bold;
        padding: 0 1;
        background: $surface;
    }
    #file-list {
        height: 1fr;
    }
    #task-list {
        height: 1fr;
    }
    TaskItem {
        height: 4;
        padding: 0 1;
    }
    #confirm-dialog, #input-dialog {
        align: center middle;
        width: 60;
        max-width: 80%;
        height: auto;
        max-height: 12;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #confirm-message {
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: 3;
        align: center middle;
    }
    #confirm-buttons Button {
        margin: 0 1;
    }
    #help-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("u", "upload", "Upload"),
        ("d", "delete", "Delete"),
        ("m", "mkdir", "Mkdir"),
        ("r", "rename", "Rename"),
        ("x", "cancel_task", "Cancel"),
        (".", "toggle_hidden", "Hidden"),
        ("f5", "refresh", "Refresh"),
        ("backspace", "go_up", "Up dir"),
    ]

    current_path: reactive[str] = reactive("/")
    show_hidden: reactive[bool] = reactive(False)

    def __init__(self, server: str, proxies: dict | None) -> None:
        super().__init__()
        self.server = server
        self.proxies = proxies
        self._task_counter = 0
        self._last_selected_item: FileItem | None = None
        self._last_selected_time: float = 0.0
        self._active_downloads: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}  # task_id -> cancel event
        self._active_tasks: list[str] = []  # ordered list of in-progress task IDs

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-container"):
            with Vertical(id="file-panel"):
                yield Static("Remote Files: /", id="file-panel-title")
                yield ListView(id="file-list")
            with Vertical(id="task-panel"):
                yield Static("Tasks", id="task-panel-title")
                yield ListView(id="task-list")
        yield Static(
            "2xEnter: Open/Download | u: Upload | x: Cancel | .: Hidden | F5: Refresh | d: Del | m: Mkdir | r: Rename | q: Quit",
            id="help-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._load_directory()

    def watch_current_path(self, new_path: str) -> None:
        title = self.query_one("#file-panel-title", Static)
        title.update(f"Remote Files: {new_path}")
        self._load_directory()

    def watch_show_hidden(self, show: bool) -> None:
        self._load_directory()

    @work(thread=True)
    def _load_directory(self) -> None:
        entries = _list_remote_dir(
            self.server, self.proxies, self.current_path.strip("/"),
            show_hidden=self.show_hidden,
        )
        self.call_from_thread(self._populate_file_list, entries)

    def _populate_file_list(self, entries: list[tuple[str, str]]) -> None:
        file_list = self.query_one("#file-list", ListView)
        file_list.clear()
        self._last_selected_item = None
        for href, display_name in entries:
            is_dir = href.endswith("/")
            file_list.append(FileItem(href=href, label_text=display_name, is_dir=is_dir))

    def _get_highlighted_item(self) -> FileItem | None:
        """Get the currently highlighted file item."""
        file_list = self.query_one("#file-list", ListView)
        if file_list.highlighted_child is not None:
            item = file_list.highlighted_child
            if isinstance(item, FileItem):
                return item
        return None

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Only handle file list selections, not task list
        if event.list_view.id != "file-list":
            return

        item = event.item
        if not isinstance(item, FileItem):
            return

        now = time.monotonic()
        if (
            self._last_selected_item is item
            and (now - self._last_selected_time) < 1.0
        ):
            self._last_selected_item = None
            self._last_selected_time = 0.0
            if item.is_dir:
                new_path = self.current_path.rstrip("/") + "/" + item.href
                self.current_path = new_path
            else:
                remote_path = self.current_path.rstrip("/") + "/" + item.href
                if remote_path in self._active_downloads:
                    self.notify(f"Already downloading: {item.label_text}", severity="warning")
                else:
                    self._start_download(remote_path)
        else:
            self._last_selected_item = item
            self._last_selected_time = now

    # ─── Actions ───────────────────────────────────────────────────────────────

    def action_go_up(self) -> None:
        if self.current_path == "/":
            return
        parts = self.current_path.rstrip("/").split("/")
        self.current_path = "/".join(parts[:-1]) or "/"

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        state = "ON" if self.show_hidden else "OFF"
        self.notify(f"Show hidden files: {state}", severity="information")

    def action_refresh(self) -> None:
        self._load_directory()
        self.notify("Refreshed", severity="information")

    def action_upload(self) -> None:
        self._pick_and_upload()

    def action_delete(self) -> None:
        item = self._get_highlighted_item()
        if not item:
            self.notify("No file selected", severity="warning")
            return
        if item.is_dir:
            self.notify("Cannot delete directories", severity="warning")
            return
        name = item.label_text
        self.push_screen(
            ConfirmDialog(f"Delete '{name}'? (moves to .Trash)"),
            callback=lambda confirmed: self._handle_delete(confirmed, item),
        )

    def action_mkdir(self) -> None:
        self.push_screen(
            InputDialog("New directory name:"),
            callback=self._handle_mkdir,
        )

    def action_rename(self) -> None:
        item = self._get_highlighted_item()
        if not item:
            self.notify("No file selected", severity="warning")
            return
        old_name = item.href.rstrip("/")
        self.push_screen(
            InputDialog(f"Rename '{old_name}' to:", default=old_name),
            callback=lambda new_name: self._handle_rename(new_name, item),
        )

    def action_cancel_task(self) -> None:
        """Cancel the currently selected task in the task list."""
        task_list = self.query_one("#task-list", ListView)
        item = task_list.highlighted_child
        if not isinstance(item, TaskItem):
            self.notify("No task selected", severity="warning")
            return
        if not item.is_active:
            self.notify("Task already finished", severity="warning")
            return
        task_id = item.task_id_str
        if task_id not in self._cancel_events:
            self.notify("Task is not cancellable", severity="warning")
            return
        self.push_screen(
            ConfirmDialog(f"Cancel task #{item.display_id} '{item.task_name}'?"),
            callback=lambda confirmed: self._handle_cancel_task(confirmed, task_id),
        )

    def _handle_cancel_task(self, confirmed: bool, task_id: str) -> None:
        if not confirmed:
            return
        cancel_event = self._cancel_events.get(task_id)
        if cancel_event:
            cancel_event.set()
            if task_id in self._active_tasks:
                self._active_tasks.remove(task_id)
            try:
                task_widget = self.query_one(f"#{task_id}", TaskItem)
                task_widget.mark_cancelled()
            except Exception:
                pass
            self.notify("Task cancelled", severity="warning")

    # ─── Action Handlers ───────────────────────────────────────────────────────

    def _handle_delete(self, confirmed: bool, item: FileItem) -> None:
        if not confirmed:
            return
        remote_path = self.current_path.rstrip("/") + "/" + item.href
        self._do_delete(remote_path)

    @work(thread=True)
    def _do_delete(self, remote_path: str) -> None:
        try:
            resp = _remote_delete(self.server, self.proxies, remote_path)
            data = resp.json()
            if data.get("ok"):
                self.call_from_thread(
                    self.notify, f"Deleted: {os.path.basename(remote_path)}", severity="information"
                )
                self.call_from_thread(self._reload_current_dir)
            else:
                self.call_from_thread(
                    self.notify, f"Delete failed: {data.get('error')}", severity="error"
                )
        except Exception as e:
            self.call_from_thread(self.notify, f"Delete failed: {e}", severity="error")

    def _handle_mkdir(self, name: str | None) -> None:
        if not name:
            return
        remote_path = self.current_path.rstrip("/") + "/" + name
        self._do_mkdir(remote_path)

    @work(thread=True)
    def _do_mkdir(self, remote_path: str) -> None:
        try:
            resp = _remote_mkdir(self.server, self.proxies, remote_path)
            data = resp.json()
            if data.get("ok"):
                self.call_from_thread(
                    self.notify, f"Created: {os.path.basename(remote_path)}", severity="information"
                )
                self.call_from_thread(self._reload_current_dir)
            else:
                self.call_from_thread(
                    self.notify, f"Mkdir failed: {data.get('error')}", severity="error"
                )
        except Exception as e:
            self.call_from_thread(self.notify, f"Mkdir failed: {e}", severity="error")

    def _handle_rename(self, new_name: str | None, item: FileItem) -> None:
        if not new_name:
            return
        old_name = item.href.rstrip("/")
        if new_name == old_name:
            return
        from_path = self.current_path.rstrip("/") + "/" + old_name
        to_path = self.current_path.rstrip("/") + "/" + new_name
        # Confirmation for rename
        self.push_screen(
            ConfirmDialog(f"Rename '{old_name}' -> '{new_name}'?"),
            callback=lambda confirmed: self._do_rename_if_confirmed(confirmed, from_path, to_path),
        )

    def _do_rename_if_confirmed(self, confirmed: bool, from_path: str, to_path: str) -> None:
        if not confirmed:
            return
        self._do_rename(from_path, to_path)

    @work(thread=True)
    def _do_rename(self, from_path: str, to_path: str) -> None:
        try:
            resp = _remote_rename(self.server, self.proxies, from_path, to_path)
            data = resp.json()
            if data.get("ok"):
                self.call_from_thread(
                    self.notify,
                    f"Renamed: {os.path.basename(from_path)} -> {os.path.basename(to_path)}",
                    severity="information",
                )
                self.call_from_thread(self._reload_current_dir)
            else:
                self.call_from_thread(
                    self.notify, f"Rename failed: {data.get('error')}", severity="error"
                )
        except Exception as e:
            self.call_from_thread(self.notify, f"Rename failed: {e}", severity="error")

    # ─── Upload / Download ─────────────────────────────────────────────────────

    @work(thread=True)
    def _pick_and_upload(self) -> None:
        files = _pick_files()
        if not files:
            return
        # Get remote directory listing to check for conflicts
        entries = _list_remote_dir(
            self.server, self.proxies, self.current_path.strip("/"),
            show_hidden=self.show_hidden,
        )
        # Map name -> is_dir for conflict detection
        remote_entries = {}
        for href, name in entries:
            remote_entries[name.rstrip("/")] = href.endswith("/")
        self.call_from_thread(self._process_upload_queue, files, remote_entries)

    def _process_upload_queue(self, files: list[str], remote_entries: dict[str, bool]) -> None:
        """Process upload queue, checking for conflicts one file at a time."""
        self._pending_uploads = list(files)
        self._pending_remote_entries = remote_entries
        self._process_next_upload()

    def _process_next_upload(self) -> None:
        """Process the next file in the pending upload queue."""
        if not self._pending_uploads:
            return
        local_file = self._pending_uploads.pop(0)
        filename = os.path.basename(local_file)
        if filename in self._pending_remote_entries:
            is_dir = self._pending_remote_entries[filename]
            self.push_screen(
                UploadConflictDialog(filename, is_dir=is_dir),
                callback=lambda result: self._handle_upload_conflict(result, local_file),
            )
        else:
            self._start_upload(local_file)
            self._process_next_upload()

    def _handle_upload_conflict(self, result: str, local_file: str) -> None:
        """Handle user choice from upload conflict dialog."""
        if result == "cancel":
            self._process_next_upload()
        elif result == "overwrite":
            self._start_upload(local_file, overwrite=True)
            self._process_next_upload()
        else:
            # result is the new filename — upload with rename
            self._start_upload(local_file, rename_to=result)
            self._process_next_upload()

    def _next_task_id(self) -> tuple[str, int]:
        self._task_counter += 1
        return f"task-{self._task_counter}", self._task_counter

    @work(thread=True)
    def _start_download(self, remote_path: str) -> None:
        filename = os.path.basename(remote_path)
        task_id, display_id = self._next_task_id()

        cancel_event = threading.Event()
        self._cancel_events[task_id] = cancel_event
        self._active_tasks.append(task_id)
        self._active_downloads.add(remote_path)

        task_widget = TaskItem(name=filename, direction="download", task_id=task_id, display_id=display_id)
        self.call_from_thread(self._add_task_widget, task_widget)

        local_path = os.path.join(os.getcwd(), filename)

        start_time = time.monotonic()
        last_update_time = [start_time]

        def on_progress(downloaded: int, total: int) -> None:
            if cancel_event.is_set():
                raise _CancelledError()

            now = time.monotonic()
            if now - last_update_time[0] < 0.1 and downloaded < total:
                return
            last_update_time[0] = now

            elapsed = now - start_time
            speed = downloaded / elapsed if elapsed > 0 else 0
            speed_str = _fmt_speed(speed)
            size_str = f"{_fmt_size(downloaded)}/{_fmt_size(total)}" if total > 0 else _fmt_size(downloaded)
            percent = min(int(downloaded * 100 / total), 100) if total > 0 else 0
            self.call_from_thread(
                self._update_task_progress, task_id, percent, speed_str, size_str, False
            )

        try:
            _, local_md5, server_md5 = _download_with_progress(
                self.server, self.proxies, remote_path, local_path, on_progress
            )
            elapsed = time.monotonic() - start_time
            total_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            avg_speed = total_size / elapsed if elapsed > 0 else 0
            final_size_str = _fmt_size(total_size)
            self.call_from_thread(
                self._update_task_progress, task_id, 100, _fmt_speed(avg_speed), final_size_str, True
            )
            self.call_from_thread(self._set_task_md5, task_id, local_md5, server_md5)
            md5_msg = f" MD5:{local_md5[:8]}" if server_md5 else ""
            self.call_from_thread(
                self.notify, f"Downloaded: {filename}{md5_msg}", severity="information"
            )
        except _CancelledError:
            # Remove partial file
            if os.path.exists(local_path):
                os.remove(local_path)
        except ValueError as e:
            self.call_from_thread(
                self.notify, f"MD5 MISMATCH: {e}", severity="error"
            )
        except Exception as e:
            if not cancel_event.is_set():
                self.call_from_thread(
                    self.notify, f"Download failed: {e}", severity="error"
                )
        finally:
            self._active_downloads.discard(remote_path)
            self._cancel_events.pop(task_id, None)
            if task_id in self._active_tasks:
                self._active_tasks.remove(task_id)

    @work(thread=True)
    def _start_upload(self, local_file: str, rename_to: str | None = None, overwrite: bool = False) -> None:
        filename = rename_to or os.path.basename(local_file)
        task_id, display_id = self._next_task_id()

        cancel_event = threading.Event()
        self._cancel_events[task_id] = cancel_event
        self._active_tasks.append(task_id)

        task_widget = TaskItem(name=filename, direction="upload", task_id=task_id, display_id=display_id)
        self.call_from_thread(self._add_task_widget, task_widget)

        remote_dir = self.current_path
        start_time = time.monotonic()
        last_update_time = [start_time]

        def on_progress(uploaded: int, total: int) -> None:
            if cancel_event.is_set():
                raise _CancelledError()

            now = time.monotonic()
            if now - last_update_time[0] < 0.1 and uploaded < total:
                return
            last_update_time[0] = now

            elapsed = now - start_time
            speed = uploaded / elapsed if elapsed > 0 else 0
            speed_str = _fmt_speed(speed)
            size_str = f"{_fmt_size(uploaded)}/{_fmt_size(total)}" if total > 0 else _fmt_size(uploaded)
            percent = min(int(uploaded * 100 / total), 100) if total > 0 else 0
            self.call_from_thread(
                self._update_task_progress, task_id, percent, speed_str, size_str, False
            )

        try:
            _, local_md5, server_md5 = _upload_with_progress(
                self.server, self.proxies, local_file, remote_dir, on_progress,
                remote_name=rename_to, overwrite=overwrite,
            )
            elapsed = time.monotonic() - start_time
            total_size = os.path.getsize(local_file)
            avg_speed = total_size / elapsed if elapsed > 0 else 0
            final_size_str = _fmt_size(total_size)
            self.call_from_thread(
                self._update_task_progress, task_id, 100, _fmt_speed(avg_speed), final_size_str, True
            )
            self.call_from_thread(self._set_task_md5, task_id, local_md5, server_md5)
            md5_msg = f" MD5:{local_md5[:8]}" if server_md5 else ""
            self.call_from_thread(
                self.notify, f"Uploaded: {filename}{md5_msg}", severity="information"
            )
            self.call_from_thread(self._reload_current_dir)
        except _CancelledError:
            pass
        except ValueError as e:
            self.call_from_thread(
                self.notify, f"MD5 MISMATCH: {e}", severity="error"
            )
        except Exception as e:
            if not cancel_event.is_set():
                self.call_from_thread(
                    self.notify, f"Upload failed: {e}", severity="error"
                )
        finally:
            self._cancel_events.pop(task_id, None)
            if task_id in self._active_tasks:
                self._active_tasks.remove(task_id)

    # ─── Widget Helpers ────────────────────────────────────────────────────────

    def _add_task_widget(self, task_widget: TaskItem) -> None:
        task_list = self.query_one("#task-list", ListView)
        task_list.append(task_widget)

    def _update_task_progress(
        self, task_id: str, percent: int, speed: str, size_text: str, finished: bool
    ) -> None:
        try:
            task_widget = self.query_one(f"#{task_id}", TaskItem)
            task_widget.update_progress(percent, speed, size_text, finished)
        except Exception:
            pass

    def _set_task_md5(self, task_id: str, local_md5: str, server_md5: str) -> None:
        try:
            task_widget = self.query_one(f"#{task_id}", TaskItem)
            task_widget.set_md5_result(local_md5, server_md5)
        except Exception:
            pass

    def _reload_current_dir(self) -> None:
        self._load_directory()

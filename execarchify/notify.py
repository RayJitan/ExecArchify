"""Optional native Windows notifications. Each event hints that Git state needs reconciling."""

from __future__ import annotations

import os
import struct
import threading
import time
from pathlib import Path

from .gitio import GitError, git_bytes
from .model import Workspace


def _git_metadata(relative):
    name = relative.as_posix().casefold()
    return name in {
        "head",
        "index",
        "packed-refs",
        "shallow",
        "config",
        "config.worktree",
        "info/exclude",
        "info/sparse-checkout",
        "refs",
        "reftable",
    } or name.startswith(("refs/", "reftable/"))


def _relevant(path, workspace, common):
    if path.is_relative_to(workspace.database.parent):
        return False
    for gitdir in (workspace.gitdir, common):
        if path.is_relative_to(gitdir):
            return _git_metadata(path.relative_to(gitdir))
    if not path.is_relative_to(workspace.root):
        return False
    relative = path.relative_to(workspace.root)
    if any(part.casefold() == ".git" for part in relative.parts):
        return (
            relative.name.casefold() == ".git"
        )  # New/deleted repository boundary or gitfile.
    return True


def _needs_scan(raw, directory, workspace, common):
    """Decode the aligned variable-length FILE_NOTIFY_INFORMATION chain; overflow means rescan."""
    position = 0
    while position + 12 <= len(raw):
        next_record, _, length = struct.unpack_from("<III", raw, position)
        start = position + 12
        end = start + length
        if not length or length % 2 or end > len(raw):
            return True
        name = raw[start:end].decode("utf-16-le", "surrogatepass")
        if _relevant(directory / name, workspace, common):
            return True
        if not next_record:
            return False
        if next_record < 12 + length or next_record % 4:
            return True
        position += next_record
    return True


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("internal", ctypes.c_size_t),
            ("internal_high", ctypes.c_size_t),
            ("offset", wintypes.DWORD),
            ("offset_high", wintypes.DWORD),
            ("event", wintypes.HANDLE),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    def _function(name, result, *args):
        function = getattr(_kernel, name)
        function.argtypes = args
        function.restype = result
        return function

    _handle, _dword, _pointer, _bool = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.BOOL,
    )
    _open = _function(
        "CreateFileW",
        _handle,
        wintypes.LPCWSTR,
        _dword,
        _dword,
        _pointer,
        _dword,
        _dword,
        _handle,
    )
    _event = _function(
        "CreateEventW", _handle, _pointer, _bool, _bool, wintypes.LPCWSTR
    )
    _reset = _function("ResetEvent", _bool, _handle)
    _read = _function(
        "ReadDirectoryChangesW",
        _bool,
        _handle,
        _pointer,
        _dword,
        _bool,
        _dword,
        _pointer,
        _pointer,
        _pointer,
    )
    _result = _function(
        "GetOverlappedResult", _bool, _handle, _pointer, _pointer, _bool
    )
    _cancel = _function("CancelIoEx", _bool, _handle, _pointer)
    _close = _function("CloseHandle", _bool, _handle)
    _FILTER = (
        0x01 | 0x02 | 0x04 | 0x08 | 0x10 | 0x100
    )  # Names, attributes, size, write time, ACLs
    _BUFFER_SIZE = 64 * 1024  # Also supported on SMB.
    _NOTIFY_ENUM_DIR = 1022


class _Directory(threading.Thread):
    """One outstanding overlapped read. Cancellation and rearming share a lock."""

    def __init__(self, path, workspace, common, wake):
        super().__init__(
            target=self._observe, daemon=True, name="execarchify-directory-events"
        )
        self.path, self.workspace, self.common, self.wake = (
            path,
            workspace,
            common,
            wake,
        )
        self.guard = threading.Lock()
        self.stopping = False
        self.buffer = (ctypes.c_uint32 * (_BUFFER_SIZE // 4))()
        # FILE_LIST_DIRECTORY, share read/write/delete, OPEN_EXISTING,
        # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED.
        self.directory = _open(str(path), 1, 7, None, 3, 0x02000000 | 0x40000000, None)
        if self.directory == _handle(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.completion = _event(None, True, False, None)
        try:
            if not self.completion:
                raise ctypes.WinError(ctypes.get_last_error())
            self._arm()
        except BaseException:
            self._close_handles()
            raise

    def _arm(self):
        self.operation = _Overlapped()
        self.operation.event = self.completion
        _reset(self.completion)
        if not _read(
            self.directory,
            self.buffer,
            _BUFFER_SIZE,
            True,
            _FILTER,
            None,
            ctypes.byref(self.operation),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def _close_handles(self):
        if self.completion:
            _close(self.completion)
        _close(self.directory)
        self.directory = self.completion = None

    def stop(self):
        with self.guard:
            self.stopping = True
            if self.directory:
                _cancel(self.directory, ctypes.byref(self.operation))

    def finish_unstarted(self):
        self.stop()
        count = _dword()
        _result(self.directory, ctypes.byref(self.operation), ctypes.byref(count), True)
        self._close_handles()

    def _observe(self):
        try:
            while True:
                count = _dword()
                ok = _result(
                    self.directory,
                    ctypes.byref(self.operation),
                    ctypes.byref(count),
                    True,
                )
                error = 0 if ok else ctypes.get_last_error()
                with self.guard:
                    if self.stopping:
                        return
                    if error and error != _NOTIFY_ENUM_DIR:
                        self.wake.set()
                        return
                    raw = ctypes.string_at(self.buffer, count.value) if ok else b""
                    if _needs_scan(raw, self.path, self.workspace, self.common):
                        self.wake.set()
                    # The handle queues changes between reads. An overflow causes a
                    # full Git reconciliation even if individual names were dropped.
                    self._arm()
        except OSError:
            self.wake.set()
        finally:
            with self.guard:
                self._close_handles()


class _Watches:
    def __init__(self):
        self.readers = []

    def stop(self):
        for reader in self.readers:
            reader.stop()

    def join(self, timeout=None):
        until = time.monotonic() + timeout if timeout is not None else None
        for reader in self.readers:
            reader.join(None if until is None else max(0, until - time.monotonic()))

    def is_alive(self):
        return all(reader.is_alive() for reader in self.readers)


def native_events(workspace: Workspace, event: threading.Event):
    """Return a started stop()/join() source or None when polling is needed."""
    if os.name != "nt":
        return None
    watches = _Watches()
    try:
        common_raw = git_bytes(
            workspace.root, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        common = Path(os.fsdecode(common_raw.removesuffix(b"\n"))).resolve()
        paths = {workspace.root, workspace.gitdir, common}
        for path in sorted(paths):
            if any(path != other and path.is_relative_to(other) for other in paths):
                continue
            reader = _Directory(path, workspace, common, event)
            try:
                reader.start()
            except RuntimeError:
                reader.finish_unstarted()
                raise
            watches.readers.append(reader)
        return watches
    except (GitError, OSError, RuntimeError):
        watches.stop()
        watches.join(timeout=2)
        return None

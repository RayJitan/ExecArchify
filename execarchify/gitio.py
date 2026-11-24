"""Read Git worktrees and immutable objects using only the local Git executable."""

from __future__ import annotations

import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .model import Workspace


class GitError(ValueError):
    """The selected Git source or object could not be read."""


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    # A caller may itself be a Git hook. Never inherit its repository selection.
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
        "GIT_NAMESPACE",
        "GIT_QUARANTINE_PATH",
    ):
        env.pop(key, None)
    env.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _run(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        env=_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )


def git_bytes(path: Path, *args: str) -> bytes:
    result = _run(path, *args)
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(message or f"git {args[0]} failed at {path}")
    return result.stdout


def _git_path(raw: bytes) -> Path:
    # Git emits UTF-8 paths on Windows and the filesystem encoding on POSIX.
    return Path(os.fsdecode(raw.removesuffix(b"\n"))).resolve()


def discover(path: str | Path) -> Workspace:
    location = Path(path).expanduser()
    if location.is_file() or (location.is_symlink() and not location.is_dir()):
        location = location.parent
    location = location.resolve()
    root = _git_path(git_bytes(location, "rev-parse", "--show-toplevel"))
    gitdir = _git_path(git_bytes(root, "rev-parse", "--absolute-git-dir"))
    return Workspace(root, gitdir, gitdir / "execarchify" / "index.sqlite3")


def exact_git_root(path: Path) -> bool:
    """Do not let Git walk up from an empty submodule into its superproject."""
    if not path.is_dir():
        return False
    result = _run(path, "rev-parse", "--show-toplevel")
    if result.returncode == 0:
        return _git_path(result.stdout) == path
    # An explicitly selected local bare mirror is also a valid object source.
    bare = _run(path, "rev-parse", "--is-bare-repository")
    return (
        bare.returncode == 0
        and bare.stdout.strip() == b"true"
        and _git_path(git_bytes(path, "rev-parse", "--absolute-git-dir")) == path
    )


def commit_at(path: Path, revision: str = "HEAD") -> str:
    result = _run(
        path,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        revision + "^{commit}",
    )
    if result.returncode == 1:
        return ""
    if result.returncode:
        raise GitError(
            result.stderr.decode("utf-8", "replace").strip()
            or f"could not resolve {revision!r} in {path}"
        )
    return result.stdout.decode("ascii").strip()


def in_scope_path(raw: bytes) -> str | None:
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    parts = name.split("/")
    if any(part.casefold() in {"", ".", "..", ".git"} for part in parts):
        return None
    if os.name == "nt" and ("\\" in name or ":" in name):
        return None
    return name


def worktree_entries(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Map eligible paths to index blob IDs, plus gitlinks from the current index."""
    rows = git_bytes(
        path,
        "ls-files",
        "-z",
        "-t",
        "--cached",
        "--stage",
        "--others",
        "--exclude-standard",
    )
    files: dict[str, str] = {}
    links: dict[str, str] = {}
    for row in rows.split(b"\0"):
        if not row:
            continue
        record = row[
            2:
        ]  # -t tags untracked records with '? ', staged records separately.
        if row.startswith(b"? "):
            name = in_scope_path(record)
            if name is not None:
                files[name] = ""
            continue
        header, _, raw_name = record.partition(b"\t")
        mode, oid, stage = header.split()
        name = in_scope_path(raw_name)
        if name is None:
            continue
        if mode == b"160000":
            # A conflicted gitlink has no single expected revision.
            links[name] = oid.decode("ascii") if stage == b"0" else ""
        elif mode != b"120000":
            files[name] = oid.decode("ascii")
    for name in links:
        files.pop(name, None)
    return files, links


def tree_entries(path: Path, commit: str):
    """Yield regular file paths, blob IDs, and byte sizes at one fixed commit."""
    rows = git_bytes(path, "ls-tree", "-r", "-l", "-z", "--full-tree", commit)
    for row in rows.split(b"\0"):
        if not row:
            continue
        header, _, raw_name = row.partition(b"\t")
        mode, kind, oid, size = header.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            continue
        name = in_scope_path(raw_name)
        if name is not None:
            if not size.isdigit():
                raise GitError(f"blob unavailable for {name!r} at {commit}")
            yield name, oid.decode("ascii"), int(size)


@contextmanager
def blob_reader(path: Path):
    """Stream blobs using one Git process, with at most one response in memory."""
    with subprocess.Popen(
        ["git", "-C", str(path), "cat-file", "--batch"],
        env=_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:

        def read(oid: str, expected_size: int) -> bytes:
            process.stdin.write(oid.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            if header != [
                oid.encode("ascii"),
                b"blob",
                str(expected_size).encode("ascii"),
            ]:
                raise GitError(f"blob {oid} is unavailable")
            raw = process.stdout.read(expected_size)
            if len(raw) != expected_size or process.stdout.read(1) != b"\n":
                raise GitError(f"incomplete blob {oid}")
            return raw

        try:
            yield read
            process.stdin.close()
            if process.wait():
                raise GitError(f"could not finish reading Git objects in {path}")
        finally:
            if process.poll() is None:
                process.kill()


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_int64) for name in ("creation", "access", "write", "change")
        ]
        _fields_.append(("attributes", wintypes.DWORD))

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _open_attributes = _kernel.CreateFileW
    _open_attributes.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _open_attributes.restype = wintypes.HANDLE
    _get_info = _kernel.GetFileInformationByHandleEx
    _get_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _get_info.restype = wintypes.BOOL
    _close_handle = _kernel.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL


def change_time(path: Path, info: os.stat_result) -> int | None:
    """Windows stat exposes creation time as ctime. NTFS change time also catches restored mtimes."""
    if os.name != "nt":
        return info.st_ctime_ns
    # Read attributes only. Share reads, writes and deletes. Open the reparse point itself.
    handle = _open_attributes(str(path), 0x80, 7, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        return None
    try:
        basic = _FileBasicInfo()
        if _get_info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            return basic.change or None
        return None
    finally:
        _close_handle(handle)


class SourceDirectories:
    """Check each ancestor once per scan, including Windows directory junctions."""

    def __init__(self, root: Path):
        self.root = root
        self.allowed = {root: True}

    def contains(self, directory: Path) -> bool:
        if not directory.is_relative_to(self.root):
            return False
        if directory not in self.allowed:
            allowed = self.contains(directory.parent)
            if allowed:
                try:
                    info = directory.lstat()
                    allowed = (
                        stat.S_ISDIR(info.st_mode)
                        and not is_link(info)
                        and not os.path.lexists(directory / ".git")
                    )
                    if (
                        allowed
                        and (directory / "HEAD").is_file()
                        and (directory / "objects").is_dir()
                        and (directory / "refs").is_dir()
                    ):
                        allowed = not exact_git_root(directory)
                except (FileNotFoundError, NotADirectoryError, PermissionError):
                    allowed = False
            self.allowed[directory] = allowed
        return self.allowed[directory]

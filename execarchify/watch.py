"""Worktree-local observer, single-owner OS lock, and safe cooperative stop."""

from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .index import synchronize


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "observer.lock"
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            def acquire():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle, fcntl.LOCK_UN)

        try:
            acquire()
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            release()


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def status(workspace):
    directory = workspace.database.parent
    with locked(directory) as available:
        state = read_json(directory / "observer.json")
        return {**state, "running": not available}


def event_source(workspace, event):
    from .notify import native_events

    observer = native_events(workspace, event)
    if observer is not None:
        return observer, "windows-events"
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None, "poll"

    class Changed(FileSystemEventHandler):
        def on_any_event(self, entry):
            if entry.event_type not in (
                "created",
                "deleted",
                "modified",
                "moved",
                "closed",
            ) or (entry.is_directory and entry.event_type == "modified"):
                return
            paths = [entry.src_path, getattr(entry, "dest_path", "")]
            for name in filter(None, paths):
                parts = Path(name).parts
                if ".git" in parts:
                    continue
                event.set()
                return

    observer = Observer()
    observer.schedule(Changed(), str(workspace.root), recursive=True)
    try:
        observer.start()
        return observer, "filesystem-events"
    except (OSError, RuntimeError):
        return None, "poll"


def observe(workspace, interval=2.0):
    if not 0.15 <= interval <= 3600:
        raise ValueError("interval must be between 0.15 and 3600 seconds")
    directory = workspace.database.parent
    with locked(directory) as acquired:
        if not acquired:
            return
        token = uuid.uuid4().hex
        changed = threading.Event()
        observer, mode = event_source(workspace, changed)
        state = {
            "token": token,
            "pid": os.getpid(),
            "started": int(time.time()),
            "last_error": None,
            "mode": mode,
            "interval": interval,
            "repair_interval": max(15, interval) if observer else interval,
        }
        state_path = directory / "observer.json"
        write_json(state_path, state)
        stop_path = directory / "observer-stop.json"
        try:
            next_scan = 0.0
            while workspace.root.is_dir() and workspace.gitdir.is_dir():
                if read_json(stop_path).get("token") == token:
                    break
                if (
                    observer is not None
                    and not observer.is_alive()
                    and state["mode"] != "poll"
                ):
                    state.update(mode="poll", repair_interval=interval)
                    write_json(state_path, state)
                    next_scan = 0
                remaining = next_scan - time.monotonic()
                if remaining > 0 and not changed.wait(min(0.25, remaining)):
                    continue
                if changed.is_set():
                    time.sleep(0.15)
                    changed.clear()
                try:
                    synchronize(workspace)
                    error = None
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                if error != state["last_error"]:
                    state["last_error"] = error
                    write_json(state_path, state)
                next_scan = time.monotonic() + state["repair_interval"]
        finally:
            if observer:
                observer.stop()
                observer.join(timeout=2)
            with suppress(OSError):
                if read_json(stop_path).get("token") == token:
                    stop_path.unlink(missing_ok=True)


def start(workspace, interval=2.0):
    if not 0.15 <= interval <= 3600:
        raise ValueError("interval must be between 0.15 and 3600 seconds")
    existing = status(workspace)
    if existing["running"]:
        return existing
    pkg = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (pkg, env.get("PYTHONPATH"))))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    opts = (
        {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "execarchify",
            "_watch",
            "--workspace",
            str(workspace.root),
            "--interval",
            str(interval),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=env,
        cwd=pkg,
        **opts,
    )
    threading.Thread(
        target=proc.wait, daemon=True, name="execarchify-observer-exit"
    ).start()
    until = time.monotonic() + 8
    while time.monotonic() < until:
        current = status(workspace)
        if (
            current["running"]
            and current.get("token")
            and (
                current.get("token") != existing.get("token")
                or current.get("pid") == proc.pid
            )
        ):
            return current
        if proc.poll() is not None and not current["running"]:
            break
        time.sleep(0.05)
    current = status(workspace)
    if not current["running"]:
        current["last_error"] = (
            "Observer did not start. Run execarchify watch in the foreground for diagnostics."
        )
    return current


def stop(workspace):
    current = status(workspace)
    if current["running"] and current.get("token"):
        write_json(
            workspace.database.parent / "observer-stop.json",
            {"token": current["token"]},
        )
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if not status(workspace)["running"]:
                break
            time.sleep(0.08)
    return status(workspace)

"""The run lock: one ul.py phase at a time per run.

runs\\<id>\\ul.lock.json holds the phase that runs now and the process that runs it. A lock whose process is gone
is stale and removed, so a killed runner never blocks the run. The stage scripts' --standalone mode checks the
same lock, because stages 1, 3 and 5 share the run's worktrees.
"""

from __future__ import annotations

import os

from .core import IS_WINDOWS, now_iso, read_json_safe, to_int, write_json

LOCK_FILE = "ul.lock.json"


def pid_alive(pid) -> bool:
    """Whether a process with this id exists."""
    pid = to_int(pid, -1)
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_path(run_dir: str) -> str:
    return os.path.join(run_dir, LOCK_FILE)


def run_lock(run_dir: str) -> dict:
    """{active; pid; key}: key is the phase that holds the lock. A stale lock (its process is gone) is removed."""
    p = lock_path(run_dir)
    lk = read_json_safe(p)
    if not lk:
        return {"active": False, "pid": None, "key": None}
    if pid_alive(lk.get("pid")):
        return {"active": True, "pid": lk.get("pid"), "key": lk.get("key")}
    release(run_dir)
    return {"active": False, "pid": lk.get("pid"), "key": lk.get("key")}


def acquire(run_dir: str, key: str, pid: int | None = None) -> dict:
    """Takes the lock for a phase. Returns the holder when another live process has it ({active: True}),
    else {active: False} and the lock is now ours."""
    held = run_lock(run_dir)
    if held["active"] and to_int(held["pid"]) != (pid or os.getpid()):
        return held
    write_json(lock_path(run_dir), {"key": key, "pid": pid or os.getpid(), "since": now_iso()})
    return {"active": False, "pid": None, "key": None}


def release(run_dir: str) -> None:
    try:
        os.remove(lock_path(run_dir))
    except OSError:
        pass

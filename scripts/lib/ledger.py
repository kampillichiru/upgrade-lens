"""The ledger and the stage lock of an orchestrated UpgradeLens run.

runs\\<id>\\orchestration\\ledger.json: one entry per attempt of a plan step, in order. Written only by
run-stage.py, next-step.py (skips) and agent-step.py (begin and end). An agent that writes it stops the run.
runs\\<id>\\orchestration\\lock.json: the one stage that runs now, and the process that holds it.
"""

from __future__ import annotations

import os

from .core import IS_WINDOWS, as_list, get, now_iso, read_json_safe, to_int, write_json


# ---------- ledger ----------

def ledger_path(run_dir: str) -> str:
    return os.path.join(run_dir, "orchestration", "ledger.json")


def read_ledger(run_dir: str):
    led = read_json_safe(ledger_path(run_dir))
    if isinstance(led, dict) and not led.get("entries"):
        led["entries"] = []
    return led if isinstance(led, dict) else None


def new_ledger(run_dir: str, run_id: str, config_path: str) -> dict:
    led = {"schemaVersion": 1, "runId": run_id, "config": config_path, "createdAt": now_iso(), "entries": []}
    write_json(ledger_path(run_dir), led)
    return led


def ledger_entry(ledger, key: str):
    """The last entry for a key, or None."""
    hits = [e for e in as_list(get(ledger, "entries")) if e and e.get("key") == key]
    return hits[-1] if hits else None


def key_entries(ledger, key: str, state: str | None = None) -> list[dict]:
    """Every entry (attempt) of a key, optionally only those in one state."""
    return [e for e in as_list(get(ledger, "entries"))
            if e and e.get("key") == key and (state is None or e.get("state") == state)]


def set_ledger_entry(run_dir: str, key: str, kind: str, state: str, reason: str = "", extra: dict | None = None,
                     update: bool = False) -> None:
    """Adds an entry, or with update=True replaces the last entry of the key (the same attempt)."""
    led = read_ledger(run_dir)
    if not led:
        raise RuntimeError(f"No ledger in {run_dir}")
    entries = [e for e in as_list(led.get("entries")) if e]
    now = now_iso()
    attempt = len([e for e in entries if e.get("key") == key]) + 1
    extra = extra or {}
    if update:
        for i in range(len(entries) - 1, -1, -1):
            if entries[i].get("key") == key:
                old = entries[i]
                entry = {"key": key, "kind": kind, "attempt": old.get("attempt"), "state": state, "reason": reason,
                         "startedAt": old.get("startedAt"), "finishedAt": now}
                for k, v in old.items():
                    entry.setdefault(k, v)
                entry.update(extra)
                entries[i] = entry
                break
    else:
        entry = {"key": key, "kind": kind, "attempt": attempt, "state": state, "reason": reason, "startedAt": now,
                 "finishedAt": None if state == "running" else now}
        entry.update(extra)
        entries.append(entry)
    led["entries"] = entries
    write_json(ledger_path(run_dir), led)


# ---------- lock ----------

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
    return os.path.join(run_dir, "orchestration", "lock.json")


def run_lock(run_dir: str) -> dict:
    """The stage lock: {active; pid; key}. A lock whose process is gone is removed (the ledger keeps the
    step as running, and next-step reports the dead runner)."""
    p = lock_path(run_dir)
    lk = read_json_safe(p)
    if not lk:
        return {"active": False, "pid": None, "key": None}
    if pid_alive(lk.get("pid")):
        return {"active": True, "pid": lk.get("pid"), "key": lk.get("key")}
    try:
        os.remove(p)
    except OSError:
        pass
    return {"active": False, "pid": lk.get("pid"), "key": lk.get("key")}

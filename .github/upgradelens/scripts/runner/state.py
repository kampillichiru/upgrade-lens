"""The state of a run, one folder per phase:

    runs\\<id>\\<phase>\\status.json    state, exit code, each job's state and reason, the input hashes it ran on
    runs\\<id>\\<phase>\\quality.json   green / amber / red with the numbers behind it (runner\\quality.py)
    runs\\<id>\\<phase>\\outputs\\       a copy of what the phase wrote, as it was when the phase ended
    runs\\<id>\\<phase>\\history\\<n>\\   the previous attempt of the phase, moved here when it runs again
    runs\\current.txt                  the run that ul.py uses when --run is left out

The shared working files stay in runs\\<id>\\artifacts\\ (the stage scripts read them there); only the runner writes
them. A phase is stale when a file it read has changed since it ran.
"""

from __future__ import annotations

import os
import shutil

from lib.core import file_sha256, native_path, now_iso, read_json_safe, sort_key, write_json, write_text, read_text
from lib.lock import run_lock

from .registry import PHASES, Phase

STATUS = "status.json"
QUALITY = "quality.json"
CURRENT = "current.txt"
KEEP_ON_ARCHIVE = {"history"}


def phase_dir(run_dir: str, phase: Phase) -> str:
    return os.path.join(run_dir, phase.id)


def read_status(run_dir: str, phase: Phase) -> dict:
    st = read_json_safe(os.path.join(phase_dir(run_dir, phase), STATUS))
    return st if isinstance(st, dict) else {}


def write_status(run_dir: str, phase: Phase, st: dict) -> None:
    st["updatedAt"] = now_iso()
    write_json(os.path.join(phase_dir(run_dir, phase), STATUS), st)


def read_quality(run_dir: str, phase: Phase) -> dict:
    q = read_json_safe(os.path.join(phase_dir(run_dir, phase), QUALITY))
    return q if isinstance(q, dict) else {}


def hash_inputs(run_dir: str, phase: Phase) -> dict:
    """relative path -> SHA-256 of each input that exists now (missing ones are left out)."""
    out = {}
    for rel in phase.hashed_inputs():
        p = os.path.join(run_dir, native_path(rel))
        if os.path.isfile(p):
            out[rel] = file_sha256(p)
    return out


def changed_inputs(run_dir: str, phase: Phase, st: dict) -> list[str]:
    """The inputs that changed, appeared or disappeared since the phase ran (empty = current)."""
    before = st.get("inputs") or {}
    now = hash_inputs(run_dir, phase)
    return sorted(k for k in set(before) | set(now) if before.get(k) != now.get(k))


def missing_needs(run_dir: str, phase: Phase) -> list[str]:
    return [n for n in phase.needs() if not os.path.isfile(os.path.join(run_dir, native_path(n)))]


def phase_state(run_dir: str, phase: Phase) -> dict:
    """What status shows for one phase:
       not-run | running | interrupted | waiting | failed | done | stale | blocked (inputs missing)"""
    st = read_status(run_dir, phase)
    lock = run_lock(run_dir) if os.path.isdir(run_dir) else {"active": False}
    missing = missing_needs(run_dir, phase) if phase.id != "00-setup" else []
    state = st.get("state") or "not-run"
    stale = []
    if state == "running" and not (lock.get("active") and lock.get("key") == phase.id):
        state = "interrupted"
    if state in ("done",):
        stale = changed_inputs(run_dir, phase, st)
        if stale:
            state = "stale"
    if state in ("not-run", "failed", "interrupted") and missing:
        state = "blocked" if state == "not-run" else state
    return {"phase": phase, "state": state, "status": st, "missing": missing, "stale": stale,
            "quality": read_quality(run_dir, phase)}


def archive(run_dir: str, phase: Phase) -> str | None:
    """Moves the previous attempt of a phase (everything but history\\) to history\\<n>\\. Returns that folder."""
    d = phase_dir(run_dir, phase)
    if not os.path.isdir(d) or not os.path.isfile(os.path.join(d, STATUS)):
        return None
    hist = os.path.join(d, "history")
    n = 1
    while os.path.exists(os.path.join(hist, str(n))):
        n += 1
    target = os.path.join(hist, str(n))
    os.makedirs(target, exist_ok=True)
    for name in os.listdir(d):
        if name in KEEP_ON_ARCHIVE:
            continue
        shutil.move(os.path.join(d, name), os.path.join(target, name))
    return target


def snapshot_outputs(run_dir: str, phase: Phase) -> list[str]:
    """Copies what the phase wrote into <phase>\\outputs\\, so a later phase that rewrites a shared file (03-analysis
    rewrites impact-findings.json) never hides what this phase produced."""
    copied = []
    out = os.path.join(phase_dir(run_dir, phase), "outputs")
    for rel in phase.writes():
        src = os.path.join(run_dir, native_path(rel))
        if os.path.isfile(src):
            dst = os.path.join(out, native_path(rel))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    return copied


# ---------- runs ----------

def list_runs(runs_dir: str) -> list[str]:
    if not os.path.isdir(runs_dir):
        return []
    names = [n for n in os.listdir(runs_dir) if os.path.isfile(os.path.join(runs_dir, n, "status.json"))]
    return sorted(names, key=sort_key, reverse=True)


def current_run(runs_dir: str) -> str | None:
    p = os.path.join(runs_dir, CURRENT)
    if os.path.isfile(p):
        rid = read_text(p).strip()
        if rid and os.path.isfile(os.path.join(runs_dir, rid, "status.json")):
            return rid
    runs = list_runs(runs_dir)
    return runs[0] if runs else None


def set_current(runs_dir: str, run_id: str) -> None:
    write_text(os.path.join(runs_dir, CURRENT), run_id + "\n")


def all_states(run_dir: str) -> list[dict]:
    return [phase_state(run_dir, p) for p in PHASES]

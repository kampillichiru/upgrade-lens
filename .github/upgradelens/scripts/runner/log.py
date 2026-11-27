"""Logging of a phase: what happens, and why.

    runs\\<id>\\<phase>\\log.txt        the full story of the phase, one line per event:
                                       2026-11-27T10:31:04 INFO  02-impact/rules  done 3.1s exit 0 ...
    runs\\<id>\\<phase>\\events.jsonl   the same events as JSON (read by status, why and trace)
    runs\\<id>\\<phase>\\tools\\<job>.log the raw output of the job's stage script (Maven, git, the JVMs)
    runs\\<id>\\run.log                 the run's timeline: one line per phase or job start, end, skip, wait, reject

Rules: every decision is logged with its reason (skip, stale, reject, reuse); every line names its phase and job;
inputs and outputs are logged with hashes or counts; secrets are masked in every log line and in the raw tool logs.
"""

from __future__ import annotations

import datetime
import json
import os
import re

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

# Masks secrets before anything is written: Maven settings passwords, tokens, URLs with credentials.
_SECRET_PATTERNS = (
    (re.compile(r"(<(password|passphrase|privateKey)>)(.*?)(</\2>)", re.I), r"\1***\4"),
    (re.compile(r"(?i)\b([\w.-]*(password|passwd|pwd|token|secret|apikey|api[_-]key)[\w.-]*)(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"),
     r"\1\3***"),
    (re.compile(r"(?i)\b(authorization:\s*(bearer|basic)\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "***"),
)


def redact(text: str) -> str:
    out = str(text)
    for rx, repl in _SECRET_PATTERNS:
        out = rx.sub(repl, out)
    return out


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _append(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


class PhaseLog:
    """The log of one phase of one run. Timeline events (start, done, skip, wait, reject, fail) also go to run.log."""

    TIMELINE = {"start", "done", "skip", "wait", "reject", "fail", "reuse", "accept", "stale"}

    def __init__(self, run_dir: str, phase_id: str, verbose: bool = False, echo=None):
        self.run_dir = run_dir
        self.phase_id = phase_id
        self.verbose = verbose
        self.echo = echo        # optional callable(line) for --follow in the foreground
        self.dir = os.path.join(run_dir, phase_id)
        self.counts = {lvl: 0 for lvl in LEVELS}

    @property
    def path(self) -> str:
        return os.path.join(self.dir, "log.txt")

    def event(self, level: str, job: str, message: str, kind: str = "", **data) -> None:
        level = level.upper()
        self.counts[level] = self.counts.get(level, 0) + 1
        if level == "DEBUG" and not self.verbose:
            return
        where = f"{self.phase_id}/{job}" if job else self.phase_id
        text = redact(message)
        line = f"{_now()} {level:<5} {where:<26} {text}"
        _append(self.path, line)
        rec = {"at": _now(), "level": level, "phase": self.phase_id, "job": job, "kind": kind, "message": text}
        if data:
            rec["data"] = json.loads(redact(json.dumps(data, ensure_ascii=False, default=str)))
        _append(os.path.join(self.dir, "events.jsonl"), json.dumps(rec, ensure_ascii=False))
        if kind in self.TIMELINE or level == "ERROR":
            _append(os.path.join(self.run_dir, "run.log"), line)
        if self.echo:
            self.echo(line)

    def debug(self, job: str, message: str, kind: str = "", **data) -> None:
        self.event("DEBUG", job, message, kind, **data)

    def info(self, job: str, message: str, kind: str = "", **data) -> None:
        self.event("INFO", job, message, kind, **data)

    def warn(self, job: str, message: str, kind: str = "", **data) -> None:
        self.event("WARN", job, message, kind, **data)

    def error(self, job: str, message: str, kind: str = "", **data) -> None:
        self.event("ERROR", job, message, kind, **data)


def read_events(run_dir: str, phase_id: str) -> list[dict]:
    p = os.path.join(run_dir, phase_id, "events.jsonl")
    out = []
    if not os.path.isfile(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out

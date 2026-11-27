"""Runs one phase of one run.

    1. lock the run (one phase at a time; a stale lock from a killed process is cleared)
    2. a phase that waits for its agent resumes at that job; any other run moves the previous attempt to history\\
    3. check that every input the phase needs is there, and record their hashes (staleness)
    4. run the jobs in order; a job whose condition is not met is skipped with the reason
    5. stop at the first failed job (its exit code is the phase's) or at an agent job that waits (exit 20)
    6. on success: the quality score, a copy of the outputs, and a console summary of at most ~10 lines

The console is short on purpose (Copilot agents read it: fewer tokens); the full story is in the phase log.
"""

from __future__ import annotations

import datetime
import os
import time
import traceback

from lib.core import native_path, now_iso, read_json_safe, read_text, s, tail, write_json, write_text
from lib.lock import acquire, release
from lib.stages import exit_meaning

from .jobs import BLOCKED, OK, TOOL, UL_EXIT, USAGE, WAITING, Ctx, Result, condition, run_job, run_setup_script, skip
from .log import PhaseLog
from .quality import score
from .registry import PHASES, UL, Phase, phase_after, producer
from .state import (QUALITY, archive, hash_inputs, missing_needs, phase_dir, read_status, set_current, snapshot_outputs,
                    write_status)

CONSOLE = "console.txt"


def _rel(run_dir: str, path: str) -> str:
    try:
        return os.path.relpath(path, os.path.dirname(os.path.dirname(run_dir)))
    except ValueError:
        return path


def _secs(st: dict) -> str:
    try:
        a = datetime.datetime.fromisoformat(s(st.get("startedAt")))
        b = datetime.datetime.fromisoformat(s(st.get("finishedAt")))
        return f"{(b - a).total_seconds():.0f}s"
    except ValueError:
        return "?"


def console_lines(run_dir: str, phase: Phase, st: dict, q: dict | None) -> list[str]:
    """The phase's console summary: what happened, the one thing to look at, and the next command."""
    jobs = st.get("jobs") or {}
    state, code = st.get("state"), st.get("exitCode", 0)
    run_id = os.path.basename(run_dir)
    job_line = "  jobs: " + " | ".join(f"{j.name} {jobs.get(j.name, {}).get('state', '-')}" for j in phase.jobs)
    log = os.path.join(phase_dir(run_dir, phase), "log.txt")
    if state == "waiting":
        name = next((n for n, r in jobs.items() if r.get("state") == "waiting"), "")
        rec = jobs.get(name, {})
        return [f"{phase.id}: WAITING for agent job '{name}' (attempt {rec.get('attempt', 1)})", job_line,
                f"  brief:  {_rel(run_dir, s(rec.get('brief')))}",
                f"  answer: {_rel(run_dir, s(rec.get('answer')))}",
                f"  then:   {phase.command} --run {run_id}   (checks the answer and continues)",
                "  exit 20 = waiting for the agent"]
    if state == "failed":
        name = next((n for n, r in jobs.items() if r.get("state") == "failed"), "")
        rec = jobs.get(name, {})
        lines = [f"{phase.id}: FAILED at job '{name}' (exit {code}: {UL_EXIT.get(code, '')})", job_line,
                 f"  reason: {s(rec.get('reason') or st.get('reason'))[:300]}"]
        for x in (rec.get("tail") or [])[-3:]:
            lines.append(f"  | {s(x)[:200]}")
        lines += [f"  log:  {_rel(run_dir, log)}", f"  why:  {UL} why {phase.name} --run {run_id}"]
        return lines
    head = f"{phase.id}: done in {_secs(st)}"
    if phase.id == "06-report" and state == "done":
        v = read_json_safe(os.path.join(run_dir, "verdict.json")) or {}
        head += f" (verdict: {v.get('verdict', '?')}: {len(v.get('differences') or [])} difference(s))"
    if q:
        head += f"  [quality: {q.get('status')}]"
    lines = [head, job_line]
    for n in (q or {}).get("notes", [])[:3]:
        lines.append(f"  ! {n}")
    lines.append(f"  log: {_rel(run_dir, log)}")
    nxt = phase_after(phase)
    if nxt:
        lines.append(f"  next: {nxt.command}   (Copilot agent: {nxt.agent})")
    else:
        lines.append(f"  report: {_rel(run_dir, os.path.join(run_dir, 'reports', 'summary.md'))}")
    return lines


def _write_console(run_dir: str, phase: Phase, lines: list[str]) -> None:
    write_text(os.path.join(phase_dir(run_dir, phase), CONSOLE), "\n".join(lines) + "\n")


def read_console(run_dir: str, phase: Phase) -> str:
    p = os.path.join(phase_dir(run_dir, phase), CONSOLE)
    return read_text(p) if os.path.isfile(p) else ""


def _finish(run_dir: str, phase: Phase, st: dict, log: PhaseLog) -> list[str]:
    q = None
    if st["state"] == "done":
        q = score(run_dir, phase, st.get("jobs") or {})
        write_json(os.path.join(phase_dir(run_dir, phase), QUALITY), q)
        st["quality"] = q["status"]
        copied = snapshot_outputs(run_dir, phase)
        if copied:
            log.debug("", f"outputs copied to {phase.id}\\outputs: {', '.join(copied)}")
        level = {"green": "INFO", "amber": "WARN", "red": "WARN"}[q["status"]]
        log.event(level, "", f"quality {q['status']}: {q['metrics']}")
        for n in q["notes"]:
            log.event(level, "", f"quality note: {n}")
        log.info("", f"done in {_secs(st)}, exit {st.get('exitCode')} ({UL_EXIT.get(st.get('exitCode'), '')})", "done")
    write_status(run_dir, phase, st)
    lines = console_lines(run_dir, phase, st, q)
    _write_console(run_dir, phase, lines)
    return lines


def run_phase(config: str, runs_dir: str, run_id: str, phase: Phase, job: str | None = None, from_job: str | None = None,
              restart: bool = False, agents: bool = True, force: bool = False, verbose: bool = False,
              echo=None) -> tuple[int, list[str]]:
    run_dir = os.path.join(runs_dir, run_id)
    if not os.path.isfile(os.path.join(run_dir, "status.json")):
        return USAGE, [f"Run not found: {run_dir}. Start one with: {UL} setup"]
    for name in (job, from_job):
        if name and not phase.job(name):
            return USAGE, [f"{phase.id} has no job '{name}'. Its jobs: {', '.join(j.name for j in phase.jobs)}"]
    held = acquire(run_dir, phase.id)
    if held["active"]:
        return USAGE, [f"Run {run_id} is busy: phase {held['key']} is running (pid {held['pid']}).",
                       f"Wait for it: {UL} wait --run {run_id}"]
    try:
        prev = read_status(run_dir, phase)
        partial = bool(job or from_job)
        resume = prev.get("state") == "waiting" and not restart and not partial
        archived = None
        if not resume and not partial:
            try:
                archived = archive(run_dir, phase)
            except OSError as e:
                return TOOL, [f"{phase.id}: could not move the previous attempt to history (a file is open?): {e}",
                              "Close the file (or the program using it) and run the phase again."]
        st = dict(prev) if (resume or partial) else {}
        jobs_rec = dict(st.get("jobs") or {})
        st.update({"schemaVersion": 1, "phase": phase.id, "runId": run_id, "state": "running", "exitCode": None,
                   "pid": os.getpid(), "startedAt": now_iso(), "finishedAt": None, "jobs": jobs_rec,
                   "agents": agents, "mode": "resume" if resume else ("partial" if partial else "full")})
        if not resume:
            st["inputs"] = hash_inputs(run_dir, phase)
        log = PhaseLog(run_dir, phase.id, verbose, echo)
        how = {"resume": "resume after the agent job", "partial": f"job {job or 'from ' + str(from_job)} only",
               "full": "all jobs"}[st["mode"]]
        log.info("", f"start {phase.id} ({phase.title}): {how}; agents {'on' if agents else 'off'}", "start")
        if archived:
            log.info("", f"previous attempt moved to {_rel(run_dir, archived)}")
        for rel, sha in sorted((st.get("inputs") or {}).items()):
            log.debug("", f"input {rel} sha256 {sha[:12]}")
        write_status(run_dir, phase, st)

        # Every input from outside the phase must be there before anything runs. Inputs that only an agent
        # writes (artifacts\agent\...) are decided by the job itself: no answer means nothing to do.
        if not partial and not resume:
            missing = [m for m in missing_needs(run_dir, phase) if not m.startswith("artifacts\\agent\\")]
            if missing:
                for m in missing:
                    log.error("", f"missing input {m} (written by {producer(m)})", "fail")
                st.update({"state": "failed", "exitCode": USAGE, "finishedAt": now_iso(),
                           "reason": "missing input(s): " + ", ".join(missing), "missing": missing})
                jobs_rec[phase.jobs[0].name] = {"state": "failed", "code": USAGE, "reason": st["reason"],
                                                "missing": missing}
                return USAGE, _finish(run_dir, phase, st, log)

        def save_job(name: str, rec: dict) -> None:
            jobs_rec[name] = rec
            write_status(run_dir, phase, st)

        ctx = Ctx(config=config, run_id=run_id, run_dir=run_dir, phase=phase, log=log, agents=agents, force=force,
                  save_job=save_job)
        if job:
            selected = [phase.job(job)]
        elif from_job:
            selected = list(phase.jobs[[j.name for j in phase.jobs].index(from_job):])
        else:
            selected = list(phase.jobs)
        final_state, final_code = "done", OK
        for j in selected:
            rec = jobs_rec.get(j.name) or {}
            if resume and rec.get("state") in ("done", "skipped"):
                continue
            started = now_iso()
            try:
                why = "" if job else condition(ctx, j, jobs_rec)
                res = skip(ctx, j, why) if why else run_job(ctx, j, rec if resume or partial else {})
            except Exception as e:  # noqa: BLE001 - a crash must end as a logged failure, never a phase stuck "running"
                for line in traceback.format_exc().rstrip().splitlines():
                    log.error(j.name, f"  | {line}")
                log.error(j.name, f"crashed: {type(e).__name__}: {e}", "fail")
                res = Result("failed", TOOL, f"the runner crashed in this job: {type(e).__name__}: {e}")
            if res.state == "waiting":
                new = dict(res.extra)
                new.update({"state": "waiting", "code": WAITING, "reason": res.reason})
            else:
                new = {"state": res.state, "code": res.code, "reason": res.reason, "startedAt": started,
                       "finishedAt": now_iso(), **res.extra}
            save_job(j.name, new)
            if res.state == "failed":
                final_state, final_code = "failed", res.code
                break
            if res.state == "waiting":
                final_state, final_code = "waiting", WAITING
                break
            if res.code == BLOCKED:
                final_code = BLOCKED
        if final_state == "done" and any((jobs_rec.get(j.name) or {}).get("state") not in ("done", "skipped")
                                          for j in phase.jobs):
            final_state = "partial"
        st.update({"state": final_state, "exitCode": final_code, "finishedAt": now_iso()})
        if final_state == "failed":
            log.error("", f"FAILED: exit {final_code} ({UL_EXIT.get(final_code, '')})", "fail")
        if final_state == "partial":
            log.info("", "the selected job(s) finished; the phase is not complete until every job has run", "done")
        return final_code, _finish(run_dir, phase, st, log)
    finally:
        release(run_dir)


def run_setup(config: str, runs_dir: str, verbose: bool = False, echo=None) -> tuple[int, str | None, list[str]]:
    """Phase 00-setup: stage 0 makes the run folder, so its log is moved into the run afterwards."""
    phase = PHASES[0]
    os.makedirs(runs_dir, exist_ok=True)
    before = {n for n in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, n))}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp_log = os.path.join(runs_dir, f"_setup-{stamp}.log")
    started = now_iso()
    code, secs = run_setup_script(config, tmp_log)
    new = sorted(n for n in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, n)) and n not in before
                 and os.path.isfile(os.path.join(runs_dir, n, "status.json")))
    if not new:
        last = [x for x in tail(tmp_log, 40) if x.strip()][-12:]
        if code == OK:
            head = f"00-setup: FAILED: stage 0 finished, but no new run folder is under {runs_dir} (see 'run folder' below)"
        else:
            head = f"00-setup: FAILED before a run was created (exit {code}: {exit_meaning(code, 0) or UL_EXIT.get(code, '')})"
        return (code or TOOL), None, [head] + [f"  | {x}" for x in last] + \
            [f"  full output: {tmp_log}", "  fix the config or prerequisite named above, then run setup again"]
    run_id = new[-1]
    run_dir = os.path.join(runs_dir, run_id)
    tools = os.path.join(phase_dir(run_dir, phase), "tools")
    os.makedirs(tools, exist_ok=True)
    os.replace(tmp_log, os.path.join(tools, "setup.log"))
    log = PhaseLog(run_dir, phase.id, verbose, echo)
    log.info("setup", f"start {phase.title}", "start", config=config)
    meaning = exit_meaning(code, 0) or UL_EXIT.get(code, "")
    jobs_rec = {}
    if code == OK:
        log.info("setup", f"output {native_path('artifacts/environment.json')}")
        log.info("setup", f"done in {secs:.1f}s, exit 0", "done")
        jobs_rec["setup"] = {"state": "done", "code": OK, "reason": meaning, "startedAt": started, "finishedAt": now_iso()}
        set_current(runs_dir, run_id)
    else:
        last = [x for x in tail(os.path.join(tools, "setup.log"), 40) if x.strip()][-8:]
        for x in last:
            log.error("setup", f"  | {x}")
        log.error("setup", f"failed in {secs:.1f}s, exit {code} ({meaning})", "fail")
        jobs_rec["setup"] = {"state": "failed", "code": code, "reason": meaning, "tail": last,
                             "toolLog": os.path.join(tools, "setup.log")}
    st = {"schemaVersion": 1, "phase": phase.id, "runId": run_id, "state": "done" if code == OK else "failed",
          "exitCode": code if code in (OK, USAGE, TOOL) else TOOL, "startedAt": started, "finishedAt": now_iso(),
          "jobs": jobs_rec, "config": config, "inputs": {}, "mode": "full"}
    lines = _finish(run_dir, phase, st, log)
    lines.insert(1, f"  run: {run_id}  (now the current run; later commands use it without --run)")
    _write_console(run_dir, phase, lines)
    return st["exitCode"], run_id, lines

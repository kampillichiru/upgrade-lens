"""ul.py: run any UpgradeLens phase on its own, see where a run is, and find out why something went wrong.

Phases (each one is a Copilot agent too: ul-00-setup ... ul-06-report):
  setup      00-setup     config check, clone, one worktree per branch, merge base. Makes a new run.
  build      01-build     build and test both branches, with coverage
  impact     02-impact    changed files and methods, dependency and API changes, impact rules
  analysis   03-analysis  release-notes and analyst agent jobs, merged into the findings
  tests      04-tests     probes, test gaps, test-writer agent job, validation on both branches, repair
  review     05-review    reviewer agent job: pass or fail per validated test
  report     06-report    flows, manual items, report, review record, verdict

Every phase runs on its own whenever its inputs are there (`ul.py needs <phase>` lists them). Running a phase
again moves its previous attempt to <phase>\\history\\ and marks later phases stale when its outputs change.

Views (change nothing):
  status            every phase: state, quality, jobs, the next command
  why <phase>       why a phase failed, waits, is stale or scored amber/red, and what to do
  needs <phase>     its inputs, whether each is there, which phase writes it
  funnel            where results drop: changed methods -> findings -> gaps -> tests -> validated -> reviewed
  continue          run the next phase that is not done (the ul-run agent calls it in a loop)
  runs / use <id>   list the runs / make one the current run
  doctor            check the config and the prerequisites (JDKs, Maven, git, repository)
  wait              wait for the phase that runs in the background

Exit codes: 0 done; 1 verdict different or incomplete (a result); 2 inputs, config or usage; 3 tool failure; 4 build failed;
            5 agent wrote outside its folder; 6 agent answer failed its checks; 10 still running; 20 waiting for
            the agent job (read the brief, write the answer, run the same command again).

Examples:
  python .github/upgradelens/ul.py setup
  python .github/upgradelens/ul.py build
  python .github/upgradelens/ul.py analysis --job analyst
  python .github/upgradelens/ul.py all --no-agents
  python .github/upgradelens/ul.py why impact
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from lib.core import IS_WINDOWS, ROOT_DIR, read_json_safe, resolve_config_path, runs_root
from lib.lock import pid_alive, run_lock

from .engine import read_console, run_phase, run_setup
from .jobs import OK, RUNNING, USAGE, run_check_only
from .registry import PHASES, UL, find_phase
from .state import all_states, current_run, list_runs, read_status, set_current
from .views import funnel_lines, needs_lines, status_lines, why_lines

UL_PY = os.path.join(ROOT_DIR, "ul.py")
PHASE_NAMES = [p.name for p in PHASES]


def _print(lines) -> None:
    for line in lines:
        print(line)
    sys.stdout.flush()


def _config(a) -> str:
    return resolve_config_path(getattr(a, "config", None))


def _runs_dir(config: str) -> str:
    return runs_root(read_json_safe(config) or {})


def _run_id(a, runs_dir: str) -> str | None:
    rid = getattr(a, "run", None) or current_run(runs_dir)
    if not rid:
        print(f"No run yet under {runs_dir}. Start one: {UL} setup")
        return None
    if not os.path.isfile(os.path.join(runs_dir, rid, "status.json")):
        print(f"Run not found: {os.path.join(runs_dir, rid)}. List the runs: {UL} runs")
        return None
    return rid


def _run_config(runs_dir: str, run_id: str, given: str | None, default: str) -> str:
    """The config the run was set up with (recorded by 00-setup), unless --config is given."""
    if given:
        return default
    st = read_status(os.path.join(runs_dir, run_id), PHASES[0])
    rec = st.get("config")
    return rec if rec and os.path.isfile(rec) else default


def _start_detached(args: list[str], out_log: str) -> subprocess.Popen:
    """A process that keeps running when this one ends (a closed chat terminal does not stop a build)."""
    os.makedirs(os.path.dirname(out_log), exist_ok=True)
    out = open(out_log, "w", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        if IS_WINDOWS:
            base = 0x08000000 | 0x00000200   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: a hidden console of its own
            try:
                return subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                        creationflags=base | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:
                return subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                        creationflags=base)
        return subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True)
    finally:
        out.close()


def _follow(log_path: str, pos: int) -> int:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            chunk = f.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            return f.tell()
    except OSError:
        return pos


def _wait_for(is_running, run_dir: str, phase, minutes: float, follow: bool) -> bool:
    """Waits while is_running(); True when it ended within the time."""
    log_path = os.path.join(run_dir, phase.id, "log.txt")
    pos = os.path.getsize(log_path) if follow and os.path.isfile(log_path) else 0
    deadline = time.time() + minutes * 60
    while is_running():
        if time.time() > deadline:
            return False
        time.sleep(1)
        if follow:
            pos = _follow(log_path, pos)
    if follow:
        _follow(log_path, pos)
    return True


def cmd_phase(a, phase) -> int:
    config = _config(a)
    runs_dir = _runs_dir(config)
    if phase.name == "setup":
        code, _rid, lines = run_setup(config, runs_dir, a.verbose)
        _print(lines)
        return code
    rid = _run_id(a, runs_dir)
    if not rid:
        return USAGE
    config = _run_config(runs_dir, rid, getattr(a, "config", None), config)
    run_dir = os.path.join(runs_dir, rid)
    agents = not a.no_agents
    if a.foreground or a.child:
        echo = print if (a.follow and not a.child) else None
        code, lines = run_phase(config, runs_dir, rid, phase, a.job, a.from_job, a.restart, agents, a.force, a.verbose, echo)
        if not a.child:
            _print(lines)
        return code
    held = run_lock(run_dir)
    if held["active"]:
        print(f"Run {rid} is busy: phase {held['key']} is running (pid {held['pid']}). Wait: {UL} wait --run {rid}")
        return USAGE
    args = [sys.executable, UL_PY, phase.name, "--run", rid, "--config", config, "--child"]
    for flag, on in (("--restart", a.restart), ("--no-agents", a.no_agents), ("--force", a.force), ("--verbose", a.verbose)):
        if on:
            args.append(flag)
    if a.job:
        args += ["--job", a.job]
    if a.from_job:
        args += ["--from", a.from_job]
    out_log = os.path.join(run_dir, "logs", f"ul-{phase.id}.out.log")   # outside the phase folder: never archived
    proc = _start_detached(args, out_log)
    if not _wait_for(lambda: proc.poll() is None, run_dir, phase, a.wait_minutes, a.follow):
        print(f"{phase.id}: still running in the background (pid {proc.pid}); it keeps running if this terminal closes.")
        print(f"  follow: {UL} wait --run {rid} --follow")
        print("  exit 10 = still running")
        return RUNNING
    text = read_console(run_dir, phase)
    if text:
        sys.stdout.write(text)
    else:
        print(f"{phase.id}: the runner ended (exit {proc.returncode}) without a summary. "
              f"See {out_log}")
    return proc.returncode


def cmd_all(a) -> int:
    config = _config(a)
    runs_dir = _runs_dir(config)
    first = find_phase(a.from_phase) if a.from_phase else PHASES[0]
    last = find_phase(a.to_phase) if a.to_phase else PHASES[-1]
    if not first or not last:
        print(f"Unknown phase. Phases: {', '.join(PHASE_NAMES)}")
        return USAGE
    rid = None
    if first.name != "setup":
        rid = _run_id(a, runs_dir)
        if not rid:
            return USAGE
        config = _run_config(runs_dir, rid, getattr(a, "config", None), config)
    code = OK
    for p in PHASES[PHASES.index(first):PHASES.index(last) + 1]:
        print(f"== {p.id}: {p.title} ==", flush=True)
        if p.name == "setup":
            code, rid, lines = run_setup(config, runs_dir, a.verbose)
        else:
            code, lines = run_phase(config, runs_dir, rid, p, agents=not a.no_agents, verbose=a.verbose,
                                    echo=print if a.follow else None)
        _print(lines)
        if code not in (OK, 1) or rid is None:
            return code
    return code


ALL_DONE = "ALL PHASES DONE"


def cmd_continue(a) -> int:
    """Runs the next phase that is not done (not run, stale, waiting, failed), one phase per call, the same way
    `ul.py <phase>` does. Without a run (or with --new) it runs setup. The ul-run agent calls this in a loop."""
    config = _config(a)
    runs_dir = _runs_dir(config)
    if a.new or not (getattr(a, "run", None) or current_run(runs_dir)):
        print(f"== {PHASES[0].id}: {PHASES[0].title} ==")
        code, _rid, lines = run_setup(config, runs_dir, a.verbose)
        _print(lines)
        return code
    rid = _run_id(a, runs_dir)
    if not rid:
        return USAGE
    run_dir = os.path.join(runs_dir, rid)
    nxt = next((x for x in all_states(run_dir) if x["state"] != "done"), None)
    if nxt is None:
        v = read_json_safe(os.path.join(run_dir, "verdict.json")) or {}
        print(f"{ALL_DONE}: run {rid}, verdict {v.get('verdict', '?')}")
        print(f"  summary: {os.path.join(run_dir, 'reports', 'summary.md')}")
        print(f"  report:  {os.path.join(run_dir, 'reports', 'report.md')}")
        return OK
    p = nxt["phase"]
    print(f"== {p.id}: {p.title} ({nxt['state']}) ==", flush=True)
    ns = argparse.Namespace(config=getattr(a, "config", None), run=rid, job=None, from_job=None, restart=False,
                            no_agents=a.no_agents, force=False, foreground=False, follow=False,
                            wait_minutes=a.wait_minutes, verbose=a.verbose, child=False)
    return cmd_phase(ns, p)


def cmd_wait(a) -> int:
    config = _config(a)
    runs_dir = _runs_dir(config)
    rid = _run_id(a, runs_dir)
    if not rid:
        return USAGE
    run_dir = os.path.join(runs_dir, rid)
    held = run_lock(run_dir)
    phase = find_phase(held["key"] or "") if held["active"] else None
    if not phase:
        print(f"Nothing is running in run {rid}.")
        _print(status_lines(runs_dir, rid))
        return OK
    pid = held["pid"]
    if not _wait_for(lambda: pid_alive(pid), run_dir, phase, a.wait_minutes, a.follow):
        print(f"{phase.id}: still running (pid {pid}). Wait again: {UL} wait --run {rid}")
        return RUNNING
    text = read_console(run_dir, phase)
    sys.stdout.write(text or f"{phase.id} ended.\n")
    code = read_status(run_dir, phase).get("exitCode")
    return int(code) if code is not None else OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ul.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, run=True):
        sp.add_argument("--config", help="config file (default .github/upgradelens/config/upgrade-lens.json)")
        if run:
            sp.add_argument("--run", help="run id (default: the current run, see 'runs')")

    for ph in PHASES:
        sp = sub.add_parser(ph.name, help=f"{ph.id}: {ph.title}")
        common(sp, run=ph.name != "setup")
        sp.add_argument("--job", help=f"run only this job ({', '.join(j.name for j in ph.jobs)})")
        sp.add_argument("--from", dest="from_job", help="run from this job to the end of the phase")
        sp.add_argument("--restart", action="store_true", help="start the phase over, also when an agent job waits")
        sp.add_argument("--no-agents", action="store_true", help="skip the agent jobs (scripts only)")
        sp.add_argument("--force", action="store_true", help="ask the agent again even when its answer could be reused")
        sp.add_argument("--foreground", action="store_true", help="run in this process (Ctrl+C stops it)")
        sp.add_argument("--follow", action="store_true", help="print the phase log while it runs")
        sp.add_argument("--wait-minutes", type=float, default=8.0,
                        help="return exit 10 after this long; the phase keeps running (default 8)")
        sp.add_argument("--verbose", action="store_true", help="DEBUG lines in the log")
        sp.add_argument("--child", action="store_true", help=argparse.SUPPRESS)

    sp = sub.add_parser("all", help="run the phases in order, stopping at a failure or a waiting agent job")
    common(sp)
    sp.add_argument("--from", dest="from_phase")
    sp.add_argument("--to", dest="to_phase")
    sp.add_argument("--no-agents", action="store_true")
    sp.add_argument("--follow", action="store_true")
    sp.add_argument("--verbose", action="store_true")

    sp = sub.add_parser("continue", help="run the next phase that is not done (one per call; setup when there is no run)")
    common(sp)
    sp.add_argument("--new", action="store_true", help="start a new run (setup) instead of continuing")
    sp.add_argument("--no-agents", action="store_true")
    sp.add_argument("--wait-minutes", type=float, default=8.0)
    sp.add_argument("--verbose", action="store_true")

    for name, hlp in (("status", "every phase of a run"), ("funnel", "where results drop"),
                      ("runs", "list the runs"), ("doctor", "check config and prerequisites")):
        sp = sub.add_parser(name, help=hlp)
        common(sp, run=name in ("status", "funnel"))
    for name, hlp in (("why", "why a phase failed, waits, is stale or not green"), ("needs", "what a phase needs")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("phase", help=", ".join(PHASE_NAMES))
        common(sp)
        if name == "why":
            sp.add_argument("--lines", type=int, default=20)
    sp = sub.add_parser("use", help="make a run the current run")
    sp.add_argument("run_id")
    common(sp, run=False)
    sp = sub.add_parser("wait", help="wait for the phase running in the background")
    common(sp)
    sp.add_argument("--wait-minutes", type=float, default=8.0)
    sp.add_argument("--follow", action="store_true")
    return p


def main(argv) -> int:
    a = build_parser().parse_args(argv)
    phase = find_phase(a.cmd) if a.cmd in PHASE_NAMES else None
    if phase:
        return cmd_phase(a, phase)
    if a.cmd == "all":
        return cmd_all(a)
    if a.cmd == "continue":
        return cmd_continue(a)
    if a.cmd == "wait":
        return cmd_wait(a)
    config = _config(a)
    runs_dir = _runs_dir(config)
    if a.cmd == "doctor":
        print(f"config: {config}\nruns:   {runs_dir}\npython: {sys.version.split()[0]}")
        return run_check_only(config)
    if a.cmd == "runs":
        cur = current_run(runs_dir)
        runs = list_runs(runs_dir)
        print(f"Runs under {runs_dir}:" if runs else f"No runs under {runs_dir}.")
        for r in runs:
            print(f"  {'*' if r == cur else ' '} {r}")
        return OK
    if a.cmd == "use":
        if not os.path.isfile(os.path.join(runs_dir, a.run_id, "status.json")):
            print(f"Run not found: {os.path.join(runs_dir, a.run_id)}")
            return USAGE
        set_current(runs_dir, a.run_id)
        print(f"Current run: {a.run_id}")
        return OK
    rid = _run_id(a, runs_dir)
    if not rid:
        return USAGE
    if a.cmd == "status":
        _print(status_lines(runs_dir, rid))
        return OK
    if a.cmd == "funnel":
        _print(funnel_lines(runs_dir, rid))
        return OK
    ph = find_phase(a.phase)
    if not ph:
        print(f"Unknown phase '{a.phase}'. Phases: {', '.join(PHASE_NAMES)}")
        return USAGE
    if a.cmd == "needs":
        _print(needs_lines(runs_dir, rid, ph))
        return OK
    _print(why_lines(runs_dir, rid, ph, a.lines))
    return OK

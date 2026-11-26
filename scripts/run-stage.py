"""Runs one UpgradeLens stage of an orchestrated run, only when next-step.py says it is next.

Guards:
  - order: the key must be the step next-step.py prints (no stage runs early, late or twice by mistake)
  - lock: one stage at a time per run (runs\\<id>\\orchestration\\lock.json)
  - detached: the stage runs in its own process, so a chat tool that times out a long command (a Maven build
    can take an hour) does not kill it. This call waits up to --wait-seconds, then returns exit 10 (still
    running); run it again with --wait until it returns.
  - inputs: a stage whose inputs (lib\\stages.py, needs) are missing is refused before it starts (exit 2)
  - failure report: when a stage fails, stage 8 writes a partial report (after a failed build, stage 2 first)
  - ledger: each run is recorded in runs\\<id>\\orchestration\\ledger.json with its exit code

Exit codes:
  0   the stage finished (stage 10: any verdict; read next-step.py for the next action)
  2   usage, out of order, or locked by another stage
  3   tool failure in the stage          4   build or tests failed (stage 1)
  10  still running: run the same command with --wait

Examples:
  python scripts/run-stage.py --new
  python scripts/run-stage.py --run-id 20261126-152851 --key stage-1
  python scripts/run-stage.py --run-id 20261126-152851 --wait
"""

import argparse
import os
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json  # noqa: E402

from lib.core import (IS_WINDOWS, as_list, command, config_value, ignore_interrupts, now_iso, read_json_safe,  # noqa: E402
                      resolve_config_path, run_main, run_script, runs_root, tail, to_int, write_json)
from lib.facts import run_facts  # noqa: E402
from lib.ledger import ledger_entry, lock_path, new_ledger, read_ledger, run_lock, set_ledger_entry  # noqa: E402
from lib.plan import max_stage_attempts, next_action, plan_step, run_plan, stage_script  # noqa: E402
from lib.stages import missing_message, missing_needs  # noqa: E402


def wait_seconds(value: str) -> int:
    n = int(value)
    if not 10 <= n <= 3000:
        raise argparse.ArgumentTypeError("must be 10 to 3000")
    return n


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Run one stage of an orchestrated UpgradeLens run.")
    p.add_argument("--run-id")
    p.add_argument("--key", help="the key next-step.py printed (for example stage-1)")
    p.add_argument("--new", action="store_true", help="start a new run (stage 0)")
    p.add_argument("--wait", action="store_true", help="wait for the running stage")
    p.add_argument("--wait-seconds", type=wait_seconds, default=480)
    p.add_argument("--config")
    p.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    os.environ["UPGRADELENS_CONFIG"] = config
    runs = runs_root(read_json_safe(config))
    stage_env = {"UPGRADELENS_INVOKER": "run-stage", "UPGRADELENS_CONFIG": config}

    def invoke_stage(stage: int, args) -> int:
        """Runs a stage script and returns its exit code. A failure to start is a tool failure (3)."""
        return run_script(stage_script(stage), args, stage_env)

    # ---------- stage 0: a new run, in the foreground (it is short: config check, clone, worktrees) ----------

    if a.new:
        before = set(os.listdir(runs)) if os.path.isdir(runs) else set()
        code = invoke_stage(0, ["--config", config])
        after = [n for n in (os.listdir(runs) if os.path.isdir(runs) else [])
                 if n not in before and os.path.isdir(os.path.join(runs, n))]
        if not after:
            print(f"Stage 0 failed (exit {code}) before a run folder was created. Fix the FAIL lines above "
                  "(config\\upgrade-lens.json) and start again.")
            return 3 if code == 0 else code
        run_id = sorted(after, reverse=True)[0]
        run_dir = os.path.join(runs, run_id)
        new_ledger(run_dir, run_id, config)
        set_ledger_entry(run_dir, "stage-0", "stage", "done" if code == 0 else "failed",
                         "" if code == 0 else "setup failed; see status.json and logs\\phase0.log", {"exitCode": code})
        print(json.dumps({"runId": run_id, "runDir": run_dir, "stage": 0, "exitCode": code,
                          "next": command("next-step.py", "--run-id", run_id, "--config", config)}, indent=2))
        return code

    if not a.run_id:
        print("--run-id is required (or --new to start a run).")
        return 2
    run_id = a.run_id
    run_dir = os.path.join(runs, run_id)
    if not read_ledger(run_dir):
        print(f"Run {run_id} has no orchestration ledger. Start runs with run-stage.py --new.")
        return 2
    orch_dir = os.path.join(run_dir, "orchestration")
    lock_file = lock_path(run_dir)

    # ---------- child: runs the stage, records the result, releases the lock ----------

    if a.child:
        # The stage must finish even when the terminal that started run-stage.py sends Ctrl+C.
        ignore_interrupts()
        stage = plan_step(run_plan(9), a.key)["stage"]
        code = invoke_stage(stage, ["--run-id", run_id, "--config", config])
        ok = code == 0 or (stage == 10 and code == 1)
        reason = ""
        if not ok:
            st = read_json_safe(os.path.join(run_dir, "status.json"))
            failed = [x for x in as_list((st or {}).get("steps")) if x and x.get("state") not in ("done", "skipped")]
            reason = f"{failed[-1].get('name')}: {failed[-1].get('reason')}" if failed else f"exit {code}; see logs\\phase{stage}.log"
            if stage == 1 and code == 4:
                invoke_stage(2, ["--run-id", run_id, "--config", config])
            if run_script("phase8-output.py", ["--run-id", run_id, "--report-only", "--config", config], stage_env) != 0:
                print("Partial report failed.")
        set_ledger_entry(run_dir, a.key, "stage", "done" if ok else "failed", reason, {"exitCode": code}, update=True)
        try:
            os.remove(lock_file)
        except OSError:
            pass
        return code

    # ---------- parent: wait for a running stage ----------

    def wait_stage(stage_key: str, seconds: int) -> int:
        deadline = time.time() + seconds
        out_log = os.path.join(orch_dir, f"{stage_key}.out.log")
        while time.time() < deadline:
            if not run_lock(run_dir)["active"]:
                e = ledger_entry(read_ledger(run_dir), stage_key)
                for line in tail(out_log, 25):
                    print(line)
                if not e or e.get("state") == "running":
                    print(f"Stage {stage_key} ended without recording a result (the runner died). See {out_log}.")
                    return 3
                why = f": {e.get('reason')}" if e.get("reason") else ""
                print(f"Stage {stage_key} {e.get('state')} (exit {e.get('exitCode')}){why}. "
                      f"Next: {command('next-step.py', '--run-id', run_id)}")
                if e.get("state") == "done":
                    return 0
                return 3 if to_int(e.get("exitCode")) == 0 else to_int(e.get("exitCode"), 3)
            time.sleep(5)
        print(f"Stage {stage_key} is still running (pid {run_lock(run_dir)['pid']}). "
              f"Run: {command('run-stage.py', '--run-id', run_id, '--wait')}")
        return 10

    if a.wait:
        lk = run_lock(run_dir)
        key = lk["key"]
        if not key:
            stages = [e for e in as_list(read_ledger(run_dir).get("entries")) if e and e.get("kind") == "stage"]
            key = stages[-1]["key"] if stages else None
        if not key:
            print("No stage to wait for.")
            return 2
        return wait_stage(key, a.wait_seconds)

    # ---------- parent: start a stage, only if it is the next step ----------

    if not a.key:
        print("--key is required: the key next-step.py printed (for example stage-1).")
        return 2
    run_cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    plan = run_plan(to_int(config_value(run_cfg, "orchestration.maxTestRepairRounds", 1), 1))
    ledger = read_ledger(run_dir)
    nxt = next_action(plan, ledger, run_facts(run_dir, ledger), to_int(config_value(run_cfg, "orchestration.maxAgentAttempts", 2), 2),
                      max_stage_attempts(run_cfg))
    if nxt["action"] == "wait":
        print(f"Stage {nxt['key']} is running. Run: {command('run-stage.py', '--run-id', run_id, '--wait')}")
        return 2
    if nxt["action"] != "run-stage" or nxt["key"] != a.key:
        print(f"Out of order: next-step says '{nxt['action']} {nxt['key']}', not '{a.key}'. "
              f"Run {command('next-step.py', '--run-id', run_id)} and do what it prints.")
        return 2

    os.makedirs(orch_dir, exist_ok=True)
    # A retry after an interruption: an attempt whose runner died is still marked running. Close it first, so the
    # ledger shows the interrupted attempt and the retry as two attempts.
    last = ledger_entry(ledger, a.key)
    if last and last.get("state") == "running":
        set_ledger_entry(run_dir, a.key, "stage", "failed", "interrupted: the runner died before it recorded a result", update=True)
    # Inputs (lib\stages.py, needs): a stage whose inputs are missing is refused before it starts, and the refusal
    # is recorded, so next-step reports it as a stop with the missing files instead of printing the stage again.
    missing = missing_message(run_dir, nxt["stage"])
    if missing:
        set_ledger_entry(run_dir, a.key, "stage", "failed", "missing input: " + "; ".join(missing_needs(run_dir, nxt["stage"])),
                         {"exitCode": 2})
        print(missing)
        print(f"Diagnose: {command('diagnose.py', '--run-id', run_id)}")
        return 2
    # Lock first (held by this process), so no one sees a running step without a live lock holder.
    write_json(lock_file, {"pid": os.getpid(), "key": a.key, "startedAt": now_iso()})
    set_ledger_entry(run_dir, a.key, "stage", "running", nxt["reason"])
    out_log = os.path.join(orch_dir, f"{a.key}.out.log")
    err_log = os.path.join(orch_dir, f"{a.key}.err.log")
    child_args = [sys.executable, os.path.abspath(__file__), "--child", "--run-id", run_id, "--key", a.key, "--config", config]
    try:
        proc = start_detached(child_args, out_log, err_log)
    except OSError as e:
        set_ledger_entry(run_dir, a.key, "stage", "failed", f"could not start the stage process: {e}", {"exitCode": 3}, update=True)
        try:
            os.remove(lock_file)
        except OSError:
            pass
        print(f"Could not start {a.key}: {e}")
        return 3
    # Hand the lock to the child, unless it already finished and released it.
    e = ledger_entry(read_ledger(run_dir), a.key)
    if e and e.get("state") == "running":
        write_json(lock_file, {"pid": proc.pid, "key": a.key, "startedAt": now_iso()})
    print(f"Started {a.key} ({nxt['reason']}) as pid {proc.pid}. Output: {out_log}")
    return wait_stage(a.key, a.wait_seconds)


def start_detached(args, out_log: str, err_log: str) -> subprocess.Popen:
    """Starts a process that keeps running when this one ends (and when the caller's job is closed)."""
    out = open(out_log, "w", encoding="utf-8")
    err = open(err_log, "w", encoding="utf-8")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        if IS_WINDOWS:
            # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: the stage gets its own hidden console, which the stage
            # scripts, Maven and the JVMs inherit. (DETACHED_PROCESS would leave it without a console, so each stage
            # script would open a visible console window; closing that window kills the stage.)
            base = 0x08000000 | 0x00000200
            try:
                return subprocess.Popen(args, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                                        creationflags=base | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:
                # The caller's job does not allow breakaway: start inside it.
                return subprocess.Popen(args, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env, creationflags=base)
        return subprocess.Popen(args, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    finally:
        out.close()
        err.close()


if __name__ == "__main__":
    run_main(main)

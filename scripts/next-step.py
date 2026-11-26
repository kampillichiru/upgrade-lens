"""Tells the UpgradeLens orchestrator the one next action of a run, as JSON. The orchestrator never chooses
the order itself: it runs this, does exactly the action it prints, and runs it again.

The plan is fixed (lib\\plan.py, run_plan):

  stage 0, 1, 2, 3 -> release-notes agent -> stage 4 -> analyst agent -> stage 4 again (merge)
  -> stage 5 (probes) -> test writer agent (every gap in artifacts\\test-gaps.json) -> stage 5 (tests)
  -> [repair round: test writer, stage 5] -> stage 6, 7, 8 -> reviewer agent -> stage 9, 10

A step that is not needed (for example no library change for the release-notes agent) is recorded in the
ledger as skipped, with the reason, and the next step is printed. Nothing is skipped silently.

Actions (field "action"):
  run-stage   run "command" (scripts/run-stage.py). Exit 10 from it means still running: run "waitCommand".
  wait        a stage is running: run "command" (run-stage.py --wait).
  run-agent   run "beginCommand"; give its "prompt" unchanged to the subagent "subagent"; then run "endCommand".
  end-agent   an agent step was begun and not ended: run "endCommand".
  stop        the run cannot continue. Report "reason" and "report". Do not work around it.
  done        report the verdict, "report" and "verdict".

Exit codes: 0 an action to do or done, 1 stop, 2 usage (no run, no ledger).

Examples:
  python scripts/next-step.py
  python scripts/next-step.py --run-id 20261126-152851
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import (command, config_value, get, read_json_safe, resolve_config_path, run_main,  # noqa: E402
                      runs_root, to_int)
from lib.agents import AGENTS  # noqa: E402
from lib.facts import run_facts  # noqa: E402
from lib.ledger import ledger_path, read_ledger, set_ledger_entry  # noqa: E402
from lib.plan import max_stage_attempts, next_action, run_plan  # noqa: E402


def emit(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    return code


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Print the one next action of an UpgradeLens run, as JSON.")
    p.add_argument("--run-id", help="the run; leave it out only to start a new run")
    p.add_argument("--config", help="config file (default: UPGRADELENS_CONFIG, then config\\upgrade-lens.json)")
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))

    if not a.run_id:
        return emit({
            "action": "run-stage", "key": "stage-0", "stage": 0,
            "reason": "start a new run: config check, clone, worktrees, merge base",
            "command": command("run-stage.py", "--new", "--config", config),
            "next": "The command prints the new run id. Pass --run-id <id> to every later next-step.py call.",
        })

    run_id = a.run_id
    run_dir = os.path.join(runs, run_id)
    if not os.path.isfile(os.path.join(run_dir, "status.json")):
        print(f"Run not found: {run_dir}")
        return 2
    if not read_ledger(run_dir):
        print(f"Run {run_id} has no orchestration ledger: it was not started by the orchestrator (run-stage.py --new). "
              "Start a new run.")
        return 2
    run_cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    max_repair = to_int(config_value(run_cfg, "orchestration.maxTestRepairRounds", 1), 1)
    max_attempts = to_int(config_value(run_cfg, "orchestration.maxAgentAttempts", 2), 2)
    plan = run_plan(max_repair)

    nxt = None
    for _ in range(50):
        ledger = read_ledger(run_dir)
        nxt = next_action(plan, ledger, run_facts(run_dir, ledger), max_attempts, max_stage_attempts(run_cfg))
        if nxt["action"] != "skip":
            break
        kind = next(x["kind"] for x in plan if x["key"] == nxt["key"])
        set_ledger_entry(run_dir, nxt["key"], kind, "skipped", nxt["reason"])

    report = os.path.join(run_dir, "reports", "report.md")
    summary = os.path.join(run_dir, "reports", "summary.md")
    out = {"runId": run_id, "action": nxt["action"], "key": nxt["key"], "reason": nxt["reason"]}
    act = nxt["action"]
    if act == "run-stage":
        out["stage"] = nxt["stage"]
        out["command"] = command("run-stage.py", "--run-id", run_id, "--key", nxt["key"], "--config", config)
        out["waitCommand"] = command("run-stage.py", "--run-id", run_id, "--wait", "--config", config)
    elif act == "wait":
        out["command"] = command("run-stage.py", "--run-id", run_id, "--wait", "--config", config)
    elif act == "run-agent":
        out["step"] = nxt["step"]
        out["attempt"] = nxt["attempt"]
        out["subagent"] = AGENTS[nxt["step"]].agent
        out["beginCommand"] = command("agent-step.py", "--run-id", run_id, "--key", nxt["key"], "--action", "begin", "--config", config)
        out["endCommand"] = command("agent-step.py", "--run-id", run_id, "--key", nxt["key"], "--action", "end", "--config", config)
    elif act == "end-agent":
        out["step"] = nxt["step"]
        out["subagent"] = AGENTS[nxt["step"]].agent
        out["endCommand"] = command("agent-step.py", "--run-id", run_id, "--key", nxt["key"], "--action", "end", "--config", config)
    elif act == "stop":
        out["summary"] = summary if os.path.isfile(summary) else ""
        out["report"] = report if os.path.isfile(report) else ""
        out["ledger"] = ledger_path(run_dir)
        return emit(out, 1)
    elif act == "done":
        v = read_json_safe(os.path.join(run_dir, "verdict.json"))
        out["verdict"] = get(v, "verdict", default="")
        out["blocking"] = list(get(v, "blocking", default=[]))
        out["summary"] = summary
        out["report"] = report
        out["newTests"] = os.path.join(run_dir, "artifacts", "new-tests.json")
    return emit(out, 0)


if __name__ == "__main__":
    run_main(main)

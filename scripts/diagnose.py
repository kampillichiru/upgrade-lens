"""Explains the state of an UpgradeLens run in one place: which step failed or is stuck, what its exit code
means, which inputs it is missing (and which stage writes them), the end of the logs to read, and the one
command to run next. Read only: it changes nothing in the run.

Without --stage it finds the problem itself:
  orchestrated run   the first plan step whose last attempt failed or is still running (runs\\<id>\\orchestration\\
                     ledger.json); interrupted attempts that were retried are shown too
  scripts-only run   the stage named in status.json when its state is failed-*, else the next stage to run
With --stage N it shows stage N, whatever its state. --key shows one plan step (for example agent-tests).

Where the facts come from: lib\\stages.py (what each stage needs, writes, logs, and its exit codes),
lib\\agents.py (what each agent needs), the ledger, status.json, and the logs.

Exit codes: 0 no problem (done, or waiting for its next step); 1 a step failed or is stuck; 2 usage.

Examples:
  python scripts/diagnose.py --run-id 20261126-180056
  python scripts/diagnose.py --run-id 20261126-180056 --stage 5
  python scripts/diagnose.py --run-id 20261126-180056 --key agent-tests --lines 60
  python scripts/diagnose.py --run-id 20261126-180056 --json
"""

import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.agents import AGENTS, missing_inputs  # noqa: E402
from lib.core import (as_list, command, config_value, get, is_orchestrated_run, read_json_safe,  # noqa: E402
                      resolve_config_path, run_main, runs_root, s, tail, to_int)
from lib.ledger import key_entries, ledger_entry, lock_path, pid_alive, read_ledger  # noqa: E402
from lib.plan import agent_key_step, plan_step, run_plan, stage_interrupted  # noqa: E402
from lib.stages import STAGES, exit_meaning, missing_needs, stage_logs, stage_spec, written_by  # noqa: E402


def describe_exit(code, stage) -> str:
    if code is None or s(code) == "":
        return "no exit code recorded"
    meaning = exit_meaning(code, stage)
    return f"exit {code} = {meaning}" if meaning else f"exit {code}"


def log_tails(paths, lines: int, limit: int = 3) -> list[dict]:
    """The end of the first non-empty logs (at most limit), and the paths of the others."""
    out, shown = [], 0
    for p in paths:
        if not os.path.isfile(p):
            continue
        if shown < limit and os.path.getsize(p) > 0:
            out.append({"path": p, "tail": tail(p, lines)})
            shown += 1
        else:
            out.append({"path": p, "tail": []})
    return out


def stage_detail(run_dir: str, stage: int, key: str | None, entries, lines: int) -> dict:
    spec = stage_spec(stage)
    last = entries[-1] if entries else None
    d = {
        "kind": "stage", "stage": stage, "key": key or f"stage-{stage}", "title": spec.title, "script": f"scripts\\{spec.script}",
        "state": s(get(last, "state")) or "not run",
        "attempts": [{"attempt": e.get("attempt"), "state": e.get("state"), "exit": describe_exit(e.get("exitCode"), stage),
                      "interrupted": stage_interrupted(e) if e.get("state") == "failed" else False, "reason": s(e.get("reason"))}
                     for e in entries],
        "inputs": [{"file": n, "present": n not in missing_needs(run_dir, stage), "writtenBy": written_by(n)} for n in spec.needs],
        "optional": [{"file": n, "present": os.path.isfile(os.path.join(run_dir, n))} for n in spec.optional],
        "outputs": [{"file": n, "present": os.path.isfile(os.path.join(run_dir, n))} for n in spec.writes],
        "hint": spec.hint,
    }
    st = read_json_safe(os.path.join(run_dir, "status.json")) or {}
    failed_steps = [x for x in as_list(st.get("steps")) if x and x.get("state") not in ("done", "skipped")]
    d["statusSteps"] = [{"name": x.get("name"), "state": x.get("state"), "reason": x.get("reason")} for x in failed_steps]
    # The runner's stderr first (a crash before the stage logged anything lands there), then the stage's own logs;
    # the runner's stdout repeats the stage log, so it comes last.
    orch = os.path.join(run_dir, "orchestration")
    paths = [os.path.join(orch, f"{key}.err.log")] if key else []
    paths += stage_logs(run_dir, stage)
    paths += [os.path.join(orch, f"{key}.out.log")] if key else []
    d["logs"] = log_tails(paths, lines)
    return d


def agent_detail(run_dir: str, key: str, entries, lines: int) -> dict:
    step = agent_key_step(key)
    spec = AGENTS[step]
    last = entries[-1] if entries else None
    errors = []
    if last and last.get("errorsFile") and os.path.isfile(last["errorsFile"]):
        errors = [s(x) for x in as_list(get(read_json_safe(last["errorsFile"]), "errors"))]
    repair = key.startswith("agent-tests-repair-")
    missing = missing_inputs(run_dir, step, repair)
    return {
        "kind": "agent", "key": key, "step": step, "agent": spec.agent, "title": spec.title,
        "instructions": f".github\\agents\\{spec.agent}.agent.md", "state": s(get(last, "state")) or "not run",
        "attempts": [{"attempt": e.get("attempt"), "state": e.get("state"), "reason": s(e.get("reason"))} for e in entries],
        "errors": errors,
        "inputs": [{"file": f"artifacts\\{n}", "present": n not in missing, "writtenBy": written_by(f"artifacts\\{n}")}
                   for n in list(spec.reads) + (list(spec.repair_reads) if repair else [])],
        "output": f"artifacts\\agent\\{spec.output}",
        "hint": ("guard: the agent wrote outside its files; the errors list each file. The run stops: start a new run."
                 if like_guard(last) else
                 "checks: each error names what the answer lacks; the retry prompt carries them." if errors else ""),
        "tryAlone": command("run-agent.py", "--run-id", os.path.basename(run_dir), "--step", step, "--action", "begin")
                    + (" --repair" if repair else ""),
        "logs": [],
    }


def like_guard(entry) -> bool:
    return bool(entry) and s(entry.get("reason")).startswith("guard:")


def lock_state(run_dir: str) -> dict:
    """The stage lock, read only (a stale lock is reported, not removed)."""
    lk = read_json_safe(lock_path(run_dir))
    if not lk:
        return {"active": False}
    return {"active": pid_alive(lk.get("pid")), "pid": lk.get("pid"), "key": lk.get("key"), "stale": not pid_alive(lk.get("pid"))}


def diagnose(run_dir: str, run_id: str, stage=None, key=None, lines: int = 25) -> dict:
    st = read_json_safe(os.path.join(run_dir, "status.json")) or {}
    verdict = read_json_safe(os.path.join(run_dir, "verdict.json"))
    orchestrated = is_orchestrated_run(run_dir)
    out = {"runId": run_id, "runDir": run_dir, "orchestrated": orchestrated, "state": st.get("state"), "phase": st.get("phase"),
           "verdict": get(verdict, "verdict"), "blocking": [s(x) for x in as_list(get(verdict, "blocking"))],
           "steps": [], "problem": None, "next": "", "lock": lock_state(run_dir)}
    report = os.path.join(run_dir, "reports", "summary.md")
    out["summary"] = report if os.path.isfile(report) else ""

    if orchestrated:
        cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
        plan = run_plan(to_int(config_value(cfg, "orchestration.maxTestRepairRounds", 1), 1))
        ledger = read_ledger(run_dir)
        problem_key, waiting_key = None, None
        for stp in plan:
            entries = key_entries(ledger, stp["key"])
            last = entries[-1] if entries else None
            interrupted = len([e for e in entries if e.get("state") == "failed" and stage_interrupted(e)])
            out["steps"].append({"key": stp["key"], "kind": stp["kind"], "state": s(get(last, "state")) or "-",
                                 "attempts": len(entries), "interrupted": interrupted,
                                 "exit": describe_exit(get(last, "exitCode"), stp["stage"]) if stp["kind"] == "stage" and last else "",
                                 "reason": s(get(last, "reason"))})
            if not last and not waiting_key and not problem_key:
                waiting_key = stp["key"]
            if last and last.get("state") in ("failed", "running") and not problem_key and not waiting_key:
                problem_key = stp["key"]
        chosen = key or (f"stage-{stage}" if stage is not None and plan_step(plan, f"stage-{stage}") else None)
        if stage is not None and not chosen:
            chosen = next((x["key"] for x in plan if x["stage"] == stage), None)
        chosen = chosen or problem_key
        if chosen:
            stp = plan_step(plan, chosen)
            if not stp:
                raise ValueError(f"unknown plan key '{chosen}'")
            entries = key_entries(ledger, chosen)
            out["problem"] = (agent_detail(run_dir, chosen, entries, lines) if stp["kind"] == "agent"
                              else stage_detail(run_dir, stp["stage"], chosen, entries, lines))
        last = ledger_entry(ledger, problem_key) if problem_key else None
        if out["lock"].get("active"):
            out["next"] = f"stage {out['lock']['key']} is running: {command('run-stage.py', '--run-id', run_id, '--wait')}"
        elif problem_key and last and last.get("state") == "running" and last.get("kind") == "agent":
            out["next"] = f"the agent step was begun and not ended: {command('next-step.py', '--run-id', run_id)} (it prints the endCommand)"
        elif problem_key:
            out["next"] = (f"the run stopped at {problem_key}. Fix the cause above, then start a new run "
                           f"(/upgradelens-orchestrator), or ask {command('next-step.py', '--run-id', run_id)} whether it retries")
        elif waiting_key:
            out["next"] = f"waiting for {waiting_key}: continue with /upgradelens-orchestrator continue run {run_id}"
        else:
            out["next"] = "done: read the summary"
    else:
        state, phase = s(st.get("state")), s(st.get("phase"))
        m = re.match(r"^phase(\d+)$", phase)
        last_stage = int(m.group(1)) if m else 0
        failed = state.startswith("failed")
        done = state.startswith("verdict-")
        problem_stage = stage if stage is not None else (last_stage if failed else (None if done else min(last_stage + 1, 10)))
        for n, spec in STAGES.items():
            ok = n < last_stage or (n == last_stage and not failed) or done
            out["steps"].append({"key": f"stage-{n}", "kind": "stage", "state": "failed" if (failed and n == last_stage) else
                                 ("done" if ok else "-"), "attempts": 1 if ok or (failed and n == last_stage) else 0,
                                 "interrupted": 0, "exit": "", "reason": ""})
        if problem_stage is not None:
            out["problem"] = stage_detail(run_dir, problem_stage, None, [], lines)
            if failed and problem_stage == last_stage:
                out["problem"]["state"] = state
        if failed:
            out["next"] = (f"fix the cause above, then {command('run-pipeline.py', '--scripts-only', '--run-id', run_id, '--from', str(last_stage))}")
        elif done:
            out["next"] = "done: read the summary"
        else:
            out["next"] = f"next stage: {command('run-pipeline.py', '--scripts-only', '--run-id', run_id, '--from', str(problem_stage))}"
    return out


def render(d: dict) -> list[str]:
    L = [f"Run {d['runId']} ({'orchestrated' if d['orchestrated'] else 'scripts-only'})   state: {d['state']}   phase: {d['phase']}"]
    if d["verdict"]:
        L.append(f"Verdict: {d['verdict']}" + (f", {len(d['blocking'])} blocking reason(s):" if d["blocking"] else ""))
        L += [f"  - {b}" for b in d["blocking"][:10]]
    lk = d["lock"]
    if lk.get("active"):
        L.append(f"Running now: {lk['key']} (pid {lk['pid']})")
    elif lk.get("stale"):
        L.append(f"Stale lock: {lk['key']} (pid {lk['pid']} is gone): the runner died")
    L += ["", "Steps"]
    for x in d["steps"]:
        mark = {"done": "ok  ", "skipped": "skip", "failed": "FAIL", "running": "RUN ", "-": "    "}.get(x["state"], x["state"][:4])
        extra = []
        if x["attempts"] > 1:
            extra.append(f"{x['attempts']} attempts")
        if x["interrupted"]:
            extra.append(f"{x['interrupted']} interrupted and retried")
        if x["state"] in ("failed", "running") and x["exit"]:
            extra.append(x["exit"])
        if x["state"] in ("failed", "skipped") and x["reason"]:
            extra.append(x["reason"][:110])
        L.append(f"  {mark}  {x['key']:<22} {'; '.join(extra)}")
    p = d["problem"]
    if p:
        L += ["", f"== {p['key']}: {p['title']} ({p['state']}) =="]
        if p["kind"] == "stage":
            L.append(f"Script: {p['script']}")
            for a in p["attempts"]:
                L.append(f"  attempt {a['attempt']}: {a['state']}, {a['exit']}" + (" (interrupted)" if a["interrupted"] else "")
                         + (f": {a['reason']}" if a["reason"] else ""))
            for x in p["statusSteps"]:
                L.append(f"  status.json step '{x['name']}': {x['state']}" + (f": {x['reason']}" if x["reason"] else ""))
        else:
            L.append(f"Agent: {p['agent']} ({p['instructions']})")
            for a in p["attempts"]:
                L.append(f"  attempt {a['attempt']}: {a['state']}: {a['reason']}")
            if p["errors"]:
                L.append("Errors of the last attempt:")
                L += [f"  - {e}" for e in p["errors"][:30]]
            L.append(f"Try it on its own: {p['tryAlone']}")
        L.append("Inputs:")
        for x in p["inputs"]:
            L.append(f"  {'ok     ' if x['present'] else 'MISSING'}  {x['file']}" + ("" if x["present"] else f"  (written by {x['writtenBy']})"))
        if not p["inputs"]:
            L.append("  (none)")
        if p.get("outputs"):
            L.append("Outputs: " + ", ".join(f"{x['file']} {'ok' if x['present'] else 'not written'}" for x in p["outputs"]))
        if p["hint"]:
            L.append(f"Hint: {p['hint']}")
        for lg in p["logs"]:
            if lg["tail"]:
                L += ["", f"--- {lg['path']} (last {len(lg['tail'])} lines) ---"] + lg["tail"]
        others = [lg["path"] for lg in p["logs"] if not lg["tail"]]
        if others:
            L += ["", "Other logs:"] + [f"  {x}" for x in others]
    L += ["", f"Next: {d['next']}"]
    if d["summary"]:
        L.append(f"Summary: {d['summary']}")
    return L


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Explain the state of an UpgradeLens run: the failed step, its inputs, logs and the next command.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--stage", type=int, choices=sorted(STAGES), help="show this stage, whatever its state")
    p.add_argument("--key", help="show this plan step (orchestrated run), for example stage-5-tests or agent-analyst")
    p.add_argument("--lines", type=int, default=25, help="lines of each log to show (default 25)")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument("--config")
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    run_dir = os.path.join(runs_root(read_json_safe(config)), a.run_id)
    if not os.path.isfile(os.path.join(run_dir, "status.json")):
        print(f"Run not found: {run_dir}")
        return 2
    if a.key and not is_orchestrated_run(run_dir):
        print("--key needs an orchestrated run; use --stage for a scripts-only run.")
        return 2
    try:
        d = diagnose(run_dir, a.run_id, a.stage, a.key, max(1, a.lines))
    except ValueError as e:
        print(e)
        return 2
    if a.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
    else:
        print("\n".join(render(d)))
    bad = d["problem"] and d["problem"]["state"] not in ("done", "skipped", "not run") and a.stage is None and not a.key
    return 1 if bad or d["lock"].get("stale") else 0


if __name__ == "__main__":
    run_main(main)

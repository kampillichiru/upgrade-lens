"""Runs one UpgradeLens agent on its own, on any run where the data that agent needs is there: outside the
run's plan, with the same prompt, write-scope guard and content checks as an orchestrated step.

The agent writes into a session folder, runs\\<id>\\standalone\\<session>\\out\\, never into the run itself.
Everything else (the run folder, its artifacts and earlier agent files, the ledger, both worktrees' git state,
the tool's scripts, config, agents and skills) is fingerprinted at begin and compared at end.

--action list    (default) for each agent: ready, missing (with the artifacts and the stage that writes them),
                 or nothing to do. Prints JSON with each ready agent's beginCommand.
--action begin   --step <step> [--repair]: checks the agent's data, creates the session folder, takes the
                 fingerprint, and prints JSON with the exact prompt for the subagent and its endCommand.
                 The test writer's work list (artifacts\\test-gaps.json) is written first when a scripts-only
                 run has the stage 4 and 5 artifacts but no list yet. --repair seeds the session with the run's
                 current tests and asks the writer to fix the ones that did not validate.
--action end     --session <s> --guard-sha256 <h> (both printed by begin): guard and content checks, as for an
                 orchestrated step. Writes standalone\\<session>\\result.json. An agent call is not counted against
                 agentBudget, and nothing is written to the ledger.
--action apply   --session <s>: copies an accepted answer into artifacts\\agent of a scripts-only run (one
                 started with run-pipeline.py --scripts-only), so its stages use it; prints the run-pipeline
                 command that reruns the stages from the first one that reads it. Refused for an orchestrated
                 run (its ledger owns artifacts\\agent) and when the agent's inputs changed since the call.

Exit codes: 0 ok or accepted; 2 usage, data missing, nothing to do, a stage running, not applicable;
            5 guard violation; 6 checks failed.

Examples:
  python scripts/run-agent.py --run-id 20261126-152851
  python scripts/run-agent.py --run-id 20261126-152851 --step analyst --action begin
  python scripts/run-agent.py --run-id 20261126-152851 --action end --session analyst-20261126-160000 --guard-sha256 <h>
  python scripts/run-agent.py --run-id 20261126-152851 --action apply --session analyst-20261126-160000
"""

import argparse
import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.agents import (AGENTS, AgentRun, agent_dir, agent_prompt, agent_write_scope, check_output, copy_output,  # noqa: E402
                        finish_output, missing_inputs)
from lib.core import (ROOT_DIR, command, config_value, file_sha256, is_orchestrated_run, now_iso, read_json_safe,  # noqa: E402
                      resolve_config_path, run_main, runs_root, s, write_json)
from lib.facts import GAP_SOURCES, test_gaps_path, write_test_gaps  # noqa: E402
from lib.guards import compare_fingerprint, file_tree_hashes, protected_fingerprint  # noqa: E402
from lib.ledger import run_lock  # noqa: E402
from lib.stages import written_by  # noqa: E402

SESSION_FILES = {"guard.json", "out", "result.json"}


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Run one UpgradeLens agent on its own, on a run where its data is there.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--action", choices=["list", "begin", "end", "apply"], default="list")
    p.add_argument("--step", choices=sorted(AGENTS), help="the agent (begin)")
    p.add_argument("--repair", action="store_true", help="test writer: repair the run's tests that did not validate")
    p.add_argument("--session", help="the session begin printed (end, apply)")
    p.add_argument("--guard-sha256", help="the guard hash begin printed (end)")
    p.add_argument("--config")
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    run_id = a.run_id
    run_dir = os.path.join(runs_root(read_json_safe(config)), run_id)
    if not os.path.isfile(os.path.join(run_dir, "status.json")):
        print(f"Run not found: {run_dir}")
        return 2
    orchestrated = is_orchestrated_run(run_dir)
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    git_cmd = s(config_value(cfg, "git.command", "git"))

    def gaps_can_be_written() -> bool:
        a_dir = os.path.join(run_dir, "artifacts")
        return (not orchestrated and not os.path.isfile(test_gaps_path(run_dir))
                and all(os.path.isfile(os.path.join(a_dir, n)) for n in GAP_SOURCES))

    def availability(step: str, repair: bool = False) -> dict:
        missing = missing_inputs(run_dir, step, repair)
        if step == "tests" and missing == ["test-gaps.json"] and gaps_can_be_written():
            missing = []
        if missing:
            return {"state": "missing", "missing": [{"artifact": m, "writtenBy": written_by("artifacts\\" + m)} for m in missing]}
        if step == "tests" and gaps_can_be_written():
            return {"state": "ready", "note": "the gap list (artifacts\\test-gaps.json) is written at begin"}
        why = AGENTS[step].nothing_to_do(AgentRun(step=step, run_id=run_id, run_dir=run_dir, repair=repair))
        if why:
            return {"state": "nothing-to-do", "reason": why}
        return {"state": "ready"}

    # ---------- list ----------

    if a.action == "list":
        rows = []
        for step, spec in AGENTS.items():
            row = {"step": step, "agent": spec.agent, "title": spec.title, **availability(step)}
            if row["state"] == "ready":
                row["beginCommand"] = command("run-agent.py", "--run-id", run_id, "--step", step, "--action", "begin",
                                              "--config", config)
            rows.append(row)
        print(json.dumps({"runId": run_id, "orchestrated": orchestrated, "agents": rows}, indent=2, ensure_ascii=False))
        return 0

    # ---------- begin ----------

    if a.action == "begin":
        if not a.step:
            print("--step is required for --action begin.")
            return 2
        if a.repair and a.step != "tests":
            print("--repair applies to the test writer (--step tests) only.")
            return 2
        lock = run_lock(run_dir)
        if lock["active"]:
            print(f"Stage {lock['key']} of run {run_id} is running (pid {lock['pid']}). Run the agent when it has finished.")
            return 2
        av = availability(a.step, a.repair)
        if av["state"] == "missing":
            print(f"The {a.step} agent cannot run on {run_id} yet. Missing:")
            for m in av["missing"]:
                print(f"  - artifacts\\{m['artifact']}  (written by {m['writtenBy']})")
            return 2
        if av["state"] == "nothing-to-do":
            print(f"Nothing for the {a.step} agent to do on {run_id}: {av['reason']}.")
            return 2
        if a.step == "tests" and gaps_can_be_written():
            gaps = write_test_gaps(run_dir, run_id)
            print(f"Wrote the work list {test_gaps_path(run_dir)} ({len(gaps)} gap(s)).", file=sys.stderr)
            if not gaps:
                print(f"Nothing for the tests agent to do on {run_id}: no gap.")
                return 2

        session = f"{a.step}{'-repair' if a.repair else ''}-{time.strftime('%Y%m%d-%H%M%S')}"
        base = os.path.join(run_dir, "standalone")
        n = 1
        while os.path.exists(os.path.join(base, session if n == 1 else f"{session}-{n}")):
            n += 1
        session = session if n == 1 else f"{session}-{n}"
        session_dir = os.path.join(base, session)
        run = AgentRun(step=a.step, run_id=run_id, run_dir=run_dir, out_dir=os.path.join(session_dir, "out"),
                       repair=a.repair, standalone=True)
        os.makedirs(run.out_dir)
        if a.repair:
            copy_output(agent_dir(run_dir), run.out_dir, a.step)

        guard_file = os.path.join(session_dir, "guard.json")
        write_json(guard_file, {
            "runId": run_id, "step": a.step, "repair": a.repair, "session": session, "inputHash": run.input_hash(),
            "createdAt": now_iso(),
            "protected": protected_fingerprint(run_dir, ROOT_DIR, git_cmd, writable=(f"standalone\\{session}\\*",)),
            "outFiles": file_tree_hashes(run.out_dir),
        })
        guard_sha = file_sha256(guard_file)
        print(json.dumps({
            "runId": run_id, "step": a.step, "session": session, "subagent": run.spec.agent, "outputFolder": run.out_dir,
            "inputHash": run.input_hash(), "prompt": agent_prompt(run),
            "endCommand": command("run-agent.py", "--run-id", run_id, "--action", "end", "--session", session,
                                  "--guard-sha256", guard_sha, "--config", config),
        }, indent=2, ensure_ascii=False))
        return 0

    # ---------- end / apply: an existing session ----------

    if not a.session:
        print(f"--session is required for --action {a.action} (the session begin printed).")
        return 2
    session_dir = os.path.join(run_dir, "standalone", a.session)
    guard_file = os.path.join(session_dir, "guard.json")
    g = read_json_safe(guard_file)
    if not isinstance(g, dict) or s(g.get("step")) not in AGENTS:
        print(f"Session not found: {session_dir}")
        return 2
    run = AgentRun(step=s(g["step"]), run_id=run_id, run_dir=run_dir, out_dir=os.path.join(session_dir, "out"),
                   repair=bool(g.get("repair")), standalone=True)
    result_file = os.path.join(session_dir, "result.json")

    if a.action == "end":
        if not a.guard_sha256:
            print("--guard-sha256 is required for --action end (begin printed it in endCommand).")
            return 2
        guard, errors = [], []
        if file_sha256(guard_file) != a.guard_sha256:
            guard.append(f"the guard record {guard_file} was changed")
        else:
            before = {k: s(v) for k, v in (g.get("protected") or {}).items()}
            guard += compare_fingerprint(before, protected_fingerprint(run_dir, ROOT_DIR, git_cmd,
                                                                       writable=(f"standalone\\{a.session}\\*",)))
            out_changed = compare_fingerprint({k: s(v) for k, v in (g.get("outFiles") or {}).items()},
                                              file_tree_hashes(run.out_dir))
            guard += [os.path.join("out", x) for x in agent_write_scope(out_changed, run.step)]
            guard += [x for x in sorted(os.listdir(session_dir)) if x not in SESSION_FILES]
        if not guard:
            errors = check_output(run)
        accepted = not guard and not errors
        if accepted:
            finish_output(run)
        write_json(result_file, {
            "schemaVersion": 1, "runId": run_id, "step": run.step, "session": a.session, "accepted": accepted,
            "guard": guard, "errors": errors, "inputHash": g.get("inputHash"), "output": run.output, "finishedAt": now_iso(),
        })
        if guard:
            print(f"Rejected: {run.spec.agent} changed files it must not change:")
            for x in guard[:20]:
                print(f"  - {x}")
            return 5
        if errors:
            print(f"Rejected: {len(errors)} problem(s) in {run.output}:")
            for x in errors:
                print(f"  - {x}")
            print(f"Run the agent again: {command('run-agent.py', '--run-id', run_id, '--step', run.step, '--action', 'begin')}"
                  + (" --repair" if run.repair else ""))
            return 6
        print(f"Accepted: {run.output}")
        if orchestrated:
            print("This run is orchestrated: its artifacts\\agent stays as the run's plan left it. The answer is kept in the "
                  "session folder.")
        else:
            print(f"To let the run's stages use it: {command('run-agent.py', '--run-id', run_id, '--action', 'apply', '--session', a.session)}")
        return 0

    # apply
    res = read_json_safe(result_file)
    if not isinstance(res, dict) or not res.get("accepted"):
        print(f"Session {a.session} has no accepted answer. Run its endCommand first; only an accepted answer is applied.")
        return 2
    if orchestrated:
        print(f"Run {run_id} is orchestrated: its ledger owns artifacts\\agent, so a separate agent call is not applied to it. "
              f"The accepted answer stays in {run.out_dir}.")
        return 2
    if s(res.get("inputHash")) != run.input_hash():
        print(f"The {run.step} agent's inputs changed since session {a.session}. Run the agent again.")
        return 2
    copied = copy_output(run.out_dir, agent_dir(run_dir), run.step)
    print(f"Applied session {a.session} to {agent_dir(run_dir)}: {', '.join(copied)}")
    print(f"Rerun the stages that use it: {command('run-pipeline.py', '--scripts-only', '--run-id', run_id, '--from', str(run.spec.feeds_stage))}")
    return 0


if __name__ == "__main__":
    run_main(main)

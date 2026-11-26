"""Guards around each agent step of an orchestrated UpgradeLens run: begin before the subagent runs, end
after it returns. Also prints the input hash an agent writes into its proposal.

What each agent reads, writes and is checked on is defined once, in lib\\agents.py (AGENTS). This script only
adds what an orchestrated run needs: the order, the budget, the ledger and the retry with the errors.
To run one agent on its own (outside the plan), use run-agent.py.

--action begin   (orchestrator) Only when next-step.py says this agent step is next. Checks the inputs and the
                 agent budget (config agentBudget), records the call, takes a fingerprint of everything the agent
                 must not change, and prints JSON with the exact prompt for the subagent.
--action end     (orchestrator) After the subagent returns. Fails the step when:
                   guard   the agent changed anything outside its allowed files (the run folder outside
                           artifacts\\agent, a worktree's git state, the tool's scripts, config, agents or skills,
                           or another agent's files). The run stops.
                   checks  its output is missing, stale or incomplete (lib\\agent_checks.py): for example a gap with
                           no test and no reason, a changed method with no reason, a confirmed release note with
                           no source. next-step.py allows a retry (orchestration.maxAgentAttempts, default 2),
                           with the errors in the new prompt.
                 An accepted output is finished as its AgentSpec says (the reviewer's becomes review.json).
--action hash    (agents) prints the SHA-256 of a step's inputs (lib\\agents.py, agent_inputs).

Exit codes: 0 done; 2 usage, out of order or budget spent; 5 guard violation (run stops); 6 checks failed.

Examples:
  python scripts/agent-step.py --run-id 20261126-152851 --key agent-analyst --action begin
  python scripts/agent-step.py --run-id 20261126-152851 --key agent-analyst --action end
  python scripts/agent-step.py --run-id 20261126-152851 --step tests --action hash
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.agents import (AGENTS, AgentRun, agent_dir, agent_inputs, agent_prompt, agent_write_scope, check_output,  # noqa: E402
                        finish_output, missing_inputs)
from lib.core import (ROOT_DIR, as_list, command, config_value, file_sha256, get, input_hash, items, now_iso,  # noqa: E402
                      read_json_safe, resolve_config_path, run_main, runs_root, s, to_int, write_json)
from lib.facts import run_facts  # noqa: E402
from lib.guards import compare_fingerprint, file_tree_hashes, protected_fingerprint  # noqa: E402
from lib.ledger import key_entries, ledger_entry, read_ledger, set_ledger_entry  # noqa: E402
from lib.plan import agent_key_step, is_repair_key, max_stage_attempts, next_action, run_plan  # noqa: E402


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Begin and end guards around an UpgradeLens agent step.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--key", help="the agent key next-step.py printed (begin, end)")
    p.add_argument("--step", choices=sorted(AGENTS), help="the agent step (hash)")
    p.add_argument("--action", choices=["hash", "begin", "end", "record", "review"], default="hash")
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
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    ledger = read_ledger(run_dir)
    key = a.key
    step = a.step

    if key:
        step = agent_key_step(key)
        if not step:
            print(f"Unknown agent key '{key}'.")
            return 2
    if not step:
        print("--key (begin, end) or --step (hash) is required.")
        return 2

    # ---------- hash: for the agents ----------

    if a.action == "hash":
        if not AGENTS[step].hashed:
            print(f"The {step} agent has no input hash.")
            return 2
        inputs = agent_inputs(run_dir, step)
        for f in inputs:
            if not os.path.isfile(f):
                print(f"Missing input for {step}: {f}. Run the earlier stages first.")
                return 2
        print(input_hash(inputs))
        return 0

    if a.action in ("record", "review"):
        print(f"--action {a.action} is replaced by --action begin / end, run by the orchestrator (next-step.py prints both "
              "commands). An agent never records its own call.")
        return 2
    if not ledger:
        print(f"Run {run_id} has no orchestration ledger. Agent steps of the plan run only in orchestrated runs "
              f"(run-stage.py --new). To run one agent on its own: {command('run-agent.py', '--run-id', run_id, '--action', 'list')}")
        return 2
    if not key:
        print(f"--key is required for --action {a.action} (the key next-step.py printed).")
        return 2

    spec = AGENTS[step]
    run = AgentRun(step=step, run_id=run_id, run_dir=run_dir, repair=is_repair_key(key))
    out_dir = agent_dir(run_dir)
    plan = run_plan(to_int(config_value(cfg, "orchestration.maxTestRepairRounds", 1), 1))
    max_attempts = to_int(config_value(cfg, "orchestration.maxAgentAttempts", 2), 2)
    git_cmd = s(config_value(cfg, "git.command", "git"))

    # ---------- begin ----------

    if a.action == "begin":
        nxt = next_action(plan, ledger, run_facts(run_dir, ledger), max_attempts, max_stage_attempts(cfg))
        if nxt["action"] != "run-agent" or nxt["key"] != key:
            print(f"Out of order: next-step says '{nxt['action']} {nxt['key']}', not 'run-agent {key}'. "
                  f"Run {command('next-step.py', '--run-id', run_id)} and do what it prints.")
            return 2
        missing = missing_inputs(run_dir, step, run.repair)
        if missing:
            print(f"Missing input for {step}: {', '.join(os.path.join(run_dir, 'artifacts', m) for m in missing)}.")
            return 2
        hash_value = run.input_hash()
        # Budget: one call per begin, retries included.
        budget = to_int(config_value(cfg, "agentBudget", 12), 12)
        usage_path = os.path.join(out_dir, "usage.json")
        rows = items(get(read_json_safe(usage_path), "agents"))
        total = sum(to_int(r.get("calls")) for r in rows)
        if total + 1 > budget:
            set_ledger_entry(run_dir, key, "agent", "failed",
                             f"budget: {total} of {budget} agent calls used. Raise agentBudget in the config and start a new run.")
            print(f"Agent budget spent: {total} of {budget} calls. The run stops here.")
            return 2
        rows.append({"agent": spec.agent, "step": step, "key": key, "calls": 1, "recordedAt": now_iso()})
        write_json(usage_path, {"schemaVersion": 1, "runId": run_id, "budget": budget, "totalCalls": total + 1, "agents": rows})
        # Some agents start from a clean answer file on a retry; the others may improve their earlier files.
        if spec.fresh_start:
            try:
                os.remove(run.output)
            except OSError:
                pass
        os.makedirs(out_dir, exist_ok=True)

        failed = key_entries(ledger, key, "failed")
        prev_errors = []
        if failed and failed[-1].get("errorsFile") and os.path.isfile(failed[-1]["errorsFile"]):
            prev_errors = as_list(get(read_json_safe(failed[-1]["errorsFile"]), "errors"))
        prompt = agent_prompt(run, prev_errors)

        attempt = len(key_entries(ledger, key)) + 1
        guard_file = os.path.join(run_dir, "orchestration", f"{key}-{attempt}.guard.json")
        write_json(guard_file, {
            "key": key, "step": step, "attempt": attempt, "inputHash": hash_value, "createdAt": now_iso(),
            "protected": protected_fingerprint(run_dir, ROOT_DIR, git_cmd),
            "agentFiles": file_tree_hashes(out_dir),
        })
        set_ledger_entry(run_dir, key, "agent", "running", nxt["reason"],
                         {"agent": spec.agent, "inputHash": hash_value, "guardFile": guard_file, "guardSha256": file_sha256(guard_file)})
        print(json.dumps({
            "runId": run_id, "key": key, "step": step, "attempt": attempt, "subagent": spec.agent, "inputHash": hash_value,
            "prompt": prompt,
            "endCommand": command("agent-step.py", "--run-id", run_id, "--key", key, "--action", "end", "--config", config),
        }, indent=2, ensure_ascii=False))
        return 0

    # ---------- end ----------

    entry = ledger_entry(ledger, key)
    if not entry or entry.get("state") != "running":
        print(f"Agent step {key} was not begun (state: {entry.get('state') if entry else 'none'}). Run its beginCommand first.")
        return 2

    def stop_step(state: str, reason: str, errors, code: int) -> int:
        err_file = os.path.join(run_dir, "orchestration", f"{key}-{entry.get('attempt')}.errors.json")
        write_json(err_file, {"key": key, "attempt": entry.get("attempt"), "errors": list(errors)})
        set_ledger_entry(run_dir, key, "agent", state, reason, {"errorsFile": err_file}, update=True)
        print(f"Agent step {key} {state}. {reason}")
        for e in errors:
            print(f"  - {e}")
        print(f"Next: {command('next-step.py', '--run-id', run_id)}")
        return code

    # 1. write scope
    guard_file = s(entry.get("guardFile"))
    if not os.path.isfile(guard_file) or file_sha256(guard_file) != entry.get("guardSha256"):
        return stop_step("failed", "guard: the guard record of this step was changed or removed", [f"guard file {guard_file} changed"], 5)
    g = read_json_safe(guard_file) or {}
    before_protected = {k: s(v) for k, v in (g.get("protected") or {}).items()}
    before_agent = {k: s(v) for k, v in (g.get("agentFiles") or {}).items()}
    bad = compare_fingerprint(before_protected, protected_fingerprint(run_dir, ROOT_DIR, git_cmd))
    agent_changed = compare_fingerprint(before_agent, file_tree_hashes(out_dir))
    bad += [os.path.join("artifacts", "agent", x) for x in agent_write_scope(agent_changed, step)]
    if bad:
        return stop_step("failed", f"guard: {spec.agent} changed files it must not change: {', '.join(bad[:5])}", bad, 5)

    # 2. content checks
    errors = check_output(run)
    if errors:
        left = max_attempts - len(key_entries(ledger, key))
        tail = f"; {left} retry left" if left > 0 else "; no retry left"
        return stop_step("failed", f"checks: {len(errors)} problem(s) in {spec.output}{tail}", errors, 6)

    # 3. an accepted output is finished (the reviewer's answer becomes review.json)
    finish_output(run)
    set_ledger_entry(run_dir, key, "agent", "done", "output accepted", update=True)
    print(f"Agent step {key} done: {run.output} accepted. Next: {command('next-step.py', '--run-id', run_id)}")
    return 0


if __name__ == "__main__":
    run_main(main)

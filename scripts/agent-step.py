"""Guards around each agent step of an orchestrated UpgradeLens run: begin before the subagent runs, end
after it returns. Also prints the input hash an agent writes into its proposal.

--action begin   (orchestrator) Only when next-step.py says this agent step is next. Checks the inputs and the
                 agent budget (config agentBudget), records the call, takes a fingerprint of everything the agent
                 must not change, and prints JSON with the exact prompt for the subagent.
--action end     (orchestrator) After the subagent returns. Fails the step when:
                   guard   the agent changed anything outside its allowed files (the run folder outside
                           artifacts\\agent, a worktree's git state, the tool's scripts, config, agents or skills,
                           or another agent's files). The run stops.
                   checks  its output is missing, stale or incomplete (lib\\orchestration.py, check_*):
                           for example a gap with no test and no reason, a changed method with no reason, a
                           confirmed release note with no source. next-step.py allows a retry
                           (orchestration.maxAgentAttempts, default 2), with the errors in the new prompt.
                 For the reviewer, end also writes artifacts\\agent\\review.json with each test file's hash.
--action hash    (agents) prints the SHA-256 of a step's inputs (lib\\core.py, agent_inputs).

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

from lib.core import (ROOT_DIR, agent_inputs, as_list, command, config_value, file_sha256, get, input_hash, items,  # noqa: E402
                      native_path, now_iso, read_json_safe, resolve_config_path, run_main, runs_root, s, to_int, write_json)
from lib.orchestration import (AGENT_FILES, agent_key_step, agent_prompt, agent_write_scope, check_analyst_proposal,  # noqa: E402
                               check_release_notes_proposal, check_review_answer, check_tests_proposal, compare_fingerprint,
                               file_tree_hashes, ledger_entry, next_action, protected_fingerprint, read_ledger,
                               release_notes_refs, run_facts, run_plan, set_ledger_entry)


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Begin and end guards around an UpgradeLens agent step.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--key", help="the agent key next-step.py printed (begin, end)")
    p.add_argument("--step", choices=["analyst", "release-notes", "tests", "reviewer"], help="the agent step (hash)")
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
    artifacts = os.path.join(run_dir, "artifacts")
    agent_dir = os.path.join(artifacts, "agent")
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
        if step == "reviewer":
            print("The reviewer has no input hash.")
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
        print(f"Run {run_id} has no orchestration ledger. Agent steps run only in orchestrated runs (run-stage.py --new).")
        return 2
    if not key:
        print(f"--key is required for --action {a.action} (the key next-step.py printed).")
        return 2

    info = AGENT_FILES[step]
    plan = run_plan(to_int(config_value(cfg, "orchestration.maxTestRepairRounds", 1), 1))
    max_attempts = to_int(config_value(cfg, "orchestration.maxAgentAttempts", 2), 2)
    head_wt = get(read_json_safe(os.path.join(artifacts, "environment.json")), "branches", "head", "worktree")
    git_cmd = s(config_value(cfg, "git.command", "git"))

    def validated_test_ids():
        b = read_json_safe(os.path.join(artifacts, "behavior-results.json"))
        # Validated tests, and tests that prove a behavior change (pass on master, fail on change_branch).
        return [s(t.get("id")) for t in items(get(b, "tests"))
                if t.get("validated") or (get(t, "base", "passed") and not get(t, "head", "passed"))]

    # ---------- begin ----------

    if a.action == "begin":
        nxt = next_action(plan, ledger, run_facts(run_dir, ledger), max_attempts)
        if nxt["action"] != "run-agent" or nxt["key"] != key:
            print(f"Out of order: next-step says '{nxt['action']} {nxt['key']}', not 'run-agent {key}'. "
                  f"Run {command('next-step.py', '--run-id', run_id)} and do what it prints.")
            return 2
        hash_value = ""
        if step != "reviewer":
            inputs = agent_inputs(run_dir, step)
            for f in inputs:
                if not os.path.isfile(f):
                    print(f"Missing input for {step}: {f}.")
                    return 2
            hash_value = input_hash(inputs)
        # Budget: one call per begin, retries included.
        budget = to_int(config_value(cfg, "agentBudget", 12), 12)
        usage_path = os.path.join(agent_dir, "usage.json")
        rows = items(get(read_json_safe(usage_path), "agents"))
        total = sum(to_int(r.get("calls")) for r in rows)
        if total + 1 > budget:
            set_ledger_entry(run_dir, key, "agent", "failed",
                             f"budget: {total} of {budget} agent calls used. Raise agentBudget in the config and start a new run.")
            print(f"Agent budget spent: {total} of {budget} calls. The run stops here.")
            return 2
        rows.append({"agent": info["agent"], "step": step, "key": key, "calls": 1, "recordedAt": now_iso()})
        write_json(usage_path, {"schemaVersion": 1, "runId": run_id, "budget": budget, "totalCalls": total + 1, "agents": rows})
        # A retry starts from a clean answer file for the reviewer; other agents may improve their earlier files.
        if step == "reviewer":
            try:
                os.remove(os.path.join(agent_dir, "review-answer.json"))
            except OSError:
                pass
        os.makedirs(agent_dir, exist_ok=True)

        failed = [e for e in as_list(ledger.get("entries")) if e and e.get("key") == key and e.get("state") == "failed"]
        prev_errors = []
        if failed and failed[-1].get("errorsFile") and os.path.isfile(failed[-1]["errorsFile"]):
            prev_errors = as_list(get(read_json_safe(failed[-1]["errorsFile"]), "errors"))
        review_ids = validated_test_ids() if step == "reviewer" else []
        prompt = agent_prompt(step, run_id, run_dir, hash_value, key, review_ids)
        if prev_errors:
            prompt += "\nYour previous attempt was rejected by the checks. Fix every item:\n- " + "\n- ".join(s(x) for x in prev_errors)

        attempt = len([e for e in as_list(ledger.get("entries")) if e and e.get("key") == key]) + 1
        guard_file = os.path.join(run_dir, "orchestration", f"{key}-{attempt}.guard.json")
        write_json(guard_file, {
            "key": key, "step": step, "attempt": attempt, "inputHash": hash_value, "createdAt": now_iso(),
            "protected": protected_fingerprint(run_dir, ROOT_DIR, git_cmd),
            "agentFiles": file_tree_hashes(agent_dir),
        })
        set_ledger_entry(run_dir, key, "agent", "running", nxt["reason"],
                         {"agent": info["agent"], "inputHash": hash_value, "guardFile": guard_file, "guardSha256": file_sha256(guard_file)})
        print(json.dumps({
            "runId": run_id, "key": key, "step": step, "attempt": attempt, "subagent": info["agent"], "inputHash": hash_value,
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
    agent_changed = compare_fingerprint(before_agent, file_tree_hashes(agent_dir))
    bad += [os.path.join("artifacts", "agent", x) for x in agent_write_scope(agent_changed, step)]
    if bad:
        return stop_step("failed", f"guard: {info['agent']} changed files it must not change: {', '.join(bad[:5])}", bad, 5)

    # 2. content checks
    out = os.path.join(agent_dir, info["output"])
    doc = read_json_safe(out)
    expected = input_hash(agent_inputs(run_dir, step)) if step != "reviewer" else ""
    manifest = None
    if step == "analyst":
        errors = check_analyst_proposal(doc, expected, read_json_safe(os.path.join(artifacts, "change-inventory.json")),
                                        read_json_safe(os.path.join(artifacts, "impact-findings.json")))
    elif step == "release-notes":
        refs = release_notes_refs(read_json_safe(os.path.join(artifacts, "library-api-diff.json")),
                                  read_json_safe(os.path.join(artifacts, "dependency-diff.json")))
        errors = check_release_notes_proposal(doc, expected, refs)
    elif step == "tests":
        gaps = items(get(read_json_safe(os.path.join(artifacts, "test-gaps.json")), "gaps"))
        errors = check_tests_proposal(doc, expected, gaps, os.path.join(agent_dir, "tests"), head_wt,
                                      to_int(config_value(cfg, "orchestration.maxTests", 15), 15))
    else:
        manifest = read_json_safe(os.path.join(agent_dir, "tests-manifest.json"))
        errors = check_review_answer(doc, validated_test_ids())
        if manifest and s(manifest.get("writer")) == info["agent"]:
            errors.append("the reviewer wrote these tests; a review needs an independent reviewer")
    if errors:
        attempts = len([e for e in as_list(ledger.get("entries")) if e and e.get("key") == key])
        left = max_attempts - attempts
        tail = f"; {left} retry left" if left > 0 else "; no retry left"
        return stop_step("failed", f"checks: {len(errors)} problem(s) in {info['output']}{tail}", errors, 6)

    # 3. the reviewer's answer becomes review.json, with each reviewed file's hash (stage 9 checks them)
    if step == "reviewer":
        rows = []
        for r in items(doc.get("reviews")):
            t = next((x for x in items(get(manifest, "tests")) if x.get("id") == r.get("testId")), {})
            f = os.path.join(agent_dir, "tests", native_path(t.get("relativePath")))
            rows.append({"testId": r.get("testId"), "fileSha256": file_sha256(f) if os.path.isfile(f) else "",
                         "writer": get(manifest, "writer"), "reviewer": info["agent"], "verdict": r.get("verdict"),
                         "notes": r.get("notes")})
        write_json(os.path.join(agent_dir, "review.json"), {"schemaVersion": 1, "runId": run_id, "reviews": rows})

    set_ledger_entry(run_dir, key, "agent", "done", "output accepted", update=True)
    print(f"Agent step {key} done: {out} accepted. Next: {command('next-step.py', '--run-id', run_id)}")
    return 0


if __name__ == "__main__":
    run_main(main)

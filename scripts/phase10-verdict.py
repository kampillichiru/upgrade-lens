"""Stage 10 of UpgradeLens (verdict): sets the verdict and the exit code from the stage outputs, then
rebuilds the report with the verdict in it.

Verdicts (approach section 10):
  blocked                  any blocking reason. Exit 1.
  safe-with-listed-tests   no blocking reason, but something is listed: a manual item with no decision,
                           an open or unverified finding, untraced code, a changed method no test executes,
                           a library change not validated against its release notes, a gap, a missing or
                           stale agent output.
                           Exit 0.
  safe                     no blocking reason, nothing listed, every required stage ran. Exit 0.
A required stage that did not run can never give safe.

Blocking reasons:
  - a stage recorded as failed
  - an existing test that passes on master and fails on change_branch (stage 1 test comparison)
  - a probe or a test that could not run (stage 5 blockingErrors)
  - a confirmed behavior change that no person accepted (a probe or test differs on change_branch)
  - a validated new test with no agent review that passed (missing, stale, same author, or failed)
  - a changed code or config file that no stage analysed (category "other" with a code or config extension)

Writes verdict.json in the run folder, sets the run state to verdict-blocked, verdict-listed or verdict-safe,
and regenerates reports\\report.md and report.html with the verdict.

Exit codes:
  0  safe or safe-with-listed-tests
  1  blocked
  2  usage or run problem (no run, stage 9 not done)

Example:
  python scripts/phase10-verdict.py --run-id 20261126-131717
"""

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.analysis import finding_status  # noqa: E402
from lib.core import (agent_proposal, as_list, get, items, log, now_iso, ps_sorted, read_json_safe, resolve_config_path,  # noqa: E402
                      resolve_run_id, run_main, run_script, runs_root, s, set_run_state, set_stage_log, set_stage_step,
                      verdict, write_json)

LATER_STATES = ["phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe"]
REQUIRED = ["changed-files", "dependencies-api", "impact", "behavior", "flows", "manual", "output", "review"]


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 10: verdict and exit code.")
    p.add_argument("--run-id")
    p.add_argument("--config")
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))
    run_id = resolve_run_id(runs, a.run_id, LATER_STATES)
    if not run_id:
        print(f"No run with stage 9 done under {runs}. Run phase9-review.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    artifacts = os.path.join(run_dir, "artifacts")
    status_file = os.path.join(run_dir, "status.json")
    set_stage_log(os.path.join(run_dir, "logs", "phase10.log"))
    art = lambda name: read_json_safe(os.path.join(artifacts, name))  # noqa: E731
    status = read_json_safe(status_file)
    impact, flows, behavior, manual = art("impact-findings.json"), art("flows.json"), art("behavior-results.json"), art("manual-items.json")
    gaps, inventory, new_tests = art("gaps.json"), art("change-inventory.json"), art("new-tests.json")
    test_results, lib_api, dep_diff = art("test-results.json"), art("library-api-diff.json"), art("dependency-diff.json")
    changed_methods = art("changed-methods.json")
    review = read_json_safe(os.path.join(run_dir, "review", "review-record.json"))
    tests_location = get(new_tests, "location") or "artifacts\\agent\\tests"
    if not all((status, impact, flows, behavior, manual, review)):
        print(f"Run incomplete: {run_dir} (needs stages 5 to 9)")
        return 2

    log(f"phase 10 verdict for run {run_id}")
    blocking, listed = [], []
    findings = items(impact.get("findings"))
    checks = items(behavior.get("checks"))
    tests = items(behavior.get("tests"))
    decisions = items(get(review, "manual", "decisions"))

    # 1. stage failures
    for st in items(status.get("steps")):
        if st.get("state") == "failed":
            blocking.append(f"stage {st.get('name')} failed: {st.get('reason')}")

    # 2. checks that could not run
    blocking += [f"check did not run: {e}" for e in items(behavior.get("blockingErrors"))]

    # 3. decisions: which findings a person accepted
    accepted = set()
    for d in decisions:
        if d.get("decision") != "accept":
            continue
        if d.get("kind") == "finding":
            accepted.add(s(d.get("ref")))
        if d.get("kind") == "probe-difference":
            for c in checks:
                if c.get("source") == "probe" and c.get("id") == d.get("ref"):
                    accepted.update(f["id"] for f in findings if f.get("ruleId") == c.get("ruleId"))
    accepted_rows = [{"findingId": x, "decision": "accept"} for x in accepted]

    # 4. confirmed behavior changes not accepted, and test failures on change_branch
    status_rows = []
    for f in findings:
        st = finding_status(f, checks, accepted_rows)
        status_rows.append({"id": f["id"], "status": st})
        if st == "confirmed":
            diffs = [f"{c.get('source')} {c.get('id')} {c.get('outcome')}" for c in checks
                     if c.get("ruleId") == f.get("ruleId") and c.get("outcome") != "same"]
            blocking.append(f"confirmed behavior change not accepted: {f['id']} [{f.get('ruleId')}] {f.get('title')}. "
                            f"Differences: {'; '.join(diffs)}")
    # A probe that differs between the branches is a behavior change even when no finding is linked to it.
    # It blocks until a person accepts its probe-difference item.
    accepted_probes = {d.get("ref") for d in decisions if d.get("decision") == "accept" and d.get("kind") == "probe-difference"}
    for pr in items(behavior.get("probes")):
        if pr.get("outcome") == "different" and pr.get("id") not in accepted_probes:
            blocking.append(f"behavior change not accepted: probe {pr.get('id')} ({pr.get('request')}) differs on change_branch: "
                            f"{'; '.join(as_list(pr.get('differences')))}")
    for t in tests:
        if t.get("validated") is False and get(t, "base", "passed") and not get(t, "head", "passed"):
            blocking.append(f"test regression on change_branch: {t.get('id')} fails where master passes: {get(t, 'head', 'detail')}")

    # 5. validated new tests need a passing agent review
    review_rows = items(get(review, "agent", "tests"))
    for t in tests:
        if not t.get("validated"):
            continue
        row = next((r for r in review_rows if r.get("testId") == t.get("id")), None)
        if not row or row.get("result") != "reviewed":
            why = row.get("result") if row else "missing"
            blocking.append(f"unreviewed new test: {t.get('id')} ({why}). It is placed, uncommitted, in {tests_location}; do not "
                            "commit it until a reviewer passes it.")

    # 6. changed files no stage analysed
    for f in items(get(inventory, "files")):
        if f.get("category") == "other" and re.search(r"\.(java|xml|ya?ml|properties|json|toml|gradle|kts)$", s(f.get("path")), re.I):
            blocking.append(f"unreviewed change: {f.get('path')} is not analysed by any stage (category other)")

    # ---- listed items (verdict cannot be a clean pass while any of these exist) ----

    pending = as_list(get(review, "manual", "pending"))
    if pending:
        listed.append(f"manual review pending: {len(pending)} item(s) without a valid decision ({', '.join(s(x) for x in pending)})")
    listed += [f"invalid decision for {r.get('itemId')}: {r.get('reason')}" for r in items(get(review, "manual", "invalid"))]
    rule_of = {f["id"]: f.get("ruleId") for f in findings}
    for row in status_rows:
        if row["status"] in ("open", "hypothesis"):
            listed.append(f"finding {row['id']} [{rule_of.get(row['id'])}] not verified by any check ({row['status']})")
    for fl in items(flows.get("flows")):
        if as_list(fl.get("untraced")):
            listed.append(f"flow {fl.get('id')} has code no test executes: {', '.join(as_list(fl.get('untraced')))}")
    for t in tests:
        if not t.get("validated") and get(t, "base", "passed") and get(t, "head", "passed"):
            listed.append(f"test {t.get('id')} passes on both branches but is not validated: it does not fail for every injected regression")
    for t in tests:
        if not get(t, "base", "passed"):
            listed.append(f"test {t.get('id')} fails on master, so it was not written against master's behavior. Fix it and run again.")
    listed += [f"not run: {g.get('item')}. {g.get('reason')}" for g in items(get(gaps, "gaps"))]
    listed += [f"agent review missing for {r.get('testId')}" for r in review_rows if r.get("result") == "missing"]

    # Agent proposals count only when their input hash matches the current inputs.
    analyst = agent_proposal(run_dir, "analyst", "hypotheses.json")
    if analyst["state"] != "used":
        listed.append(f"analyst proposal {analyst['state']}: no hypotheses and no explained change reasons for this run's inputs")
    tests_proposal = agent_proposal(run_dir, "tests", "tests-manifest.json")
    if tests_proposal["state"] != "used":
        untraced = [fl.get("id") for fl in items(flows.get("flows")) if as_list(fl.get("untraced"))]
        if untraced:
            listed.append(f"no current tests proposal ({tests_proposal['state']}) for untraced flow(s): {', '.join(untraced)}")
    for m in items(get(changed_methods, "methods")):
        if not m.get("tested") and m.get("kind") != "file":
            listed.append(f"changed {m.get('kind')} {m.get('name')} in {m.get('file')} is not executed by any test on change_branch")

    # Library changes must be validated against the release notes.
    notes = agent_proposal(run_dir, "release-notes", "release-notes.json")
    need_notes = []
    if get(lib_api, "available"):
        need_notes += [i.get("id") for i in items(lib_api.get("items")) if i.get("severity") in ("high", "medium")]
    if dep_diff:
        need_notes += [c.get("ga") for c in items(dep_diff.get("changed")) if c.get("direct") and c.get("major")]
    if need_notes:
        if notes["state"] != "used":
            more = ", ..." if len(need_notes) > 8 else ""
            listed.append(f"release-notes validation {notes['state']} for {len(need_notes)} library change(s): "
                          f"{', '.join(s(x) for x in need_notes[:8])}{more}")
        else:
            covered = {s(n.get("ref")) for n in items(get(notes, "doc", "items"))}
            missing = [x for x in need_notes if s(x) not in covered]
            if missing:
                listed.append(f"release notes not checked for: {', '.join(s(x) for x in missing)}")
            for n in items(get(notes, "doc", "items")):
                if n.get("result") == "contradicted":
                    listed.append(f"release notes contradict the expected change for {n.get('ref')} ({n.get('artifact')}): {n.get('claim')}")

    # Test-by-test comparison of the two builds (stage 1).
    if test_results:
        compared = items(test_results.get("tests"))
        blocking += [f"existing test passes on master and fails on change_branch: {t.get('test')} ({t.get('message')})"
                     for t in compared if t.get("change") == "now-failing"]
        listed += [f"test removed on change_branch: {t.get('test')}" for t in compared if t.get("change") == "removed"]
        listed += [f"test skipped on change_branch but run on master: {t.get('test')}" for t in compared if t.get("change") == "now-skipped"]

    placed = items(get(new_tests, "placed"))
    if placed:
        listed.append(f"new tests are placed, uncommitted, in the change worktree {new_tests.get('location')} (branch "
                      f"{new_tests.get('branch')}) for a developer to review, commit and push ({len(placed)} file(s); patch: "
                      f"{new_tests.get('patch')})")

    # required stages: stage 2 to 9 must be done (behavior done, not done-with-errors)
    required_ran = True
    for name in REQUIRED:
        steps = [x for x in items(status.get("steps")) if x.get("name") == name]
        if not steps or steps[-1].get("state") != "done":
            required_ran = False
            listed.append(f"required stage {name} did not complete cleanly")

    result = verdict(blocking, listed, required_ran)
    state_name = {"blocked": "verdict-blocked", "safe-with-listed-tests": "verdict-listed", "safe": "verdict-safe"}[result["verdict"]]

    write_json(os.path.join(run_dir, "verdict.json"), {
        "schemaVersion": 1, "runId": run_id, "verdict": result["verdict"], "exitCode": result["exitCode"],
        "blocking": blocking, "listed": listed, "acceptedFindings": ps_sorted(accepted), "findingStatus": status_rows,
        "decidedAt": now_iso(),
    })
    log(f"VERDICT: {result['verdict']} (exit {result['exitCode']}). Blocking: {len(blocking)}. Listed: {len(listed)}.")
    for b in blocking:
        log(f"  blocking: {b}")
    for x in listed:
        log(f"  listed: {x}")

    set_stage_step(status, "verdict", "done", result["verdict"], os.path.join(run_dir, "verdict.json"))
    set_run_state(status, state_name, "phase10")
    write_json(status_file, status)

    # Rebuild the report with the verdict in it.
    run_script("phase8-output.py", ["--run-id", run_id, "--report-only", "--config", config])
    log(f"report updated with the verdict: {os.path.join(run_dir, 'reports', 'report.md')}")
    return result["exitCode"]


if __name__ == "__main__":
    run_main(main)

"""Stage 10 of UpgradeLens (verdict): the answer of the two-branch comparison, from the stage outputs, then
the report again with it. It reports what the checks showed; it does not wait for anybody's approval.

Verdicts (lib\\core.py verdict):
  incomplete         a stage failed or a check could not run, so the comparison is not complete. Exit 1.
  different          at least one check gave a different result on change_branch. Exit 1. Each difference is
                     counted once: a probe, a new test method, or an existing test (lib\\analysis.py differences).
  same-where-tested  no difference, but some changes are not proven either way: a finding no check covered, a
                     changed line no test runs, existing tests whose assertions miss a changed line, a library
                     change no release note mentions, a part of the regression that did not run. Exit 0.
  same               no difference and nothing left unproven. Exit 0.

Manual items (stage 7) are questions for the reader of the report. A person may record in
review\\manual-decisions.json that a difference is expected; the report notes it, and the verdict ignores it.

Writes verdict.json in the run folder (differences, incomplete, notProven, notes), sets the run state to
verdict-<verdict>, and regenerates reports\\report.md and report.html with the verdict.

Exit codes:
  0  same or same-where-tested
  1  different or incomplete (a result, not a failure of the stage)
  2  usage or run problem (no run, stage 9 not done)

Example:
  python .github/upgradelens/scripts/phase10-verdict.py --run-id 20261126-131717
"""

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.analysis import differences, finding_status, flows_checked_by_new_tests  # noqa: E402
from lib.agents import agent_proposal  # noqa: E402
from lib.core import (as_list, get, items, log, now_iso, ps_sorted, read_json_safe, resolve_config_path,  # noqa: E402
                      run_main, run_script, runs_root, s, set_run_state, set_stage_log, set_stage_step,
                      VERDICTS, verdict, write_json)
from lib.standalone import StagePaths, add_standalone_arg, select_run  # noqa: E402

LATER_STATES = ["phase9-done"] + [f"verdict-{v}" for v in VERDICTS]
REQUIRED = ["changed-files", "dependencies-api", "impact", "behavior", "flows", "manual", "output", "review"]


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 10: verdict and exit code.")
    p.add_argument("--run-id")
    p.add_argument("--config")
    add_standalone_arg(p)
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))
    run_id = select_run(runs, a.run_id, a.standalone, LATER_STATES)
    if not run_id:
        print(f"No run with stage 9 done under {runs}. Run phase9-review.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    paths = StagePaths(run_dir, a.standalone)
    status_file = paths.out("status.json")
    verdict_file = paths.out("verdict.json")
    set_stage_log(paths.out("logs", "phase10.log"))
    art = lambda name: paths.read("artifacts", name)  # noqa: E731
    status = paths.read("status.json")
    impact, flows, behavior, manual = art("impact-findings.json"), art("flows.json"), art("behavior-results.json"), art("manual-items.json")
    gaps, inventory, new_tests = art("gaps.json"), art("change-inventory.json"), art("new-tests.json")
    test_results, lib_api, dep_diff = art("test-results.json"), art("library-api-diff.json"), art("dependency-diff.json")
    changed_methods, asserts = art("changed-methods.json"), art("assert-strength.json")
    review = paths.read("review", "review-record.json")
    tests_location = get(new_tests, "location") or "artifacts\\agent\\tests"
    if not all((status, impact, flows, behavior, manual, review)):
        print(f"Run incomplete: {run_dir} (needs stages 5 to 9)")
        return 2

    log(f"phase 10 verdict for run {run_id}")
    incomplete, not_proven, notes = [], [], []
    findings = items(impact.get("findings"))
    checks = items(behavior.get("checks"))
    tests = items(behavior.get("tests"))
    decisions = items(get(review, "manual", "decisions"))

    # 1. the comparison is incomplete when a stage failed or a check could not run
    for st in items(status.get("steps")):
        if st.get("state") == "failed":
            incomplete.append(f"stage {st.get('name')} failed: {st.get('reason')}")
    incomplete += [f"check did not run: {e}" for e in items(behavior.get("blockingErrors"))]

    # 2. differences: every check that gave another result on change_branch, each counted once
    diffs = differences(behavior, test_results)
    found = [d["text"] for d in diffs]

    # 3. findings: what the checks showed for each. A person may record that a difference is expected
    #    (review\manual-decisions.json); the report notes it, but the verdict does not wait for it.
    accepted = {s(d.get("ref")) for d in decisions if d.get("decision") == "accept" and d.get("kind") == "finding"}
    accepted_rows = [{"findingId": x, "decision": "accept"} for x in accepted]
    status_rows = [{"id": f["id"], "status": finding_status(f, checks, accepted_rows)} for f in findings]
    rule_of = {f["id"]: f.get("ruleId") for f in findings}

    # 4. not proven either way: what no check covered, with the reason
    for row in status_rows:
        if row["status"] in ("open", "hypothesis"):
            not_proven.append(f"finding {row['id']} [{rule_of.get(row['id'])}] not checked by any probe or test")
    checked_flows = flows_checked_by_new_tests(tests)
    for fl in items(flows.get("flows")):
        if as_list(fl.get("untraced")) and fl.get("id") not in checked_flows:
            not_proven.append(f"flow {fl.get('id')} has code no test executes: {', '.join(as_list(fl.get('untraced')))}")
    for m in items(get(changed_methods, "methods")):
        if not m.get("tested") and m.get("kind") != "file":
            lines = as_list(get(m, "lines", "uncovered"))
            where = f" (changed lines no test runs: {', '.join(str(x) for x in lines)})" if lines else ""
            not_proven.append(f"changed {m.get('kind')} {m.get('name')} in {m.get('file')} is not covered by any test on "
                              f"change_branch{where}")
    for w in items(get(asserts, "methods")):
        if w.get("strength") == "weak":
            survived = [f"line {x.get('line')} ({x.get('operator')})" for x in items(w.get("mutants")) if x.get("result") == "survived"]
            not_proven.append(f"existing tests run {w.get('name')} in {w.get('file')} but do not detect a change of "
                              f"{', '.join(survived)}: their assertions are too weak to prove it")
    for t in tests:
        if not t.get("validated") and get(t, "base", "passed") and get(t, "head", "passed"):
            not_proven.append(f"new test {t.get('id')} passes on both branches but misses an injected regression, so it proves nothing")
        if get(t, "base", "ran") and not get(t, "base", "passed"):
            not_proven.append(f"new test {t.get('id')} fails on master, so it was not written against master's behavior")
    not_proven += [f"not run: {g.get('item')}. {g.get('reason')}" for g in items(get(gaps, "gaps"))]
    for r in items(get(review, "agent", "tests")):
        if r.get("result") not in ("reviewed", None):
            notes.append(f"new test {r.get('testId')}: independent review {r.get('result')}")

    analyst = agent_proposal(run_dir, "analyst", "hypotheses.json")
    if analyst["state"] != "used":
        not_proven.append(f"analyst proposal {analyst['state']}: no hypotheses and no explained change reasons for this run's inputs")
    tests_proposal = agent_proposal(run_dir, "tests", "tests-manifest.json")
    if tests_proposal["state"] != "used":
        untraced = [fl.get("id") for fl in items(flows.get("flows")) if as_list(fl.get("untraced"))]
        if untraced:
            not_proven.append(f"no current tests proposal ({tests_proposal['state']}) for untraced flow(s): {', '.join(untraced)}")

    # Library changes are checked against the release notes; a change no source mentions is not proven.
    notes_doc = agent_proposal(run_dir, "release-notes", "release-notes.json")
    need_notes = []
    if get(lib_api, "available"):
        need_notes += [i.get("id") for i in items(lib_api.get("items")) if i.get("severity") in ("high", "medium")]
    if dep_diff:
        need_notes += [c.get("ga") for c in items(dep_diff.get("changed")) if c.get("direct") and c.get("major")]
    if need_notes:
        if notes_doc["state"] != "used":
            more = ", ..." if len(need_notes) > 8 else ""
            not_proven.append(f"release-notes check {notes_doc['state']} for {len(need_notes)} library change(s): "
                              f"{', '.join(s(x) for x in need_notes[:8])}{more}")
        else:
            covered = {s(n.get("ref")) for n in items(get(notes_doc, "doc", "items"))}
            missing = [x for x in need_notes if s(x) not in covered]
            if missing:
                not_proven.append(f"release notes not checked for: {', '.join(s(x) for x in missing)}")
            for n in items(get(notes_doc, "doc", "items")):
                if n.get("result") == "contradicted":
                    not_proven.append(f"release notes contradict the expected change for {n.get('ref')} ({n.get('artifact')}): "
                                      f"{n.get('claim')}")

    # Changed files no stage analysed.
    for f in items(get(inventory, "files")):
        if f.get("category") == "other" and re.search(r"\.(java|xml|ya?ml|properties|json|toml|gradle|kts)$", s(f.get("path")), re.I):
            not_proven.append(f"changed file {f.get('path')} is not analysed by any stage (category other)")

    # Existing tests that tell nothing about the upgrade.
    if test_results:
        compared = items(test_results.get("tests"))
        not_proven += [f"test fails on both branches (not caused by the upgrade, but it checks nothing): {t.get('test')}"
                       for t in compared if t.get("change") == "same" and t.get("head") in ("failed", "error")]
        not_proven += [f"test removed on change_branch: {t.get('test')}" for t in compared if t.get("change") == "removed"]
        not_proven += [f"test skipped on change_branch but run on master: {t.get('test')}"
                       for t in compared if t.get("change") == "now-skipped"]

    placed = items(get(new_tests, "placed"))
    if placed:
        notes.append(f"new tests are placed, uncommitted, in the change worktree {new_tests.get('location')} (branch "
                     f"{new_tests.get('branch')}) for a developer to commit ({len(placed)} file(s); patch: {new_tests.get('patch')})")
    if accepted:
        notes.append(f"recorded as expected by a person: {', '.join(ps_sorted(accepted))}")

    # required stages: stage 2 to 9 must be done (behavior done, not done-with-errors)
    required_ran = True
    for name in REQUIRED:
        steps = [x for x in items(status.get("steps")) if x.get("name") == name]
        if not steps or steps[-1].get("state") != "done":
            required_ran = False
            incomplete.append(f"required stage {name} did not complete cleanly")

    result = verdict(incomplete, found, not_proven, required_ran)
    state_name = "verdict-" + result["verdict"]

    write_json(verdict_file, {
        "schemaVersion": 2, "runId": run_id, "verdict": result["verdict"], "exitCode": result["exitCode"],
        "differences": diffs, "incomplete": incomplete, "notProven": not_proven, "notes": notes,
        "findingStatus": status_rows, "decidedAt": now_iso(),
    })
    log(f"VERDICT: {result['verdict']} (exit {result['exitCode']}). Differences: {len(found)}. "
        f"Incomplete: {len(incomplete)}. Not proven: {len(not_proven)}.")
    for x in incomplete:
        log(f"  incomplete: {x}")
    for x in found:
        log(f"  different: {x}")
    for x in not_proven:
        log(f"  not proven: {x}")

    set_stage_step(status, "verdict", "done", result["verdict"], verdict_file)
    set_run_state(status, state_name, "phase10")
    write_json(status_file, status)

    # Rebuild the report with the verdict in it.
    run_script("phase8-output.py", ["--run-id", run_id, "--report-only", "--config", config]
               + (["--standalone"] if a.standalone else []))
    log(f"report updated with the verdict: {paths.out('reports', 'report.md')}")
    return result["exitCode"]


if __name__ == "__main__":
    run_main(main)

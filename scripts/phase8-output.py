"""Stage 8 of UpgradeLens (output): places the validated new tests in the change_branch worktree of the run
(uncommitted, for review), writes findings.json (the single source of truth), and the report
(report.md and report.html, both generated from the same model).

Steps, in order:
  1. tests       validated tests (passed on master, passed unchanged on change_branch, and failed for each
                 injected regression) are copied from artifacts\\agent\\tests into the change worktree,
                 runs\\<id>\\worktrees\\<head branch>\\src\\test\\... That worktree is on the local branch
                 <head branch> of the run's own clone, tracking origin. The files are NOT staged or committed:
                 a developer reviews them there, commits and pushes (the report gives the commands).
                 artifacts\\new-tests.patch holds the same files as a git patch ("git apply" adds them to
                 any checkout of change_branch), and artifacts\\new-tests.json records each file, its hash,
                 and why it was added.
  2. collect     every stage's output, into reports\\findings.json
  3. report      reports\\report.md and reports\\report.html. One report covers: run and configuration,
                 workspace, both builds and the test comparison, changed files (three-dot), changed
                 classes and methods with their reason and coverage, dependencies (added, removed,
                 required or not, conflicts, suggestions), library API changes with the code they touch
                 and the release-notes result, impact findings, flows, new tests and why each was added,
                 manual decisions, agent usage, gaps, and evidence.

--report-only rebuilds steps 2 and 3 only. It also works on an incomplete run (a stage failed): each
section then says which stage did not produce its input. run-pipeline.py and run-stage.py use this after a
failure, and stage 10 uses it after the verdict.

Exit codes:
  0  written
  2  usage or run problem (no run; without --report-only, stage 7 not done)
  3  a test file could not be placed

Examples:
  python scripts/phase8-output.py --run-id 20261126-131717
  python scripts/phase8-output.py --run-id 20261126-131717 --report-only
"""

import argparse
import html
import os
import re
import shutil
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.analysis import new_added_file_patch  # noqa: E402
from lib.summary import build_summary, markdown_to_html  # noqa: E402
from lib.agents import agent_proposal  # noqa: E402
from lib.core import (TOOL_VERSION, as_list, file_sha256, get, icontains, items, like, log, native_path,  # noqa: E402
                      read_json_safe, read_lines, resolve_config_path, resolve_run_id, run_main, runs_root, s, set_run_state,
                      set_stage_log, set_stage_step, short, unique, write_json, write_text)

LATER_STATES = ["phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe"]


def cell(v) -> str:
    if isinstance(v, (list, tuple)):
        v = " ".join(s(x) for x in v)
    return re.sub(r"\r?\n", " ", s(v)).strip()


def table(headers, rows) -> dict:
    return {"headers": list(headers), "rows": [list(r) for r in rows]}


def not_run(what: str) -> str:
    return f"Not available: {what} did not complete in this run."


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 8: new tests placed uncommitted, findings.json and the report.")
    p.add_argument("--run-id")
    p.add_argument("--report-only", action="store_true", help="rebuild the report from the run's files; no test placement")
    p.add_argument("--config")
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))
    run_id = resolve_run_id(runs, a.run_id, LATER_STATES, any_state=a.report_only)
    if not run_id:
        print(f"No run with stage 7 done under {runs}. Run phase7-manual.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    artifacts = os.path.join(run_dir, "artifacts")
    reports_dir = os.path.join(run_dir, "reports")
    status_file = os.path.join(run_dir, "status.json")
    if not os.path.isfile(status_file):
        print(f"Run not found: {run_dir}")
        return 2
    set_stage_log(os.path.join(run_dir, "logs", "phase8.log"))

    art = lambda name: read_json_safe(os.path.join(artifacts, name))  # noqa: E731
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    status = read_json_safe(status_file)
    env_rec, builds, test_results = art("environment.json"), art("build-results.json"), art("test-results.json")
    inventory, dep_diff, api_diff, lib_api = art("change-inventory.json"), art("dependency-diff.json"), art("api-diff.json"), art("library-api-diff.json")
    impact, changed_methods, flows = art("impact-findings.json"), art("changed-methods.json"), art("flows.json")
    behavior, manual, gaps = art("behavior-results.json"), art("manual-items.json"), art("gaps.json")
    usage = art(os.path.join("agent", "usage.json"))
    review = read_json_safe(os.path.join(run_dir, "review", "review-record.json"))
    verdict = read_json_safe(os.path.join(run_dir, "verdict.json"))

    if not a.report_only and not all((cfg, env_rec, impact, flows, behavior, manual)):
        print(f"Run incomplete: {run_dir} (needs stages 4 to 7). Use --report-only for a partial report.")
        return 2
    os.makedirs(reports_dir, exist_ok=True)
    log(f"phase 8 output for run {run_id}" + (" (report only)" if a.report_only else ""))

    base_name = cfg.get("baseBranch") if cfg else "base"
    head_name = cfg.get("headBranch") if cfg else "head"

    # ---------- 1. place the validated tests (uncommitted) ----------

    new_tests_path = os.path.join(artifacts, "new-tests.json")
    patch_path = os.path.join(artifacts, "new-tests.patch")
    new_tests = read_json_safe(new_tests_path)
    if not a.report_only:
        head_wt = get(env_rec, "branches", "head", "worktree")
        placed, skipped, patch_text = [], [], []
        try:
            for t in items(behavior.get("tests")):
                if not t.get("validated"):
                    continue
                rel = s(t.get("relativePath"))
                src = os.path.join(artifacts, "agent", "tests", native_path(rel))
                if not os.path.isfile(src):
                    raise RuntimeError(f"validated test file missing: {src}")
                dst = os.path.join(head_wt, native_path(rel))
                src_hash = file_sha256(src)
                if os.path.isfile(dst):
                    if file_sha256(dst) != src_hash:
                        skipped.append({"testId": t.get("id"), "relativePath": rel,
                                        "reason": f"a different file with this path already exists on {head_name}; rename the test class"})
                        log(f"tests: {rel} not placed: a different file exists at that path on {head_name}")
                        continue
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
                patch_text.append(new_added_file_patch(rel, read_lines(src)))
                placed.append({"testId": t.get("id"), "className": t.get("className"), "relativePath": rel, "sha256": src_hash,
                               "purpose": t.get("purpose"), "gap": t.get("gap"), "linkedRules": as_list(t.get("linkedRules")),
                               "flowIds": as_list(t.get("flowIds"))})
        except (OSError, RuntimeError) as e:
            log(f"test placement failed: {e}")
            set_stage_step(status, "output", "failed", f"test placement failed: {e}")
            set_run_state(status, "failed-tool", "phase8")
            write_json(status_file, status)
            return 3
        write_text(patch_path, "".join(patch_text))
        new_tests = {
            "schemaVersion": 1, "runId": run_id, "committed": False, "location": head_wt, "branch": head_name,
            "baseCommit": get(env_rec, "branches", "head", "commit"), "patch": "artifacts\\new-tests.patch",
            "placed": placed, "skipped": skipped,
            "commitSteps": [
                f'cd "{head_wt}"',
                "git status   # the new test files show as untracked",
                "git add " + " ".join(x["relativePath"] for x in placed),
                f'git commit -m "Add characterization tests from UpgradeLens run {run_id}"',
                f"git push     # the branch tracks origin/{head_name}",
            ],
            "note": "Placed for review only: the automation does not stage, commit or push. A developer reviews the files in the "
                    "change worktree and commits them (commitSteps), or applies artifacts\\new-tests.patch to another checkout.",
        }
        write_json(new_tests_path, new_tests)
        log(f"tests: {len(placed)} validated test file(s) placed, uncommitted, in {head_wt}; {len(skipped)} skipped; patch {patch_path}")

    # ---------- 2. collect ----------

    status_by_id = {fs.get("id"): fs.get("status") for fs in items(get(flows, "findingStatus"))}
    status_by_id.update({fs.get("id"): fs.get("status") for fs in items(get(verdict, "findingStatus"))})
    decisions = items(get(review, "manual", "decisions"))
    checks = items(get(behavior, "checks"))

    finding_rows = []
    for f in items(get(impact, "findings")):
        linked = [c for c in checks if c.get("ruleId") == f.get("ruleId")]
        evidence_text = "no check" if not linked else "; ".join(unique(f"{c.get('source')} {c.get('id')}: {c.get('outcome')}" for c in linked))
        finding_rows.append({"id": f.get("id"), "ruleId": f.get("ruleId"), "title": f.get("title"), "severity": f.get("severity"),
                             "scope": f.get("scope"), "status": status_by_id.get(f.get("id")), "file": f.get("file"),
                             "line": f.get("line"), "evidence": f.get("evidence"), "checkResults": evidence_text,
                             "impact": f.get("impact"), "check": f.get("check"), "affectedFlows": as_list(f.get("affectedFlows")),
                             "source": f.get("source")})

    analyst = agent_proposal(run_dir, "analyst", "hypotheses.json")
    notes = agent_proposal(run_dir, "release-notes", "release-notes.json")
    notes_by_ref = {s(n.get("ref")): n for n in items(get(notes, "doc", "items"))} if notes["state"] == "used" else {}
    agent_states = {"analyst": analyst["state"], "testWriter": get(behavior, "testState") or "not-run",
                    "reviewer": get(review, "agent", "state") or "not-run", "releaseNotes": notes["state"]}

    stage_rows = [{"name": x.get("name"), "state": x.get("state"), "reason": x.get("reason")} for x in items(get(status, "steps"))]
    failed_stages = [x for x in stage_rows if x["state"] == "failed"]
    count_status = lambda st: len([f for f in finding_rows if f["status"] == st])  # noqa: E731

    doc = {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "state": get(status, "state"),
        "complete": not failed_stages and manual is not None,
        "base": {"branch": base_name, "commit": get(env_rec, "branches", "base", "commit"), "mergeBase": get(env_rec, "branches", "mergeBase")},
        "head": {"branch": head_name, "commit": get(env_rec, "branches", "head", "commit")},
        "rulesSha256": get(impact, "rulesSha256"),
        "verdict": {"verdict": verdict.get("verdict"), "exitCode": verdict.get("exitCode"), "blocking": as_list(verdict.get("blocking")),
                    "listed": as_list(verdict.get("listed"))} if verdict else None,
        "stages": stage_rows,
        "summary": {
            "findings": len(finding_rows),
            "byStatus": {st: count_status(st) for st in ("open", "hypothesis", "verified", "confirmed", "accepted")},
            "flows": get(flows, "summary"), "probes": get(behavior, "probeSummary"),
            "tests": {"proposed": len(items(get(behavior, "tests"))), "validated": len([t for t in items(get(behavior, "tests")) if t.get("validated")]),
                      "placed": len(items(get(new_tests, "placed")))},
            "manualItems": get(manual, "summary", "items"), "gaps": len(items(get(gaps, "gaps"))),
        },
        "builds": builds,
        "testComparison": test_results,
        "changes": {"files": get(inventory, "summary"), "diff": get(inventory, "diff"), "dependencies": get(dep_diff, "summary"),
                    "api": get(api_diff, "summary"), "library": get(lib_api, "summary") if get(lib_api, "available") else None},
        "changedMethods": items(get(changed_methods, "methods")),
        "dependencies": {"assessment": items(dep_diff.get("assessment")), "conflicts": items(dep_diff.get("conflicts")),
                         "suggestions": items(dep_diff.get("suggestions")),
                         "analyze": {"base": get(dep_diff, "base", "analyze"), "head": get(dep_diff, "head", "analyze")}} if dep_diff else None,
        "library": {"available": lib_api.get("available"), "reason": lib_api.get("reason"),
                    "items": [{"id": i.get("id"), "class": i.get("class"), "state": i.get("state"), "severity": i.get("severity"),
                               "summary": i.get("summary"), "artifact": i.get("artifact"), "movedTo": as_list(i.get("movedTo")),
                               "releaseNotes": notes_by_ref.get(i.get("id"))} for i in items(lib_api.get("items"))]} if lib_api else None,
        "findings": finding_rows,
        "flows": [{"id": x.get("id"), "kind": x.get("kind"), "scope": x.get("scope"), "reason": x.get("reason"),
                   "probes": as_list(x.get("probes")), "tests": as_list(x.get("tests")), "untraced": as_list(x.get("untraced"))}
                  for x in items(get(flows, "flows"))],
        "probes": [{"id": x.get("id"), "flowId": x.get("flowId"), "request": x.get("request"), "outcome": x.get("outcome"),
                    "differences": as_list(x.get("differences"))} for x in items(get(behavior, "probes"))],
        "servers": get(behavior, "servers"),
        "dumps": get(behavior, "dumps"),
        "tests": items(get(behavior, "tests")),
        "newTests": new_tests,
        "manualItems": items(get(manual, "items")),
        "decisions": decisions,
        "gaps": items(get(gaps, "gaps")),
        "agents": {"states": agent_states, "usage": get(usage, "agents") or []},
        "evidence": {
            "environment": "artifacts\\environment.json", "builds": "artifacts\\build-results.json",
            "testResults": "artifacts\\test-results.json", "changeInventory": "artifacts\\change-inventory.json",
            "changedMethods": "artifacts\\changed-methods.json", "dependencyDiff": "artifacts\\dependency-diff.json",
            "apiDiff": "artifacts\\api-diff.json", "libraryApiDiff": "artifacts\\library-api-diff.json",
            "impactFindings": "artifacts\\impact-findings.json", "reach": "artifacts\\reach.json",
            "behaviorResults": "artifacts\\behavior-results.json", "flows": "artifacts\\flows.json",
            "manualItems": "artifacts\\manual-items.json", "gaps": "artifacts\\gaps.json", "newTests": "artifacts\\new-tests.json",
            "reviewRecord": "review\\review-record.json", "logs": "logs\\",
        },
    }
    write_json(os.path.join(reports_dir, "findings.json"), doc)

    # ---------- 3. report (markdown and HTML, from the same model) ----------

    sections = []

    def add_section(title, text=(), tbl=None, tbl2=None, text2=()):
        sections.append({"title": title, "text": [x for x in text if x is not None], "table": tbl,
                         "text2": [x for x in text2 if x is not None], "table2": tbl2})

    # 1. Verdict
    if verdict:
        verdict_line = f"**{verdict.get('verdict')}** (exit {verdict.get('exitCode')})"
    elif failed_stages:
        verdict_line = "**not reached**: the run stopped at a failed stage"
    else:
        verdict_line = "pending: stage 10 has not run"
    verdict_text = [f"Verdict: {verdict_line}"] + [f"- Failed stage: {x['name']}: {x['reason']}" for x in failed_stages]
    if verdict:
        verdict_text += [f"- Blocking: {x}" for x in items(verdict.get("blocking"))]
        verdict_text += [f"- Listed: {x}" for x in items(verdict.get("listed"))]
    add_section("1. Verdict", verdict_text)

    # 2. Run, configuration and workspace
    run_rows = []
    if env_rec:
        eb = lambda side, *k: get(env_rec, "branches", side, *k)  # noqa: E731
        runtime = f"Python {env_rec['python']}" if env_rec.get("python") else f"PowerShell {s(env_rec.get('powershell'))}"
        run_rows = [
            ["Config", f"{env_rec.get('configPath')} (sha256 {short(env_rec.get('configSha256'))})"],
            ["Repository", env_rec.get("repo")],
            ["Base branch", f"{base_name} at {short(eb('base', 'commit'))}"],
            ["Head branch", f"{head_name} at {short(eb('head', 'commit'))}"],
            ["Merge base", f"{short(get(env_rec, 'branches', 'mergeBase'))} (diff: {base_name}...{head_name}, three-dot)"],
            ["Base worktree", f"{eb('base', 'worktree')} (JDK {eb('base', 'jdk')})"],
            ["Head worktree", f"{eb('head', 'worktree')} (JDK {eb('head', 'jdk')})"],
            ["Maven settings", get(env_rec, "maven", "settingsFile") or "(Maven default)"],
            ["Maven goals", get(env_rec, "maven", "goals")],
            ["Rules", f"sha256 {short(get(impact, 'rulesSha256'))}"],
            ["Tools", f"{s(env_rec.get('git'))}; {s(eb('head', 'maven', 'maven'))}; {runtime}"],
        ]
    add_section("2. Run, configuration and workspace",
                ["Each branch is checked out in its own detached worktree inside the run folder; the repository's own checkout is not switched."],
                table(["Item", "Value"], run_rows))

    # 3. Builds and tests
    build_rows = []
    if builds:
        for side in ("base", "head"):
            b = get(builds, "sides", side)
            if not b:
                continue
            result = "SUCCESS" if b.get("exitCode") == 0 else f"FAILED (exit {b.get('exitCode')})"
            build_rows.append([b.get("branch"), b.get("jdk"), result,
                               f"{get(b, 'tests', 'passed')} passed, {get(b, 'tests', 'failed')} failed, {get(b, 'tests', 'skipped')} skipped",
                               f"{get(b, 'integrationTests', 'mode')} ({len(as_list(get(b, 'integrationTests', 'classes')))} class(es))", b.get("log")])
    cmp_rows = [[t.get("test"), t.get("base") or "-", t.get("head") or "-", t.get("change"), t.get("message")]
                for t in items(get(test_results, "tests")) if t.get("change") != "same"]
    if builds:
        settings_text = f", and the settings file {builds['settingsFile']}" if builds.get("settingsFile") else ""
        build_text = [f"Both branches were built in parallel with `mvn {builds.get('goals')}`, each with its own JDK, worktree and target folder{settings_text}."]
        if test_results:
            sm = test_results.get("summary") or {}
            build_text.append(f"Test comparison: {sm.get('tests')} test(s); {sm.get('same')} same, {sm.get('nowFailing')} now failing, "
                              f"{sm.get('removed')} removed, {sm.get('new')} new, {sm.get('nowSkipped')} now skipped.")
    else:
        build_text = [not_run("stage 1 (build)")]
    add_section("3. Builds and tests", build_text, table(["Branch", "JDK", "Result", "Tests", "Integration tests", "Log"], build_rows),
                table(["Test", "Master", "Change branch", "Change", "Message"], cmp_rows), ["Tests whose result differs between the branches:"])

    # 4. Changed files
    file_rows = []
    if inventory:
        for f in items(inventory.get("files")):
            methods = ", ".join(as_list(f.get("changedMethods"))) or "-"
            commits = "; ".join(f"{c.get('commit')} {c.get('subject')}" for c in items(f.get("commits"))) or "-"
            file_rows.append([f.get("path"), f.get("change"), f.get("category"), f"+{f.get('additions')} / -{f.get('deletions')}", methods, commits])
        by_cat = ", ".join(f"{k} {v}" for k, v in (get(inventory, "summary", "byCategory") or {}).items())
        file_text = [f"Diff: `{get(inventory, 'diff', 'expression')}` (three-dot: merge base {short(get(inventory, 'diff', 'left'))} to "
                     f"{short(get(inventory, 'diff', 'right'))}). {get(inventory, 'summary', 'files')} file(s), "
                     f"{get(inventory, 'summary', 'commits')} commit(s). By category: {by_cat}."]
        file_text += [f"Finding: {x.get('type')} in `{x.get('file')}`: {x.get('detail')}" for x in items(inventory.get("findings"))]
    else:
        file_text = [not_run("stage 2 (changed files)")]
    add_section("4. Changed files", file_text, table(["File", "Change", "Category", "Lines", "Changed methods", "Commits"], file_rows))

    # 5. Changed classes and methods
    method_rows = []
    if changed_methods:
        for m in items(changed_methods.get("methods")):
            if m.get("kind") == "file":
                continue
            if m.get("analystReason"):
                cons = f" Consequence: {m['consequence']}" if m.get("consequence") else ""
                why = f"{m['analystReason']} [analyst]{cons} | {m.get('reason')}"
            else:
                why = m.get("reason")
            cb, ch = get(m, "coverage", "base"), get(m, "coverage", "head")
            method_rows.append([m.get("class"), f"{m.get('kind')} {m.get('name')}", ", ".join(as_list(m.get("changeKinds"))), why,
                                f"master {cb if cb is not None else '-'} / change_branch {ch if ch is not None else '-'}",
                                "yes" if m.get("tested") else "NO", ", ".join(as_list(m.get("flows")))])
        method_text = [f"Each diff hunk is mapped to the method or type it changes. Coverage is JaCoCo covered instructions from the unit and "
                       f"integration tests of each build ({get(changed_methods, 'coverage', 'integrationTests')} for integration tests). "
                       f"{get(changed_methods, 'summary', 'untested')} changed symbol(s) are not executed by any test on change_branch."]
    else:
        method_text = [not_run("stage 4 (impact)")]
    add_section("5. Changed classes and methods", method_text,
                table(["Class", "Symbol", "What changed", "Why", "Coverage (instructions)", "Tested", "Flows"], method_rows))

    # 6. Dependencies
    assess_rows, conflict_rows, sugg_rows = [], [], []
    if dep_diff:
        sm = dep_diff.get("summary") or {}
        dep_text = [
            f"Resolved artifacts: {get(dep_diff, 'base', 'artifacts')} on {base_name}, {get(dep_diff, 'head', 'artifacts')} on {head_name}. "
            f"Added {sm.get('added')} ({sm.get('directAdded')} direct), removed {sm.get('removed')} ({sm.get('directRemoved')} direct), "
            f"version changed {sm.get('changed')} ({sm.get('majorChanged')} major, {sm.get('directChanged')} direct).",
            f"Version conflicts on {head_name}: {sm.get('conflicts')} ({sm.get('majorConflicts')} major)"
            f"{'' if dep_diff.get('verboseTree') else ' - NOT CHECKED: no verbose tree'}. Duplicates omitted by Maven: "
            f"{get(dep_diff, 'duplicates', 'head')}.",
        ]
        base_und = as_list(get(dep_diff, "base", "analyze", "undeclared"))
        head_und = as_list(get(dep_diff, "head", "analyze", "undeclared"))
        new_und = [x for x in head_und if not icontains(base_und, x)]
        dep_text.append(f"dependency:analyze on {head_name}: {len(as_list(get(dep_diff, 'head', 'analyze', 'unused')))} declared but unused, "
                        f"{len(head_und)} used but undeclared ({len(new_und)} new: {', '.join(new_und[:6])}).")
        assess_rows = [[x.get("change"), x.get("ga"), x.get("version"), x.get("assessment"), x.get("reason"), x.get("impact")]
                       for x in items(dep_diff.get("assessment"))]
        conflict_rows = [[c.get("ga"), c.get("requested"), c.get("resolved"), "major" if c.get("major") else "minor",
                          ", ".join(as_list(c.get("requiredBy"))), "yes" if c.get("newOnHead") else "no"] for c in items(dep_diff.get("conflicts"))]
        sugg_rows = [[x.get("command"), x.get("why")] for x in items(dep_diff.get("suggestions"))]
    else:
        dep_text = [not_run("stage 3 (dependencies)")]
    add_section("6. Dependencies", dep_text, table(["Change", "Artifact", "Version", "Assessment", "Why", "Potential impact"], assess_rows),
                table(["Artifact", "Requested", "Resolved", "Difference", "Requested by", "New on change_branch"], conflict_rows),
                ["Version conflicts (mvn dependency:tree -Dverbose):"])
    add_section("6a. Suggested deeper analysis", [], table(["Command", "Why"], sugg_rows))

    # 7. Library API changes
    lib_rows, lib_detail = [], []
    if get(lib_api, "available"):
        ls = lib_api.get("summary") or {}
        lib_text = [f"Library classes the code refers to: {ls.get('referencedClasses')}. Same: {ls.get('same')}, changed: {ls.get('changed')}, "
                    f"removed: {ls.get('removed')}, moved: {ls.get('moved')}, added: {ls.get('added')}. Severity: high {ls.get('high')}, "
                    f"medium {ls.get('medium')}, low {ls.get('low')}, info {ls.get('info')}."]
        notes_count = f" ({len(items(get(notes, 'doc', 'items')))} item(s))" if notes["state"] == "used" else ""
        lib_text.append(f"Release-notes validation: {notes['state']}{notes_count}.")
        for it in items(lib_api.get("items")):
            if not ((it.get("severity") != "info") or it.get("state") != "added"):
                continue
            users = [u for u in as_list(get(it, "usage", "base")) + as_list(get(it, "usage", "head")) if u and u.get("source")]
            where = "; ".join(unique(f"{u['source']}" + (f":{u['line']}" if u.get("line") else "") + (f" {u['method']}" if u.get("method") else "")
                                     for u in users)[:4])
            rn = notes_by_ref.get(it.get("id"))
            moved = as_list(it.get("movedTo"))
            lib_rows.append([it.get("id"), it.get("severity"), it.get("class"), f"moved to {', '.join(moved)}" if moved else it.get("state"),
                             f"{get(it, 'artifact', 'base')} -> {get(it, 'artifact', 'head')}", it.get("summary"), where,
                             f"{rn.get('result')}: {rn.get('claim')} ({s(get(rn, 'source', 'url'))})" if rn else "not validated"])
        for it in items(lib_api.get("items")):
            if it.get("severity") == "high":
                lib_detail += [[it.get("id"), u.get("declaration"), f"{u.get('source')}:{s(u.get('line'))}", u.get("method")]
                               for u in items(it.get("removedUsed"))]
    else:
        lib_text = [f"Not available: {lib_api.get('reason') if lib_api else 'stage 3 did not complete'}."]
    add_section("7. Library API changes", lib_text,
                table(["ID", "Severity", "Library class", "Change", "Artifact (master -> change_branch)", "Summary", "Used at", "Release notes"], lib_rows),
                table(["ID", "Removed member used on master", "Where (master)", "Method"], lib_detail),
                ["Removed or changed members that master's code called:"])

    # 8. Impact findings
    add_section("8. Impact findings",
                ["Status: verified (a check matched master), confirmed (a check differs), open (no check ran), hypothesis (from the analyst, "
                 "not yet checked), accepted (a person accepted it)."],
                table(["ID", "Severity", "Change", "Scope", "What could happen", "Evidence", "Status", "Action"],
                      [[f["id"], f["severity"], f"{f['title']} ({s(f['file'])}" + (f":{f['line']}" if f["line"] else "") + ")", f["scope"],
                        f["impact"], f"{f['checkResults']} | {s(f['evidence'])}", f["status"], f["check"]] for f in finding_rows]))

    # 8a. Impact in plain words (analyst: one explanation per rule behind a high or medium finding, and each
    # hypothesis), with the status the checks gave it.
    plain_rows = []
    if analyst["state"] == "used":
        status_by_rule = {}
        for fr in finding_rows:
            status_by_rule.setdefault(s(fr["ruleId"]), []).append(f"{fr['id']} {s(fr['status'])}")
        for x in items(analyst["doc"].get("explanations")):
            plain_rows.append([s(x.get("ruleId")), s(x.get("plain")), s(x.get("example")), s(x.get("productionRisk")),
                               ", ".join(status_by_rule.get(s(x.get("ruleId")), [])) or "-"])
        for h in items(analyst["doc"].get("hypotheses")):
            plain_rows.append([f"{h.get('id')} (hypothesis)", s(h.get("plain")), s(h.get("example")), s(h.get("productionRisk")),
                               ", ".join(status_by_rule.get(s(h.get("id")), [])) or "-"])
        plain_text = ["Each potential impact in simple words, with an example and how it could break production. Status comes from the "
                      "probes and tests of this run."]
    else:
        plain_text = [f"Not available: the analyst proposal is {analyst['state']} for this run's inputs."]
    add_section("8a. Impact explained", plain_text,
                table(["Rule or hypothesis", "In simple words", "Example", "How it can break production", "Findings and status"], plain_rows))

    # 9. Flows
    add_section("9. Flows to test", ["Time saved is not estimated: there is no regression duration data for this application."],
                table(["Flow", "Scope", "Why", "Probes", "Tests"],
                      [[x["id"], x["scope"], x["reason"], ", ".join(s(y) for y in x["probes"]), ", ".join(s(y) for y in x["tests"])]
                       for x in doc["flows"]]))

    # 10. New tests
    test_rows = []
    for t in doc["tests"]:
        why = " ".join(x for x in (s(t.get("purpose")), f"Gap: {t['gap']}" if t.get("gap") else "") if x)
        covers = "rules: " + ", ".join(as_list(t.get("linkedRules"))) + "; flows: " + ", ".join(as_list(t.get("flowIds")))
        result = f"master {'pass' if get(t, 'base', 'passed') else 'FAIL'}, change_branch {'pass' if get(t, 'head', 'passed') else 'FAIL'}"
        inj = "; ".join(f"{'detected' if i.get('detected') else ('MISSED' if i.get('applied') else 'not applied')}: {i.get('note')}"
                        for i in items(t.get("injections")))
        test_rows.append([t.get("id"), t.get("className"), why, covers, result, inj, "validated" if t.get("validated") else "not validated"])
    if new_tests and items(new_tests.get("placed")):
        new_test_text = [f"Each test below was written in the run folder, run on both branches, and validated. It is now copied, uncommitted, into "
                         f"the change worktree `{new_tests.get('location')}` (branch `{new_tests.get('branch')}`, tracking origin). The "
                         "automation does not stage, commit or push.",
                         "To keep the tests, a developer reviews them there, then commits and pushes:"]
        new_test_text += [f"- `{x}`" for x in items(new_tests.get("commitSteps"))]
        new_test_text.append(f"Or apply `{new_tests.get('patch')}` to another checkout of {new_tests.get('branch')} with `git apply`.")
        new_test_text += [f"Not placed: {x.get('relativePath')}: {x.get('reason')}" for x in items(new_tests.get("skipped"))]
    elif behavior and behavior.get("testState") != "not-proposed":
        new_test_text = ["No test is validated, so none is placed."]
    else:
        new_test_text = [f"No tests were proposed in this run (test writer: {agent_states['testWriter']})."]
    add_section("10. New tests", new_test_text,
                table(["ID", "Class", "Why it was added", "Covers", "Result", "Injected regressions", "Status"], test_rows))

    # 11. Manual decisions
    manual_rows = []
    for item in doc["manualItems"]:
        hit = [d for d in decisions if d.get("itemId") == item.get("id")]
        dec_text = "; ".join(f"{d.get('decision')} by {d.get('reviewer')}" for d in hit) if hit else "pending"
        manual_rows.append([item.get("id"), item.get("kind"), item.get("question"), item.get("expectedMaster"), item.get("owner"), dec_text])
    add_section("11. Manual decisions", ["Expected master results come from the master probes and the master build where one exists."],
                table(["ID", "Kind", "Question", "Expected on master", "Owner", "Decision"], manual_rows))

    # 12. Agents
    calls = f"{usage.get('totalCalls')} of {usage.get('budget')}" if usage else "0"
    add_section("12. Agent usage", [f"Agent output counts only when its input hash matches the current inputs. Calls used: {calls}."],
                table(["Agent", "State"], [["analyst (hypotheses, change reasons)", agent_states["analyst"]],
                                           ["test writer", agent_states["testWriter"]], ["test reviewer", agent_states["reviewer"]],
                                           ["release notes", agent_states["releaseNotes"]]]))

    # 13. Gaps
    add_section("13. Not run and limits", ["A gap listed here means the verdict cannot be a clean pass."],
                table(["Stage", "Item", "Reason"], [[g.get("stage"), g.get("item"), g.get("reason")] for g in doc["gaps"]]))

    # 14. Evidence
    add_section("14. Evidence", [
        f"Run folder: `{run_dir}`. Machine-readable results: `reports\\findings.json`.",
        "Stages: " + ", ".join(f"{x['name']} {x['state']}" for x in stage_rows) + ".",
        "Artifacts: " + ", ".join(f"`{v}`" for v in doc["evidence"].values()) + ".",
    ])

    # Markdown
    def md_table(md, tbl):
        md.append("| " + " | ".join(tbl["headers"]) + " |")
        md.append("|" + "|".join("---" for _ in tbl["headers"]) + "|")
        for row in tbl["rows"]:
            md.append("| " + " | ".join(cell(c).replace("|", "\\|") for c in row) + " |")
        if not tbl["rows"]:
            md.append("| None. |" + " |" * (len(tbl["headers"]) - 1))
        md.append("")

    md = [f"# Upgrade impact check: run {run_id}", "", f"`{base_name}` -> `{head_name}`. Generated by UpgradeLens {TOOL_VERSION}.", ""]
    for sec in sections:
        md += [f"## {sec['title']}", ""]
        text = sec["text"]
        for i, line in enumerate(text):
            md.append(line)
            next_is_item = i + 1 < len(text) and like(text[i + 1], "- *")
            if not (like(line, "- *") and next_is_item):
                md.append("")
        if sec["table"]:
            md_table(md, sec["table"])
        for line in sec["text2"]:
            md += [line, ""]
        if sec["table2"]:
            md_table(md, sec["table2"])
    write_text(os.path.join(reports_dir, "report.md"), "\r\n".join(md) + "\r\n")

    # HTML (one file, inline styles, no external resources)
    esc = lambda v: html.escape(s(v), quote=True)  # noqa: E731

    def html_table(h, tbl):
        h.append("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{esc(x)}</th>" for x in tbl["headers"]) + "</tr></thead><tbody>")
        for row in tbl["rows"]:
            h.append("<tr>" + "".join(f"<td>{esc(cell(c))}</td>" for c in row) + "</tr>")
        if not tbl["rows"]:
            h.append(f"<tr><td colspan='{len(tbl['headers'])}'>None.</td></tr>")
        h.append("</tbody></table></div>")

    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         f"<title>Upgrade impact check {esc(run_id)}</title>",
         "<style>:root{--bg:#FFFFFF;--fg:#2C2C2A;--line:#D3D1C7;--head:#F1EFE8}@media (prefers-color-scheme: dark){:root{--bg:#1F1E1B;"
         "--fg:#EDEBE4;--line:#5F5E5A;--head:#2C2C2A}}body{font-family:Arial,Helvetica,sans-serif;margin:24px;color:var(--fg);"
         "background:var(--bg);line-height:1.45}h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid var(--line);"
         "padding-bottom:4px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 16px}"
         "th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--head)}"
         "code{font-size:12px;background:var(--head);padding:1px 4px;border-radius:3px}@media (max-width:700px){body{margin:16px}}</style>"
         "</head><body>",
         f"<h1>Upgrade impact check: run {esc(run_id)}</h1>",
         f"<p>{esc(base_name)} &rarr; {esc(head_name)}. Generated by UpgradeLens {TOOL_VERSION}.</p>"]
    for sec in sections:
        h.append(f"<h2>{esc(sec['title'])}</h2>")
        # Lines in order; consecutive "- " lines become one list.
        in_list = False
        for line in sec["text"]:
            clean = esc(line.replace("`", "").replace("**", ""))
            if like(line, "- *"):
                if not in_list:
                    h.append("<ul>")
                    in_list = True
                h.append(f"<li>{clean[2:]}</li>")
            else:
                if in_list:
                    h.append("</ul>")
                    in_list = False
                h.append(f"<p>{clean}</p>")
        if in_list:
            h.append("</ul>")
        if sec["table"]:
            html_table(h, sec["table"])
        h += [f"<p>{esc(line.replace('`', ''))}</p>" for line in sec["text2"]]
        if sec["table2"]:
            html_table(h, sec["table2"])
    h.append("</body></html>")
    write_text(os.path.join(reports_dir, "report.html"), "\n".join(h) + "\n")

    # The one-page summary (lib\summary.py): the answer, changes, coverage, new tests, differences, run trace.
    summary_md = build_summary(run_dir, run_id)
    write_text(os.path.join(reports_dir, "summary.md"), "\r\n".join(summary_md) + "\r\n")
    write_text(os.path.join(reports_dir, "summary.html"), markdown_to_html(summary_md, f"Upgrade check summary {run_id}"))
    log(f"summary: {os.path.join(reports_dir, 'summary.md')}")
    log(f"report: {os.path.join(reports_dir, 'report.md')}")
    log(f"report: {os.path.join(reports_dir, 'report.html')}")
    log(f"findings: {os.path.join(reports_dir, 'findings.json')}")

    if not a.report_only:
        set_stage_step(status, "output", "done", "", os.path.join(reports_dir, "report.md"))
        set_run_state(status, "phase8-done", "phase8")
        write_json(status_file, status)
    log(f"phase 8 done for run {run_id}")
    return 0


if __name__ == "__main__":
    run_main(main)

"""Stage 4 of UpgradeLens (impact evaluation): applies the rule set to the changes, maps every flow
(HTTP endpoint, scheduled job, listener) to the code it reaches, marks reached code that no test
executes as untraced, and lists every changed method with its reason and its coverage on both sides.

Inputs (from earlier stages, in the run folder): change-inventory.json, dependency-diff.json,
api-diff.json, library-api-diff.json, build-results.json, the three-dot git diff (merge base to head),
the source of both sides, and the coverage reports of both builds (target\\site\\jacoco\\jacoco.xml, which
includes the integration tests when stage 1 ran them). Optional: the analyst's proposal, used only when
its input hash matches the current inputs.

Steps, in order:
  1. rules           copy the rules folder (config rulesDir) into the run (config\\rules\\) and hash them
  2. evidence        the patch, the head source and the merge-base source of each changed file
  3. findings        apply every rule; one finding per diff or inventory match, dependency, analysis and
                     library findings grouped per rule; each finding gets a scope (global, symbol, test, none)
  4. hypotheses      merge the analyst's hypotheses if they match the inputs (status "hypothesis")
  5. flows           endpoints, jobs and listeners of the head source; the code each one reaches
  6. coverage        JaCoCo covered instructions per reached symbol on head; untraced = not covered
  7. affect          which flows each finding touches (global: all; symbol: flows that reach it)
  8. changed methods each changed method or type of the main code: what changed (signature, annotation,
                     body...), the commits that changed it, the rules that matched it, its coverage on
                     master and on change_branch, and the reason for the change (derived from the commits
                     and the rules; the analyst's explanation when its proposal is current)
  9. write           artifacts\\impact-findings.json, reach.json and changed-methods.json (no timestamps, so
                     the same inputs give the same bytes)

Exit codes:
  0  findings and reach written
  2  usage or run problem (no run, stage 3 not done)
  3  a required input is missing (for example the head coverage report)

Example:
  python .github/upgradelens/scripts/phase4-impact.py --run-id 20261126-131717
"""

import argparse
import os
import shutil
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.analysis import finding_affects_flow, flow_coverage, read_unified_patch, rule_findings  # noqa: E402
from lib.agents import agent_proposal  # noqa: E402
from lib.core import (ROOT_DIR, TOOL_VERSION, as_list, config_value, get, icontains, input_hash, items,  # noqa: E402
                      changed_line_coverage, like, log, parse_jacoco_lines, parse_jacoco_xml, ps_sorted, read_json_safe,
                      read_lines, reach_set, resolve_config_path,
                      resolve_project_path, run_main, run_native, runs_root, s, set_run_state, set_stage_log,
                      set_stage_step, sort_key, source_flows, source_index, unique_sorted, write_json)
from lib.standalone import StagePaths, add_standalone_arg, select_run  # noqa: E402

from lib.core import VERDICT_STATES  # noqa: E402

LATER_STATES = ["phase3-done", "phase4-done", "phase5-done", "phase6-done", "phase7-done", "phase8-done", "phase9-done",
                ] + VERDICT_STATES


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 4: impact rules, flows, reach and coverage.")
    p.add_argument("--run-id")
    p.add_argument("--config")
    add_standalone_arg(p)
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    live_cfg = read_json_safe(config)
    runs = runs_root(live_cfg)
    run_id = select_run(runs, a.run_id, a.standalone, LATER_STATES)
    if not run_id:
        print(f"No run with stage 3 done under {runs}. Run phase3-deps.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    paths = StagePaths(run_dir, a.standalone)
    artifacts = paths.out("artifacts")
    status_file = paths.out("status.json")
    set_stage_log(paths.out("logs", "phase4.log"))
    art = lambda name: paths.read("artifacts", name)  # noqa: E731
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    env_rec, status = art("environment.json"), paths.read("status.json")
    inventory, dep_diff, api_diff = art("change-inventory.json"), art("dependency-diff.json"), art("api-diff.json")
    lib_api, builds = art("library-api-diff.json"), art("build-results.json")
    if not all((cfg, env_rec, status, inventory, dep_diff, api_diff)):
        print(f"Run incomplete: {run_dir} (needs stages 2 and 3)")
        return 2

    def stop_stage(code: int, reason: str) -> int:
        log(reason)
        set_stage_step(status, "impact", "failed", reason)
        set_run_state(status, "failed-tool", "phase4")
        write_json(status_file, status)
        return code

    log(f"phase 4 impact evaluation for run {run_id}")
    base_wt = get(env_rec, "branches", "base", "worktree")
    head_wt = get(env_rec, "branches", "head", "worktree")
    head_commit = get(env_rec, "branches", "head", "commit")
    merge_base = get(env_rec, "branches", "mergeBase") or get(inventory, "base", "mergeBase")
    if not merge_base:
        return stop_stage(2, "No merge base in the environment record or the change inventory. Run stages 0 and 2 again.")

    # ---------- 1. rules ----------

    rules_src = resolve_project_path(s(config_value(live_cfg, "rulesDir", "config\\rules")), ROOT_DIR)
    rules_dst = paths.out("config", "rules")
    os.makedirs(rules_dst, exist_ok=True)
    src_names = [n for n in ps_sorted(os.listdir(rules_src)) if n.lower().endswith(".json")] if os.path.isdir(rules_src) else []
    if not src_names:
        return stop_stage(2, f"No rule files in {rules_src}")
    for n in os.listdir(rules_dst):
        if n.lower().endswith(".json") and n not in src_names:
            os.remove(os.path.join(rules_dst, n))
    for n in src_names:
        shutil.copyfile(os.path.join(rules_src, n), os.path.join(rules_dst, n))
    run_rule_files = [os.path.join(rules_dst, n) for n in ps_sorted(os.listdir(rules_dst)) if n.lower().endswith(".json")]
    rules_hash = input_hash(run_rule_files)
    rules = [r for f in run_rule_files for r in items(get(read_json_safe(f), "rules"))]
    log(f"rules: {len(rules)} from {len(run_rule_files)} file(s) in {rules_src}, sha256 {rules_hash[:12]}")

    # ---------- 2. evidence ----------

    git = s(get(cfg, "git", "command", default="git"))
    repo = env_rec.get("repo") or cfg.get("repo")
    diff = run_native(git, ["-C", repo, "-c", "core.quotepath=off", "diff", "-U0", "-M", merge_base, head_commit])
    if diff.exit_code != 0:
        return stop_stage(3, f"git diff failed: {diff.text()}")
    patch = read_unified_patch(diff.output)

    # Removed lines are numbered against the merge base, so their symbols come from the merge-base source.
    head_lines, base_lines = {}, {}
    old_path_of = {f["path"]: (f.get("oldPath") or f["path"]) for f in items(inventory.get("files"))}
    for path in patch:
        if path.lower().endswith(".java"):
            hp = os.path.join(head_wt, *path.split("/"))
            head_lines[path] = read_lines(hp) if os.path.isfile(hp) else []
            show = run_native(git, ["-C", repo, "show", f"{merge_base}:{old_path_of.get(path, path)}"])
            base_lines[path] = show.output if show.exit_code == 0 else []

    it_files = []
    test_root = os.path.join(head_wt, "src", "test")
    for d, _dirs, files in os.walk(test_root):
        for f in files:
            if f.lower().endswith(".java") and (f.lower().endswith("it.java") or f.lower().endswith("itcase.java")):
                it_files.append(os.path.relpath(os.path.join(d, f), head_wt).replace("\\", "/"))
    it_files = ps_sorted(it_files)
    it_mode = s(get(builds, "sides", "head", "integrationTests", "mode")) or "not-run"

    ctx = {
        "patch": patch, "head_lines": head_lines, "base_lines": base_lines,
        "inventory": items(inventory.get("files")), "inventory_findings": items(inventory.get("findings")),
        "dep_diff": dep_diff, "dep_analyze": {"base": get(dep_diff, "base", "analyze"), "head": get(dep_diff, "head", "analyze")},
        "api_diff": api_diff, "library_api": lib_api if lib_api and lib_api.get("available") else {"items": []},
        "it_files": it_files, "it_mode": it_mode,
    }
    log(f"evidence: {len(patch)} changed path(s) in the patch (merge base {merge_base[:10]}), {len(it_files)} IT test file(s) "
        f"on head, integration tests on head: {it_mode}")

    # ---------- 3. findings ----------

    found = [f for rule in rules for f in rule_findings(rule, ctx)]

    # ---------- 4. hypotheses (analyst output, counted only when its inputs match) ----------

    analyst = agent_proposal(run_dir, "analyst", "hypotheses.json")
    hyp_count = 0
    if analyst["state"] == "used":
        for h in items(analyst["doc"].get("hypotheses")):
            hyp_count += 1
            found.append({"ruleId": h.get("id"), "title": h.get("claim"), "severity": h.get("severity") or "medium",
                          "scope": h.get("scope") or "global", "file": h.get("file") or "", "line": None,
                          "symbols": as_list(h.get("symbols")), "evidence": h.get("claim"),
                          "impact": s(h.get("productionRisk")) or s(h.get("claim")), "check": h.get("probeIdea"), "source": "analyst"})
    elif analyst["state"] == "stale":
        log("analyst output is stale (its inputs changed). Not used. Run the analyst again.")
    log(f"hypotheses: {analyst['state']} ({hyp_count} used)")

    # Stable order and ids: sort by rule, file, line, then assign F-001...
    found.sort(key=lambda f: (sort_key(f.get("ruleId")), sort_key(f.get("file")), sort_key(f.get("line")), sort_key(f.get("evidence"))))
    findings = []
    for n, f in enumerate(found, start=1):
        findings.append({"id": f"F-{n:03d}", "ruleId": f.get("ruleId"), "title": f.get("title"), "severity": f.get("severity"),
                         "scope": f.get("scope"), "file": f.get("file"), "line": f.get("line"), "symbols": as_list(f.get("symbols")),
                         "evidence": f.get("evidence"), "impact": f.get("impact"), "check": f.get("check"), "source": f.get("source"),
                         "status": "hypothesis" if f.get("source") == "analyst" else "open"})

    # ---------- 5 to 7. flows, coverage, affected flows ----------

    head_jacoco_path = os.path.join(head_wt, "target", "site", "jacoco", "jacoco.xml")
    if not os.path.isfile(head_jacoco_path):
        return stop_stage(3, f"No head coverage report at {head_jacoco_path}. Add the jacoco-maven-plugin (prepare-agent and "
                             "report) to the build, then run stage 1 again.")
    head_jacoco = parse_jacoco_xml(head_jacoco_path)
    head_line_cov = parse_jacoco_lines(head_jacoco_path)
    base_jacoco_path = os.path.join(base_wt, "target", "site", "jacoco", "jacoco.xml")
    base_jacoco = parse_jacoco_xml(base_jacoco_path)

    index = source_index(os.path.join(head_wt, "src", "main", "java"))
    fqcn_by_simple = {k: v["fqcn"] for k, v in index.items()}

    flows = []
    for simple in ps_sorted(index):
        for fl in source_flows(index[simple]["text"], simple):
            reach = reach_set(index, simple, fl["method"])
            coverage = flow_coverage(reach, head_jacoco, fqcn_by_simple)
            base_cov = {c["symbol"]: c["covered"] for c in flow_coverage(reach, base_jacoco, fqcn_by_simple)}
            flows.append({**fl, "reach": reach,
                          "coverage": [{"symbol": c["symbol"], "headCovered": c["covered"], "baseCovered": base_cov.get(c["symbol"]),
                                        "traced": c["traced"]} for c in coverage],
                          "untraced": [c["symbol"] for c in coverage if not c["traced"]]})
    flow_rows = ps_sorted(flows, key=lambda f: f["id"])

    for f in findings:
        f["affectedFlows"] = [fl["id"] for fl in flow_rows if finding_affects_flow(f, fl)]

    # ---------- 8. changed methods: what, why, and whether a test executes them ----------

    reasons_by_key = {}
    if analyst["state"] == "used":
        reasons_by_key = {f"{r.get('file')}|{r.get('symbol')}": r for r in items(analyst["doc"].get("changeReasons"))}
    changed_rows = []
    for file in items(inventory.get("files")):
        if file.get("category") != "main-code" or not s(file.get("path")).lower().endswith(".java"):
            continue
        simple = os.path.splitext(s(file["path"]).split("/")[-1])[0]
        fq = fqcn_by_simple.get(simple, simple)
        ja_key = fq.replace(".", "/")
        for sc in items(file.get("symbols")):
            symbol = s(sc.get("symbol"))
            is_method = like(symbol, "method:*")
            name = symbol[7:] if is_method else (symbol[5:] if like(symbol, "type:*") else "")
            owner = simple if is_method else name
            # JaCoCo names constructors "<init>".
            jacoco_method = "<init>" if is_method and name == simple else name
            cov_key = f"{ja_key}|{jacoco_method}" if is_method else f"{ja_key}|*"
            head_cov = int(head_jacoco[cov_key]) if cov_key in head_jacoco else None
            base_cov = int(base_jacoco[cov_key]) if cov_key in base_jacoco else None
            # Changed-line coverage: a test must run every changed executable line of change_branch. When the change
            # has no executable head line (a removed line, a signature or annotation only), the method must run.
            pkg = ja_key.rsplit("/", 1)[0] if "/" in ja_key else ""
            src_key = f"{pkg}/{s(file['path']).split('/')[-1]}" if pkg else s(file["path"]).split("/")[-1]
            lines = changed_line_coverage(sc.get("headLines"), head_line_cov.get(src_key) if head_line_cov else None)
            if lines["executable"]:
                tested = not lines["uncovered"]
            else:
                tested = head_cov is not None and head_cov > 0
            sym_key = f"{simple}#{name}" if is_method else f"{owner}#*"
            matched = unique_sorted([f["ruleId"] for f in findings if f["file"] == file["path"] and
                                     (not f["symbols"] or icontains(f["symbols"], sym_key) or icontains(f["symbols"], f"{simple}#*"))])
            commit_text = [f"{c.get('commit')} \"{c.get('subject')}\"" for c in items(file.get("commits"))]
            kinds = as_list(sc.get("kinds"))
            what = ", ".join(kinds) if kinds else "body"
            derived = f"Changed: {what} (+{sc.get('added')}/-{sc.get('removed')} lines)."
            if commit_text:
                derived += f" Commits: {'; '.join(commit_text)}."
            if matched:
                derived += f" Matches rules: {', '.join(matched)}."
            an = reasons_by_key.get(f"{file['path']}|{symbol}")
            flows_reaching = [fl["id"] for fl in flow_rows if icontains(fl["reach"], sym_key) or
                              (not is_method and "#" in sym_key and any(like(r, f"{owner}#*") for r in fl["reach"]))]
            changed_rows.append({
                "file": file["path"], "class": fq, "symbol": symbol,
                "kind": "method" if is_method else ("type" if name else "file"), "name": name, "changeKinds": kinds,
                "added": sc.get("added"), "removed": sc.get("removed"), "commits": items(file.get("commits")), "rules": matched,
                "coverage": {"base": base_cov, "head": head_cov}, "lines": lines, "tested": tested,
                "flows": flows_reaching, "reason": derived,
                "analystReason": s(an.get("reason")) if an else "", "consequence": s(an.get("consequence")) if an else "",
            })
    untested = [c for c in changed_rows if not c["tested"] and c["kind"] != "file"]
    write_json(os.path.join(artifacts, "changed-methods.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id,
        "coverage": {"base": base_jacoco_path, "head": head_jacoco_path, "integrationTests": it_mode},
        "analyst": analyst["state"],
        "summary": {"symbols": len(changed_rows), "tested": len([c for c in changed_rows if c["tested"]]), "untested": len(untested)},
        "methods": changed_rows,
    })
    for c in untested:
        log(f"  changed but not executed by any test: {c['file']} {c['symbol']}")

    # ---------- 9. write ----------

    by_sev = {sv: len([f for f in findings if f["severity"] == sv]) for sv in ("high", "medium", "low", "info")}
    write_json(os.path.join(artifacts, "impact-findings.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "rulesSha256": rules_hash,
        "rules": [r.get("id") for r in rules], "analyst": analyst["state"],
        "summary": {"findings": len(findings), "bySeverity": by_sev,
                    "byScope": {sc: len([f for f in findings if f["scope"] == sc]) for sc in ("global", "symbol", "test", "none")}},
        "findings": findings,
    })
    untraced_flows = len([fl for fl in flow_rows if fl["untraced"]])
    write_json(os.path.join(artifacts, "reach.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "coverageSource": head_jacoco_path,
        "summary": {"flows": len(flow_rows), "flowsWithUntracedCode": untraced_flows}, "flows": flow_rows,
    })
    log(f"findings: {len(findings)} (high {by_sev['high']}, medium {by_sev['medium']}, low {by_sev['low']}, info {by_sev['info']}); "
        f"flows: {len(flow_rows)}, with untraced code: {untraced_flows}; changed symbols: {len(changed_rows)}, "
        f"not executed by a test: {len(untested)}")
    for fl in flow_rows:
        if fl["untraced"]:
            log(f"  untraced in {fl['id']}: {', '.join(fl['untraced'])}")

    set_stage_step(status, "impact", "done", "", os.path.join(artifacts, "impact-findings.json"))
    set_run_state(status, "phase4-done", "phase4")
    write_json(status_file, status)
    log(f"phase 4 done: impact findings, reach and changed methods written for run {run_id}")
    return 0


if __name__ == "__main__":
    run_main(main)

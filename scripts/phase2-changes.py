"""Stage 2 of UpgradeLens (changed files): inventories what change_branch changed relative to the base
branch, as a three-dot diff (base...head).

Uses only git, on the run's own clone. The left side of every diff is the merge base of the two branches
(recorded by stage 0), so commits made on the base branch after the branch point are not reported as
changes. Steps, in order:
  1. select-run    the given run, or the newest run whose stage 1 finished (passed or failed: the inventory
                   is useful context for a failed build too)
  2. diff          name-status (renames detected), whitespace-insensitive numstat, and the full patch with
                   no context lines, all between <merge base> and <head commit>
  3. commits       the commits on change_branch since the merge base, and which files each one touched
  4. classify      each file: main-code, test, build-dependency, config, other, or formatting-only
  5. test-check    per test file: weakened assertions, removed tests, added disabled tests
  6. code-check    per Java file: each diff hunk line is mapped to the method or type it belongs to
                   (lib\\core.py symbol_for_line: any access level, generic and multi-line declarations,
                   brace-aware), with signature, annotation and field-default changes, and removed
                   if/else/switch/case/catch lines
  7. write         artifacts\\change-inventory.json and reports\\change-inventory.md

Findings are recorded, not failed. Stage 2 passes when the inventory is written. The verdict stage
decides whether a finding blocks a release.

Exit codes:
  0  inventory written
  2  usage or run problem (no run, stage 1 not run, missing config snapshot or environment record)
  3  git failure

Example:
  python scripts/phase2-changes.py --run-id 20261126-123622
"""

import argparse
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import (TOOL_VERSION, get, like, log, method_declaration_name, read_json_safe, resolve_config_path,  # noqa: E402
                      resolve_run_id, run_main, run_native, runs_root, s, set_stage_log, set_stage_step, symbol_for_line,
                      type_declaration_name, write_json, write_text)

ANN_RX = re.compile(r"^\s*@\w+", re.I)
FIELD_DEFAULT_RX = re.compile(r"^\s*(?:(?:private|protected|public|static|final|transient|volatile)\s+)+[^(=]*=", re.I)
BRANCH_RX = re.compile(r"(\bif\s*\(|\belse\b|\bswitch\s*\(|\bcase\b|\bcatch\s*\()", re.I)
ASSERT_RX = re.compile(r"\bassert\w*\(|\bverify\(|\bandExpect\(|\bfail\(|\bexpect\w*\(", re.I)
TEST_ANN_RX = re.compile(r"^\s*@(Test|ParameterizedTest|RepeatedTest|TestFactory)\b", re.I)
DISABLED_RX = re.compile(r"^\s*@(Disabled|Ignore)\b", re.I)
BUILD_LEAVES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradle.properties",
    "libs.versions.toml", "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat", "package.json", "package-lock.json",
    "requirements.txt", "pyproject.toml", "pipfile", "pipfile.lock",
}


def read_patch(lines) -> dict:
    """Parses "git diff -U0" output into per-file entries: path, oldPath, added and removed lines, each with its
    line number on its own side."""
    files = {}
    entry = None
    pending_old = ""
    in_hunk = False
    old_ln = new_ln = 0
    for raw in lines:
        line = s(raw).rstrip("\r")
        if line.startswith("diff --git "):
            entry, in_hunk, pending_old = None, False, ""
            continue
        if in_hunk and entry is not None:
            if line.startswith("+"):
                entry["added"].append({"line": new_ln, "text": line[1:]})
                new_ln += 1
                continue
            if line.startswith("-"):
                entry["removed"].append({"line": old_ln, "text": line[1:]})
                old_ln += 1
                continue
            if line.startswith("\\"):
                continue
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            old_ln, new_ln, in_hunk = int(m.group(1)), int(m.group(2)), True
            continue
        m = re.match(r"^--- (a/(.+)|/dev/null)$", line)
        if m:
            pending_old = m.group(2) or ""
            continue
        m = re.match(r"^\+\+\+ (b/(.+)|/dev/null)$", line)
        if m:
            key = m.group(2) or pending_old
            entry = {"path": key, "oldPath": pending_old, "added": [], "removed": []}
            files[key] = entry
            in_hunk = False
            continue
    return files


def category(path: str) -> str:
    p = path.replace("\\", "/")
    leaf = p.split("/")[-1]
    if re.search(r"(^|/)src/test/", p, re.I) or re.search(r"(Test|IT|Tests|ITCase)\.java$", leaf, re.I):
        return "test"
    if leaf.lower() in BUILD_LEAVES or re.search(r"(^|/)\.mvn/", p, re.I) or re.search(r"(^|/)gradle/", p, re.I):
        return "build-dependency"
    if re.search(r"(^|/)src/main/resources/", p, re.I) or re.search(r"\.(ya?ml|properties|xml|json|toml|env)$", leaf, re.I):
        return "config"
    if re.search(r"(^|/)src/main/", p, re.I) and re.search(r"\.(java|kt|groovy|scala)$", leaf, re.I):
        return "main-code"
    return "other"


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 2: changed files, three-dot.")
    p.add_argument("--run-id")
    p.add_argument("--config")
    a = p.parse_args(argv)

    # ---------- step 1: select run ----------

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))
    ready = ["phase1-done", "failed-build", "phase2-done"]
    run_id = resolve_run_id(runs, a.run_id, ready)
    if not run_id:
        print(f"No run with stage 1 finished under {runs}. Run phase1-build.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    status_file = os.path.join(run_dir, "status.json")
    env_file = os.path.join(run_dir, "artifacts", "environment.json")
    artifacts = os.path.join(run_dir, "artifacts")
    reports = os.path.join(run_dir, "reports")
    run_cfg_file = os.path.join(run_dir, "config", "upgrade-lens.json")
    if not all(os.path.isfile(x) for x in (status_file, run_cfg_file, env_file)):
        print(f"Run not found or incomplete: {run_dir} (needs status.json, config\\upgrade-lens.json and artifacts\\environment.json)")
        return 2
    set_stage_log(os.path.join(run_dir, "logs", "phase2.log"))
    cfg = read_json_safe(run_cfg_file)
    status = read_json_safe(status_file)
    env_rec = read_json_safe(env_file)

    build_failed = status.get("state") == "failed-build"
    if status.get("state") not in ready:
        print(f"Run {run_id} is not ready: state is '{status.get('state')}', stage 1 must have run first.")
        return 2

    git = s(get(cfg, "git", "command", default="git"))
    # The run's own clone (stage 0). Runs from before the clone-per-run change recorded the configured repo.
    repo = env_rec.get("repo") or cfg.get("repo")

    def git_repo(args):
        return run_native(git, ["-C", repo, "-c", "core.quotepath=off"] + list(args))

    base_commit = get(env_rec, "branches", "base", "commit")
    head_commit = get(env_rec, "branches", "head", "commit")
    merge_base = get(env_rec, "branches", "mergeBase")
    if not base_commit or not head_commit:
        print(f"Environment record has no base or head commit: {env_file}")
        return 2
    if not merge_base:
        # A run created before the merge base was recorded: compute it now.
        mb = git_repo(["merge-base", base_commit, head_commit])
        if mb.exit_code != 0:
            print(f"No merge base between {base_commit} and {head_commit}: {mb.text()}")
            return 3
        merge_base = mb.first().strip()

    base_name, head_name = cfg.get("baseBranch"), cfg.get("headBranch")
    log(f"phase 2 changed files for run {run_id} ({base_name}...{head_name}: merge base {merge_base[:10]} -> {head_commit[:10]})")

    def stop_stage(code: int, reason: str) -> int:
        log(reason)
        set_stage_step(status, "changed-files", "failed", reason)
        status["phase"] = "phase2"
        status["state"] = "failed-tool"
        write_json(status_file, status)
        return code

    content_cache = {}

    def file_lines(commit: str, path: str):
        """Content of one file at one commit, as lines. Cached, because several changes can share a file."""
        if not path:
            return None
        key = f"{commit}:{path}"
        if key not in content_cache:
            r = git_repo(["show", key])
            content_cache[key] = r.output if r.exit_code == 0 else None
        return content_cache[key]

    # ---------- step 2: diff (three-dot: merge base to head) ----------

    ns = git_repo(["diff", "--name-status", "-M", merge_base, head_commit])
    if ns.exit_code != 0:
        return stop_stage(3, f"git diff --name-status failed: {ns.text()}")
    patch = git_repo(["diff", "-U0", "-M", merge_base, head_commit])
    if patch.exit_code != 0:
        return stop_stage(3, f"git diff failed: {patch.text()}")
    ws = git_repo(["diff", "--numstat", "-M", "-w", "--ignore-blank-lines", merge_base, head_commit])
    if ws.exit_code != 0:
        return stop_stage(3, f"git diff --numstat failed: {ws.text()}")

    changes = []
    for line in ns.output:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        change = parts[0][:1]
        if change in ("R", "C") and len(parts) > 2:
            old_path, new_path = parts[1], parts[2]
        else:
            old_path = new_path = parts[1]
        changes.append({"change": change, "oldPath": old_path, "path": new_path})
    log(f"diff: {len(changes)} changed file(s)")

    # Paths that still have a change when whitespace and blank lines are ignored.
    non_ws = set()
    for line in ws.output:
        parts = line.split("\t")
        if len(parts) != 3 or "=>" in parts[2]:
            continue
        if parts[0] != "0" or parts[1] != "0":
            non_ws.add(parts[2])

    patch_files = read_patch(patch.output)

    # ---------- step 3: commits since the merge base ----------

    commit_list, commits_by_file = [], {}
    glog = git_repo(["log", "--no-merges", "--format=%x1e%H%x09%an%x09%s", "--name-only", f"{merge_base}..{head_commit}"])
    if glog.exit_code != 0:
        return stop_stage(3, f"git log failed: {glog.text()}")
    cur = None
    for t in glog.output:
        if t.startswith("\x1e"):
            parts = t[1:].split("\t")
            cur = {"commit": parts[0], "short": parts[0][:10], "author": parts[1] if len(parts) > 1 else "",
                   "subject": "\t".join(parts[2:]), "files": []}
            commit_list.append(cur)
            continue
        if cur and t.strip():
            f = t.strip()
            cur["files"].append(f)
            commits_by_file.setdefault(f, []).append({"commit": cur["short"], "subject": cur["subject"]})
    log(f"commits on {head_name} since the merge base: {len(commit_list)}")

    # ---------- step 4 to 6: classify, check tests, check code ----------

    file_records, finding_list, method_keys = [], [], set()
    for it in changes:
        e = patch_files.get(it["path"]) or {"path": it["path"], "oldPath": it["oldPath"], "added": [], "removed": []}
        added, removed = e["added"], e["removed"]
        whitespace_only = it["change"] not in ("R", "A", "D") and it["path"] not in non_ws
        cat = "formatting-only" if whitespace_only else category(it["path"])

        methods, symbol_changes = [], {}
        sigs, anns, defs, branches, file_findings = [], [], [], [], []

        def add_symbol_change(symbol, kind, side):
            symbol = symbol or "file"
            sc = symbol_changes.setdefault(symbol, {"symbol": symbol, "added": 0, "removed": 0, "kinds": []})
            sc["added" if side == "+" else "removed"] += 1
            if kind and kind not in sc["kinds"]:
                sc["kinds"].append(kind)

        if it["path"].lower().endswith(".java"):
            head_lines = file_lines(head_commit, it["path"]) if added else None
            base_lines = file_lines(merge_base, it["oldPath"]) if removed else None
            pairs = [("+", x, head_lines) for x in added] + [("-", x, base_lines) for x in removed]
            for side, item, lines in pairs:
                text = item["text"]
                sym = symbol_for_line(lines, item["line"])
                kind = "body"
                if method_declaration_name(text) or type_declaration_name(text):
                    sigs.append(f"{side} {text.strip()}")
                    kind = "signature"
                elif ANN_RX.search(text):
                    anns.append(f"{side} {text.strip()}")
                    kind = "annotation"
                elif FIELD_DEFAULT_RX.search(text) and like(sym, "type:*"):
                    defs.append(f"{side} {text.strip()}")
                    kind = "field-default"
                elif re.search(r"^\s*import\s", text, re.I):
                    kind = "import"
                if side == "-" and BRANCH_RX.search(text):
                    branches.append(text.strip())
                    kind = "removed-branch"
                add_symbol_change(sym, kind, side)
                if like(sym, "method:*"):
                    m = sym[7:]
                    if m not in methods:
                        methods.append(m)
            if branches:
                file_findings.append({"type": "removed-branch-or-catch",
                                      "detail": f"{len(branches)} removed line(s), e.g. {' | '.join(branches[:3])}"})

        if cat == "test":
            a_rem = len([x for x in removed if ASSERT_RX.search(x["text"])])
            a_add = len([x for x in added if ASSERT_RX.search(x["text"])])
            if a_rem > a_add:
                file_findings.append({"type": "weakened-assertions", "detail": f"removed {a_rem} assertion line(s), added {a_add}"})
            t_rem = len([x for x in removed if TEST_ANN_RX.search(x["text"])])
            t_add = len([x for x in added if TEST_ANN_RX.search(x["text"])])
            if t_rem > t_add:
                file_findings.append({"type": "test-removed", "detail": f"removed {t_rem} test annotation(s), added {t_add}"})
            d_add = len([x for x in added if DISABLED_RX.search(x["text"])])
            d_rem = len([x for x in removed if DISABLED_RX.search(x["text"])])
            if d_add > d_rem:
                file_findings.append({"type": "test-disabled", "detail": f"added {d_add} @Disabled or @Ignore, removed {d_rem}"})

        for m in methods:
            method_keys.add(f"{it['path']}::{m}")
        for f in file_findings:
            finding_list.append({"file": it["path"], "type": f["type"], "detail": f["detail"]})

        file_commits = commits_by_file.get(it["path"]) or (commits_by_file.get(it["oldPath"]) if it["oldPath"] else None) or []
        file_records.append({
            "path": it["path"], "oldPath": it["oldPath"], "change": it["change"], "category": cat,
            "additions": len(added), "deletions": len(removed), "changedMethods": methods,
            "symbols": list(symbol_changes.values()), "signatureChanges": sigs, "annotationChanges": anns,
            "defaultChanges": defs, "removedBranches": branches, "commits": file_commits,
        })
        log(f"  {it['change']} {it['path']} [{cat}] methods: {', '.join(methods)}")

    # ---------- step 7: write ----------

    by_category = {c: len([r for r in file_records if r["category"] == c])
                   for c in ("main-code", "test", "build-dependency", "config", "other", "formatting-only")}
    inventory = {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id,
        "diff": {"mode": "three-dot", "expression": f"{base_name}...{head_name}", "left": merge_base, "right": head_commit},
        "base": {"name": base_name, "commit": base_commit, "mergeBase": merge_base},
        "head": {"name": head_name, "commit": head_commit},
        "buildFailed": build_failed,
        "summary": {"files": len(file_records), "byCategory": by_category, "changedMethods": len(method_keys),
                    "findings": len(finding_list), "commits": len(commit_list)},
        "commits": [{"commit": c["commit"], "author": c["author"], "subject": c["subject"], "files": c["files"]} for c in commit_list],
        "files": file_records,
        "findings": finding_list,
    }
    os.makedirs(artifacts, exist_ok=True)
    os.makedirs(reports, exist_ok=True)
    json_path = os.path.join(artifacts, "change-inventory.json")
    write_json(json_path, inventory)

    md = [f"# Change inventory: run {run_id}", "",
          f"- Diff: `{base_name}...{head_name}` (three-dot), merge base `{merge_base[:10]}` to `{head_commit[:10]}`",
          f"- Files changed: {len(file_records)}. Changed methods: {len(method_keys)}. Commits: {len(commit_list)}. "
          f"Findings: {len(finding_list)}.", "", "## Files by category", ""]
    md += [f"- {c}: {n}" for c, n in by_category.items()]
    md += ["", "## Findings", ""]
    if not finding_list:
        md.append("None.")
    md += [f"- **{f['type']}** `{f['file']}`: {f['detail']}" for f in finding_list]
    md += ["", "## Files", "", "| File | Change | Category | +/- | Changed methods | Commits |", "|---|---|---|---|---|---|"]
    for r in file_records:
        method_text = ", ".join(r["changedMethods"]) if r["changedMethods"] else "-"
        commit_text = "; ".join(f"{c['commit']} {c['subject']}" for c in r["commits"]).replace("|", "\\|") if r["commits"] else "-"
        md.append(f"| `{r['path']}` | {r['change']} | {r['category']} | +{r['additions']} / -{r['deletions']} | {method_text} | {commit_text} |")
    md.append("")
    write_text(os.path.join(reports, "change-inventory.md"), "\r\n".join(md) + "\r\n")

    # ---------- done ----------

    set_stage_step(status, "changed-files", "done", "", json_path)
    status["phase"] = "phase2"
    # After a failed build the inventory is context for the failure report; the run stays failed.
    if not build_failed:
        status["state"] = "phase2-done"
    write_json(status_file, status)
    log(f"changed files: {len(file_records)}, changed methods: {len(method_keys)}, findings: {len(finding_list)}")
    log(f"inventory: {json_path}")
    log(f"phase 2 done: changed files inventoried for run {run_id}")
    return 0


if __name__ == "__main__":
    run_main(main)

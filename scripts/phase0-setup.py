"""Phase 0 of UpgradeLens: validates the config and prerequisites, then creates the run folder, clones the
repository into it, checks out each branch in its own worktree, and writes the environment record.

Steps, in order:
  1. validate-config   schema version, required keys, distinct branches, optional keys and their types
  2. prereq-check      git and Maven on PATH, the repository URL answers and has both branches
                       (git ls-remote), a JDK per side with java, javap and jdeps, Maven settings file (if
                       configured), rules folder and probe manifest
  3. run-folder        runs\\<id>\\ with config\\, artifacts\\, reports\\, logs\\, worktrees\\, and a snapshot of the
                       config, the rules and the probes used by this run
  4. clone             runs\\<id>\\repo.git: a bare repository with the two branches only (full history, no tags),
                       fetched from repoUrl. The run owns it; no other checkout is used or changed.
  5. worktrees         one worktree per branch, each in its own directory (so each build has its own target\\):
                         worktrees\\<base branch>  detached at the base commit (read only)
                         worktrees\\<head branch>  on the local branch <head branch>, tracking origin, so a
                                                  developer can review the new tests stage 8 places there,
                                                  commit them and push
  6. merge-base        the merge base of the two branches: every diff of the run is three-dot
                       (base...head), so commits made on the base branch after the branch point are not
                       reported as changes of the head branch
  7. environment       artifacts\\environment.json: tool, OS, git, Maven and JDK versions, commits, merge base,
                       config hash

Config and prerequisite failures stop the run with exit 2 and a clear message.
Tool failures (git, Maven) stop the run with exit 3.
Every step is written to runs\\<id>\\status.json as it runs.

Nothing outside the run folder is written. The automation never commits or pushes; only a developer does,
from the head worktree. scripts\\cleanup-run.py removes a run's clone and worktrees (it refuses while the
head worktree has uncommitted or unpushed work, unless --force).

Examples:
  python scripts/phase0-setup.py
  python scripts/phase0-setup.py --check-only
"""

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import (IS_WINDOWS, ROOT_DIR, TOOL_VERSION, append_text, config_value, file_sha256, jdk_env, jdk_tool,  # noqa: E402
                      now_iso, ps_sorted, resolve_config_path, resolve_project_path, run_main, run_native, runs_root, s,
                      which, write_json)


def non_empty(value) -> bool:
    return value is not None and s(value).strip() != ""


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 0: config, prerequisites, run folder, clone, worktrees.")
    p.add_argument("--config")
    p.add_argument("--check-only", action="store_true", help="validate the config and prerequisites only")
    a = p.parse_args(argv)
    config = resolve_config_path(a.config)

    checks = []

    def add_check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})

    def show_checks() -> None:
        if not checks:
            return
        w1 = max(len("Check"), *(len(c["check"]) for c in checks))
        print()
        print(f"{'Check':<{w1}}  Result  Detail")
        print(f"{'-----':<{w1}}  ------  ------")
        for c in checks:
            print(f"{c['check']:<{w1}}  {c['result']:<6}  {c['detail']}")
        print()

    # ---------- step 1: config ----------

    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        print("Create config\\upgrade-lens.json with the repo, branch, JDK and Maven settings paths.")
        return 2
    try:
        with open(config, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except ValueError as e:
        print(f"Config is not valid JSON: {config}")
        print(str(e))
        return 2

    def setting(path):
        return config_value(cfg, path)

    config_ok = True
    if cfg.get("schemaVersion") != 1:
        add_check("config schemaVersion", False, f"must be 1, found '{s(cfg.get('schemaVersion'))}'")
        config_ok = False
    else:
        add_check("config schemaVersion", True, "1")

    # repoUrl: any URL or path git can clone (githubUrl is the older name of the same key).
    repo_url = s(config_value(cfg, "repoUrl", config_value(cfg, "githubUrl", "")))
    ok = non_empty(repo_url)
    add_check("config repoUrl", ok, repo_url if ok else f"missing or empty in {config} (the repository URL to clone)")
    config_ok &= ok

    for key in ("baseBranch", "headBranch", "runsFolder", "git.command", "maven.command", "java.base", "java.head"):
        ok = non_empty(setting(key))
        add_check(f"config {key}", ok, "set" if ok else f"missing or empty in {config}")
        config_ok &= ok

    if non_empty(cfg.get("baseBranch")) and cfg.get("baseBranch") == cfg.get("headBranch"):
        add_check("config branches", False, "baseBranch and headBranch must differ")
        config_ok = False

    # Optional keys: checked only for their type and allowed values, so a typo does not silently fall back.
    it_mode = s(config_value(cfg, "build.integrationTests", "auto"))
    if it_mode not in ("auto", "failsafe-only"):
        add_check("config build.integrationTests", False, f"must be auto or failsafe-only; found '{it_mode}'")
        config_ok = False
    for t in ("timeouts.buildMinutes", "timeouts.startupSeconds", "agentBudget", "library.maxMembersPerClass",
              "orchestration.maxAgentAttempts", "orchestration.maxTests"):
        v = config_value(cfg, t)
        if v is not None and not (re.fullmatch(r"\d+", s(v)) and int(s(v)) > 0):
            add_check(f"config {t}", False, f"must be a positive whole number; found '{s(v)}'")
            config_ok = False
    rr = config_value(cfg, "orchestration.maxTestRepairRounds")
    if rr is not None and not (re.fullmatch(r"\d+", s(rr)) and int(s(rr)) <= 3):
        add_check("config orchestration.maxTestRepairRounds", False, f"must be 0 to 3; found '{s(rr)}'")
        config_ok = False
    goals = s(config_value(cfg, "maven.goals", "clean verify"))
    if not re.search(r"\bverify\b|\binstall\b|\bpackage\b", goals, re.I):
        add_check("config maven.goals", False, "must run the package phase or later (later stages need the jar and the "
                                               f"coverage report); found '{goals}'")
        config_ok = False

    if not config_ok:
        show_checks()
        print(f"The config is incomplete. Fix the FAIL lines in {config}, then run again.")
        return 2

    rules_dir = resolve_project_path(s(config_value(cfg, "rulesDir", "config\\rules")), ROOT_DIR)
    probes_file = resolve_project_path(s(config_value(cfg, "probesFile", "config\\probes.json")), ROOT_DIR)
    git = s(setting("git.command"))
    mvn = s(setting("maven.command"))

    # ---------- prerequisites (shared by --check-only and the full run) ----------

    def rule_files():
        if not os.path.isdir(rules_dir):
            return []
        return [n for n in ps_sorted(os.listdir(rules_dir)) if n.lower().endswith(".json") and os.path.isfile(os.path.join(rules_dir, n))]

    def check_prerequisites() -> None:
        git_path = which(git)
        add_check("git", git_path is not None, git_path or "not on PATH")
        mvn_path = which(mvn)
        add_check("maven", mvn_path is not None, mvn_path or "not on PATH")

        # The repository is cloned into each run. Here: the URL answers, and both branches exist on it.
        if git_path:
            ls = run_native(git, ["ls-remote", "--heads", repo_url])
            add_check("repo", ls.exit_code == 0, f"reachable: {repo_url}" if ls.exit_code == 0 else
                      f"git ls-remote failed (URL, network or credentials): {' '.join(ls.output[:2])}")
            if ls.exit_code == 0:
                for b in (cfg["baseBranch"], cfg["headBranch"]):
                    line = next((x for x in ls.output if re.search(r"\srefs/heads/" + re.escape(b) + "$", x)), None)
                    add_check(f"branch {b}", bool(line), line.split()[0] if line else f"no branch {b} on {repo_url}")

        for side in ("base", "head"):
            java_home = setting(f"java.{side}")
            for tool in ("java", "javap", "jdeps"):
                exe = jdk_tool(java_home, tool) if non_empty(java_home) else ""
                ok = non_empty(exe) and os.path.isfile(exe)
                add_check(f"{tool} {side}", ok, exe if ok else f"java.{side} must be a JDK folder with bin\\{tool}{'.exe' if IS_WINDOWS else ''}")

        settings_file = setting("maven.settingsFile")
        if non_empty(settings_file):
            ok = os.path.isfile(settings_file)
            add_check("maven settings", ok, settings_file if ok else f"not found: {settings_file}")

        n = len(rule_files())
        add_check("rules", n > 0, f"{n} file(s) in {rules_dir}" if n > 0 else f"no *.json rule file in {rules_dir}")
        ok = os.path.isfile(probes_file)
        add_check("probes", ok, probes_file if ok else f"not found: {probes_file}")

    if a.check_only:
        check_prerequisites()
        show_checks()
        if any(c["result"] == "FAIL" for c in checks):
            print("Some checks failed. Fix them, then run this again.")
            return 2
        print("All checks passed.")
        return 0

    # ---------- run folder ----------

    runs = runs_root(cfg, ROOT_DIR)
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(runs, run_id)
    while os.path.exists(run_dir):
        time.sleep(1.1)
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = os.path.join(runs, run_id)
    log_file = os.path.join(run_dir, "logs", "phase0.log")
    status_file = os.path.join(run_dir, "status.json")
    env_file = os.path.join(run_dir, "artifacts", "environment.json")

    status = {"runId": run_id, "toolVersion": TOOL_VERSION, "phase": "phase0", "state": "running", "steps": []}

    def save_status() -> None:
        write_json(status_file, status)

    def write_log(message: str) -> None:
        line = f"{datetime.datetime.now():%H:%M:%S} {message}"
        print(line, flush=True)
        append_text(log_file, line + "\n")

    def invoke_step(name: str, action) -> bool:
        step = {"name": name, "state": "running", "reason": "", "startedAt": now_iso(), "finishedAt": None}
        status["steps"].append(step)
        save_status()
        write_log(f"step {name}: running")
        try:
            action()
            step["state"] = "done"
        except Exception as e:  # noqa: BLE001 - a failed step is recorded, not raised
            step["state"] = "failed"
            step["reason"] = str(e)
        step["finishedAt"] = now_iso()
        save_status()
        write_log(f"step {name}: {step['state']}" + (f" - {step['reason']}" if step["reason"] else ""))
        return step["state"] == "done"

    def stop_run(code: int, message: str) -> int:
        status["state"] = "failed-prereq" if code == 2 else "failed-tool"
        save_status()
        write_log(message)
        write_log(f"run folder: {run_dir}")
        return code

    for sub in ("config", "artifacts", "reports", "logs", "worktrees"):
        os.makedirs(os.path.join(run_dir, sub), exist_ok=True)
    save_status()
    write_log(f"run {run_id} started (tool {TOOL_VERSION}, config {config})")

    # Snapshot of the config, rules and probes used for this run. The config hash goes into the environment record.
    shutil.copyfile(config, os.path.join(run_dir, "config", "upgrade-lens.json"))
    config_hash = file_sha256(config)

    # ---------- step 2: prerequisites ----------

    def prereq_step():
        check_prerequisites()
        show_checks()
        failed = [c for c in checks if c["result"] == "FAIL"]
        if failed:
            raise RuntimeError(f"{len(failed)} prerequisite check(s) failed")

    if not invoke_step("prereq-check", prereq_step):
        return stop_run(2, "Prerequisites failed. Fix the FAIL lines above, then run again.")

    run_rules = os.path.join(run_dir, "config", "rules")
    os.makedirs(run_rules, exist_ok=True)
    for n in rule_files():
        shutil.copyfile(os.path.join(rules_dir, n), os.path.join(run_rules, n))
    shutil.copyfile(probes_file, os.path.join(run_dir, "config", "probes.json"))

    # ---------- step 3: clone the two branches into the run ----------

    # The run owns its repository: a bare clone with the two branches only. No other checkout is read or changed.
    repo_dir = os.path.join(run_dir, "repo.git")
    base, head = cfg["baseBranch"], cfg["headBranch"]

    def clone_step():
        r = run_native(git, ["init", "--bare", "--quiet", repo_dir])
        if r.exit_code != 0:
            raise RuntimeError(f"git init failed: {r.text()}")
        # "remote add" sets the standard fetch refspec, so a developer can later fetch, pull and push as usual.
        r = run_native(git, ["-C", repo_dir, "remote", "add", "origin", repo_url])
        if r.exit_code != 0:
            raise RuntimeError(f"git remote add failed: {r.text()}")
        r = run_native(git, ["-C", repo_dir, "fetch", "--no-tags", "origin",
                             f"+refs/heads/{base}:refs/remotes/origin/{base}", f"+refs/heads/{head}:refs/remotes/origin/{head}"])
        if r.exit_code != 0:
            raise RuntimeError(f"git fetch from {repo_url} failed: {r.text()}")
        write_log(f"cloned {base} and {head} from {repo_url} into {repo_dir}")

    if not invoke_step("clone", clone_step):
        return stop_run(3, "Clone failed. See the step reason in status.json.")

    # ---------- step 4: worktrees, one directory per branch ----------

    commits, worktrees = {}, {}

    def worktrees_step():
        dir_names = {side: re.sub(r'[\\/:*?"<>|]', "_", base if side == "base" else head) for side in ("base", "head")}
        # Two branch names can map to one folder name ("a/b" and "a_b"). Keep the two checkouts apart.
        if dir_names["base"] == dir_names["head"]:
            dir_names["base"] += "-base"
            dir_names["head"] += "-head"
        for side in ("base", "head"):
            branch = base if side == "base" else head
            rev = run_native(git, ["-C", repo_dir, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}"])
            if rev.exit_code != 0:
                raise RuntimeError(f"origin/{branch} is missing after the fetch")
            commits[side] = rev.first().strip()
            path = os.path.join(run_dir, "worktrees", dir_names[side])
            if os.path.exists(path):
                raise RuntimeError(f"worktree folder already exists: {path}")
            if side == "base":
                # The baseline is only read: detached at its commit.
                add = run_native(git, ["-C", repo_dir, "worktree", "add", "--detach", path, commits[side]])
            else:
                # The change side is a real branch that tracks origin, so the developer can commit the new tests
                # stage 8 places here and push them ("git push" from this folder).
                br = run_native(git, ["-C", repo_dir, "branch", "--track", branch, f"refs/remotes/origin/{branch}"])
                if br.exit_code != 0:
                    raise RuntimeError(f"git branch {branch} failed: {br.text()}")
                add = run_native(git, ["-C", repo_dir, "worktree", "add", path, branch])
            if add.exit_code != 0:
                raise RuntimeError(f"git worktree add failed for {side}: {add.text()}")
            worktrees[side] = path
            how = f" (branch {branch}, tracking origin/{branch})" if side == "head" else " (detached)"
            write_log(f"worktree {side}: {branch} at {commits[side][:10]} -> {path}{how}")

    if not invoke_step("worktrees", worktrees_step):
        return stop_run(3, "Worktree setup failed. See the step reason in status.json.")

    # ---------- step 5: merge base (three-dot diff) ----------

    merge = {}

    def merge_base_step():
        mb = run_native(git, ["-C", repo_dir, "merge-base", commits["base"], commits["head"]])
        if mb.exit_code != 0 or not mb.output:
            raise RuntimeError(f"the branches have no common ancestor, so a three-dot diff is not possible: {mb.text()}")
        merge["base"] = mb.first().strip()
        ahead = run_native(git, ["-C", repo_dir, "rev-list", "--count", f"{merge['base']}..{commits['base']}"])
        write_log(f"merge base: {merge['base'][:10]}; base branch is {ahead.first()} commit(s) past it (not part of the diff)")

    if not invoke_step("merge-base", merge_base_step):
        return stop_run(3, "Merge base failed. See the step reason in status.json.")
    merge_base = merge["base"]

    # ---------- step 6: environment record ----------

    def environment_step():
        java_versions, maven_versions = {}, {}
        for side in ("base", "head"):
            jdk = setting(f"java.{side}")
            java_versions[side] = run_native(jdk_tool(jdk, "java"), ["-version"]).first()
            # Maven runs with the JDK of each side, so the report shows what each side builds with.
            mv = run_native(mvn, ["-v"], env=jdk_env(jdk))
            if mv.exit_code != 0:
                raise RuntimeError(f"mvn -v failed with JDK {side}")
            maven_versions[side] = {
                "maven": next((x for x in mv.output if re.match(r"^Apache Maven", x)), None),
                "java": next((x for x in mv.output if re.match(r"^Java version:", x)), None),
            }
        record = {
            "runId": run_id,
            "createdAt": now_iso(),
            "toolVersion": TOOL_VERSION,
            "configPath": config,
            "configSha256": config_hash,
            "os": platform.platform(),
            "python": platform.python_version(),
            "git": run_native(git, ["--version"]).first(),
            "repo": repo_dir,
            "repoUrl": repo_url,
            "diff": {"mode": "three-dot", "left": merge_base, "right": commits["head"], "expression": f"{base}...{head}"},
            "maven": {"settingsFile": s(setting("maven.settingsFile")), "goals": goals},
            "branches": {
                "base": {"name": base, "commit": commits["base"], "worktree": worktrees["base"], "jdk": setting("java.base"),
                         "java": java_versions["base"], "maven": maven_versions["base"]},
                "head": {"name": head, "commit": commits["head"], "worktree": worktrees["head"], "jdk": setting("java.head"),
                         "java": java_versions["head"], "maven": maven_versions["head"]},
                "mergeBase": merge_base,
            },
        }
        write_json(env_file, record)
        write_log(f"environment record: {env_file}")

    if not invoke_step("environment", environment_step):
        return stop_run(3, "Environment record failed. See the step reason in status.json.")

    # ---------- done ----------

    status["state"] = "done"
    save_status()
    write_log(f"phase 0 done. run id: {run_id}")
    write_log(f"run folder: {run_dir}")
    return 0


if __name__ == "__main__":
    run_main(main)

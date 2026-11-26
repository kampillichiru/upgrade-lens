"""Phase 1 of UpgradeLens (build and test): runs Maven on both branches in parallel, in the background,
each with its own JDK, its own worktree (so its own target\\), the configured settings.xml, and its own log.

Reads the run's config snapshot (runs\\<id>\\config\\upgrade-lens.json), so the run uses the config it
was started with. Uses the worktrees that Phase 0 recorded in artifacts\\environment.json.

Steps, in order:
  1. select-run     the given run, or the newest run whose Phase 0 is done
  2. check          worktrees, pom.xml, JDK per side, Maven and settings file
  3. build          both sides start as background processes at the same time:
                      mvn -B -ntp -s <settings> -f <worktree>\\pom.xml <maven.goals> <maven.extraArgs>
                    logged to logs\\build-<branch>.log. The script waits, prints the last log line of each
                    side every 15 s, and stops a side that runs past timeouts.buildMinutes.
  4. results        artifacts\\build-results.json (per side: exit code, BUILD line, test totals, how the
                    integration tests ran) and artifacts\\test-results.json (every test case of both builds,
                    compared one by one: same, new, removed, now-failing, now-passing, now-skipped).

Integration tests (config build.integrationTests):
  auto           (default) when a side has *IT classes and its pom binds no maven-failsafe-plugin, they are
                 run through surefire (-Dtest with the default patterns plus *IT and *ITCase), so their
                 result and coverage are part of the build. With failsafe, the pom's own setup runs them.
  failsafe-only  never add them; a side without failsafe reports them as not run.

Each side runs with its own JAVA_HOME and PATH: java.base for the base branch, java.head for the head branch.
A failure on either side is recorded (status.json, build-results.json) and returned as the exit code.

Exit codes:
  0  both sides built and tested successfully
  2  usage or config problem (no run found, worktree or JDK missing); nothing was started
  3  tool failure (Maven could not start, or a side timed out and was stopped)
  4  a side failed to build or its tests failed; build-results.json and the log say which and why

Examples:
  python scripts/phase1-build.py
  python scripts/phase1-build.py --run-id 20261126-121722
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.analysis import compare_test_results, parse_surefire_reports  # noqa: E402
from lib.core import (IS_WINDOWS, TOOL_VERSION, append_text, as_list, config_value, get, jdk_env, jdk_tool, log,  # noqa: E402
                      now_iso, ps_sorted, read_json_safe, read_lines, read_text, resolve_config_path, resolve_run_id,
                      run_main, runs_root, s, set_run_state, set_stage_log, set_stage_step, to_int, which, write_json,
                      write_text)


def last_log_line(path: str) -> str:
    if not os.path.isfile(path):
        return "(no output yet)"
    try:
        lines = read_lines(path)
    except OSError:
        return "(log busy)"
    for line in reversed(lines):
        if line.strip():
            return re.sub(r"\s+", " ", line).strip()
    return "(no output yet)"


def stop_process_tree(proc: subprocess.Popen) -> None:
    """Maven runs child JVMs, so stop the whole tree."""
    if IS_WINDOWS:
        subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def failsafe_bound(pom_text: str) -> bool:
    """Failsafe declared in <build><plugins>. A declaration only inside <pluginManagement> does not run."""
    t = re.sub(r"(?s)<pluginManagement>.*?</pluginManagement>", "", pom_text)
    t = re.sub(r"(?s)<!--.*?-->", "", t)
    return re.search(r"<artifactId>\s*maven-failsafe-plugin\s*</artifactId>", t, re.I) is not None


def it_classes(worktree: str) -> list[str]:
    root = os.path.join(worktree, "src", "test")
    out = []
    for d, _dirs, files in os.walk(root):
        for f in files:
            if re.search(r"IT(Case)?\.java$", f, re.I):
                out.append(os.path.splitext(f)[0])
    return ps_sorted(out)


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 1: build and test both branches in parallel.")
    p.add_argument("--run-id")
    p.add_argument("--timeout-minutes", type=int, default=0, help="stop a side that runs longer (default: timeouts.buildMinutes, else 60)")
    p.add_argument("--config")
    a = p.parse_args(argv)

    # ---------- step 1: select run ----------

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))
    run_id = resolve_run_id(runs, a.run_id, ["done", "phase1-done", "failed-build"])
    if not run_id:
        print(f"No run with Phase 0 done under {runs}. Run phase0-setup.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    status_file = os.path.join(run_dir, "status.json")
    artifacts = os.path.join(run_dir, "artifacts")
    logs_dir = os.path.join(run_dir, "logs")
    set_stage_log(os.path.join(logs_dir, "phase1.log"))

    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    status = read_json_safe(status_file)
    env_rec = read_json_safe(os.path.join(artifacts, "environment.json"))
    if not cfg or not status or not env_rec:
        print(f"Run not found or incomplete: {run_dir} (needs status.json, config\\upgrade-lens.json and artifacts\\environment.json)")
        return 2
    timeout_minutes = a.timeout_minutes if a.timeout_minutes > 0 else to_int(config_value(cfg, "timeouts.buildMinutes", 60), 60)
    goals = s(config_value(cfg, "maven.goals", "clean verify")).split()
    extra_args = [s(x) for x in as_list(config_value(cfg, "maven.extraArgs", [])) if s(x)]
    it_config = s(config_value(cfg, "build.integrationTests", "auto"))

    log(f"phase 1 build for run {run_id}")

    # ---------- step 2: check ----------

    sides = []
    for side, branch_key in (("base", "baseBranch"), ("head", "headBranch")):
        wt = get(env_rec, "branches", side, "worktree")
        dir_name = os.path.basename(s(wt).rstrip("\\/"))
        sides.append({"side": side, "branch": cfg.get(branch_key), "jdk": get(cfg, "java", side),
                      "commit": get(env_rec, "branches", side, "commit"), "worktree": wt,
                      "log": os.path.join(logs_dir, f"build-{dir_name}.log"), "step": f"build-{dir_name}"})

    problems = []
    mvn_path = which(s(get(cfg, "maven", "command")))
    if not mvn_path:
        problems.append(f"maven '{s(get(cfg, 'maven', 'command'))}' not on PATH")
    settings = s(get(cfg, "maven", "settingsFile"))
    if settings and not os.path.isfile(settings):
        problems.append(f"Maven settings not found: {settings}")
    for sd in sides:
        if not sd["worktree"] or not os.path.isfile(os.path.join(sd["worktree"], "pom.xml")):
            problems.append(f"{sd['side']}: no pom.xml in {sd['worktree']}")
        if not os.path.isfile(jdk_tool(sd["jdk"], "java")):
            problems.append(f"{sd['side']}: no java under {sd['jdk']}")
    if problems:
        for x in problems:
            log(f"check failed: {x}")
        return 2

    # How each side runs its integration tests.
    for sd in sides:
        classes = it_classes(sd["worktree"])
        pom = read_text(os.path.join(sd["worktree"], "pom.xml"))
        if not classes:
            mode = "none"
        elif failsafe_bound(pom):
            mode = "failsafe"
        elif it_config == "failsafe-only":
            mode = "not-run"
        else:
            mode = "surefire"
        sd["itMode"] = mode
        sd["itClasses"] = classes
        log(f"{sd['side']}: {len(classes)} integration test class(es), mode {mode}")

    # ---------- step 3: build both sides in the background ----------

    def start_side_build(sd: dict):
        mvn_args = ["-B", "-ntp"]
        if settings:
            mvn_args += ["-s", settings]
        mvn_args += ["-f", os.path.join(sd["worktree"], "pom.xml")]
        if sd["itMode"] == "surefire":
            mvn_args += ["-Dtest=Test*,*Test,*Tests,*TestCase,*IT,*ITCase", "-Dsurefire.failIfNoSpecifiedTests=false"]
        mvn_args += extra_args + goals
        # Appending keeps the header lines written before the build started. Each side gets its own JDK.
        out = open(sd["log"], "ab")
        try:
            kwargs = {"creationflags": 0x08000000} if IS_WINDOWS else {"start_new_session": True}  # CREATE_NO_WINDOW
            proc = subprocess.Popen([mvn_path] + mvn_args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=sd["worktree"], env=jdk_env(sd["jdk"]), **kwargs)
        finally:
            out.close()
        return proc, "mvn " + subprocess.list2cmdline(mvn_args)

    builds = []
    for sd in sides:
        if os.path.isfile(sd["log"]):
            os.remove(sd["log"])
        header = (f"# {now_iso()} phase 1 build: branch {sd['branch']} at {sd['commit']}\n# worktree {sd['worktree']}\n"
                  f"# JAVA_HOME {sd['jdk']}\n# settings {settings or '(Maven default)'}\n# integration tests: {sd['itMode']}\n")
        try:
            write_text(sd["log"], header)
            proc, cmd = start_side_build(sd)
        except OSError as e:
            log(f"{sd['step']}: could not start Maven: {e}")
            for b in builds:
                if b["proc"].poll() is None:
                    stop_process_tree(b["proc"])
            set_stage_step(status, sd["step"], "failed", f"could not start Maven: {e}")
            set_run_state(status, "failed-tool", "phase1")
            write_json(status_file, status)
            return 3
        append_text(sd["log"], f"# command: {cmd}\n")
        builds.append({"side": sd, "proc": proc, "startedAt": now_iso(), "timedOut": False, "command": cmd})
        log(f"{sd['step']}: started (pid {proc.pid}), log: {sd['log']}")

    # The child processes already run in the background. This loop only waits and reports progress.
    deadline = time.time() + timeout_minutes * 60
    last_report = 0.0
    while any(b["proc"].poll() is None for b in builds):
        if time.time() > deadline:
            for b in builds:
                if b["proc"].poll() is None:
                    stop_process_tree(b["proc"])
                    b["timedOut"] = True
                    log(f"{b['side']['step']}: timed out after {timeout_minutes} min, stopped")
            break
        if time.time() - last_report >= 15:
            for b in builds:
                if b["proc"].poll() is None:
                    log(f"{b['side']['step']}: {last_log_line(b['side']['log'])}")
            last_report = time.time()
        time.sleep(2)

    # ---------- step 4: record results ----------

    results, side_results, test_cases = [], {}, {}
    for b in builds:
        sd = b["side"]
        try:
            b["proc"].wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        rc = b["proc"].poll()
        exit_code = -1 if b["timedOut"] or rc is None else rc
        log_lines = read_lines(sd["log"]) if os.path.isfile(sd["log"]) else []
        build_line = next((x for x in reversed(log_lines) if re.search(r"BUILD (SUCCESS|FAILURE|ERROR)", x)), None)
        # Maven prints one "Tests run:" total per test plugin run (surefire for *Test, failsafe for *IT).
        test_lines = [x.strip() for x in log_lines
                      if re.search(r"\[(INFO|ERROR|WARNING)\] Tests run: \d+, Failures: \d+, Errors: \d+, Skipped: \d+$", x, re.I)]
        error_lines = [x.strip() for x in log_lines if re.search(r"^\[ERROR\]", x, re.I)][:15]

        reports = []
        for sub in (("target", "surefire-reports"), ("target", "failsafe-reports")):
            d = os.path.join(sd["worktree"], *sub)
            if os.path.isdir(d):
                reports += [os.path.join(d, n) for n in ps_sorted(os.listdir(d)) if n.startswith("TEST-") and n.endswith(".xml")]
        cases = parse_surefire_reports(reports)
        test_cases[sd["side"]] = cases

        state = "done" if exit_code == 0 else "failed"
        # The first decisive Maven error says why: a failed goal, failing tests or a compilation error.
        cause = next((x for x in error_lines if re.search(r"Failed to execute goal|There are test failures|COMPILATION ERROR|"
                                                          r"Tests run:.*Failures: [1-9]|Could not resolve", x, re.I)), "")
        cause = re.sub(r"\s*->\s*\[Help \d+\]$", "", re.sub(r"^\[ERROR\]\s*", "", cause, flags=re.I), flags=re.I) if cause else ""
        if b["timedOut"]:
            reason = f"timed out after {timeout_minutes} min"
        elif exit_code != 0:
            reason = f"mvn exit code {exit_code}" + (f": {cause}" if cause else "") + f"; see {sd['log']}"
        else:
            reason = ""
        build_result = build_line.strip() if build_line else ""
        results.append({"name": sd["step"], "state": state, "reason": reason, "branch": sd["branch"], "commit": sd["commit"],
                        "jdk": sd["jdk"], "exitCode": exit_code, "buildResult": build_result, "testSummary": " | ".join(test_lines),
                        "log": sd["log"], "startedAt": b["startedAt"], "finishedAt": now_iso()})
        side_results[sd["side"]] = {
            "branch": sd["branch"], "commit": sd["commit"], "worktree": sd["worktree"], "jdk": sd["jdk"], "command": b["command"],
            "exitCode": exit_code, "timedOut": b["timedOut"],
            "result": build_result or "no BUILD line (Maven did not finish)",
            "testTotals": test_lines,
            "tests": {"total": len(cases), "passed": len([c for c in cases if c["status"] == "passed"]),
                      "failed": len([c for c in cases if c["status"] in ("failed", "error")]),
                      "skipped": len([c for c in cases if c["status"] == "skipped"])},
            "integrationTests": {"mode": sd["itMode"], "classes": sd["itClasses"]},
            "errors": error_lines,
            "log": sd["log"],
        }
        log(f"{sd['step']}: {state}" + (f" - {reason}" if reason else ""))
        if build_line:
            log(f"{sd['step']}: {build_result}")
        for t in test_lines:
            log(f"{sd['step']}: {t}")

    comparison = compare_test_results(test_cases.get("base"), test_cases.get("head"))
    count = lambda change: len([c for c in comparison if c["change"] == change])  # noqa: E731
    write_json(os.path.join(artifacts, "test-results.json"), {
        "schemaVersion": 1, "runId": run_id,
        "summary": {"tests": len(comparison), "same": count("same"), "new": count("new"), "removed": count("removed"),
                    "nowFailing": count("now-failing"), "nowPassing": count("now-passing"), "nowSkipped": count("now-skipped"),
                    "nowRunning": count("now-running")},
        "tests": comparison,
    })
    for c in comparison:
        if c["change"] != "same":
            log(f"  test {c['change']}: {c['test']} (base {c['base'] or '-'}, head {c['head'] or '-'})")

    all_done = all(r["state"] == "done" for r in results)
    any_timeout = any(b["timedOut"] for b in builds)
    write_json(os.path.join(artifacts, "build-results.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "goals": " ".join(goals), "settingsFile": settings,
        "parallel": True, "ok": all_done, "sides": side_results,
    })

    # Keep Phase 0 steps; replace the build steps. Phase 1 never removes earlier steps.
    for r in results:
        status["steps"] = [x for x in as_list(status.get("steps")) if x and x.get("name") != r["name"]] + [r]
    if all_done:
        set_run_state(status, "phase1-done", "phase1")
    elif any_timeout:
        set_run_state(status, "failed-tool", "phase1")
    else:
        set_run_state(status, "failed-build", "phase1")
    write_json(status_file, status)

    if all_done:
        log(f"phase 1 done: both branches built and tested. Logs in {logs_dir}")
        return 0
    if any_timeout:
        log(f"phase 1 stopped: a build timed out. Logs in {logs_dir}")
        return 3
    for r in results:
        if r["state"] == "done":
            continue
        log(f"FAILED {r['branch']}: {r['buildResult']} {r['testSummary']}. Log: {r['log']}")
        side = "base" if r["branch"] == cfg.get("baseBranch") else "head"
        for e in side_results[side]["errors"][:5]:
            log(f"  {e}")
    log(f"phase 1 finished with a failed build or failed tests. Read the log of the failed side in {logs_dir}")
    return 4


if __name__ == "__main__":
    run_main(main)

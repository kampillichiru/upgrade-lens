"""Stage 11 of UpgradeLens (assertion strength): do the existing tests that run a changed line also check it?

For each changed main-code method whose changed lines a test runs on change_branch (artifacts\\changed-methods.json,
lines.covered), it changes one changed line at a time (lib\\mutation.py: a flipped comparison, a number + 1, a
changed string, a removed call, ...), runs the existing tests on the head worktree, and restores the file byte for
byte. A mutant that makes a test fail is killed; one that leaves every test passing survived: the tests run that
line but their assertions do not pin what it does. A method with a surviving mutant is "weak", and the gaps job turns
it into a weak-assertion gap for the test writer.

Runs in phase 04-tests (job asserts), after the probes and before the gaps. Head only: it judges the tests the
upgrade will ship with. Limits from config: asserts.enabled (default true), asserts.maxMutants (12 per run),
asserts.maxPerMethod (3), asserts.mutantMinutes (5, the timeout of one test run).

Writes artifacts\\assert-strength.json, also when there is nothing to check (enabled false, no changed lines).

Exit codes:
  0  written
  2  usage or run problem (no run, stage 4 not done)
  3  tool failure (Maven missing, a file could not be restored)

Example:
  python .github/upgradelens/scripts/phase11-asserts.py --run-id 20261127-133238
"""

import argparse
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import (TOOL_VERSION, VERDICT_STATES, as_list, config_value, file_sha256, get, items, jdk_env,  # noqa: E402
                      log, native_path, now_iso, read_json_safe, resolve_config_path, run_main, run_native, runs_root, s,
                      set_stage_log, to_int, which, write_json, write_text)
from lib.mutation import method_strength, plan_mutants  # noqa: E402
from lib.standalone import StagePaths, add_standalone_arg, select_run  # noqa: E402

LATER_STATES = [f"phase{n}-done" for n in range(4, 12)] + VERDICT_STATES


def failed_tests(reports_dirs) -> tuple[set, int]:
    """(failed "fqcn#method" names, number of tests run) from the surefire and failsafe XML reports."""
    failed, total = set(), 0
    for d in reports_dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not (name.startswith("TEST-") and name.endswith(".xml")):
                continue
            try:
                suite = ET.parse(os.path.join(d, name)).getroot()
            except ET.ParseError:
                continue
            for tc in suite.findall("testcase"):
                total += 1
                if tc.find("failure") is not None or tc.find("error") is not None:
                    failed.add(f"{s(tc.get('classname'))}#{s(tc.get('name'))}")
    return failed, total


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 11: assertion strength of the existing tests on changed lines.")
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
        print(f"No run with stage 4 done under {runs}. Run phase4-impact.py first.")
        return 2
    run_dir = os.path.join(runs, run_id)
    paths = StagePaths(run_dir, a.standalone)
    set_stage_log(paths.out("logs", "phase11.log"))
    out_file = paths.out("artifacts", "assert-strength.json")
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json")) or {}
    env_rec = paths.read("artifacts", "environment.json")
    changed = paths.read("artifacts", "changed-methods.json")
    builds = paths.read("artifacts", "build-results.json")
    results = paths.read("artifacts", "test-results.json")
    if not env_rec or not changed:
        print(f"Run incomplete: {run_dir} (needs artifacts\\environment.json and artifacts\\changed-methods.json)")
        return 2

    enabled = bool(config_value(cfg, "asserts.enabled", True))
    max_total = to_int(config_value(cfg, "asserts.maxMutants", 12), 12)
    per_method = to_int(config_value(cfg, "asserts.maxPerMethod", 3), 3)
    timeout = to_int(config_value(cfg, "asserts.mutantMinutes", 5), 5) * 60

    candidates = [m for m in items(get(changed, "methods"))
                  if m.get("kind") != "file" and as_list(get(m, "lines", "covered"))]
    doc = {"schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "enabled": enabled, "createdAt": now_iso(),
           "limits": {"maxMutants": max_total, "maxPerMethod": per_method, "mutantMinutes": timeout // 60},
           "methods": [], "summary": {}}
    log(f"phase 11 assertion strength for run {run_id}: {len(candidates)} changed method(s) with covered changed lines")
    if not enabled or not candidates:
        doc["summary"] = {"methods": 0, "mutants": 0, "reason": "asserts.enabled is false" if not enabled else
                          "no changed main-code line that a test runs"}
        write_json(out_file, doc)
        log(f"nothing to check: {doc['summary']['reason']}")
        return 0

    head_wt = get(env_rec, "branches", "head", "worktree")
    jdk = get(cfg, "java", "head")
    mvn = which(s(get(cfg, "maven", "command")))
    if not mvn:
        log(f"maven '{s(get(cfg, 'maven', 'command'))}' not on PATH")
        return 3
    settings = s(get(cfg, "maven", "settingsFile"))
    head_cmd = s(get(builds, "sides", "head", "command"))
    test_filter = re.search(r"(-Dtest=\S+)", head_cmd)
    args = ["-B", "-ntp"] + (["-s", settings] if settings and os.path.isfile(settings) else []) + \
           ["-f", os.path.join(head_wt, "pom.xml")] + \
           ([test_filter.group(1), "-Dsurefire.failIfNoSpecifiedTests=false"] if test_filter else []) + \
           ["-Djacoco.skip=true", "-Dmaven.test.failure.ignore=true", "test"]
    reports = [os.path.join(head_wt, "target", "surefire-reports"), os.path.join(head_wt, "target", "failsafe-reports")]
    # Tests that already fail on change_branch without any mutant cannot kill one.
    baseline = {s(t.get("test")) for t in items(get(results, "tests")) if t.get("head") in ("failed", "error")}
    logs_dir = paths.out("logs")

    done = 0
    for m in candidates:
        rel = s(m.get("file"))
        target = os.path.join(head_wt, native_path(rel))
        if not os.path.isfile(target):
            log(f"skip {rel}: not in the head worktree")
            continue
        with open(target, "rb") as f:
            original_bytes = f.read()
        text = original_bytes.decode("utf-8-sig", errors="replace")
        src_lines = text.split("\n")
        by_number = {i + 1: ln.rstrip("\r") for i, ln in enumerate(src_lines)}
        plan = plan_mutants(by_number, get(m, "lines", "covered"), per_method)
        row = {"file": rel, "kind": m.get("kind"), "name": m.get("name"), "symbol": m.get("symbol"),
               "coveredLines": as_list(get(m, "lines", "covered")), "mutants": []}
        for mu in plan:
            if done >= max_total:
                row["mutants"].append(dict(mu, result="not-run", detail=f"limit of {max_total} mutants per run reached"))
                continue
            done += 1
            i = mu["line"] - 1
            eol = "\r" if src_lines[i].endswith("\r") else ""
            mutated_lines = list(src_lines)
            mutated_lines[i] = mu.pop("mutatedLine") + eol
            original_hash = file_sha256(target)
            started = time.time()
            for d in reports:
                shutil.rmtree(d, ignore_errors=True)
            try:
                with open(target, "wb") as f:
                    f.write("\n".join(mutated_lines).encode("utf-8"))
                r = run_native(mvn, args, env=jdk_env(jdk), timeout=timeout)
            finally:
                with open(target, "wb") as f:
                    f.write(original_bytes)
            if file_sha256(target) != original_hash:
                log(f"could not restore {rel} after a mutant")
                return 3
            out = list(r.output)
            log_path = os.path.join(logs_dir, f"asserts-{done:02d}.log")
            write_text(log_path, "\n".join(out) + "\n")
            failed, total = failed_tests(reports)
            killers = sorted(failed - baseline)
            if r.exit_code == -1 and any("timed out" in x.lower() for x in out):
                result = "timeout"   # a mutant that makes the tests hang changed behavior too (PIT counts it as killed)
            elif any("COMPILATION ERROR" in x for x in out) and total == 0:
                result = "not-compiled"
            elif total == 0:
                result = "error"
            else:
                result = "killed" if killers else "survived"
            row["mutants"].append(dict(mu, result=result, killedBy=killers[:5], testsRun=total,
                                       seconds=round(time.time() - started, 1), log=log_path))
            log(f"  {rel}:{mu['line']} {mu['operator']}: {result}" + (f" by {', '.join(killers[:3])}" if killers else "")
                + f" ({total} tests, {time.time() - started:.0f} s)")
        row["strength"] = method_strength(row["mutants"])
        doc["methods"].append(row)
        log(f"{rel} {m.get('name')}: {row['strength']}")

    # Leave the head build as it was: compile the restored sources once more.
    run_native(mvn, ["-B", "-ntp", "-q"] + (["-s", settings] if settings and os.path.isfile(settings) else [])
               + ["-f", os.path.join(head_wt, "pom.xml"), "-Djacoco.skip=true", "test-compile"], env=jdk_env(jdk), timeout=timeout)
    strengths = [x["strength"] for x in doc["methods"]]
    doc["summary"] = {"methods": len(doc["methods"]), "mutants": done, "strong": strengths.count("strong"),
                      "weak": strengths.count("weak"), "unknown": strengths.count("unknown")}
    write_json(out_file, doc)
    log(f"assertion strength: {doc['summary']}")
    return 0


if __name__ == "__main__":
    run_main(main)

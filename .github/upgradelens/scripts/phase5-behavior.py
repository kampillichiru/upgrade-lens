"""Stage 5 of UpgradeLens (behavior checks): starts the application on both branches, compares what it
does, runs the proposed tests on master and on change_branch, and records what cannot be run here.

Steps, in order:
  1. startup     starts each side's jar with its own JDK on a free port, waits for /actuator/health
                 to report UP, records startup time and heap (jcmd GC.heap_info)
  2. dumps       /actuator/beans (bean names and types) and /actuator/env (application config),
                 diffed between the two sides
  3. probes      config\\probes.json, one request at a time on a fresh server per side; compares status,
                 content type and body (timestamps masked). Probe errors are recorded as errors.
  4. tests       the proposed tests (artifacts\\agent\\tests-manifest.json, written by the test writer).
                 Each test must pass on master. On change_branch it is run unchanged, and then once per
                 injected regression (a small find-and-replace on a main file, restored afterwards); the
                 test must fail for each injection to count as a regression detector.
  5. gaps        what this stage did not run, and why (Testcontainers, mutation testing, migrations)
Writes artifacts\\behavior-results.json and artifacts\\gaps.json.

The servers run from the worktrees of the run. The proposed tests are copied into the worktrees for their
run and removed afterwards (tests that stage 8 placed in the head worktree on an earlier pass are taken out
first, and stage 8 places them again). Injected regressions are restored byte for byte (checked by hash).

Exit codes:
  0  stage ran (the verdict decides what the results mean)
  2  usage or run problem (no run, stage 4 not done)
  3  a tool could not run (no JDK tool, Maven, or no jar)

Example:
  python .github/upgradelens/scripts/phase5-behavior.py --run-id 20261126-131717
"""

import argparse
import atexit
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.agents import agent_proposal  # noqa: E402
from lib.analysis import app_properties, case_checks, case_results  # noqa: E402
from lib.core import (IS_WINDOWS, MAVEN_NATIVE_GIT_ARGS, ROOT_DIR, TOOL_VERSION, as_list, config_value, file_sha256, get,  # noqa: E402
                      items, jdk_env, jdk_tool, log, native_path, ps_sorted, read_json_safe, resolve_config_path,
                      resolve_project_path, run_main, run_native, runs_root, s, set_run_state, set_stage_log,
                      set_stage_step, to_int, unique_sorted, write_json, write_text)
from lib.standalone import StagePaths, add_standalone_arg, select_run  # noqa: E402

from lib.core import VERDICT_STATES  # noqa: E402

LATER_STATES = ["phase4-done", "phase5-done", "phase6-done", "phase7-done", "phase8-done", "phase9-done",
                ] + VERDICT_STATES


# ---------- helpers ----------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def masked_body(text) -> str:
    """Timestamps differ between runs by design, so they are masked before comparing."""
    if text is None:
        return ""
    t = re.sub(r'"timestamp"\s*:\s*"[^"]*"', '"timestamp":"<masked>"', text, flags=re.I)
    t = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?", "<masked-time>", t, flags=re.I)
    return t.strip()


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def http(method: str, url: str, headers=None, body=None, timeout: float = 15):
    """(status, content type, text) of one request. An HTTP error status is a response, not an exception;
    a connection failure raises."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    if data is None and method.upper() in ("POST", "PUT", "PATCH"):
        data = b""
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, s(r.headers.get("Content-Type")), decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, s(e.headers.get("Content-Type")) if e.headers else "", decode(e.read() or b"")


def run_probe(port: int, probe: dict) -> dict:
    """Status, content type and masked body. Connection failures are returned as an error."""
    headers = {k: s(v) for k, v in (probe.get("headers") or {}).items()}
    body = probe.get("body")
    if body and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"
    try:
        status, ctype, text = http(s(probe.get("method") or "GET"), f"http://localhost:{port}{s(probe.get('path'))}", headers, body)
        return {"status": status, "contentType": ctype, "body": masked_body(text)}
    except Exception as e:  # noqa: BLE001 - every client failure is a probe error
        return {"error": str(e)}


def bean_set(doc) -> dict:
    """Bean name -> type. A name can repeat across contexts only with the same type in practice; the first wins."""
    out = {}
    for ctx in (get(doc, "contexts") or {}).values():
        for name, bean in (get(ctx, "beans") or {}).items():
            out.setdefault(name, s(get(bean, "type")))
    return out


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 5: startup, dumps, probes and proposed tests on both branches.")
    p.add_argument("--run-id")
    p.add_argument("--startup-timeout-seconds", type=int, default=0)
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
        print(f"No run with stage 4 done under {runs}. Run phase4-impact.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    paths = StagePaths(run_dir, a.standalone)
    artifacts = paths.out("artifacts")
    agent_tests_dir = os.path.join(run_dir, "artifacts", "agent", "tests")  # agent output: always the run's own
    logs_dir = paths.out("logs")
    status_file = paths.out("status.json")
    set_stage_log(os.path.join(logs_dir, "phase5.log"))
    art = lambda name: paths.read("artifacts", name)  # noqa: E731
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    env_rec, status = art("environment.json"), paths.read("status.json")
    impact, reach, inventory = art("impact-findings.json"), art("reach.json"), art("change-inventory.json")
    if not all((cfg, env_rec, status, impact, reach)):
        print(f"Run incomplete: {run_dir} (needs stage 4)")
        return 2
    startup_timeout = a.startup_timeout_seconds if a.startup_timeout_seconds > 0 else to_int(config_value(cfg, "timeouts.startupSeconds", 120), 120)
    # The probe manifest: the configured file, snapshotted into the run so the report shows what was run.
    probes_src = resolve_project_path(s(config_value(live_cfg, "probesFile", "config\\probes.json")), ROOT_DIR)
    probes_run = paths.out("config", "probes.json")
    os.makedirs(os.path.dirname(probes_run), exist_ok=True)
    if os.path.isfile(probes_src):
        shutil.copyfile(probes_src, probes_run)
    probe_manifest = read_json_safe(probes_run)
    if not probe_manifest:
        print(f"Probe manifest not found: {probes_src}")
        return 2
    settings = s(get(cfg, "maven", "settingsFile"))
    settings = settings if settings and os.path.isfile(settings) else None
    mvn = s(get(cfg, "maven", "command"))

    def stop_stage(code: int, reason: str) -> int:
        log(reason)
        set_stage_step(status, "behavior", "failed", reason)
        set_run_state(status, "failed-tool", "phase5")
        write_json(status_file, status)
        return code

    log(f"phase 5 behavior checks for run {run_id}")
    sides = [{"side": side, "branch": cfg.get(f"{side}Branch"), "jdk": get(cfg, "java", side),
              "worktree": get(env_rec, "branches", side, "worktree")} for side in ("base", "head")]
    for sd in sides:
        target = os.path.join(sd["worktree"], "target")
        jars = [n for n in ps_sorted(os.listdir(target)) if n.endswith(".jar")] if os.path.isdir(target) else []
        if not jars:
            return stop_stage(3, f"{sd['side']}: no jar in {target}. Run stage 1 first.")
        sd["jar"] = os.path.join(target, jars[0])
        sd["java"] = jdk_tool(sd["jdk"], "java")
        sd["jcmd"] = jdk_tool(sd["jdk"], "jcmd")
        if not os.path.isfile(sd["java"]):
            return stop_stage(3, f"{sd['side']}: no java under {sd['jdk']}")

    def start_app(sd: dict, port: int) -> dict:
        """Starts the jar, waits for UP. Returns the process, startup ms, heap and an error if any."""
        out_log = os.path.join(logs_dir, f"app-{sd['side']}.out.log")
        err_log = os.path.join(logs_dir, f"app-{sd['side']}.err.log")
        args = [sd["java"], "-jar", sd["jar"], f"--server.port={port}",
                "--management.endpoints.web.exposure.include=health,beans,env", "--management.endpoint.health.show-details=never",
                # Spring Boot 3+ masks every /actuator/env value by default, so a changed value would look the same.
                # Only this local run sees the values; they are compared, not written to the report.
                "--management.endpoint.env.show-values=ALWAYS"]
        started = time.time()
        with open(out_log, "wb") as out, open(err_log, "wb") as err:
            kwargs = {"creationflags": 0x08000000} if IS_WINDOWS else {}  # CREATE_NO_WINDOW
            proc = subprocess.Popen(args, stdout=out, stderr=err, stdin=subprocess.DEVNULL, cwd=sd["worktree"],
                                    env=jdk_env(sd["jdk"]), **kwargs)
        up = False
        while time.time() < started + startup_timeout:
            if proc.poll() is not None:
                break
            try:
                _st, _ct, text = http("GET", f"http://localhost:{port}/actuator/health", timeout=3)
                if re.search(r'"status"\s*:\s*"UP"', text, re.I):
                    up = True
                    break
            except Exception:  # noqa: BLE001 - not listening yet
                pass
            time.sleep(0.5)
        ms = int((time.time() - started) * 1000)
        heap = ""
        if up and os.path.isfile(sd["jcmd"]):
            heap = next((x for x in run_native(sd["jcmd"], [str(proc.pid), "GC.heap_info"]).output if re.search(r"total", x, re.I)), "")
        error = None
        if not up:
            error = (f"process exited with code {proc.returncode}; see {err_log}" if proc.poll() is not None
                     else f"not UP after {startup_timeout} s; see {err_log}")
        return {"proc": proc, "up": up, "startupMs": ms, "heap": heap, "error": error, "port": port}

    def stop_app(handle) -> None:
        proc = handle.get("proc") if handle else None
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass

    # ---------- 1 to 3: startup, dumps, probes ----------

    handles, server_results, probe_results, dumps = {}, {}, {}, {}
    startup_ok = True
    startup_errors = []

    def fetch_json(port: int, path: str):
        status_code, _ctype, text = http("GET", f"http://localhost:{port}{path}", timeout=30)
        if status_code != 200:
            raise RuntimeError(f"{path} returned HTTP {status_code}")
        return json.loads(text)

    try:
        # Each side has its own try: a failure on one side (a server that does not start, a dump that is
        # refused) is recorded for that side, and the other side still runs.
        for sd in sides:
            side = sd["side"]
            try:
                port = free_port()
                log(f"{side}: starting {sd['jar']} on port {port} with {sd['jdk']}")
                h = start_app(sd, port)
                handles[side] = h
                server_results[side] = {"port": h["port"], "up": h["up"], "startupMs": h["startupMs"], "heap": h["heap"],
                                        "error": h["error"], "jdk": sd["jdk"]}
            except Exception as e:  # noqa: BLE001 - recorded as this side's startup error
                server_results[side] = {"port": None, "up": False, "startupMs": None, "heap": "", "error": f"could not start: {e}",
                                        "jdk": sd["jdk"]}
            if not server_results[side]["up"]:
                startup_ok = False
                startup_errors.append(f"{side} ({sd['branch']}) application did not start: {server_results[side]['error']}")
                log(f"{side}: startup failed: {server_results[side]['error']}")
                continue
            h = handles[side]
            log(f"{side}: UP in {h['startupMs']} ms. {h['heap']}")
            try:
                dumps[side] = {"beans": bean_set(fetch_json(h["port"], "/actuator/beans")),
                               "props": app_properties(fetch_json(h["port"], "/actuator/env"))}
            except Exception as e:  # noqa: BLE001 - no dump for this side; probes still run
                server_results[side]["dumpError"] = str(e)
                log(f"{side}: bean and property dump not available: {e}")
            try:
                probe_results[side] = [{"id": pr.get("id"), "result": run_probe(h["port"], pr)}
                                       for pr in items(probe_manifest.get("probes"))]
                log(f"{side}: {len(probe_results[side])} probe(s) run")
            except Exception as e:  # noqa: BLE001 - the probes of this side become errors below
                log(f"{side}: probes threw: {e}")
    finally:
        for h in handles.values():
            stop_app(h)

    # Compare the two sides, probe by probe.
    checks, probe_rows = [], []
    # An application that does not start is the largest behavior change an upgrade can make, and without both
    # servers nothing can be compared: it blocks the verdict even when no probe is configured.
    blocking_errors = list(startup_errors)
    for pr in items(probe_manifest.get("probes")):
        b = next((x for x in probe_results.get("base", []) if x["id"] == pr.get("id")), None)
        hd = next((x for x in probe_results.get("head", []) if x["id"] == pr.get("id")), None)
        diffs, outcome = [], "same"
        if not b or not hd or b["result"].get("error") or hd["result"].get("error"):
            outcome = "error"
            errs = [x for x in (get(b, "result", "error"), get(hd, "result", "error")) if x]
            blocking_errors.append(f"probe {pr.get('id')}: " + "; ".join(errs))
        else:
            br, hr = b["result"], hd["result"]
            if br["status"] != hr["status"]:
                diffs.append(f"status {br['status']} -> {hr['status']}")
            if re.sub(r"\s", "", br["contentType"]).lower() != re.sub(r"\s", "", hr["contentType"]).lower():
                diffs.append(f"content-type '{br['contentType']}' -> '{hr['contentType']}'")
            if br["body"] != hr["body"]:
                diffs.append("body differs")
            if diffs:
                outcome = "different"
        for rule in as_list(pr.get("linkedRules")):
            checks.append({"ruleId": rule, "source": "probe", "id": pr.get("id"), "outcome": outcome})
        probe_rows.append({"id": pr.get("id"), "flowId": pr.get("flowId"), "request": f"{pr.get('method')} {pr.get('path')}",
                           "linkedRules": as_list(pr.get("linkedRules")), "outcome": outcome, "differences": diffs,
                           "base": b["result"] if b else None, "head": hd["result"] if hd else None})

    # Dump differences: beans and application properties.
    dump_diff = {"available": "base" in dumps and "head" in dumps}
    if not dump_diff["available"]:
        dump_diff["reason"] = "; ".join(f"{sd['side']}: {server_results.get(sd['side'], {}).get('dumpError') or server_results.get(sd['side'], {}).get('error') or 'no dump'}"
                                        for sd in sides if sd["side"] not in dumps)
    if dump_diff["available"]:
        # Beans are compared by name. A bean whose name stays but whose class moved is a type change.
        bb, hb = dumps["base"]["beans"], dumps["head"]["beans"]
        dump_diff["beans"] = {"base": len(bb), "head": len(hb),
                              "added": ps_sorted([k for k in hb if k not in bb]),
                              "removed": ps_sorted([k for k in bb if k not in hb]),
                              "typeChanged": ps_sorted([k for k in bb if k in hb and bb[k] != hb[k]])}
        bp, hp = dumps["base"]["props"], dumps["head"]["props"]
        dump_diff["properties"] = {"added": ps_sorted([k for k in hp if k not in bp]),
                                   "removed": ps_sorted([k for k in bp if k not in hp]),
                                   "changed": ps_sorted([k for k in bp if k in hp and bp[k] != hp[k]])}
        db = dump_diff["beans"]
        log(f"dumps: beans {len(bb)} -> {len(hb)} (added {len(db['added'])}, removed {len(db['removed'])}, type changed "
            f"{len(db['typeChanged'])}); app properties changed {len(dump_diff['properties']['changed'])}")

    # ---------- 4. proposed tests ----------

    manifest_path = os.path.join(run_dir, "artifacts", "agent", "tests-manifest.json")
    test_rows = []
    test_state = "not-proposed"
    head_wt = get(env_rec, "branches", "head", "worktree")

    # Tests that stage 8 placed in the head worktree on an earlier pass: take them out so the run below starts
    # from change_branch's own files (stage 8 places them again). Only files whose bytes are still the ones
    # stage 8 wrote are removed; anything else is left alone and reported.
    placed_record = art("new-tests.json")
    edited_by_developer = set()
    # Standalone: stage 8 does not place them again, so they are put back when this stage ends (any exit).
    taken_out = {}
    if a.standalone:
        atexit.register(restore_placed_tests, taken_out)
    for pl in items(get(placed_record, "placed")):
        dst = os.path.join(head_wt, native_path(pl.get("relativePath")))
        if not os.path.isfile(dst):
            continue
        if file_sha256(dst) == pl.get("sha256"):
            if a.standalone:
                with open(dst, "rb") as fh:
                    taken_out[dst] = fh.read()
            os.remove(dst)
            log(f"tests: removed {pl.get('relativePath')} placed by stage 8, before the run")
        else:
            edited_by_developer.add(s(pl.get("relativePath")))
            log(f"tests: {pl.get('relativePath')} in the head worktree was edited after stage 8 placed it; left as is")

    git = s(get(cfg, "git", "command", default="git"))

    def repair_worktrees(manifest: dict) -> None:
        """A stage that was killed (Ctrl+C, a closed terminal, a crash) cannot run its cleanup, so an injected
        regression or a copied test file can be left in a worktree. The worktrees are the run's own clean checkouts,
        so before the tests run: a file named by an injection that git reports modified is restored from git, and a
        test file of the manifest that git reports untracked (a copy from the killed pass) is removed. A test that
        stage 8 placed and a developer then edited is left alone."""
        inj_files = {s(i.get("file")) for t in items(manifest.get("tests")) for i in items(t.get("injections")) if i.get("file")}
        test_files = {s(t.get("relativePath")) for t in items(manifest.get("tests"))} - edited_by_developer
        for sd in sides:
            st = run_native(git, ["-C", sd["worktree"], "status", "--porcelain=v1", "--untracked-files=all"])
            for line in st.output:
                code, path = line[:2], line[3:].strip().strip('"')
                if code.strip() == "M" and path in inj_files:
                    r = run_native(git, ["-C", sd["worktree"], "checkout", "--", path])
                    log(f"tests: repaired {sd['side']} {path}: an injected regression from an interrupted pass was restored "
                        f"from git (exit {r.exit_code})")
                elif code == "??" and path in test_files:
                    os.remove(os.path.join(sd["worktree"], native_path(path)))
                    log(f"tests: repaired {sd['side']}: removed {path} left by an interrupted pass")

    def test_fingerprint(t: dict) -> str:
        """Identity of one proposed test: its manifest entry and its file. Within a run the source of both branches
        does not change, so a test with the same fingerprint gives the same results."""
        src = os.path.join(agent_tests_dir, native_path(t.get("relativePath")))
        file_hash = file_sha256(src) if os.path.isfile(src) else "missing"
        return hashlib.sha256((json.dumps(t, sort_keys=True) + file_hash).encode("utf-8")).hexdigest().upper()

    def run_test_classes(sd: dict, class_names, label: str) -> dict:
        """Runs the named test classes on one worktree; returns per-class results from the surefire XML."""
        wt = sd["worktree"]
        started = time.time()
        log(f"tests: {label}: running {len(class_names)} class(es) on {sd['side']}")
        test_log = os.path.join(logs_dir, f"tests-{sd['side']}-{datetime.datetime.now():%H%M%S%f}.log")
        args = ["-B", "-ntp"] + list(MAVEN_NATIVE_GIT_ARGS) + (["-s", settings] if settings else []) + [
            "-f", os.path.join(wt, "pom.xml"), "test", f"-Dtest={','.join(class_names)}", "-Dsurefire.failIfNoSpecifiedTests=false"]
        r = run_native(mvn, args, env=jdk_env(sd["jdk"]))
        write_text(test_log, "\n".join(r.output) + "\n")
        reports_dir = os.path.join(wt, "target", "surefire-reports")
        out = {}
        for c in class_names:
            # Surefire names the report with the package-qualified class: TEST-com.example.orders.Foo.xml
            found = [n for n in ps_sorted(os.listdir(reports_dir)) if n.startswith("TEST-") and n.endswith(f".{c}.xml")] \
                if os.path.isdir(reports_dir) else []
            xml_path = os.path.join(reports_dir, found[0] if found else f"TEST-{c}.xml")
            if not os.path.isfile(xml_path):
                out[c] = {"ran": False, "passed": False, "detail": f"no surefire report (mvn exit {r.exit_code}); see {test_log}"}
                continue
            suite = ET.parse(xml_path).getroot()
            failed = to_int(suite.get("failures")) + to_int(suite.get("errors"))
            tests_run = to_int(suite.get("tests"))
            if failed > 0:
                detail = ", ".join(s(tc.get("name")) for tc in suite.findall("testcase")
                                   if tc.find("failure") is not None or tc.find("error") is not None)
            else:
                detail = f"{tests_run} test(s) passed"
            out[c] = {"ran": True, "passed": failed == 0 and tests_run > 0, "detail": detail, "log": test_log}
        summary = ", ".join(f"{c} {'pass' if v['passed'] else ('FAIL' if v['ran'] else 'not run')}" for c, v in out.items())
        log(f"tests: {label}: done in {int(time.time() - started)} s ({summary})")
        return out

    if os.path.isfile(manifest_path):
        proposal = agent_proposal(run_dir, "tests", "tests-manifest.json")
        manifest = proposal["doc"] or {}
        if proposal["state"] != "used":
            test_state = "stale"
            log("tests: the test writer's output is stale (findings or reach changed). Not run.")
        else:
            test_state = "run"
            placed = []
            try:
                repair_worktrees(manifest)
                # A repair round changes only some tests. A test whose fingerprint matches its result from the earlier
                # pass keeps that result; only the changed tests run again (with all their injections).
                previous = {p.get("id"): p for p in items(get(art("behavior-results.json"), "tests"))}
                gaps_by_id = {g.get("id"): g for g in items(get(art("test-gaps.json"), "gaps"))}
                prints = {t.get("id"): test_fingerprint(t) for t in items(manifest.get("tests"))}

                def reusable(t):
                    p = previous.get(t.get("id"))
                    return bool(p) and p.get("fingerprint") == prints[t.get("id")] and "ran" in (p.get("base") or {}) \
                        and "ran" in (p.get("head") or {})

                rerun_classes = {t.get("className") for t in items(manifest.get("tests")) if not reusable(t)}
                to_run = [t for t in items(manifest.get("tests")) if t.get("className") in rerun_classes]
                reused = [t for t in items(manifest.get("tests")) if t.get("className") not in rerun_classes]
                total_injections = sum(len(items(t.get("injections"))) for t in to_run)
                log(f"tests: {len(to_run)} test(s) to run with {total_injections} injection(s); {len(reused)} unchanged since the "
                    f"earlier pass, results kept: {', '.join(s(t.get('id')) for t in reused) or 'none'}")
                for t in reused:
                    row = dict(previous[t.get("id")], reused=True)
                    row["cases"] = case_results(t, row, gaps_by_id)
                    test_rows.append(row)
                    h = row["head"]
                    checks.extend(case_checks(t.get("id"), t.get("linkedRules"), row["cases"]))
                    if not h["ran"]:
                        blocking_errors.append(f"test {t.get('id')} did not run on head: {h['detail']}")

                # Copy the proposed test files into both worktrees. A path that already exists is a conflict.
                # Several tests can share one class file: copy each file once per side.
                for rel in unique_sorted([t.get("relativePath") for t in to_run]):
                    src = os.path.join(agent_tests_dir, native_path(rel))
                    if not os.path.isfile(src):
                        raise RuntimeError(f"proposed test file missing: {src}")
                    for sd in sides:
                        dst = os.path.join(sd["worktree"], native_path(rel))
                        if os.path.exists(dst):
                            raise RuntimeError(f"{sd['side']}: {rel} already exists in the worktree")
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copyfile(src, dst)
                        placed.append(dst)
                classes = unique_sorted([t.get("className") for t in to_run])
                base_side, head_side = sides
                base_res = run_test_classes(base_side, classes, "master") if classes else {}
                head_res = run_test_classes(head_side, classes, "change_branch") if classes else {}

                done_injections = 0
                for t in to_run:
                    injections = []
                    for inj in items(t.get("injections")):
                        done_injections += 1
                        target = os.path.join(head_side["worktree"], native_path(inj.get("file")))
                        if not os.path.isfile(target):
                            injections.append({"note": inj.get("note"), "applied": False, "detected": False,
                                               "detail": f"file not found in the head worktree: {inj.get('file')}"})
                            continue
                        # Keep the exact bytes (BOM, line endings) so the restore is byte for byte.
                        with open(target, "rb") as f:
                            original_bytes = f.read()
                        original = original_bytes.decode("utf-8-sig", errors="replace")
                        find, replace = s(inj.get("find")), s(inj.get("replace"))
                        occurrences = original.count(find) if find else 0
                        if occurrences != 1:
                            injections.append({"note": inj.get("note"), "applied": False, "detected": False,
                                               "detail": f"find text occurs {occurrences} time(s), needs exactly 1"})
                            continue
                        original_hash = file_sha256(target)
                        try:
                            with open(target, "wb") as f:
                                f.write(original.replace(find, replace).encode("utf-8"))
                            label = f"injection {done_injections}/{total_injections} {t.get('id')} ({s(inj.get('note'))[:60]})"
                            res = run_test_classes(head_side, [t.get("className")], label)[t.get("className")]
                            injections.append({"note": inj.get("note"), "applied": True, "detected": res["ran"] and not res["passed"],
                                               "detail": res["detail"]})
                        finally:
                            with open(target, "wb") as f:
                                f.write(original_bytes)
                            if file_sha256(target) != original_hash:
                                raise RuntimeError(f"could not restore {inj.get('file')} after injection")

                    b, h = base_res[t.get("className")], head_res[t.get("className")]
                    all_detected = bool(injections) and all(i["applied"] and i["detected"] for i in injections)
                    test_rows.append({
                        "id": t.get("id"), "className": t.get("className"), "relativePath": t.get("relativePath"),
                        "purpose": s(t.get("purpose")), "gap": s(t.get("gap")), "linkedRules": as_list(t.get("linkedRules")),
                        "flowIds": as_list(t.get("flowIds")), "methods": as_list(t.get("methods")),
                        "base": {"ran": b["ran"], "passed": b["passed"], "detail": b["detail"]},
                        "head": {"ran": h["ran"], "passed": h["passed"], "detail": h["detail"]},
                        "injections": injections,
                        "fingerprint": prints[t.get("id")],
                        "reused": False,
                        # Validated: passes on master, passes unchanged on change_branch, and fails for every injected
                        # regression (at least one injection is required; a test that cannot detect a regression proves nothing).
                        "detectsInjected": all_detected,
                        "validated": b["passed"] and h["passed"] and all_detected,
                    })
                    # One check per test method (case): it speaks only for the findings of the gaps that method
                    # covers, so one failing method does not confirm every finding the class is linked to. A difference
                    # needs master to pass and change_branch to fail; failing on master is "unverified", not a change.
                    test_rows[-1]["cases"] = case_results(t, test_rows[-1], gaps_by_id)
                    checks.extend(case_checks(t.get("id"), t.get("linkedRules"), test_rows[-1]["cases"]))
                    if not h["ran"]:
                        blocking_errors.append(f"test {t.get('id')} did not run on head: {h['detail']}")
            except Exception as e:  # noqa: BLE001 - a failed test run is a blocking error, not a crash
                log(f"tests: {e}")
                blocking_errors.append(f"test run: {e}")
            finally:
                for path in placed:
                    if os.path.isfile(path):
                        os.remove(path)
        order = [t.get("id") for t in items(manifest.get("tests"))]
        test_rows.sort(key=lambda r: order.index(r.get("id")) if r.get("id") in order else len(order))
    else:
        log("tests: no proposed tests (artifacts\\agent\\tests-manifest.json missing)")

    # ---------- 5. gaps ----------

    changed_main = len([f for f in items(get(inventory, "files")) if f.get("category") == "main-code"])
    gaps = [{"stage": "behavior", "item": "Integration tests with Testcontainers",
             "reason": "Not run. Docker is not used on this machine. The approach (section 6) leaves the Testcontainers method "
                       "open, to be decided.", "blocking": False}]
    if changed_main > 0:
        gaps.append({"stage": "behavior", "item": "Mutation testing on changed classes",
                     "reason": f"Not run. {changed_main} changed main file(s) exist, but the PIT step is not wired yet.", "blocking": False})
    lib_diff = art("library-api-diff.json")
    if not lib_diff or not lib_diff.get("available"):
        why = lib_diff.get("reason") if lib_diff else "stage 3 did not write library-api-diff.json"
        gaps.append({"stage": "dependencies", "item": "Library API diff", "reason": f"Not available: {why}.", "blocking": False})
    dep_doc = art("dependency-diff.json")
    if dep_doc and not dep_doc.get("verboseTree"):
        gaps.append({"stage": "dependencies", "item": "Dependency version conflicts",
                     "reason": "Not listed: the maven-dependency-plugin of this build cannot print a verbose tree.", "blocking": False})
    builds_doc = art("build-results.json")
    for side in ("base", "head"):
        if get(builds_doc, "sides", side, "integrationTests", "mode") == "not-run":
            gaps.append({"stage": "build", "item": f"Integration tests on {side}",
                         "reason": "Not run: the pom binds no maven-failsafe-plugin and build.integrationTests is failsafe-only.",
                         "blocking": False})

    # ---------- write ----------

    summary = {"total": len(probe_rows), **{o: len([r for r in probe_rows if r["outcome"] == o]) for o in ("same", "different", "error")}}
    write_json(os.path.join(artifacts, "behavior-results.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id, "servers": server_results, "startupOk": startup_ok,
        "dumps": dump_diff, "probes": probe_rows, "probeSummary": summary, "testState": test_state, "tests": test_rows,
        "checks": checks, "blockingErrors": blocking_errors,
    })
    write_json(os.path.join(artifacts, "gaps.json"), {"schemaVersion": 1, "runId": run_id, "gaps": gaps})

    log(f"probes: {summary['same']} same, {summary['different']} different, {summary['error']} error. Tests: state {test_state}, "
        f"{len(test_rows)} proposed.")
    for row in probe_rows:
        if row["outcome"] != "same":
            log(f"  {row['outcome']} {row['id']}: {'; '.join(row['differences'])} [{','.join(s(x) for x in row['linkedRules'])}]")
    for g in gaps:
        log(f"gap: {g['item']}: {g['reason']}")

    set_stage_step(status, "behavior", "done" if startup_ok else "done-with-errors", "", os.path.join(artifacts, "behavior-results.json"))
    set_run_state(status, "phase5-done", "phase5")
    write_json(status_file, status)
    log(f"phase 5 done: behavior results written for run {run_id}")
    return 0


def restore_placed_tests(taken_out: dict) -> None:
    """Standalone mode: puts back, byte for byte, the tests stage 8 placed in the head worktree, which this stage
    took out for its own run. A file that exists again (a developer recreated it) is left as it is."""
    for dst, data in taken_out.items():
        if os.path.exists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(data)
        print(f"tests: put back {dst} (placed by stage 8)")


if __name__ == "__main__":
    run_main(main)

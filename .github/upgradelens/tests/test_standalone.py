"""Standalone mode of the stage scripts (--standalone, lib\\standalone.py): a stage runs on any run, in any state,
and writes only to runs\\<id>\\standalone-stages\\. Needs git (a small repository is made in a temporary folder);
no Maven, server or agent."""

import json
import os
import subprocess
import sys

import pytest

from lib.core import write_json
from lib.standalone import MIRROR, StagePaths, select_run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = "src/main/java/a/Greeter.java"


def script(name):
    return os.path.join(ROOT, "scripts", name)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_file(repo, path, text, message):
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def snapshot(run):
    """Every file of the run outside the mirror, with its bytes."""
    out = {}
    for d, _, files in os.walk(run):
        if MIRROR in os.path.relpath(d, run).split(os.sep):
            continue
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, run)] = open(p, "rb").read()
    return out


@pytest.fixture
def finished_run(tmp_path):
    """A finished, orchestrated run (verdict written, ledger present) whose clone has one changed method, and
    minimal outputs of stages 4 and 5."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    base = commit_file(repo, JAVA, "package a;\npublic class Greeter {\n    public String hi() {\n        return \"hi\";\n    }\n}\n", "base")
    git(repo, "checkout", "-q", "-b", "change_branch")
    head = commit_file(repo, JAVA, "package a;\npublic class Greeter {\n    public String hi() {\n        return \"hello\";\n    }\n}\n", "change")

    write_json(str(tmp_path / "cfg.json"), {"runsFolder": str(tmp_path / "runs")})
    run = tmp_path / "runs" / "r1"
    write_json(str(run / "status.json"), {"state": "verdict-blocked", "phase": "phase10", "steps": []})
    write_json(str(run / "verdict.json"), {"verdict": "blocked"})
    write_json(str(run / "config" / "upgrade-lens.json"), {"baseBranch": "master", "headBranch": "change_branch"})
    write_json(str(run / "artifacts" / "environment.json"),
               {"repo": str(repo), "branches": {"base": {"commit": base}, "head": {"commit": head}, "mergeBase": base}})
    write_json(str(run / "artifacts" / "impact-findings.json"), {"findings": []})
    write_json(str(run / "artifacts" / "reach.json"), {"flows": [{"id": "GET /hi", "kind": "http", "untraced": []}]})
    write_json(str(run / "artifacts" / "behavior-results.json"), {"checks": [], "probes": [], "tests": []})
    write_json(str(run / "orchestration" / "ledger.json"), {"runId": "r1", "entries": []})
    return tmp_path, run


def run_stage(tmp_path, name, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("UPGRADELENS_CONFIG", "UPGRADELENS_INVOKER")}
    p = subprocess.run([sys.executable, script(name), "--config", str(tmp_path / "cfg.json"), *args],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    return p.returncode, p.stdout + p.stderr


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


# ---------- lib\standalone.py ----------

def test_paths_write_to_the_mirror_and_read_the_mirror_first(tmp_path):
    write_json(str(tmp_path / "artifacts" / "a.json"), {"from": "run"})
    write_json(str(tmp_path / "artifacts" / "b.json"), {"from": "run"})
    write_json(str(tmp_path / MIRROR / "artifacts" / "b.json"), {"from": "mirror"})
    p = StagePaths(str(tmp_path), True)
    assert p.out("artifacts", "x.json") == str(tmp_path / MIRROR / "artifacts" / "x.json")
    assert p.read("artifacts", "a.json") == {"from": "run"}
    assert p.read("artifacts", "b.json") == {"from": "mirror"}


def test_paths_without_standalone_are_the_run_folder(tmp_path):
    write_json(str(tmp_path / MIRROR / "artifacts" / "b.json"), {"from": "mirror"})
    p = StagePaths(str(tmp_path), False)
    assert p.out("artifacts", "b.json") == str(tmp_path / "artifacts" / "b.json")
    assert p.inp("artifacts", "b.json") == str(tmp_path / "artifacts" / "b.json")


def test_select_run_needs_a_run_id_and_refuses_while_a_stage_runs(tmp_path, capsys):
    runs = tmp_path / "runs"
    write_json(str(runs / "r1" / "status.json"), {"state": "verdict-safe"})
    assert select_run(str(runs), None, True) is None
    assert select_run(str(runs), "nope", True) is None
    assert select_run(str(runs), "r1", True) == "r1"
    write_json(str(runs / "r1" / "ul.lock.json"), {"pid": os.getpid(), "key": "04-tests"})
    assert select_run(str(runs), "r1", True) is None
    assert "04-tests" in capsys.readouterr().out


# ---------- the stages ----------

def test_stage_2_runs_on_a_finished_run_and_leaves_it_unchanged(finished_run):
    tmp_path, run = finished_run
    before = snapshot(run)

    code, out = run_stage(tmp_path, "phase2-changes.py", "--run-id", "r1", "--standalone")

    assert code == 0, out
    assert snapshot(run) == before
    inv = read(run / MIRROR / "artifacts" / "change-inventory.json")
    assert inv["summary"]["files"] == 1
    assert inv["files"][0]["path"] == JAVA
    assert inv["files"][0]["changedMethods"] == ["hi"]
    assert (run / MIRROR / "reports" / "change-inventory.md").is_file()
    assert (run / MIRROR / "logs" / "phase2.log").is_file()
    # The mirror's status records the standalone stage; the run's own status.json is unchanged (snapshot above).
    assert read(run / MIRROR / "status.json")["state"] == "phase2-done"
    assert read(run / "status.json")["state"] == "verdict-blocked"


def test_stage_6_reads_the_mirror_first(finished_run):
    tmp_path, run = finished_run
    # An earlier standalone stage 4 found a second flow; stage 6 must use that reach, not the run's.
    write_json(str(run / MIRROR / "artifacts" / "reach.json"),
               {"flows": [{"id": "GET /hi", "untraced": []}, {"id": "GET /bye", "untraced": ["method:bye"]}]})
    before = snapshot(run)

    code, out = run_stage(tmp_path, "phase6-flows.py", "--run-id", "r1", "--standalone")

    assert code == 0, out
    assert snapshot(run) == before
    flows = {f["id"]: f["scope"] for f in read(run / MIRROR / "artifacts" / "flows.json")["flows"]}
    assert flows == {"GET /hi": "smoke", "GET /bye": "full"}


def test_standalone_needs_a_run_id(finished_run):
    tmp_path, _ = finished_run
    code, out = run_stage(tmp_path, "phase6-flows.py", "--standalone")
    assert code == 2
    assert "--run-id" in out


def test_stage_5_puts_back_the_tests_stage_8_placed(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("phase5", script("phase5-behavior.py"))
    phase5 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phase5)

    kept = tmp_path / "wt" / "src" / "test" / "KeptTest.java"
    recreated = tmp_path / "wt" / "src" / "test" / "RecreatedTest.java"
    recreated.parent.mkdir(parents=True)
    recreated.write_bytes(b"developer's own")
    phase5.restore_placed_tests({str(kept): b"placed by stage 8", str(recreated): b"placed by stage 8"})
    assert kept.read_bytes() == b"placed by stage 8"
    assert recreated.read_bytes() == b"developer's own"


def test_stages_2_and_6_to_10_chain_in_the_mirror_and_leave_the_run_unchanged(finished_run):
    tmp_path, run = finished_run
    before = snapshot(run)

    for name, args in (("phase2-changes.py", ()), ("phase6-flows.py", ()), ("phase7-manual.py", ()),
                       ("phase8-output.py", ()), ("phase9-review.py", ()), ("phase10-verdict.py", ())):
        code, out = run_stage(tmp_path, name, "--run-id", "r1", "--standalone", *args)
        assert code in (0, 1), f"{name} exit {code}: {out}"

    assert snapshot(run) == before
    mirror = run / MIRROR
    for rel in ("artifacts/flows.json", "artifacts/manual-items.json", "review/review-record.json", "verdict.json",
                "reports/report.md", "reports/summary.md"):
        assert (mirror / rel).is_file(), rel
    assert not (mirror / "artifacts" / "new-tests.json").exists()  # stage 8 places no tests in standalone mode
    assert read(mirror / "status.json")["state"].startswith("verdict-")



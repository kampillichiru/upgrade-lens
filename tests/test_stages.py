"""The stage definitions (scripts\\lib\\stages.py) and diagnose.py. No Maven, server, git or agent is needed."""

import json
import os
import subprocess
import sys

import pytest

from lib.agents import AGENTS
from lib.core import write_json
from lib.plan import run_plan
from lib.stages import STAGES, exit_meaning, missing_message, missing_needs, stage_logs, written_by

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIAGNOSE = os.path.join(ROOT, "scripts", "diagnose.py")


# ---------- the definitions are complete and consistent ----------

def test_every_stage_script_exists():
    assert sorted(STAGES) == list(range(11))
    for spec in STAGES.values():
        assert os.path.isfile(os.path.join(ROOT, "scripts", spec.script)), spec.script


def test_every_stage_input_is_written_by_an_earlier_stage():
    for spec in STAGES.values():
        for need in spec.needs:
            writers = [o.stage for o in STAGES.values() if need in o.writes]
            assert writers and min(writers) < spec.stage, f"stage {spec.stage} needs {need}, written by {writers}"


def test_every_agent_input_has_a_known_writer():
    for spec in AGENTS.values():
        for name in list(spec.reads) + list(spec.repair_reads):
            assert written_by(f"artifacts\\{name}") != "unknown", f"{spec.step} reads {name}"


def test_the_plan_runs_only_defined_stages():
    assert {x["stage"] for x in run_plan(2) if x["kind"] == "stage"} <= set(STAGES)


def test_exit_codes_have_stage_specific_meanings():
    assert exit_meaning(4, 1).startswith("a build or a test failed")
    assert exit_meaning(1, 10).startswith("verdict blocked")
    assert exit_meaning(3221225786).startswith("killed")
    assert exit_meaning(99) == ""


def test_missing_inputs_name_the_stage_that_writes_them(tmp_path):
    write_json(str(tmp_path / "status.json"), {})
    assert set(missing_needs(str(tmp_path), 4)) == {"config\\upgrade-lens.json", "artifacts\\environment.json",
                                                   "artifacts\\change-inventory.json", "artifacts\\dependency-diff.json",
                                                   "artifacts\\api-diff.json"}
    msg = missing_message(str(tmp_path), 4)
    assert "artifacts\\change-inventory.json  (written by stage 2" in msg
    assert missing_message(str(tmp_path), 0) == ""


def test_stage_logs_show_the_newest_per_branch_logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("build-main.log", "build-change.log", "phase1.log"):
        (logs / name).write_text("x", encoding="utf-8")
    found = [os.path.basename(p) for p in stage_logs(str(tmp_path), 1)]
    assert set(found[:2]) == {"build-main.log", "build-change.log"} and found[2] == "phase1.log"


# ---------- diagnose.py ----------

@pytest.fixture
def runs(tmp_path):
    write_json(str(tmp_path / "cfg.json"), {"runsFolder": str(tmp_path / "runs")})
    return tmp_path


def make_run(runs, status, ledger=None):
    d = runs / "runs" / "r1"
    write_json(str(d / "status.json"), status)
    write_json(str(d / "config" / "upgrade-lens.json"), {})
    write_json(str(d / "artifacts" / "environment.json"), {})
    if ledger is not None:
        write_json(str(d / "orchestration" / "ledger.json"), {"runId": "r1", "entries": ledger})
    return d


def diagnose(runs, *args):
    p = subprocess.run([sys.executable, DIAGNOSE, "--run-id", "r1", "--config", str(runs / "cfg.json"), *args],
                       capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout


def done(key):
    return {"key": key, "kind": "stage", "attempt": 1, "state": "done", "exitCode": 0, "reason": ""}


def test_diagnose_points_at_the_failed_build_and_its_log(runs):
    d = make_run(runs, {"state": "phase1-done", "phase": "phase1"},
                 [done("stage-0"), {"key": "stage-1", "kind": "stage", "attempt": 1, "state": "failed", "exitCode": 4,
                                    "reason": "build-main: tests failed"}])
    (d / "logs").mkdir()
    (d / "logs" / "build-main.log").write_text("[ERROR] Tests run: 3, Failures: 1\nBUILD FAILURE\n", encoding="utf-8")
    code, out = diagnose(runs)
    assert code == 1
    assert "FAIL  stage-1" in out and "a build or a test failed on one branch" in out
    assert "BUILD FAILURE" in out and "build-main.log" in out
    assert "start a new run" in out


def test_diagnose_names_missing_inputs_and_their_writer(runs):
    make_run(runs, {"state": "phase3-done", "phase": "phase3"},
             [done(k) for k in ("stage-0", "stage-1", "stage-2", "stage-3")] +
             [{"key": "agent-release-notes", "kind": "agent", "attempt": 1, "state": "skipped", "reason": "none"},
              {"key": "stage-4", "kind": "stage", "attempt": 1, "state": "failed", "exitCode": 2,
               "reason": "missing input: artifacts\\api-diff.json"}])
    code, out = diagnose(runs, "--json")
    d = json.loads(out)
    assert code == 1 and d["problem"]["key"] == "stage-4"
    missing = {x["file"]: x["writtenBy"] for x in d["problem"]["inputs"] if not x["present"]}
    assert missing["artifacts\\api-diff.json"].startswith("stage 3")


def test_diagnose_shows_an_agent_steps_check_errors(runs):
    d = make_run(runs, {"state": "phase3-done", "phase": "phase3"}, [])
    err = d / "orchestration" / "agent-release-notes-1.errors.json"
    write_json(str(err), {"errors": ["no item for com.example:lib"]})
    write_json(str(d / "orchestration" / "ledger.json"), {"runId": "r1", "entries":
               [done(k) for k in ("stage-0", "stage-1", "stage-2", "stage-3")] +
               [{"key": "agent-release-notes", "kind": "agent", "attempt": 1, "state": "failed",
                 "reason": "checks: 1 problem(s)", "errorsFile": str(err)}]})
    code, out = diagnose(runs)
    assert code == 1
    assert "no item for com.example:lib" in out and "run-agent.py --run-id r1 --step release-notes" in out


def test_diagnose_reports_interrupted_retries_of_a_finished_run(runs):
    make_run(runs, {"state": "verdict-safe", "phase": "phase10"},
             [done("stage-0"), {"key": "stage-1", "kind": "stage", "attempt": 1, "state": "failed", "exitCode": 130,
                                "reason": "exit 130"}, done("stage-1")])
    code, out = diagnose(runs)
    assert "2 attempts; 1 interrupted and retried" in out


def test_diagnose_a_scripts_only_run_that_failed(runs):
    make_run(runs, {"state": "failed-tool", "phase": "phase4",
                    "steps": [{"name": "impact", "state": "failed", "reason": "No head coverage report"}]})
    code, out = diagnose(runs)
    assert code == 1 and "stage-4" in out and "No head coverage report" in out
    assert "run-pipeline.py --scripts-only --run-id r1 --from 4" in out


def test_diagnose_changes_nothing(runs):
    d = make_run(runs, {"state": "phase1-done", "phase": "phase1"}, [done("stage-0")])
    write_json(str(d / "orchestration" / "lock.json"), {"pid": 999999, "key": "stage-1"})
    before = sorted(os.path.relpath(os.path.join(b, f), d) for b, _x, fs in os.walk(d) for f in fs)
    code, out = diagnose(runs)
    after = sorted(os.path.relpath(os.path.join(b, f), d) for b, _x, fs in os.walk(d) for f in fs)
    assert before == after and "Stale lock" in out and code == 1


def test_run_stage_refuses_a_stage_whose_inputs_are_missing_and_next_step_stops(runs):
    make_run(runs, {"state": "phase3-done", "phase": "phase3"},
             [done(k) for k in ("stage-0", "stage-1", "stage-2", "stage-3")] +
             [{"key": "agent-release-notes", "kind": "agent", "attempt": 1, "state": "skipped", "reason": "none"}])
    cfg = str(runs / "cfg.json")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "run-stage.py"), "--run-id", "r1", "--key", "stage-4",
                        "--config", cfg], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 2 and "cannot start" in p.stdout and "change-inventory.json  (written by stage 2" in p.stdout
    assert not os.path.isfile(runs / "runs" / "r1" / "orchestration" / "lock.json")
    n = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "next-step.py"), "--run-id", "r1", "--config", cfg],
                       capture_output=True, text=True, encoding="utf-8")
    assert n.returncode == 1 and json.loads(n.stdout)["action"] == "stop"
    assert "missing input" in json.loads(n.stdout)["reason"]

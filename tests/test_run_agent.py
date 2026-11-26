"""One agent on its own (scripts\\run-agent.py, lib\\agents.py): availability, the sandboxed session, the guard,
the content check, and applying an accepted answer. No Maven, server, git or real agent is needed: the test
plays the agent by writing its answer file."""

import json
import os
import subprocess
import sys

import pytest

from lib.agents import AGENTS, AgentRun, agent_proposal, missing_inputs
from lib.core import write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "run-agent.py")


@pytest.fixture
def run(tmp_path):
    """A scripts-only run with the stage 3 artifacts: enough for the release-notes agent only."""
    runs = tmp_path / "runs"
    run_dir = runs / "r1"
    write_json(str(tmp_path / "cfg.json"), {"runsFolder": str(runs)})
    write_json(str(run_dir / "status.json"), {"state": "done"})
    write_json(str(run_dir / "config" / "upgrade-lens.json"), {})
    write_json(str(run_dir / "artifacts" / "dependency-diff.json"),
               {"changed": [{"ga": "com.example:lib", "direct": True, "major": True}]})
    write_json(str(run_dir / "artifacts" / "library-api-diff.json"), {"available": False, "items": []})
    return {"cfg": str(tmp_path / "cfg.json"), "dir": str(run_dir)}


def agent(run, *args):
    p = subprocess.run([sys.executable, SCRIPT, "--run-id", "r1", "--config", run["cfg"], *args],
                       capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout


def begin(run, step, *extra):
    code, out = agent(run, "--step", step, "--action", "begin", *extra)
    assert code == 0, out
    return json.loads(out)


def end(run, b):
    return agent(run, "--action", "end", "--session", b["session"], "--guard-sha256", b["endCommand"].split("--guard-sha256 ")[1].split()[0])


def notes_answer(b, **over):
    doc = {"agent": "upgradelens-release-notes", "inputHash": b["inputHash"],
           "items": [{"ref": "com.example:lib", "result": "not-found", "quote": "", "impact": "no notes found"}]}
    doc.update(over)
    write_json(os.path.join(b["outputFolder"], "release-notes.json"), doc)


def test_every_agent_has_one_complete_definition():
    for step, spec in AGENTS.items():
        assert spec.step == step and spec.output in [a.replace("\\*", "") for a in spec.allowed] + [spec.output]
        assert spec.reads and callable(spec.task) and callable(spec.check) and callable(spec.nothing_to_do)
        assert 4 <= spec.feeds_stage <= 10


def test_list_shows_which_agents_have_their_data(run):
    code, out = agent(run)
    assert code == 0
    states = {r["step"]: r for r in json.loads(out)["agents"]}
    assert states["release-notes"]["state"] == "ready" and "beginCommand" in states["release-notes"]
    assert states["analyst"]["state"] == "missing"
    by = {m["artifact"]: m["writtenBy"] for m in states["analyst"]["missing"]}
    assert by["change-inventory.json"].startswith("stage 2 ")
    assert states["reviewer"]["state"] == "missing"


def test_begin_refuses_an_agent_whose_data_is_missing(run):
    code, out = agent(run, "--step", "analyst", "--action", "begin")
    assert code == 2 and "change-inventory.json" in out and "stage 2" in out


def test_accepted_answer_is_applied_to_a_scripts_only_run(run):
    b = begin(run, "release-notes")
    assert b["outputFolder"].startswith(os.path.join(run["dir"], "standalone"))
    assert "outside the run's plan" in b["prompt"] and b["inputHash"] in b["prompt"]
    notes_answer(b)
    code, out = end(run, b)
    assert code == 0, out
    assert agent_proposal(run["dir"], "release-notes")["state"] == "not-run"  # nothing reaches the run before apply
    code, out = agent(run, "--action", "apply", "--session", b["session"])
    assert code == 0, out
    assert "--from 7" in out
    assert agent_proposal(run["dir"], "release-notes")["state"] == "used"


def test_a_write_outside_the_session_is_rejected(run):
    b = begin(run, "release-notes")
    notes_answer(b)
    write_json(os.path.join(run["dir"], "artifacts", "dependency-diff.json"), {"changed": []})
    code, out = end(run, b)
    assert code == 5 and "dependency-diff.json" in out
    code, _ = agent(run, "--action", "apply", "--session", b["session"])
    assert code == 2


def test_a_file_the_agent_may_not_write_in_its_folder_is_rejected(run):
    b = begin(run, "release-notes")
    notes_answer(b)
    write_json(os.path.join(b["outputFolder"], "hypotheses.json"), {})
    code, out = end(run, b)
    assert code == 5 and "hypotheses.json" in out


def test_a_failed_check_is_reported_and_not_applied(run):
    b = begin(run, "release-notes")
    notes_answer(b, inputHash="wrong", items=[])
    code, out = end(run, b)
    assert code == 6 and "no item for com.example:lib" in out and "inputHash" in out
    code, _ = agent(run, "--action", "apply", "--session", b["session"])
    assert code == 2


def test_a_tampered_guard_record_is_rejected(run):
    b = begin(run, "release-notes")
    notes_answer(b)
    code, out = agent(run, "--action", "end", "--session", b["session"], "--guard-sha256", "0" * 64)
    assert code == 5


def test_an_orchestrated_run_keeps_its_agent_files(run):
    write_json(os.path.join(run["dir"], "orchestration", "ledger.json"), {"runId": "r1", "entries": []})
    b = begin(run, "release-notes")
    notes_answer(b)
    code, out = end(run, b)
    assert code == 0 and "orchestrated" in out
    code, out = agent(run, "--action", "apply", "--session", b["session"])
    assert code == 2
    assert agent_proposal(run["dir"], "release-notes")["state"] == "not-run"


def test_apply_refuses_an_answer_whose_inputs_changed(run):
    b = begin(run, "release-notes")
    notes_answer(b)
    assert end(run, b)[0] == 0
    write_json(os.path.join(run["dir"], "artifacts", "library-api-diff.json"), {"available": False, "items": [], "x": 1})
    code, out = agent(run, "--action", "apply", "--session", b["session"])
    assert code == 2 and "changed" in out


def test_the_test_writer_gets_its_work_list_in_a_scripts_only_run(run):
    a = os.path.join(run["dir"], "artifacts")
    write_json(os.path.join(a, "changed-methods.json"),
               {"methods": [{"file": "src/main/java/A.java", "kind": "method", "name": "m", "tested": False}]})
    write_json(os.path.join(a, "reach.json"), {"flows": []})
    write_json(os.path.join(a, "impact-findings.json"), {"findings": []})
    write_json(os.path.join(a, "behavior-results.json"), {"tests": [], "checks": []})
    write_json(os.path.join(a, "environment.json"), {"branches": {"head": {"worktree": ""}}})
    code, out = agent(run)
    assert {r["step"]: r["state"] for r in json.loads(out)["agents"]}["tests"] == "ready"
    b = begin(run, "tests")
    assert os.path.isfile(os.path.join(a, "test-gaps.json"))
    assert "G-01" not in b["prompt"] and "test-gaps.json" in b["prompt"]
    write_json(os.path.join(b["outputFolder"], "tests-manifest.json"),
               {"writer": "upgradelens-test-writer", "inputHash": b["inputHash"], "tests": [],
                "uncovered": [{"gapId": "G-01", "reason": "runtime only: needs the real database to show it"}]})
    code, out = end(run, b)
    assert code == 0, out


def test_repair_needs_the_earlier_tests_and_their_results(run):
    assert "behavior-results.json" in missing_inputs(run["dir"], "tests", repair=True)
    assert "agent\\tests-manifest.json" in missing_inputs(run["dir"], "tests", repair=True)


def test_reviewer_prompt_points_at_the_run_tests_but_writes_to_its_own_folder(tmp_path):
    r = AgentRun(step="reviewer", run_id="r1", run_dir=str(tmp_path), out_dir=str(tmp_path / "s" / "out"), standalone=True)
    from lib.agents import agent_prompt
    p = agent_prompt(r)
    assert os.path.join(str(tmp_path), "artifacts", "agent") in p
    assert os.path.join(str(tmp_path / "s" / "out"), "review-answer.json") in p

"""The ul.py runner (scripts\\runner\\): phases run on their own, agent jobs go begin -> answer -> check, the guardrails
reject what they must, every decision is logged, and a phase that ran is stale when its inputs change.
Needs git for the script-phase test; no Maven, server or real agent."""

import json
import os
import subprocess
import sys

import pytest

from lib.core import input_hash, write_json
from runner.engine import run_phase
from runner.log import redact
from runner.registry import PHASES, find_phase, producer, producer_phase
from runner.state import phase_state, read_status

TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UL_PY = os.path.join(TOOL, "ul.py")
JAVA = "src/main/java/a/Greeter.java"


# ---------- the registry ----------

def test_every_phase_input_is_written_by_an_earlier_phase_or_a_person():
    for i, p in enumerate(PHASES):
        for need in p.needs():
            src = producer_phase(need)
            assert src is not None, f"{p.id} needs {need}, which no phase writes"
            assert PHASES.index(src) < i, f"{p.id} needs {need}, written by the later phase {src.id}"


def test_no_phase_goes_stale_on_a_file_a_later_phase_writes():
    # 02-impact's rules read the analyst's answer when it is there; 03-analysis writes it. If it counted as an
    # input, 02-impact would go stale as soon as 03-analysis was done, and `continue` would loop between them.
    assert "artifacts\\agent\\hypotheses.json" in find_phase("impact").optional()
    for i, p in enumerate(PHASES):
        for rel in p.hashed_inputs():
            src = producer_phase(rel)
            assert src is None or PHASES.index(src) < i, f"{p.id} hashes {rel}, written by the later phase {src.id}"


def test_every_job_is_one_script_one_python_step_or_one_agent():
    for p in PHASES:
        for j in p.jobs:
            assert j.kind in ("script", "python", "agent")
            assert (j.stage is not None) == (j.kind == "script")
            assert (j.step is not None) == (j.kind == "agent")


def test_phases_are_found_by_name_folder_or_number():
    assert find_phase("impact").id == "02-impact"
    assert find_phase("02-impact").id == "02-impact"
    assert find_phase("2").id == find_phase("02").id == "02-impact"
    assert find_phase("nope") is None
    assert "02-impact" in producer("artifacts\\change-inventory.json")
    assert producer("review\\manual-decisions.json").startswith("a person")


def test_runs_go_to_the_workspace_not_into_github():
    from lib.core import WORKSPACE_DIR, runs_root
    assert runs_root({}) == os.path.join(WORKSPACE_DIR, "runs")
    assert ".github" not in os.path.relpath(runs_root({"runsFolder": "runs"}), WORKSPACE_DIR)
    for name in os.listdir(os.path.join(TOOL, "scripts")):
        if name.endswith(".py"):
            with open(os.path.join(TOOL, "scripts", name), encoding="utf-8") as f:
                assert "runs_root(cfg, ROOT_DIR)" not in f.read(), name


def test_secrets_are_masked_in_logs():
    text = redact('mvn -Dtoken=abc123 https://bob:s3cret@repo.example.com <password>hunter2</password> '
                  'Authorization: Bearer xyz.abc api_key: "k-1" ghp_' + "a" * 30)
    for secret in ("abc123", "s3cret", "hunter2", "xyz.abc", "k-1", "ghp_aaaa"):
        assert secret not in text
    assert "repo.example.com" in text and "-Dtoken=***" in text


# ---------- a run with an agent job ----------

@pytest.fixture
def review_run(tmp_path):
    """A run where 05-review can start: one validated test and its manifest."""
    run = tmp_path / "runs" / "r1"
    write_json(str(run / "status.json"), {"state": "phase5-done"})
    write_json(str(run / "config" / "upgrade-lens.json"), {"agentBudget": 5, "orchestration": {"maxAgentAttempts": 2}})
    write_json(str(run / "artifacts" / "environment.json"), {"branches": {}})
    write_json(str(run / "artifacts" / "behavior-results.json"), {"tests": [{"id": "T-01", "validated": True}]})
    write_json(str(run / "artifacts" / "agent" / "tests-manifest.json"),
               {"writer": "upgradelens-test-writer", "tests": [{"id": "T-01", "relativePath": "src/test/java/a/GreeterCharacterizationTest.java"}]})
    t = run / "artifacts" / "agent" / "tests" / "src" / "test" / "java" / "a" / "GreeterCharacterizationTest.java"
    t.parent.mkdir(parents=True)
    t.write_text("class GreeterCharacterizationTest {}", encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    write_json(str(cfg), {"runsFolder": str(tmp_path / "runs")})
    return str(cfg), str(tmp_path / "runs"), run


def answer_path(run, phase="05-review", job="review", name="review-answer.json"):
    return run / phase / job / "out" / name


def test_an_agent_job_waits_then_accepts_a_good_answer(review_run):
    cfg, runs, run = review_run
    review = find_phase("review")

    code, lines = run_phase(cfg, runs, "r1", review)
    assert code == 20, lines
    assert "WAITING" in lines[0]
    brief = (run / "05-review" / "review" / "brief.md").read_text(encoding="utf-8")
    assert "test-reviewer.md" in brief and "T-01" in brief and "Write only" in brief
    assert read_status(str(run), review)["state"] == "waiting"

    code, lines = run_phase(cfg, runs, "r1", review)   # no answer yet: still waiting, no new agent call
    assert code == 20
    assert len(json.loads((run / "artifacts" / "agent" / "usage.json").read_text(encoding="utf-8"))["calls"]) == 1

    write_json(str(answer_path(run)), {"reviews": [{"testId": "T-01", "verdict": "pass", "reviewer": "upgradelens-test-reviewer",
                                                   "notes": "pins base"}]})
    code, lines = run_phase(cfg, runs, "r1", review)
    assert code == 0, lines
    published = json.loads((run / "artifacts" / "agent" / "review.json").read_text(encoding="utf-8"))
    assert published["reviews"][0]["verdict"] == "pass" and published["reviews"][0]["fileSha256"]
    st = read_status(str(run), review)
    assert st["state"] == "done" and st["jobs"]["review"]["state"] == "done"
    assert (run / "05-review" / "quality.json").is_file()
    assert (run / "05-review" / "outputs" / "artifacts" / "agent" / "review.json").is_file()
    log = (run / "05-review" / "log.txt").read_text(encoding="utf-8")
    assert "WAIT for agent ul-05-review" in log and "ACCEPT attempt 1" in log
    timeline = (run / "run.log").read_text(encoding="utf-8")
    assert "05-review/review" in timeline


def test_an_answer_that_fails_its_checks_gets_a_new_brief_then_fails(review_run):
    cfg, runs, run = review_run
    review = find_phase("review")
    run_phase(cfg, runs, "r1", review)
    write_json(str(answer_path(run)), {"reviews": []})                       # misses T-01
    code, lines = run_phase(cfg, runs, "r1", review)
    assert code == 20, lines                                                  # retry with the problems
    brief = (run / "05-review" / "review" / "brief.md").read_text(encoding="utf-8")
    assert "attempt 2" in brief and "previous attempt was rejected" in brief and "T-01" in brief
    write_json(str(answer_path(run)), {"reviews": []})
    code, lines = run_phase(cfg, runs, "r1", review)
    assert code == 6, lines                                                   # no attempt left
    checks = json.loads((run / "05-review" / "review" / "checks.json").read_text(encoding="utf-8"))
    assert checks["attempt"] == 2 and checks["errors"]


def test_an_agent_that_writes_outside_its_folder_is_rejected(review_run):
    cfg, runs, run = review_run
    review = find_phase("review")
    run_phase(cfg, runs, "r1", review)
    write_json(str(answer_path(run)), {"reviews": [{"testId": "T-01", "verdict": "pass", "notes": "x"}]})
    write_json(str(run / "artifacts" / "behavior-results.json"), {"tests": [{"id": "T-01", "validated": True}], "x": 1})
    code, lines = run_phase(cfg, runs, "r1", review)
    assert code == 5, lines
    assert not (run / "artifacts" / "agent" / "review.json").exists()
    checks = json.loads((run / "05-review" / "review" / "checks.json").read_text(encoding="utf-8"))
    assert any("behavior-results.json" in g for g in checks["guard"])
    assert "REJECT" in (run / "05-review" / "log.txt").read_text(encoding="utf-8")


def test_the_agent_budget_stops_new_agent_calls(review_run):
    cfg, runs, run = review_run
    write_json(str(run / "artifacts" / "agent" / "usage.json"), {"calls": [{}] * 5})
    code, lines = run_phase(cfg, runs, "r1", find_phase("review"))
    assert code == 2
    assert "budget" in " ".join(lines)


def test_agents_off_skips_the_job_with_the_reason(review_run):
    cfg, runs, run = review_run
    code, lines = run_phase(cfg, runs, "r1", find_phase("review"), agents=False)
    assert code == 0, lines
    rec = read_status(str(run), find_phase("review"))["jobs"]["review"]
    assert rec["state"] == "skipped" and "agents are off" in rec["reason"]
    q = json.loads((run / "05-review" / "quality.json").read_text(encoding="utf-8"))
    assert q["status"] == "amber"


def test_a_rerun_keeps_the_previous_attempt_in_history_and_a_changed_input_makes_the_phase_stale(review_run):
    cfg, runs, run = review_run
    review = find_phase("review")
    run_phase(cfg, runs, "r1", review, agents=False)
    assert phase_state(str(run), review)["state"] == "done"
    write_json(str(run / "artifacts" / "behavior-results.json"), {"tests": [{"id": "T-01", "validated": True}], "new": 1})
    x = phase_state(str(run), review)
    assert x["state"] == "stale" and x["stale"] == ["artifacts\\behavior-results.json"]
    run_phase(cfg, runs, "r1", review, agents=False)
    assert (run / "05-review" / "history" / "1" / "status.json").is_file()
    assert phase_state(str(run), review)["state"] == "done"


def test_a_phase_whose_inputs_are_missing_says_which_phase_writes_them(tmp_path):
    run = tmp_path / "runs" / "r1"
    write_json(str(run / "status.json"), {})
    write_json(str(run / "config" / "upgrade-lens.json"), {})
    write_json(str(tmp_path / "cfg.json"), {"runsFolder": str(tmp_path / "runs")})
    code, lines = run_phase(str(tmp_path / "cfg.json"), str(tmp_path / "runs"), "r1", find_phase("impact"))
    assert code == 2
    log = (run / "02-impact" / "log.txt").read_text(encoding="utf-8")
    assert "missing input artifacts\\environment.json (written by 00-setup" in log
    assert phase_state(str(run), find_phase("impact"))["state"] == "failed"


def test_an_analyst_answer_for_other_inputs_is_rejected_and_a_good_one_is_reused_later(tmp_path):
    run = tmp_path / "runs" / "r1"
    write_json(str(run / "status.json"), {})
    write_json(str(run / "config" / "upgrade-lens.json"), {"agentBudget": 9})
    a = run / "artifacts"
    write_json(str(a / "environment.json"), {"branches": {}})
    write_json(str(a / "change-inventory.json"), {"files": [{"path": "A.java", "category": "main-code",
                                                              "symbols": [{"symbol": "method:hi"}]}]})
    for n in ("dependency-diff.json", "api-diff.json", "library-api-diff.json"):
        write_json(str(a / n), {})
    write_json(str(a / "impact-findings.json"), {"findings": []})
    write_json(str(tmp_path / "cfg.json"), {"runsFolder": str(tmp_path / "runs")})
    cfg, runs, analysis = str(tmp_path / "cfg.json"), str(tmp_path / "runs"), find_phase("analysis")

    code, _ = run_phase(cfg, runs, "r1", analysis, job="analyst")
    assert code == 20
    good_hash = input_hash([str(a / n) for n in ("change-inventory.json", "dependency-diff.json", "api-diff.json",
                                                   "library-api-diff.json")])
    answer = {"agent": "upgradelens-analyst", "inputHash": "WRONG", "hypotheses": [], "explanations": [],
              "changeReasons": [{"file": "A.java", "symbol": "method:hi", "reason": "not determined",
                                 "evidence": "no commit subject", "consequence": "greeting changes"}]}
    write_json(str(answer_path(run, "03-analysis", "analyst", "hypotheses.json")), answer)
    code, _ = run_phase(cfg, runs, "r1", analysis, job="analyst")
    assert code == 20                                                         # stale hash: retry
    answer["inputHash"] = good_hash
    write_json(str(answer_path(run, "03-analysis", "analyst", "hypotheses.json")), answer)
    code, lines = run_phase(cfg, runs, "r1", analysis, job="analyst")
    assert code == 0, lines
    assert (a / "agent" / "hypotheses.json").is_file()
    calls = len(json.loads((run / "artifacts" / "agent" / "usage.json").read_text(encoding="utf-8"))["calls"])

    code, _ = run_phase(cfg, runs, "r1", analysis, job="analyst")          # inputs unchanged: reused, no call
    assert code == 0
    assert len(json.loads((run / "artifacts" / "agent" / "usage.json").read_text(encoding="utf-8"))["calls"]) == calls
    assert "REUSE" in (run / "03-analysis" / "log.txt").read_text(encoding="utf-8")


# ---------- a script phase, through the CLI ----------

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def report_run(tmp_path):
    """A run with the outputs of 02 and 04 (no tests), so 06-report can run all its stages."""
    repo = tmp_path / "repo"
    (repo / "src/main/java/a").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "master")
    (repo / JAVA).write_text("package a;\npublic class Greeter {}\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    run = tmp_path / "runs" / "r1"
    write_json(str(run / "status.json"), {"state": "phase5-done", "steps": []})
    write_json(str(run / "config" / "upgrade-lens.json"), {"baseBranch": "master", "headBranch": "change_branch"})
    write_json(str(run / "artifacts" / "environment.json"),
               {"repo": str(repo), "branches": {"base": {"commit": base}, "head": {"commit": base}, "mergeBase": base}})
    write_json(str(run / "artifacts" / "impact-findings.json"), {"findings": []})
    write_json(str(run / "artifacts" / "reach.json"), {"flows": [{"id": "GET /hi", "kind": "http", "untraced": []}]})
    write_json(str(run / "artifacts" / "behavior-results.json"), {"checks": [], "probes": [], "tests": []})
    write_json(str(tmp_path / "cfg.json"), {"runsFolder": str(tmp_path / "runs")})
    return tmp_path, run


def ul(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("UPGRADELENS_CONFIG", "UPGRADELENS_INVOKER")}
    p = subprocess.run([sys.executable, UL_PY, *args, "--config", str(tmp_path / "cfg.json")],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    return p.returncode, p.stdout + p.stderr


@pytest.mark.parametrize("mode", ["--foreground", "background"])
def test_the_report_phase_runs_its_stage_scripts_and_logs_each_step(report_run, mode):
    tmp_path, run = report_run
    args = ["report", "--run", "r1"] + ([mode] if mode.startswith("--") else [])
    code, out = ul(tmp_path, *args)
    assert code in (0, 1), out                           # 1 = verdict blocked, a result
    assert out.splitlines()[0].startswith("06-report: done"), out
    assert len(out.splitlines()) <= 10, "the console summary stays short"
    st = read_status(str(run), find_phase("report"))
    assert [st["jobs"][j]["state"] for j in ("flows", "manual", "report", "record", "verdict")] == ["done"] * 5
    assert (run / "verdict.json").is_file() and (run / "reports" / "summary.md").is_file()
    log = (run / "06-report" / "log.txt").read_text(encoding="utf-8")
    assert "06-report/verdict" in log and "start phase10-verdict.py" in log and "output verdict.json verdict=" in log
    assert (run / "06-report" / "tools" / "verdict.log").is_file()
    code, out = ul(tmp_path, "status", "--run", "r1")
    assert code == 0 and "06-report" in out and "next:" in out


def test_why_explains_a_failed_phase_with_its_raw_output_and_hint(report_run):
    tmp_path, run = report_run
    code, out = ul(tmp_path, "build", "--run", "r1", "--foreground")
    assert code == 2, out                                 # no worktrees, no JDKs: stage 1 refuses
    assert "FAILED at job 'build'" in out and "why build" in out
    code, out = ul(tmp_path, "why", "build", "--run", "r1")
    assert code == 0
    assert "phase1-build.py exit 2" in out and "hint:" in out and "raw output" in out


def test_a_background_rerun_moves_the_previous_attempt_to_history(review_run):
    cfg, runs, run = review_run
    tmp_path = run.parent.parent
    for _ in range(2):
        code, out = ul(tmp_path, "review", "--run", "r1", "--no-agents")
        assert code == 0, out
    assert (run / "05-review" / "history" / "1" / "status.json").is_file()
    assert (run / "logs" / "ul-05-review.out.log").is_file()


def test_a_job_that_crashes_ends_as_a_logged_failure_not_a_stuck_phase(review_run, monkeypatch):
    import runner.engine as engine
    cfg, runs, run = review_run

    def boom(ctx, job, rec):
        raise RuntimeError("disk full")
    monkeypatch.setattr(engine, "run_job", boom)
    code, lines = run_phase(cfg, runs, "r1", find_phase("review"))
    assert code == 3
    assert read_status(str(run), find_phase("review"))["state"] == "failed"
    log = (run / "05-review" / "log.txt").read_text(encoding="utf-8")
    assert "crashed: RuntimeError: disk full" in log and "Traceback" in log
    assert not (run / "ul.lock.json").exists()


def test_needs_lists_each_input_and_who_writes_it(report_run):
    tmp_path, run = report_run
    code, out = ul(tmp_path, "needs", "analysis", "--run", "r1")
    assert code == 0
    assert "[MISSING] artifacts\\change-inventory.json" in out and "02-impact" in out
    assert "[ok] artifacts\\environment.json" in out


def test_continue_runs_the_next_phase_that_is_not_done_and_says_when_all_are(review_run):
    cfg, runs, run = review_run
    tmp_path = run.parent.parent
    code, out = ul(tmp_path, "continue", "--run", "r1", "--no-agents")
    assert out.startswith("== 00-setup"), out          # the fixture has no setup phase recorded: it is first
    # Mark every phase done with its current inputs: continue then has nothing left.
    from runner.state import hash_inputs, write_status
    for p in PHASES:
        write_status(str(run), p, {"state": "done", "exitCode": 0, "inputs": hash_inputs(str(run), p)})
    write_json(str(run / "verdict.json"), {"verdict": "safe"})
    code, out = ul(tmp_path, "continue", "--run", "r1")
    assert code == 0 and out.startswith("ALL PHASES DONE: run r1, verdict safe"), out
    # A changed input makes 05-review stale: continue picks it up again.
    write_json(str(run / "artifacts" / "behavior-results.json"), {"tests": [{"id": "T-01", "validated": True}], "x": 2})
    code, out = ul(tmp_path, "continue", "--run", "r1", "--no-agents")
    assert out.startswith("== 05-review") and "(stale)" in out.splitlines()[0], out

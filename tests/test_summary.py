"""The one-page summary (scripts\\lib\\summary.py) must answer the questions even for a partial run."""

import os

from lib.core import write_json
from lib.summary import build_summary, exit_meaning, markdown_to_html


def test_a_run_that_stopped_early_still_gets_a_summary_that_points_at_the_failed_step(tmp_path):
    run = str(tmp_path)
    write_json(os.path.join(run, "status.json"), {"state": "failed-build", "steps": []})
    write_json(os.path.join(run, "orchestration", "ledger.json"), {"entries": [
        {"key": "stage-0", "kind": "stage", "attempt": 1, "state": "done", "startedAt": "2026-11-26T10:00:00+05:30",
         "finishedAt": "2026-11-26T10:00:05+05:30", "exitCode": 0},
        {"key": "stage-1", "kind": "stage", "attempt": 1, "state": "failed", "reason": "build-main: mvn exit code 1",
         "startedAt": "2026-11-26T10:00:06+05:30", "finishedAt": "2026-11-26T10:02:06+05:30", "exitCode": 4}]})
    text = "\n".join(build_summary(run, "r1"))
    assert "No verdict" in text
    assert "| Build and run existing tests | FAILED | 1 | 2 min 00 s | exit 4 = build or tests failed. build-main: mvn exit code 1 |" in text
    assert "Not known: step `Find changed files` did not run." in text


def test_a_difference_is_listed_with_both_results(tmp_path):
    run = str(tmp_path)
    write_json(os.path.join(run, "status.json"), {"state": "verdict-blocked", "steps": []})
    write_json(os.path.join(run, "verdict.json"), {"verdict": "blocked", "blocking": ["x"], "listed": [], "findingStatus": []})
    write_json(os.path.join(run, "artifacts", "behavior-results.json"), {
        "probes": [{"id": "P-1", "flowId": "infrastructure", "request": "GET /h", "outcome": "different", "differences": ["body differs"],
                    "base": {"status": 200, "body": "{\"status\":\"UP\"}"}, "head": {"status": 200, "body": "{\"groups\":[],\"status\":\"UP\"}"}}],
        "tests": []})
    md = build_summary(run, "r1")
    text = "\n".join(md)
    assert "**head is NOT ready to release**" in text
    assert "| probe P-1 | GET /h | 200 {\"status\":\"UP\"} | 200 {\"groups\":[],\"status\":\"UP\"} | body differs |" in text
    assert "<td>200 {&quot;status&quot;:&quot;UP&quot;}</td>" in markdown_to_html(md, "t")


def test_explains_exit_codes_in_plain_words():
    assert exit_meaning(3221225786) == "exit 3221225786 = killed: Ctrl+C or its console window was closed"
    assert exit_meaning(4) == "exit 4 = build or tests failed"

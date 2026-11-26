"""The summary (scripts\\lib\\summary.py) must answer the questions in plain words, even for a partial run."""

import os
import re

from lib.core import write_json
from lib.summary import build_summary, exit_meaning, markdown_to_html


def sections(md):
    """Section title -> its lines."""
    out, cur = {}, None
    for line in md:
        if line.startswith("## "):
            cur = line[3:]
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return out


def blocked_run(tmp_path):
    run = str(tmp_path)
    write_json(os.path.join(run, "status.json"), {"state": "verdict-blocked", "steps": []})
    write_json(os.path.join(run, "verdict.json"), {"verdict": "blocked", "blocking": ["x"], "listed": [],
                                                   "findingStatus": [{"id": "F-001", "status": "confirmed"},
                                                                     {"id": "F-002", "status": "verified"},
                                                                     {"id": "F-003", "status": "open"}]})
    write_json(os.path.join(run, "artifacts", "impact-findings.json"), {"findings": [
        {"id": "F-001", "ruleId": "R-HEALTH", "title": "Health answer may change", "severity": "high", "scope": "global", "source": "rule",
         "impact": "Probes may fail."},
        {"id": "F-002", "ruleId": "R-JSON", "title": "JSON library moved", "severity": "high", "scope": "global", "source": "rule",
         "impact": "Bodies may change."},
        {"id": "F-003", "ruleId": "H-01", "title": "CSV charset", "severity": "low", "scope": "symbol", "source": "analyst",
         "affectedFlows": ["GET /r"], "check": "Download the CSV on both branches.", "impact": "Names garbled."}]})
    write_json(os.path.join(run, "artifacts", "agent", "hypotheses.json"), {
        "hypotheses": [{"id": "H-01", "plain": "The CSV may lose accents.", "productionRisk": "Spreadsheets show garbled names."}],
        "explanations": [{"ruleId": "R-HEALTH", "plain": "The health check answers differently.", "productionRisk": "Load balancers may drop servers."}]})
    write_json(os.path.join(run, "artifacts", "behavior-results.json"), {
        "probes": [{"id": "P-1", "flowId": "infrastructure", "request": "GET /h", "outcome": "different", "differences": ["body differs"],
                    "linkedRules": ["R-HEALTH"], "base": {"status": 200, "body": "{\"status\":\"UP\"}"},
                    "head": {"status": 200, "body": "{\"groups\":[],\"status\":\"UP\"}"}},
                   {"id": "P-2", "flowId": "GET /r", "request": "GET /r", "outcome": "same", "linkedRules": ["R-JSON"]}],
        "checks": [{"ruleId": "R-HEALTH", "source": "probe", "id": "P-1", "outcome": "different"},
                   {"ruleId": "R-JSON", "source": "probe", "id": "P-2", "outcome": "same"}],
        "tests": []})
    write_json(os.path.join(run, "artifacts", "reach.json"), {"flows": [{"id": "GET /r", "untraced": []}]})
    return run


def test_sections_come_in_the_order_people_read_them(tmp_path):
    titles = list(sections(build_summary(blocked_run(tmp_path), "r1")))
    assert [re.sub(r"^\d+\. ", "", t).split(":")[0] for t in titles] == [
        "The answer", "base and head side by side", "Issues found", "Proven", "Potential issues", "What to do", "Details"]


def test_each_part_answers_its_question_in_plain_words(tmp_path):
    md = build_summary(blocked_run(tmp_path), "r1")
    sec = {re.sub(r"^\d+\. ", "", k).split(":")[0]: "\n".join(v) for k, v in sections(md).items()}
    assert "**head is not ready to release: 1 thing(s) behave differently**" in sec["The answer"]
    issue = sec["Issues found"]
    assert "The health check answers differently." in issue
    assert "| GET /h | 200 {\"status\":\"UP\"} | 200 {\"groups\":[],\"status\":\"UP\"} |" in issue
    assert "**Who is affected:** Load balancers may drop servers." in issue
    assert "JSON library moved" in sec["Proven"] and "1 endpoint request(s)" in sec["Proven"]
    potential = sec["Potential issues"]
    assert "The CSV may lose accents." in potential and "Spreadsheets show garbled names." in potential
    assert "Rated low risk, so no automated test was generated" in potential
    assert "Download the CSV on both branches." in sec["What to do"]


def test_internal_ids_stay_out_of_the_readable_part(tmp_path):
    md = build_summary(blocked_run(tmp_path), "r1")
    readable = "\n".join(line for k, v in sections(md).items() if not k.startswith("7.") for line in v)
    assert not re.search(r"\b(F|H|G|P|LIB)-\d+\b", readable), "an internal id is shown outside the details section"


def test_a_run_that_stopped_early_still_gets_a_summary_that_points_at_the_failed_step(tmp_path):
    run = str(tmp_path)
    write_json(os.path.join(run, "status.json"), {"state": "failed-build", "steps": [{"name": "build-main", "state": "failed"}]})
    write_json(os.path.join(run, "orchestration", "ledger.json"), {"entries": [
        {"key": "stage-1", "kind": "stage", "attempt": 1, "state": "failed", "reason": "build-main: mvn exit code 1",
         "startedAt": "2026-11-26T10:00:06+05:30", "finishedAt": "2026-11-26T10:02:06+05:30", "exitCode": 4}]})
    text = "\n".join(build_summary(run, "r1"))
    assert "No answer: the run stopped at step `build-main`." in text
    assert "| Build and run existing tests | FAILED | 1 | 2 min 00 s | exit 4 = build or tests failed. build-main: mvn exit code 1 |" in text


def test_html_has_collapsed_details_and_checklists(tmp_path):
    page = markdown_to_html(build_summary(blocked_run(tmp_path), "r1"), "t")
    assert "<details><summary>Run trace (for troubleshooting)</summary>" in page
    assert "<ul class='check'>" in page and "☐ " in page


def test_explains_exit_codes_in_plain_words():
    assert exit_meaning(3221225786) == "exit 3221225786 = killed: Ctrl+C or its console window was closed"
    assert exit_meaning(4) == "exit 4 = build or tests failed"

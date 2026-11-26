"""Unit tests for the UpgradeLens orchestration and guards (scripts\\lib\\orchestration.py, resolve_run_id).

Run from the repository root:
    python -m pytest tests
No Maven, no server, no git and no agent are needed.
"""

import os

import pytest

from lib.core import resolve_run_id, write_json
from lib.orchestration import (agent_key_step, agent_write_scope, compare_fingerprint, file_tree_hashes, next_action,
                               run_plan, set_ledger_entry, new_ledger, read_ledger, check_analyst_proposal as analyst_check,
                               find_test_gaps as gaps_of, needs_repair,
                               check_release_notes_proposal as notes_check, check_review_answer as review_check,
                               check_tests_proposal as manifest_check)


def ledger(*entries):
    return {"runId": "r", "entries": list(entries)}


def entry(key, state, reason="", kind="stage"):
    return {"key": key, "kind": kind, "state": state, "reason": reason, "exitCode": 0}


def facts(**over):
    f = {"lockActive": False, "lockPid": None, "lockKey": None, "inventoryFiles": 3, "notesNeeded": 0, "gapCount": 0,
         "testsNotValidated": 0, "validatedTests": 0, "regressionsFound": 0}
    f.update(over)
    return f


def done_ledger(keys):
    return ledger(*[entry(k, "done") for k in keys])


PLAN = run_plan(1)
UP_TO_3 = ["stage-0", "stage-1", "stage-2", "stage-3"]
UP_TO_PROBES = UP_TO_3 + ["agent-release-notes", "stage-4", "agent-analyst", "stage-4-merge", "stage-5-probes"]


class TestRunPlan:
    def test_orders_the_agents_between_the_right_stages(self):
        keys = [p["key"] for p in run_plan(1)]
        assert keys[0] == "stage-0"
        assert keys[-1] == "stage-10"
        assert keys.index("agent-release-notes") < keys.index("stage-4")
        assert keys.index("agent-analyst") > keys.index("stage-4")
        assert keys.index("stage-4-merge") == keys.index("agent-analyst") + 1
        assert keys.index("agent-tests") == keys.index("stage-5-probes") + 1
        assert keys.index("agent-reviewer") == keys.index("stage-8") + 1

    def test_adds_one_test_repair_pair_per_round(self):
        assert not [p for p in run_plan(0) if "repair" in p["key"]]
        assert len([p for p in run_plan(2) if "repair" in p["key"]]) == 4


class TestNextAction:
    def test_starts_with_stage_0_on_an_empty_ledger(self):
        assert next_action(PLAN, ledger(), facts())["key"] == "stage-0"

    def test_waits_while_a_stage_holds_the_lock(self):
        assert next_action(PLAN, ledger(), facts(lockActive=True, lockKey="stage-1"))["action"] == "wait"

    def test_skips_the_release_notes_agent_with_a_reason_when_nothing_needs_validating(self):
        a = next_action(PLAN, done_ledger(UP_TO_3), facts())
        assert a["action"] == "skip"
        assert a["key"] == "agent-release-notes"
        assert a["reason"]

    def test_runs_the_release_notes_agent_when_library_changes_need_it(self):
        a = next_action(PLAN, done_ledger(UP_TO_3), facts(notesNeeded=4))
        assert a["action"] == "run-agent"
        assert a["step"] == "release-notes"

    def test_stops_on_a_failed_stage(self):
        led = ledger(entry("stage-0", "done"), entry("stage-1", "failed", "build failed"))
        assert next_action(PLAN, led, facts())["action"] == "stop"

    def test_retries_an_agent_whose_checks_failed_then_stops_after_the_attempt_limit(self):
        base = [entry(k, "done") for k in UP_TO_3 + ["agent-release-notes", "stage-4"]]
        one_fail = ledger(*base, entry("agent-analyst", "failed", "checks: 2 problem(s)", "agent"))
        a = next_action(PLAN, one_fail, facts(), 2)
        assert a["action"] == "run-agent"
        assert a["attempt"] == 2
        two_fail = ledger(*base, entry("agent-analyst", "failed", "checks: x", "agent"), entry("agent-analyst", "failed", "checks: y", "agent"))
        assert next_action(PLAN, two_fail, facts(), 2)["action"] == "stop"

    def test_stops_at_once_on_a_write_scope_violation_or_a_spent_budget(self):
        base = [entry(k, "done") for k in UP_TO_3]
        g = ledger(*base, entry("agent-release-notes", "failed", "guard: changed run:status.json", "agent"))
        assert next_action(PLAN, g, facts(notesNeeded=1))["action"] == "stop"
        b = ledger(*base, entry("agent-release-notes", "failed", "budget: 12 of 12", "agent"))
        assert next_action(PLAN, b, facts(notesNeeded=1))["action"] == "stop"

    def test_asks_for_the_end_guard_when_an_agent_step_was_begun_and_not_ended(self):
        led = ledger(*[entry(k, "done") for k in UP_TO_3], entry("agent-release-notes", "running", "", "agent"))
        assert next_action(PLAN, led, facts())["action"] == "end-agent"

    def test_stops_when_a_stage_is_marked_running_but_no_process_holds_the_lock(self):
        led = ledger(entry("stage-0", "done"), entry("stage-1", "running"))
        assert next_action(PLAN, led, facts())["action"] == "stop"

    def test_runs_the_test_writer_after_the_probes_when_there_are_gaps(self):
        a = next_action(PLAN, done_ledger(UP_TO_PROBES), facts(gapCount=3))
        assert a["action"] == "run-agent"
        assert a["key"] == "agent-tests"

    def test_skips_the_test_run_when_the_test_writer_was_skipped(self):
        led = ledger(*[entry(k, "done") for k in UP_TO_PROBES], entry("agent-tests", "skipped", "no gap", "agent"))
        a = next_action(PLAN, led, facts())
        assert a["key"] == "stage-5-tests"
        assert a["action"] == "skip"

    def test_repairs_tests_that_did_not_validate_and_skips_the_repair_when_all_validated(self):
        keys = UP_TO_PROBES + ["agent-tests", "stage-5-tests"]
        a = next_action(PLAN, done_ledger(keys), facts(testsNotValidated=1))
        assert a["key"] == "agent-tests-repair-1"
        assert a["action"] == "run-agent"
        assert next_action(PLAN, done_ledger(keys), facts())["action"] == "skip"

    def test_runs_the_reviewer_only_when_tests_validated(self):
        keys = [p["key"] for p in PLAN if p["key"] not in ("agent-reviewer", "stage-9", "stage-10")]
        assert next_action(PLAN, done_ledger(keys), facts(validatedTests=2))["action"] == "run-agent"
        assert next_action(PLAN, done_ledger(keys), facts())["action"] == "skip"

    def test_is_done_when_every_step_is_done_or_skipped(self):
        assert next_action(PLAN, done_ledger([p["key"] for p in PLAN]), facts())["action"] == "done"


class TestTestNeedsRepair:
    @staticmethod
    def t(base, head, validated):
        return {"base": {"passed": base}, "head": {"passed": head}, "validated": validated}

    def test_repairs_a_test_that_fails_on_master(self):
        assert needs_repair(self.t(False, False, False)) is True

    def test_repairs_a_test_that_passes_on_both_but_misses_an_injection(self):
        assert needs_repair(self.t(True, True, False)) is True

    def test_keeps_a_test_that_proves_a_behavior_change(self):
        assert needs_repair(self.t(True, False, False)) is False

    def test_keeps_a_validated_test(self):
        assert needs_repair(self.t(True, True, True)) is False


class TestTestGaps:
    cm = {"methods": [{"file": "src/main/java/a/B.java", "kind": "method", "name": "run", "tested": False},
                      {"file": "src/main/java/a/B.java", "kind": "method", "name": "ok", "tested": True}]}
    reach = {"flows": [{"id": "POST /x", "untraced": ["B#run"]}, {"id": "GET /y", "untraced": []}]}
    impact = {"findings": [
        {"id": "F-001", "ruleId": "R-A", "severity": "high", "scope": "global", "title": "a", "impact": "i"},
        {"id": "F-002", "ruleId": "R-B", "severity": "high", "scope": "global", "title": "b", "impact": "i"},
        {"id": "F-003", "ruleId": "R-C", "severity": "low", "scope": "global", "title": "c", "impact": "i"},
        {"id": "F-004", "ruleId": "R-D", "severity": "high", "scope": "test", "title": "d", "impact": "i"}]}
    checks = [{"ruleId": "R-B", "outcome": "same"}]

    def test_lists_untested_changes_untraced_flows_and_open_production_findings_in_that_order(self):
        g = gaps_of(self.cm, self.reach, self.impact, self.checks)
        assert len(g) == 3
        assert g[0]["type"] == "untested-change"
        assert g[1]["type"] == "untraced-flow"
        assert g[2]["ref"] == "F-001"
        assert g[0]["id"] == "G-01"
        assert g[2]["id"] == "G-03"

    def test_leaves_out_verified_low_and_test_only_findings(self):
        g = gaps_of(self.cm, self.reach, self.impact, self.checks)
        assert not [x for x in g if x["ref"] in ("F-002", "F-003", "F-004")]


class TestAnalystProposal:
    inv = {"files": [{"path": "src/main/java/a/B.java", "category": "main-code", "symbols": [{"symbol": "method:run"}]}]}
    imp = {"findings": [{"ruleId": "R-A", "severity": "high", "source": "rule"}, {"ruleId": "R-L", "severity": "low", "source": "rule"}]}

    @staticmethod
    def good():
        return {"agent": "upgradelens-analyst", "inputHash": "H",
                "hypotheses": [{"id": "H-01", "claim": "c", "severity": "high", "scope": "global", "probeIdea": "p", "plain": "p",
                                "example": "e", "productionRisk": "r"}],
                "explanations": [{"ruleId": "R-A", "plain": "p", "example": "e", "productionRisk": "r"}],
                "changeReasons": [{"file": "src/main/java/a/B.java", "symbol": "method:run", "reason": "why", "evidence": "commit",
                                   "consequence": "c"}]}

    def test_accepts_a_complete_proposal(self):
        assert analyst_check(self.good(), "H", self.inv, self.imp) == []

    def test_rejects_a_stale_hash_a_missing_change_reason_and_a_missing_plain_explanation(self):
        bad = self.good()
        bad["changeReasons"] = []
        bad["explanations"] = []
        e = analyst_check(bad, "OTHER", self.inv, self.imp)
        assert len([x for x in e if "inputHash" in x]) == 1
        assert len([x for x in e if x.startswith("no change reason")]) == 1
        assert len([x for x in e if x.startswith("no plain explanation for rule R-A")]) == 1

    def test_rejects_a_hypothesis_without_an_example_and_more_than_8_hypotheses(self):
        bad = self.good()
        bad["hypotheses"][0]["example"] = ""
        assert len([x for x in analyst_check(bad, "H", self.inv, self.imp) if "no example" in x]) == 1
        many = self.good()
        many["hypotheses"] = [{"id": f"H-{n:02d}", "claim": "c", "severity": "low", "scope": "none", "probeIdea": "p", "plain": "p",
                               "example": "e", "productionRisk": "r"} for n in range(1, 10)]
        assert len([x for x in analyst_check(many, "H", self.inv, self.imp) if "at most 8" in x]) == 1

    def test_rejects_a_missing_document(self):
        assert analyst_check(None, "H", self.inv, self.imp) == ["hypotheses.json is missing or not valid JSON"]


class TestReleaseNotesProposal:
    def test_needs_an_item_for_every_ref_and_an_https_source_for_a_confirmed_item(self):
        doc = {"agent": "upgradelens-release-notes", "inputHash": "H",
               "items": [{"ref": "LIB-001", "result": "confirmed", "source": {"url": "http://x"}, "quote": "q"}]}
        e = notes_check(doc, "H", ["LIB-001", "g:a"])
        assert len([x for x in e if "https" in x]) == 1
        assert "no item for g:a" in e

    def test_accepts_not_found_without_a_source(self):
        doc = {"agent": "upgradelens-release-notes", "inputHash": "H", "items": [{"ref": "LIB-001", "result": "not-found"}]}
        assert notes_check(doc, "H", ["LIB-001"]) == []


class TestTestsProposal:
    gaps = [{"id": "G-01"}, {"id": "G-02"}]
    reason_g2 = {"gapId": "G-02", "reason": "runtime only: needs the real database server"}

    @pytest.fixture
    def dirs(self, tmp_path):
        tests_dir = tmp_path / "tests"
        head = tmp_path / "head"
        (tests_dir / "src" / "test" / "java" / "a").mkdir(parents=True)
        (head / "src" / "main" / "java" / "a").mkdir(parents=True)
        (tests_dir / "src" / "test" / "java" / "a" / "BCharacterizationTest.java").write_text("class BCharacterizationTest {}\n")
        (head / "src" / "main" / "java" / "a" / "B.java").write_text('return status("CANCELLED");\nreturn other;\n')
        return str(tests_dir), str(head)

    @staticmethod
    def manifest(uncovered):
        return {"writer": "upgradelens-test-writer", "inputHash": "H", "uncovered": list(uncovered), "tests": [
            {"id": "T-01", "className": "BCharacterizationTest", "relativePath": "src/test/java/a/BCharacterizationTest.java",
             "purpose": "pins cancel", "gapIds": ["G-01"],
             "injections": [{"file": "src/main/java/a/B.java", "find": 'status("CANCELLED")', "replace": 'status("NEW")',
                             "note": "wrong status"}]}]}

    def test_accepts_a_manifest_that_covers_every_gap_with_a_test_or_a_reason(self, dirs):
        assert manifest_check(self.manifest([self.reason_g2]), "H", self.gaps, *dirs) == []

    def test_rejects_a_gap_with_neither_a_test_nor_a_reason(self, dirs):
        e = manifest_check(self.manifest([]), "H", self.gaps, *dirs)
        assert len([x for x in e if x.startswith("gap G-02")]) == 1

    def test_rejects_an_injection_whose_text_is_not_found_exactly_once_and_an_unlisted_test_file(self, dirs):
        tests_dir, head = dirs
        m = self.manifest([self.reason_g2])
        m["tests"][0]["injections"][0]["find"] = "return"
        with open(os.path.join(tests_dir, "src", "test", "java", "a", "Extra.java"), "w") as f:
            f.write("x")
        e = manifest_check(m, "H", self.gaps, tests_dir, head)
        assert len([x for x in e if "occurs 2 time(s)" in x]) == 1
        assert len([x for x in e if "not in the manifest" in x and x.endswith("Extra.java")]) == 1

    def test_rejects_a_test_path_outside_src_test(self, dirs):
        m = self.manifest([self.reason_g2])
        m["tests"][0]["relativePath"] = "src/main/java/a/BCharacterizationTest.java"
        assert len([x for x in manifest_check(m, "H", self.gaps, *dirs) if "under src/test/" in x]) == 1


class TestReviewAnswer:
    def test_needs_a_verdict_with_notes_for_every_validated_test_and_nothing_else(self):
        doc = {"reviews": [{"testId": "T-01", "verdict": "pass", "notes": "ok"}, {"testId": "T-09", "verdict": "maybe", "notes": ""}]}
        e = review_check(doc, ["T-01", "T-02"])
        assert "no review for T-02" in e
        assert [x for x in e if "T-09" in x]


class TestFingerprintAndWriteScope:
    def test_finds_added_removed_and_changed_keys(self):
        assert compare_fingerprint({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "X", "d": "4"}) == ["b", "c", "d"]

    def test_lets_each_agent_write_only_its_own_files(self):
        assert agent_write_scope(["tests-manifest.json", os.path.join("tests", "src", "test", "java", "A.java")], "tests") == []
        assert len(agent_write_scope(["hypotheses.json", "usage.json"], "tests")) == 2
        assert len(agent_write_scope(["review.json"], "reviewer")) == 1

    def test_maps_agent_keys_to_steps(self):
        assert agent_key_step("agent-analyst") == "analyst"
        assert agent_key_step("agent-tests-repair-2") == "tests"
        assert agent_key_step("stage-4") is None


class TestFileTreeHashes:
    def test_hashes_files_and_honours_the_exclude_patterns(self, tmp_path):
        (tmp_path / "artifacts" / "agent").mkdir(parents=True)
        (tmp_path / "status.json").write_text("{}")
        (tmp_path / "artifacts" / "agent" / "x.json").write_text("{}")
        (tmp_path / "artifacts" / "y.json").write_text("{}")
        m = file_tree_hashes(str(tmp_path), exclude=["artifacts\\agent\\*"], prefix="run:")
        assert "run:status.json" in m
        assert "run:" + os.path.join("artifacts", "y.json") in m
        assert "run:" + os.path.join("artifacts", "agent", "x.json") not in m


class TestLedger:
    def test_adds_entries_and_updates_the_last_attempt_of_a_key(self, tmp_path):
        run = str(tmp_path)
        new_ledger(run, "r1", "cfg.json")
        set_ledger_entry(run, "stage-1", "stage", "running", "go")
        set_ledger_entry(run, "stage-1", "stage", "done", "", {"exitCode": 0}, update=True)
        set_ledger_entry(run, "agent-x", "agent", "failed", "checks")
        set_ledger_entry(run, "agent-x", "agent", "running", "retry")
        led = read_ledger(run)
        assert [e["key"] for e in led["entries"]] == ["stage-1", "agent-x", "agent-x"]
        assert led["entries"][0]["state"] == "done"
        assert led["entries"][0]["exitCode"] == 0
        assert led["entries"][0]["reason"] == ""
        assert led["entries"][2]["attempt"] == 2


class TestResolveRunIdGuards:
    @pytest.fixture
    def runs(self, tmp_path):
        r = tmp_path / "runs"
        (r / "r1" / "orchestration").mkdir(parents=True)
        (r / "r2").mkdir(parents=True)
        write_json(str(r / "r1" / "status.json"), {"state": "phase3-done"})
        write_json(str(r / "r1" / "orchestration" / "ledger.json"), {"entries": []})
        write_json(str(r / "r2" / "status.json"), {"state": "phase2-done"})
        return str(r)

    def test_refuses_a_stage_of_an_orchestrated_run_that_does_not_come_from_run_stage(self, runs, monkeypatch):
        monkeypatch.delenv("UPGRADELENS_INVOKER", raising=False)
        assert resolve_run_id(runs, "r1", ["phase3-done"]) is None
        monkeypatch.setenv("UPGRADELENS_INVOKER", "run-stage")
        assert resolve_run_id(runs, "r1", ["phase3-done"]) == "r1"

    def test_refuses_a_run_in_the_wrong_state_unless_any_state(self, runs, monkeypatch):
        monkeypatch.delenv("UPGRADELENS_INVOKER", raising=False)
        assert resolve_run_id(runs, "r2", ["phase7-done"]) is None
        assert resolve_run_id(runs, "r2", ["phase7-done"], any_state=True) == "r2"

    def test_picks_the_newest_run_in_one_of_the_states(self, runs):
        assert resolve_run_id(runs, None, ["phase2-done", "phase3-done"]) == "r2"

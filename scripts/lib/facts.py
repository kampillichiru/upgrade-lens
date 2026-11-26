"""The facts of a run that the plan decides on (lib\\plan.py, next_action), read from its artifacts, and the
test writer's work list (artifacts\\test-gaps.json)."""

from __future__ import annotations

import os

from .analysis import find_test_gaps, needs_repair, proves_change, release_notes_refs
from .core import as_list, get, items, now_iso, read_json_safe, write_json
from .ledger import ledger_entry, run_lock

GAP_SOURCES = ("changed-methods.json", "reach.json", "impact-findings.json", "behavior-results.json")


def test_gaps_path(run_dir: str) -> str:
    return os.path.join(run_dir, "artifacts", "test-gaps.json")


def write_test_gaps(run_dir: str, run_id: str) -> list[dict]:
    """Writes artifacts\\test-gaps.json from the stage 4 and stage 5 artifacts, and returns the gaps. The list is
    fixed once the test writer begins: it lives outside artifacts\\agent, so the writer cannot change it."""
    a = os.path.join(run_dir, "artifacts")
    behavior = read_json_safe(os.path.join(a, "behavior-results.json"))
    gaps = find_test_gaps(read_json_safe(os.path.join(a, "changed-methods.json")),
                          read_json_safe(os.path.join(a, "reach.json")),
                          read_json_safe(os.path.join(a, "impact-findings.json")),
                          as_list(get(behavior, "checks")))
    write_json(test_gaps_path(run_dir), {"schemaVersion": 1, "runId": run_id, "createdAt": now_iso(), "gaps": gaps})
    return gaps


def run_facts(run_dir: str, ledger) -> dict:
    """The facts next_action needs, read from the run's artifacts. Writes artifacts\\test-gaps.json when the
    test writer is next and has not started (the gap list is fixed once the writer begins)."""
    a = os.path.join(run_dir, "artifacts")
    lock = run_lock(run_dir)
    inventory = read_json_safe(os.path.join(a, "change-inventory.json"))
    lib_api = read_json_safe(os.path.join(a, "library-api-diff.json"))
    dep_diff = read_json_safe(os.path.join(a, "dependency-diff.json"))
    behavior = read_json_safe(os.path.join(a, "behavior-results.json"))
    gap_count = 0
    probes = ledger_entry(ledger, "stage-5-probes")
    writer = ledger_entry(ledger, "agent-tests")
    if probes and probes.get("state") == "done" and not writer:
        gap_count = len(write_test_gaps(run_dir, get(ledger, "runId")))
    elif os.path.isfile(test_gaps_path(run_dir)):
        gap_count = len(items(get(read_json_safe(test_gaps_path(run_dir)), "gaps")))
    tests = items(get(behavior, "tests"))
    return {
        "testsNotValidated": len([t for t in tests if needs_repair(t)]),
        "regressionsFound": len([t for t in tests if proves_change(t)]),
        "lockActive": lock["active"],
        "lockPid": lock["pid"],
        "lockKey": lock["key"],
        "inventoryFiles": len(items(get(inventory, "files"))),
        "notesNeeded": len(release_notes_refs(lib_api, dep_diff)),
        "gapCount": gap_count,
        "validatedTests": len([t for t in tests if t.get("validated")]),
    }

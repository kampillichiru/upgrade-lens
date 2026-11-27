"""The test writer's work list (artifacts\\test-gaps.json), written by the gaps job of phase 04-tests."""

from __future__ import annotations

import os

from .analysis import find_test_gaps
from .core import as_list, get, now_iso, read_json_safe, write_json

GAP_SOURCES = ("changed-methods.json", "reach.json", "impact-findings.json", "behavior-results.json", "assert-strength.json")


def test_gaps_path(run_dir: str) -> str:
    return os.path.join(run_dir, "artifacts", "test-gaps.json")


def write_test_gaps(run_dir: str, run_id: str) -> list[dict]:
    """Writes artifacts\\test-gaps.json from the stage 4 and stage 5 artifacts, and returns the gaps. The list
    lives outside artifacts\\agent and the writer's job folder, so the test writer cannot change its own work list."""
    a = os.path.join(run_dir, "artifacts")
    behavior = read_json_safe(os.path.join(a, "behavior-results.json"))
    gaps = find_test_gaps(read_json_safe(os.path.join(a, "changed-methods.json")),
                          read_json_safe(os.path.join(a, "reach.json")),
                          read_json_safe(os.path.join(a, "impact-findings.json")),
                          as_list(get(behavior, "checks")),
                          read_json_safe(os.path.join(a, "assert-strength.json")))
    write_json(test_gaps_path(run_dir), {"schemaVersion": 1, "runId": run_id, "createdAt": now_iso(), "gaps": gaps})
    return gaps

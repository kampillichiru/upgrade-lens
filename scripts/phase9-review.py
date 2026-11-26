"""Stage 9 of UpgradeLens (test and report review): records the agent review of the validated tests
and the human decisions on the manual items, and checks both against the files they refer to.

Agent review (approach section 9): artifacts\\agent\\review.json, written from the reviewer agent's
answer. For each validated test, a review counts only when:
  - its verdict is "pass"
  - its fileSha256 matches the proposal file on disk now, and the copy stage 8 placed in the head worktree
  - its reviewer is not the test's writer (the reviewer did not write the test)

Manual review: review\\manual-decisions.json, written by a person. Each entry:
    { "itemId": "MI-001", "decision": "accept|reject|needs-test|defer", "reviewer": "name", "note": "..." }
A decision without an itemId from artifacts\\manual-items.json, a decision outside the four values, or a
missing reviewer is recorded as invalid and does not count.

Writes review\\review-record.json. Stage 10 reads it. This stage does not decide the verdict.

Exit codes:
  0  record written
  2  usage or run problem (no run, stage 8 not done)

Example:
  python scripts/phase9-review.py --run-id 20261126-131717
"""

import argparse
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.core import (TOOL_VERSION, file_sha256, get, items, log, native_path, read_json_safe, resolve_config_path,  # noqa: E402
                      resolve_run_id, run_main, runs_root, s, set_run_state, set_stage_log, set_stage_step, unique_sorted,
                      write_json)

LATER_STATES = ["phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe"]
ALLOWED = ["accept", "reject", "needs-test", "defer"]


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 9: agent review and manual decisions.")
    p.add_argument("--run-id")
    p.add_argument("--config")
    a = p.parse_args(argv)

    config = resolve_config_path(a.config)
    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return 2
    runs = runs_root(read_json_safe(config))
    run_id = resolve_run_id(runs, a.run_id, LATER_STATES)
    if not run_id:
        print(f"No run with stage 8 done under {runs}. Run phase8-output.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    artifacts = os.path.join(run_dir, "artifacts")
    review_dir = os.path.join(run_dir, "review")
    status_file = os.path.join(run_dir, "status.json")
    set_stage_log(os.path.join(run_dir, "logs", "phase9.log"))
    os.makedirs(review_dir, exist_ok=True)
    art = lambda name: read_json_safe(os.path.join(artifacts, name))  # noqa: E731
    status, behavior, manual = read_json_safe(status_file), art("behavior-results.json"), art("manual-items.json")
    manifest, agent_review = art(os.path.join("agent", "tests-manifest.json")), art(os.path.join("agent", "review.json"))
    decisions_in = read_json_safe(os.path.join(review_dir, "manual-decisions.json"))
    if not all((status, behavior, manual)):
        print(f"Run incomplete: {run_dir} (needs stages 5 to 8)")
        return 2

    log(f"phase 9 review for run {run_id}")

    # ---------- agent review ----------

    validated = [t for t in items(behavior.get("tests")) if t.get("validated")]
    placed_record = art("new-tests.json")
    agent_rows = []
    agent_state = "done" if agent_review else "not-run"
    writer = s(get(manifest, "writer"))

    for t in validated:
        f = os.path.join(artifacts, "agent", "tests", native_path(t.get("relativePath")))
        current = file_sha256(f) if os.path.isfile(f) else ""
        rev = next((r for r in items(get(agent_review, "reviews")) if r.get("testId") == t.get("id")), None)
        # The copy stage 8 placed in the head worktree must still be the reviewed file.
        placed_hash = current
        placed = next((x for x in items(get(placed_record, "placed")) if x.get("relativePath") == t.get("relativePath")), None)
        if placed:
            pp = os.path.join(s(placed_record.get("location")), native_path(t.get("relativePath")))
            placed_hash = file_sha256(pp) if os.path.isfile(pp) else ""
        result = "missing"
        if rev:
            if rev.get("fileSha256") != current or placed_hash != current:
                result = "stale"
            elif rev.get("reviewer") and writer and s(rev.get("reviewer")).strip().lower() == writer.strip().lower():
                result = "same-author"
            elif rev.get("verdict") == "pass":
                result = "reviewed"
            else:
                result = "failed"
        agent_rows.append({"testId": t.get("id"), "relativePath": t.get("relativePath"), "result": result,
                           "reviewer": rev.get("reviewer") if rev else "", "verdict": rev.get("verdict") if rev else "",
                           "notes": rev.get("notes") if rev else ""})
        log(f"  agent review {t.get('id')}: {result}")
    if not validated and agent_state != "done":
        agent_state = "not-needed"

    # ---------- manual decisions ----------

    by_id = {i.get("id"): i for i in items(manual.get("items"))}
    valid, invalid = [], []
    for d in items(get(decisions_in, "decisions")):
        why = None
        if not d.get("itemId") or d.get("itemId") not in by_id:
            why = f"unknown itemId '{s(d.get('itemId'))}'"
        elif d.get("decision") not in ALLOWED:
            why = f"decision must be one of: {', '.join(ALLOWED)}"
        elif not s(d.get("reviewer")).strip():
            why = "reviewer is empty"
        if why:
            invalid.append({"itemId": d.get("itemId"), "reason": why})
        else:
            item = by_id[d["itemId"]]
            valid.append({"itemId": d["itemId"], "kind": item.get("kind"), "ref": item.get("ref"), "decision": d["decision"],
                          "reviewer": s(d.get("reviewer")), "note": s(d.get("note"))})
    decided = unique_sorted([v["itemId"] for v in valid])
    pending = [i.get("id") for i in items(manual.get("items")) if i.get("id") not in decided]
    if not items(manual.get("items")):
        manual_status = "none-needed"
    elif not pending:
        manual_status = "recorded"
    elif decided:
        manual_status = "partial"
    else:
        manual_status = "pending"

    write_json(os.path.join(review_dir, "review-record.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id,
        "agent": {"state": agent_state, "writer": writer, "tests": agent_rows},
        "manual": {"status": manual_status, "decisions": valid, "invalid": invalid, "pending": pending},
    })
    reviewed = len([r for r in agent_rows if r["result"] == "reviewed"])
    log(f"agent review: {agent_state}; validated tests reviewed: {reviewed} of {len(validated)}")
    log(f"manual review: {manual_status}; decided {len(decided)} of {len(items(manual.get('items')))} item(s); invalid entries: {len(invalid)}")

    set_stage_step(status, "review", "done", "", os.path.join(review_dir, "review-record.json"))
    set_run_state(status, "phase9-done", "phase9")
    write_json(status_file, status)
    log(f"phase 9 done: review record written for run {run_id}")
    return 0


if __name__ == "__main__":
    run_main(main)

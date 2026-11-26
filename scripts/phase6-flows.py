"""Stage 6 of UpgradeLens (flow selection): decides the regression scope of each flow, from the findings,
the behavior checks and the coverage.

Scope per flow (approach section 6 and 7):
  full      keep the full regression. Any confirmed behavior change, any untraced code in the flow,
            or any affecting finding that no check has verified.
  targeted  reduced: targeted tests plus a smoke suite. Only when every affecting finding is verified
            by a check that matched master, all reached code is covered and traced, and nothing is confirmed.
  smoke     smoke only: no affecting finding and no untraced code.

The reduction is never based on a pass alone. A flow with untraced code stays full. Accepted
differences (stage 9) do not reduce a flow in this version: they unblock the verdict only.

Time saved is not estimated: the run has no regression duration data.

Writes artifacts\\flows.json.

Exit codes:
  0  written
  2  usage or run problem (no run, stage 5 not done)

Example:
  python scripts/phase6-flows.py --run-id 20261126-131717
"""

import argparse
import os
import sys
from collections import Counter

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.analysis import finding_status  # noqa: E402
from lib.core import (TOOL_VERSION, as_list, flow_scope, icontains, items, log, ps_sorted, read_json_safe,  # noqa: E402
                      resolve_config_path, resolve_run_id, run_main, runs_root, set_run_state, set_stage_log, set_stage_step,
                      write_json)

LATER_STATES = ["phase5-done", "phase6-done", "phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed",
                "verdict-safe"]


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 6: regression scope per flow.")
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
        print(f"No run with stage 5 done under {runs}. Run phase5-behavior.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    artifacts = os.path.join(run_dir, "artifacts")
    status_file = os.path.join(run_dir, "status.json")
    set_stage_log(os.path.join(run_dir, "logs", "phase6.log"))
    status = read_json_safe(status_file)
    impact = read_json_safe(os.path.join(artifacts, "impact-findings.json"))
    reach = read_json_safe(os.path.join(artifacts, "reach.json"))
    behavior = read_json_safe(os.path.join(artifacts, "behavior-results.json"))
    if not all((status, impact, reach, behavior)):
        print(f"Run incomplete: {run_dir} (needs stages 4 and 5)")
        return 2

    log(f"phase 6 flow selection for run {run_id}")
    checks = items(behavior.get("checks"))
    findings = items(impact.get("findings"))

    # Finding status from the checks only (decisions are applied at the verdict).
    finding_states = {f["id"]: finding_status(f, checks) for f in findings}

    rows = []
    for fl in items(reach.get("flows")):
        affecting = [f for f in findings if icontains(f.get("affectedFlows"), fl["id"])]
        statuses = [finding_states[f["id"]] for f in affecting]
        untraced = as_list(fl.get("untraced"))
        any_confirmed = "confirmed" in statuses
        scope = flow_scope(statuses, len(untraced), any_confirmed)

        reasons = []
        if any_confirmed:
            reasons.append("confirmed behavior change in this flow")
        if untraced:
            reasons.append(f"{len(untraced)} reached symbol(s) with no test execution: {', '.join(untraced)}")
        open_count = len([x for x in statuses if x in ("open", "hypothesis")])
        if open_count:
            reasons.append(f"{open_count} affecting finding(s) not verified by any check")
        if scope == "targeted":
            reasons.append("every affecting finding verified, all reached code covered and traced")
        if scope == "smoke":
            reasons.append("no affecting finding and no untraced code")

        rows.append({
            "id": fl["id"], "kind": fl.get("kind"), "scope": scope, "reason": "; ".join(reasons),
            "affectingFindings": [f["id"] for f in affecting], "untraced": untraced,
            "probes": [pr.get("id") for pr in items(behavior.get("probes")) if pr.get("flowId") == fl["id"]],
            "tests": [t.get("id") for t in items(behavior.get("tests")) if icontains(t.get("flowIds"), fl["id"])],
        })
        log(f"  {scope:<8} {fl['id']}: {'; '.join(reasons)}")

    doc = {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id,
        "timeSaved": "not estimated: no regression duration data for this application",
        "summary": {"flows": len(rows), **{sc: len([r for r in rows if r["scope"] == sc]) for sc in ("full", "targeted", "smoke")}},
        "findingStatus": [{"id": f["id"], "ruleId": f.get("ruleId"), "status": finding_states[f["id"]]} for f in findings],
        "flows": rows,
    }
    write_json(os.path.join(artifacts, "flows.json"), doc)

    sm = doc["summary"]
    log(f"flows: {sm['full']} full, {sm['targeted']} targeted, {sm['smoke']} smoke")
    counts = Counter(x["status"] for x in doc["findingStatus"])
    log("findings by status: " + ", ".join(f"{k} {counts[k]}" for k in ps_sorted(counts)))

    set_stage_step(status, "flows", "done", "", os.path.join(artifacts, "flows.json"))
    set_run_state(status, "phase6-done", "phase6")
    write_json(status_file, status)
    log(f"phase 6 done: flow scope written for run {run_id}")
    return 0


if __name__ == "__main__":
    run_main(main)

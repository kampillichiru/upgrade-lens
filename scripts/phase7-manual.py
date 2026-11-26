"""Stage 7 of UpgradeLens (manual review items): lists what the tests and probes cannot decide, with the
question, the expected result on master, and an owner. People answer these in stage 9.

Items come from:
  - each library API change (stage 3) that master's code used, with the release-notes result if any
  - each release-notes claim that the notes contradict or do not mention (release-notes agent)
  - each added or removed dependency assessed unnecessary or review, and each major version conflict
  - each changed method or type that no test executes on change_branch
  - each finding that no check verified (status open or hypothesis) and each confirmed behavior change
  - each flow with untraced code (the expected master result is the master probe of that flow, if any)
  - each probe that differs between the branches
  - bean and property dump differences (a bean whose class moved is normal in a major upgrade, but it
    still needs a person to say so)
  - each blocking error from stage 5 (a probe or a test that could not run)

Owners come from config\\upgrade-lens.json, "owners.reviewer" and "owners.riskAcceptor". An empty owner
is written as "unassigned", and the verdict lists the item as pending.

Writes artifacts\\manual-items.json. Item ids (MI-001...) are what the decisions file refers to.

Exit codes:
  0  written
  2  usage or run problem (no run, stage 6 not done)

Example:
  python scripts/phase7-manual.py --run-id 20261126-131717
"""

import argparse
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.agents import agent_proposal  # noqa: E402
from lib.core import (TOOL_VERSION, as_list, get, items, like, log, read_json_safe, resolve_config_path,  # noqa: E402
                      resolve_run_id, run_main, runs_root, s, set_run_state, set_stage_log, set_stage_step, write_json)

LATER_STATES = ["phase6-done", "phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe"]
KINDS = [("finding", "finding"), ("untracedFlow", "untraced-flow"), ("probeDifference", "probe-difference"),
         ("beanDump", "bean-dump"), ("propertyDump", "property-dump"), ("libraryChange", "library-change"),
         ("releaseNote", "release-note"), ("dependency", "dependency"), ("dependencyConflict", "dependency-conflict"),
         ("untestedChange", "untested-change"), ("stageError", "stage-error")]


def main(argv) -> int:
    p = argparse.ArgumentParser(description="UpgradeLens stage 7: manual review items.")
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
        print(f"No run with stage 6 done under {runs}. Run phase6-flows.py first.")
        return 2

    run_dir = os.path.join(runs, run_id)
    artifacts = os.path.join(run_dir, "artifacts")
    status_file = os.path.join(run_dir, "status.json")
    set_stage_log(os.path.join(run_dir, "logs", "phase7.log"))
    art = lambda name: read_json_safe(os.path.join(artifacts, name))  # noqa: E731
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json"))
    status = read_json_safe(status_file)
    impact, flows, behavior = art("impact-findings.json"), art("flows.json"), art("behavior-results.json")
    if not all((cfg, status, impact, flows, behavior)):
        print(f"Run incomplete: {run_dir} (needs stages 4 to 6)")
        return 2

    def owner(role: str) -> str:
        v = s(get(cfg, "owners", role)).strip()
        return v if v else "unassigned"

    log(f"phase 7 manual review items for run {run_id}")
    found = []

    def add_item(kind: str, ref, question: str, expected_master: str, who: str) -> None:
        found.append({"id": f"MI-{len(found) + 1:03d}", "kind": kind, "ref": ref, "question": question,
                      "expectedMaster": expected_master, "owner": who})

    checks = items(behavior.get("checks"))
    probes = items(behavior.get("probes"))

    # Status per finding comes from stage 6.
    status_by_id = {fs.get("id"): fs.get("status") for fs in items(flows.get("findingStatus"))}
    for f in items(impact.get("findings")):
        if status_by_id.get(f["id"]) not in ("open", "hypothesis", "confirmed"):
            continue
        expected = "Master behaves as the master code and its probes show (see the linked probes)."
        if not [c for c in checks if c.get("ruleId") == f.get("ruleId")]:
            expected = "Not determined: no check is linked to this rule. Master's behavior has to be read from the master code."
        add_item("finding", f["id"], f"[{f.get('severity')}] {f.get('title')}: {f.get('evidence')}. Is this change safe for the "
                                     f"flows it touches? Check: {f.get('check')}", expected, owner("reviewer"))

    for fl in items(flows.get("flows")):
        untraced = as_list(fl.get("untraced"))
        if not untraced:
            continue
        probe = next((pr for pr in probes if pr.get("flowId") == fl["id"]), None)
        expected = "Not determined: no probe covers this flow."
        if probe and probe.get("base"):
            expected = f"Master (base) gives status {get(probe, 'base', 'status')} on {probe.get('request')}: {get(probe, 'base', 'body')}"
        add_item("untraced-flow", fl["id"], f"Flow {fl['id']} reaches code that no test executes: {', '.join(untraced)}. Test it, or "
                                            "accept the full regression for this flow.", expected, owner("reviewer"))

    order_probes = [pr for pr in probes if like(pr.get("flowId"), "*/api/*")]
    order_same = len([pr for pr in order_probes if pr.get("outcome") == "same"])
    for pr in probes:
        if pr.get("outcome") != "different":
            continue
        expected = (f"Master (base) gives status {get(pr, 'base', 'status')}, content type '{s(get(pr, 'base', 'contentType'))}', "
                    f"body: {s(get(pr, 'base', 'body'))}")
        # Focus: the flow the difference belongs to. Infrastructure probes are not order flows; say so, and say
        # how many order-flow probes still match master, so the reader knows what is unchanged.
        if pr.get("flowId") == "infrastructure":
            focus = f"Focus: infrastructure, not an order flow. Platform health checks call {pr.get('request')}."
        else:
            focus = f"Focus: flow {pr.get('flowId')}."
        focus += f" Order-flow probes matching master: {order_same} of {len(order_probes)}."
        add_item("probe-difference", pr.get("id"), f"Probe {pr.get('id')} ({pr.get('request')}) differs on change_branch: "
                                                   f"{'; '.join(as_list(pr.get('differences')))}. Is this change intended? {focus}",
                 expected, owner("riskAcceptor"))

    if get(behavior, "dumps", "available"):
        d = get(behavior, "dumps", "beans") or {}
        added, removed, type_changed = as_list(d.get("added")), as_list(d.get("removed")), as_list(d.get("typeChanged"))
        if added or removed or type_changed:
            sample = ", ".join((type_changed + removed)[:6])
            add_item("bean-dump", "beans", f"Bean set differs: {len(added)} added, {len(removed)} removed, {len(type_changed)} with a "
                                           f"different class. Examples: {sample}. Are these framework renames only?",
                     "Master's bean set is in artifacts\\behavior-results.json (dumps.beans).", owner("reviewer"))
        pr = get(behavior, "dumps", "properties") or {}
        if as_list(pr.get("added")) or as_list(pr.get("removed")) or as_list(pr.get("changed")):
            add_item("property-dump", "properties", f"Application properties differ: added {len(as_list(pr.get('added')))}, removed "
                                                    f"{len(as_list(pr.get('removed')))}, changed {len(as_list(pr.get('changed')))}.",
                     "Master's application properties are in artifacts\\behavior-results.json.", owner("reviewer"))

    # Library API changes that master's code used (removed or moved), with the release-notes result when the
    # release-notes agent ran on the current inputs.
    lib_api = art("library-api-diff.json")
    notes = agent_proposal(run_dir, "release-notes", "release-notes.json")
    notes_by_ref = {s(n.get("ref")): n for n in items(get(notes, "doc", "items"))} if notes["state"] == "used" else {}
    important = [it for it in items(get(lib_api, "items")) if it.get("severity") in ("high", "medium")] if get(lib_api, "available") else []
    for it in important:
        users = [u for u in as_list(get(it, "usage", "base")) + as_list(get(it, "usage", "head")) if u and u.get("source")]
        where = ", ".join(f"{u['source']}:{s(u.get('line'))}" for u in users[:3])
        rn = notes_by_ref.get(it.get("id"))
        rn_text = f" Release notes: {rn.get('result')} ({s(get(rn, 'source', 'url'))})." if rn else " Release notes: not validated."
        expected = f"Master uses {it.get('class')} from {get(it, 'artifact', 'base')}" + (f" at {where}" if where else "") + "."
        add_item("library-change", it.get("id"), f"[{it.get('severity')}] {it.get('state')} {it.get('class')}: {it.get('summary')} Is the "
                                                 f"change_branch replacement equivalent for this code?{rn_text}", expected, owner("reviewer"))
    if notes["state"] == "used":
        important_ids = {it.get("id") for it in important}
        for n in items(get(notes, "doc", "items")):
            if n.get("result") not in ("contradicted", "not-found") or n.get("ref") in important_ids:
                continue
            add_item("release-note", s(n.get("ref")), f"Release notes {n.get('result')} for {n.get('artifact')}: {n.get('claim')} Source: "
                                                      f"{s(get(n, 'source', 'url'))}",
                     "Not applicable: a statement about the library, not a master result.", owner("reviewer"))

    # Dependencies: unnecessary or undecided additions, removals master used, and major version conflicts.
    dep_diff = art("dependency-diff.json")
    if dep_diff:
        for x in items(dep_diff.get("assessment")):
            if x.get("assessment") not in ("unnecessary", "review") or x.get("change") == "version":
                continue
            expected = f"Master builds without {x['ga']}." if x.get("change") == "added" else f"Master declares {x['ga']} {x.get('version')}."
            add_item("dependency", x["ga"], f"{x.get('change')} dependency {x['ga']} ({x.get('assessment')}): {x.get('reason')}. "
                                            f"{x.get('impact')} Keep it?", expected, owner("reviewer"))
        for c in items(dep_diff.get("conflicts")):
            if not c.get("major"):
                continue
            by = ", ".join(as_list(c.get("requiredBy")))
            add_item("dependency-conflict", c["ga"], f"{c['ga']}: {by} asks for {c.get('requested')}, but {c.get('resolved')} is resolved "
                                                     f"(major difference). Does {by} work with {c.get('resolved')}?",
                     "Not applicable: a change_branch resolution.", owner("reviewer"))

    # Changed main code that no test executes (unit or integration).
    for m in items(get(art("changed-methods.json"), "methods")):
        if m.get("tested") or m.get("kind") == "file":
            continue
        base_cov = get(m, "coverage", "base")
        expected = (f"Master's tests execute it ({base_cov} covered instructions)." if base_cov is not None and base_cov > 0
                    else "Master's tests do not execute it either.")
        add_item("untested-change", f"{m.get('file')}#{m.get('name')}", f"Changed {m.get('kind')} {m.get('name')} in {m.get('file')} is "
                                                                       f"not executed by any test on change_branch. {m.get('reason')} "
                                                                       "Write a test, or accept the risk.", expected, owner("reviewer"))

    for e in items(behavior.get("blockingErrors")):
        add_item("stage-error", "stage5", f"A check could not run: {e}. Fix the cause and run stage 5 again.",
                 "Not applicable: the check did not produce a result.", owner("reviewer"))

    write_json(os.path.join(artifacts, "manual-items.json"), {
        "schemaVersion": 1, "toolVersion": TOOL_VERSION, "runId": run_id,
        "summary": {"items": len(found), "byKind": {name: len([i for i in found if i["kind"] == kind]) for name, kind in KINDS}},
        "items": found,
    })
    log(f"manual items: {len(found)}")
    for i in found:
        log(f"  {i['id']} [{i['kind']}] {i['ref']}")

    set_stage_step(status, "manual", "done", "", os.path.join(artifacts, "manual-items.json"))
    set_run_state(status, "phase7-done", "phase7")
    write_json(status_file, status)
    log(f"phase 7 done: manual items written for run {run_id}")
    return 0


if __name__ == "__main__":
    run_main(main)

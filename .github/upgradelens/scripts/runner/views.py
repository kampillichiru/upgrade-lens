"""Read-only views of a run. They change nothing.

    status   every phase: state, quality, when, its jobs; the first thing to look at; the next command
    why      one phase: what failed and why (exit code in words, missing inputs and who writes them, the raw
             output, the stage's hint, the agent's rejected checks), what is stale, the quality notes, the fix
    needs    what a phase needs, whether each input is there, and which phase writes it
    funnel   where results are lost: changed methods -> findings -> gaps -> tests -> validated -> reviewed
"""

from __future__ import annotations

import collections
import os

from lib.analysis import needs_repair, proves_change
from lib.core import get, items, native_path, read_json_safe, s, tail, to_int
from lib.stages import stage_logs, stage_spec

from .jobs import UL_EXIT
from .registry import UL, Phase, producer, producer_phase
from .state import all_states, phase_dir, phase_state


def _ago(st: dict) -> str:
    return s(st.get("finishedAt") or st.get("startedAt"))[5:16].replace("T", " ") or "-"


def next_step(states: list[dict], run_id: str) -> str:
    for x in states:
        p, state = x["phase"], x["state"]
        if state in ("done",):
            continue
        if state == "waiting":
            return f"{p.command} --run {run_id}   (agent job waiting; Copilot agent: {p.agent})"
        if state in ("failed", "interrupted"):
            return f"{UL} why {p.name} --run {run_id}   (then fix and run {p.command})"
        if state == "stale":
            return f"{p.command} --run {run_id}   (its inputs changed since it ran)"
        if state == "blocked":
            src = producer_phase(x["missing"][0]) if x["missing"] else None
            if src and src is not p:
                return f"{src.command} --run {run_id}   (writes {x['missing'][0]})"
        return f"{p.command} --run {run_id}   (Copilot agent: {p.agent})"
    return f"done: read runs\\{run_id}\\reports\\summary.md"


def status_lines(runs_dir: str, run_id: str) -> list[str]:
    run_dir = os.path.join(runs_dir, run_id)
    cfg = read_json_safe(os.path.join(run_dir, "config", "upgrade-lens.json")) or {}
    states = all_states(run_dir)
    out = [f"UpgradeLens run {run_id}   {cfg.get('baseBranch', '?')} -> {cfg.get('headBranch', '?')}   ({run_dir})",
           f"{'PHASE':<12} {'STATE':<12} {'QUALITY':<8} {'WHEN':<12} JOBS"]
    first_note = None
    for x in states:
        p, st = x["phase"], x["status"]
        jobs = st.get("jobs") or {}
        jl = " ".join(f"{j.name}:{(jobs.get(j.name) or {}).get('state', '-')}" for j in p.jobs)
        q = (x["quality"] or {}).get("status", "-") if x["state"] in ("done", "stale") else "-"
        out.append(f"{p.id:<12} {x['state']:<12} {q:<8} {_ago(st):<12} {jl}")
        if x["state"] == "stale":
            out.append(f"{'':<12} changed since it ran: {', '.join(x['stale'][:3])}")
        if x["state"] == "blocked" and x["missing"]:
            out.append(f"{'':<12} needs {x['missing'][0]} (from {producer(x['missing'][0])})")
        if first_note is None and q in ("amber", "red"):
            first_note = (p, (x["quality"] or {}).get("notes", [])[:3])
    if first_note:
        out.append(f"first phase to check: {first_note[0].id}")
        out += [f"  ! {n}" for n in first_note[1]]
    out.append(f"next: {next_step(states, run_id)}")
    return out


def needs_lines(runs_dir: str, run_id: str, phase: Phase) -> list[str]:
    run_dir = os.path.join(runs_dir, run_id)
    out = [f"{phase.id} ({phase.title}) needs, in run {run_id}:"]
    for rel in phase.needs():
        here = os.path.isfile(os.path.join(run_dir, native_path(rel)))
        out.append(f"  [{'ok' if here else 'MISSING'}] {rel}" + ("" if here else f"   <- {producer(rel)}"))
    opt = phase.optional()
    if opt:
        out.append("  optional (used when present):")
        for rel in opt:
            here = os.path.isfile(os.path.join(run_dir, native_path(rel)))
            out.append(f"  [{'ok' if here else '--'}] {rel}" + ("" if here else f"   <- {producer(rel)}"))
    out.append(f"  writes: {', '.join(phase.writes())}")
    out.append(f"  run: {phase.command} --run {run_id}   (Copilot agent: {phase.agent})")
    return out


def why_lines(runs_dir: str, run_id: str, phase: Phase, n_lines: int = 20) -> list[str]:
    run_dir = os.path.join(runs_dir, run_id)
    x = phase_state(run_dir, phase)
    st, state = x["status"], x["state"]
    out = [f"{phase.id}: {state}" + (f" (exit {st.get('exitCode')}: {UL_EXIT.get(st.get('exitCode'), '')})"
                                     if st.get("exitCode") is not None else "")]
    if state in ("not-run", "blocked"):
        out += needs_lines(runs_dir, run_id, phase)[1:]
    jobs = st.get("jobs") or {}
    for j in phase.jobs:
        rec = jobs.get(j.name) or {}
        if rec.get("state") not in ("failed", "waiting"):
            continue
        out.append(f"job {j.name} ({j.kind}): {rec.get('state')} - {s(rec.get('reason'))}")
        for m in rec.get("missing") or []:
            out.append(f"  missing {m}  <- {producer(m)}")
        if j.kind == "script":
            spec = stage_spec(j.stage)
            if rec.get("stageExit") is not None:
                from lib.stages import exit_meaning
                out.append(f"  {spec.script} exit {rec.get('stageExit')}: {exit_meaning(rec.get('stageExit'), j.stage)}")
            if spec.hint:
                out.append(f"  hint: {spec.hint}")
            tool_log = s(rec.get("toolLog"))
            if tool_log and os.path.isfile(tool_log):
                out.append(f"  raw output (last {n_lines} lines of {tool_log}):")
                out += [f"    | {line}" for line in tail(tool_log, n_lines)]
            for lg in stage_logs(run_dir, j.stage)[:2]:
                out.append(f"  stage log (last 8 lines of {lg}):")
                out += [f"    | {line}" for line in tail(lg, 8)]
        if j.kind == "agent":
            checks = read_json_safe(os.path.join(phase_dir(run_dir, phase), j.name, "checks.json")) or {}
            if checks.get("guard"):
                out.append("  files changed outside the agent's out folder (undo them, then run the phase again):")
                out += [f"    - {g}" for g in checks["guard"][:20]]
            if checks.get("errors"):
                out.append(f"  check errors of attempt {checks.get('attempt')}:")
                out += [f"    - {e}" for e in checks["errors"][:20]]
            if rec.get("brief"):
                out.append(f"  brief: {rec.get('brief')}")
    if x["stale"]:
        out.append("stale: these inputs changed since the phase ran:")
        out += [f"  - {r}  <- {producer(r)}" for r in x["stale"]]
    notes = (x["quality"] or {}).get("notes") or []
    if notes and state in ("done", "stale"):
        out.append(f"quality {(x['quality'] or {}).get('status')}:")
        out += [f"  {n}" for n in notes]
    out.append(f"log: {os.path.join(phase_dir(run_dir, phase), 'log.txt')}")
    if state in ("failed", "interrupted", "stale", "waiting", "not-run", "blocked"):
        out.append(f"then: {phase.command} --run {run_id}")
    return out


def funnel_lines(runs_dir: str, run_id: str) -> list[str]:
    a = os.path.join(runs_dir, run_id, "artifacts")

    def r(name):
        return read_json_safe(os.path.join(a, native_path(name)))
    cm = get(r("changed-methods.json"), "summary") or {}
    imp = r("impact-findings.json")
    sev = get(imp, "summary", "bySeverity") or {}
    gaps = items(get(r("test-gaps.json"), "gaps"))
    man = r("agent/tests-manifest.json")
    tests = items(get(r("behavior-results.json"), "tests"))
    rv = items(get(r("agent/review.json"), "reviews"))
    by_type = collections.Counter(s(g.get("type")) for g in gaps)
    written = len(items(get(man, "tests")))
    validated = len([t for t in tests if t.get("validated")])
    passed = len([x for x in rv if x.get("verdict") == "pass"])
    rows = [
        ("changed main-code methods and types", to_int(cm.get("symbols")), ""),
        ("  executed by an existing test", to_int(cm.get("tested")), f"{to_int(cm.get('untested'))} not"),
        ("findings, high or medium", to_int(sev.get("high")) + to_int(sev.get("medium")),
         f"{to_int(get(imp, 'summary', 'findings'))} in all"),
        ("gaps for the test writer", len(gaps), ", ".join(f"{k} {v}" for k, v in sorted(by_type.items()))),
        ("tests written", written, f"{len(items(get(man, 'uncovered')))} gap(s) left uncovered"),
        ("tests that validated", validated, f"{len([t for t in tests if needs_repair(t)])} did not"),
        ("tests proving a behavior change", len([t for t in tests if proves_change(t)]), "pass on base, fail on head"),
        ("tests passed by the reviewer", passed, f"{len(rv) - passed} failed review"),
    ]
    out = [f"Funnel of run {run_id} (where results drop):"]
    out += [f"  {label:<40} {n:>5}   {note}" for label, n, note in rows]
    hints = []
    if gaps and written == 0:
        hints.append("gaps but no tests: 04-tests job 'write' (test writer) did not run or was skipped")
    elif gaps and written and len(items(get(man, "uncovered"))) > len(gaps) / 3:
        hints.append("many gaps uncovered: read the reasons in tests-manifest.json (test writer, .github/upgradelens/jobs/test-writer.md)")
    if tests and validated < len(tests) / 2:
        hints.append("most tests did not validate: test writer quality, or the build environment (04-tests, tools\\validate.log)")
    if rv and passed < len(rv) / 2:
        hints.append("most tests failed review: read 05-review\\review\\out\\review-answer.json")
    if to_int(cm.get("symbols")) and to_int(cm.get("tested")) == 0:
        hints.append("no changed method is executed by a test: coverage is missing (01-build quality)")
    out += [f"  -> {h}" for h in hints] or ["  -> no large drop found"]
    return out

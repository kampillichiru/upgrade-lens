"""UpgradeLens orchestration: the fixed plan of a run, the decision of the next step, the test gaps, and the
guards around each agent step.

next-step.py, run-stage.py and agent-step.py use this module. The decision and validation functions are
pure: they take values and return values, so tests\\test_orchestration.py checks them without a build, a
server or an agent.

The orchestrator (an agent) never decides the order. It asks next-step.py, does exactly that one action,
and asks again.
"""

from __future__ import annotations

import os
import re

from .analysis import finding_status
from .core import (IS_WINDOWS, as_list, file_sha256, get, items, like, now_iso, ps_sorted, read_json_safe, read_text, sort_key,
                   run_native, s, to_int, unique, unique_sorted, write_json)

AGENT_FILES = {
    "analyst": {"agent": "upgradelens-analyst", "output": "hypotheses.json", "allowed": ["hypotheses.json"]},
    "release-notes": {"agent": "upgradelens-release-notes", "output": "release-notes.json", "allowed": ["release-notes.json"]},
    "tests": {"agent": "upgradelens-test-writer", "output": "tests-manifest.json", "allowed": ["tests-manifest.json", "tests\\*"]},
    "reviewer": {"agent": "upgradelens-test-reviewer", "output": "review-answer.json", "allowed": ["review-answer.json"]},
}

STAGE_SCRIPTS = {
    0: "phase0-setup.py", 1: "phase1-build.py", 2: "phase2-changes.py", 3: "phase3-deps.py",
    4: "phase4-impact.py", 5: "phase5-behavior.py", 6: "phase6-flows.py", 7: "phase7-manual.py",
    8: "phase8-output.py", 9: "phase9-review.py", 10: "phase10-verdict.py",
}


# ---------- the plan ----------

def run_plan(max_repair_rounds: int = 1) -> list[dict]:
    """The ordered steps of an orchestrated run. Every step is either run, or skipped with a recorded reason
    (a step that is not needed). Nothing is left out silently."""
    plan = []

    def add(key, kind, stage=None, step=None, title=""):
        plan.append({"key": key, "kind": kind, "stage": stage, "step": step, "title": title})

    add("stage-0", "stage", 0, None, "setup: config check, clone, worktrees, merge base")
    add("stage-1", "stage", 1, None, "build and test both branches")
    add("stage-2", "stage", 2, None, "changed files and methods")
    add("stage-3", "stage", 3, None, "dependencies, application API and library API")
    add("agent-release-notes", "agent", None, "release-notes", "validate library changes against release notes")
    add("stage-4", "stage", 4, None, "impact rules, flows, reach, coverage")
    add("agent-analyst", "agent", None, "analyst", "hypotheses, plain explanations, reason for each changed method")
    add("stage-4-merge", "stage", 4, None, "impact again, with the analyst's hypotheses and reasons")
    add("stage-5-probes", "stage", 5, None, "startup, dumps and probes on both branches")
    add("agent-tests", "agent", None, "tests", "a test for every gap (untested change, untraced flow, unverified impact)")
    add("stage-5-tests", "stage", 5, None, "run the new tests on both branches, with injected regressions")
    for k in range(1, max_repair_rounds + 1):
        add(f"agent-tests-repair-{k}", "agent", None, "tests", f"repair the tests that did not validate (round {k})")
        add(f"stage-5-repair-{k}", "stage", 5, None, f"run the repaired tests on both branches (round {k})")
    add("stage-6", "stage", 6, None, "flow selection")
    add("stage-7", "stage", 7, None, "manual review items")
    add("stage-8", "stage", 8, None, "report; validated tests placed uncommitted")
    add("agent-reviewer", "agent", None, "reviewer", "independent review of the validated tests")
    add("stage-9", "stage", 9, None, "agent review and manual decisions")
    add("stage-10", "stage", 10, None, "verdict")
    return plan


def stage_script(stage: int) -> str:
    return STAGE_SCRIPTS[int(stage)]


def ledger_entry(ledger, key: str):
    """The last entry for a key, or None."""
    hits = [e for e in as_list(get(ledger, "entries")) if e and e.get("key") == key]
    return hits[-1] if hits else None


def step_needed(step: dict, ledger, facts: dict) -> dict:
    """Whether a plan step must run, from the facts of the run at the time the step is reached.
    Returns {needed; reason}. The reason is recorded for a skipped step."""
    def done(k):
        e = ledger_entry(ledger, k)
        return e is not None and e.get("state") == "done"

    key = step["key"]
    f = lambda name: to_int(facts.get(name))  # noqa: E731
    if key == "agent-release-notes":
        if f("notesNeeded") > 0:
            return {"needed": True, "reason": f"{facts.get('notesNeeded')} library change(s) or major direct dependency change(s) to validate"}
        return {"needed": False, "reason": "no high or medium library API change and no major direct dependency change"}
    if key == "agent-analyst":
        if f("inventoryFiles") > 0:
            return {"needed": True, "reason": f"{facts.get('inventoryFiles')} changed file(s)"}
        return {"needed": False, "reason": "no changed file between the branches"}
    if key == "stage-4-merge":
        if done("agent-analyst"):
            return {"needed": True, "reason": "merge the analyst proposal"}
        return {"needed": False, "reason": "the analyst did not run"}
    if key == "agent-tests":
        if f("gapCount") > 0:
            return {"needed": True, "reason": f"{facts.get('gapCount')} gap(s) in artifacts\\test-gaps.json"}
        return {"needed": False, "reason": "no gap: every changed method and reached symbol is executed by a test, and every "
                                           "high or medium impact is checked"}
    if key == "stage-5-tests":
        if done("agent-tests"):
            return {"needed": True, "reason": "run the new tests"}
        return {"needed": False, "reason": "no tests were written"}
    m = re.match(r"^agent-tests-repair-(\d+)$", key)
    if m:
        k = int(m.group(1))
        prev = "stage-5-tests" if k == 1 else f"stage-5-repair-{k - 1}"
        if not done(prev):
            return {"needed": False, "reason": "no tests ran in the previous step"}
        if f("testsNotValidated") > 0:
            return {"needed": True, "reason": f"{facts.get('testsNotValidated')} test(s) did not validate"}
        return {"needed": False, "reason": "every test validated"}
    m = re.match(r"^stage-5-repair-(\d+)$", key)
    if m:
        if done(f"agent-tests-repair-{m.group(1)}"):
            return {"needed": True, "reason": "run the repaired tests"}
        return {"needed": False, "reason": "no repair"}
    if key == "agent-reviewer":
        n = f("validatedTests") + f("regressionsFound")
        if n > 0:
            return {"needed": True, "reason": f"{f('validatedTests')} validated test(s) and {f('regressionsFound')} test(s) that "
                                              "prove a behavior change, to review"}
        return {"needed": False, "reason": "no validated test and no test that proves a behavior change"}
    return {"needed": True, "reason": ""}


def next_action(plan, ledger, facts: dict, max_agent_attempts: int = 2) -> dict:
    """The one next action of a run: run-stage, run-agent, end-agent, wait, skip, stop or done.
    Pure: the ledger, the facts and the limits in, one action out.
      skip      the caller records the skip and asks again
      end-agent an agent step was begun and never ended: run its end guard
      stop      a stage failed, an agent failed its guard too often, the budget is spent, or a guard tripped"""
    if facts.get("lockActive"):
        return {"action": "wait", "key": facts.get("lockKey"),
                "reason": f"stage {facts.get('lockKey')} is running (pid {facts.get('lockPid')})"}
    for st in plan:
        e = ledger_entry(ledger, st["key"])
        if e and e.get("state") in ("done", "skipped"):
            continue
        if e and e.get("state") == "running":
            if st["kind"] == "agent":
                return {"action": "end-agent", "key": st["key"], "step": st["step"],
                        "reason": "the agent step was begun and not ended"}
            return {"action": "stop", "key": st["key"],
                    "reason": f"stage {st['key']} is marked running but no runner holds the lock: the runner died. Start a new run."}
        if e and e.get("state") == "failed":
            reason = s(e.get("reason"))
            if st["kind"] == "stage":
                return {"action": "stop", "key": st["key"],
                        "reason": f"stage {st['key']} failed (exit {e.get('exitCode')}): {reason}"}
            if like(reason, "budget:*"):
                return {"action": "stop", "key": st["key"], "reason": reason}
            if like(reason, "guard:*"):
                return {"action": "stop", "key": st["key"],
                        "reason": f"agent {st['key']} wrote outside its allowed files: {reason}. The run is stopped; start a new run."}
            attempts = len([x for x in as_list(get(ledger, "entries")) if x and x.get("key") == st["key"] and x.get("state") == "failed"])
            if attempts >= max_agent_attempts:
                return {"action": "stop", "key": st["key"],
                        "reason": f"agent step {st['key']} failed its checks {attempts} time(s). Last: {reason}"}
            return {"action": "run-agent", "key": st["key"], "step": st["step"], "attempt": attempts + 1,
                    "reason": f"retry after: {reason}"}
        need = step_needed(st, ledger, facts)
        if not need["needed"]:
            return {"action": "skip", "key": st["key"], "reason": need["reason"]}
        if st["kind"] == "stage":
            return {"action": "run-stage", "key": st["key"], "stage": st["stage"], "reason": st["title"]}
        return {"action": "run-agent", "key": st["key"], "step": st["step"], "attempt": 1, "reason": need["reason"]}
    return {"action": "done", "key": "", "reason": "every step ran or was skipped with a reason"}


# ---------- facts ----------

def needs_repair(test: dict) -> bool:
    """A proposed test is broken when it fails on master (it does not pin master's behavior), or when it passes
    on both branches but misses an injected regression (it detects nothing). A test that passes on master and
    fails on change_branch is not broken: it proves a behavior change, and is kept as evidence."""
    if not get(test, "base", "passed"):
        return True
    if get(test, "head", "passed") and not test.get("validated"):
        return True
    return False


def release_notes_refs(lib_api, dep_diff) -> list[str]:
    """The library changes the release-notes agent must cover: high or medium library API items, and direct
    dependencies that changed by a major version. Stage 10 uses the same list."""
    refs = []
    if lib_api and lib_api.get("available"):
        refs += [s(i.get("id")) for i in items(lib_api.get("items")) if s(i.get("severity")) in ("high", "medium")]
    if dep_diff:
        refs += [s(c.get("ga")) for c in items(dep_diff.get("changed")) if c.get("direct") and c.get("major")]
    return unique(refs)


def find_test_gaps(changed_methods, reach, impact, checks=()) -> list[dict]:
    """Every item a test must cover, or the test writer must explain why it cannot (requirement: write a test
    for each potential impact). Ids are stable for one input: G-01, G-02, ... in this order:
      untested-change     a changed main-code method or type that no test executes on change_branch
      untraced-flow       a flow that reaches code no test executes
      unverified-impact   a high or medium finding with production scope that no probe or test checked"""
    rows = []
    methods = [m for m in items(get(changed_methods, "methods")) if not m.get("tested") and m.get("kind") != "file"]
    for m in sorted(methods, key=lambda m: (sort_key(m.get("file")), sort_key(m.get("name")))):
        rows.append({"type": "untested-change", "ref": f"{m.get('file')}|{m.get('kind')}:{m.get('name')}", "severity": "high",
                     "detail": f"changed {m.get('kind')} {m.get('name')} in {m.get('file')} is not executed by any test on change_branch"})
    for fl in ps_sorted([f for f in items(get(reach, "flows")) if as_list(f.get("untraced"))], key=lambda f: f.get("id")):
        rows.append({"type": "untraced-flow", "ref": s(fl.get("id")), "severity": "high",
                     "detail": f"flow {fl.get('id')} reaches code no test executes: {', '.join(s(x) for x in as_list(fl.get('untraced')))}"})
    for f in ps_sorted(items(get(impact, "findings")), key=lambda f: f.get("id")):
        if s(f.get("severity")) not in ("high", "medium"):
            continue
        if s(f.get("scope")) not in ("global", "symbol"):
            continue
        if finding_status(f, as_list(checks)) != "open":
            continue
        rows.append({"type": "unverified-impact", "ref": s(f.get("id")), "severity": s(f.get("severity")),
                     "detail": f"{f.get('id')} [{f.get('ruleId')}] {f.get('title')}: {f.get('impact')}"})
    return [{"id": f"G-{i:02d}", **r} for i, r in enumerate(rows, start=1)]


# ---------- agent output checks (pure) ----------

def non_empty(value) -> bool:
    return value is not None and s(value).strip() != ""


def check_analyst_proposal(doc, expected_hash: str, inventory, impact) -> list[str]:
    """Errors in the analyst proposal. Empty list = accepted.
    Requires: matching input hash; at most 8 hypotheses, each with a plain explanation, an example and the
    production risk; a reason for every changed main-code method or type (including added ones); a plain
    explanation for every rule behind a high or medium finding."""
    err = []
    if not isinstance(doc, dict):
        return ["hypotheses.json is missing or not valid JSON"]
    if s(doc.get("agent")) != "upgradelens-analyst":
        err.append("agent must be 'upgradelens-analyst'")
    if s(doc.get("inputHash")) != s(expected_hash):
        err.append(f"inputHash '{s(doc.get('inputHash'))}' does not match the inputs ({expected_hash})")
    hyps = items(doc.get("hypotheses"))
    if len(hyps) > 8:
        err.append(f"{len(hyps)} hypotheses; at most 8")
    ids = set()
    for h in hyps:
        hid = s(h.get("id"))
        if not re.search(r"^H-\d{2,}$", hid, re.I):
            err.append(f"hypothesis id '{hid}' must look like H-01")
        if hid in ids:
            err.append(f"duplicate hypothesis id {hid}")
        ids.add(hid)
        for field in ("claim", "plain", "example", "productionRisk", "probeIdea"):
            if not non_empty(h.get(field)):
                err.append(f"hypothesis {hid} has no {field}")
        if s(h.get("severity")) not in ("high", "medium", "low"):
            err.append(f"hypothesis {hid} severity must be high, medium or low")
        if s(h.get("scope")) not in ("global", "symbol", "none"):
            err.append(f"hypothesis {hid} scope must be global, symbol or none")
        if s(h.get("scope")) == "symbol" and not any("#" in s(x) for x in as_list(h.get("symbols"))):
            err.append(f"hypothesis {hid} has scope symbol but no Class#method symbol")
    reasons = {}
    for r in items(doc.get("changeReasons")):
        reasons[f"{r.get('file')}|{r.get('symbol')}"] = r
        for field, label in (("reason", "is empty"), ("evidence", "has no evidence"), ("consequence", "has no consequence")):
            if not non_empty(r.get(field)):
                err.append(f"change reason for {r.get('file')} {r.get('symbol')} {label}")
    for f in items(get(inventory, "files")):
        if f.get("category") != "main-code":
            continue
        for sy in items(f.get("symbols")):
            if f"{f.get('path')}|{sy.get('symbol')}" not in reasons:
                err.append(f"no change reason for {f.get('path')} {sy.get('symbol')}")
    expl = {}
    for x in items(doc.get("explanations")):
        expl[s(x.get("ruleId"))] = x
        for field in ("plain", "example", "productionRisk"):
            if not non_empty(x.get(field)):
                err.append(f"explanation for {x.get('ruleId')} has no {field}")
    rules = unique([s(f.get("ruleId")) for f in items(get(impact, "findings"))
                    if s(f.get("severity")) in ("high", "medium") and s(f.get("source")) != "analyst"])
    for rid in rules:
        if rid not in expl:
            err.append(f"no plain explanation for rule {rid} (a high or medium finding)")
    return err


def check_release_notes_proposal(doc, expected_hash: str, needed_refs) -> list[str]:
    err = []
    if not isinstance(doc, dict):
        return ["release-notes.json is missing or not valid JSON"]
    if s(doc.get("agent")) != "upgradelens-release-notes":
        err.append("agent must be 'upgradelens-release-notes'")
    if s(doc.get("inputHash")) != s(expected_hash):
        err.append(f"inputHash '{s(doc.get('inputHash'))}' does not match the inputs ({expected_hash})")
    by_ref = {}
    for i in items(doc.get("items")):
        by_ref[s(i.get("ref"))] = i
        res = s(i.get("result"))
        if res not in ("confirmed", "contradicted", "not-found"):
            err.append(f"item {i.get('ref')}: result must be confirmed, contradicted or not-found")
            continue
        if res != "not-found":
            if not re.search(r"^https://", s(get(i, "source", "url")), re.I):
                err.append(f"item {i.get('ref')} is {res} but has no https source url")
            if not non_empty(i.get("quote")):
                err.append(f"item {i.get('ref')} is {res} but has no quote")
            elif len(s(i.get("quote")).split()) > 40:
                err.append(f"item {i.get('ref')}: the quote is longer than 40 words")
    for r in as_list(needed_refs):
        if s(r) not in by_ref:
            err.append(f"no item for {r}")
    return err


def _occurrences(path: str, text: str) -> int:
    return read_text(path).count(text) if text else 0


def check_tests_proposal(doc, expected_hash: str, gaps, tests_dir: str | None, head_worktree: str | None,
                        max_tests: int = 15) -> list[str]:
    """Errors in the test writer's manifest. Every gap must be covered by a test or listed in 'uncovered' with a
    reason. Each test file must exist where the manifest says, under src/test, with a class name that matches
    the file, and each injection must hit exactly one place in the head source."""
    err = []
    if not isinstance(doc, dict):
        return ["tests-manifest.json is missing or not valid JSON"]
    if s(doc.get("writer")) != "upgradelens-test-writer":
        err.append("writer must be 'upgradelens-test-writer'")
    if s(doc.get("inputHash")) != s(expected_hash):
        err.append(f"inputHash '{s(doc.get('inputHash'))}' does not match the inputs ({expected_hash})")
    gap_ids = {s(g.get("id")) for g in items(gaps)}
    covered = set()
    tests = items(doc.get("tests"))
    if len(tests) > max_tests:
        err.append(f"{len(tests)} tests; at most {max_tests} (orchestration.maxTests)")
    seen, paths = set(), set()
    for t in tests:
        tid = s(t.get("id"))
        if not re.search(r"^T-\d{2,}$", tid, re.I):
            err.append(f"test id '{tid}' must look like T-01")
        if tid in seen:
            err.append(f"duplicate test id {tid}")
        seen.add(tid)
        rel = s(t.get("relativePath"))
        if not re.search(r"^src/test/", rel, re.I) or not re.search(r"\.java$", rel, re.I) or ".." in rel or "\\" in rel:
            err.append(f"test {tid}: relativePath '{rel}' must be a forward-slash path under src/test/ ending in .java")
            continue
        paths.add(rel.lower())
        leaf = os.path.splitext(rel.split("/")[-1])[0]
        if s(t.get("className")) != leaf:
            err.append(f"test {tid}: className '{s(t.get('className'))}' does not match the file name '{leaf}'")
        if tests_dir and not os.path.isfile(os.path.join(tests_dir, *rel.split("/"))):
            err.append(f"test {tid}: file not found under artifacts\\agent\\tests: {rel}")
        if not non_empty(t.get("purpose")):
            err.append(f"test {tid} has no purpose")
        tg = items(t.get("gapIds"))
        if not tg:
            err.append(f"test {tid} names no gapIds")
        for g in tg:
            if s(g) not in gap_ids:
                err.append(f"test {tid} names unknown gap {g}")
            else:
                covered.add(s(g))
        inj = items(t.get("injections"))
        if not inj:
            err.append(f"test {tid} has no injected regression")
        for i in inj:
            if not non_empty(i.get("find")) or s(i.get("find")) == s(i.get("replace")):
                err.append(f"test {tid}: an injection has an empty find or a replace equal to find")
                continue
            if not non_empty(i.get("note")):
                err.append(f"test {tid}: an injection has no note")
            f = s(i.get("file"))
            if ".." in f or not re.search(r"^src/main/", f, re.I):
                err.append(f"test {tid}: injection file '{f}' must be under src/main/")
                continue
            if head_worktree:
                p = os.path.join(head_worktree, *f.split("/"))
                if not os.path.isfile(p):
                    err.append(f"test {tid}: injection file not found on change_branch: {f}")
                    continue
                n = _occurrences(p, s(i.get("find")))
                if n != 1:
                    err.append(f"test {tid}: injection text occurs {n} time(s) in {f}; it must occur exactly once")
    for u in items(doc.get("uncovered")):
        gid = s(u.get("gapId"))
        if gid not in gap_ids:
            err.append(f"uncovered names unknown gap {u.get('gapId')}")
            continue
        if len(s(u.get("reason")).strip()) < 20:
            err.append(f"uncovered gap {gid} needs a reason of at least 20 characters")
            continue
        covered.add(gid)
    for g in ps_sorted(gap_ids):
        if g not in covered:
            err.append(f"gap {g} is neither covered by a test nor listed in 'uncovered' with a reason")
    if tests_dir and os.path.isdir(tests_dir):
        for d, _dirs, files in os.walk(tests_dir):
            for name in files:
                rel = os.path.relpath(os.path.join(d, name), tests_dir).replace("\\", "/")
                if rel.lower() not in paths:
                    err.append(f"file in artifacts\\agent\\tests is not in the manifest: {rel}")
    return err


def check_review_answer(doc, test_ids) -> list[str]:
    """Errors in the reviewer's answer: a pass or fail with notes for every validated test, no unknown test."""
    err = []
    if not isinstance(doc, dict):
        return ["review-answer.json is missing or not valid JSON"]
    ids = [s(t) for t in as_list(test_ids)]
    by = {}
    for r in items(doc.get("reviews")):
        tid = s(r.get("testId"))
        by[tid] = r
        if s(r.get("verdict")) not in ("pass", "fail"):
            err.append(f"review of {tid}: verdict must be pass or fail")
        if not non_empty(r.get("notes")):
            err.append(f"review of {tid} has no notes")
        if tid not in ids:
            err.append(f"review names unknown or unvalidated test {tid}")
    for t in ids:
        if t not in by:
            err.append(f"no review for {t}")
    return err


# ---------- write scope (pure part) ----------

def compare_fingerprint(before: dict, after: dict) -> list[str]:
    """Keys added, removed or changed between two fingerprints (dicts key -> hash)."""
    changed = [k for k in before if k not in after or after[k] != before[k]]
    changed += [k for k in after if k not in before]
    return unique_sorted(changed)


def agent_write_scope(changed_agent_files, step: str) -> list[str]:
    """Files under artifacts\\agent that one step may create or change. Everything else it touched is a violation."""
    allowed = [a.replace("\\", "/") for a in AGENT_FILES[step]["allowed"]]
    return [c for c in as_list(changed_agent_files)
            if not any(like(s(c).replace("\\", "/"), a) for a in allowed)]


# ---------- fingerprints (file system) ----------

def _excluded(rel: str, patterns) -> bool:
    r = rel.replace("\\", "/")
    return any(like(r, p.replace("\\", "/")) for p in patterns)


def file_tree_hashes(root: str, exclude=(), prefix: str = "") -> dict:
    """relative path -> SHA-256 for every file under root (OS path separators in the keys), skipping relative
    paths that match an exclude pattern (wildcards, / or \\)."""
    result = {}
    if not os.path.isdir(root):
        return result
    root_full = os.path.abspath(root)
    patterns = list(exclude)
    for d, dirs, files in os.walk(root_full):
        rel_dir = os.path.relpath(d, root_full)
        rel_dir = "" if rel_dir == "." else rel_dir
        # A folder whose every path is excluded ("worktrees\*") is not walked at all.
        dirs[:] = [x for x in dirs if not any(p.endswith("*") and _excluded(os.path.join(rel_dir, x) + os.sep, [p]) for p in patterns)]
        for name in files:
            rel = os.path.join(rel_dir, name) if rel_dir else name
            if _excluded(rel, patterns):
                continue
            try:
                result[prefix + rel] = file_sha256(os.path.join(d, name))
            except OSError:
                result[prefix + rel] = "unreadable"
    return result


def protected_fingerprint(run_dir: str, tool_root: str, git_cmd: str = "git") -> dict:
    """What an agent must not change: the run folder outside artifacts\\agent (status, config snapshot, stage
    artifacts such as test-gaps.json, reports, review decisions), the git state of both worktrees, and the
    tool itself (scripts, config, agent and skill definitions). The orchestration folder is written by the
    guards themselves around the agent; its guard record is protected by the hash kept in the ledger.
    Python bytecode caches are not part of the tool."""
    fp = file_tree_hashes(run_dir, prefix="run:", exclude=["artifacts\\agent\\*", "worktrees\\*", "repo.git\\*",
                                                             "logs\\*", "orchestration\\*"])
    for d in ("scripts", "config", ".github", ".claude"):
        part = file_tree_hashes(os.path.join(tool_root, d), prefix=f"tool:{d}{os.sep}",
                                exclude=["__pycache__\\*", "*\\__pycache__\\*", "*.pyc"])
        fp.update(part)
    claude_md = os.path.join(tool_root, "CLAUDE.md")
    if os.path.isfile(claude_md):
        fp["tool:CLAUDE.md"] = file_sha256(claude_md)
    wt_root = os.path.join(run_dir, "worktrees")
    if os.path.isdir(wt_root):
        for name in ps_sorted(os.listdir(wt_root)):
            wt = os.path.join(wt_root, name)
            if not os.path.isdir(wt):
                continue
            st = run_native(git_cmd, ["-C", wt, "status", "--porcelain=v1", "--untracked-files=all"])
            hd = run_native(git_cmd, ["-C", wt, "rev-parse", "HEAD"])
            fp[f"worktree:{name}"] = "\n".join(hd.output + st.output)
    return fp


# ---------- ledger (file system) ----------
# runs\<id>\orchestration\ledger.json: one entry per attempt of a plan step, in order. Written only by
# run-stage.py, next-step.py (skips) and agent-step.py (begin and end). An agent that writes it stops the run.

def ledger_path(run_dir: str) -> str:
    return os.path.join(run_dir, "orchestration", "ledger.json")


def read_ledger(run_dir: str):
    led = read_json_safe(ledger_path(run_dir))
    if isinstance(led, dict) and not led.get("entries"):
        led["entries"] = []
    return led if isinstance(led, dict) else None


def new_ledger(run_dir: str, run_id: str, config_path: str) -> dict:
    led = {"schemaVersion": 1, "runId": run_id, "config": config_path, "createdAt": now_iso(), "entries": []}
    write_json(ledger_path(run_dir), led)
    return led


def set_ledger_entry(run_dir: str, key: str, kind: str, state: str, reason: str = "", extra: dict | None = None,
                     update: bool = False) -> None:
    """Adds an entry, or with update=True replaces the last entry of the key (the same attempt)."""
    led = read_ledger(run_dir)
    if not led:
        raise RuntimeError(f"No ledger in {run_dir}")
    entries = [e for e in as_list(led.get("entries")) if e]
    now = now_iso()
    attempt = len([e for e in entries if e.get("key") == key]) + 1
    extra = extra or {}
    if update:
        for i in range(len(entries) - 1, -1, -1):
            if entries[i].get("key") == key:
                old = entries[i]
                entry = {"key": key, "kind": kind, "attempt": old.get("attempt"), "state": state, "reason": reason,
                         "startedAt": old.get("startedAt"), "finishedAt": now}
                for k, v in old.items():
                    entry.setdefault(k, v)
                entry.update(extra)
                entries[i] = entry
                break
    else:
        entry = {"key": key, "kind": kind, "attempt": attempt, "state": state, "reason": reason, "startedAt": now,
                 "finishedAt": None if state == "running" else now}
        entry.update(extra)
        entries.append(entry)
    led["entries"] = entries
    write_json(ledger_path(run_dir), led)


def pid_alive(pid) -> bool:
    """Whether a process with this id exists."""
    pid = to_int(pid, -1)
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_path(run_dir: str) -> str:
    return os.path.join(run_dir, "orchestration", "lock.json")


def run_lock(run_dir: str) -> dict:
    """The stage lock: {active; pid; key}. A lock whose process is gone is removed (the ledger keeps the
    step as running, and next-step reports the dead runner)."""
    p = lock_path(run_dir)
    lk = read_json_safe(p)
    if not lk:
        return {"active": False, "pid": None, "key": None}
    if pid_alive(lk.get("pid")):
        return {"active": True, "pid": lk.get("pid"), "key": lk.get("key")}
    try:
        os.remove(p)
    except OSError:
        pass
    return {"active": False, "pid": lk.get("pid"), "key": lk.get("key")}


def run_facts(run_dir: str, ledger) -> dict:
    """The facts next_action needs, read from the run's artifacts. Writes artifacts\\test-gaps.json when the
    test writer is next and has not started (the gap list is fixed once the writer begins)."""
    a = os.path.join(run_dir, "artifacts")
    lock = run_lock(run_dir)
    inventory = read_json_safe(os.path.join(a, "change-inventory.json"))
    lib_api = read_json_safe(os.path.join(a, "library-api-diff.json"))
    dep_diff = read_json_safe(os.path.join(a, "dependency-diff.json"))
    behavior = read_json_safe(os.path.join(a, "behavior-results.json"))
    gaps_path = os.path.join(a, "test-gaps.json")
    gap_count = 0
    probes = ledger_entry(ledger, "stage-5-probes")
    writer = ledger_entry(ledger, "agent-tests")
    if probes and probes.get("state") == "done" and not writer:
        gaps = find_test_gaps(read_json_safe(os.path.join(a, "changed-methods.json")),
                         read_json_safe(os.path.join(a, "reach.json")),
                         read_json_safe(os.path.join(a, "impact-findings.json")),
                         as_list(get(behavior, "checks")))
        write_json(gaps_path, {"schemaVersion": 1, "runId": get(ledger, "runId"), "createdAt": now_iso(), "gaps": gaps})
        gap_count = len(gaps)
    elif os.path.isfile(gaps_path):
        gap_count = len(items(get(read_json_safe(gaps_path), "gaps")))
    tests = items(get(behavior, "tests"))
    return {
        "testsNotValidated": len([t for t in tests if needs_repair(t)]),
        "regressionsFound": len([t for t in tests if get(t, "base", "passed") and not get(t, "head", "passed")]),
        "lockActive": lock["active"],
        "lockPid": lock["pid"],
        "lockKey": lock["key"],
        "inventoryFiles": len(items(get(inventory, "files"))),
        "notesNeeded": len(release_notes_refs(lib_api, dep_diff)),
        "gapCount": gap_count,
        "validatedTests": len([t for t in tests if t.get("validated")]),
    }


# ---------- prompts ----------

def agent_prompt(step: str, run_id: str, run_dir: str, input_hash_value: str, key: str, review_test_ids=()) -> str:
    """The exact prompt the orchestrator hands to the subagent. The orchestrator passes it unchanged."""
    info = AGENT_FILES[step]
    agent = info["agent"]
    out = os.path.join(run_dir, "artifacts", "agent", info["output"])
    allowed = ", ".join(os.path.join(run_dir, "artifacts", "agent", a.replace("\\", os.sep)) for a in info["allowed"])
    lines = [
        f"UpgradeLens run {run_id}. Run folder: {run_dir}",
        f"You are {agent}. Read your agent instructions (.github\\agents\\{agent}.agent.md) first and follow them exactly.",
        f"Write only: {allowed}.",
        "Anything else you write (the worktrees, repo.git, config, the stage artifacts, the review folder, the tool's "
        "scripts) stops the run.",
        "Do not run Maven, git commands that change state, or scripts/run-stage.py, next-step.py or agent-step.py "
        "begin/end: the orchestrator records your call.",
    ]
    if step == "analyst":
        lines.append(f"Put this value in inputHash: {input_hash_value}")
        lines.append(f"Write {out} with hypotheses (each with plain, example, productionRisk), explanations (one per rule "
                     "behind a high or medium finding in artifacts\\impact-findings.json) and changeReasons (one per changed "
                     "main-code symbol, each with reason, evidence and consequence).")
    elif step == "release-notes":
        lines.append(f"Put this value in inputHash: {input_hash_value}")
        lines.append(f"Write {out} with one item for every high or medium item of artifacts\\library-api-diff.json and "
                     "every direct major dependency change of artifacts\\dependency-diff.json.")
    elif step == "tests":
        lines.append(f"Put this value in inputHash: {input_hash_value}")
        lines.append(f"Follow the skill upgradelens-missing-tests. Your work list is "
                     f"{os.path.join(run_dir, 'artifacts', 'test-gaps.json')}: cover every gap with a test (gapIds) or list "
                     "it in 'uncovered' with the reason.")
        if like(key, "agent-tests-repair-*"):
            lines.append("This is a repair round. Read artifacts\\behavior-results.json (tests[]): fix only the tests that "
                         "fail on master, and the tests that pass on both branches but miss an injected regression "
                         "(injections[].detected false). Keep the ids and the gap coverage. Do not touch a test that passes "
                         "on master and fails on change_branch: it proves a behavior change and is kept as evidence.")
    elif step == "reviewer":
        lines.append(f"Review these validated tests: {', '.join(as_list(review_test_ids))}. Write your answer (reviews with "
                     f"testId, verdict pass|fail, reviewer, notes) to {out} and nothing else.")
    return "\n".join(lines)


def agent_key_step(key: str) -> str | None:
    """The agent step of a plan key: agent-analyst -> analyst, agent-tests-repair-1 -> tests."""
    m = re.match(r"^agent-(analyst|release-notes|reviewer)$", s(key))
    if m:
        return m.group(1)
    if re.match(r"^agent-tests(-repair-\d+)?$", s(key)):
        return "tests"
    return None

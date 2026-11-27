"""Content checks of each agent's output. Pure: values in, a list of errors out (empty = accepted), so
tests\\ can call them without an agent. lib\\agents.py ties each check to its agent.
"""

from __future__ import annotations

import os
import re

from .core import as_list, get, items, ps_sorted, read_text, s, unique


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
            err.append(f"test {tid}: file not found under the tests folder: {rel}")
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
        # Each gap of the test is covered by named test methods (cases), so a failing method speaks only for its own
        # gaps and a test cannot claim a gap that none of its methods checks.
        cases = items(t.get("cases"))
        if not cases:
            err.append(f"test {tid} has no cases: list each test method with the gapIds it covers")
        source = ""
        if tests_dir and os.path.isfile(os.path.join(tests_dir, *rel.split("/"))):
            with open(os.path.join(tests_dir, *rel.split("/")), encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        case_gaps = set()
        for c in cases:
            name = s(c.get("method"))
            if not re.search(r"^[A-Za-z_$][\w$]*$", name):
                err.append(f"test {tid}: case method '{name}' is not a Java method name")
                continue
            if source and not re.search(r"\b" + re.escape(name) + r"\s*\(", source):
                err.append(f"test {tid}: case method {name} is not in {rel}")
            cg = {s(g) for g in items(c.get("gapIds"))}
            if not cg:
                err.append(f"test {tid}: case {name} names no gapIds")
            extra = cg - {s(g) for g in tg}
            if extra:
                err.append(f"test {tid}: case {name} names gap(s) {', '.join(sorted(extra))} that are not in the test's gapIds")
            case_gaps |= cg
        if cases:
            missing = {s(g) for g in tg} - case_gaps
            if missing:
                err.append(f"test {tid}: gap(s) {', '.join(sorted(missing))} are in gapIds but no case method covers them")
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
                    err.append(f"file in the tests folder is not in the manifest: {rel}")
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

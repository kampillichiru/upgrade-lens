"""A quality score per phase: green, amber or red, with the numbers behind it.

"Did it run" is not enough: a phase can finish and still produce weak results (no tests ran, no coverage, a rule
that fires on everything, an analyst that could not say why most methods changed, tests that do not validate).
The score names the first place a weak result shows, so the first phase that is not green is where to look.

    red     the phase's result cannot be trusted (for example no coverage, so changed methods cannot be judged)
    amber   usable, with something to check (listed in notes)
    green   nothing found to check
"""

from __future__ import annotations

import os

from lib.analysis import needs_repair, proves_change
from lib.core import as_list, get, items, read_json_safe, s, to_int

from .registry import Phase

RANK = {"green": 0, "amber": 1, "red": 2}


def _art(run_dir: str, name: str):
    return read_json_safe(os.path.join(run_dir, "artifacts", name.replace("/", os.sep)))


class Score:
    def __init__(self):
        self.status = "green"
        self.metrics: dict = {}
        self.notes: list[str] = []

    def flag(self, level: str, note: str) -> None:
        if RANK[level] > RANK[self.status]:
            self.status = level
        self.notes.append(f"[{level}] {note}")

    def as_dict(self) -> dict:
        return {"status": self.status, "metrics": self.metrics, "notes": self.notes}


def _setup(run_dir: str, q: Score, jobs: dict) -> None:
    env = _art(run_dir, "environment.json")
    for side in ("base", "head"):
        commit = get(env, "branches", side, "commit")
        q.metrics[f"{side}Commit"] = s(commit)[:12]
        if not commit:
            q.flag("red", f"no {side} commit in environment.json")
        wt = get(env, "branches", side, "worktree")
        if wt and not os.path.isdir(s(wt)):
            q.flag("red", f"the {side} worktree is missing: {wt}")
    mb, base = get(env, "branches", "mergeBase"), get(env, "branches", "base", "commit")
    if mb and base and mb != base:
        q.flag("amber", "the base branch moved on after the head branch was made: commits after the merge base are "
                        "not part of the check")


def _build(run_dir: str, q: Score, jobs: dict) -> None:
    br = _art(run_dir, "build-results.json")
    tr = get(_art(run_dir, "test-results.json"), "summary") or {}
    env = _art(run_dir, "environment.json")
    q.metrics.update({"buildOk": bool(get(br, "ok")), "tests": to_int(tr.get("tests")),
                      "nowFailing": to_int(tr.get("nowFailing")), "removed": to_int(tr.get("removed"))})
    if not get(br, "ok"):
        q.flag("red", "the build did not finish on both branches")
    if to_int(tr.get("tests")) == 0:
        q.flag("red", "no tests ran: changed methods cannot be judged as tested")
    if to_int(tr.get("nowFailing")):
        q.flag("amber", f"{tr.get('nowFailing')} test(s) pass on base and fail on head")
    if to_int(tr.get("removed")):
        q.flag("amber", f"{tr.get('removed')} test(s) exist on base but not on head")
    for side in ("base", "head"):
        wt = get(env, "branches", side, "worktree")
        jacoco = os.path.join(s(wt), "target", "site", "jacoco", "jacoco.xml") if wt else ""
        has = bool(jacoco) and os.path.isfile(jacoco)
        q.metrics[f"{side}Coverage"] = has
        if not has:
            q.flag("red" if side == "head" else "amber",
                   f"no JaCoCo report on {side} ({jacoco or 'no worktree'}): the build goals must run jacoco-maven-plugin")


def _impact(run_dir: str, q: Score, jobs: dict) -> None:
    inv = _art(run_dir, "change-inventory.json")
    imp = _art(run_dir, "impact-findings.json")
    cm = get(_art(run_dir, "changed-methods.json"), "summary") or {}
    reach = get(_art(run_dir, "reach.json"), "summary") or {}
    lib = get(_art(run_dir, "library-api-diff.json"), "summary") or {}
    files = items(get(inv, "files"))
    findings = items(get(imp, "findings"))
    sev = get(imp, "summary", "bySeverity") or {}
    q.metrics.update({"changedFiles": len(files), "changedSymbols": to_int(cm.get("symbols")),
                      "untestedSymbols": to_int(cm.get("untested")), "findings": len(findings),
                      "high": to_int(sev.get("high")), "medium": to_int(sev.get("medium")),
                      "flows": to_int(reach.get("flows")), "libraryClassesReferenced": to_int(lib.get("referencedClasses"))})
    if not files:
        q.flag("amber", "no changed file between the branches: check baseBranch and headBranch")
    if files and to_int(reach.get("flows")) == 0:
        q.flag("amber", "no flows found (endpoints, jobs, listeners): reach and test scope are empty")
    if to_int(cm.get("symbols")) and to_int(cm.get("untested")) == to_int(cm.get("symbols")):
        q.flag("amber", "no changed method is executed by any test: check coverage in 01-build")
    if files and "referencedClasses" in lib and to_int(lib.get("referencedClasses")) == 0:
        q.flag("amber", "the library API diff found no referenced classes (jdeps/javap): see logs\\phase3.log")
    # Rules that fire on most changed files are likely too broad.
    n_files = max(1, len({s(f.get("path")) for f in files}))
    per_rule: dict[str, set] = {}
    for f in findings:
        if s(f.get("source")) == "analyst":
            continue
        per_rule.setdefault(s(f.get("ruleId")), set()).add(s(f.get("file")))
    noisy = {r: len(v) for r, v in per_rule.items() if len(v) >= 4 and len(v) / n_files > 0.5}
    q.metrics["noisyRules"] = noisy
    for r, n in sorted(noisy.items()):
        q.flag("amber", f"rule {r} fired on {n} of {n_files} changed files: likely too broad (config\\rules)")


def _analysis(run_dir: str, q: Score, jobs: dict) -> None:
    for name in ("release-notes", "analyst"):
        rec = jobs.get(name) or {}
        q.metrics[name] = rec.get("state", "not-run")
        if rec.get("state") == "skipped" and "agents are off" in s(rec.get("reason")):
            q.flag("amber", f"{name}: agents are off, so this judgment is not covered")
    hyp = _art(run_dir, "agent/hypotheses.json")
    if isinstance(hyp, dict):
        reasons = items(hyp.get("changeReasons"))
        unknown = len([r for r in reasons if "not determined" in s(r.get("reason")).lower()])
        q.metrics.update({"hypotheses": len(items(hyp.get("hypotheses"))), "changeReasons": len(reasons),
                          "reasonsNotDetermined": unknown})
        if reasons and unknown / len(reasons) > 0.3:
            q.flag("amber", f"{unknown} of {len(reasons)} change reasons are 'not determined': the analyst lacked evidence")
    notes = _art(run_dir, "agent/release-notes.json")
    if isinstance(notes, dict):
        res = [s(i.get("result")) for i in items(notes.get("items"))]
        q.metrics.update({"notesConfirmed": res.count("confirmed"), "notesContradicted": res.count("contradicted"),
                          "notesNotFound": res.count("not-found")})
        if res.count("contradicted"):
            q.flag("amber", f"{res.count('contradicted')} library change(s) contradict their release notes")
        if res and res.count("not-found") / len(res) > 0.5:
            q.flag("amber", "most library changes have no release-note source")
    imp = _art(run_dir, "impact-findings.json")
    if get(imp, "analyst") == "stale":
        q.flag("amber", "impact-findings.json says the analyst answer is stale: its inputs changed after it was given")


def _tests(run_dir: str, q: Score, jobs: dict) -> None:
    b = _art(run_dir, "behavior-results.json")
    ps = get(b, "probeSummary") or {}
    gaps = items(get(_art(run_dir, "test-gaps.json"), "gaps"))
    man = _art(run_dir, "agent/tests-manifest.json")
    tests = items(get(b, "tests"))
    validated = len([t for t in tests if t.get("validated")])
    q.metrics.update({"probes": to_int(ps.get("total")), "probesDifferent": to_int(ps.get("different")),
                      "probeErrors": to_int(ps.get("error")), "gaps": len(gaps),
                      "testsWritten": len(items(get(man, "tests"))), "uncoveredGaps": len(items(get(man, "uncovered"))),
                      "testsRun": len(tests), "validated": validated,
                      "provesChange": len([t for t in tests if proves_change(t)]),
                      "notValidated": len([t for t in tests if needs_repair(t)])})
    if isinstance(b, dict) and b.get("startupOk") is False:
        q.flag("red", "the application did not start on both branches: probes could not run (logs\\app-<side>.err.log)")
    if to_int(ps.get("error")):
        q.flag("amber", f"{ps.get('error')} probe(s) ended in an error")
    if gaps and (jobs.get("write") or {}).get("state") == "skipped":
        q.flag("amber", f"{len(gaps)} gap(s) and no tests written ({(jobs.get('write') or {}).get('reason')})")
    if tests:
        ratio = validated / len(tests)
        q.metrics["validatedRatio"] = round(ratio, 2)
        if validated == 0 and not q.metrics["provesChange"]:
            q.flag("red", "none of the new tests validated")
        elif ratio < 0.5:
            q.flag("amber", f"only {validated} of {len(tests)} new tests validated")
    if len(items(get(man, "uncovered"))) > max(2, len(gaps) // 3):
        q.flag("amber", f"{len(items(get(man, 'uncovered')))} of {len(gaps)} gaps left uncovered by the test writer")


def _review(run_dir: str, q: Score, jobs: dict) -> None:
    rv = _art(run_dir, "agent/review.json")
    verdicts = [s(r.get("verdict")) for r in items(get(rv, "reviews"))]
    q.metrics.update({"reviews": len(verdicts), "pass": verdicts.count("pass"), "fail": verdicts.count("fail")})
    if (jobs.get("review") or {}).get("state") == "skipped":
        q.flag("amber", f"no review: {(jobs.get('review') or {}).get('reason')}")
    if verdicts.count("fail"):
        q.flag("amber", f"{verdicts.count('fail')} test(s) failed the independent review")


def _report(run_dir: str, q: Score, jobs: dict) -> None:
    v = read_json_safe(os.path.join(run_dir, "verdict.json"))
    mi = _art(run_dir, "manual-items.json")
    rr = read_json_safe(os.path.join(run_dir, "review", "review-record.json"))
    verdict = s(get(v, "verdict"))
    # The quality of the report is how complete the comparison is. A difference between the branches is what the
    # report is for, not a quality problem; the manual items are questions for its reader and wait for nobody.
    q.metrics.update({"verdict": verdict, "differences": len(as_list(get(v, "differences"))),
                      "incomplete": len(as_list(get(v, "incomplete"))), "notProven": len(as_list(get(v, "notProven"))),
                      "questionsForReader": len(items(get(mi, "items"))),
                      "recordedDecisions": len(items(get(rr, "manual", "decisions")))})
    if verdict == "incomplete":
        q.flag("red", f"comparison incomplete: {q.metrics['incomplete']} check(s) or stage(s) did not run (verdict.json)")
    elif not verdict:
        q.flag("red", "no verdict.json: the verdict job did not finish")
    if q.metrics["notProven"]:
        q.flag("amber", f"{q.metrics['notProven']} change(s) not proven either way (verdict.json notProven)")


SCORERS = {"00-setup": _setup, "01-build": _build, "02-impact": _impact, "03-analysis": _analysis,
           "04-tests": _tests, "05-review": _review, "06-report": _report}


def score(run_dir: str, phase: Phase, jobs: dict) -> dict:
    q = Score()
    try:
        SCORERS[phase.id](run_dir, q, jobs or {})
    except Exception as e:  # noqa: BLE001 - a scorer must never fail the phase
        q.flag("amber", f"quality could not be scored: {e}")
    return q.as_dict()

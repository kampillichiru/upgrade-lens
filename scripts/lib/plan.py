"""The fixed plan of an orchestrated UpgradeLens run, and the decision of its one next action.

Pure: the plan, the ledger, the facts (lib\\facts.py) and the limits in, one action out. next-step.py prints
it; run-stage.py and agent-step.py refuse any step it did not print. The orchestrator never decides the order.
"""

from __future__ import annotations

import re

from .core import INTERRUPTED_EXIT_CODES, get, like, s, to_int
from .ledger import key_entries, ledger_entry
from .stages import stage_spec


def stage_script(stage: int) -> str:
    return stage_spec(stage).script


def plan_step(plan, key: str):
    """The plan step of a key, or None."""
    return next((x for x in plan if x["key"] == key), None)


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


def agent_key_step(key: str) -> str | None:
    """The agent step of a plan key: agent-analyst -> analyst, agent-tests-repair-1 -> tests."""
    m = re.match(r"^agent-(analyst|release-notes|reviewer)$", s(key))
    if m:
        return m.group(1)
    if re.match(r"^agent-tests(-repair-\d+)?$", s(key)):
        return "tests"
    return None


def is_repair_key(key: str) -> bool:
    return like(s(key), "agent-tests-repair-*")


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


def max_stage_attempts(cfg) -> int:
    """Attempts of one stage when it is interrupted (config orchestration.maxStageAttempts, default 3). An
    interruption comes from the machine, not from the code under test, so a stage may be tried again."""
    return max(1, to_int(get(cfg, "orchestration", "maxStageAttempts"), 3) or 3)


def stage_interrupted(entry: dict) -> bool:
    """A stage attempt that ended by an interruption (Ctrl+C, or a runner that died), not by a failed check."""
    return to_int(entry.get("exitCode"), 0) in INTERRUPTED_EXIT_CODES or s(entry.get("reason")).startswith("interrupted:")


def next_action(plan, ledger, facts: dict, max_agent_attempts: int = 2, max_stage_attempts: int = 2) -> dict:
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
        stage_attempts = len(key_entries(ledger, st["key"]))
        if e and e.get("state") == "running":
            if st["kind"] == "agent":
                return {"action": "end-agent", "key": st["key"], "step": st["step"],
                        "reason": "the agent step was begun and not ended"}
            # No lock holder (a held lock gives "wait" above): the runner died. Retry it, like an interruption.
            if stage_attempts < max_stage_attempts:
                return {"action": "run-stage", "key": st["key"], "stage": st["stage"], "attempt": stage_attempts + 1,
                        "reason": f"retry: the runner of stage {st['key']} died before it recorded a result"}
            return {"action": "stop", "key": st["key"],
                    "reason": f"stage {st['key']} is marked running but no runner holds the lock: the runner died. Start a new run."}
        if e and e.get("state") == "failed":
            reason = s(e.get("reason"))
            if st["kind"] == "stage":
                # A stage that was interrupted (Ctrl+C, a killed runner) did not fail a check: retry it, a limited
                # number of times. A real failure (build, tool, usage) stops the run.
                if stage_interrupted(e) and stage_attempts < max_stage_attempts:
                    return {"action": "run-stage", "key": st["key"], "stage": st["stage"], "attempt": stage_attempts + 1,
                            "reason": f"retry: stage {st['key']} was interrupted (exit {e.get('exitCode')}), not failed"}
                return {"action": "stop", "key": st["key"],
                        "reason": f"stage {st['key']} failed (exit {e.get('exitCode')}): {reason}"}
            if like(reason, "budget:*"):
                return {"action": "stop", "key": st["key"], "reason": reason}
            if like(reason, "guard:*"):
                return {"action": "stop", "key": st["key"],
                        "reason": f"agent {st['key']} wrote outside its allowed files: {reason}. The run is stopped; start a new run."}
            attempts = len(key_entries(ledger, st["key"], "failed"))
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

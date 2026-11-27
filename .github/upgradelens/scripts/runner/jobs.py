"""How one job of a phase runs. Three kinds, never mixed:

  script   one stage script (phaseN-*.py). Its inputs are checked first; its raw output goes, with secrets masked,
           to <phase>\\tools\\<job>.log; its outputs are logged with their summary.
  python   one Python step of the runner (the test writer's work list).
  agent    one judgment job, done by the phase's Copilot agent. It runs in two halves, with the same command:
             begin  the runner writes <phase>\\<job>\\brief.md (the exact task, the inputs, the one folder the agent
                    may write) and a fingerprint of everything else, then exits 20: waiting for the agent
             end    the agent has written its answer and runs the phase command again: the runner compares the
                    fingerprint (write scope), runs the content checks, and either accepts the answer (published
                    to artifacts\\agent for the stage scripts), or writes a new brief with the problems (retry), or
                    fails the job
           An accepted answer whose inputs did not change is reused on a rerun (no agent call), unless --force.

Guardrails enforced here, whatever the agent's instructions say: write scope (anything outside the job's out
folder rejects the answer), input hash (an answer for other inputs is stale), content checks (lib\\agent_checks.py),
attempts (orchestration.maxAgentAttempts) and the run's agent budget (agentBudget).
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from lib.agents import (AGENTS, AgentRun, agent_dir, agent_proposal, agent_write_scope, check_output, copy_output,
                        finish_output, missing_inputs)
from lib.analysis import needs_repair, proves_change
from lib.core import (INTERRUPTED_EXIT_CODES, SCRIPTS_DIR, UL_INVOKER, WORKSPACE_DIR, as_list, config_value, get, items, native_path,
                      now_iso, read_json_safe, remove_tree, s, tail, to_int, write_json, write_text)
from lib.facts import write_test_gaps
from lib.guards import compare_fingerprint, file_tree_hashes, protected_fingerprint
from lib.lock import LOCK_FILE
from lib.stages import exit_meaning, missing_needs as stage_missing_needs, stage_spec

from .log import PhaseLog, redact
from .registry import UL, Job, Phase, producer

# Exit codes of every ul.py phase command.
OK, BLOCKED, USAGE, TOOL, BUILD, GUARD, CHECKS, RUNNING, WAITING = 0, 1, 2, 3, 4, 5, 6, 10, 20
UL_EXIT = {
    OK: "done",
    BLOCKED: "done: the branches differ, or the comparison is incomplete (a result, not a failure)",
    USAGE: "inputs, config or usage problem: nothing was changed",
    TOOL: "tool failure (git, Maven, Java, or an error in a stage script)",
    BUILD: "a build failed on one branch",
    GUARD: "the agent changed files outside its output folder: answer rejected",
    CHECKS: "the agent's answer failed its checks on every attempt",
    RUNNING: "still running in the background",
    WAITING: "waiting for the agent job: read the brief, write the answer, run the same command again",
}


@dataclass
class Ctx:
    config: str
    run_id: str
    run_dir: str
    phase: Phase
    log: PhaseLog
    agents: bool = True
    force: bool = False
    save_job: Callable[[str, dict], None] = lambda name, rec: None

    @property
    def cfg(self) -> dict:
        return read_json_safe(os.path.join(self.run_dir, "config", "upgrade-lens.json")) or {}

    def pdir(self, *parts) -> str:
        return os.path.join(self.run_dir, self.phase.id, *parts)


@dataclass
class Result:
    state: str              # done | skipped | failed | waiting
    code: int = OK
    reason: str = ""
    extra: dict = field(default_factory=dict)


def skip(ctx: Ctx, job: Job, reason: str) -> Result:
    ctx.log.info(job.name, f"SKIP {reason}", "skip")
    return Result("skipped", OK, reason)


# ---------- conditions ----------

def condition(ctx: Ctx, job: Job, jobs: dict) -> str:
    """'' when the job must run, else the reason it is skipped. jobs: this phase's job records so far."""
    def st(name):
        return (jobs.get(name) or {}).get("state")
    if not job.when:
        return ""
    if job.when == "analyst-accepted":
        return "" if st("analyst") == "done" else "the analyst's answer is not accepted, so the rules-only findings stay"
    if job.when == "write-accepted":
        return "" if st("write") == "done" else "no new tests were written"
    if job.when == "repair-accepted":
        return "" if st("repair") == "done" else "no tests were repaired"
    if job.when == "needs-repair":
        if st("validate") != "done":
            return "the new tests did not run"
        if to_int(config_value(ctx.cfg, "orchestration.maxTestRepairRounds", 1), 1) < 1:
            return "repair is off (orchestration.maxTestRepairRounds is 0)"
        tests = items(get(read_json_safe(os.path.join(ctx.run_dir, "artifacts", "behavior-results.json")), "tests"))
        n = len([t for t in tests if needs_repair(t)])
        return "" if n else ("no new test needs repair: each one validated, or fails only on change_branch (that proves a "
                             "behavior change)")
    return ""


# ---------- script jobs ----------

def _env(config: str) -> dict:
    env = dict(os.environ)
    env.update({"UPGRADELENS_INVOKER": UL_INVOKER, "UPGRADELENS_CONFIG": config, "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1"})
    return env


def run_logged(args: list[str], log_path: str, env: dict) -> tuple[int, float]:
    """Runs a command; its output, secrets masked, goes to log_path as it comes. Returns (exit code, seconds)."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    started = time.time()
    with open(log_path, "w", encoding="utf-8", newline="\n") as out:
        out.write(redact("$ " + " ".join(args)) + "\n")
        try:
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        except OSError as e:
            out.write(f"could not start: {e}\n")
            return TOOL, time.time() - started
        for raw in iter(p.stdout.readline, b""):
            out.write(redact(raw.decode("utf-8", errors="replace").rstrip("\r\n")) + "\n")
            out.flush()
        code = p.wait()
    return code, time.time() - started


def describe_output(run_dir: str, rel: str) -> str:
    """One line about a written file: its summary when it has one, else its size."""
    p = os.path.join(run_dir, native_path(rel))
    if not os.path.isfile(p):
        return f"{rel} (not written)"
    if rel.endswith(".json"):
        doc = read_json_safe(p)
        summ = doc.get("summary") if isinstance(doc, dict) else None
        if isinstance(summ, dict):
            text = json.dumps(summ, separators=(",", ":"), ensure_ascii=False)
            return f"{rel} {text[:220]}"
        if isinstance(doc, dict) and "verdict" in doc:
            return f"{rel} verdict={doc.get('verdict')}"
    return f"{rel} ({os.path.getsize(p)} bytes)"


def run_script_job(ctx: Ctx, job: Job) -> Result:
    spec = stage_spec(job.stage)
    missing = stage_missing_needs(ctx.run_dir, job.stage)
    if missing:
        for m in missing:
            ctx.log.error(job.name, f"missing input {m} (written by {producer(m)})")
        return Result("failed", USAGE, f"missing input(s): {', '.join(missing)}", {"missing": missing})
    args = [sys.executable, os.path.join(SCRIPTS_DIR, spec.script), "--run-id", ctx.run_id, "--config", ctx.config]
    tool_log = ctx.pdir("tools", f"{job.name}.log")
    ctx.log.info(job.name, f"start {spec.script}: {spec.title}", "start", command=" ".join(args[1:]))
    code, secs = run_logged(args, tool_log, _env(ctx.config))
    meaning = exit_meaning(code, job.stage) or UL_EXIT.get(code, "")
    for rel in spec.writes:
        ctx.log.info(job.name, f"output {describe_output(ctx.run_dir, rel)}")
    if code == OK or (job.stage == 10 and code == BLOCKED):
        ctx.log.info(job.name, f"done in {secs:.1f}s, exit {code} ({meaning})", "done")
        return Result("done", code, meaning, {"toolLog": tool_log, "seconds": round(secs, 1)})
    last = [x for x in tail(tool_log, 60) if x.strip()][-8:]
    for x in last:
        ctx.log.error(job.name, f"  | {x}")
    if code in INTERRUPTED_EXIT_CODES:
        mapped, meaning = TOOL, "interrupted (Ctrl+C or the process was killed)"
    else:
        mapped = code if code in (USAGE, TOOL, BUILD) else TOOL
    ctx.log.error(job.name, f"failed in {secs:.1f}s, exit {code} ({meaning}). Raw output: {tool_log}", "fail")
    if spec.hint:
        ctx.log.info(job.name, f"hint: {spec.hint}")
    return Result("failed", mapped, f"{spec.script} exit {code}: {meaning}",
                  {"toolLog": tool_log, "tail": last, "stageExit": code, "seconds": round(secs, 1)})


def run_setup_script(config: str, log_path: str) -> tuple[int, float]:
    """Stage 0 creates the run; the runner finds its folder afterwards."""
    args = [sys.executable, os.path.join(SCRIPTS_DIR, stage_spec(0).script), "--config", config]
    return run_logged(args, log_path, _env(config))


def run_check_only(config: str) -> int:
    args = [sys.executable, os.path.join(SCRIPTS_DIR, stage_spec(0).script), "--check-only", "--config", config]
    return subprocess.call(args, env=_env(config), stdin=subprocess.DEVNULL)


# ---------- python jobs ----------

def run_python_job(ctx: Ctx, job: Job) -> Result:
    if job.name == "gaps":
        missing = [n for n in job.needs() if not os.path.isfile(os.path.join(ctx.run_dir, native_path(n)))]
        if missing:
            for m in missing:
                ctx.log.error(job.name, f"missing input {m} (written by {producer(m)})")
            return Result("failed", USAGE, f"missing input(s): {', '.join(missing)}")
        ctx.log.info(job.name, "start: the test writer's work list from changed methods, reach, findings and probes", "start")
        gaps = write_test_gaps(ctx.run_dir, ctx.run_id)
        by_type = collections.Counter(s(g.get("type")) for g in gaps)
        for g in gaps:
            ctx.log.debug(job.name, f"gap {g.get('id')} {g.get('type')} {g.get('ref')}: {s(g.get('detail'))[:160]}")
        ctx.log.info(job.name, f"done: artifacts\\test-gaps.json, {len(gaps)} gap(s) {dict(by_type)}", "done")
        return Result("done", OK, f"{len(gaps)} gap(s)", {"gaps": len(gaps), "byType": dict(by_type)})
    raise ValueError(f"unknown python job {job.name}")


# ---------- agent jobs ----------

def _job_dirs(ctx: Ctx, job: Job) -> tuple[str, str]:
    jdir = ctx.pdir(job.name)
    return jdir, os.path.join(jdir, "out")


def _writable(ctx: Ctx, job: Job) -> list[str]:
    """What may change in the run folder while the agent works: its own out folder, and what the runner writes."""
    p, j = ctx.phase.id, job.name
    return [f"{p}\\{j}\\out\\*", f"{p}\\{j}\\guard.json", f"{p}\\status.json", f"{p}\\log.txt", f"{p}\\events.jsonl",
            f"{p}\\console.txt", "run.log", LOCK_FILE]


def _worktrees(run_dir: str) -> dict:
    env = read_json_safe(os.path.join(run_dir, "artifacts", "environment.json"))
    return {side: get(env, "branches", side, "worktree") for side in ("base", "head")}


def build_brief(ctx: Ctx, job: Job, run: AgentRun, attempt: int, errors: list[str]) -> dict:
    spec = run.spec
    names = list(spec.reads) + (list(spec.repair_reads) if job.repair else [])
    inputs, total = [], 0
    for n in dict.fromkeys(names):
        p = run.artifact(n)
        size = os.path.getsize(p) if os.path.isfile(p) else 0
        total += size
        inputs.append({"path": p, "bytes": size})
    write_only = [os.path.join(run.out_dir, native_path(a.rstrip("*").rstrip("\\"))) + ("\\**" if a.endswith("*") else "")
                  for a in spec.allowed]
    return {
        "schemaVersion": 1, "runId": ctx.run_id, "runDir": ctx.run_dir, "phase": ctx.phase.id, "job": job.name,
        "agent": ctx.phase.agent, "identity": spec.agent, "title": spec.title, "attempt": attempt,
        "repair": job.repair, "instructions": f".github/upgradelens/jobs/{spec.instructions}",
        "task": spec.task(run), "inputs": inputs, "inputBytes": total, "estimatedInputTokens": total // 4,
        "worktrees": _worktrees(ctx.run_dir), "outputFolder": run.out_dir, "answer": run.output,
        "writeOnly": write_only, "inputHash": run.input_hash() if spec.hashed else "",
        "previousErrors": list(errors), "whenDone": f"{UL} {ctx.phase.name} --run {ctx.run_id}",
        "createdAt": now_iso(),
    }


def brief_markdown(b: dict) -> str:
    lines = [f"# UpgradeLens agent job: {b['phase']} / {b['job']} (attempt {b['attempt']})", "",
             f"You are the `{b['agent']}` agent doing the **{b['job']}** job: {b['title']}.",
             f"Run `{b['runId']}`, run folder `{b['runDir']}`.", "",
             f"**First read `{b['instructions']}` and follow it exactly.** It has the procedure and the answer format.", "",
             "## Your task", ""]
    lines += [f"- {t}" for t in b["task"]]
    lines += ["", "## Inputs (read only)", ""]
    lines += [f"- `{i['path']}` ({i['bytes']:,} bytes)" for i in b["inputs"]]
    if any(b["worktrees"].values()):
        lines.append(f"- source, read only: base `{b['worktrees'].get('base')}`, head `{b['worktrees'].get('head')}`")
    lines += ["", "## Write only", ""]
    lines += [f"- `{w}`" for w in b["writeOnly"]]
    if b["inputHash"]:
        lines += ["", f"Put this exact value in `inputHash`: `{b['inputHash']}`"]
    lines += ["", "## Guardrails (checked by the runner when you hand the answer back)", "",
              "- Anything you create or change outside the folder above rejects the answer: artifacts, other phases or "
              "jobs, the worktrees, the clone, config, `.github`.",
              "- Do not run Maven, git commands that change state, or any ul.py command other than the one below.",
              "- Do not guess. When the evidence does not show something, say \"not determined\" (or list the item as "
              "uncovered / unknown) with the reason.",
              "- Every item in your task needs an answer. A missing item fails the checks."]
    if b["previousErrors"]:
        lines += ["", "## Your previous attempt was rejected. Fix every item:", ""]
        lines += [f"- {e}" for e in b["previousErrors"]]
    lines += ["", "## When you are done", "", f"Run: `{b['whenDone']}`", "",
              "It checks your answer. Exit 0: accepted. Exit 20: rejected with a new brief (read this file again). "
              "Exit 5 or 6: rejected, the reason is printed."]
    return "\n".join(lines) + "\n"


def _usage_path(run_dir: str) -> str:
    """The agent calls of the run, against agentBudget (the report shows them)."""
    return os.path.join(run_dir, "artifacts", "agent", "usage.json")


def begin_agent(ctx: Ctx, job: Job, run: AgentRun, attempt: int, errors: list[str]) -> Result:
    spec = run.spec
    budget = to_int(config_value(ctx.cfg, "agentBudget", 12), 12)
    usage = read_json_safe(_usage_path(ctx.run_dir)) or {}
    calls = [c for c in as_list(usage.get("calls")) if c is not None]
    if len(calls) + 1 > budget:
        reason = f"agent budget spent: {len(calls)} of {budget} calls (agentBudget in the config)"
        ctx.log.error(job.name, reason, "fail")
        return Result("failed", USAGE, reason)
    jdir, out = _job_dirs(ctx, job)
    if attempt == 1:
        remove_tree(out)
        os.makedirs(out, exist_ok=True)
        if job.repair:
            seeded = copy_output(agent_dir(ctx.run_dir), out, job.step)
            ctx.log.info(job.name, f"repair: out folder seeded with the current tests ({', '.join(seeded)})")
    elif spec.fresh_start:
        try:
            os.remove(run.output)
        except OSError:
            pass
    os.makedirs(out, exist_ok=True)
    brief = build_brief(ctx, job, run, attempt, errors)
    write_json(os.path.join(jdir, "brief.json"), brief)
    brief_md = os.path.join(jdir, "brief.md")
    write_text(brief_md, brief_markdown(brief))
    calls.append({"phase": ctx.phase.id, "job": job.name, "attempt": attempt, "at": now_iso()})
    write_json(_usage_path(ctx.run_dir), {"schemaVersion": 1, "budget": budget, "totalCalls": len(calls), "calls": calls})
    rec = {"state": "waiting", "attempt": attempt, "brief": brief_md, "answer": run.output,
           "inputHash": brief["inputHash"], "since": now_iso()}
    ctx.save_job(job.name, rec)
    git = s(config_value(ctx.cfg, "git.command", "git"))
    write_json(os.path.join(jdir, "guard.json"), {
        "attempt": attempt, "inputHash": brief["inputHash"], "createdAt": now_iso(),
        "protected": protected_fingerprint(ctx.run_dir, WORKSPACE_DIR, git, _writable(ctx, job)),
        "agentFiles": file_tree_hashes(out),
    })
    ctx.log.info(job.name, f"brief {brief_md}: {len(brief['inputs'])} input file(s), {brief['inputBytes']:,} bytes "
                           f"(~{brief['estimatedInputTokens']:,} tokens); agent call {len(calls)} of {budget}")
    ctx.log.info(job.name, f"WAIT for agent {ctx.phase.agent} (attempt {attempt}): answer to {run.output}", "wait")
    return Result("waiting", WAITING, f"waiting for the agent: {spec.title}", rec)


def answer_summary(step: str, doc) -> str:
    if not isinstance(doc, dict):
        return "no answer"
    if step == "analyst":
        reasons = items(doc.get("changeReasons"))
        unknown = len([r for r in reasons if "not determined" in s(r.get("reason")).lower()])
        return (f"{len(items(doc.get('hypotheses')))} hypotheses, {len(items(doc.get('explanations')))} explanations, "
                f"{len(reasons)} change reasons ({unknown} not determined)")
    if step == "release-notes":
        c = collections.Counter(s(i.get("result")) for i in items(doc.get("items")))
        return f"{sum(c.values())} item(s): {dict(c)}"
    if step == "tests":
        return f"{len(items(doc.get('tests')))} test(s), {len(items(doc.get('uncovered')))} gap(s) uncovered"
    if step == "reviewer":
        c = collections.Counter(s(r.get("verdict")) for r in items(doc.get("reviews")))
        return f"{sum(c.values())} review(s): {dict(c)}"
    return ""


def end_agent(ctx: Ctx, job: Job, run: AgentRun, rec: dict) -> Result:
    jdir, out = _job_dirs(ctx, job)
    attempt = to_int(rec.get("attempt"), 1)
    if not os.path.isfile(run.output):
        ctx.log.info(job.name, f"still waiting: no answer yet at {run.output}", "wait")
        return Result("waiting", WAITING, "no answer yet", rec)
    g = read_json_safe(os.path.join(jdir, "guard.json")) or {}
    if not g.get("protected"):
        ctx.log.error(job.name, "REJECT: the guard record of this job is missing or empty", "reject")
        return Result("failed", GUARD, "guard: the guard record is missing; run the phase again with --restart")
    git = s(config_value(ctx.cfg, "git.command", "git"))
    bad = compare_fingerprint(g.get("protected") or {}, protected_fingerprint(ctx.run_dir, WORKSPACE_DIR, git, _writable(ctx, job)))
    in_out = compare_fingerprint(g.get("agentFiles") or {}, file_tree_hashes(out))
    bad += [f"{ctx.phase.id}\\{job.name}\\out\\{x}" for x in agent_write_scope(in_out, job.step)]
    checks_path = os.path.join(jdir, "checks.json")
    if bad:
        write_json(checks_path, {"attempt": attempt, "checkedAt": now_iso(), "guard": bad, "errors": []})
        ctx.log.error(job.name, f"REJECT attempt {attempt}: changed {len(bad)} file(s) it must not change: "
                                f"{', '.join(bad[:5])}", "reject", files=bad)
        return Result("failed", GUARD, f"guard: changed files outside its output folder: {', '.join(bad[:3])}",
                      {"guard": bad, "checks": checks_path})
    errors = check_output(run)
    doc = read_json_safe(run.output)
    write_json(checks_path, {"attempt": attempt, "checkedAt": now_iso(), "guard": [], "errors": errors,
                             "answer": answer_summary(job.step, doc)})
    if errors:
        max_attempts = max(1, to_int(config_value(ctx.cfg, "orchestration.maxAgentAttempts", 2), 2))
        ctx.log.warn(job.name, f"REJECT attempt {attempt}: {len(errors)} problem(s) in {os.path.basename(run.output)}",
                     "reject", errors=errors[:100])
        for e in errors[:20]:
            ctx.log.warn(job.name, f"  - {e}")
        if attempt < max_attempts:
            ctx.log.info(job.name, f"retry: attempt {attempt + 1} of {max_attempts}, new brief with the problems")
            return begin_agent(ctx, job, run, attempt + 1, errors)
        return Result("failed", CHECKS, f"checks: {len(errors)} problem(s) after {attempt} attempt(s)",
                      {"errors": errors[:50], "checks": checks_path})
    finish_output(run)
    published = copy_output(out, agent_dir(ctx.run_dir), job.step)
    ctx.log.info(job.name, f"ACCEPT attempt {attempt}: {answer_summary(job.step, doc)}", "accept")
    ctx.log.info(job.name, f"published to artifacts\\agent: {', '.join(published)}")
    return Result("done", OK, f"accepted on attempt {attempt}: {answer_summary(job.step, doc)}",
                  {"published": published, "attempt": attempt})


def run_agent_job(ctx: Ctx, job: Job, rec: dict) -> Result:
    spec = AGENTS[job.step]
    _, out = _job_dirs(ctx, job)
    run = AgentRun(step=job.step, run_id=ctx.run_id, run_dir=ctx.run_dir, out_dir=out, repair=job.repair)
    if rec.get("state") == "waiting":
        return end_agent(ctx, job, run, rec)
    if not ctx.agents:
        return skip(ctx, job, "agents are off (--no-agents): this judgment is not covered, and the report says so")
    missing = missing_inputs(ctx.run_dir, job.step, job.repair)
    if missing and all(m.startswith("agent\\") for m in missing):
        return skip(ctx, job, f"nothing to do: no accepted {', '.join(m[6:] for m in missing)} from an earlier agent job")
    if missing:
        for m in missing:
            rel = "artifacts\\" + m
            ctx.log.error(job.name, f"missing input {rel} (written by {producer(rel)})")
        return Result("failed", USAGE, f"missing input(s): {', '.join(missing)}")
    why = spec.nothing_to_do(AgentRun(step=job.step, run_id=ctx.run_id, run_dir=ctx.run_dir, repair=job.repair))
    if why:
        return skip(ctx, job, f"nothing to do: {why}")
    if spec.hashed and not job.repair and not ctx.force:
        published = AgentRun(step=job.step, run_id=ctx.run_id, run_dir=ctx.run_dir)
        if agent_proposal(ctx.run_dir, job.step)["state"] == "used" and not check_output(published):
            ctx.log.info(job.name, "REUSE the accepted answer in artifacts\\agent: its inputs did not change "
                                   "(--force asks the agent again)", "reuse")
            return Result("done", OK, "accepted answer reused: its inputs did not change", {"reused": True})
    ctx.log.info(job.name, f"start agent job: {spec.title}", "start")
    return begin_agent(ctx, job, run, 1, [])


def run_job(ctx: Ctx, job: Job, rec: dict) -> Result:
    if job.kind == "script":
        return run_script_job(ctx, job)
    if job.kind == "python":
        return run_python_job(ctx, job)
    return run_agent_job(ctx, job, rec)


def validated_counts(run_dir: str) -> dict:
    tests = items(get(read_json_safe(os.path.join(run_dir, "artifacts", "behavior-results.json")), "tests"))
    return {"tests": len(tests), "validated": len([t for t in tests if t.get("validated")]),
            "provesChange": len([t for t in tests if proves_change(t)]),
            "needsRepair": len([t for t in tests if needs_repair(t)])}

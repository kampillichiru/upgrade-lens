"""One definition per UpgradeLens agent: what it reads, what it may write, the task it is given, how its
output is checked, what is done with an accepted output, and which stage uses it.

Adding or changing an agent means changing its AgentSpec here and its instructions in
.github\\upgradelens\\jobs\\<instructions>; the callers do not branch on the agent:
  ul\\jobs.py   the agent job of a phase: brief, guard, checks, publish into artifacts\\agent
  phase*.py    read an accepted proposal with agent_proposal (stale when its inputs changed)
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable

from .agent_checks import check_analyst_proposal, check_release_notes_proposal, check_review_answer, check_tests_proposal
from .analysis import proves_change, release_notes_refs
from .core import (config_value, file_sha256, get, input_hash, items, native_path, ps_sorted, read_json_safe, s, to_int,
                   write_json)
from .guards import outside_scope


# ---------- one call of one agent ----------

@dataclass
class AgentRun:
    """One call of one agent: the run it reads, the folder it writes, and the kind of call."""
    step: str
    run_id: str
    run_dir: str
    out_dir: str = ""          # the job's out folder (runs\<id>\<phase>\<job>\out); default artifacts\agent
    repair: bool = False       # test writer: repair the tests that did not validate

    def __post_init__(self):
        if not self.out_dir:
            self.out_dir = agent_dir(self.run_dir)

    @property
    def spec(self) -> "AgentSpec":
        return AGENTS[self.step]

    @property
    def output(self) -> str:
        return os.path.join(self.out_dir, self.spec.output)

    @property
    def cfg(self):
        return read_json_safe(os.path.join(self.run_dir, "config", "upgrade-lens.json"))

    def artifact(self, name: str) -> str:
        return os.path.join(self.run_dir, "artifacts", native_path(name))

    def read(self, name: str):
        return read_json_safe(self.artifact(name))

    def head_worktree(self):
        return get(self.read("environment.json"), "branches", "head", "worktree")

    def input_hash(self) -> str:
        """The hash of the inputs as they are now ("" for an agent without hashed inputs)."""
        return input_hash(self.spec.inputs(self.run_dir)) if self.spec.hashed else ""


@dataclass(frozen=True)
class AgentSpec:
    step: str                   # the agent step name
    agent: str                  # its identity, written into its answer (writer and reviewer must differ)
    instructions: str           # its job instructions: .github\upgradelens\jobs\<instructions>
    title: str
    output: str                 # its answer file, in the output folder
    allowed: tuple              # what it may create or change in the output folder (wildcards)
    reads: tuple                # artifacts that must exist before it can run (data availability)
    hashed: bool                # whether its proposal is tied to the hash of its inputs
    feeds_stage: int            # the first stage that uses an accepted output
    inputs: Callable[[str], list]               # run_dir -> the files its input hash covers
    task: Callable[[AgentRun], list]            # the task lines of its prompt
    check: Callable[[AgentRun, object], list]   # (run, its output document) -> errors
    nothing_to_do: Callable[[AgentRun], str]    # why there is nothing for it to do ("" = there is)
    finish: Callable[[AgentRun, object], None] | None = None  # after an accepted output
    produced: tuple = ()        # files finish writes next to the output
    fresh_start: bool = False   # remove an earlier answer before a new attempt
    repair_reads: tuple = ()    # extra artifacts a repair call needs


def agent_dir(run_dir: str) -> str:
    return os.path.join(run_dir, "artifacts", "agent")


# ---------- shared facts ----------

def validated_test_ids(run_dir: str) -> list[str]:
    """The tests the reviewer reviews: validated tests, and tests that prove a behavior change (pass on master,
    fail on change_branch)."""
    b = read_json_safe(os.path.join(run_dir, "artifacts", "behavior-results.json"))
    return [s(t.get("id")) for t in items(get(b, "tests")) if t.get("validated") or proves_change(t)]


def _rules_files(run_dir: str) -> list[str]:
    rules_dir = os.path.join(run_dir, "config", "rules")
    if not os.path.isdir(rules_dir):
        return []
    return [os.path.join(rules_dir, n) for n in ps_sorted(os.listdir(rules_dir))
            if n.lower().endswith(".json") and os.path.isfile(os.path.join(rules_dir, n))]


def _artifacts(run_dir: str, *names) -> list[str]:
    return [os.path.join(run_dir, "artifacts", n) for n in names]


# ---------- release notes ----------

def _notes_refs(run: AgentRun) -> list[str]:
    return release_notes_refs(run.read("library-api-diff.json"), run.read("dependency-diff.json"))


def _notes_task(run: AgentRun) -> list[str]:
    return [f"Write {run.output} with one item for every high or medium item of artifacts\\library-api-diff.json and "
            f"every direct major dependency change of artifacts\\dependency-diff.json ({', '.join(_notes_refs(run))})."]


def _notes_check(run: AgentRun, doc) -> list[str]:
    return check_release_notes_proposal(doc, run.input_hash(), _notes_refs(run))


def _notes_nothing(run: AgentRun) -> str:
    return "" if _notes_refs(run) else "no high or medium library API change and no major direct dependency change"


# ---------- analyst ----------

def _analyst_task(run: AgentRun) -> list[str]:
    return [f"Write {run.output} with hypotheses (each with plain, example, productionRisk), explanations (one per rule "
            "behind a high or medium finding in artifacts\\impact-findings.json) and changeReasons (one per changed "
            "main-code symbol, each with reason, evidence and consequence)."]


def _analyst_check(run: AgentRun, doc) -> list[str]:
    return check_analyst_proposal(doc, run.input_hash(), run.read("change-inventory.json"), run.read("impact-findings.json"))


def _analyst_nothing(run: AgentRun) -> str:
    return "" if items(get(run.read("change-inventory.json"), "files")) else "no changed file between the branches"


# ---------- test writer ----------

def _tests_task(run: AgentRun) -> list[str]:
    lines = [f"Your work list is {run.artifact('test-gaps.json')}: cover every "
             "gap with a test (gapIds) or list it in 'uncovered' with the reason. Write the test files under "
             f"{os.path.join(run.out_dir, 'tests')} and the manifest to {run.output}."]
    if run.repair:
        lines.append("This is a repair round. Read artifacts\\behavior-results.json (tests[]): fix only the tests that "
                     "fail on master, and the tests that pass on both branches but miss an injected regression "
                     "(injections[].detected false). Keep the ids and the gap coverage. Do not touch a test that passes "
                     "on master and fails on change_branch: it proves a behavior change and is kept as evidence.")
    return lines


def _tests_check(run: AgentRun, doc) -> list[str]:
    gaps = items(get(run.read("test-gaps.json"), "gaps"))
    return check_tests_proposal(doc, run.input_hash(), gaps, os.path.join(run.out_dir, "tests"), run.head_worktree(),
                                to_int(config_value(run.cfg, "orchestration.maxTests", 15), 15))


def _tests_nothing(run: AgentRun) -> str:
    if os.path.isfile(run.artifact("test-gaps.json")) and not items(get(run.read("test-gaps.json"), "gaps")):
        return "no gap in artifacts\\test-gaps.json"
    return ""


# ---------- reviewer ----------

def _review_task(run: AgentRun) -> list[str]:
    return [f"Review these validated tests: {', '.join(validated_test_ids(run.run_dir))}. They are in "
            f"{agent_dir(run.run_dir)} (tests-manifest.json and tests\\). Write your answer (reviews with testId, verdict "
            f"pass|fail, reviewer, notes) to {run.output} and nothing else."]


def _review_check(run: AgentRun, doc) -> list[str]:
    errors = check_review_answer(doc, validated_test_ids(run.run_dir))
    manifest = read_json_safe(os.path.join(agent_dir(run.run_dir), "tests-manifest.json"))
    if manifest and s(manifest.get("writer")) == run.spec.agent:
        errors.append("the reviewer wrote these tests; a review needs an independent reviewer")
    return errors


def _review_finish(run: AgentRun, doc) -> None:
    """The reviewer's answer becomes review.json, with each reviewed file's hash (stage 9 checks them)."""
    manifest = read_json_safe(os.path.join(agent_dir(run.run_dir), "tests-manifest.json"))
    rows = []
    for r in items(get(doc, "reviews")):
        t = next((x for x in items(get(manifest, "tests")) if x.get("id") == r.get("testId")), {})
        f = os.path.join(agent_dir(run.run_dir), "tests", native_path(t.get("relativePath")))
        rows.append({"testId": r.get("testId"), "fileSha256": file_sha256(f) if os.path.isfile(f) else "",
                     "writer": get(manifest, "writer"), "reviewer": run.spec.agent, "verdict": r.get("verdict"),
                     "notes": r.get("notes")})
    write_json(os.path.join(run.out_dir, "review.json"), {"schemaVersion": 1, "runId": run.run_id, "reviews": rows})


def _review_nothing(run: AgentRun) -> str:
    return "" if validated_test_ids(run.run_dir) else "no validated test and no test that proves a behavior change"


# ---------- the registry ----------

AGENTS: dict[str, AgentSpec] = {spec.step: spec for spec in (
    AgentSpec(
        step="release-notes", agent="upgradelens-release-notes", instructions="release-notes.md",
        title="validate library changes against release notes",
        output="release-notes.json", allowed=("release-notes.json",),
        reads=("dependency-diff.json", "library-api-diff.json"), hashed=True, feeds_stage=7,
        inputs=lambda d: _artifacts(d, "dependency-diff.json", "library-api-diff.json"),
        task=_notes_task, check=_notes_check, nothing_to_do=_notes_nothing),
    AgentSpec(
        step="analyst", agent="upgradelens-analyst", instructions="analyst.md",
        title="hypotheses, plain explanations, reason for each changed method",
        output="hypotheses.json", allowed=("hypotheses.json",),
        reads=("change-inventory.json", "dependency-diff.json", "api-diff.json", "library-api-diff.json", "environment.json"),
        hashed=True, feeds_stage=4,
        inputs=lambda d: _artifacts(d, "change-inventory.json", "dependency-diff.json", "api-diff.json",
                                    "library-api-diff.json") + _rules_files(d),
        task=_analyst_task, check=_analyst_check, nothing_to_do=_analyst_nothing),
    AgentSpec(
        step="tests", agent="upgradelens-test-writer", instructions="test-writer.md",
        title="a test for every gap in artifacts\\test-gaps.json",
        output="tests-manifest.json", allowed=("tests-manifest.json", "tests\\*"),
        reads=("impact-findings.json", "reach.json", "changed-methods.json", "test-gaps.json", "environment.json"),
        hashed=True, feeds_stage=5,
        # test-gaps.json is written before the test writer runs, outside artifacts\agent, so the writer cannot
        # change its own work list.
        inputs=lambda d: _artifacts(d, "impact-findings.json", "reach.json", "changed-methods.json", "test-gaps.json"),
        task=_tests_task, check=_tests_check, nothing_to_do=_tests_nothing,
        repair_reads=("behavior-results.json", "agent\\tests-manifest.json")),
    AgentSpec(
        step="reviewer", agent="upgradelens-test-reviewer", instructions="test-reviewer.md",
        title="independent review of the validated tests",
        output="review-answer.json", allowed=("review-answer.json",),
        reads=("behavior-results.json", "agent\\tests-manifest.json", "environment.json"), hashed=False, feeds_stage=9,
        inputs=lambda d: [], task=_review_task, check=_review_check, nothing_to_do=_review_nothing,
        finish=_review_finish, produced=("review.json",), fresh_start=True),
)}


def agent_spec(step: str) -> AgentSpec:
    if step not in AGENTS:
        raise ValueError(f"unknown agent step '{step}' (one of: {', '.join(AGENTS)})")
    return AGENTS[step]


# ---------- what the callers use ----------

def agent_inputs(run_dir: str, step: str) -> list[str]:
    """The files whose bytes an agent proposal is tied to. The agent writes their hash into its proposal; the
    stages recompute it and use a proposal only when the hashes match. One list, used by both."""
    spec = agent_spec(step)
    if not spec.hashed:
        raise ValueError(f"agent step '{step}' has no input hash")
    return spec.inputs(run_dir)


def agent_proposal(run_dir: str, step: str, file_name: str | None = None) -> dict:
    """State of one agent proposal file in artifacts\\agent: not-run (no file), stale (inputs changed since it was
    written), or used. Only a "used" proposal may change a result."""
    path = os.path.join(agent_dir(run_dir), file_name or agent_spec(step).output)
    if not os.path.isfile(path):
        return {"state": "not-run", "doc": None, "path": path}
    doc = read_json_safe(path)
    expected = input_hash(agent_inputs(run_dir, step))
    state = "used" if isinstance(doc, dict) and doc.get("inputHash") == expected else "stale"
    return {"state": state, "doc": doc, "path": path}


def missing_inputs(run_dir: str, step: str, repair: bool = False) -> list[str]:
    """The artifacts an agent needs that are not there yet (empty = its data is available)."""
    spec = agent_spec(step)
    names = list(spec.reads) + (list(spec.repair_reads) if repair else [])
    return [n for n in dict.fromkeys(names) if not os.path.isfile(os.path.join(run_dir, "artifacts", native_path(n)))]


def agent_write_scope(changed_files, step: str) -> list[str]:
    """Files in the output folder that one step created or changed but may not. Everything it touched outside
    its allowed files is a violation."""
    return outside_scope(changed_files, agent_spec(step).allowed)


def check_output(run: AgentRun) -> list[str]:
    """The content check of the agent's answer in its output folder."""
    return run.spec.check(run, read_json_safe(run.output))


def finish_output(run: AgentRun) -> None:
    """What is done with an accepted answer (the reviewer's becomes review.json)."""
    if run.spec.finish:
        run.spec.finish(run, read_json_safe(run.output))


def copy_output(src_dir: str, dst_dir: str, step: str) -> list[str]:
    """Copies an agent's files (its allowed files and what finish wrote) from one output folder to another.
    A folder pattern ("tests\\*") replaces the whole folder, so no file of an earlier answer stays behind.
    Returns the copied paths, relative."""
    spec = agent_spec(step)
    copied = []
    os.makedirs(dst_dir, exist_ok=True)
    for pattern in list(spec.allowed) + list(spec.produced):
        rel = pattern.replace("\\", "/")
        if rel.endswith("/*"):
            folder = rel[:-2]
            src, dst = os.path.join(src_dir, native_path(folder)), os.path.join(dst_dir, native_path(folder))
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
                copied.append(folder + "/")
            continue
        src = os.path.join(src_dir, native_path(rel))
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst_dir, native_path(rel)))
            copied.append(rel)
    return copied

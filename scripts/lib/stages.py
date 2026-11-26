"""One definition per UpgradeLens stage: its script, what it needs, what it writes, its logs, what its exit
codes mean, and where to look first when it fails.

Paths are relative to the run folder (runs\\<id>\\), with backslashes. The callers do not hard-code any of this:
  run-stage.py, run-pipeline.py   run the script; refuse a stage whose inputs are missing, before it starts
  diagnose.py                     explain a failed or stuck stage: inputs, exit code, logs, the next command
  run-agent.py                    say which stage writes an artifact an agent is missing
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field

from .core import native_path, s

# What an exit code means, for every stage. A stage adds or overrides entries in its own exit_codes.
EXIT_MEANINGS = {
    "0": "finished", "1": "verdict blocked", "2": "usage, config or order problem: nothing was changed",
    "3": "tool failure (git, Maven, Java, or an error in the stage)", "4": "build or tests failed",
    "10": "still running in the background",
    "130": "interrupted (Ctrl+C)", "-2": "interrupted (Ctrl+C)",
    "3221225786": "killed: Ctrl+C or its console window was closed",
    "-1073741510": "killed: Ctrl+C or its console window was closed",
}


@dataclass(frozen=True)
class StageSpec:
    stage: int
    script: str
    title: str
    needs: tuple                 # files that must exist before the stage starts
    writes: tuple                # its outputs
    optional: tuple = ()         # read when present (agent proposals, decisions)
    logs: tuple = ()             # its logs, most useful first (wildcards allowed; the newest match is shown)
    exit_codes: dict = field(default_factory=dict)  # stage-specific meanings
    hint: str = ""               # what usually goes wrong, and where to look


def _art(*names) -> tuple:
    return tuple(f"artifacts\\{n}" for n in names)


BASE = ("status.json", "config\\upgrade-lens.json", "artifacts\\environment.json")

STAGES: dict[int, StageSpec] = {spec.stage: spec for spec in (
    StageSpec(0, "phase0-setup.py", "setup: config check, clone, worktrees, merge base",
              needs=(), writes=BASE, logs=("logs\\phase0.log",),
              hint="FAIL lines name the config key or tool to fix (config\\upgrade-lens.json); a clone error is in phase0.log."),
    StageSpec(1, "phase1-build.py", "build and test both branches",
              needs=BASE, writes=_art("build-results.json", "test-results.json"),
              logs=("logs\\build-*.log", "logs\\phase1.log"),
              exit_codes={"2": "a worktree, JDK or Maven settings file is missing: nothing was built",
                          "3": "a build timed out (timeouts.buildMinutes)",
                          "4": "a build or a test failed on one branch: see the build log of that branch"},
              hint="The build log of each branch (logs\\build-<branch>.log) has the Maven error; search for 'ERROR' or 'FAIL'."),
    StageSpec(2, "phase2-changes.py", "changed files and methods",
              needs=BASE, writes=_art("change-inventory.json") + ("reports\\change-inventory.md",),
              logs=("logs\\phase2.log",), hint="A git error (merge base, diff) is in phase2.log."),
    StageSpec(3, "phase3-deps.py", "dependencies, application API and library API",
              needs=BASE, writes=_art("dependency-diff.json", "api-diff.json", "library-api-diff.json"),
              logs=("logs\\phase3.log", "logs\\deps-tree-*.log", "logs\\deps-classpath-*.log", "logs\\deps-analyze-*.log"),
              exit_codes={"2": "a worktree, its compiled classes or javap is missing: run stage 1 first, check java.base/java.head"},
              hint="Maven dependency goals write logs\\deps-*.log; a missing class folder means stage 1 did not compile."),
    StageSpec(4, "phase4-impact.py", "impact rules, flows, reach, coverage",
              needs=BASE + _art("change-inventory.json", "dependency-diff.json", "api-diff.json"),
              writes=_art("changed-methods.json", "impact-findings.json", "reach.json"),
              optional=_art("library-api-diff.json", "build-results.json", "agent\\hypotheses.json"),
              logs=("logs\\phase4.log",),
              exit_codes={"3": "a required input is missing, for example the head JaCoCo report (target\\site\\jacoco\\jacoco.xml)"},
              hint="No JaCoCo report: the project must run the jacoco-maven-plugin in the build goals of stage 1."),
    StageSpec(5, "phase5-behavior.py", "startup, probes and new tests on both branches",
              needs=BASE + _art("impact-findings.json", "reach.json"),
              writes=_art("behavior-results.json", "gaps.json"),
              optional=_art("changed-methods.json", "agent\\tests-manifest.json"),
              logs=("logs\\phase5.log", "logs\\app-*.err.log", "logs\\app-*.out.log", "logs\\tests-*.log"),
              hint="Startup problems are in logs\\app-<side>.err.log; a new test's Maven output is in the newest logs\\tests-*.log."),
    StageSpec(6, "phase6-flows.py", "flow selection",
              needs=("status.json",) + _art("impact-findings.json", "reach.json", "behavior-results.json"),
              writes=_art("flows.json"), logs=("logs\\phase6.log",)),
    StageSpec(7, "phase7-manual.py", "manual review items",
              needs=("status.json", "config\\upgrade-lens.json") + _art("impact-findings.json", "flows.json", "behavior-results.json"),
              writes=_art("manual-items.json"), optional=_art("agent\\release-notes.json"), logs=("logs\\phase7.log",)),
    StageSpec(8, "phase8-output.py", "report; validated tests placed uncommitted",
              needs=BASE + _art("impact-findings.json", "flows.json", "behavior-results.json", "manual-items.json"),
              writes=_art("new-tests.json", "new-tests.patch") + ("reports\\findings.json", "reports\\report.md",
                                                                  "reports\\report.html", "reports\\summary.md"),
              optional=_art("agent\\hypotheses.json", "agent\\release-notes.json"), logs=("logs\\phase8.log",),
              exit_codes={"3": "a validated test could not be placed in the head worktree (file exists or is missing)"}),
    StageSpec(9, "phase9-review.py", "agent review and manual decisions",
              needs=("status.json",) + _art("behavior-results.json", "manual-items.json"),
              writes=("review\\review-record.json",),
              optional=("review\\manual-decisions.json",) + _art("agent\\review.json", "agent\\tests-manifest.json"),
              logs=("logs\\phase9.log",)),
    StageSpec(10, "phase10-verdict.py", "verdict",
              needs=("status.json", "review\\review-record.json") + _art("impact-findings.json", "flows.json",
                                                                       "behavior-results.json", "manual-items.json"),
              writes=("verdict.json",), logs=("logs\\phase10.log",),
              exit_codes={"0": "verdict safe, or safe with listed tests", "1": "verdict blocked: see verdict.json (blocking)"},
              hint="A blocked verdict is a result, not a failure: verdict.json lists each blocking reason."),
)}

AGENT_WRITERS = {"artifacts\\agent\\tests-manifest.json": "the test writer agent",
                 "artifacts\\agent\\hypotheses.json": "the analyst agent",
                 "artifacts\\agent\\release-notes.json": "the release-notes agent",
                 "artifacts\\agent\\review.json": "the reviewer agent",
                 "artifacts\\test-gaps.json": "next-step.py after stage 5 (or run-agent.py begin, scripts-only run)",
                 "review\\manual-decisions.json": "a person (the manual decisions)"}


def stage_spec(stage) -> StageSpec:
    return STAGES[int(stage)]


def written_by(rel_path: str) -> str:
    """Who writes a run file: "stage N (title)", an agent, or a person."""
    rel = s(rel_path).replace("/", "\\")
    if rel in AGENT_WRITERS:
        return AGENT_WRITERS[rel]
    for spec in STAGES.values():
        if rel in spec.writes:
            return f"stage {spec.stage} ({spec.title})"
    return "unknown"


def exit_meaning(code, stage=None) -> str:
    """Plain words for an exit code, with the stage's own meaning when it has one."""
    c = s(code)
    meanings = dict(EXIT_MEANINGS)
    if stage is not None and int(stage) in STAGES:
        meanings.update(STAGES[int(stage)].exit_codes)
    return meanings.get(c, "")


def missing_needs(run_dir: str, stage) -> list[str]:
    """The files a stage needs that are not there (relative paths). Empty = it can start."""
    return [n for n in stage_spec(stage).needs if not os.path.isfile(os.path.join(run_dir, native_path(n)))]


def missing_message(run_dir: str, stage) -> str:
    """A refusal message for a stage whose inputs are missing, or "" when nothing is missing."""
    missing = missing_needs(run_dir, stage)
    if not missing:
        return ""
    lines = [f"Stage {stage} ({stage_spec(stage).title}) cannot start: missing input(s):"]
    lines += [f"  - {m}  (written by {written_by(m)})" for m in missing]
    return "\n".join(lines)


def stage_logs(run_dir: str, stage) -> list[str]:
    """The stage's log files that exist, most useful first; for a wildcard, the newest match per pattern
    (and the newest two for per-branch logs, so both sides show)."""
    found = []
    for pattern in stage_spec(stage).logs:
        full = os.path.join(run_dir, native_path(pattern))
        if "*" not in pattern:
            if os.path.isfile(full):
                found.append(full)
            continue
        matches = sorted(glob.glob(full), key=lambda p: os.path.getmtime(p), reverse=True)
        found += matches[:2]
    return list(dict.fromkeys(found))

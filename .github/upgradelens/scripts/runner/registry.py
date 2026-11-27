"""The 7 phases of UpgradeLens and their jobs. Defined once; the runner, status, why, needs and the agents read it.

Every job is either one stage script, one Python step, or one agent: never a mix. So when a result is wrong, the job
that produced it says whether a script or an agent is the cause.

A phase needs the files its jobs need, minus what an earlier job of the same phase writes. A phase can run on its
own whenever those files are there, whichever run or phase wrote them.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.agents import AGENTS
from lib.facts import GAP_SOURCES
from lib.stages import stage_spec

UL = "python .github/upgradelens/ul.py"

# Never part of an input hash: it changes with every stage that runs, not with the data.
UNHASHED = {"status.json"}

# Who writes a file that no phase writes.
PERSON_FILES = {"review\\manual-decisions.json": "a person (the owner of a manual item)"}


def _norm(rel: str) -> str:
    return rel.replace("/", "\\")


@dataclass(frozen=True)
class Job:
    name: str
    kind: str                  # script | agent | python
    title: str
    stage: int | None = None   # script: the stage script it runs (lib\stages.py)
    step: str | None = None    # agent: the agent spec it runs (lib\agents.py)
    repair: bool = False       # agent tests: repair the tests that did not validate
    when: str = ""             # a condition checked when the job is reached ("" = always; see jobs.condition)

    def needs(self) -> tuple:
        if self.kind == "script":
            return tuple(_norm(n) for n in stage_spec(self.stage).needs)
        if self.kind == "agent":
            spec = AGENTS[self.step]
            names = list(spec.reads) + (list(spec.repair_reads) if self.repair else [])
            return tuple(f"artifacts\\{_norm(n)}" for n in dict.fromkeys(names))
        if self.name == "gaps":
            return tuple(f"artifacts\\{n}" for n in GAP_SOURCES)
        return ()

    def optional(self) -> tuple:
        if self.kind == "script":
            return tuple(_norm(n) for n in stage_spec(self.stage).optional)
        return ()

    def writes(self) -> tuple:
        if self.kind == "script":
            return tuple(_norm(n) for n in stage_spec(self.stage).writes)
        if self.kind == "agent":
            spec = AGENTS[self.step]
            return tuple(f"artifacts\\agent\\{n}" for n in (spec.output,) + tuple(spec.produced))
        if self.name == "gaps":
            return ("artifacts\\test-gaps.json",)
        return ()


@dataclass(frozen=True)
class Phase:
    id: str           # folder name in the run, and the order: 00-setup ... 06-report
    name: str         # the ul.py command: setup, build, impact, analysis, tests, review, report
    title: str
    agent: str        # the Copilot agent that runs it (.github\agents\<agent>.agent.md)
    jobs: tuple

    def job(self, name: str) -> Job | None:
        return next((j for j in self.jobs if j.name == name), None)

    def needs(self) -> list[str]:
        """Files the phase needs from outside: its jobs' needs minus what an earlier job of the phase writes."""
        own, out = set(), []
        for j in self.jobs:
            for n in j.needs():
                if n not in own and n not in out:
                    out.append(n)
            own.update(j.writes())
        return out

    def optional(self) -> list[str]:
        own = set(self.writes())
        return [n for j in self.jobs for n in j.optional() if n not in own and n not in self.needs()]

    def writes(self) -> list[str]:
        return list(dict.fromkeys(w for j in self.jobs for w in j.writes()))

    def hashed_inputs(self) -> list[str]:
        """The inputs whose bytes decide whether the phase's result is still current.

        An optional input that a later phase writes is feedback, not an input: 02-impact's rules read the analyst's
        answer when it is there, but 03-analysis writes it and runs the rules again itself (job merge). Hashing it
        would make 02-impact stale as soon as 03-analysis is done, and `continue` would loop between the two."""
        later = {w for p in PHASES[PHASES.index(self) + 1:] for w in p.writes()}
        return [n for n in self.needs() + self.optional()
                if n not in UNHASHED and not (n in later and n not in self.needs())]

    @property
    def command(self) -> str:
        return f"{UL} {self.name}"


PHASES: tuple[Phase, ...] = (
    Phase("00-setup", "setup", "config check, clone, one worktree per branch, merge base", "ul-00-setup", (
        Job("setup", "script", "config and prerequisites, clone, worktrees, merge base", stage=0),
    )),
    Phase("01-build", "build", "build and test both branches, with coverage", "ul-01-build", (
        Job("build", "script", "Maven build and tests on both branches, JaCoCo coverage", stage=1),
    )),
    Phase("02-impact", "impact", "what changed, dependency and API changes, impact rules", "ul-02-impact", (
        Job("changes", "script", "changed files, methods and commits", stage=2),
        Job("deps", "script", "dependency tree, application API and library API changes", stage=3),
        Job("rules", "script", "impact rules, flows, reach, coverage of changed methods", stage=4),
    )),
    Phase("03-analysis", "analysis", "release notes and analyst agents, merged into the impact findings", "ul-03-analysis", (
        Job("release-notes", "agent", "check each library change against its release notes", step="release-notes"),
        Job("analyst", "agent", "a reason for each changed method, hypotheses, plain explanations", step="analyst"),
        Job("merge", "script", "impact rules again, with the analyst's accepted answer", stage=4, when="analyst-accepted"),
    )),
    Phase("04-tests", "tests", "probes, test gaps, new tests written and validated on both branches", "ul-04-tests", (
        Job("probes", "script", "start both applications, same requests to both, compare", stage=5),
        Job("asserts", "script", "do the existing tests fail when a changed line is changed on purpose", stage=11),
        Job("gaps", "python", "the test writer's work list (artifacts\\test-gaps.json)"),
        Job("write", "agent", "one characterization test per gap", step="tests"),
        Job("validate", "script", "run the new tests on both branches, with injected regressions", stage=5, when="write-accepted"),
        Job("repair", "agent", "fix only the tests that did not validate", step="tests", repair=True, when="needs-repair"),
        Job("revalidate", "script", "run the repaired tests on both branches", stage=5, when="repair-accepted"),
    )),
    Phase("05-review", "review", "independent review of the validated tests", "ul-05-review", (
        Job("review", "agent", "pass or fail per validated test, by a reviewer that did not write it", step="reviewer"),
    )),
    Phase("06-report", "report", "flows, manual items, report, review record, verdict", "ul-06-report", (
        Job("flows", "script", "test scope per flow: full, targeted, smoke", stage=6),
        Job("manual", "script", "questions a person must decide", stage=7),
        Job("report", "script", "the report; validated tests placed uncommitted in the head worktree", stage=8),
        Job("record", "script", "the review and the manual decisions, checked against their files", stage=9),
        Job("verdict", "script", "the verdict, and the report again with it", stage=10),
    )),
)


def find_phase(name: str) -> Phase | None:
    """A phase by command name (impact), folder name (02-impact) or number (2, 02)."""
    key = (name or "").strip().lower()
    for p in PHASES:
        if key in (p.name, p.id, p.id[:2], str(int(p.id[:2]))):
            return p
    return None


def phase_after(phase: Phase) -> Phase | None:
    i = PHASES.index(phase)
    return PHASES[i + 1] if i + 1 < len(PHASES) else None


def producer(rel: str) -> str:
    """Who writes a run file: "phase 02-impact (job rules)", a person, or "unknown"."""
    rel = _norm(rel)
    if rel in PERSON_FILES:
        return PERSON_FILES[rel]
    for p in PHASES:
        for j in p.jobs:
            if rel in j.writes():
                return f"{p.id} (job {j.name}; run: {p.command})"
    return "unknown"


def producer_phase(rel: str) -> Phase | None:
    rel = _norm(rel)
    for p in PHASES:
        if rel in p.writes():
            return p
    return None

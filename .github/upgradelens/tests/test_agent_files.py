"""Everything lives in .github: one Copilot agent per phase (.github/agents), one instructions file per agent job
(.github/upgradelens/jobs), the runner and its config (.github/upgradelens). These checks keep that complete and keep
the agents inside their guardrails."""

import os
import re

import pytest

from lib.agents import AGENTS
from runner.registry import PHASES, UL

TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # .github/upgradelens
WORKSPACE = os.path.dirname(os.path.dirname(TOOL))
AGENT_DIR = os.path.join(WORKSPACE, ".github", "agents")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def frontmatter(text):
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.S)
    assert m, "no frontmatter"
    return m.group(1)


def agent_text(phase):
    return read(os.path.join(AGENT_DIR, f"{phase.agent}.agent.md"))


def tools(phase):
    m = re.search(r"^tools:\s*\[(.*)\]", frontmatter(agent_text(phase)), re.M)
    return {t.strip().strip("'\"") for t in m.group(1).split(",")} if m else set()


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p.id)
def test_each_phase_has_its_agent(phase):
    fm = frontmatter(agent_text(phase))
    assert re.search(rf"^name:\s*{re.escape(phase.agent)}\s*$", fm, re.M)
    assert "target:" not in fm, "no target: the agents must work in VS Code and in IntelliJ"
    assert f"{UL} {phase.name}" in agent_text(phase)


def test_there_are_no_other_agents():
    names = sorted(n for n in os.listdir(AGENT_DIR) if n.endswith(".agent.md"))
    assert names == sorted([f"{p.agent}.agent.md" for p in PHASES] + ["ul-run.agent.md"])


def test_the_conductor_only_reacts_and_delegates_to_the_phase_agents():
    text = read(os.path.join(AGENT_DIR, "ul-run.agent.md"))
    fm = frontmatter(text)
    assert re.search(r"^name:\s*ul-run\s*$", fm, re.M)
    for p in PHASES:
        assert p.agent in fm, f"{p.agent} must be callable as a subagent"
    from runner.cli import build_parser
    commands = set(build_parser()._subparsers._group_actions[0].choices)
    used = set(re.findall(r"ul\.py (\w+)", text))
    assert "continue" in used and used <= commands, used - commands
    for p in PHASES:
        assert f"ul.py {p.name}" not in text, "the conductor never runs a phase itself: ul.py continue does"


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p.id)
def test_only_phases_with_agent_jobs_may_edit_files(phase):
    has_agent_job = any(j.kind == "agent" for j in phase.jobs)
    assert ("edit" in tools(phase)) == has_agent_job
    assert "execute" in tools(phase)
    assert ("web" in tools(phase)) == (phase.name == "analysis"), "only the release-notes job needs the web"


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p.id)
def test_agents_name_only_commands_that_exist(phase):
    from runner.cli import build_parser
    commands = set(build_parser()._subparsers._group_actions[0].choices)
    used = set(re.findall(r"ul\.py (\w+)", agent_text(phase)))
    assert used <= commands, used - commands


@pytest.mark.parametrize("step", sorted(AGENTS))
def test_each_agent_job_has_its_instructions(step):
    text = read(os.path.join(TOOL, "jobs", AGENTS[step].instructions))
    assert text.startswith("# Job:")
    assert "brief" in text, "the instructions point at the brief for paths and the input hash"
    for old in ("agent-step.py", "run-agent.py", "next-step.py", "orchestrator"):
        assert old not in text


def test_every_agent_job_belongs_to_a_phase_whose_agent_names_its_instructions():
    for p in PHASES:
        for j in p.jobs:
            if j.kind == "agent":
                assert AGENTS[j.step].instructions in agent_text(p)


def test_nothing_outside_github():
    assert not os.path.exists(os.path.join(WORKSPACE, ".claude"))
    for d in ("scripts", "config", "tests"):
        assert not os.path.isdir(os.path.join(WORKSPACE, d)), f"{d} belongs under .github/upgradelens"

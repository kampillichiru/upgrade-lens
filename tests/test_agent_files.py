"""The agent and skill instructions live only in .github (GitHub Copilot reads them there; Claude Code reaches them
through CLAUDE.md). These checks keep that single source complete."""

import os
import re

import pytest

from lib.agents import AGENTS as AGENT_SPECS, AgentRun, agent_prompt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = sorted(spec.agent for spec in AGENT_SPECS.values()) + ["upgradelens-orchestrator"]
SKILLS = ["upgradelens-missing-tests", "upgradelens-phase0", "upgradelens-run"]


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def frontmatter_name(text):
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.S)
    assert m, "no frontmatter"
    n = re.search(r"^name:\s*(\S+)", m.group(1), re.M)
    return n.group(1) if n else None


@pytest.mark.parametrize("agent", AGENTS)
def test_each_agent_has_its_github_file(agent):
    assert frontmatter_name(read(".github", "agents", f"{agent}.agent.md")) == agent


@pytest.mark.parametrize("skill", SKILLS)
def test_each_skill_has_its_github_file(skill):
    assert frontmatter_name(read(".github", "skills", skill, "SKILL.md")) == skill


@pytest.mark.parametrize("step", sorted(AGENT_SPECS))
def test_the_subagent_prompt_names_an_existing_github_file(step):
    prompt = agent_prompt(AgentRun(step=step, run_id="r1", run_dir="runs\\r1"))
    m = re.search(r"\(\.github\\agents\\([\w-]+\.agent\.md)\)", prompt)
    assert m and os.path.isfile(os.path.join(ROOT, ".github", "agents", m.group(1)))


def test_claude_md_points_to_the_github_orchestrator():
    text = read("CLAUDE.md")
    assert ".github/agents/upgradelens-orchestrator.agent.md" in text
    assert ".github/skills/upgradelens-run/SKILL.md" in text


CLAUDE_ENTRY = os.path.join(".claude", "skills", "upgradelens-orchestrator", "SKILL.md")


def test_claude_holds_only_the_slash_command_entry_point():
    files = [os.path.relpath(os.path.join(b, n), ROOT) for b, _d, fs in os.walk(os.path.join(ROOT, ".claude")) for n in fs]
    assert files == [CLAUDE_ENTRY]
    text = read(CLAUDE_ENTRY)
    assert frontmatter_name(text) == "upgradelens-orchestrator"
    assert ".github/agents/upgradelens-orchestrator.agent.md" in text
    assert len(text.splitlines()) < 20, "instructions belong in .github, not in the .claude entry point"


def test_copilot_has_the_same_slash_command():
    text = read(".github", "prompts", "upgradelens-orchestrator.prompt.md")
    assert re.search(r"^agent:\s*upgradelens-orchestrator\s*$", text, re.M)


def test_github_instructions_do_not_depend_on_claude_files():
    for base, _dirs, files in os.walk(os.path.join(ROOT, ".github")):
        for name in files:
            text = read(os.path.relpath(os.path.join(base, name), ROOT))
            for ref in re.findall(r"\.claude[\\/][\w\\/.-]+", text):
                assert ref.replace("\\", "/") == CLAUDE_ENTRY.replace("\\", "/"), f"{name} refers to {ref}"

# UpgradeLens

All agent and skill instructions are in `.github/` (shared with GitHub Copilot). The only Claude Code file is the
slash-command entry point `.claude/skills/upgradelens-orchestrator/SKILL.md`, which points back to `.github`.

To run the check: `/upgradelens-orchestrator` (optionally with `continue run <run id>`). It follows
`.github/agents/upgradelens-orchestrator.agent.md` (see its "In Claude Code" section) and
`.github/skills/upgradelens-run/SKILL.md`.

Other skills: `.github/skills/upgradelens-phase0/SKILL.md` (setup only) and
`.github/skills/upgradelens-missing-tests/SKILL.md` (used by the test writer).

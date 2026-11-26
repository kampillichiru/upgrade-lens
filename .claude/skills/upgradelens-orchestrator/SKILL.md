---
name: upgradelens-orchestrator
description: Run (or continue) the full UpgradeLens upgrade check between baseBranch and headBranch from config/upgrade-lens.json, end to end, or run one UpgradeLens agent (release-notes, analyst, tests, reviewer) on its own on a run that has its data, by following the .github orchestrator. Use when the user types /upgradelens-orchestrator or asks to run, continue or resume the UpgradeLens check, or to run one of its agents.
argument-hint: "[continue run <run id> | agent <step> on run <run id>]"
---

The instructions are in `.github` (shared with GitHub Copilot). Read `.github/agents/upgradelens-orchestrator.agent.md`
now and follow it exactly (its "In Claude Code" section applies to you), together with the skill it names,
from the first step to `done` or `stop`.

Arguments: $ARGUMENTS
If they name an agent and a run id, run only that agent (the skill's "One agent on its own"). If they name only
a run id, continue that run. Otherwise start a new run.

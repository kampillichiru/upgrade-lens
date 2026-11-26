---
name: upgradelens-orchestrator
description: Run (or continue) the full UpgradeLens upgrade check between baseBranch and headBranch from config/upgrade-lens.json, end to end, by following the .github orchestrator. Use when the user types /upgradelens-orchestrator or asks to run, continue or resume the UpgradeLens check.
argument-hint: "[continue run <run id>]"
---

The instructions are in `.github` (shared with GitHub Copilot). Read `.github/agents/upgradelens-orchestrator.agent.md`
now and follow it exactly (its "In Claude Code" section applies to you), together with the skill it names,
from the first step to `done` or `stop`.

Arguments: $ARGUMENTS
If they name a run id, continue that run; otherwise start a new run.

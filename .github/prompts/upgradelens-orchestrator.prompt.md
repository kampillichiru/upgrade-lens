---
description: Run (or continue) the full UpgradeLens upgrade check between baseBranch and headBranch from config/upgrade-lens.json.
agent: upgradelens-orchestrator
argument-hint: "(optional) continue run <run id>"
---

Run the UpgradeLens check. Follow `.github/agents/upgradelens-orchestrator.agent.md` and the skill
`.github/skills/upgradelens-run/SKILL.md` exactly, from the first step to `done` or `stop`.

If a run id was given (${input:runId:leave empty for a new run}), continue that run: start the loop with
`python scripts/next-step.py --run-id <run id>`. Otherwise start a new run.

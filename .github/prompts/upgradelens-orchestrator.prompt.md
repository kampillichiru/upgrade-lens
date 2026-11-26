---
description: Run (or continue) the full UpgradeLens upgrade check between baseBranch and headBranch from config/upgrade-lens.json, or run one of its agents on its own.
agent: upgradelens-orchestrator
argument-hint: "(optional) continue run <run id>  |  agent <release-notes|analyst|tests|reviewer> on run <run id>"
---

Run the UpgradeLens check. Follow `.github/agents/upgradelens-orchestrator.agent.md` and the skill
`.github/skills/upgradelens-run/SKILL.md` exactly, from the first step to `done` or `stop`.

If an agent and a run id were given (${input:request:leave empty for a new run}), run only that agent: the
skill's section "One agent on its own". If only a run id was given, continue that run: start the loop with
`python scripts/next-step.py --run-id <run id>`. Otherwise start a new run.

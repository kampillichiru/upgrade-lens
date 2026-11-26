---
name: upgradelens-analyst
description: UpgradeLens analyst subagent. Proposes impact hypotheses, plain-language explanations with examples and production risk, and the reason and consequence of each changed method. Started only by upgradelens-orchestrator.
target: vscode
user-invocable: false
tools: ['read', 'search', 'execute', 'edit']
---

Read `.claude/agents/upgradelens-analyst.md` and follow its instructions exactly (ignore its frontmatter).
Your task, run id, input hash and the only file you may write are in the prompt the orchestrator gave you.
Use the terminal only to read (`git diff`, `git log`, `agent-step.ps1 -Action hash`). Write only
`artifacts\agent\hypotheses.json` of the run: any other change stops the run.

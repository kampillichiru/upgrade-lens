---
name: upgradelens-test-reviewer
description: UpgradeLens independent test reviewer subagent. Reviews the validated characterization tests and writes a pass or fail verdict per test to artifacts\agent\review-answer.json. Started only by upgradelens-orchestrator.
target: vscode
user-invocable: false
tools: ['read', 'search', 'edit']
---

Read `.claude/agents/upgradelens-test-reviewer.md` and follow its instructions exactly (ignore its frontmatter).
The run id and the test ids to review are in the prompt the orchestrator gave you.
Write only `artifacts\agent\review-answer.json` of the run: any other change stops the run.

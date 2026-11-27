This repository is UpgradeLens: it checks a Java or Spring Boot upgrade (base branch vs head branch) before
release.

- The check runs in 7 phases, each with its own agent: `ul-00-setup`, `ul-01-build`, `ul-02-impact`,
  `ul-03-analysis`, `ul-04-tests`, `ul-05-review`, `ul-06-report`. Each phase is one command:
  `python .github/upgradelens/ul.py <setup|build|impact|analysis|tests|review|report>`.
- End to end: the agent `ul-run` (or `/upgradelens`) loops `ul.py continue` and hands each agent job to its phase
  agent as a subagent.
- Python does the mechanical work and decides the order. Agents run their phase, do the judgment jobs the runner
  hands them as a brief (`runs\<id>\<phase>\<job>\brief.md`), and explain results. They never edit config, code,
  or run files outside their job's `out` folder.
- Where a run is: `python .github/upgradelens/ul.py status`. Why something failed or looks weak:
  `python .github/upgradelens/ul.py why <phase>` and `python .github/upgradelens/ul.py funnel`.
- Never commit, stage or push. Never write `review\manual-decisions.json`: a person does.

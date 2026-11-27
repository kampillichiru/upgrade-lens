# UpgradeLens

Everything is in `.github` (GitHub Copilot reads it there): `.github/copilot-instructions.md` first, then the phase
agents in `.github/agents/ul-0N-*.agent.md` and the runner in `.github/upgradelens/`.

To run a phase, follow that phase's agent file: run `python .github/upgradelens/ul.py <phase>` and, on exit 20, do
the job in the brief it prints. Tests: `python -m pytest .github/upgradelens/tests`.

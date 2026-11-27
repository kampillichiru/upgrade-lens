"""The UpgradeLens runner behind ul.py: 7 phases, each made of jobs, each job one script or one agent.

    registry   the phases and their jobs: what each needs, writes, and which Copilot agent runs it
    state      the phase folder in the run: status, input hashes (staleness), history, the current run
    log        the phase log (log.txt, events.jsonl), the run timeline (run.log), secrets masked
    jobs       how one job runs: a stage script, a Python step, or an agent job (brief, guard, checks, publish)
    quality    a green / amber / red score per phase, with the numbers behind it
    engine     runs one phase: lock, resume, jobs in order, console summary
    views      status, why, needs, funnel: read-only views of a run
    cli        the ul.py commands
"""

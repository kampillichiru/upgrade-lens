"""UpgradeLens shared library (Python port of scripts\\lib\\*.ps1).

    core           files, hashes, native commands, config, run folder, Java source reading, verdict rule
    analysis       parsers, rule matching, dependency assessment, flow coverage, finding status, test gaps
    library        library API diff (jdeps, javap, jar index)
    stages         one definition per stage: script, inputs, outputs, logs, exit codes, hint
    plan           the fixed plan of an orchestrated run and its one next action
    ledger         the run ledger and the stage lock
    facts          the run facts the plan decides on; the test writer's work list
    guards         write-scope fingerprints around an agent call
    agents         one definition per agent: inputs, data it needs, prompt, check, finish, stage that uses it
    agent_checks   the content checks of each agent's output

The parsers, matchers and decision functions are pure: they take values (dicts, lists, strings) and return
values, so tests\\ can call them without Maven, a server, git or an agent.
"""

import sys

# Console and log output: UTF-8, never fail on a character the console cannot show, and line-buffered so a
# detached stage's log (run-stage.py) fills as the stage runs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

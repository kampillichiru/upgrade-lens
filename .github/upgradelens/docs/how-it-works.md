# How UpgradeLens works

UpgradeLens checks a tech upgrade (Java, Spring Boot, a library) before release by comparing a base branch with a
head branch. This page is the design: the phases, how an agent job works, the guardrails, the logs, and how to
find the cause when a result is wrong. To run a check, see [step-by-step.md](step-by-step.md). For the git, Maven
and Java commands each stage runs, see [commands.md](commands.md).

## 1. Seven rules

1. **A phase is the unit of everything:** one command, one Copilot agent, one folder in the run, one status, one
   quality score.
2. **A phase is made of jobs; each job is one stage script, one Python step, or one agent.** Never a mix. When a
   result is wrong, the job that made it tells you whether a script or an agent is the cause.
3. **Phases share data only through files**, with declared inputs. There is no central plan: a phase runs whenever
   its inputs are there.
4. **Python decides; agents propose.** The runner decides the order, checks every agent answer, and only an
   accepted answer is used. No AI orchestrates.
5. **An agent gets a brief, not the whole run:** the task, the input files, the one folder it may write, and the
   input hash its answer must carry.
6. **Every decision is logged with its reason:** skips, staleness, rejections, reuse.
7. **A phase can run again at any time.** Its previous attempt moves to `history\`, and the phases that used its
   outputs show as stale.

## 2. Phases and jobs

```
00-setup -> 01-build -> 02-impact -> 03-analysis -> 04-tests -> 05-review -> 06-report
 script      script      scripts     2 agents +     scripts +    1 agent      scripts
                                     1 script       2 agents
```

| Phase | Agent | Jobs, in order | Needs (from) | Main outputs (`runs\<id>\artifacts\`) |
|---|---|---|---|---|
| 00-setup | `ul-00-setup` | setup (stage 0) | the config | `environment.json`, worktrees |
| 01-build | `ul-01-build` | build (stage 1) | 00 | `build-results.json`, `test-results.json` |
| 02-impact | `ul-02-impact` | changes (2), deps (3), rules (4) | 00, 01 | `change-inventory.json`, `dependency-diff.json`, `api-diff.json`, `library-api-diff.json`, `impact-findings.json`, `changed-methods.json`, `reach.json` |
| 03-analysis | `ul-03-analysis` | **release-notes**, **analyst**, merge (4 again) | 02 | `agent\release-notes.json`, `agent\hypotheses.json`; findings rewritten with the analyst's answer |
| 04-tests | `ul-04-tests` | probes (5), gaps, **write**, validate (5), **repair**, revalidate (5) | 02 (03 optional) | `behavior-results.json`, `test-gaps.json`, `agent\tests-manifest.json`, `agent\tests\` |
| 05-review | `ul-05-review` | **review** | 04 | `agent\review.json` |
| 06-report | `ul-06-report` | flows (6), manual (7), report (8), record (9), verdict (10) | 02, 04 (05 optional) | `reports\summary.md`, `reports\report.md`, `verdict.json` |

**Bold** jobs are agent jobs. A job that is not needed is skipped with the reason (for example: no library change,
so no release-notes job; every test validated, so no repair). Without agents (`--no-agents`), every agent job is
skipped, the phase scores amber, and the report says what was not covered.

`python .github/upgradelens/ul.py needs <phase>` lists a phase's inputs, whether each is there, and which phase
writes it. The definitions live in one place: `scripts\runner\registry.py` (phases and jobs), `scripts\lib\stages.py`
(each stage script's inputs and outputs) and `scripts\lib\agents.py` (each agent job's inputs, task and checks).

## 3. The run folder

```
runs\<id>\
  run.log                     timeline of the run: one line per phase or job start, end, skip, wait, reject
  status.json  config\  repo.git\  worktrees\<base>\  worktrees\<head>\
  artifacts\                  the shared working files, read by the stage scripts (only the runner writes them)
  artifacts\agent\            accepted agent answers, published by the runner; usage.json (agent calls)
  02-impact\                  one folder per phase:
    status.json               state, exit code, each job's state and reason, the input hashes it ran on
    quality.json              green / amber / red, the numbers, the notes
    log.txt  events.jsonl     what happened and why (text, and the same as JSON)
    tools\<job>.log           raw output of each stage script (Maven, git, the JVMs), secrets masked
    outputs\                  a copy of what this phase wrote, as it was when the phase ended
    history\<n>\              the previous attempts of the phase
  03-analysis\analyst\        one folder per agent job:
    brief.md  brief.json      exactly what the agent was asked, and what it was given
    out\                      the only folder the agent may write
    guard.json                the fingerprint taken before the agent started
    checks.json               how its answer was judged
```

## 4. How a phase runs

`python .github/upgradelens/ul.py <phase>`:

1. Locks the run (one phase at a time; the lock of a killed process is cleared).
2. Moves the previous attempt to `history\`, unless an agent job is waiting (then it resumes at that job).
3. Checks that every input is there, and records the hash of each (for staleness).
4. Runs the jobs in order. A job whose condition is not met is skipped with the reason.
5. Stops at the first failed job (its exit code is the phase's), or at an agent job that waits (exit 20).
6. When done: the quality score, a copy of the outputs, and a console summary of about 10 lines.

Long phases (build, probes, test runs) run in a background process: the command returns exit 10 after 8 minutes
(`--wait-minutes`) and the phase keeps running, also when the terminal or chat closes.
`ul.py wait` waits for it. `--foreground` runs it in the terminal instead.

**End to end:** `ul.py continue` runs the next phase that is not done (not run, stale, waiting or failed); without a
run it runs setup. The conductor agent `ul-run` (also `/upgradelens`) repeats it: exit 0 go on, 10 wait, 20 start the
phase's agent as a subagent, anything else `why` and stop. It never decides the order and never runs a phase itself.

Options: `--run <id>` (any run; default the current run), `--job <job>` (only that job), `--from <job>` (that job
to the end of the phase), `--restart` (start over, also when an agent job waits), `--no-agents`, `--force` (ask
the agent again even when its answer could be reused), `--follow` (print the log as it runs), `--verbose`.

## 5. How an agent job runs

```
ul.py analysis            runner: check inputs -> brief.md + guard fingerprint -> exit 20 (waiting)
  agent ul-03-analysis:   read brief.md and jobs\analyst.md -> write 03-analysis\analyst\out\hypotheses.json
ul.py analysis            runner: compare fingerprint -> run checks ->
                            accepted:  publish to artifacts\agent, continue with the next job
                            rejected:  new brief.md with the problems -> exit 20 (one retry by default)
                            no attempt left: exit 6;  wrote outside its folder: exit 5
```

The agent and the runner use the same command; the agent never records its own call. An accepted answer whose
inputs did not change is reused on the next run of the phase, without an agent call (`--force` asks again). The
job instructions are in `.github\upgradelens\jobs\` (analyst, release-notes, test-writer, test-reviewer).

## 6. Guardrails

An instruction to an AI is a request, so the runner enforces the rules. Three layers:

| Layer | What it does |
|---|---|
| **Tools** (agent file) | Agents of script-only phases (00, 01, 02, 06) have no edit tool. Only `ul-03-analysis` has web access. |
| **Instructions** (agent file, brief, job file) | Run only your phase's command; write only your `out` folder; no Maven, no git that changes state, no other phase; when evidence is missing, say "not determined" instead of guessing. |
| **Runner** (decides) | See below. |

| Guard | Rejects when |
|---|---|
| Write scope | anything outside the job's `out` folder changed: artifacts, other phases or jobs, the worktrees' git state, config, `.github` (exit 5) |
| Input hash | the answer was made for other inputs (stale brief) |
| Coverage | an item of the task has no answer and no reason: a gap with no test, a changed method with no reason, a high or medium rule with no explanation, a review missing a test |
| Evidence | a release note confirmed without an https source and a quote; a test whose injection does not occur exactly once |
| Format and limits | invalid JSON, a missing field, more tests than `orchestration.maxTests` |
| Independence | the tests' writer and the reviewer are the same agent identity (the reviewer is also told not to read the writer's brief; that part is an instruction, not checked) |
| Attempts and budget | more than `orchestration.maxAgentAttempts` per job, or more than `agentBudget` calls per run |

The scripts have their own: one lock per run, inputs checked before a job starts, a timeout on every external
command, nothing ever committed or pushed, the repository under test changed only in its worktrees, and secrets
(Maven passwords, tokens, URLs with credentials) masked in every log.

## 7. Logs

| File | What is in it |
|---|---|
| `runs\<id>\run.log` | the run's timeline: phase and job start, end, skip, wait, reject, fail |
| `<phase>\log.txt` | everything: each job's command (masked), duration, exit code in words, each output with its summary, input hashes (`--verbose`), quality notes, why a job was skipped, why an answer was rejected |
| `<phase>\events.jsonl` | the same events as JSON |
| `<phase>\tools\<job>.log` | raw output of the stage script |
| `runs\<id>\logs\` | the stage scripts' own logs (`build-<branch>.log`, `phase4.log`, `app-<side>.err.log`, `tests-*.log`) |
| `<job>\brief.md`, `checks.json` | what an agent was asked, how its answer was judged |

A log line: `2026-11-27T10:31:04 WARN  02-impact/rules  quality note: [amber] rule R-X fired on 41 of 52 changed files`.

## 8. Quality: when a result is weak, not broken

"It ran" is not enough. Each phase scores its own result, so the first phase that is not green is where to look:

| Phase | Red or amber when |
|---|---|
| 00-setup | a commit or worktree is missing; the base branch moved on after the merge base |
| 01-build | the build did not finish; no tests ran; no JaCoCo report (head: red); tests now fail on head or were removed |
| 02-impact | no changed file; no flows; no changed method executed by a test; a rule fires on most changed files (too broad) |
| 03-analysis | agents off; more than 30% of change reasons "not determined"; release notes contradicted or mostly without source |
| 04-tests | the application did not start; probe errors; gaps but no tests; fewer than half the tests validated; many gaps uncovered |
| 05-review | no review; tests failed review |
| 06-report | verdict blocked (a result) or safe with listed tests; manual items without a decision |

`ul.py funnel` shows where results drop along the chain: changed methods -> executed by a test -> findings -> gaps
-> tests written -> validated -> passed review. The first big drop names the job to improve:

| Symptom in the report | First place it goes wrong | Where to improve |
|---|---|---|
| A real risk was missed | not in `impact-findings.json` | `config\rules\` (02-impact), or the analyst (`jobs\analyst.md`) |
| | a finding, but no gap | the gap logic (`find_test_gaps`, `scripts\lib\analysis.py`) |
| | a gap, but no test | the test writer (`jobs\test-writer.md`); read `uncovered` reasons |
| | a test, but not validated | the test writer, or the build environment (`04-tests\tools\validate.log`) |
| A false alarm | a rule fires too widely (02-impact quality) | that rule in `config\rules\` |
| An explanation is vague | `agent\hypotheses.json` | `jobs\analyst.md` |
| A weak test passed review | `agent\review.json` | `jobs\test-reviewer.md` |
| The verdict is wrong while the above is right | `verdict.json` | `scripts\phase10-verdict.py` |

To test a change to a rule or a job file: rerun only that phase (or `--job`), then compare its `log.txt`,
`quality.json` and `outputs\` with `history\<n>\`.

## 9. Token use

- Four of the seven phases (00, 01, 02, 06) are scripts only, and so are most jobs of 03 and 04: run them in the
  terminal and they cost nothing. From chat they cost one short request (one command, a summary of about 10 lines).
- Agents get a brief with the exact task and input files, not whole artifacts; the brief records its size
  (`estimatedInputTokens`, also in the log).
- A rejected answer gets the list of problems, not a new task; an accepted answer is reused while its inputs do
  not change.
- No AI orchestrator: the runner decides the order, so no tokens go to the loop.
- A full run is about 7 chat requests: one per agent, of which 3 (analysis, tests, review) do real AI work.

## 10. Exit codes (every phase)

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | done, verdict blocked (06-report): a result, not a failure |
| 2 | inputs, config or usage problem: nothing was changed |
| 3 | tool failure (git, Maven, Java, or an error in a stage script) |
| 4 | a build failed on one branch |
| 5 | an agent changed files outside its folder: answer rejected |
| 6 | an agent answer failed its checks on every attempt |
| 10 | still running in the background (`ul.py wait`) |
| 20 | an agent job waits: read the brief, write the answer, run the same command again |

## 11. Stage scripts on their own

The stage scripts (`scripts\phaseN-*.py`) still run on their own. `--standalone --run-id <id>` reruns one on any run
and writes only to `runs\<id>\standalone-stages\`, to try a stage script change without touching the run. For
everything else, use the phases.

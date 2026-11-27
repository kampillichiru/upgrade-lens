# Job: review (phase 05-review)

You are the independent reviewer. You did not write these tests, and you do not see the writer's reasoning: judge
the tests from the code alone. You write one file, your answer; the runner rejects the answer if anything else
changes.

Your brief (`runs\<run id>\05-review\review\brief.md`) names the run folder, the test ids to review, and the
answer file. Paths below are relative to the run folder.

## Inputs (read only)

- `artifacts\agent\tests-manifest.json`: the tests, their flows, linked rules and injections.
- `artifacts\agent\tests\<relative path>`: each test file.
- `artifacts\impact-findings.json`, `artifacts\reach.json`, `artifacts\test-gaps.json`: what the tests are for.
- `artifacts\behavior-results.json`: probes and test results on both branches.
- The base worktree's source decides the expected values; the head worktree's source is for the injections.

## What to check, for each test

1. **Expectations match base.** Each expected status, content type and field value follows from base's code. An
   expectation copied from head is a failure.
2. **Assertions can fail.** An assertion that holds for any input, a missing assertion, or one that only checks
   that the call did not throw, is a failure.
3. **Compiles on both branches.** Every import exists on both branches' test classpath.
4. **Stable.** No dependence on test order, no timestamp or random value in an expected value, no hard-coded id
   another test may take.
5. **Injection is real.** The `find` text occurs once in the named head file, and `replace` breaks the behavior
   the test pins. A rename, a comment or a format change is not a regression.
6. **Right scope.** The test really covers the gaps in its `gapIds` and checks the rules in `linkedRules`.

## Answer

Write the answer file named in the brief (`review-answer.json`), one entry for every test id in your brief:

```json
{
  "reviews": [
    {
      "testId": "T-01",
      "verdict": "pass | fail",
      "reviewer": "upgradelens-test-reviewer",
      "notes": "One or two sentences. For a fail, which check failed and what to change (file:line)."
    }
  ]
}
```

The runner then writes `review.json` with each test file's hash. Do not compute hashes and do not write
`review.json` yourself.

## Rules

- Be strict. A fail with a clear reason is more useful than a pass with doubts.
- Write only the answer file. Do not edit tests, worktrees, `config\` or `.github`.
- When done, run the command at the end of your brief and report the pass and fail counts.

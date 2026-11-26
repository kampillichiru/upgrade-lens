<#
.SYNOPSIS
  SCRIPTS ONLY (needs -ScriptsOnly). Runs the UpgradeLens stages in order, without the agent steps.
  Stops at the first failed stage, writes a partial report for it, and returns that stage's exit code.
  The full check (with agents) runs from the upgradelens-orchestrator agent: next-step.ps1 and run-stage.ps1.
  A run started by the orchestrator cannot be continued with this script.

.DESCRIPTION
  Stage 0   phase0-setup.ps1      validate config and prerequisites, run folder, worktrees, merge base, environment
  Stage 1   phase1-build.ps1      Maven on both branches, in parallel, one JDK, worktree and log per side
  Stage 2   phase2-changes.ps1    changed-files inventory, three-dot (base...head), methods per hunk, commits
  Stage 3   phase3-deps.ps1       dependency trees and conflicts, dependency assessment, application and library API
  Stage 4   phase4-impact.ps1     rules, flows, reach, coverage, untraced code, changed methods with reasons
  Stage 5   phase5-behavior.ps1   startup, dumps, probes, proposed tests on both branches, gaps
  Stage 6   phase6-flows.ps1      scope per flow: full, targeted, smoke
  Stage 7   phase7-manual.ps1     manual review items, with expected master results and owners
  Stage 8   phase8-output.ps1     validated tests placed uncommitted in the head worktree, findings.json, report
  Stage 9   phase9-review.ps1     agent review and manual decisions, checked against their files
  Stage 10  phase10-verdict.ps1   verdict and exit code

  Every stage gets the same config: -Config is passed to each, and $env:UPGRADELENS_CONFIG is set for the
  agent steps the orchestrator runs in between.

  Agent steps (analyst, release notes, test writer, reviewer) are not scripts. They run between stages, from
  the orchestrator (.claude\agents). Their outputs are checked by stages 4, 5, 7, 9 and 10 by input hash.
  When an agent output is missing or stale, the run still completes and the verdict lists it, so it is never
  a silent pass.

  On a failed stage the pipeline still writes reports\report.md (stage 8 with -ReportOnly) so the failure is
  reported, not only logged. After a failed build (stage 1, exit 4) it first runs stage 2 too, so the report
  shows what changed next to what failed.

  Stage 10 exit codes: 0 safe or safe-with-listed-tests, 1 blocked. Other stages: 2 usage or config,
  3 tool failure, 4 build or tests failed (stage 1).

.PARAMETER RunId
  Run to continue. Needed with -From (the newest run is used when it is left out). Without it, stage 0
  creates a new run.

.PARAMETER From
  First stage to run (0 to 10). Stages before it are not run. Use it to continue after an agent step,
  for example -From 4 after the analyst, -From 5 after the test writer, -From 9 after the reviewer.

.PARAMETER Through
  Last stage to run. Default 10.

.PARAMETER Config
  Config file. Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.EXAMPLE
  .\scripts\run-pipeline.ps1
  .\scripts\run-pipeline.ps1 -Config D:\configs\orders-boot4.json
  .\scripts\run-pipeline.ps1 -RunId 20261126-131717 -From 9
#>
param(
    [string]$RunId,
    [ValidateRange(0, 10)][int]$From = 0,
    [ValidateRange(0, 10)][int]$Through = 10,
    [string]$Config,
    [switch]$ScriptsOnly
)

$ErrorActionPreference = "Stop"
if (-not $ScriptsOnly) {
    Write-Host "run-pipeline.ps1 runs the scripts only: no analyst, release notes, test writer or reviewer, so no plain"
    Write-Host "explanations, no change reasons and no new tests. The full check runs from the upgradelens-orchestrator"
    Write-Host "agent (or the upgradelens-run skill), which drives scripts\next-step.ps1 and scripts\run-stage.ps1."
    Write-Host "For a scripts-only run (for example CI facts), pass -ScriptsOnly."
    exit 2
}
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
$Config = Resolve-ConfigPath -Config $Config -RootDir $RootDir

if (-not (Test-Path -LiteralPath $Config)) {
    Write-Host "Config not found: $Config"
    exit 2
}
if ($Through -lt $From) { Write-Host "-Through ($Through) is before -From ($From)."; exit 2 }
$env:UPGRADELENS_CONFIG = $Config
$cfg = Read-JsonFile $Config
$runsRoot = Get-RunsRoot -Cfg $cfg -RootDir $RootDir

if ($From -gt 0 -and -not $RunId) {
    $RunId = Get-ChildItem -LiteralPath $runsRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "status.json") } |
        Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty Name
    if (-not $RunId) { Write-Host "No run to continue under $runsRoot."; exit 2 }
    Write-Host "Continuing run $RunId from stage $From."
}
if ($RunId -and -not (Test-Path -LiteralPath (Join-Path (Join-Path $runsRoot $RunId) "status.json"))) {
    Write-Host "Run not found: $(Join-Path $runsRoot $RunId)"
    exit 2
}

function Invoke-Stage([string]$Script, [hashtable]$Arguments) {
    # Runs one stage script and returns its exit code. A stage that throws must never look like a pass.
    try {
        $global:LASTEXITCODE = $null
        # Out-Host: anything a stage writes goes to the console, never into this function's return value.
        & (Join-Path $PSScriptRoot $Script) @Arguments | Out-Host
        $code = $LASTEXITCODE
    } catch {
        Write-Host "$Script threw: $($_.Exception.Message)"
        $code = 3
    }
    if ($null -eq $code) { $code = 3 }
    return [int]$code
}

function Write-FailureReport([string]$Id) {
    # The failure goes into the report too, so it is never only in a log.
    $code = Invoke-Stage "phase8-output.ps1" @{ RunId = $Id; ReportOnly = $true; Config = $Config }
    $report = Join-Path (Join-Path $runsRoot $Id) "reports\report.md"
    if ($code -eq 0) { Write-Host "Partial report: $report" } else { Write-Host "The partial report could not be written (exit $code)." }
}

if ($From -le 0 -and $Through -ge 0) {
    $before = @(Get-ChildItem -LiteralPath $runsRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    $code = Invoke-Stage "phase0-setup.ps1" @{ Config = $Config }
    $RunId = Get-ChildItem -LiteralPath $runsRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $before -notcontains $_.Name } |
        Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty Name
    if ($code -ne 0) {
        Write-Host "Stage 0 failed (exit $code). Pipeline stopped.$(if ($RunId) { ' Run folder: ' + (Join-Path $runsRoot $RunId) })"
        exit $code
    }
    if (-not $RunId) { Write-Host "Stage 0 reported success but no new run folder was found under $runsRoot. Pipeline stopped."; exit 3 }
}

$stages = @(
    @{ n = 1;  title = "build and test both branches";      script = "phase1-build.ps1" },
    @{ n = 2;  title = "changed files";                     script = "phase2-changes.ps1" },
    @{ n = 3;  title = "dependencies and API";              script = "phase3-deps.ps1" },
    @{ n = 4;  title = "impact evaluation";                 script = "phase4-impact.ps1" },
    @{ n = 5;  title = "behavior checks";                   script = "phase5-behavior.ps1" },
    @{ n = 6;  title = "flow selection";                    script = "phase6-flows.ps1" },
    @{ n = 7;  title = "manual review items";               script = "phase7-manual.ps1" },
    @{ n = 8;  title = "output and new tests";              script = "phase8-output.ps1" },
    @{ n = 9;  title = "test and report review";            script = "phase9-review.ps1" },
    @{ n = 10; title = "verdict";                           script = "phase10-verdict.ps1" }
)

$runDir = Join-Path $runsRoot $RunId
foreach ($s in $stages) {
    if ($s.n -lt $From -or $s.n -gt $Through) { continue }
    Write-Host "== Stage $($s.n): $($s.title) (run $RunId) =="
    $code = Invoke-Stage $s.script @{ RunId = $RunId; Config = $Config }
    if ($s.n -eq 10) {
        switch ($code) {
            0 { Write-Host "Verdict: safe or safe with listed tests. Report: $runDir\reports\report.md"; exit 0 }
            1 { Write-Host "Verdict: BLOCKED. See $runDir\verdict.json and $runDir\reports\report.md"; exit 1 }
            default { Write-Host "Stage 10 failed (exit $code)."; Write-FailureReport $RunId; exit $code }
        }
    }
    if ($code -ne 0) {
        Write-Host "Stage $($s.n) ($($s.title)) failed (exit $code). Pipeline stopped. Run folder: $runDir"
        if ($s.n -eq 1 -and $code -eq 4) {
            Write-Host "== Stage 2: changed files, for the failure report (run $RunId) =="
            [void](Invoke-Stage "phase2-changes.ps1" @{ RunId = $RunId; Config = $Config })
        }
        Write-FailureReport $RunId
        exit $code
    }
}

Write-Host "Stages $From to $Through done for run $RunId. Run folder: $runDir"
exit 0

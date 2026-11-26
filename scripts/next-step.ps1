<#
.SYNOPSIS
  Tells the UpgradeLens orchestrator the one next action of a run, as JSON. The orchestrator never chooses
  the order itself: it runs this, does exactly the action it prints, and runs it again.

.DESCRIPTION
  The plan is fixed (lib\orchestration.ps1, Get-RunPlan):

    stage 0, 1, 2, 3 -> release-notes agent -> stage 4 -> analyst agent -> stage 4 again (merge)
    -> stage 5 (probes) -> test writer agent (every gap in artifacts\test-gaps.json) -> stage 5 (tests)
    -> [repair round: test writer, stage 5] -> stage 6, 7, 8 -> reviewer agent -> stage 9, 10

  A step that is not needed (for example no library change for the release-notes agent) is recorded in the
  ledger as skipped, with the reason, and the next step is printed. Nothing is skipped silently.

  Actions (field "action"):
    run-stage   run "command" (scripts\run-stage.ps1). Exit 10 from it means still running: run "waitCommand".
    wait        a stage is running: run "command" (run-stage.ps1 -Wait).
    run-agent   run "beginCommand"; give its "prompt" unchanged to the subagent "subagent"; then run "endCommand".
    end-agent   an agent step was begun and not ended: run "endCommand".
    stop        the run cannot continue. Report "reason" and "report". Do not work around it.
    done        report the verdict, "report" and "verdict".

  Exit codes: 0 an action to do or done, 1 stop, 2 usage (no run, no ledger).

.PARAMETER RunId
  The run. Leave it out only to start a new run: the action is then stage 0 (run-stage.ps1 -New).

.EXAMPLE
  .\scripts\next-step.ps1
  .\scripts\next-step.ps1 -RunId 20261126-152851
#>
param(
    [string]$RunId,
    [string]$Config
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
. (Join-Path $PSScriptRoot "lib\analysis.ps1")
. (Join-Path $PSScriptRoot "lib\orchestration.ps1")

$Config = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $Config)) { Write-Host "Config not found: $Config"; exit 2 }
$liveCfg = Read-JsonFile $Config
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$scripts = $PSScriptRoot

function Write-Action($Obj, [int]$Code = 0) {
    Write-Output (ConvertTo-Json -InputObject $Obj -Depth 6)
    exit $Code
}

if (-not $RunId) {
    Write-Action ([ordered]@{
        action  = "run-stage"; key = "stage-0"; stage = 0
        reason  = "start a new run: config check, clone, worktrees, merge base"
        command = ".\scripts\run-stage.ps1 -New -Config `"$Config`""
        next    = "The command prints the new run id. Pass -RunId <id> to every later next-step.ps1 call."
    })
}

$RunDir = Join-Path $runsRoot $RunId
if (-not (Test-Path -LiteralPath (Join-Path $RunDir "status.json"))) { Write-Host "Run not found: $RunDir"; exit 2 }
$ledger = Read-Ledger $RunDir
if (-not $ledger) {
    Write-Host "Run $RunId has no orchestration ledger: it was not started by the orchestrator (run-stage.ps1 -New). Start a new run."
    exit 2
}
$runCfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$maxRepair = [int](Get-ConfigValue $runCfg "orchestration.maxTestRepairRounds" 1)
$maxAttempts = [int](Get-ConfigValue $runCfg "orchestration.maxAgentAttempts" 2)
$plan = Get-RunPlan -MaxRepairRounds $maxRepair
$cfgArg = "-Config `"$Config`""

for ($guard = 0; $guard -lt 50; $guard++) {
    $ledger = Read-Ledger $RunDir
    $facts = Get-RunFacts -RunDir $RunDir -Ledger $ledger
    $next = Get-NextAction -Plan $plan -Ledger $ledger -Facts $facts -MaxAgentAttempts $maxAttempts
    if ($next.action -ne "skip") { break }
    $kind = (@($plan | Where-Object { $_.key -eq $next.key }) | Select-Object -First 1).kind
    Set-LedgerEntry -RunDir $RunDir -Key $next.key -Kind $kind -State "skipped" -Reason $next.reason
}

$report = Join-Path $RunDir "reports\report.md"
$out = [ordered]@{ runId = $RunId; action = $next.action; key = $next.key; reason = $next.reason }
switch ($next.action) {
    "run-stage" {
        $out.stage = $next.stage
        $out.command = ".\scripts\run-stage.ps1 -RunId $RunId -Key $($next.key) $cfgArg"
        $out.waitCommand = ".\scripts\run-stage.ps1 -RunId $RunId -Wait $cfgArg"
    }
    "wait" {
        $out.command = ".\scripts\run-stage.ps1 -RunId $RunId -Wait $cfgArg"
    }
    "run-agent" {
        $out.step = $next.step
        $out.attempt = $next.attempt
        $out.subagent = $script:AgentFiles[$next.step].agent
        $out.beginCommand = ".\scripts\agent-step.ps1 -RunId $RunId -Key $($next.key) -Action begin $cfgArg"
        $out.endCommand = ".\scripts\agent-step.ps1 -RunId $RunId -Key $($next.key) -Action end $cfgArg"
    }
    "end-agent" {
        $out.step = $next.step
        $out.subagent = $script:AgentFiles[$next.step].agent
        $out.endCommand = ".\scripts\agent-step.ps1 -RunId $RunId -Key $($next.key) -Action end $cfgArg"
    }
    "stop" {
        $out.report = $(if (Test-Path -LiteralPath $report) { $report } else { "" })
        $out.ledger = Get-LedgerPath $RunDir
        Write-Action $out 1
    }
    "done" {
        $v = Read-JsonFile (Join-Path $RunDir "verdict.json")
        $out.verdict = $(if ($v) { $v.verdict } else { "" })
        $out.blocking = $(if ($v) { @($v.blocking) } else { @() })
        $out.report = $report
        $out.newTests = Join-Path $RunDir "artifacts\new-tests.json"
    }
}
Write-Action $out 0

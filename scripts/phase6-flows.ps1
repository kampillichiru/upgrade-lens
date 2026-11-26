<#
.SYNOPSIS
  Stage 6 of UpgradeLens (flow selection): decides the regression scope of each flow, from the findings,
  the behavior checks and the coverage.

.DESCRIPTION
  Scope per flow (approach section 6 and 7):
    full      keep the full regression. Any confirmed behavior change, any untraced code in the flow,
              or any affecting finding that no check has verified.
    targeted  reduced: targeted tests plus a smoke suite. Only when every affecting finding is verified
              by a check that matched master, all reached code is covered and traced, and nothing is confirmed.
    smoke     smoke only: no affecting finding and no untraced code.

  The reduction is never based on a pass alone. A flow with untraced code stays full. Accepted
  differences (phase 9) do not reduce a flow in this version: they unblock the verdict only.

  Time saved is not estimated: the run has no regression duration data.

  Writes artifacts\flows.json.

  Exit codes:
    0  written
    2  usage or run problem (no run, stage 5 not done)

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 5 is done.

.EXAMPLE
  .\scripts\phase6-flows.ps1 -RunId 20261126-131717
#>
param(
    [string]$RunId,
    [string]$Config
)

$ErrorActionPreference = "Stop"
$ToolVersion = "1.0.0"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
. (Join-Path $PSScriptRoot "lib\analysis.ps1")

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase5-done", "phase6-done", "phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe")
if (-not $RunId) { Write-Host "No run with stage 5 done under $runsRoot. Run phase5-behavior.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$StatusFile = Join-Path $RunDir "status.json"
$script:StageLog = Join-Path $RunDir "logs\phase6.log"
$status = Read-JsonFile $StatusFile
$impact = Read-JsonFile (Join-Path $ArtifactsDir "impact-findings.json")
$reach = Read-JsonFile (Join-Path $ArtifactsDir "reach.json")
$behavior = Read-JsonFile (Join-Path $ArtifactsDir "behavior-results.json")
if (-not $status -or -not $impact -or -not $reach -or -not $behavior) { Write-Host "Run incomplete: $RunDir (needs stages 4 and 5)"; exit 2 }

Write-StageLog "phase 6 flow selection for run $RunId"
$checks = @($behavior.checks)

# Finding status from the checks only (decisions are applied at the verdict).
$findingStatus = @{}
foreach ($f in @($impact.findings)) {
    $findingStatus[$f.id] = Get-FindingStatus -Finding $f -Checks $checks
}

$flowRows = New-Object System.Collections.Generic.List[object]
foreach ($fl in @($reach.flows)) {
    $affecting = @($impact.findings | Where-Object { @($_.affectedFlows) -contains $fl.id })
    $affectingIds = @($affecting | ForEach-Object { $_.id })
    $statuses = @($affecting | ForEach-Object { $findingStatus[$_.id] })
    $untraced = @($fl.untraced)
    $anyConfirmed = (@($statuses | Where-Object { $_ -eq "confirmed" }).Count -gt 0)
    $scope = Get-FlowScope -AffectingStatuses $statuses -UntracedCount $untraced.Count -AnyConfirmed $anyConfirmed

    $reasons = New-Object System.Collections.Generic.List[string]
    if ($anyConfirmed) { $reasons.Add("confirmed behavior change in this flow") }
    if ($untraced.Count -gt 0) { $reasons.Add("$($untraced.Count) reached symbol(s) with no test execution: $($untraced -join ', ')") }
    $openCount = @($statuses | Where-Object { $_ -eq "open" -or $_ -eq "hypothesis" }).Count
    if ($openCount -gt 0) { $reasons.Add("$openCount affecting finding(s) not verified by any check") }
    if ($scope -eq "targeted") { $reasons.Add("every affecting finding verified, all reached code covered and traced") }
    if ($scope -eq "smoke") { $reasons.Add("no affecting finding and no untraced code") }

    $probes = @($behavior.probes | Where-Object { $_.flowId -eq $fl.id } | ForEach-Object { $_.id })
    $tests = @($behavior.tests | Where-Object { @($_.flowIds) -contains $fl.id } | ForEach-Object { $_.id })

    $flowRows.Add([pscustomobject]@{
        id               = $fl.id
        kind             = $fl.kind
        scope            = $scope
        reason           = ($reasons -join "; ")
        affectingFindings = $affectingIds
        untraced         = $untraced
        probes           = $probes
        tests            = $tests
    })
    Write-StageLog ("  {0,-8} {1}: {2}" -f $scope, $fl.id, ($reasons -join "; "))
}

$rows = $flowRows.ToArray()
$doc = [ordered]@{
    schemaVersion   = 1
    toolVersion     = $ToolVersion
    runId           = $RunId
    timeSaved       = "not estimated: no regression duration data for this application"
    summary         = [ordered]@{
        flows   = $rows.Count
        full    = @($rows | Where-Object { $_.scope -eq "full" }).Count
        targeted = @($rows | Where-Object { $_.scope -eq "targeted" }).Count
        smoke   = @($rows | Where-Object { $_.scope -eq "smoke" }).Count
    }
    findingStatus  = @($impact.findings | ForEach-Object { [pscustomobject]@{ id = $_.id; ruleId = $_.ruleId; status = $findingStatus[$_.id] } })
    flows          = $rows
}
Write-JsonFile (Join-Path $ArtifactsDir "flows.json") $doc

Write-StageLog ("flows: {0} full, {1} targeted, {2} smoke" -f $doc.summary.full, $doc.summary.targeted, $doc.summary.smoke)
$statusCounts = @($doc.findingStatus | Group-Object status | ForEach-Object { "$($_.Name) $($_.Count)" }) -join ", "
Write-StageLog "findings by status: $statusCounts"

Set-StageStep $status "flows" "done" "" (Join-Path $ArtifactsDir "flows.json")
Set-RunState $status "phase6-done" "phase6"
Write-JsonFile $StatusFile $status
Write-StageLog "phase 6 done: flow scope written for run $RunId"
exit 0

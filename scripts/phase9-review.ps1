<#
.SYNOPSIS
  Stage 9 of UpgradeLens (test and report review): records the agent review of the validated tests
  and the human decisions on the manual items, and checks both against the files they refer to.

.DESCRIPTION
  Agent review (approach section 9): artifacts\agent\review.json, written from the reviewer agent's
  answer. For each validated test, a review counts only when:
    - its verdict is "pass"
    - its fileSha256 matches the proposal file on disk now, and the copy stage 8 placed in the head worktree
    - its reviewer is not the test's writer (the reviewer did not write the test)

  Manual review: review\manual-decisions.json, written by a person. Each entry:
      { "itemId": "MI-001", "decision": "accept|reject|needs-test|defer", "reviewer": "name", "note": "..." }
  A decision without an itemId from artifacts\manual-items.json, a decision outside the four values, or a
  missing reviewer is recorded as invalid and does not count.

  Writes review\review-record.json. Stage 10 reads it. This stage does not decide the verdict.

  Exit codes:
    0  record written
    2  usage or run problem (no run, stage 8 not done)

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 8 is done.

.EXAMPLE
  .\scripts\phase9-review.ps1 -RunId 20261126-131717
#>
param(
    [string]$RunId,
    [string]$Config
)

$ErrorActionPreference = "Stop"
$ToolVersion = "1.0.0"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe")
if (-not $RunId) { Write-Host "No run with stage 8 done under $runsRoot. Run phase8-output.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$ReviewDir = Join-Path $RunDir "review"
$StatusFile = Join-Path $RunDir "status.json"
$script:StageLog = Join-Path $RunDir "logs\phase9.log"
New-Item -ItemType Directory -Force -Path $ReviewDir | Out-Null
$status = Read-JsonFile $StatusFile
$behavior = Read-JsonFile (Join-Path $ArtifactsDir "behavior-results.json")
$manual = Read-JsonFile (Join-Path $ArtifactsDir "manual-items.json")
$manifest = Read-JsonFile (Join-Path $ArtifactsDir "agent\tests-manifest.json")
$agentReview = Read-JsonFile (Join-Path $ArtifactsDir "agent\review.json")
$decisionsIn = Read-JsonFile (Join-Path $ReviewDir "manual-decisions.json")
if (-not $status -or -not $behavior -or -not $manual) { Write-Host "Run incomplete: $RunDir (needs stages 5 to 8)"; exit 2 }

Write-StageLog "phase 9 review for run $RunId"

# ---------- agent review ----------

$validated = @($behavior.tests | Where-Object { $_.validated })
$placedRecord = Read-JsonFile (Join-Path $ArtifactsDir "new-tests.json")
$agentRows = New-Object System.Collections.Generic.List[object]
$agentState = "not-run"
if ($agentReview) { $agentState = "done" }
$writer = if ($manifest) { [string]$manifest.writer } else { "" }

foreach ($t in $validated) {
    $file = Join-Path $ArtifactsDir ("agent\tests\" + ($t.relativePath -replace '/', '\'))
    $currentHash = if (Test-Path -LiteralPath $file) { Get-FileSha256 $file } else { "" }
    $rev = $null
    if ($agentReview) { $rev = @($agentReview.reviews | Where-Object { $_.testId -eq $t.id }) | Select-Object -First 1 }
    # The copy stage 8 placed in the head worktree must still be the reviewed file.
    $placedHash = $currentHash
    $placed = if ($placedRecord) { @($placedRecord.placed | Where-Object { $_.relativePath -eq $t.relativePath }) | Select-Object -First 1 } else { $null }
    if ($placed) {
        $placedPath = Join-Path $placedRecord.location ($t.relativePath -replace '/', '\')
        $placedHash = if (Test-Path -LiteralPath $placedPath) { Get-FileSha256 $placedPath } else { "" }
    }
    $result = "missing"
    if ($rev) {
        if ($rev.fileSha256 -ne $currentHash -or $placedHash -ne $currentHash) { $result = "stale" }
        elseif ($rev.reviewer -and $writer -and ([string]$rev.reviewer).Trim().ToLower() -eq $writer.Trim().ToLower()) { $result = "same-author" }
        elseif ($rev.verdict -eq "pass") { $result = "reviewed" }
        else { $result = "failed" }
    }
    $agentRows.Add([pscustomobject]@{
        testId     = $t.id
        relativePath = $t.relativePath
        result     = $result
        reviewer   = if ($rev) { $rev.reviewer } else { "" }
        verdict    = if ($rev) { $rev.verdict } else { "" }
        notes      = if ($rev) { $rev.notes } else { "" }
    })
    Write-StageLog ("  agent review {0}: {1}" -f $t.id, $result)
}
if ($validated.Count -eq 0) { $agentState = if ($agentState -eq "done") { "done" } else { "not-needed" } }

# ---------- manual decisions ----------

$itemIds = @{}
foreach ($i in @($manual.items)) { $itemIds[$i.id] = $i }
$allowed = @("accept", "reject", "needs-test", "defer")
$valid = New-Object System.Collections.Generic.List[object]
$invalid = New-Object System.Collections.Generic.List[object]
foreach ($d in @($decisionsIn.decisions | Where-Object { $_ })) {
    $why = $null
    if (-not $d.itemId -or -not $itemIds.ContainsKey($d.itemId)) { $why = "unknown itemId '$($d.itemId)'" }
    elseif ($allowed -notcontains $d.decision) { $why = "decision must be one of: $($allowed -join ', ')" }
    elseif ([string]::IsNullOrWhiteSpace([string]$d.reviewer)) { $why = "reviewer is empty" }
    if ($why) { $invalid.Add([pscustomobject]@{ itemId = $d.itemId; reason = $why }) }
    else {
        $valid.Add([pscustomobject]@{ itemId = $d.itemId; kind = $itemIds[$d.itemId].kind; ref = $itemIds[$d.itemId].ref; decision = $d.decision; reviewer = [string]$d.reviewer; note = [string]$d.note })
    }
}
$decided = @($valid | ForEach-Object { $_.itemId } | Sort-Object -Unique)
$pending = @($manual.items | Where-Object { $decided -notcontains $_.id } | ForEach-Object { $_.id })
$manualStatus = if ($manual.items.Count -eq 0) { "none-needed" } elseif ($pending.Count -eq 0) { "recorded" } elseif ($decided.Count -gt 0) { "partial" } else { "pending" }

$record = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    agent         = [ordered]@{
        state     = $agentState
        writer    = $writer
        tests     = $agentRows.ToArray()
    }
    manual        = [ordered]@{
        status    = $manualStatus
        decisions = $valid.ToArray()
        invalid   = $invalid.ToArray()
        pending   = $pending
    }
}
Write-JsonFile (Join-Path $ReviewDir "review-record.json") $record

Write-StageLog ("agent review: {0}; validated tests reviewed: {1} of {2}" -f $agentState, @($agentRows | Where-Object { $_.result -eq "reviewed" }).Count, $validated.Count)
Write-StageLog ("manual review: {0}; decided {1} of {2} item(s); invalid entries: {3}" -f $manualStatus, $decided.Count, $manual.items.Count, $invalid.Count)

Set-StageStep $status "review" "done" "" (Join-Path $ReviewDir "review-record.json")
Set-RunState $status "phase9-done" "phase9"
Write-JsonFile $StatusFile $status
Write-StageLog "phase 9 done: review record written for run $RunId"
exit 0

<#
.SYNOPSIS
  Stage 10 of UpgradeLens (verdict): sets the verdict and the exit code from the stage outputs, then
  rebuilds the report with the verdict in it.

.DESCRIPTION
  Verdicts (approach section 10):
    blocked                  any blocking reason. Exit 1.
    safe-with-listed-tests   no blocking reason, but something is listed: a manual item with no decision,
                             an open or unverified finding, untraced code, a changed method no test executes,
                             a library change not validated against its release notes, a gap, a missing or
                             stale agent output.
                             Exit 0.
    safe                     no blocking reason, nothing listed, every required stage ran. Exit 0.
  A required stage that did not run can never give safe.

  Blocking reasons:
    - a stage recorded as failed
    - an existing test that passes on master and fails on change_branch (stage 1 test comparison)
    - a probe or a test that could not run (stage 5 blockingErrors)
    - a confirmed behavior change that no person accepted (a probe or test differs on change_branch)
    - a validated new test with no agent review that passed (missing, stale, same author, or failed)
    - a changed code or config file that no stage analysed (category "other" with a code or config extension)

  Writes verdict.json in the run folder, sets the run state to verdict-blocked, verdict-listed or verdict-safe,
  and regenerates reports\report.md and report.html with the verdict.

  Exit codes:
    0  safe or safe-with-listed-tests
    1  blocked
    2  usage or run problem (no run, stage 9 not done)

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 9 is done.

.EXAMPLE
  .\scripts\phase10-verdict.ps1 -RunId 20261126-131717
#>
param(
    [string]$RunId,
    [string]$Config
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
. (Join-Path $PSScriptRoot "lib\analysis.ps1")

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe")
if (-not $RunId) { Write-Host "No run with stage 9 done under $runsRoot. Run phase9-review.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$StatusFile = Join-Path $RunDir "status.json"
$script:StageLog = Join-Path $RunDir "logs\phase10.log"
$status = Read-JsonFile $StatusFile
$impact = Read-JsonFile (Join-Path $ArtifactsDir "impact-findings.json")
$flows = Read-JsonFile (Join-Path $ArtifactsDir "flows.json")
$behavior = Read-JsonFile (Join-Path $ArtifactsDir "behavior-results.json")
$manual = Read-JsonFile (Join-Path $ArtifactsDir "manual-items.json")
$gaps = Read-JsonFile (Join-Path $ArtifactsDir "gaps.json")
$inventory = Read-JsonFile (Join-Path $ArtifactsDir "change-inventory.json")
$review = Read-JsonFile (Join-Path $RunDir "review\review-record.json")
$newTests = Read-JsonFile (Join-Path $ArtifactsDir "new-tests.json")
$testResults = Read-JsonFile (Join-Path $ArtifactsDir "test-results.json")
$libApi = Read-JsonFile (Join-Path $ArtifactsDir "library-api-diff.json")
$depDiff = Read-JsonFile (Join-Path $ArtifactsDir "dependency-diff.json")
$changedMethods = Read-JsonFile (Join-Path $ArtifactsDir "changed-methods.json")
$testsLocation = if ($newTests -and $newTests.location) { $newTests.location } else { "artifacts\agent\tests" }
if (-not $status -or -not $impact -or -not $flows -or -not $behavior -or -not $manual -or -not $review) {
    Write-Host "Run incomplete: $RunDir (needs stages 5 to 9)"; exit 2
}

Write-StageLog "phase 10 verdict for run $RunId"
$blocking = New-Object System.Collections.Generic.List[string]
$listed = New-Object System.Collections.Generic.List[string]

# 1. stage failures
foreach ($s in @($status.steps)) {
    if ($s.state -eq "failed") { $blocking.Add("stage $($s.name) failed: $($s.reason)") }
}

# 2. checks that could not run
foreach ($e in @($behavior.blockingErrors)) { $blocking.Add("check did not run: $e") }

# 3. decisions: which findings a person accepted
$acceptedFindings = New-Object System.Collections.Generic.HashSet[string]
$decisions = @($review.manual.decisions | Where-Object { $_.decision -eq "accept" })
foreach ($d in $decisions) {
    if ($d.kind -eq "finding") { [void]$acceptedFindings.Add([string]$d.ref) }
    if ($d.kind -eq "probe-difference") {
        foreach ($c in @($behavior.checks | Where-Object { $_.source -eq "probe" -and $_.id -eq $d.ref })) {
            foreach ($f in @($impact.findings | Where-Object { $_.ruleId -eq $c.ruleId })) { [void]$acceptedFindings.Add($f.id) }
        }
    }
}
$acceptedRows = @($acceptedFindings | ForEach-Object { [pscustomobject]@{ findingId = $_; decision = "accept" } })

# 4. confirmed behavior changes not accepted, and test failures on change_branch
$statusRows = New-Object System.Collections.Generic.List[object]
foreach ($f in @($impact.findings)) {
    $st = Get-FindingStatus -Finding $f -Checks @($behavior.checks) -Decisions $acceptedRows
    $statusRows.Add([pscustomobject]@{ id = $f.id; status = $st })
    if ($st -eq "confirmed") {
        $diffs = @($behavior.checks | Where-Object { $_.ruleId -eq $f.ruleId -and $_.outcome -ne "same" } | ForEach-Object { "$($_.source) $($_.id) $($_.outcome)" })
        $blocking.Add("confirmed behavior change not accepted: $($f.id) [$($f.ruleId)] $($f.title). Differences: $($diffs -join '; ')")
    }
}
# A probe that differs between the branches is a behavior change even when no finding is linked to it.
# It blocks until a person accepts its probe-difference item.
$acceptedProbes = @($review.manual.decisions | Where-Object { $_.decision -eq "accept" -and $_.kind -eq "probe-difference" } | ForEach-Object { $_.ref })
foreach ($p in @($behavior.probes | Where-Object { $_.outcome -eq "different" })) {
    if ($acceptedProbes -notcontains $p.id) {
        $blocking.Add("behavior change not accepted: probe $($p.id) ($($p.request)) differs on change_branch: $($p.differences -join '; ')")
    }
}
foreach ($t in @($behavior.tests | Where-Object { $_.validated -eq $false -and $_.base.passed -and -not $_.head.passed })) {
    $blocking.Add("test regression on change_branch: $($t.id) fails where master passes: $($t.head.detail)")
}

# 5. validated new tests need a passing agent review
$reviewRows = @($review.agent.tests)
foreach ($t in @($behavior.tests | Where-Object { $_.validated })) {
    $row = @($reviewRows | Where-Object { $_.testId -eq $t.id }) | Select-Object -First 1
    if (-not $row -or $row.result -ne "reviewed") {
        $why = if ($row) { $row.result } else { "missing" }
        $blocking.Add("unreviewed new test: $($t.id) ($why). It is placed, uncommitted, in $testsLocation; do not commit it until a reviewer passes it.")
    }
}

# 6. changed files no stage analysed
foreach ($f in @($inventory.files | Where-Object { $_.category -eq "other" })) {
    if ($f.path -match '\.(java|xml|ya?ml|properties|json|toml|gradle|kts)$') {
        $blocking.Add("unreviewed change: $($f.path) is not analysed by any stage (category other)")
    }
}

# ---- listed items (verdict cannot be a clean pass while any of these exist) ----

$pendingManual = @($review.manual.pending)
if ($pendingManual.Count -gt 0) { $listed.Add("manual review pending: $($pendingManual.Count) item(s) without a valid decision ($($pendingManual -join ', '))") }
foreach ($r in @($review.manual.invalid)) { $listed.Add("invalid decision for $($r.itemId): $($r.reason)") }
foreach ($row in $statusRows) {
    if ($row.status -in @("open", "hypothesis")) {
        $f = @($impact.findings | Where-Object { $_.id -eq $row.id })[0]
        $listed.Add("finding $($row.id) [$($f.ruleId)] not verified by any check ($($row.status))")
    }
}
foreach ($fl in @($flows.flows | Where-Object { @($_.untraced).Count -gt 0 })) {
    $listed.Add("flow $($fl.id) has code no test executes: $(@($fl.untraced) -join ', ')")
}
foreach ($t in @($behavior.tests | Where-Object { -not $_.validated -and $_.base.passed -and $_.head.passed })) {
    $listed.Add("test $($t.id) passes on both branches but is not validated: it does not fail for every injected regression")
}
foreach ($t in @($behavior.tests | Where-Object { -not $_.base.passed })) {
    $listed.Add("test $($t.id) fails on master, so it was not written against master's behavior. Fix it and run again.")
}
foreach ($g in @($gaps.gaps)) { $listed.Add("not run: $($g.item). $($g.reason)") }
foreach ($ag in @($review.agent.tests | Where-Object { $_.result -eq "missing" })) { $listed.Add("agent review missing for $($ag.testId)") }

# Agent proposals count only when their input hash matches the current inputs.
$analyst = Get-AgentProposal -RunDir $RunDir -Step "analyst" -FileName "hypotheses.json"
if ($analyst.state -ne "used") { $listed.Add("analyst proposal $($analyst.state): no hypotheses and no explained change reasons for this run's inputs") }
$testsProposal = Get-AgentProposal -RunDir $RunDir -Step "tests" -FileName "tests-manifest.json"
if ($testsProposal.state -ne "used") {
    $untraced = @($flows.flows | Where-Object { @($_.untraced).Count -gt 0 } | ForEach-Object { $_.id })
    if ($untraced.Count -gt 0) { $listed.Add("no current tests proposal ($($testsProposal.state)) for untraced flow(s): $($untraced -join ', ')") }
}
foreach ($m in @($changedMethods.methods | Where-Object { -not $_.tested -and $_.kind -ne "file" })) {
    $listed.Add("changed $($m.kind) $($m.name) in $($m.file) is not executed by any test on change_branch")
}

# Library changes must be validated against the release notes.
$notes = Get-AgentProposal -RunDir $RunDir -Step "release-notes" -FileName "release-notes.json"
$needNotes = @()
if ($libApi -and $libApi.available) { $needNotes += @($libApi.items | Where-Object { $_.severity -in @("high", "medium") } | ForEach-Object { $_.id }) }
if ($depDiff) { $needNotes += @($depDiff.changed | Where-Object { $_.direct -and $_.major } | ForEach-Object { $_.ga }) }
if ($needNotes.Count -gt 0) {
    if ($notes.state -ne "used") {
        $listed.Add("release-notes validation $($notes.state) for $($needNotes.Count) library change(s): $(@($needNotes | Select-Object -First 8) -join ', ')$(if ($needNotes.Count -gt 8) { ', ...' })")
    } else {
        $covered = @($notes.doc.items | ForEach-Object { [string]$_.ref })
        $missing = @($needNotes | Where-Object { $covered -notcontains $_ })
        if ($missing.Count -gt 0) { $listed.Add("release notes not checked for: $($missing -join ', ')") }
        foreach ($n in @($notes.doc.items | Where-Object { $_.result -eq "contradicted" })) {
            $listed.Add("release notes contradict the expected change for $($n.ref) ($($n.artifact)): $($n.claim)")
        }
    }
}

# Test-by-test comparison of the two builds (stage 1).
if ($testResults) {
    foreach ($t in @($testResults.tests | Where-Object { $_.change -eq "now-failing" })) { $blocking.Add("existing test passes on master and fails on change_branch: $($t.test) ($($t.message))") }
    foreach ($t in @($testResults.tests | Where-Object { $_.change -eq "removed" })) { $listed.Add("test removed on change_branch: $($t.test)") }
    foreach ($t in @($testResults.tests | Where-Object { $_.change -eq "now-skipped" })) { $listed.Add("test skipped on change_branch but run on master: $($t.test)") }
}

if ($newTests -and @($newTests.placed).Count -gt 0) {
    $listed.Add("new tests are placed, uncommitted, in the change worktree $($newTests.location) (branch $($newTests.branch)) for a developer to review, commit and push ($(@($newTests.placed).Count) file(s); patch: $($newTests.patch))")
}

# required stages: stage 1 to 9 must be done (behavior done, not done-with-errors)
$required = @("changed-files", "dependencies-api", "impact", "behavior", "flows", "manual", "output", "review")
$requiredRan = $true
foreach ($name in $required) {
    $st = @($status.steps | Where-Object { $_.name -eq $name }) | Select-Object -Last 1
    if (-not $st -or $st.state -ne "done") { $requiredRan = $false; $listed.Add("required stage $name did not complete cleanly") }
}

$result = Get-Verdict -BlockingReasons $blocking.ToArray() -ListedItems $listed.ToArray() -RequiredStagesRan $requiredRan
$stateName = "verdict-" + ($result.verdict -replace 'safe-with-listed-tests', 'listed' -replace '^blocked$', 'blocked' -replace '^safe$', 'safe')

$acceptedSorted = New-Object System.Collections.Generic.List[string]
foreach ($x in $acceptedFindings) { $acceptedSorted.Add([string]$x) }
$acceptedSorted.Sort()

$verdictDoc = [ordered]@{
    schemaVersion = 1
    runId         = $RunId
    verdict       = $result.verdict
    exitCode      = $result.exitCode
    blocking      = $blocking.ToArray()
    listed        = $listed.ToArray()
    acceptedFindings = $acceptedSorted.ToArray()
    findingStatus = $statusRows.ToArray()
    decidedAt     = (Get-Date).ToString("o")
}
Write-JsonFile (Join-Path $RunDir "verdict.json") $verdictDoc

Write-StageLog ("VERDICT: {0} (exit {1}). Blocking: {2}. Listed: {3}." -f $result.verdict, $result.exitCode, $blocking.Count, $listed.Count)
foreach ($b in $blocking) { Write-StageLog "  blocking: $b" }
foreach ($l in $listed) { Write-StageLog "  listed: $l" }

Set-StageStep $status "verdict" "done" $result.verdict (Join-Path $RunDir "verdict.json")
Set-RunState $status $stateName "phase10"
Write-JsonFile $StatusFile $status

# Rebuild the report with the verdict in it.
& (Join-Path $PSScriptRoot "phase8-output.ps1") -RunId $RunId -ReportOnly -Config $localConfig | Out-Null
Write-StageLog "report updated with the verdict: $(Join-Path $RunDir 'reports\report.md')"
exit $result.exitCode

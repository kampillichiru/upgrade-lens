<#
.SYNOPSIS
  Stage 8 of UpgradeLens (output): places the validated new tests in the change_branch worktree of the run
  (uncommitted, for review), writes findings.json (the single source of truth), and the report
  (report.md and report.html, both generated from the same model).

.DESCRIPTION
  Steps, in order:
    1. tests       validated tests (passed on master, passed unchanged on change_branch, and failed for each
                   injected regression) are copied from artifacts\agent\tests into the change worktree,
                   runs\<id>\worktrees\<head branch>\src\test\... That worktree is on the local branch
                   <head branch> of the run's own clone, tracking origin. The files are NOT staged or committed:
                   a developer reviews them there, commits and pushes (the report gives the commands).
                   artifacts\new-tests.patch holds the same files as a git patch ("git apply" adds them to
                   any checkout of change_branch), and artifacts\new-tests.json records each file, its hash,
                   and why it was added.
    2. collect     every stage's output, into reports\findings.json
    3. report      reports\report.md and reports\report.html. One report covers: run and configuration,
                   workspace, both builds and the test comparison, changed files (three-dot), changed
                   classes and methods with their reason and coverage, dependencies (added, removed,
                   required or not, conflicts, suggestions), library API changes with the code they touch
                   and the release-notes result, impact findings, flows, new tests and why each was added,
                   manual decisions, agent usage, gaps, and evidence.

  -ReportOnly rebuilds steps 2 and 3 only. It also works on an incomplete run (a stage failed): each
  section then says which stage did not produce its input. run-pipeline.ps1 uses this after a failure, and
  stage 10 uses it after the verdict.

  Exit codes:
    0  written
    2  usage or run problem (no run; without -ReportOnly, stage 7 not done)
    3  a test file could not be placed

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 7 is done.

.PARAMETER ReportOnly
  Rebuild the report from the run's files. No test placement.

.PARAMETER Config
  Config file (for the runs folder). Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.EXAMPLE
  .\scripts\phase8-output.ps1 -RunId 20261126-131717
  .\scripts\phase8-output.ps1 -RunId 20261126-131717 -ReportOnly
#>
param(
    [string]$RunId,
    [switch]$ReportOnly,
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
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe") -AnyState:$ReportOnly
if (-not $RunId) { Write-Host "No run with stage 7 done under $runsRoot. Run phase7-manual.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$ReportsDir = Join-Path $RunDir "reports"
$ReviewDir = Join-Path $RunDir "review"
$StatusFile = Join-Path $RunDir "status.json"
if (-not (Test-Path -LiteralPath $StatusFile)) { Write-Host "Run not found: $RunDir"; exit 2 }
$script:StageLog = Join-Path $RunDir "logs\phase8.log"

function Read-Artifact([string]$Name) { return (Read-JsonFile (Join-Path $ArtifactsDir $Name)) }
$cfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$status = Read-JsonFile $StatusFile
$envRec = Read-Artifact "environment.json"
$builds = Read-Artifact "build-results.json"
$testResults = Read-Artifact "test-results.json"
$inventory = Read-Artifact "change-inventory.json"
$depDiff = Read-Artifact "dependency-diff.json"
$apiDiff = Read-Artifact "api-diff.json"
$libApi = Read-Artifact "library-api-diff.json"
$impact = Read-Artifact "impact-findings.json"
$changedMethods = Read-Artifact "changed-methods.json"
$flows = Read-Artifact "flows.json"
$behavior = Read-Artifact "behavior-results.json"
$manual = Read-Artifact "manual-items.json"
$gaps = Read-Artifact "gaps.json"
$usage = Read-Artifact "agent\usage.json"
$review = Read-JsonFile (Join-Path $ReviewDir "review-record.json")
$verdict = Read-JsonFile (Join-Path $RunDir "verdict.json")

if (-not $ReportOnly -and (-not $cfg -or -not $envRec -or -not $impact -or -not $flows -or -not $behavior -or -not $manual)) {
    Write-Host "Run incomplete: $RunDir (needs stages 4 to 7). Use -ReportOnly for a partial report."; exit 2
}
if (-not (Test-Path -LiteralPath $ReportsDir)) { New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null }

Write-StageLog "phase 8 output for run $RunId$(if ($ReportOnly) { ' (report only)' })"

function Get-Short([string]$Sha) { if ($Sha -and $Sha.Length -ge 10) { return $Sha.Substring(0, 10) } return [string]$Sha }
$baseName = if ($cfg) { $cfg.baseBranch } else { "base" }
$headName = if ($cfg) { $cfg.headBranch } else { "head" }

# ---------- 1. place the validated tests (uncommitted) ----------

$newTestsPath = Join-Path $ArtifactsDir "new-tests.json"
$patchPath = Join-Path $ArtifactsDir "new-tests.patch"
$newTests = Read-JsonFile $newTestsPath
if (-not $ReportOnly) {
    $headWt = $envRec.branches.head.worktree
    $validated = @($behavior.tests | Where-Object { $_.validated })
    $placed = New-Object System.Collections.Generic.List[object]
    $skipped = New-Object System.Collections.Generic.List[object]
    $patchText = New-Object System.Text.StringBuilder
    try {
        foreach ($t in $validated) {
            $src = Join-Path $ArtifactsDir ("agent\tests\" + ($t.relativePath -replace '/', '\'))
            if (-not (Test-Path -LiteralPath $src)) { throw "validated test file missing: $src" }
            $dst = Join-Path $headWt ($t.relativePath -replace '/', '\')
            $srcHash = Get-FileSha256 $src
            if (Test-Path -LiteralPath $dst) {
                if ((Get-FileSha256 $dst) -ne $srcHash) {
                    $skipped.Add([pscustomobject]@{ testId = $t.id; relativePath = $t.relativePath; reason = "a different file with this path already exists on $headName; rename the test class" })
                    Write-StageLog "tests: $($t.relativePath) not placed: a different file exists at that path on $headName"
                    continue
                }
            } else {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
                Copy-Item -LiteralPath $src -Destination $dst
            }
            [void]$patchText.Append((New-AddedFilePatch -RelativePath $t.relativePath -Lines @(Get-Content -LiteralPath $src -Encoding UTF8)))
            $placed.Add([pscustomobject]@{
                testId       = $t.id
                className    = $t.className
                relativePath = $t.relativePath
                sha256       = $srcHash
                purpose      = $t.purpose
                gap          = $t.gap
                linkedRules  = @($t.linkedRules)
                flowIds      = @($t.flowIds)
            })
        }
    } catch {
        Write-StageLog "test placement failed: $($_.Exception.Message)"
        Set-StageStep $status "output" "failed" "test placement failed: $($_.Exception.Message)"
        Set-RunState $status "failed-tool" "phase8"
        Write-JsonFile $StatusFile $status
        exit 3
    }
    [System.IO.File]::WriteAllText($patchPath, $patchText.ToString(), $script:Utf8NoBom)
    $newTests = [pscustomobject]@{
        schemaVersion = 1
        runId         = $RunId
        committed     = $false
        location      = $headWt
        branch        = $headName
        baseCommit    = $envRec.branches.head.commit
        patch         = "artifacts\new-tests.patch"
        placed        = $placed.ToArray()
        skipped       = $skipped.ToArray()
        commitSteps   = @(
            "cd `"$headWt`"",
            "git status   # the new test files show as untracked",
            "git add $(@($placed | ForEach-Object { $_.relativePath }) -join ' ')",
            "git commit -m `"Add characterization tests from UpgradeLens run $RunId`"",
            "git push     # the branch tracks origin/$headName"
        )
        note          = "Placed for review only: the automation does not stage, commit or push. A developer reviews the files in the change worktree and commits them (commitSteps), or applies artifacts\new-tests.patch to another checkout."
    }
    Write-JsonFile $newTestsPath $newTests
    Write-StageLog "tests: $($placed.Count) validated test file(s) placed, uncommitted, in $headWt; $($skipped.Count) skipped; patch $patchPath"
}

# ---------- 2. collect ----------

$flowById = @{}
foreach ($fl in @($flows.flows | Where-Object { $_ })) { $flowById[$fl.id] = $fl }
$statusById = @{}
foreach ($fs in @($flows.findingStatus | Where-Object { $_ })) { $statusById[$fs.id] = $fs.status }
if ($verdict) { foreach ($fs in @($verdict.findingStatus | Where-Object { $_ })) { $statusById[$fs.id] = $fs.status } }
$decisions = @()
if ($review -and $review.manual -and $review.manual.decisions) { $decisions = @($review.manual.decisions | Where-Object { $_ }) }

$findingRows = @($impact.findings | Where-Object { $_ } | ForEach-Object {
    $f = $_
    $linked = @($behavior.checks | Where-Object { $_.ruleId -eq $f.ruleId })
    $evidenceText = if ($linked.Count -eq 0) { "no check" } else {
        (@($linked | ForEach-Object { "$($_.source) $($_.id): $($_.outcome)" }) | Select-Object -Unique) -join "; "
    }
    [pscustomobject]@{
        id            = $f.id
        ruleId        = $f.ruleId
        title         = $f.title
        severity      = $f.severity
        scope         = $f.scope
        status        = $statusById[$f.id]
        file          = $f.file
        line          = $f.line
        evidence      = $f.evidence
        checkResults  = $evidenceText
        impact        = $f.impact
        check         = $f.check
        affectedFlows = @($f.affectedFlows)
        source        = $f.source
    }
})

$analystProposal = Get-AgentProposal -RunDir $RunDir -Step "analyst" -FileName "hypotheses.json"
$analystState = $analystProposal.state
$notes = Get-AgentProposal -RunDir $RunDir -Step "release-notes" -FileName "release-notes.json"
$notesByRef = @{}
if ($notes.state -eq "used") { foreach ($n in @($notes.doc.items | Where-Object { $_ })) { $notesByRef[[string]$n.ref] = $n } }
$agentStates = [ordered]@{
    analyst      = $analystState
    testWriter   = if ($behavior) { $behavior.testState } else { "not-run" }
    reviewer     = if ($review -and $review.agent) { $review.agent.state } else { "not-run" }
    releaseNotes = $notes.state
}

$stageRows = @($status.steps | Where-Object { $_ } | ForEach-Object { [pscustomobject]@{ name = $_.name; state = $_.state; reason = $_.reason } })
$failedStages = @($stageRows | Where-Object { $_.state -eq "failed" })

$doc = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    state         = $status.state
    complete      = ($failedStages.Count -eq 0 -and $null -ne $manual)
    base          = [ordered]@{ branch = $baseName; commit = $envRec.branches.base.commit; mergeBase = $envRec.branches.mergeBase }
    head          = [ordered]@{ branch = $headName; commit = $envRec.branches.head.commit }
    rulesSha256   = $impact.rulesSha256
    verdict       = if ($verdict) { [ordered]@{ verdict = $verdict.verdict; exitCode = $verdict.exitCode; blocking = @($verdict.blocking); listed = @($verdict.listed) } } else { $null }
    stages        = $stageRows
    summary       = [ordered]@{
        findings    = $findingRows.Count
        byStatus    = [ordered]@{
            open       = @($findingRows | Where-Object { $_.status -eq "open" }).Count
            hypothesis = @($findingRows | Where-Object { $_.status -eq "hypothesis" }).Count
            verified   = @($findingRows | Where-Object { $_.status -eq "verified" }).Count
            confirmed  = @($findingRows | Where-Object { $_.status -eq "confirmed" }).Count
            accepted   = @($findingRows | Where-Object { $_.status -eq "accepted" }).Count
        }
        flows       = $flows.summary
        probes      = $behavior.probeSummary
        tests       = [ordered]@{ proposed = @($behavior.tests).Count; validated = @($behavior.tests | Where-Object { $_.validated }).Count; placed = @($newTests.placed).Count }
        manualItems = $manual.summary.items
        gaps        = @($gaps.gaps).Count
    }
    builds        = $builds
    testComparison = $testResults
    changes       = [ordered]@{
        files          = if ($inventory) { $inventory.summary } else { $null }
        diff           = if ($inventory) { $inventory.diff } else { $null }
        dependencies   = if ($depDiff) { $depDiff.summary } else { $null }
        api            = if ($apiDiff) { $apiDiff.summary } else { $null }
        library        = if ($libApi -and $libApi.available) { $libApi.summary } else { $null }
    }
    changedMethods = @($changedMethods.methods | Where-Object { $_ })
    dependencies  = if ($depDiff) { [ordered]@{ assessment = @($depDiff.assessment); conflicts = @($depDiff.conflicts); suggestions = @($depDiff.suggestions); analyze = [ordered]@{ base = $depDiff.base.analyze; head = $depDiff.head.analyze } } } else { $null }
    library       = if ($libApi) { [ordered]@{ available = $libApi.available; reason = $libApi.reason; items = @($libApi.items | Where-Object { $_ } | ForEach-Object { [pscustomobject]@{ id = $_.id; class = $_.class; state = $_.state; severity = $_.severity; summary = $_.summary; artifact = $_.artifact; movedTo = @($_.movedTo); releaseNotes = $notesByRef[$_.id] } }) } } else { $null }
    findings      = $findingRows
    flows         = @($flows.flows | Where-Object { $_ } | ForEach-Object { [pscustomobject]@{ id = $_.id; kind = $_.kind; scope = $_.scope; reason = $_.reason; probes = @($_.probes); tests = @($_.tests); untraced = @($_.untraced) } })
    probes        = @($behavior.probes | Where-Object { $_ } | ForEach-Object { [pscustomobject]@{ id = $_.id; flowId = $_.flowId; request = $_.request; outcome = $_.outcome; differences = @($_.differences) } })
    servers       = $behavior.servers
    dumps         = $behavior.dumps
    tests         = @($behavior.tests | Where-Object { $_ })
    newTests      = $newTests
    manualItems   = @($manual.items | Where-Object { $_ })
    decisions     = $decisions
    gaps          = @($gaps.gaps | Where-Object { $_ })
    agents        = [ordered]@{ states = $agentStates; usage = if ($usage) { $usage.agents } else { @() } }
    evidence      = [ordered]@{
        environment     = "artifacts\environment.json"
        builds          = "artifacts\build-results.json"
        testResults     = "artifacts\test-results.json"
        changeInventory = "artifacts\change-inventory.json"
        changedMethods  = "artifacts\changed-methods.json"
        dependencyDiff  = "artifacts\dependency-diff.json"
        apiDiff         = "artifacts\api-diff.json"
        libraryApiDiff  = "artifacts\library-api-diff.json"
        impactFindings  = "artifacts\impact-findings.json"
        reach           = "artifacts\reach.json"
        behaviorResults = "artifacts\behavior-results.json"
        flows           = "artifacts\flows.json"
        manualItems     = "artifacts\manual-items.json"
        gaps            = "artifacts\gaps.json"
        newTests        = "artifacts\new-tests.json"
        reviewRecord    = "review\review-record.json"
        logs            = "logs\"
    }
}
Write-JsonFile (Join-Path $ReportsDir "findings.json") $doc -Depth 14

# ---------- 3. report (markdown and HTML, from the same model) ----------

function New-Table($Headers, $Rows) { return [pscustomobject]@{ headers = @($Headers); rows = @($Rows) } }
function Format-Cell($v) { return ([string]$v -replace '\r?\n', ' ').Trim() }
function Get-NotRun([string]$What) { return "Not available: $What did not complete in this run." }

$sections = New-Object System.Collections.Generic.List[object]
function Add-Section([string]$Title, [string[]]$Text = @(), $Table = $null, $Table2 = $null, [string[]]$Text2 = @()) {
    $sections.Add([pscustomobject]@{ title = $Title; text = @($Text | Where-Object { $_ -ne $null }); table = $Table; text2 = @($Text2 | Where-Object { $_ -ne $null }); table2 = $Table2 })
}

# 1. Verdict
$verdictLine = if ($verdict) { "**$($verdict.verdict)** (exit $($verdict.exitCode))" }
               elseif ($failedStages.Count -gt 0) { "**not reached**: the run stopped at a failed stage" }
               else { "pending: stage 10 has not run" }
$verdictText = @("Verdict: $verdictLine")
foreach ($fs in $failedStages) { $verdictText += "- Failed stage: $($fs.name): $($fs.reason)" }
if ($verdict) {
    $verdictText += @($verdict.blocking | Where-Object { $_ } | ForEach-Object { "- Blocking: $_" })
    $verdictText += @($verdict.listed | Where-Object { $_ } | ForEach-Object { "- Listed: $_" })
}
Add-Section "1. Verdict" $verdictText

# 2. Run, configuration and workspace (requirements 1 and 2)
$runRows = @()
if ($envRec) {
    $runRows = @(
        , @("Config", "$($envRec.configPath) (sha256 $(Get-Short $envRec.configSha256))")
        , @("Repository", $envRec.repo)
        , @("Base branch", "$baseName at $(Get-Short $envRec.branches.base.commit)")
        , @("Head branch", "$headName at $(Get-Short $envRec.branches.head.commit)")
        , @("Merge base", "$(Get-Short $envRec.branches.mergeBase) (diff: $baseName...$headName, three-dot)")
        , @("Base worktree", "$($envRec.branches.base.worktree) (JDK $($envRec.branches.base.jdk))")
        , @("Head worktree", "$($envRec.branches.head.worktree) (JDK $($envRec.branches.head.jdk))")
        , @("Maven settings", $(if ($envRec.maven.settingsFile) { $envRec.maven.settingsFile } else { "(Maven default)" }))
        , @("Maven goals", $envRec.maven.goals)
        , @("Rules", "sha256 $(Get-Short $impact.rulesSha256)")
        , @("Tools", "$($envRec.git); $($envRec.branches.head.maven.maven); PowerShell $($envRec.powershell)")
    )
}
Add-Section "2. Run, configuration and workspace" @("Each branch is checked out in its own detached worktree inside the run folder; the repository's own checkout is not switched.") (New-Table @("Item", "Value") $runRows)

# 3. Builds and tests (requirement 3)
$buildRows = @()
if ($builds) {
    foreach ($side in @("base", "head")) {
        $b = $builds.sides.$side
        if (-not $b) { continue }
        $buildRows += , @($b.branch, $b.jdk, $(if ($b.exitCode -eq 0) { "SUCCESS" } else { "FAILED (exit $($b.exitCode))" }), "$($b.tests.passed) passed, $($b.tests.failed) failed, $($b.tests.skipped) skipped", "$($b.integrationTests.mode) ($(@($b.integrationTests.classes).Count) class(es))", $b.log)
    }
}
$cmpRows = @()
if ($testResults) { $cmpRows = @($testResults.tests | Where-Object { $_ -and $_.change -ne "same" } | ForEach-Object { , @($_.test, $(if ($_.base) { $_.base } else { "-" }), $(if ($_.head) { $_.head } else { "-" }), $_.change, $_.message) }) }
$buildText = if ($builds) { @("Both branches were built in parallel with ``mvn $($builds.goals)``, each with its own JDK, worktree and target folder$(if ($builds.settingsFile) { ', and the settings file ' + $builds.settingsFile }).", $(if ($testResults) { "Test comparison: $($testResults.summary.tests) test(s); $($testResults.summary.same) same, $($testResults.summary.nowFailing) now failing, $($testResults.summary.removed) removed, $($testResults.summary.new) new, $($testResults.summary.nowSkipped) now skipped." })) } else { @(Get-NotRun "stage 1 (build)") }
Add-Section "3. Builds and tests" $buildText (New-Table @("Branch", "JDK", "Result", "Tests", "Integration tests", "Log") $buildRows) (New-Table @("Test", "Master", "Change branch", "Change", "Message") $cmpRows) @("Tests whose result differs between the branches:")

# 4. Changed files (requirement 4)
$fileRows = @()
if ($inventory) {
    $fileRows = @($inventory.files | Where-Object { $_ } | ForEach-Object {
        , @($_.path, $_.change, $_.category, "+$($_.additions) / -$($_.deletions)", $(if (@($_.changedMethods).Count -gt 0) { @($_.changedMethods) -join ", " } else { "-" }), $(if (@($_.commits).Count -gt 0) { @($_.commits | ForEach-Object { "$($_.commit) $($_.subject)" }) -join "; " } else { "-" }))
    })
}
$fileText = if ($inventory) { @("Diff: ``$($inventory.diff.expression)`` (three-dot: merge base $(Get-Short $inventory.diff.left) to $(Get-Short $inventory.diff.right)). $($inventory.summary.files) file(s), $($inventory.summary.commits) commit(s). By category: " + (@($inventory.summary.byCategory.PSObject.Properties | ForEach-Object { "$($_.Name) $($_.Value)" }) -join ", ") + ".") + @($inventory.findings | ForEach-Object { "Finding: $($_.type) in ``$($_.file)``: $($_.detail)" }) } else { @(Get-NotRun "stage 2 (changed files)") }
Add-Section "4. Changed files" $fileText (New-Table @("File", "Change", "Category", "Lines", "Changed methods", "Commits") $fileRows)

# 5. Changed classes and methods (requirement 6)
$methodRows = @()
if ($changedMethods) {
    $methodRows = @($changedMethods.methods | Where-Object { $_ -and $_.kind -ne "file" } | ForEach-Object {
        $why = if ($_.analystReason) { "$($_.analystReason) [analyst]$(if ($_.consequence) { ' Consequence: ' + $_.consequence }) | $($_.reason)" } else { $_.reason }
        , @($_.class, "$($_.kind) $($_.name)", (@($_.changeKinds) -join ", "), $why,
            "master $(if ($null -ne $_.coverage.base) { $_.coverage.base } else { '-' }) / change_branch $(if ($null -ne $_.coverage.head) { $_.coverage.head } else { '-' })",
            $(if ($_.tested) { "yes" } else { "NO" }), (@($_.flows) -join ", "))
    })
}
$methodText = if ($changedMethods) { @("Each diff hunk is mapped to the method or type it changes. Coverage is JaCoCo covered instructions from the unit and integration tests of each build ($($changedMethods.coverage.integrationTests) for integration tests). $($changedMethods.summary.untested) changed symbol(s) are not executed by any test on change_branch.") } else { @(Get-NotRun "stage 4 (impact)") }
Add-Section "5. Changed classes and methods" $methodText (New-Table @("Class", "Symbol", "What changed", "Why", "Coverage (instructions)", "Tested", "Flows") $methodRows)

# 6. Dependencies (requirement 5)
$depText = @(); $assessRows = @(); $conflictRows = @(); $suggRows = @()
if ($depDiff) {
    $sm = $depDiff.summary
    $depText += "Resolved artifacts: $($depDiff.base.artifacts) on $baseName, $($depDiff.head.artifacts) on $headName. Added $($sm.added) ($($sm.directAdded) direct), removed $($sm.removed) ($($sm.directRemoved) direct), version changed $($sm.changed) ($($sm.majorChanged) major, $($sm.directChanged) direct)."
    $depText += "Version conflicts on ${headName}: $($sm.conflicts) ($($sm.majorConflicts) major)$(if (-not $depDiff.verboseTree) { ' - NOT CHECKED: no verbose tree' }). Duplicates omitted by Maven: $($depDiff.duplicates.head)."
    $newUndeclared = @($depDiff.head.analyze.undeclared | Where-Object { @($depDiff.base.analyze.undeclared) -notcontains $_ })
    $depText += "dependency:analyze on ${headName}: $(@($depDiff.head.analyze.unused).Count) declared but unused, $(@($depDiff.head.analyze.undeclared).Count) used but undeclared ($($newUndeclared.Count) new: $(@($newUndeclared | Select-Object -First 6) -join ', '))."
    $assessRows = @($depDiff.assessment | Where-Object { $_ } | ForEach-Object { , @($_.change, $_.ga, $_.version, $_.assessment, $_.reason, $_.impact) })
    $conflictRows = @($depDiff.conflicts | Where-Object { $_ } | ForEach-Object { , @($_.ga, $_.requested, $_.resolved, $(if ($_.major) { "major" } else { "minor" }), (@($_.requiredBy) -join ", "), $(if ($_.newOnHead) { "yes" } else { "no" })) })
    $suggRows = @($depDiff.suggestions | Where-Object { $_ } | ForEach-Object { , @($_.command, $_.why) })
} else { $depText = @(Get-NotRun "stage 3 (dependencies)") }
Add-Section "6. Dependencies" $depText (New-Table @("Change", "Artifact", "Version", "Assessment", "Why", "Potential impact") $assessRows) (New-Table @("Artifact", "Requested", "Resolved", "Difference", "Requested by", "New on change_branch") $conflictRows) @("Version conflicts (mvn dependency:tree -Dverbose):")
Add-Section "6a. Suggested deeper analysis" @() (New-Table @("Command", "Why") $suggRows)

# 7. Library API changes (requirement 7)
$libText = @(); $libRows = @(); $libDetail = @()
if ($libApi -and $libApi.available) {
    $ls = $libApi.summary
    $libText += "Library classes the code refers to: $($ls.referencedClasses). Same: $($ls.same), changed: $($ls.changed), removed: $($ls.removed), moved: $($ls.moved), added: $($ls.added). Severity: high $($ls.high), medium $($ls.medium), low $($ls.low), info $($ls.info)."
    $libText += "Release-notes validation: $($notes.state)$(if ($notes.state -eq 'used') { ' (' + @($notes.doc.items).Count + ' item(s))' })."
    $libRows = @($libApi.items | Where-Object { $_ -and $_.severity -ne "info" -or $_.state -ne "added" } | ForEach-Object {
        $where = @(@($_.usage.base) + @($_.usage.head) | Where-Object { $_ -and $_.source } | ForEach-Object { "$($_.source)$(if ($_.line) { ':' + $_.line })$(if ($_.method) { ' ' + $_.method })" } | Select-Object -Unique | Select-Object -First 4) -join "; "
        $rn = $notesByRef[$_.id]
        , @($_.id, $_.severity, $_.class, $(if ($_.movedTo.Count -gt 0) { "moved to $(@($_.movedTo) -join ', ')" } else { $_.state }), "$($_.artifact.base) -> $($_.artifact.head)", $_.summary, $where, $(if ($rn) { "$($rn.result): $($rn.claim) ($($rn.source.url))" } else { "not validated" }))
    })
    foreach ($it in @($libApi.items | Where-Object { $_.severity -eq "high" })) {
        foreach ($u in @($it.removedUsed)) { $libDetail += , @($it.id, $u.declaration, "$($u.source):$($u.line)", $u.method) }
    }
} else { $libText = @("Not available: $(if ($libApi) { $libApi.reason } else { 'stage 3 did not complete' }).") }
Add-Section "7. Library API changes" $libText (New-Table @("ID", "Severity", "Library class", "Change", "Artifact (master -> change_branch)", "Summary", "Used at", "Release notes") $libRows) (New-Table @("ID", "Removed member used on master", "Where (master)", "Method") $libDetail) @("Removed or changed members that master's code called:")

# 8. Impact findings
Add-Section "8. Impact findings" @("Status: verified (a check matched master), confirmed (a check differs), open (no check ran), hypothesis (from the analyst, not yet checked), accepted (a person accepted it).") `
    (New-Table @("ID", "Severity", "Change", "Scope", "What could happen", "Evidence", "Status", "Action") @(
        $findingRows | ForEach-Object {
            , @($_.id, $_.severity, "$($_.title) ($($_.file)$(if ($_.line) { ':' + $_.line }))", $_.scope, $_.impact, $_.checkResults + " | " + $_.evidence, $_.status, $_.check)
        }
    ))

# 8a. Impact in plain words (analyst: one explanation per rule behind a high or medium finding, and each
# hypothesis), with the status the checks gave it.
$plainRows = @()
if ($analystState -eq "used") {
    $statusByRule = @{}
    foreach ($fr in @($findingRows)) {
        $k = [string]$fr.ruleId
        if (-not $statusByRule.ContainsKey($k)) { $statusByRule[$k] = New-Object System.Collections.Generic.List[string] }
        $statusByRule[$k].Add("$($fr.id) $($fr.status)")
    }
    $plainRows += @($analystProposal.doc.explanations | Where-Object { $_ } | ForEach-Object {
        $st = if ($statusByRule.ContainsKey([string]$_.ruleId)) { @($statusByRule[[string]$_.ruleId]) -join ", " } else { "-" }
        , @([string]$_.ruleId, [string]$_.plain, [string]$_.example, [string]$_.productionRisk, $st)
    })
    $plainRows += @($analystProposal.doc.hypotheses | Where-Object { $_ } | ForEach-Object {
        $st = if ($statusByRule.ContainsKey([string]$_.id)) { @($statusByRule[[string]$_.id]) -join ", " } else { "-" }
        , @("$($_.id) (hypothesis)", [string]$_.plain, [string]$_.example, [string]$_.productionRisk, $st)
    })
}
$plainText = if ($analystState -eq "used") { @("Each potential impact in simple words, with an example and how it could break production. Status comes from the probes and tests of this run.") } else { @("Not available: the analyst proposal is $analystState for this run's inputs.") }
Add-Section "8a. Impact explained" $plainText (New-Table @("Rule or hypothesis", "In simple words", "Example", "How it can break production", "Findings and status") $plainRows)

# 9. Flows
Add-Section "9. Flows to test" @("Time saved is not estimated: there is no regression duration data for this application.") `
    (New-Table @("Flow", "Scope", "Why", "Probes", "Tests") @(
        $doc.flows | ForEach-Object { , @($_.id, $_.scope, $_.reason, (@($_.probes) -join ", "), (@($_.tests) -join ", ")) }
    ))

# 10. New tests (requirement 6)
$testRows = @($doc.tests | ForEach-Object {
    $why = @($_.purpose, $(if ($_.gap) { "Gap: $($_.gap)" })) | Where-Object { $_ }
    , @($_.id, $_.className, ($why -join " "), ("rules: " + (@($_.linkedRules) -join ", ") + "; flows: " + (@($_.flowIds) -join ", ")),
        ("master " + $(if ($_.base.passed) { "pass" } else { "FAIL" }) + ", change_branch " + $(if ($_.head.passed) { "pass" } else { "FAIL" })),
        (@($_.injections | ForEach-Object { "$(if ($_.detected) { 'detected' } elseif ($_.applied) { 'MISSED' } else { 'not applied' }): $($_.note)" }) -join "; "),
        $(if ($_.validated) { "validated" } else { "not validated" }))
})
$newTestText = if ($newTests -and @($newTests.placed).Count -gt 0) {
    @("Each test below was written in the run folder, run on both branches, and validated. It is now copied, uncommitted, into the change worktree ``$($newTests.location)`` (branch ``$($newTests.branch)``, tracking origin). The automation does not stage, commit or push.",
      "To keep the tests, a developer reviews them there, then commits and pushes:") +
    @($newTests.commitSteps | Where-Object { $_ } | ForEach-Object { "- ``$_``" }) +
    @("Or apply ``$($newTests.patch)`` to another checkout of $($newTests.branch) with ``git apply``.") +
    @($newTests.skipped | Where-Object { $_ } | ForEach-Object { "Not placed: $($_.relativePath): $($_.reason)" })
} elseif ($behavior -and $behavior.testState -ne "not-proposed") { @("No test is validated, so none is placed.") }
else { @("No tests were proposed in this run (test writer: $($agentStates.testWriter)).") }
Add-Section "10. New tests" $newTestText (New-Table @("ID", "Class", "Why it was added", "Covers", "Result", "Injected regressions", "Status") $testRows)

# 11. Manual decisions
Add-Section "11. Manual decisions" @("Expected master results come from the master probes and the master build where one exists.") `
    (New-Table @("ID", "Kind", "Question", "Expected on master", "Owner", "Decision") @(
        $doc.manualItems | ForEach-Object {
            $item = $_
            $hit = @($decisions | Where-Object { $_.itemId -eq $item.id })
            $decText = if ($hit.Count -gt 0) { ($hit | ForEach-Object { "$($_.decision) by $($_.reviewer)" }) -join "; " } else { "pending" }
            , @($item.id, $item.kind, $item.question, $item.expectedMaster, $item.owner, $decText)
        }
    ))

# 12. Agents
$agentRows = @(
    , @("analyst (hypotheses, change reasons)", $agentStates.analyst)
    , @("test writer", $agentStates.testWriter)
    , @("test reviewer", $agentStates.reviewer)
    , @("release notes", $agentStates.releaseNotes)
)
Add-Section "12. Agent usage" @("Agent output counts only when its input hash matches the current inputs. Calls used: $(if ($usage) { "$($usage.totalCalls) of $($usage.budget)" } else { '0' }).") (New-Table @("Agent", "State") $agentRows)

# 13. Gaps
$gapRows = @($doc.gaps | ForEach-Object { , @($_.stage, $_.item, $_.reason) })
Add-Section "13. Not run and limits" @("A gap listed here means the verdict cannot be a clean pass.") (New-Table @("Stage", "Item", "Reason") $gapRows)

# 14. Evidence
Add-Section "14. Evidence" @(
    "Run folder: ``$RunDir``. Machine-readable results: ``reports\findings.json``.",
    "Stages: " + (@($stageRows | ForEach-Object { "$($_.name) $($_.state)" }) -join ", ") + ".",
    "Artifacts: " + (@($doc.evidence.PSObject.Properties | ForEach-Object { "``$($_.Value)``" }) -join ", ") + "."
)

# Markdown
function Add-MdTable($md, $table) {
    $md.Add("| " + ($table.headers -join " | ") + " |")
    $md.Add("|" + (@($table.headers | ForEach-Object { "---" }) -join "|") + "|")
    foreach ($row in $table.rows) {
        $cells = @($row | ForEach-Object { (Format-Cell $_) -replace '\|', '\|' })
        $md.Add("| " + ($cells -join " | ") + " |")
    }
    if ($table.rows.Count -eq 0) { $md.Add("| None. |" + ((@($table.headers | Select-Object -Skip 1) | ForEach-Object { " |" }) -join "")) }
    $md.Add("")
}
$md = New-Object System.Collections.Generic.List[string]
$md.Add("# Upgrade impact check: run $RunId")
$md.Add("")
$md.Add("``$baseName`` -> ``$headName``. Generated by UpgradeLens $ToolVersion.")
$md.Add("")
foreach ($s in $sections) {
    $md.Add("## $($s.title)")
    $md.Add("")
    for ($i = 0; $i -lt $s.text.Count; $i++) {
        $md.Add($s.text[$i])
        $nextIsItem = ($i + 1 -lt $s.text.Count) -and ($s.text[$i + 1] -like "- *")
        if (-not ($s.text[$i] -like "- *" -and $nextIsItem)) { $md.Add("") }
    }
    if ($s.table) { Add-MdTable $md $s.table }
    foreach ($t in $s.text2) { $md.Add($t); $md.Add("") }
    if ($s.table2) { Add-MdTable $md $s.table2 }
}
[System.IO.File]::WriteAllText((Join-Path $ReportsDir "report.md"), ($md -join "`r`n") + "`r`n", $script:Utf8NoBom)

# HTML (one file, inline styles, no external resources)
function ConvertTo-HtmlText($s) { return [System.Net.WebUtility]::HtmlEncode([string]$s) }
function Add-HtmlTable($h, $table) {
    $h.Add("<div class='scroll'><table><thead><tr>" + (@($table.headers | ForEach-Object { "<th>$(ConvertTo-HtmlText $_)</th>" }) -join "") + "</tr></thead><tbody>")
    foreach ($row in $table.rows) {
        $h.Add("<tr>" + (@($row | ForEach-Object { "<td>$(ConvertTo-HtmlText (Format-Cell $_))</td>" }) -join "") + "</tr>")
    }
    if ($table.rows.Count -eq 0) { $h.Add("<tr><td colspan='$($table.headers.Count)'>None.</td></tr>") }
    $h.Add("</tbody></table></div>")
}
$h = New-Object System.Collections.Generic.List[string]
$h.Add('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
$h.Add("<title>Upgrade impact check $(ConvertTo-HtmlText $RunId)</title>")
$h.Add('<style>:root{--bg:#FFFFFF;--fg:#2C2C2A;--line:#D3D1C7;--head:#F1EFE8}@media (prefers-color-scheme: dark){:root{--bg:#1F1E1B;--fg:#EDEBE4;--line:#5F5E5A;--head:#2C2C2A}}body{font-family:Arial,Helvetica,sans-serif;margin:24px;color:var(--fg);background:var(--bg);line-height:1.45}h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid var(--line);padding-bottom:4px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 16px}th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--head)}code{font-size:12px;background:var(--head);padding:1px 4px;border-radius:3px}@media (max-width:700px){body{margin:16px}}</style></head><body>')
$h.Add("<h1>Upgrade impact check: run $(ConvertTo-HtmlText $RunId)</h1>")
$h.Add("<p>$(ConvertTo-HtmlText $baseName) &rarr; $(ConvertTo-HtmlText $headName). Generated by UpgradeLens $ToolVersion.</p>")
foreach ($s in $sections) {
    $h.Add("<h2>$(ConvertTo-HtmlText $s.title)</h2>")
    # Lines in order; consecutive "- " lines become one list.
    $inList = $false
    foreach ($t in $s.text) {
        $clean = ConvertTo-HtmlText ($t -replace '``', '' -replace '\*\*', '')
        if ($t -like "- *") {
            if (-not $inList) { $h.Add("<ul>"); $inList = $true }
            $h.Add("<li>$($clean.Substring(2))</li>")
        } else {
            if ($inList) { $h.Add("</ul>"); $inList = $false }
            $h.Add("<p>$clean</p>")
        }
    }
    if ($inList) { $h.Add("</ul>") }
    if ($s.table) { Add-HtmlTable $h $s.table }
    foreach ($t in $s.text2) { $h.Add("<p>$(ConvertTo-HtmlText ($t -replace '``', ''))</p>") }
    if ($s.table2) { Add-HtmlTable $h $s.table2 }
}
$h.Add("</body></html>")
[System.IO.File]::WriteAllText((Join-Path $ReportsDir "report.html"), ($h -join "`n") + "`n", $script:Utf8NoBom)

Write-StageLog "report: $(Join-Path $ReportsDir 'report.md')"
Write-StageLog "report: $(Join-Path $ReportsDir 'report.html')"
Write-StageLog "findings: $(Join-Path $ReportsDir 'findings.json')"

if (-not $ReportOnly) {
    Set-StageStep $status "output" "done" "" (Join-Path $ReportsDir "report.md")
    Set-RunState $status "phase8-done" "phase8"
    Write-JsonFile $StatusFile $status
}
Write-StageLog "phase 8 done for run $RunId"
exit 0

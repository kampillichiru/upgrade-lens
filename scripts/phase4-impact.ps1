<#
.SYNOPSIS
  Stage 4 of UpgradeLens (impact evaluation): applies the rule set to the changes, maps every flow
  (HTTP endpoint, scheduled job, listener) to the code it reaches, marks reached code that no test
  executes as untraced, and lists every changed method with its reason and its coverage on both sides.

.DESCRIPTION
  Inputs (from earlier stages, in the run folder): change-inventory.json, dependency-diff.json,
  api-diff.json, library-api-diff.json, build-results.json, the three-dot git diff (merge base to head),
  the source of both sides, and the coverage reports of both builds (target\site\jacoco\jacoco.xml, which
  includes the integration tests when stage 1 ran them). Optional: the analyst's proposal, used only when
  its input hash matches the current inputs.

  Steps, in order:
    1. rules           copy the rules folder (config rulesDir) into the run (config\rules\) and hash them
    2. evidence        the patch, the head source and the merge-base source of each changed file
    3. findings        apply every rule; one finding per diff or inventory match, dependency, analysis and
                       library findings grouped per rule; each finding gets a scope (global, symbol, test, none)
    4. hypotheses      merge the analyst's hypotheses if they match the inputs (status "hypothesis")
    5. flows           endpoints, jobs and listeners of the head source; the code each one reaches
    6. coverage        JaCoCo covered instructions per reached symbol on head; untraced = not covered
    7. affect          which flows each finding touches (global: all; symbol: flows that reach it)
    8. changed methods each changed method or type of the main code: what changed (signature, annotation,
                       body...), the commits that changed it, the rules that matched it, its coverage on
                       master and on change_branch, and the reason for the change (derived from the commits
                       and the rules; the analyst's explanation when its proposal is current)
    9. write           artifacts\impact-findings.json, reach.json and changed-methods.json (no timestamps, so
                       the same inputs give the same bytes)

  Exit codes:
    0  findings and reach written
    2  usage or run problem (no run, stage 3 not done)
    3  a required input is missing (for example the head coverage report)

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 3 is done.

.PARAMETER Config
  Config file (for the runs folder and the rules folder). Defaults to $env:UPGRADELENS_CONFIG, then
  config\upgrade-lens.json.

.EXAMPLE
  .\scripts\phase4-impact.ps1 -RunId 20261126-131717
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
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase3-done", "phase4-done", "phase5-done", "phase6-done", "phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe")
if (-not $RunId) { Write-Host "No run with stage 3 done under $runsRoot. Run phase3-deps.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$StatusFile = Join-Path $RunDir "status.json"
$script:StageLog = Join-Path $RunDir "logs\phase4.log"
$cfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$envRec = Read-JsonFile (Join-Path $ArtifactsDir "environment.json")
$status = Read-JsonFile $StatusFile
$inventory = Read-JsonFile (Join-Path $ArtifactsDir "change-inventory.json")
$depDiff = Read-JsonFile (Join-Path $ArtifactsDir "dependency-diff.json")
$apiDiff = Read-JsonFile (Join-Path $ArtifactsDir "api-diff.json")
$libApi = Read-JsonFile (Join-Path $ArtifactsDir "library-api-diff.json")
$builds = Read-JsonFile (Join-Path $ArtifactsDir "build-results.json")
if (-not $cfg -or -not $envRec -or -not $status -or -not $inventory -or -not $depDiff -or -not $apiDiff) {
    Write-Host "Run incomplete: $RunDir (needs stages 2 and 3)"; exit 2
}

function Stop-Stage([int]$Code, [string]$Reason) {
    Write-StageLog $Reason
    Set-StageStep $status "impact" "failed" $Reason
    Set-RunState $status "failed-tool" "phase4"
    Write-JsonFile $StatusFile $status
    exit $Code
}

Write-StageLog "phase 4 impact evaluation for run $RunId"
$baseWt = $envRec.branches.base.worktree
$headWt = $envRec.branches.head.worktree
$headCommit = $envRec.branches.head.commit
$mergeBase = if ($envRec.branches.mergeBase) { $envRec.branches.mergeBase } else { $inventory.base.mergeBase }
if (-not $mergeBase) { Stop-Stage 2 "No merge base in the environment record or the change inventory. Run stages 0 and 2 again." }

# ---------- 1. rules ----------

$rulesSrc = Resolve-ProjectPath -Path ([string](Get-ConfigValue $liveCfg "rulesDir" "config\rules")) -RootDir $RootDir
$rulesDst = Join-Path $RunDir "config\rules"
New-Item -ItemType Directory -Force -Path $rulesDst | Out-Null
$ruleFiles = @(Get-ChildItem -LiteralPath $rulesSrc -Filter *.json -File -ErrorAction SilentlyContinue | Sort-Object Name)
if ($ruleFiles.Count -eq 0) { Stop-Stage 2 "No rule files in $rulesSrc" }
Get-ChildItem -LiteralPath $rulesDst -Filter *.json -File | Where-Object { @($ruleFiles | ForEach-Object { $_.Name }) -notcontains $_.Name } | Remove-Item -Force
foreach ($rf in $ruleFiles) { Copy-Item -LiteralPath $rf.FullName -Destination (Join-Path $rulesDst $rf.Name) -Force }
$runRuleFiles = @(Get-ChildItem -LiteralPath $rulesDst -Filter *.json -File | Sort-Object Name | ForEach-Object { $_.FullName })
$rulesHash = Get-InputHash $runRuleFiles
$rules = New-Object System.Collections.Generic.List[object]
foreach ($rf in $runRuleFiles) {
    $doc = Read-JsonFile $rf
    foreach ($r in @($doc.rules)) { $rules.Add($r) }
}
Write-StageLog "rules: $($rules.Count) from $($runRuleFiles.Count) file(s) in $rulesSrc, sha256 $($rulesHash.Substring(0,12))"

# ---------- 2. evidence ----------

$repoDir = if ($envRec.repo) { $envRec.repo } else { $cfg.repo }
$patchText = Invoke-Native $cfg.git.command @("-C", $repoDir, "-c", "core.quotepath=off", "diff", "-U0", "-M", $mergeBase, $headCommit)
if ($patchText.ExitCode -ne 0) { Stop-Stage 3 "git diff failed: $($patchText.Output -join ' ')" }
$patch = Read-UnifiedPatch $patchText.Output

# Removed lines are numbered against the merge base, so their symbols come from the merge-base source.
$headLines = @{}
$baseLines = @{}
$oldPathOf = @{}
foreach ($f in @($inventory.files)) { $oldPathOf[$f.path] = $(if ($f.oldPath) { $f.oldPath } else { $f.path }) }
foreach ($path in @($patch.Keys)) {
    if ($path -like "*.java") {
        $hp = Join-Path $headWt ($path -replace '/', '\')
        $headLines[$path] = if (Test-Path -LiteralPath $hp) { @(Get-Content -LiteralPath $hp) } else { @() }
        $old = if ($oldPathOf.ContainsKey($path)) { $oldPathOf[$path] } else { $path }
        $show = Invoke-Native $cfg.git.command @("-C", $repoDir, "show", "$($mergeBase):$old")
        $baseLines[$path] = if ($show.ExitCode -eq 0) { @($show.Output) } else { @() }
    }
}

$itFiles = @(Get-ChildItem -LiteralPath (Join-Path $headWt "src\test") -Recurse -File -Filter *.java -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'IT(Case)?\.java$' } |
    ForEach-Object { $_.FullName.Substring($headWt.Length + 1) -replace '\\', '/' })
$itMode = if ($builds -and $builds.sides.head.integrationTests) { [string]$builds.sides.head.integrationTests.mode } else { "not-run" }

$ctx = @{
    Patch             = $patch
    HeadLines         = $headLines
    BaseLines         = $baseLines
    Inventory         = @($inventory.files)
    InventoryFindings = @($inventory.findings)
    DepDiff           = $depDiff
    DepAnalyze        = @{ base = $depDiff.base.analyze; head = $depDiff.head.analyze }
    ApiDiff           = $apiDiff
    LibraryApi        = $(if ($libApi -and $libApi.available) { $libApi } else { [pscustomobject]@{ items = @() } })
    ItFiles           = $itFiles
    ItMode            = $itMode
}
Write-StageLog ("evidence: {0} changed path(s) in the patch (merge base {1}), {2} IT test file(s) on head, integration tests on head: {3}" -f $patch.Count, $mergeBase.Substring(0, 10), $itFiles.Count, $itMode)

# ---------- 3. findings ----------

$found = New-Object System.Collections.Generic.List[object]
foreach ($rule in $rules) {
    $hits = @(Get-RuleFindings -Rule $rule -Ctx $ctx)
    foreach ($h in $hits) { $found.Add($h) }
}

# ---------- 4. hypotheses (analyst output, counted only when its inputs match) ----------

$analyst = Get-AgentProposal -RunDir $RunDir -Step "analyst" -FileName "hypotheses.json"
$hypCount = 0
if ($analyst.state -eq "used") {
    foreach ($h in @($analyst.doc.hypotheses)) {
        $hypCount++
        $found.Add([pscustomobject]@{
            ruleId   = $h.id
            title    = $h.claim
            severity = $(if ($h.severity) { $h.severity } else { "medium" })
            scope    = $(if ($h.scope) { $h.scope } else { "global" })
            file     = $(if ($h.file) { $h.file } else { "" })
            line     = $null
            symbols  = @($h.symbols)
            evidence = $h.claim
            impact   = $(if ($h.productionRisk) { [string]$h.productionRisk } else { [string]$h.claim })
            check    = $h.probeIdea
            source   = "analyst"
        })
    }
} elseif ($analyst.state -eq "stale") {
    Write-StageLog "analyst output is stale (its inputs changed). Not used. Run the analyst again."
}
Write-StageLog "hypotheses: $($analyst.state) ($hypCount used)"

# Stable order and ids: sort by rule, file, line, then assign F-001...
$sorted = @($found | Sort-Object -Property @{ e = { $_.ruleId } }, @{ e = { [string]$_.file } }, @{ e = { [string]$_.line } }, @{ e = { [string]$_.evidence } })
$findings = New-Object System.Collections.Generic.List[object]
$n = 0
foreach ($f in $sorted) {
    $n++
    $findings.Add([pscustomobject]@{
        id       = ("F-{0:D3}" -f $n)
        ruleId   = $f.ruleId
        title    = $f.title
        severity = $f.severity
        scope    = $f.scope
        file     = $f.file
        line     = $f.line
        symbols  = @($f.symbols)
        evidence = $f.evidence
        impact   = $f.impact
        check    = $f.check
        source   = $f.source
        status   = $(if ($f.source -eq "analyst") { "hypothesis" } else { "open" })
    })
}

# ---------- 5 to 7. flows, coverage, affected flows ----------

$headJacocoPath = Join-Path $headWt "target\site\jacoco\jacoco.xml"
if (-not (Test-Path -LiteralPath $headJacocoPath)) { Stop-Stage 3 "No head coverage report at $headJacocoPath. Add the jacoco-maven-plugin (prepare-agent and report) to the build, then run stage 1 again." }
$headJacoco = ConvertFrom-JacocoXml $headJacocoPath
$baseJacocoPath = Join-Path $baseWt "target\site\jacoco\jacoco.xml"
$baseJacoco = ConvertFrom-JacocoXml $baseJacocoPath

$index = Get-SourceIndex -SourceRoot (Join-Path $headWt "src\main\java")
$fqcnBySimple = @{}
foreach ($k in $index.Keys) { $fqcnBySimple[$k] = $index[$k].fqcn }

$flows = New-Object System.Collections.Generic.List[object]
foreach ($simple in ($index.Keys | Sort-Object)) {
    foreach ($fl in @(Get-SourceFlows -Text $index[$simple].text -ClassName $simple)) {
        $reach = @(Get-ReachSet -Index $index -ClassName $simple -MethodName $fl.method)
        $coverage = @(Get-FlowCoverage -Reach $reach -Jacoco $headJacoco -FqcnBySimple $fqcnBySimple)
        $baseCov = @(Get-FlowCoverage -Reach $reach -Jacoco $baseJacoco -FqcnBySimple $fqcnBySimple)
        $untraced = @($coverage | Where-Object { -not $_.traced } | ForEach-Object { $_.symbol })
        $flows.Add([pscustomobject]@{
            id       = $fl.id
            kind     = $fl.kind
            class    = $fl.class
            method   = $fl.method
            verb     = $fl.verb
            path     = $fl.path
            reach    = $reach
            coverage = @($coverage | ForEach-Object {
                $sym = $_.symbol
                $bc = $baseCov | Where-Object { $_.symbol -eq $sym } | Select-Object -First 1
                [pscustomobject]@{ symbol = $sym; headCovered = $_.covered; baseCovered = $bc.covered; traced = $_.traced }
            })
            untraced = $untraced
        })
    }
}
$flowRows = @($flows.ToArray() | Sort-Object id)

foreach ($f in $findings) {
    $affected = @($flowRows | Where-Object { Test-FindingAffectsFlow -Finding $f -Flow $_ } | ForEach-Object { $_.id })
    $f | Add-Member -NotePropertyName affectedFlows -NotePropertyValue $affected -Force
}

# ---------- 8. changed methods: what, why, and whether a test executes them ----------

$reasonsByKey = @{}
if ($analyst.state -eq "used") {
    foreach ($r in @($analyst.doc.changeReasons)) { $reasonsByKey["$($r.file)|$($r.symbol)"] = $r }
}
$changedRows = New-Object System.Collections.Generic.List[object]
foreach ($file in @($inventory.files | Where-Object { $_.category -eq "main-code" -and $_.path -like "*.java" })) {
    $simple = [System.IO.Path]::GetFileNameWithoutExtension($file.path)
    $fq = if ($fqcnBySimple.ContainsKey($simple)) { $fqcnBySimple[$simple] } else { $simple }
    $jaKey = $fq -replace '\.', '/'
    foreach ($sc in @($file.symbols)) {
        $isMethod = ([string]$sc.symbol) -like "method:*"
        $name = if ($isMethod) { $sc.symbol.Substring(7) } elseif (([string]$sc.symbol) -like "type:*") { $sc.symbol.Substring(5) } else { "" }
        $owner = if ($isMethod) { $simple } else { $name }
        # JaCoCo names constructors "<init>".
        $jacocoMethod = if ($isMethod -and $name -eq $simple) { "<init>" } else { $name }
        $covKey = if ($isMethod) { "$jaKey|$jacocoMethod" } else { "$jaKey|*" }
        $headCov = if ($headJacoco.ContainsKey($covKey)) { [int]$headJacoco[$covKey] } else { $null }
        $baseCov = if ($baseJacoco.ContainsKey($covKey)) { [int]$baseJacoco[$covKey] } else { $null }
        $symKey = if ($isMethod) { "$simple#$name" } else { "$owner#*" }
        $matched = @($findings | Where-Object {
            $_.file -eq $file.path -and (@($_.symbols).Count -eq 0 -or @($_.symbols) -contains $symKey -or @($_.symbols) -contains "$simple#*")
        } | ForEach-Object { $_.ruleId } | Sort-Object -Unique)
        $commitText = @($file.commits | ForEach-Object { "$($_.commit) `"$($_.subject)`"" })
        $kinds = @($sc.kinds)
        $what = if ($kinds.Count -gt 0) { ($kinds -join ", ") } else { "body" }
        $derived = "Changed: $what (+$($sc.added)/-$($sc.removed) lines)."
        if ($commitText.Count -gt 0) { $derived += " Commits: $($commitText -join '; ')." }
        if ($matched.Count -gt 0) { $derived += " Matches rules: $($matched -join ', ')." }
        $an = $reasonsByKey["$($file.path)|$($sc.symbol)"]
        $flowsReaching = @($flowRows | Where-Object { @($_.reach) -contains $symKey -or ($symKey -like "*#*" -and -not $isMethod -and @($_.reach | Where-Object { $_ -like "$owner#*" }).Count -gt 0) } | ForEach-Object { $_.id })
        $changedRows.Add([pscustomobject]@{
            file          = $file.path
            class         = $fq
            symbol        = [string]$sc.symbol
            kind          = $(if ($isMethod) { "method" } elseif ($name) { "type" } else { "file" })
            name          = $name
            changeKinds   = $kinds
            added         = $sc.added
            removed       = $sc.removed
            commits       = @($file.commits)
            rules         = $matched
            coverage      = [pscustomobject]@{ base = $baseCov; head = $headCov }
            tested        = ($null -ne $headCov -and $headCov -gt 0)
            flows         = $flowsReaching
            reason        = $derived
            analystReason = $(if ($an) { [string]$an.reason } else { "" })
            consequence   = $(if ($an) { [string]$an.consequence } else { "" })
        })
    }
}
$changedDoc = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    coverage      = [ordered]@{ base = $baseJacocoPath; head = $headJacocoPath; integrationTests = $itMode }
    analyst       = $analyst.state
    summary       = [ordered]@{
        symbols  = $changedRows.Count
        tested   = @($changedRows | Where-Object { $_.tested }).Count
        untested = @($changedRows | Where-Object { -not $_.tested -and $_.kind -ne "file" }).Count
    }
    methods       = $changedRows.ToArray()
}
Write-JsonFile (Join-Path $ArtifactsDir "changed-methods.json") $changedDoc
foreach ($c in @($changedRows | Where-Object { -not $_.tested -and $_.kind -ne "file" })) { Write-StageLog ("  changed but not executed by any test: {0} {1}" -f $c.file, $c.symbol) }

# ---------- 9. write ----------

$severityCounts = [ordered]@{}
foreach ($s in @("high", "medium", "low", "info")) { $severityCounts[$s] = @($findings | Where-Object { $_.severity -eq $s }).Count }

$impactDoc = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    rulesSha256   = $rulesHash
    rules         = @($rules | ForEach-Object { $_.id })
    analyst       = $analyst.state
    summary       = [ordered]@{
        findings     = $findings.Count
        bySeverity   = $severityCounts
        byScope      = [ordered]@{
            global = @($findings | Where-Object { $_.scope -eq "global" }).Count
            symbol = @($findings | Where-Object { $_.scope -eq "symbol" }).Count
            test   = @($findings | Where-Object { $_.scope -eq "test" }).Count
            none   = @($findings | Where-Object { $_.scope -eq "none" }).Count
        }
    }
    findings      = $findings.ToArray()
}
Write-JsonFile (Join-Path $ArtifactsDir "impact-findings.json") $impactDoc

$untracedFlows = @($flowRows | Where-Object { $_.untraced.Count -gt 0 }).Count
$reachDoc = [ordered]@{
    schemaVersion  = 1
    toolVersion    = $ToolVersion
    runId          = $RunId
    coverageSource = $headJacocoPath
    summary        = [ordered]@{ flows = $flowRows.Count; flowsWithUntracedCode = $untracedFlows }
    flows          = $flowRows
}
Write-JsonFile (Join-Path $ArtifactsDir "reach.json") $reachDoc

Write-StageLog ("findings: {0} (high {1}, medium {2}, low {3}, info {4}); flows: {5}, with untraced code: {6}; changed symbols: {7}, not executed by a test: {8}" -f `
    $findings.Count, $severityCounts.high, $severityCounts.medium, $severityCounts.low, $severityCounts.info, $flowRows.Count, $untracedFlows,
    $changedDoc.summary.symbols, $changedDoc.summary.untested)
foreach ($fl in $flowRows) {
    if ($fl.untraced.Count -gt 0) { Write-StageLog ("  untraced in {0}: {1}" -f $fl.id, ($fl.untraced -join ", ")) }
}

Set-StageStep $status "impact" "done" "" (Join-Path $ArtifactsDir "impact-findings.json")
Set-RunState $status "phase4-done" "phase4"
Write-JsonFile $StatusFile $status
Write-StageLog "phase 4 done: impact findings, reach and changed methods written for run $RunId"
exit 0

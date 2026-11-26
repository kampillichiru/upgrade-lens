<#
.SYNOPSIS
  Stage 7 of UpgradeLens (manual review items): lists what the tests and probes cannot decide, with the
  question, the expected result on master, and an owner. People answer these in stage 9.

.DESCRIPTION
  Items come from:
    - each library API change (stage 3) that master's code used, with the release-notes result if any
    - each release-notes claim that the notes contradict or do not mention (release-notes agent)
    - each added or removed dependency assessed unnecessary or review, and each major version conflict
    - each changed method or type that no test executes on change_branch
    - each finding that no check verified (status open or hypothesis) and each confirmed behavior change
    - each flow with untraced code (the expected master result is the master probe of that flow, if any)
    - each probe that differs between the branches
    - bean and property dump differences (a bean whose class moved is normal in a major upgrade, but it
      still needs a person to say so)
    - each blocking error from stage 5 (a probe or a test that could not run)

  Owners come from config\upgrade-lens.json, "owners.reviewer" and "owners.riskAcceptor". An empty owner
  is written as "unassigned", and the verdict lists the item as pending.

  Writes artifacts\manual-items.json. Item ids (MI-001...) are what the decisions file refers to.

  Exit codes:
    0  written
    2  usage or run problem (no run, stage 6 not done)

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 6 is done.

.EXAMPLE
  .\scripts\phase7-manual.ps1 -RunId 20261126-131717
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
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase6-done", "phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe")
if (-not $RunId) { Write-Host "No run with stage 6 done under $runsRoot. Run phase6-flows.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$StatusFile = Join-Path $RunDir "status.json"
$script:StageLog = Join-Path $RunDir "logs\phase7.log"
$cfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$status = Read-JsonFile $StatusFile
$impact = Read-JsonFile (Join-Path $ArtifactsDir "impact-findings.json")
$flows = Read-JsonFile (Join-Path $ArtifactsDir "flows.json")
$behavior = Read-JsonFile (Join-Path $ArtifactsDir "behavior-results.json")
if (-not $cfg -or -not $status -or -not $impact -or -not $flows -or -not $behavior) { Write-Host "Run incomplete: $RunDir (needs stages 4 to 6)"; exit 2 }

function Get-Owner([string]$Role) {
    $v = $null
    if ($cfg.owners) { $v = $cfg.owners.$Role }
    if ([string]::IsNullOrWhiteSpace([string]$v)) { return "unassigned" }
    return [string]$v
}

Write-StageLog "phase 7 manual review items for run $RunId"
$items = New-Object System.Collections.Generic.List[object]
function Add-Item($Kind, $Ref, [string]$Question, [string]$ExpectedMaster, [string]$Owner) {
    $n = $items.Count + 1
    $items.Add([pscustomobject]@{
        id             = ("MI-{0:D3}" -f $n)
        kind           = $Kind
        ref            = $Ref
        question       = $Question
        expectedMaster = $ExpectedMaster
        owner          = $Owner
    })
}

# Status per finding comes from stage 6.
$statusById = @{}
foreach ($fs in @($flows.findingStatus)) { $statusById[$fs.id] = $fs.status }

foreach ($f in @($impact.findings)) {
    $st = $statusById[$f.id]
    if ($st -notin @("open", "hypothesis", "confirmed")) { continue }
    $expected = "Master behaves as the master code and its probes show (see the linked probes)."
    $linked = @($behavior.checks | Where-Object { $_.ruleId -eq $f.ruleId })
    if ($linked.Count -eq 0) { $expected = "Not determined: no check is linked to this rule. Master's behavior has to be read from the master code." }
    $owner = Get-Owner "reviewer"
    Add-Item -Kind "finding" -Ref $f.id -Question ("[{0}] {1}: {2}. Is this change safe for the flows it touches? Check: {3}" -f $f.severity, $f.title, $f.evidence, $f.check) -ExpectedMaster $expected -Owner $owner
}

foreach ($fl in @($flows.flows)) {
    if (@($fl.untraced).Count -eq 0) { continue }
    $probe = @($behavior.probes | Where-Object { $_.flowId -eq $fl.id }) | Select-Object -First 1
    $expected = "Not determined: no probe covers this flow."
    if ($probe -and $probe.base) { $expected = "Master (base) gives status $($probe.base.status) on $($probe.request): $($probe.base.body)" }
    Add-Item -Kind "untraced-flow" -Ref $fl.id -Question ("Flow {0} reaches code that no test executes: {1}. Test it, or accept the full regression for this flow." -f $fl.id, (@($fl.untraced) -join ", ")) -ExpectedMaster $expected -Owner (Get-Owner "reviewer")
}

foreach ($p in @($behavior.probes | Where-Object { $_.outcome -eq "different" })) {
    $expected = "Master (base) gives status $($p.base.status), content type '$($p.base.contentType)', body: $($p.base.body)"
    # Focus: the flow the difference belongs to. Infrastructure probes are not order flows; say so, and say
    # how many order-flow probes still match master, so the reader knows what is unchanged.
    $focus = if ($p.flowId -eq "infrastructure") { "Focus: infrastructure, not an order flow. Platform health checks call $($p.request)." } else { "Focus: flow $($p.flowId)." }
    $orderProbes = @($behavior.probes | Where-Object { $_.flowId -like "*/api/*" })
    $orderSame = @($orderProbes | Where-Object { $_.outcome -eq "same" }).Count
    $focus += " Order-flow probes matching master: $orderSame of $($orderProbes.Count)."
    Add-Item -Kind "probe-difference" -Ref $p.id -Question ("Probe {0} ({1}) differs on change_branch: {2}. Is this change intended? {3}" -f $p.id, $p.request, ($p.differences -join "; "), $focus) -ExpectedMaster $expected -Owner (Get-Owner "riskAcceptor")
}

if ($behavior.dumps -and $behavior.dumps.available) {
    $d = $behavior.dumps.beans
    if (@($d.added).Count -gt 0 -or @($d.removed).Count -gt 0 -or @($d.typeChanged).Count -gt 0) {
        $sample = (@($d.typeChanged) + @($d.removed) | Select-Object -First 6) -join ", "
        Add-Item -Kind "bean-dump" -Ref "beans" -Question ("Bean set differs: {0} added, {1} removed, {2} with a different class. Examples: {3}. Are these framework renames only?" -f `
            @($d.added).Count, @($d.removed).Count, @($d.typeChanged).Count, $sample) -ExpectedMaster "Master's bean set is in artifacts\behavior-results.json (dumps.beans)." -Owner (Get-Owner "reviewer")
    }
    $pr = $behavior.dumps.properties
    if (@($pr.added).Count -gt 0 -or @($pr.removed).Count -gt 0 -or @($pr.changed).Count -gt 0) {
        Add-Item -Kind "property-dump" -Ref "properties" -Question ("Application properties differ: added {0}, removed {1}, changed {2}." -f @($pr.added).Count, @($pr.removed).Count, @($pr.changed).Count) -ExpectedMaster "Master's application properties are in artifacts\behavior-results.json." -Owner (Get-Owner "reviewer")
    }
}

# Library API changes that master's code used (removed or moved), with the release-notes result when the
# release-notes agent ran on the current inputs.
$libApi = Read-JsonFile (Join-Path $ArtifactsDir "library-api-diff.json")
$notes = Get-AgentProposal -RunDir $RunDir -Step "release-notes" -FileName "release-notes.json"
$notesByRef = @{}
if ($notes.state -eq "used") { foreach ($n in @($notes.doc.items)) { $notesByRef[[string]$n.ref] = $n } }
if ($libApi -and $libApi.available) {
    foreach ($it in @($libApi.items | Where-Object { $_.severity -in @("high", "medium") })) {
        $where = @(@($it.usage.base) + @($it.usage.head) | Where-Object { $_.source } | Select-Object -First 3 | ForEach-Object { "$($_.source):$($_.line)" }) -join ", "
        $rn = $notesByRef[$it.id]
        $rnText = if ($rn) { " Release notes: $($rn.result) ($($rn.source.url))." } else { " Release notes: not validated." }
        $expected = "Master uses $($it.class) from $($it.artifact.base)$(if ($where) { ' at ' + $where })."
        Add-Item -Kind "library-change" -Ref $it.id -Question ("[{0}] {1} {2}: {3} Is the change_branch replacement equivalent for this code?{4}" -f $it.severity, $it.state, $it.class, $it.summary, $rnText) -ExpectedMaster $expected -Owner (Get-Owner "reviewer")
    }
}
if ($notes.state -eq "used") {
    foreach ($n in @($notes.doc.items | Where-Object { $_.result -in @("contradicted", "not-found") })) {
        if ($libApi -and @($libApi.items | Where-Object { $_.id -eq $n.ref -and $_.severity -in @("high", "medium") }).Count -gt 0) { continue }
        Add-Item -Kind "release-note" -Ref ([string]$n.ref) -Question ("Release notes {0} for {1}: {2} Source: {3}" -f $n.result, $n.artifact, $n.claim, $n.source.url) -ExpectedMaster "Not applicable: a statement about the library, not a master result." -Owner (Get-Owner "reviewer")
    }
}

# Dependencies: unnecessary or undecided additions, removals master used, and major version conflicts.
$depDiff = Read-JsonFile (Join-Path $ArtifactsDir "dependency-diff.json")
if ($depDiff) {
    foreach ($a in @($depDiff.assessment | Where-Object { $_.assessment -in @("unnecessary", "review") -and $_.change -ne "version" })) {
        Add-Item -Kind "dependency" -Ref $a.ga -Question ("{0} dependency {1} ({2}): {3}. {4} Keep it?" -f $a.change, $a.ga, $a.assessment, $a.reason, $a.impact) -ExpectedMaster $(if ($a.change -eq "added") { "Master builds without $($a.ga)." } else { "Master declares $($a.ga) $($a.version)." }) -Owner (Get-Owner "reviewer")
    }
    foreach ($c in @($depDiff.conflicts | Where-Object { $_.major })) {
        Add-Item -Kind "dependency-conflict" -Ref $c.ga -Question ("{0}: {1} asks for {2}, but {3} is resolved (major difference). Does {1} work with {3}?" -f $c.ga, (@($c.requiredBy) -join ", "), $c.requested, $c.resolved) -ExpectedMaster "Not applicable: a change_branch resolution." -Owner (Get-Owner "reviewer")
    }
}

# Changed main code that no test executes (unit or integration).
$changed = Read-JsonFile (Join-Path $ArtifactsDir "changed-methods.json")
foreach ($m in @($changed.methods | Where-Object { -not $_.tested -and $_.kind -ne "file" })) {
    Add-Item -Kind "untested-change" -Ref "$($m.file)#$($m.name)" -Question ("Changed {0} {1} in {2} is not executed by any test on change_branch. {3} Write a test, or accept the risk." -f $m.kind, $m.name, $m.file, $m.reason) -ExpectedMaster $(if ($null -ne $m.coverage.base -and $m.coverage.base -gt 0) { "Master's tests execute it ($($m.coverage.base) covered instructions)." } else { "Master's tests do not execute it either." }) -Owner (Get-Owner "reviewer")
}

foreach ($e in @($behavior.blockingErrors)) {
    Add-Item -Kind "stage-error" -Ref "stage5" -Question ("A check could not run: {0}. Fix the cause and run stage 5 again." -f $e) -ExpectedMaster "Not applicable: the check did not produce a result." -Owner (Get-Owner "reviewer")
}

$list = $items.ToArray()
$doc = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    summary       = [ordered]@{
        items = $list.Count
        byKind = [ordered]@{
            finding          = @($list | Where-Object { $_.kind -eq "finding" }).Count
            untracedFlow     = @($list | Where-Object { $_.kind -eq "untraced-flow" }).Count
            probeDifference  = @($list | Where-Object { $_.kind -eq "probe-difference" }).Count
            beanDump         = @($list | Where-Object { $_.kind -eq "bean-dump" }).Count
            propertyDump     = @($list | Where-Object { $_.kind -eq "property-dump" }).Count
            libraryChange    = @($list | Where-Object { $_.kind -eq "library-change" }).Count
            releaseNote      = @($list | Where-Object { $_.kind -eq "release-note" }).Count
            dependency       = @($list | Where-Object { $_.kind -eq "dependency" }).Count
            dependencyConflict = @($list | Where-Object { $_.kind -eq "dependency-conflict" }).Count
            untestedChange   = @($list | Where-Object { $_.kind -eq "untested-change" }).Count
            stageError       = @($list | Where-Object { $_.kind -eq "stage-error" }).Count
        }
    }
    items         = $list
}
Write-JsonFile (Join-Path $ArtifactsDir "manual-items.json") $doc
Write-StageLog ("manual items: {0}" -f $list.Count)
foreach ($i in $list) { Write-StageLog ("  {0} [{1}] {2}" -f $i.id, $i.kind, $i.ref) }

Set-StageStep $status "manual" "done" "" (Join-Path $ArtifactsDir "manual-items.json")
Set-RunState $status "phase7-done" "phase7"
Write-JsonFile $StatusFile $status
Write-StageLog "phase 7 done: manual items written for run $RunId"
exit 0

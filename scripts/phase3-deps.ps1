<#
.SYNOPSIS
  Stage 3 of UpgradeLens (dependencies and API): compares the resolved dependencies, the public API of the
  application classes, and the API of every library class the application uses, between the base and head
  worktrees of a run.

.DESCRIPTION
  Needs stage 1 (compiled target\classes on both sides). Steps, in order:
    1. tree        mvn dependency:tree -Dverbose on each side (falls back to the plain tree when the plugin
                   cannot do verbose), so transitive, BOM-driven and conflicting versions show
    2. analyze     mvn dependency:analyze on each side: unused declared and used undeclared dependencies
    3. diff        added, removed and version-changed artifacts (major changes flagged), direct or transitive
    4. assess      per added or removed direct dependency: required, unnecessary, replacement or review, and
                   what it can affect; version conflicts in the head tree (who asked for which version);
                   suggested commands for deeper analysis
    5. api         javap -public of each application class on both sides (head JDK's javap for both)
    6. library     the library API diff (lib\library.ps1): library classes and members the code uses, compared
                   between the two classpaths, each change tied to the code lines that use it
  Writes artifacts\dependency-diff.json, artifacts\api-diff.json and artifacts\library-api-diff.json.
  None of them holds a timestamp, so the same inputs give the same bytes (agent input hashes depend on it).

  A library step that cannot run (for example jdeps fails) does not fail the stage: library-api-diff.json
  records "available": false with the reason, and stage 5 lists it as a gap, so it is never a silent pass.

  Exit codes:
    0  written
    2  usage or run problem (no run, worktree or compiled classes missing)
    3  Maven or javap could not run

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 2 is done.

.PARAMETER Config
  Config file (for the runs folder). Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.EXAMPLE
  .\scripts\phase3-deps.ps1 -RunId 20261126-131717
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
. (Join-Path $PSScriptRoot "lib\library.ps1")

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase2-done", "phase3-done", "phase4-done", "phase5-done", "phase6-done", "phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe")
if (-not $RunId) { Write-Host "No run with stage 2 done under $runsRoot. Run run-pipeline.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$LogsDir = Join-Path $RunDir "logs"
$StatusFile = Join-Path $RunDir "status.json"
$script:StageLog = Join-Path $LogsDir "phase3.log"
$cfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$envRec = Read-JsonFile (Join-Path $ArtifactsDir "environment.json")
$status = Read-JsonFile $StatusFile
if (-not $cfg -or -not $envRec -or -not $status) { Write-Host "Run incomplete: $RunDir"; exit 2 }

$settingsArg = $null
if ($cfg.maven.settingsFile -and (Test-Path -LiteralPath $cfg.maven.settingsFile)) { $settingsArg = $cfg.maven.settingsFile }
$ignoreUnused = @(Get-ConfigValue $cfg "dependencies.ignoreUnused" @() | ForEach-Object { [string]$_ } | Where-Object { $_ })
$libraryEnabled = [bool](Get-ConfigValue $cfg "library.enabled" $true)
$maxMembers = [int](Get-ConfigValue $cfg "library.maxMembersPerClass" 40)

function Stop-Stage([int]$Code, [string]$Reason) {
    Write-StageLog $Reason
    Set-StageStep $status "dependencies-api" "failed" $Reason
    Set-RunState $status "failed-tool" "phase3"
    Write-JsonFile $StatusFile $status
    exit $Code
}

Write-StageLog "phase 3 dependencies and API for run $RunId"

$sides = @(
    [pscustomobject]@{ Side = "base"; Branch = $cfg.baseBranch; Jdk = $cfg.java.base; Worktree = $envRec.branches.base.worktree; Commit = $envRec.branches.base.commit },
    [pscustomobject]@{ Side = "head"; Branch = $cfg.headBranch; Jdk = $cfg.java.head; Worktree = $envRec.branches.head.worktree; Commit = $envRec.branches.head.commit }
)
foreach ($s in $sides) {
    if (-not (Test-Path -LiteralPath (Join-Path $s.Worktree "pom.xml"))) { Write-Host "No pom.xml in $($s.Worktree)"; exit 2 }
    if (-not (Test-Path -LiteralPath (Join-Path $s.Worktree "target\classes"))) {
        Write-Host "$($s.Side): no target\classes in $($s.Worktree). Run stage 1 first."; exit 2
    }
    if (-not (Test-Path -LiteralPath (Join-Path $s.Jdk "bin\javap.exe"))) { Write-Host "$($s.Side): no javap.exe under $($s.Jdk)"; exit 2 }
}
$headJdkBin = Join-Path $sides[1].Jdk "bin"
$javap = Join-Path $headJdkBin "javap.exe"
$jdeps = Join-Path $headJdkBin "jdeps.exe"

function Get-MvnArgs($Side, [string[]]$Goals) {
    $a = @("-B", "-ntp")
    if ($settingsArg) { $a += @("-s", $settingsArg) }
    $a += @("-f", (Join-Path $Side.Worktree "pom.xml"))
    return ($a + $Goals)
}

function Invoke-SideMaven($Side, [string[]]$Goals, [string]$LogName) {
    $r = Invoke-WithJdk -JdkHome $Side.Jdk -Action { Invoke-Native $cfg.maven.command (Get-MvnArgs $Side $Goals) }
    $log = Join-Path $LogsDir $LogName
    [System.IO.File]::WriteAllLines($log, [string[]]$r.Output)
    return [pscustomobject]@{ ExitCode = $r.ExitCode; Output = $r.Output; Log = $log }
}

# ---------- 1 to 2. tree (verbose when possible) and analyze ----------

$trees = @{}
$verbose = @{}
$verboseAvailable = $true
$analyze = @{}
$classpaths = @{}
try {
    foreach ($s in $sides) {
        $treeFile = Join-Path $ArtifactsDir "deps-$($s.Side).txt"
        if (Test-Path -LiteralPath $treeFile) { Remove-Item -LiteralPath $treeFile -Force }
        $r = Invoke-SideMaven $s @("dependency:tree", "-Dverbose", "-DoutputType=text", "-DoutputFile=$treeFile") "deps-tree-$($s.Side).log"
        if ($r.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $treeFile)) {
            Write-StageLog "$($s.Side): verbose dependency tree not available (see $($r.Log)); using the plain tree"
            $verboseAvailable = $false
            $r = Invoke-SideMaven $s @("dependency:tree", "-DoutputType=text", "-DoutputFile=$treeFile") "deps-tree-$($s.Side).log"
            if ($r.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $treeFile)) { Stop-Stage 3 "dependency:tree failed on $($s.Side); see $($r.Log)" }
        }
        $treeLines = @(Get-Content -LiteralPath $treeFile)
        $trees[$s.Side] = @(ConvertFrom-DependencyTree $treeLines)
        $verbose[$s.Side] = ConvertFrom-DependencyTreeVerbose $treeLines
        Write-StageLog "$($s.Side): $($trees[$s.Side].Count) artifacts in the resolved tree, $(@($verbose[$s.Side].conflicts).Count) version conflict(s)"

        $r = Invoke-SideMaven $s @("dependency:analyze") "deps-analyze-$($s.Side).log"
        if ($r.ExitCode -ne 0) { Stop-Stage 3 "dependency:analyze failed on $($s.Side); see $($r.Log)" }
        $analyze[$s.Side] = ConvertFrom-DependencyAnalyze $r.Output

        if ($libraryEnabled) {
            $cpFile = Join-Path $ArtifactsDir "classpath-$($s.Side).txt"
            if (Test-Path -LiteralPath $cpFile) { Remove-Item -LiteralPath $cpFile -Force }
            $r = Invoke-SideMaven $s @("dependency:build-classpath", "-Dmdep.outputFile=$cpFile") "deps-classpath-$($s.Side).log"
            $classpaths[$s.Side] = if ($r.ExitCode -eq 0 -and (Test-Path -LiteralPath $cpFile)) {
                @(((Get-Content -LiteralPath $cpFile -Raw) -split ';') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
            } else { $null }
        }
    }
} catch {
    Stop-Stage 3 "Maven step threw: $($_.Exception.Message)"
}

# ---------- 3 to 4. diff, assessment, conflicts, suggestions ----------

$diff = Compare-DependencyTrees -Base $trees["base"] -Head $trees["head"]
$assessment = @(Get-DependencyAssessment -Diff $diff -BaseTree $trees["base"] -HeadTree $trees["head"] -Analyze $analyze -IgnoreUnused $ignoreUnused)
$headConflicts = @($verbose["head"].conflicts)
$baseConflictKeys = @($verbose["base"].conflicts | ForEach-Object { "$($_.ga)|$($_.requested)|$($_.resolved)" })
$conflictRows = @($headConflicts | ForEach-Object {
    [pscustomobject]@{
        ga = $_.ga; requested = $_.requested; resolved = $_.resolved; major = $_.major; requiredBy = @($_.requiredBy)
        newOnHead = ($baseConflictKeys -notcontains "$($_.ga)|$($_.requested)|$($_.resolved)")
    }
})
$suggestions = @(Get-DependencySuggestions -Conflicts $headConflicts -Assessment $assessment -Analyze $analyze -VerboseAvailable $verboseAvailable)

$depDoc = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    base          = [ordered]@{ branch = $cfg.baseBranch; commit = $sides[0].Commit; artifacts = @($trees["base"]).Count; analyze = $analyze["base"]; conflicts = @($verbose["base"].conflicts).Count }
    head          = [ordered]@{ branch = $cfg.headBranch; commit = $sides[1].Commit; artifacts = @($trees["head"]).Count; analyze = $analyze["head"]; conflicts = @($verbose["head"].conflicts).Count }
    verboseTree   = $verboseAvailable
    added         = @($diff.added)
    removed       = @($diff.removed)
    changed       = @($diff.changed)
    assessment    = $assessment
    conflicts     = $conflictRows
    duplicates    = [ordered]@{ base = $verbose["base"].duplicates; head = $verbose["head"].duplicates }
    managed       = @($verbose["head"].managed)
    suggestions   = $suggestions
    summary       = [ordered]@{
        added           = @($diff.added).Count
        removed         = @($diff.removed).Count
        changed         = @($diff.changed).Count
        majorChanged    = @($diff.changed | Where-Object { $_.major }).Count
        directChanged   = @($diff.changed | Where-Object { $_.direct }).Count
        directAdded     = @($diff.added | Where-Object { $_.direct }).Count
        directRemoved   = @($diff.removed | Where-Object { $_.direct }).Count
        unnecessary     = @($assessment | Where-Object { $_.assessment -eq "unnecessary" }).Count
        review          = @($assessment | Where-Object { $_.assessment -eq "review" }).Count
        conflicts       = $conflictRows.Count
        majorConflicts  = @($conflictRows | Where-Object { $_.major }).Count
    }
}
Write-JsonFile (Join-Path $ArtifactsDir "dependency-diff.json") $depDoc
Write-StageLog ("dependencies: added {0} ({1} direct), removed {2} ({3} direct), version changed {4} (major {5}, direct {6}); conflicts on head {7} (major {8})" -f `
    $depDoc.summary.added, $depDoc.summary.directAdded, $depDoc.summary.removed, $depDoc.summary.directRemoved, $depDoc.summary.changed,
    $depDoc.summary.majorChanged, $depDoc.summary.directChanged, $depDoc.summary.conflicts, $depDoc.summary.majorConflicts)
foreach ($a in $assessment | Where-Object { $_.change -ne "version" }) { Write-StageLog ("  {0} {1}: {2} - {3}" -f $a.change, $a.ga, $a.assessment, $a.reason) }

# ---------- 5. API: public signatures of application classes ----------

$headIndex = Get-SourceIndex -SourceRoot (Join-Path $sides[1].Worktree "src\main\java")
$baseIndex = Get-SourceIndex -SourceRoot (Join-Path $sides[0].Worktree "src\main\java")
$allClasses = @(@($headIndex.Keys) + @($baseIndex.Keys) | Sort-Object -Unique)

function Get-PublicMembers([string]$ClassesDir, [string]$Fqcn) {
    $classFile = Join-Path $ClassesDir (($Fqcn -replace '\.', '\') + ".class")
    if (-not (Test-Path -LiteralPath $classFile)) { return $null }
    $r = Invoke-Native $javap @("-public", "-cp", $ClassesDir, $Fqcn)
    if ($r.ExitCode -ne 0) { throw "javap failed for $Fqcn in $ClassesDir" }
    return @($r.Output | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -notmatch '^Compiled from' })
}

$apiClasses = New-Object System.Collections.Generic.List[object]
try {
    foreach ($simple in $allClasses) {
        $fqcn = if ($headIndex.ContainsKey($simple)) { $headIndex[$simple].fqcn } else { $baseIndex[$simple].fqcn }
        $baseMembers = Get-PublicMembers (Join-Path $sides[0].Worktree "target\classes") $fqcn
        $headMembers = Get-PublicMembers (Join-Path $sides[1].Worktree "target\classes") $fqcn
        if ($null -eq $baseMembers -and $null -eq $headMembers) { continue }
        if ($null -eq $baseMembers) { $state = "added" }
        elseif ($null -eq $headMembers) { $state = "removed" }
        else {
            $removedMembers = @($baseMembers | Where-Object { $headMembers -notcontains $_ })
            $addedMembers = @($headMembers | Where-Object { $baseMembers -notcontains $_ })
            $state = if ($removedMembers.Count -eq 0 -and $addedMembers.Count -eq 0) { "same" } else { "changed" }
        }
        # Empty lists are built as variables first: an "if" expression that yields @() is unrolled to $null.
        if ($state -ne "changed") { $removedMembers = @(); $addedMembers = @() }
        $apiClasses.Add([pscustomobject]@{
            fqcn           = $fqcn
            state          = $state
            removedMembers = @($removedMembers)
            addedMembers   = @($addedMembers)
        })
    }
} catch {
    Stop-Stage 3 "API step failed: $($_.Exception.Message)"
}

$apiDoc = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    scope         = "application classes in target\classes"
    javap         = $javap
    summary       = [ordered]@{
        same    = @($apiClasses | Where-Object { $_.state -eq "same" }).Count
        changed = @($apiClasses | Where-Object { $_.state -eq "changed" }).Count
        added   = @($apiClasses | Where-Object { $_.state -eq "added" }).Count
        removed = @($apiClasses | Where-Object { $_.state -eq "removed" }).Count
    }
    classes       = $apiClasses.ToArray()
}
Write-JsonFile (Join-Path $ArtifactsDir "api-diff.json") $apiDoc
Write-StageLog ("application api: {0} same, {1} changed, {2} added, {3} removed" -f $apiDoc.summary.same, $apiDoc.summary.changed, $apiDoc.summary.added, $apiDoc.summary.removed)

# ---------- 6. library API diff ----------

function Get-ClassDirs($Side) {
    return @(@("target\classes", "target\test-classes") | ForEach-Object { Join-Path $Side.Worktree $_ } | Where-Object { Test-Path -LiteralPath $_ })
}

function Get-SourceMap($Side) {
    # Outer application class fqcn -> @{ path (relative, "/"); lines }, from src\main\java and src\test\java.
    $map = @{}
    foreach ($root in @("src\main\java", "src\test\java")) {
        $dir = Join-Path $Side.Worktree $root
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        foreach ($f in @(Get-ChildItem -LiteralPath $dir -Recurse -Filter *.java -File)) {
            $rel = $f.FullName.Substring($dir.Length + 1)
            $fq = ($rel.Substring(0, $rel.Length - 5)) -replace '\\', '.'
            $map[$fq] = @{ path = (($root -replace '\\', '/') + "/" + ($rel -replace '\\', '/')); lines = @(Get-Content -LiteralPath $f.FullName) }
        }
    }
    return $map
}

function Invoke-JavapBatch([string[]]$Arguments, [string[]]$Targets, [int]$BatchSize = 40) {
    # javap over many classes, in batches that keep the command line short. A class javap cannot read
    # is reported on stderr and skipped; the rest of the batch is still printed.
    $out = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $Targets.Count; $i += $BatchSize) {
        $chunk = @($Targets[$i..([Math]::Min($i + $BatchSize, $Targets.Count) - 1)])
        $r = Invoke-Native $javap ($Arguments + $chunk)
        foreach ($l in $r.Output) { $out.Add([string]$l) }
    }
    return $out.ToArray()
}

$libDoc = $null
$libPath = Join-Path $ArtifactsDir "library-api-diff.json"
if (-not $libraryEnabled) {
    $libDoc = [ordered]@{ schemaVersion = 1; toolVersion = $ToolVersion; runId = $RunId; available = $false; reason = "disabled in the config (library.enabled = false)"; items = @() }
} elseif (-not (Test-Path -LiteralPath $jdeps)) {
    $libDoc = [ordered]@{ schemaVersion = 1; toolVersion = $ToolVersion; runId = $RunId; available = $false; reason = "no jdeps.exe under $headJdkBin"; items = @() }
} elseif ($null -eq $classpaths["base"] -or $null -eq $classpaths["head"]) {
    $libDoc = [ordered]@{ schemaVersion = 1; toolVersion = $ToolVersion; runId = $RunId; available = $false; reason = "dependency:build-classpath failed; see logs\deps-classpath-*.log"; items = @() }
} else {
    try {
        $deps = @{}; $code = @{}; $jars = @{}; $api = @{}; $sources = @{}
        foreach ($s in $sides) {
            $dirs = @(Get-ClassDirs $s)
            $jd = Invoke-Native $jdeps (@("-verbose:class") + $dirs)
            if ($jd.ExitCode -ne 0) { throw "jdeps failed on $($s.Side): $(@($jd.Output | Select-Object -First 3) -join ' ')" }
            $deps[$s.Side] = @(ConvertFrom-JdepsVerbose $jd.Output)
            $classFiles = @($dirs | ForEach-Object { Get-ChildItem -LiteralPath $_ -Recurse -Filter *.class -File } | ForEach-Object { $_.FullName })
            $code[$s.Side] = @(ConvertFrom-JavapCode (Invoke-JavapBatch -Arguments @("-c", "-p", "-l") -Targets $classFiles))
            $sources[$s.Side] = Get-SourceMap $s
            Write-StageLog "$($s.Side): $($classFiles.Count) application class file(s), $($deps[$s.Side].Count) class reference(s), $($code[$s.Side].Count) code reference(s)"
        }
        $appClasses = @{}
        foreach ($side in @("base", "head")) { foreach ($r in $deps[$side]) { $appClasses[$r.from] = $true } }
        $libClasses = @(@($deps["base"]) + @($deps["head"]) | Where-Object { $_.location -eq "not found" -and -not $appClasses.ContainsKey($_.to) } | ForEach-Object { $_.to } | Sort-Object -Unique)

        $baseIdx = Get-JarClassIndex -Jars $classpaths["base"] -Classes $libClasses
        $jars["base"] = $baseIdx.jarByClass
        $missingOnHeadSimple = @()
        $headIdx = Get-JarClassIndex -Jars $classpaths["head"] -Classes $libClasses
        $missingOnHead = @($libClasses | Where-Object { $baseIdx.jarByClass.ContainsKey($_) -and -not $headIdx.jarByClass.ContainsKey($_) })
        if ($missingOnHead.Count -gt 0) {
            $missingOnHeadSimple = @($missingOnHead | ForEach-Object { ($_ -split '\.')[-1] } | Sort-Object -Unique)
            $headIdx = Get-JarClassIndex -Jars $classpaths["head"] -Classes $libClasses -SimpleNames $missingOnHeadSimple
        }
        $jars["head"] = $headIdx.jarByClass
        $moved = @{}
        foreach ($m in $missingOnHead) {
            $simple = ($m -split '\.')[-1]
            if ($headIdx.bySimpleName.ContainsKey($simple)) { $moved[$m] = @($headIdx.bySimpleName[$simple]) }
        }
        # Moved targets are compared too, so the moved item can show the new class's API.
        $extraHead = @($moved.Values | ForEach-Object { $_ } | Where-Object { -not $headIdx.jarByClass.ContainsKey($_) } | Sort-Object -Unique)
        if ($extraHead.Count -gt 0) {
            $extraIdx = Get-JarClassIndex -Jars $classpaths["head"] -Classes $extraHead
            foreach ($k in $extraIdx.jarByClass.Keys) { $jars["head"][$k] = $extraIdx.jarByClass[$k] }
        }

        foreach ($side in @("base", "head")) {
            $urls = @($jars[$side].Keys | Sort-Object | ForEach-Object {
                "jar:file:///" + ($jars[$side][$_] -replace '\\', '/') + "!/" + ($_ -replace '\.', '/') + ".class"
            })
            $api[$side] = ConvertFrom-JavapApi (Invoke-JavapBatch -Arguments @("-public", "-s") -Targets $urls)
        }

        $result = Get-LibraryApiItems -Deps $deps -Code $code -Api $api -Jars $jars -Sources $sources -MovedCandidates $moved -MaxMembers $maxMembers
        $items = @($result.items)
        $libDoc = [ordered]@{
            schemaVersion = 1
            toolVersion   = $ToolVersion
            runId         = $RunId
            available     = $true
            scope         = "library classes referenced by the compiled main and test classes of either branch (jdeps), compared with javap -public -s between the two resolved classpaths; member use from javap -c with source lines"
            tools         = [ordered]@{ jdeps = $jdeps; javap = $javap }
            summary       = [ordered]@{
                referencedClasses = $result.referencedClasses
                same     = $result.counts.same
                changed  = $result.counts.changed
                removed  = $result.counts.removed
                moved    = $result.counts.moved
                added    = $result.counts.added
                high     = @($items | Where-Object { $_.severity -eq "high" }).Count
                medium   = @($items | Where-Object { $_.severity -eq "medium" }).Count
                low      = @($items | Where-Object { $_.severity -eq "low" }).Count
                info     = @($items | Where-Object { $_.severity -eq "info" }).Count
            }
            items         = $items
        }
        Write-StageLog ("library api: {0} referenced class(es); {1} same, {2} changed, {3} removed, {4} moved, {5} added; high {6}, medium {7}" -f `
            $result.referencedClasses, $result.counts.same, $result.counts.changed, $result.counts.removed, $result.counts.moved, $result.counts.added,
            $libDoc.summary.high, $libDoc.summary.medium)
        foreach ($it in @($items | Where-Object { $_.severity -in @("high", "medium") })) { Write-StageLog ("  {0} [{1}] {2} {3}: {4}" -f $it.id, $it.severity, $it.state, $it.class, $it.summary) }
    } catch {
        $libDoc = [ordered]@{ schemaVersion = 1; toolVersion = $ToolVersion; runId = $RunId; available = $false; reason = "library API step failed: $($_.Exception.Message)"; items = @() }
        Write-StageLog "library api: NOT AVAILABLE - $($libDoc.reason)"
    }
}
Write-JsonFile $libPath $libDoc -Depth 14

Set-StageStep $status "dependencies-api" "done" $(if ($libDoc.available) { "" } else { "library API diff not available: $($libDoc.reason)" }) (Join-Path $ArtifactsDir "dependency-diff.json")
Set-RunState $status "phase3-done" "phase3"
Write-JsonFile $StatusFile $status
Write-StageLog "phase 3 done: dependencies, application API and library API written for run $RunId"
exit 0

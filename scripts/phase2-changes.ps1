<#
.SYNOPSIS
  Stage 2 of UpgradeLens (changed files): inventories what change_branch changed relative to the base
  branch, as a three-dot diff (base...head).

.DESCRIPTION
  Uses only git, on the source repo. The left side of every diff is the merge base of the two branches
  (recorded by stage 0), so commits made on the base branch after the branch point are not reported as
  changes. Steps, in order:
    1. select-run    the given run, or the newest run whose stage 1 finished (passed or failed: the inventory
                     is useful context for a failed build too)
    2. diff          name-status (renames detected), whitespace-insensitive numstat, and the full patch with
                     no context lines, all between <merge base> and <head commit>
    3. commits       the commits on change_branch since the merge base, and which files each one touched
    4. classify      each file: main-code, test, build-dependency, config, other, or formatting-only
    5. test-check    per test file: weakened assertions, removed tests, added disabled tests
    6. code-check    per Java file: each diff hunk line is mapped to the method or type it belongs to
                     (lib\core.ps1 Get-SymbolForLine: any access level, generic and multi-line declarations,
                     brace-aware), with signature, annotation and field-default changes, and removed
                     if/else/switch/case/catch lines
    7. write         artifacts\change-inventory.json and reports\change-inventory.md

  Findings are recorded, not failed. Stage 2 passes when the inventory is written. The verdict stage
  decides whether a finding blocks a release.

  Exit codes:
    0  inventory written
    2  usage or run problem (no run, stage 1 not run, missing config snapshot or environment record)
    3  git failure

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 1 finished.

.PARAMETER Config
  Config file (for the runs folder). Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.EXAMPLE
  .\scripts\phase2-changes.ps1 -RunId 20261126-123622
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
$script:GitCmd = "git"
$script:RepoDir = ""
$contentCache = @{}

# ---------- helpers ----------

function Invoke-GitRepo([string[]]$Arguments) {
    Invoke-Native $script:GitCmd (@("-C", $script:RepoDir, "-c", "core.quotepath=off") + $Arguments)
}

function Get-FileLines([string]$Commit, [string]$Path) {
    # Content of one file at one commit, as lines. Cached, because several changes can share a file.
    if ([string]::IsNullOrEmpty($Path)) { return $null }
    $key = $Commit + ":" + $Path
    if (-not $contentCache.ContainsKey($key)) {
        $r = Invoke-GitRepo @("show", $key)
        $contentCache[$key] = $(if ($r.ExitCode -eq 0) { [string[]]$r.Output } else { $null })
    }
    $value = $contentCache[$key]
    if ($null -eq $value) { return $null }
    return ,$value
}

function Read-Patch($Lines) {
    # Parses "git diff -U0" output into per-file entries: Path, OldPath, Added and Removed lines,
    # each with its line number on its own side.
    $files = @{}
    $entry = $null
    $pendingOld = ""
    $inHunk = $false
    $oldLn = 0
    $newLn = 0
    foreach ($raw in $Lines) {
        $l = ([string]$raw).TrimEnd("`r")
        if ($l.StartsWith("diff --git ")) { $entry = $null; $inHunk = $false; $pendingOld = ""; continue }
        if ($inHunk -and $entry) {
            if ($l.StartsWith("+")) { $entry.Added.Add([pscustomobject]@{ Line = $newLn; Text = $l.Substring(1) }); $newLn++; continue }
            if ($l.StartsWith("-")) { $entry.Removed.Add([pscustomobject]@{ Line = $oldLn; Text = $l.Substring(1) }); $oldLn++; continue }
            if ($l.StartsWith("\")) { continue }
        }
        if ($l -match '^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@') {
            $oldLn = [int]$Matches[1]
            $newLn = [int]$Matches[2]
            $inHunk = $true
            continue
        }
        if ($l -match '^--- (a/(.+)|/dev/null)$') {
            $pendingOld = $Matches[2]
            continue
        }
        if ($l -match '^\+\+\+ (b/(.+)|/dev/null)$') {
            $key = if ($Matches[2]) { $Matches[2] } else { $pendingOld }
            $entry = [pscustomobject]@{
                Path    = $key
                OldPath = $pendingOld
                Added   = (New-Object System.Collections.Generic.List[object])
                Removed = (New-Object System.Collections.Generic.List[object])
            }
            $files[$key] = $entry
            $inHunk = $false
            continue
        }
    }
    return $files
}

function Get-Category([string]$Path) {
    $p = $Path -replace '\\', '/'
    $leaf = ($p -split '/')[-1]
    if ($p -match '(^|/)src/test/' -or $leaf -match '(Test|IT|Tests|ITCase)\.java$') { return "test" }
    $buildLeaves = @(
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "gradle.properties", "libs.versions.toml", "mvnw", "mvnw.cmd", "gradlew", "gradlew.bat",
        "package.json", "package-lock.json", "requirements.txt", "pyproject.toml", "Pipfile", "Pipfile.lock"
    )
    if (($buildLeaves -contains $leaf) -or ($p -match '(^|/)\.mvn/') -or ($p -match '(^|/)gradle/')) { return "build-dependency" }
    if (($p -match '(^|/)src/main/resources/') -or ($leaf -match '\.(ya?ml|properties|xml|json|toml|env)$')) { return "config" }
    if (($p -match '(^|/)src/main/') -and ($leaf -match '\.(java|kt|groovy|scala)$')) { return "main-code" }
    return "other"
}

# ---------- step 1: select run ----------

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$readyStates = @("phase1-done", "failed-build", "phase2-done")
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States $readyStates
if (-not $RunId) { Write-Host "No run with stage 1 finished under $runsRoot. Run phase1-build.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$StatusFile = Join-Path $RunDir "status.json"
$RunConfigFile = Join-Path $RunDir "config\upgrade-lens.json"
$EnvFile = Join-Path $RunDir "artifacts\environment.json"
$ArtifactsDir = Join-Path $RunDir "artifacts"
$ReportsDir = Join-Path $RunDir "reports"
$LogsDir = Join-Path $RunDir "logs"

if (-not (Test-Path -LiteralPath $StatusFile) -or -not (Test-Path -LiteralPath $RunConfigFile) -or -not (Test-Path -LiteralPath $EnvFile)) {
    Write-Host "Run not found or incomplete: $RunDir (needs status.json, config\upgrade-lens.json and artifacts\environment.json)"
    exit 2
}
$script:StageLog = Join-Path $LogsDir "phase2.log"
$cfg = Read-JsonFile $RunConfigFile
$status = Read-JsonFile $StatusFile
$envRecord = Read-JsonFile $EnvFile

$buildFailed = ($status.state -eq "failed-build")
if ($readyStates -notcontains $status.state) {
    Write-Host "Run $RunId is not ready: state is '$($status.state)', stage 1 must have run first."
    exit 2
}

$script:GitCmd = $cfg.git.command
# The run's own clone (stage 0). Runs from before the clone-per-run change recorded the configured repo.
$script:RepoDir = if ($envRecord.repo) { $envRecord.repo } else { $cfg.repo }
$baseCommit = $envRecord.branches.base.commit
$headCommit = $envRecord.branches.head.commit
$mergeBase = $envRecord.branches.mergeBase
if (-not $baseCommit -or -not $headCommit) {
    Write-Host "Environment record has no base or head commit: $EnvFile"
    exit 2
}
if (-not $mergeBase) {
    # A run created before the merge base was recorded: compute it now.
    $mb = Invoke-GitRepo @("merge-base", $baseCommit, $headCommit)
    if ($mb.ExitCode -ne 0) { Write-Host "No merge base between $baseCommit and ${headCommit}: $($mb.Output -join ' ')"; exit 3 }
    $mergeBase = ($mb.Output | Select-Object -First 1).Trim()
}

$startedAt = (Get-Date).ToString("o")
Write-StageLog "phase 2 changed files for run $RunId ($($cfg.baseBranch)...$($cfg.headBranch): merge base $($mergeBase.Substring(0,10)) -> $($headCommit.Substring(0,10)))"

function Stop-Stage([int]$Code, [string]$Reason) {
    Write-StageLog $Reason
    Set-StageStep $status "changed-files" "failed" $Reason
    $status.phase = "phase2"
    $status.state = "failed-tool"
    Write-JsonFile $StatusFile $status
    exit $Code
}

# ---------- step 2: diff (three-dot: merge base to head) ----------

$ns = Invoke-GitRepo @("diff", "--name-status", "-M", $mergeBase, $headCommit)
if ($ns.ExitCode -ne 0) { Stop-Stage 3 "git diff --name-status failed: $($ns.Output -join ' ')" }

$patch = Invoke-GitRepo @("diff", "-U0", "-M", $mergeBase, $headCommit)
if ($patch.ExitCode -ne 0) { Stop-Stage 3 "git diff failed: $($patch.Output -join ' ')" }

$ws = Invoke-GitRepo @("diff", "--numstat", "-M", "-w", "--ignore-blank-lines", $mergeBase, $headCommit)
if ($ws.ExitCode -ne 0) { Stop-Stage 3 "git diff --numstat failed: $($ws.Output -join ' ')" }

$items = New-Object System.Collections.Generic.List[object]
foreach ($line in $ns.Output) {
    $parts = ([string]$line).Split("`t")
    if ($parts.Count -lt 2) { continue }
    $change = $parts[0].Substring(0, 1)
    if (($change -eq "R") -or ($change -eq "C")) {
        $oldPath = $parts[1]; $newPath = $parts[2]
    } else {
        $oldPath = $parts[1]; $newPath = $parts[1]
    }
    $items.Add([pscustomobject]@{ Change = $change; OldPath = $oldPath; Path = $newPath })
}
Write-StageLog "diff: $($items.Count) changed file(s)"

# Paths that still have a change when whitespace and blank lines are ignored.
$nonWs = @{}
foreach ($line in $ws.Output) {
    $parts = ([string]$line).Split("`t")
    if ($parts.Count -ne 3) { continue }
    if ($parts[2] -match '=>') { continue }
    if (($parts[0] -ne "0") -or ($parts[1] -ne "0")) { $nonWs[$parts[2]] = $true }
}

$patchFiles = Read-Patch $patch.Output

# ---------- step 3: commits since the merge base ----------

$commitList = New-Object System.Collections.Generic.List[object]
$commitsByFile = @{}
$log = Invoke-GitRepo @("log", "--no-merges", "--format=%x1e%H%x09%an%x09%s", "--name-only", "$mergeBase..$headCommit")
if ($log.ExitCode -ne 0) { Stop-Stage 3 "git log failed: $($log.Output -join ' ')" }
$cur = $null
foreach ($l in $log.Output) {
    $t = [string]$l
    if ($t.StartsWith([string][char]0x1e)) {
        $p = $t.Substring(1).Split("`t")
        $cur = [pscustomobject]@{ commit = $p[0]; short = $p[0].Substring(0, 10); author = $p[1]; subject = ($p[2..($p.Count - 1)] -join "`t"); files = (New-Object System.Collections.Generic.List[string]) }
        $commitList.Add($cur)
        continue
    }
    if ($cur -and $t.Trim()) {
        $cur.files.Add($t.Trim())
        if (-not $commitsByFile.ContainsKey($t.Trim())) { $commitsByFile[$t.Trim()] = New-Object System.Collections.Generic.List[object] }
        $commitsByFile[$t.Trim()].Add([pscustomobject]@{ commit = $cur.short; subject = $cur.subject })
    }
}
Write-StageLog "commits on $($cfg.headBranch) since the merge base: $($commitList.Count)"

# ---------- step 4 to 6: classify, check tests, check code ----------

$annRegex = '^\s*@\w+'
$fieldDefaultRegex = '^\s*(?:(?:private|protected|public|static|final|transient|volatile)\s+)+[^(=]*='
$branchRegex = '(\bif\s*\(|\belse\b|\bswitch\s*\(|\bcase\b|\bcatch\s*\()'
$assertRegex = '\bassert\w*\(|\bverify\(|\bandExpect\(|\bfail\(|\bexpect\w*\('
$testAnnRegex = '^\s*@(Test|ParameterizedTest|RepeatedTest|TestFactory)\b'
$disabledRegex = '^\s*@(Disabled|Ignore)\b'

$fileRecords = New-Object System.Collections.Generic.List[object]
$findingList = New-Object System.Collections.Generic.List[object]
$methodKeys = @{}

foreach ($it in $items) {
    $e = $patchFiles[$it.Path]
    if (-not $e) {
        $e = [pscustomobject]@{ Path = $it.Path; OldPath = $it.OldPath; Added = (New-Object System.Collections.Generic.List[object]); Removed = (New-Object System.Collections.Generic.List[object]) }
    }
    # .ToArray(): Windows PowerShell 5.1 cannot @()-wrap a generic List held in a property.
    $added = $e.Added.ToArray()
    $removed = $e.Removed.ToArray()

    $whitespaceOnly = ($it.Change -ne "R") -and ($it.Change -ne "A") -and ($it.Change -ne "D") -and (-not $nonWs.ContainsKey($it.Path))
    $category = if ($whitespaceOnly) { "formatting-only" } else { Get-Category $it.Path }

    $methods = New-Object System.Collections.Generic.List[string]
    $symbolChanges = [ordered]@{}
    $sigs = New-Object System.Collections.Generic.List[string]
    $anns = New-Object System.Collections.Generic.List[string]
    $defs = New-Object System.Collections.Generic.List[string]
    $branches = New-Object System.Collections.Generic.List[string]
    $fileFindings = New-Object System.Collections.Generic.List[object]

    function Add-SymbolChange([string]$Symbol, [string]$Kind, [string]$Side) {
        if (-not $Symbol) { $Symbol = "file" }
        if (-not $symbolChanges.Contains($Symbol)) {
            $symbolChanges[$Symbol] = [ordered]@{ symbol = $Symbol; added = 0; removed = 0; kinds = (New-Object System.Collections.Generic.List[string]) }
        }
        $sc = $symbolChanges[$Symbol]
        if ($Side -eq "+") { $sc.added++ } else { $sc.removed++ }
        if ($Kind -and -not $sc.kinds.Contains($Kind)) { $sc.kinds.Add($Kind) }
    }

    if ($it.Path -like "*.java") {
        $headLines = if ($added.Count -gt 0) { Get-FileLines $headCommit $it.Path } else { $null }
        $baseLines = if ($removed.Count -gt 0) { Get-FileLines $mergeBase $it.OldPath } else { $null }

        foreach ($pair in @(@($added | ForEach-Object { @{ side = "+"; item = $_; lines = $headLines } }) + @($removed | ForEach-Object { @{ side = "-"; item = $_; lines = $baseLines } }))) {
            $a = $pair.item
            $sym = Get-SymbolForLine -Lines $pair.lines -LineNo $a.Line
            $kind = "body"
            $declName = Get-MethodDeclarationName $a.Text
            if ($declName -or (Get-TypeDeclarationName $a.Text)) { $sigs.Add("$($pair.side) " + $a.Text.Trim()); $kind = "signature" }
            elseif ($a.Text -match $annRegex) { $anns.Add("$($pair.side) " + $a.Text.Trim()); $kind = "annotation" }
            elseif ($a.Text -match $fieldDefaultRegex -and $sym -like "type:*") { $defs.Add("$($pair.side) " + $a.Text.Trim()); $kind = "field-default" }
            elseif ($a.Text -match '^\s*import\s') { $kind = "import" }
            if ($pair.side -eq "-" -and $a.Text -match $branchRegex) { $branches.Add($a.Text.Trim()); $kind = "removed-branch" }
            Add-SymbolChange $sym $kind $pair.side
            if ($sym -like "method:*") {
                $m = $sym.Substring(7)
                if (-not $methods.Contains($m)) { $methods.Add($m) }
            }
        }
        if ($branches.Count -gt 0) {
            $shown = ($branches | Select-Object -First 3) -join " | "
            $fileFindings.Add([pscustomobject]@{ type = "removed-branch-or-catch"; detail = "$($branches.Count) removed line(s), e.g. $shown" })
        }
    }

    if ($category -eq "test") {
        $assertRemoved = @($removed | Where-Object { $_.Text -match $assertRegex }).Count
        $assertAdded = @($added | Where-Object { $_.Text -match $assertRegex }).Count
        if ($assertRemoved -gt $assertAdded) {
            $fileFindings.Add([pscustomobject]@{ type = "weakened-assertions"; detail = "removed $assertRemoved assertion line(s), added $assertAdded" })
        }
        $testRemoved = @($removed | Where-Object { $_.Text -match $testAnnRegex }).Count
        $testAdded = @($added | Where-Object { $_.Text -match $testAnnRegex }).Count
        if ($testRemoved -gt $testAdded) {
            $fileFindings.Add([pscustomobject]@{ type = "test-removed"; detail = "removed $testRemoved test annotation(s), added $testAdded" })
        }
        $disabledAdded = @($added | Where-Object { $_.Text -match $disabledRegex }).Count
        $disabledRemoved = @($removed | Where-Object { $_.Text -match $disabledRegex }).Count
        if ($disabledAdded -gt $disabledRemoved) {
            $fileFindings.Add([pscustomobject]@{ type = "test-disabled"; detail = "added $disabledAdded @Disabled or @Ignore, removed $disabledRemoved" })
        }
    }

    foreach ($m in $methods) { $methodKeys["$($it.Path)::$m"] = $true }
    foreach ($f in $fileFindings) {
        $findingList.Add([pscustomobject]@{ file = $it.Path; type = $f.type; detail = $f.detail })
    }

    $fileCommits = @()
    if ($commitsByFile.ContainsKey($it.Path)) { $fileCommits = $commitsByFile[$it.Path].ToArray() }
    elseif ($it.OldPath -and $commitsByFile.ContainsKey($it.OldPath)) { $fileCommits = $commitsByFile[$it.OldPath].ToArray() }

    $fileRecords.Add([pscustomobject]@{
        path              = $it.Path
        oldPath           = $it.OldPath
        change            = $it.Change
        category          = $category
        additions         = $added.Count
        deletions         = $removed.Count
        changedMethods    = @($methods)
        symbols           = @($symbolChanges.Values | ForEach-Object { [pscustomobject]@{ symbol = $_.symbol; added = $_.added; removed = $_.removed; kinds = $_.kinds.ToArray() } })
        signatureChanges  = @($sigs)
        annotationChanges = @($anns)
        defaultChanges    = @($defs)
        removedBranches   = @($branches)
        commits           = $fileCommits
    })
    Write-StageLog "  $($it.Change) $($it.Path) [$category] methods: $(@($methods) -join ', ')"
}

# ---------- step 7: write ----------

$byCategory = [ordered]@{}
foreach ($c in @("main-code", "test", "build-dependency", "config", "other", "formatting-only")) {
    $byCategory[$c] = @($fileRecords | Where-Object { $_.category -eq $c }).Count
}

$inventory = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    diff          = [ordered]@{ mode = "three-dot"; expression = "$($cfg.baseBranch)...$($cfg.headBranch)"; left = $mergeBase; right = $headCommit }
    base          = [ordered]@{ name = $cfg.baseBranch; commit = $baseCommit; mergeBase = $mergeBase }
    head          = [ordered]@{ name = $cfg.headBranch; commit = $headCommit }
    buildFailed   = $buildFailed
    summary       = [ordered]@{
        files          = $fileRecords.Count
        byCategory     = $byCategory
        changedMethods = $methodKeys.Count
        findings       = $findingList.Count
        commits        = $commitList.Count
    }
    commits  = @($commitList | ForEach-Object { [pscustomobject]@{ commit = $_.commit; author = $_.author; subject = $_.subject; files = $_.files.ToArray() } })
    files    = $fileRecords.ToArray()
    findings = $findingList.ToArray()
}

New-Item -ItemType Directory -Force -Path $ArtifactsDir, $ReportsDir | Out-Null
$jsonPath = Join-Path $ArtifactsDir "change-inventory.json"
$mdPath = Join-Path $ReportsDir "change-inventory.md"
Write-JsonFile $jsonPath $inventory

$md = New-Object System.Collections.Generic.List[string]
$md.Add("# Change inventory: run $RunId")
$md.Add("")
$md.Add("- Diff: ``$($cfg.baseBranch)...$($cfg.headBranch)`` (three-dot), merge base ``$($mergeBase.Substring(0,10))`` to ``$($headCommit.Substring(0,10))``")
$md.Add("- Files changed: $($fileRecords.Count). Changed methods: $($methodKeys.Count). Commits: $($commitList.Count). Findings: $($findingList.Count).")
$md.Add("")
$md.Add("## Files by category")
$md.Add("")
foreach ($c in $byCategory.Keys) { $md.Add("- ${c}: $($byCategory[$c])") }
$md.Add("")
$md.Add("## Findings")
$md.Add("")
if ($findingList.Count -eq 0) { $md.Add("None.") }
foreach ($f in $findingList) { $md.Add("- **$($f.type)** ``$($f.file)``: $($f.detail)") }
$md.Add("")
$md.Add("## Files")
$md.Add("")
$md.Add("| File | Change | Category | +/- | Changed methods | Commits |")
$md.Add("|---|---|---|---|---|---|")
foreach ($r in $fileRecords) {
    $methodText = if (@($r.changedMethods).Count -gt 0) { (@($r.changedMethods) -join ", ") } else { "-" }
    $commitText = if (@($r.commits).Count -gt 0) { (@($r.commits | ForEach-Object { "$($_.commit) $($_.subject)" }) -join "; ") -replace '\|', '\|' } else { "-" }
    $md.Add("| ``$($r.path)`` | $($r.change) | $($r.category) | +$($r.additions) / -$($r.deletions) | $methodText | $commitText |")
}
$md.Add("")
[System.IO.File]::WriteAllText($mdPath, ($md -join "`r`n") + "`r`n", $script:Utf8NoBom)

# ---------- done ----------

Set-StageStep $status "changed-files" "done" "" $jsonPath
$status.phase = "phase2"
# After a failed build the inventory is context for the failure report; the run stays failed.
if (-not $buildFailed) { $status.state = "phase2-done" }
Write-JsonFile $StatusFile $status

Write-StageLog "changed files: $($fileRecords.Count), changed methods: $($methodKeys.Count), findings: $($findingList.Count)"
Write-StageLog "inventory: $jsonPath"
Write-StageLog "phase 2 done: changed files inventoried for run $RunId"
exit 0

<#
.SYNOPSIS
  Phase 0 of UpgradeLens: validates the config and prerequisites, then creates the run folder, clones the
  repository into it, checks out each branch in its own worktree, and writes the environment record.

.DESCRIPTION
  Steps, in order:
    1. validate-config   schema version, required keys, distinct branches, optional keys and their types
    2. prereq-check      git and Maven on PATH, the repository URL answers and has both branches
                         (git ls-remote), a JDK per side with java, javap and jdeps, Maven settings file (if
                         configured), rules folder and probe manifest
    3. run-folder        runs\<id>\ with config\, artifacts\, reports\, logs\, worktrees\, and a snapshot of the
                         config, the rules and the probes used by this run
    4. clone             runs\<id>\repo.git: a bare repository with the two branches only (full history, no tags),
                         fetched from repoUrl. The run owns it; no other checkout is used or changed.
    5. worktrees         one worktree per branch, each in its own directory (so each build has its own target\):
                           worktrees\<base branch>  detached at the base commit (read only)
                           worktrees\<head branch>  on the local branch <head branch>, tracking origin, so a
                                                    developer can review the new tests stage 8 places there,
                                                    commit them and push
    6. merge-base        the merge base of the two branches: every diff of the run is three-dot
                         (base...head), so commits made on the base branch after the branch point are not
                         reported as changes of the head branch
    7. environment       artifacts\environment.json: tool, OS, git, Maven and JDK versions, commits, merge base,
                         config hash

  Config and prerequisite failures stop the run with exit 2 and a clear message.
  Tool failures (git, Maven) stop the run with exit 3.
  Every step is written to runs\<id>\status.json as it runs.

  Nothing outside the run folder is written. The automation never commits or pushes; only a developer does,
  from the head worktree. scripts\cleanup-run.ps1 removes a run's clone and worktrees (it refuses while the
  head worktree has uncommitted or unpushed work, unless -Force).

.PARAMETER Config
  Path to the config file. Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.PARAMETER CheckOnly
  Validate the config and prerequisites only. Creates no run folder and no worktrees.

.EXAMPLE
  .\scripts\phase0-setup.ps1
  .\scripts\phase0-setup.ps1 -CheckOnly
#>
param(
    [string]$Config,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ToolVersion = "1.0.0"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
$Config = Resolve-ConfigPath -Config $Config -RootDir $RootDir

# ---------- helpers ----------

function Get-Setting([string]$Path) { return (Get-ConfigValue -Cfg $script:cfg -Path $Path) }

function Test-NonEmpty($Value) {
    return ($null -ne $Value) -and ("$Value".Trim().Length -gt 0)
}

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $checks.Add([pscustomobject]@{
        Check  = $Name
        Result = $(if ($Ok) { "PASS" } else { "FAIL" })
        Detail = $Detail
    })
}

function Show-Checks {
    $checks | Format-Table -AutoSize | Out-String -Width 220 | Write-Host
}

# ---------- step 1: config ----------

if (-not (Test-Path -LiteralPath $Config)) {
    Write-Host "Config not found: $Config"
    Write-Host "Create config\upgrade-lens.json with the repo, branch, JDK and Maven settings paths."
    exit 2
}

try {
    $script:cfg = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Host "Config is not valid JSON: $Config"
    Write-Host $_.Exception.Message
    exit 2
}

$configOk = $true
if ($script:cfg.schemaVersion -ne 1) {
    Add-Check -Name "config schemaVersion" -Ok $false -Detail "must be 1, found '$($script:cfg.schemaVersion)'"
    $configOk = $false
} else {
    Add-Check -Name "config schemaVersion" -Ok $true -Detail "1"
}

# repoUrl: any URL or path git can clone (githubUrl is the older name of the same key).
$repoUrl = [string](Get-ConfigValue $script:cfg "repoUrl" (Get-ConfigValue $script:cfg "githubUrl" ""))
$ok = Test-NonEmpty $repoUrl
Add-Check -Name "config repoUrl" -Ok $ok -Detail $(if ($ok) { $repoUrl } else { "missing or empty in $Config (the repository URL to clone)" })
if (-not $ok) { $configOk = $false }

$requiredKeys = @(
    "baseBranch", "headBranch", "runsFolder",
    "git.command", "maven.command", "java.base", "java.head"
)
foreach ($key in $requiredKeys) {
    $value = Get-Setting $key
    $ok = Test-NonEmpty $value
    Add-Check -Name "config $key" -Ok $ok -Detail $(if ($ok) { "set" } else { "missing or empty in $Config" })
    if (-not $ok) { $configOk = $false }
}

if ((Test-NonEmpty $script:cfg.baseBranch) -and ($script:cfg.baseBranch -eq $script:cfg.headBranch)) {
    Add-Check -Name "config branches" -Ok $false -Detail "baseBranch and headBranch must differ"
    $configOk = $false
}

# Optional keys: checked only for their type and allowed values, so a typo does not silently fall back.
$itMode = [string](Get-ConfigValue $script:cfg "build.integrationTests" "auto")
if (@("auto", "failsafe-only") -notcontains $itMode) {
    Add-Check -Name "config build.integrationTests" -Ok $false -Detail "must be auto or failsafe-only; found '$itMode'"
    $configOk = $false
}
foreach ($t in @("timeouts.buildMinutes", "timeouts.startupSeconds", "agentBudget", "library.maxMembersPerClass",
                 "orchestration.maxAgentAttempts", "orchestration.maxTests")) {
    $v = Get-ConfigValue $script:cfg $t $null
    if ($null -ne $v -and -not ("$v" -match '^\d+$' -and [int]"$v" -gt 0)) {
        Add-Check -Name "config $t" -Ok $false -Detail "must be a positive whole number; found '$v'"
        $configOk = $false
    }
}
$rr = Get-ConfigValue $script:cfg "orchestration.maxTestRepairRounds" $null
if ($null -ne $rr -and -not ("$rr" -match '^\d+$' -and [int]"$rr" -le 3)) {
    Add-Check -Name "config orchestration.maxTestRepairRounds" -Ok $false -Detail "must be 0 to 3; found '$rr'"
    $configOk = $false
}
$goals = [string](Get-ConfigValue $script:cfg "maven.goals" "clean verify")
if ($goals -notmatch '\bverify\b|\binstall\b|\bpackage\b') {
    Add-Check -Name "config maven.goals" -Ok $false -Detail "must run the package phase or later (later stages need the jar and the coverage report); found '$goals'"
    $configOk = $false
}

if (-not $configOk) {
    Show-Checks
    Write-Host "The config is incomplete. Fix the FAIL lines in $Config, then run again."
    exit 2
}

$rulesDir = Resolve-ProjectPath -Path ([string](Get-ConfigValue $script:cfg "rulesDir" "config\rules")) -RootDir $RootDir
$probesFile = Resolve-ProjectPath -Path ([string](Get-ConfigValue $script:cfg "probesFile" "config\probes.json")) -RootDir $RootDir

# ---------- prerequisites (shared by -CheckOnly and the full run) ----------

function Test-Prerequisites {
    $gitCmd = Get-Command $script:cfg.git.command -ErrorAction SilentlyContinue
    Add-Check -Name "git" -Ok ($null -ne $gitCmd) -Detail $(if ($gitCmd) { $gitCmd.Source } else { "not on PATH" })

    $mvnCmd = Get-Command $script:cfg.maven.command -ErrorAction SilentlyContinue
    Add-Check -Name "maven" -Ok ($null -ne $mvnCmd) -Detail $(if ($mvnCmd) { $mvnCmd.Source } else { "not on PATH" })

    # The repository is cloned into each run. Here: the URL answers, and both branches exist on it.
    if ($gitCmd) {
        $ls = Invoke-Native $script:cfg.git.command @("ls-remote", "--heads", $repoUrl)
        Add-Check -Name "repo" -Ok ($ls.ExitCode -eq 0) -Detail $(if ($ls.ExitCode -eq 0) { "reachable: $repoUrl" } else { "git ls-remote failed (URL, network or credentials): $(@($ls.Output | Select-Object -First 2) -join ' ')" })
        if ($ls.ExitCode -eq 0) {
            foreach ($b in @($script:cfg.baseBranch, $script:cfg.headBranch)) {
                $line = @($ls.Output | Where-Object { $_ -match ("\srefs/heads/" + [regex]::Escape($b) + "$") }) | Select-Object -First 1
                Add-Check -Name "branch $b" -Ok ([bool]$line) -Detail $(if ($line) { ($line -split '\s+')[0] } else { "no branch $b on $repoUrl" })
            }
        }
    }

    foreach ($side in @("base", "head")) {
        $javaHome = $script:cfg.java.$side
        foreach ($tool in @("java", "javap", "jdeps")) {
            $exe = if (Test-NonEmpty $javaHome) { Join-Path $javaHome "bin\$tool.exe" } else { "" }
            $ok = (Test-NonEmpty $exe) -and (Test-Path -LiteralPath $exe)
            Add-Check -Name "$tool $side" -Ok $ok -Detail $(if ($ok) { $exe } else { "java.$side must be a JDK folder with bin\$tool.exe" })
        }
    }

    if (Test-NonEmpty $script:cfg.maven.settingsFile) {
        $settingsOk = Test-Path -LiteralPath $script:cfg.maven.settingsFile
        Add-Check -Name "maven settings" -Ok $settingsOk -Detail $(if ($settingsOk) { $script:cfg.maven.settingsFile } else { "not found: $($script:cfg.maven.settingsFile)" })
    }

    $ruleCount = if (Test-Path -LiteralPath $rulesDir) { @(Get-ChildItem -LiteralPath $rulesDir -Filter *.json -File).Count } else { 0 }
    Add-Check -Name "rules" -Ok ($ruleCount -gt 0) -Detail $(if ($ruleCount -gt 0) { "$ruleCount file(s) in $rulesDir" } else { "no *.json rule file in $rulesDir" })
    $probesOk = Test-Path -LiteralPath $probesFile
    Add-Check -Name "probes" -Ok $probesOk -Detail $(if ($probesOk) { $probesFile } else { "not found: $probesFile" })
}

if ($CheckOnly) {
    Test-Prerequisites
    Show-Checks
    if ($checks | Where-Object { $_.Result -eq "FAIL" }) {
        Write-Host "Some checks failed. Fix them, then run this again."
        exit 2
    }
    Write-Host "All checks passed."
    exit 0
}

# ---------- run folder ----------

$runsRoot = Get-RunsRoot -Cfg $script:cfg -RootDir $RootDir
$runId = Get-Date -Format "yyyyMMdd-HHmmss"
$RunDir = Join-Path $runsRoot $runId
while (Test-Path -LiteralPath $RunDir) {
    Start-Sleep -Milliseconds 1100
    $runId = Get-Date -Format "yyyyMMdd-HHmmss"
    $RunDir = Join-Path $runsRoot $runId
}
$LogFile = Join-Path $RunDir "logs\phase0.log"
$StatusFile = Join-Path $RunDir "status.json"
$EnvFile = Join-Path $RunDir "artifacts\environment.json"

$status = [ordered]@{
    runId       = $runId
    toolVersion = $ToolVersion
    phase       = "phase0"
    state       = "running"
    steps       = New-Object System.Collections.Generic.List[object]
}

function Save-Status { Write-JsonFile $StatusFile $status }

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line
    [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, $script:Utf8NoBom)
}

function Invoke-Step([string]$Name, [scriptblock]$Action) {
    $step = [ordered]@{
        name       = $Name
        state      = "running"
        reason     = ""
        startedAt  = (Get-Date).ToString("o")
        finishedAt = $null
    }
    $status.steps.Add($step)
    Save-Status
    Write-Log "step ${Name}: running"
    try {
        & $Action
        $step.state = "done"
    } catch {
        $step.state = "failed"
        $step.reason = $_.Exception.Message
    }
    $step.finishedAt = (Get-Date).ToString("o")
    Save-Status
    Write-Log "step ${Name}: $($step.state)$(if ($step.reason) { " - $($step.reason)" })"
    return ($step.state -eq "done")
}

function Stop-Run([int]$Code, [string]$Message) {
    $status.state = $(if ($Code -eq 2) { "failed-prereq" } else { "failed-tool" })
    Save-Status
    Write-Log $Message
    Write-Log "run folder: $RunDir"
    exit $Code
}

New-Item -ItemType Directory -Force -Path (Join-Path $RunDir "config"), (Join-Path $RunDir "artifacts"),
    (Join-Path $RunDir "reports"), (Join-Path $RunDir "logs"), (Join-Path $RunDir "worktrees") | Out-Null
Save-Status
Write-Log "run $runId started (tool $ToolVersion, config $Config)"

# Snapshot of the config, rules and probes used for this run. The config hash goes into the environment record.
Copy-Item -LiteralPath $Config -Destination (Join-Path $RunDir "config\upgrade-lens.json")
$configHash = Get-FileSha256 $Config

# ---------- step 2: prerequisites ----------

$ok = Invoke-Step "prereq-check" {
    Test-Prerequisites
    Show-Checks
    $failed = @($checks | Where-Object { $_.Result -eq "FAIL" })
    if ($failed.Count -gt 0) { throw "$($failed.Count) prerequisite check(s) failed" }
}
if (-not $ok) {
    Stop-Run 2 "Prerequisites failed. Fix the FAIL lines above, then run again."
}

$runRules = Join-Path $RunDir "config\rules"
New-Item -ItemType Directory -Force -Path $runRules | Out-Null
Get-ChildItem -LiteralPath $rulesDir -Filter *.json -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $runRules $_.Name) }
Copy-Item -LiteralPath $probesFile -Destination (Join-Path $RunDir "config\probes.json")

# ---------- step 3: clone the two branches into the run ----------

# The run owns its repository: a bare clone with the two branches only. No other checkout is read or changed.
$RepoDir = Join-Path $RunDir "repo.git"
$git = $script:cfg.git.command
$ok = Invoke-Step "clone" {
    $base = $script:cfg.baseBranch
    $head = $script:cfg.headBranch
    $r = Invoke-Native $git @("init", "--bare", "--quiet", $RepoDir)
    if ($r.ExitCode -ne 0) { throw "git init failed: $($r.Output -join ' ')" }
    # "remote add" sets the standard fetch refspec, so a developer can later fetch, pull and push as usual.
    $r = Invoke-Native $git @("-C", $RepoDir, "remote", "add", "origin", $repoUrl)
    if ($r.ExitCode -ne 0) { throw "git remote add failed: $($r.Output -join ' ')" }
    $r = Invoke-Native $git @("-C", $RepoDir, "fetch", "--no-tags", "origin", "+refs/heads/$($base):refs/remotes/origin/$base", "+refs/heads/$($head):refs/remotes/origin/$head")
    if ($r.ExitCode -ne 0) { throw "git fetch from $repoUrl failed: $($r.Output -join ' ')" }
    Write-Log "cloned $base and $head from $repoUrl into $RepoDir"
}
if (-not $ok) {
    Stop-Run 3 "Clone failed. See the step reason in status.json."
}

# ---------- step 4: worktrees, one directory per branch ----------

$commits = @{}
$worktrees = @{}
$ok = Invoke-Step "worktrees" {
    $dirNames = @{}
    foreach ($side in @("base", "head")) {
        $branch = if ($side -eq "base") { $script:cfg.baseBranch } else { $script:cfg.headBranch }
        $dirNames[$side] = $branch -replace '[\\/:*?"<>|]', '_'
    }
    # Two branch names can map to one folder name ("a/b" and "a_b"). Keep the two checkouts apart.
    if ($dirNames["base"] -eq $dirNames["head"]) { $dirNames["base"] += "-base"; $dirNames["head"] += "-head" }

    foreach ($side in @("base", "head")) {
        $branch = if ($side -eq "base") { $script:cfg.baseBranch } else { $script:cfg.headBranch }
        $rev = Invoke-Native $git @("-C", $RepoDir, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/$branch^{commit}")
        if ($rev.ExitCode -ne 0) { throw "origin/$branch is missing after the fetch" }
        $commits[$side] = ($rev.Output | Select-Object -First 1).Trim()

        $path = Join-Path $RunDir "worktrees\$($dirNames[$side])"
        if (Test-Path -LiteralPath $path) { throw "worktree folder already exists: $path" }
        if ($side -eq "base") {
            # The baseline is only read: detached at its commit.
            $add = Invoke-Native $git @("-C", $RepoDir, "worktree", "add", "--detach", $path, $commits[$side])
        } else {
            # The change side is a real branch that tracks origin, so the developer can commit the new tests
            # stage 8 places here and push them ("git push" from this folder).
            $br = Invoke-Native $git @("-C", $RepoDir, "branch", "--track", $branch, "refs/remotes/origin/$branch")
            if ($br.ExitCode -ne 0) { throw "git branch $branch failed: $($br.Output -join ' ')" }
            $add = Invoke-Native $git @("-C", $RepoDir, "worktree", "add", $path, $branch)
        }
        if ($add.ExitCode -ne 0) { throw "git worktree add failed for ${side}: $($add.Output -join ' ')" }
        $worktrees[$side] = $path
        Write-Log "worktree ${side}: $branch at $($commits[$side].Substring(0,10)) -> $path$(if ($side -eq 'head') { " (branch $branch, tracking origin/$branch)" } else { ' (detached)' })"
    }
}
if (-not $ok) {
    Stop-Run 3 "Worktree setup failed. See the step reason in status.json."
}
# ---------- step 5: merge base (three-dot diff) ----------

$mergeBase = $null
$ok = Invoke-Step "merge-base" {
    $mb = Invoke-Native $git @("-C", $RepoDir, "merge-base", $commits["base"], $commits["head"])
    if ($mb.ExitCode -ne 0 -or -not $mb.Output) { throw "the branches have no common ancestor, so a three-dot diff is not possible: $($mb.Output -join ' ')" }
    $script:mergeBase = ($mb.Output | Select-Object -First 1).Trim()
    $ahead = Invoke-Native $git @("-C", $RepoDir, "rev-list", "--count", "$($script:mergeBase)..$($commits['base'])")
    Write-Log "merge base: $($script:mergeBase.Substring(0,10)); base branch is $(($ahead.Output | Select-Object -First 1)) commit(s) past it (not part of the diff)"
}
if (-not $ok) {
    Stop-Run 3 "Merge base failed. See the step reason in status.json."
}
$mergeBase = $script:mergeBase

# ---------- step 6: environment record ----------

$ok = Invoke-Step "environment" {
    $javaVersions = @{}
    $mavenVersions = @{}
    foreach ($side in @("base", "head")) {
        $javaExe = Join-Path $script:cfg.java.$side "bin\java.exe"
        $javaOut = Invoke-Native $javaExe @("-version")
        $javaVersions[$side] = ($javaOut.Output | Select-Object -First 1)

        # Maven runs with the JDK of each side, so the report shows what each side builds with.
        $mvnOut = Invoke-WithJdk -JdkHome $script:cfg.java.$side -Action { Invoke-Native $script:cfg.maven.command @("-v") }
        if ($mvnOut.ExitCode -ne 0) { throw "mvn -v failed with JDK $side" }
        $mavenVersions[$side] = [ordered]@{
            maven = ($mvnOut.Output | Where-Object { $_ -match '^Apache Maven' } | Select-Object -First 1)
            java  = ($mvnOut.Output | Where-Object { $_ -match '^Java version:' } | Select-Object -First 1)
        }
    }

    $gitOut = Invoke-Native $script:cfg.git.command @("--version")

    $record = [ordered]@{
        runId        = $runId
        createdAt    = (Get-Date).ToString("o")
        toolVersion  = $ToolVersion
        configPath   = $Config
        configSha256 = $configHash
        os           = [System.Environment]::OSVersion.VersionString
        powershell   = $PSVersionTable.PSVersion.ToString()
        git          = ($gitOut.Output | Select-Object -First 1)
        repo         = $RepoDir
        repoUrl      = $repoUrl
        diff         = [ordered]@{ mode = "three-dot"; left = $mergeBase; right = $commits["head"]; expression = "$($script:cfg.baseBranch)...$($script:cfg.headBranch)" }
        maven        = [ordered]@{ settingsFile = [string]$script:cfg.maven.settingsFile; goals = $goals }
        branches     = [ordered]@{
            base = [ordered]@{ name = $script:cfg.baseBranch; commit = $commits["base"]; worktree = $worktrees["base"]; jdk = $script:cfg.java.base; java = $javaVersions["base"]; maven = $mavenVersions["base"] }
            head = [ordered]@{ name = $script:cfg.headBranch; commit = $commits["head"]; worktree = $worktrees["head"]; jdk = $script:cfg.java.head; java = $javaVersions["head"]; maven = $mavenVersions["head"] }
            mergeBase = $mergeBase
        }
    }
    Write-JsonFile $EnvFile $record
    Write-Log "environment record: $EnvFile"
}
if (-not $ok) {
    Stop-Run 3 "Environment record failed. See the step reason in status.json."
}

# ---------- done ----------

$status.state = "done"
Save-Status
Write-Log "phase 0 done. run id: $runId"
Write-Log "run folder: $RunDir"
exit 0

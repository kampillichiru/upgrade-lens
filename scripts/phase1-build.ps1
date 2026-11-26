<#
.SYNOPSIS
  Phase 1 of UpgradeLens (build and test): runs Maven on both branches in parallel, in the background,
  each with its own JDK, its own worktree (so its own target\), the configured settings.xml, and its own log.

.DESCRIPTION
  Reads the run's config snapshot (runs\<id>\config\upgrade-lens.json), so the run uses the config it
  was started with. Uses the worktrees that Phase 0 recorded in artifacts\environment.json.

  Steps, in order:
    1. select-run     the given run, or the newest run whose Phase 0 is done
    2. check          worktrees, pom.xml, JDK per side, Maven and settings file
    3. build          both sides start as background processes at the same time:
                        mvn -B -ntp -s <settings> -f <worktree>\pom.xml <maven.goals> <maven.extraArgs>
                      logged to logs\build-<branch>.log. The script waits, prints the last log line of each
                      side every 15 s, and stops a side that runs past timeouts.buildMinutes.
    4. results        artifacts\build-results.json (per side: exit code, BUILD line, test totals, how the
                      integration tests ran) and artifacts\test-results.json (every test case of both builds,
                      compared one by one: same, new, removed, now-failing, now-passing, now-skipped).

  Integration tests (config build.integrationTests):
    auto           (default) when a side has *IT classes and its pom binds no maven-failsafe-plugin, they are
                   run through surefire (-Dtest with the default patterns plus *IT and *ITCase), so their
                   result and coverage are part of the build. With failsafe, the pom's own setup runs them.
    failsafe-only  never add them; a side without failsafe reports them as not run.

  Each side runs with its own JAVA_HOME and PATH: java.base for the base branch, java.head for the head branch.
  A failure on either side is recorded (status.json, build-results.json) and returned as the exit code.

  Exit codes:
    0  both sides built and tested successfully
    2  usage or config problem (no run found, worktree or JDK missing); nothing was started
    3  tool failure (Maven could not start, or a side timed out and was stopped)
    4  a side failed to build or its tests failed; build-results.json and the log say which and why

.PARAMETER RunId
  Run folder name under runs\. Defaults to the newest run with Phase 0 done.

.PARAMETER TimeoutMinutes
  Stop a side that runs longer than this. Default: timeouts.buildMinutes from the config, else 60.

.PARAMETER Config
  Config file (for the runs folder). Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.EXAMPLE
  .\scripts\phase1-build.ps1
  .\scripts\phase1-build.ps1 -RunId 20261126-121722
#>
param(
    [string]$RunId,
    [int]$TimeoutMinutes = 0,
    [string]$Config
)

$ErrorActionPreference = "Stop"
$ToolVersion = "1.0.0"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
. (Join-Path $PSScriptRoot "lib\analysis.ps1")

function Get-LastLogLine([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return "(no output yet)" }
    try {
        $line = Get-Content -LiteralPath $Path -Tail 1 -ErrorAction Stop
        if ($line) { return ($line -replace '\s+', ' ').Trim() }
        return "(no output yet)"
    } catch {
        return "(log busy)"
    }
}

function Stop-ProcessTree([int]$ProcessId) {
    # cmd.exe runs mvn as a child, so stop the whole tree.
    & taskkill.exe /PID $ProcessId /T /F *> $null
}

function Test-FailsafeBound([string]$PomText) {
    # Failsafe declared in <build><plugins>. A declaration only inside <pluginManagement> does not run.
    $t = [regex]::Replace($PomText, '(?s)<pluginManagement>.*?</pluginManagement>', '')
    $t = [regex]::Replace($t, '(?s)<!--.*?-->', '')
    return ($t -match '<artifactId>\s*maven-failsafe-plugin\s*</artifactId>')
}

# ---------- step 1: select run ----------

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("done", "phase1-done", "failed-build")
if (-not $RunId) { Write-Host "No run with Phase 0 done under $runsRoot. Run phase0-setup.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$StatusFile = Join-Path $RunDir "status.json"
$RunConfigFile = Join-Path $RunDir "config\upgrade-lens.json"
$ArtifactsDir = Join-Path $RunDir "artifacts"
$LogsDir = Join-Path $RunDir "logs"
$script:StageLog = Join-Path $LogsDir "phase1.log"

$cfg = Read-JsonFile $RunConfigFile
$status = Read-JsonFile $StatusFile
$envRecord = Read-JsonFile (Join-Path $ArtifactsDir "environment.json")
if (-not $cfg -or -not $status -or -not $envRecord) {
    Write-Host "Run not found or incomplete: $RunDir (needs status.json, config\upgrade-lens.json and artifacts\environment.json)"
    exit 2
}
if ($TimeoutMinutes -le 0) { $TimeoutMinutes = [int](Get-ConfigValue $cfg "timeouts.buildMinutes" 60) }
$goals = @(([string](Get-ConfigValue $cfg "maven.goals" "clean verify")).Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries))
$extraArgs = @(Get-ConfigValue $cfg "maven.extraArgs" @() | ForEach-Object { [string]$_ } | Where-Object { $_ })
$itConfig = [string](Get-ConfigValue $cfg "build.integrationTests" "auto")

Write-StageLog "phase 1 build for run $RunId"

# ---------- step 2: check ----------

$sides = @(
    [pscustomobject]@{ Side = "base"; Branch = $cfg.baseBranch; Jdk = $cfg.java.base; Commit = $envRecord.branches.base.commit; WorkTree = $envRecord.branches.base.worktree },
    [pscustomobject]@{ Side = "head"; Branch = $cfg.headBranch; Jdk = $cfg.java.head; Commit = $envRecord.branches.head.commit; WorkTree = $envRecord.branches.head.worktree }
)
foreach ($s in $sides) {
    $dirName = Split-Path -Leaf $s.WorkTree
    $s | Add-Member -NotePropertyName LogFile -NotePropertyValue (Join-Path $LogsDir "build-$dirName.log") -Force
    $s | Add-Member -NotePropertyName StepName -NotePropertyValue "build-$dirName" -Force
}

$problems = @()
$mvnCmd = Get-Command $cfg.maven.command -ErrorAction SilentlyContinue
if (-not $mvnCmd) { $problems += "maven '$($cfg.maven.command)' not on PATH" }
$settings = [string]$cfg.maven.settingsFile
if ($settings -and -not (Test-Path -LiteralPath $settings)) { $problems += "Maven settings not found: $settings" }
foreach ($s in $sides) {
    if (-not $s.WorkTree -or -not (Test-Path -LiteralPath (Join-Path $s.WorkTree "pom.xml"))) { $problems += "$($s.Side): no pom.xml in $($s.WorkTree)" }
    if (-not (Test-Path -LiteralPath (Join-Path $s.Jdk "bin\java.exe"))) { $problems += "$($s.Side): no java.exe under $($s.Jdk)" }
}
if ($problems.Count -gt 0) {
    $problems | ForEach-Object { Write-StageLog "check failed: $_" }
    exit 2
}

# How each side runs its integration tests.
foreach ($s in $sides) {
    $itFiles = @(Get-ChildItem -LiteralPath (Join-Path $s.WorkTree "src\test") -Recurse -File -Filter *.java -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'IT(Case)?\.java$' })
    $pom = Get-Content -LiteralPath (Join-Path $s.WorkTree "pom.xml") -Raw
    $mode = if ($itFiles.Count -eq 0) { "none" }
            elseif (Test-FailsafeBound $pom) { "failsafe" }
            elseif ($itConfig -eq "failsafe-only") { "not-run" }
            else { "surefire" }
    $s | Add-Member -NotePropertyName ItMode -NotePropertyValue $mode -Force
    $s | Add-Member -NotePropertyName ItClasses -NotePropertyValue @($itFiles | ForEach-Object { [System.IO.Path]::GetFileNameWithoutExtension($_.Name) } | Sort-Object) -Force
    Write-StageLog ("{0}: {1} integration test class(es), mode {2}" -f $s.Side, $itFiles.Count, $mode)
}

# ---------- step 3: build both sides in the background ----------

function Format-CmdArg([string]$Value) {
    if ($Value -match '[\s"&|<>^]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

function Start-SideBuild($Side) {
    # Build the command line for cmd.exe. /s strips only the outer quotes, so the paths keep theirs.
    $mvnArgs = New-Object System.Collections.Generic.List[string]
    $mvnArgs.Add("-B"); $mvnArgs.Add("-ntp")
    if ($settings) { $mvnArgs.Add("-s"); $mvnArgs.Add($settings) }
    $mvnArgs.Add("-f"); $mvnArgs.Add((Join-Path $Side.WorkTree 'pom.xml'))
    if ($Side.ItMode -eq "surefire") {
        $mvnArgs.Add("-Dtest=Test*,*Test,*Tests,*TestCase,*IT,*ITCase")
        $mvnArgs.Add("-Dsurefire.failIfNoSpecifiedTests=false")
    }
    foreach ($a in $extraArgs) { $mvnArgs.Add($a) }
    foreach ($g in $goals) { $mvnArgs.Add($g) }
    $argText = ($mvnArgs | ForEach-Object { Format-CmdArg $_ }) -join ' '
    # ">>" keeps the header lines the script wrote before starting the build.
    $inner = "`"$($mvnCmd.Source)`" $argText >> `"$($Side.LogFile)`" 2>&1"
    $argLine = "/d /s /c `"$inner`""

    # Start-Process gives the child its own environment copy at launch, so set JAVA_HOME and PATH
    # only for this launch and restore them right after. The two sides therefore get different JDKs.
    $proc = Invoke-WithJdk -JdkHome $Side.Jdk -Action {
        Start-Process -FilePath "cmd.exe" -ArgumentList $argLine -WorkingDirectory $Side.WorkTree -WindowStyle Hidden -PassThru
    }
    return [pscustomobject]@{ Process = $proc; Command = "mvn $argText" }
}

$builds = @()
foreach ($s in $sides) {
    if (Test-Path -LiteralPath $s.LogFile) { Remove-Item -LiteralPath $s.LogFile -Force }
    $header = "# {0} phase 1 build: branch {1} at {2}`r`n# worktree {3}`r`n# JAVA_HOME {4}`r`n# settings {5}`r`n# integration tests: {6}`r`n" -f `
        (Get-Date -Format "o"), $s.Branch, $s.Commit, $s.WorkTree, $s.Jdk, $(if ($settings) { $settings } else { "(Maven default)" }), $s.ItMode
    [System.IO.File]::WriteAllText($s.LogFile, $header, $script:Utf8NoBom)
    try {
        $started = Start-SideBuild $s
    } catch {
        Write-StageLog "$($s.StepName): could not start Maven: $($_.Exception.Message)"
        foreach ($b in $builds) { if (-not $b.Process.HasExited) { Stop-ProcessTree $b.Process.Id } }
        Set-StageStep $status $s.StepName "failed" "could not start Maven: $($_.Exception.Message)"
        Set-RunState $status "failed-tool" "phase1"
        Write-JsonFile $StatusFile $status
        exit 3
    }
    [System.IO.File]::AppendAllText($s.LogFile, "# command: $($started.Command)`r`n", $script:Utf8NoBom)
    $builds += [pscustomobject]@{ Side = $s; Process = $started.Process; StartedAt = (Get-Date); TimedOut = $false; Command = $started.Command }
    Write-StageLog "$($s.StepName): started (pid $($started.Process.Id)), log: $($s.LogFile)"
}

# The child processes already run in the background. This loop only waits and reports progress.
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$lastReport = Get-Date "2000-01-01"
while (@($builds | Where-Object { -not $_.Process.HasExited }).Count -gt 0) {
    if ((Get-Date) -gt $deadline) {
        foreach ($b in $builds) {
            if (-not $b.Process.HasExited) {
                Stop-ProcessTree $b.Process.Id
                $b.TimedOut = $true
                Write-StageLog "$($b.Side.StepName): timed out after $TimeoutMinutes min, stopped"
            }
        }
        break
    }
    if (((Get-Date) - $lastReport).TotalSeconds -ge 15) {
        foreach ($b in $builds) {
            if (-not $b.Process.HasExited) {
                Write-StageLog ("{0}: {1}" -f $b.Side.StepName, (Get-LastLogLine $b.Side.LogFile))
            }
        }
        $lastReport = Get-Date
    }
    Start-Sleep -Seconds 2
}

# ---------- step 4: record results ----------

$results = @()
$sideResults = [ordered]@{}
$testCases = @{}
foreach ($b in $builds) {
    $b.Process.WaitForExit(10000) | Out-Null
    $b.Process.Refresh()
    $exit = if ($b.TimedOut) { -1 } elseif ($null -ne $b.Process.ExitCode) { [int]$b.Process.ExitCode } else { -1 }
    $logText = if (Test-Path -LiteralPath $b.Side.LogFile) { Get-Content -LiteralPath $b.Side.LogFile } else { @() }
    $buildLine = ($logText | Select-String -Pattern 'BUILD (SUCCESS|FAILURE|ERROR)' | Select-Object -Last 1)
    # Maven prints one "Tests run:" total per test plugin run (surefire for *Test, failsafe for *IT).
    $testLines = @($logText | Select-String -Pattern '\[(INFO|ERROR|WARNING)\] Tests run: \d+, Failures: \d+, Errors: \d+, Skipped: \d+$')
    $errorLines = @($logText | Select-String -Pattern '^\[ERROR\]' | Select-Object -First 15 | ForEach-Object { $_.Line.Trim() })

    $reports = @()
    foreach ($dir in @("target\surefire-reports", "target\failsafe-reports")) {
        $p = Join-Path $b.Side.WorkTree $dir
        if (Test-Path -LiteralPath $p) { $reports += @(Get-ChildItem -LiteralPath $p -Filter "TEST-*.xml" -File | ForEach-Object { $_.FullName }) }
    }
    $cases = @(ConvertFrom-SurefireReport -Paths $reports)
    $testCases[$b.Side.Side] = $cases

    $state = if ($exit -eq 0) { "done" } else { "failed" }
    # The first decisive Maven error says why: a failed goal, failing tests or a compilation error.
    $cause = @($errorLines | Where-Object { $_ -match 'Failed to execute goal|There are test failures|COMPILATION ERROR|Tests run:.*Failures: [1-9]|Could not resolve' } | Select-Object -First 1)
    $causeText = if ($cause.Count -gt 0) { ($cause[0] -replace '^\[ERROR\]\s*', '' -replace '\s*->\s*\[Help \d+\]$', '') } else { "" }
    $reason = if ($b.TimedOut) { "timed out after $TimeoutMinutes min" }
              elseif ($exit -ne 0) { "mvn exit code $exit$(if ($causeText) { ': ' + $causeText }); see $($b.Side.LogFile)" }
              else { "" }
    $results += [pscustomobject]@{
        name        = $b.Side.StepName
        state       = $state
        reason      = $reason
        branch      = $b.Side.Branch
        commit      = $b.Side.Commit
        jdk         = $b.Side.Jdk
        exitCode    = $exit
        buildResult = if ($buildLine) { $buildLine.Line.Trim() } else { "" }
        testSummary = (@($testLines | ForEach-Object { $_.Line.Trim() }) -join " | ")
        log         = $b.Side.LogFile
        startedAt   = $b.StartedAt.ToString("o")
        finishedAt  = (Get-Date).ToString("o")
    }
    $sideResults[$b.Side.Side] = [ordered]@{
        branch       = $b.Side.Branch
        commit       = $b.Side.Commit
        worktree     = $b.Side.WorkTree
        jdk          = $b.Side.Jdk
        command      = $b.Command
        exitCode     = $exit
        timedOut     = $b.TimedOut
        result       = if ($buildLine) { $buildLine.Line.Trim() } else { "no BUILD line (Maven did not finish)" }
        testTotals   = @($testLines | ForEach-Object { $_.Line.Trim() })
        tests        = [ordered]@{
            total   = $cases.Count
            passed  = @($cases | Where-Object { $_.status -eq "passed" }).Count
            failed  = @($cases | Where-Object { $_.status -eq "failed" -or $_.status -eq "error" }).Count
            skipped = @($cases | Where-Object { $_.status -eq "skipped" }).Count
        }
        integrationTests = [ordered]@{ mode = $b.Side.ItMode; classes = @($b.Side.ItClasses) }
        errors       = $errorLines
        log          = $b.Side.LogFile
    }
    Write-StageLog ("{0}: {1}{2}" -f $b.Side.StepName, $state, $(if ($reason) { " - $reason" } else { "" }))
    if ($buildLine) { Write-StageLog ("{0}: {1}" -f $b.Side.StepName, $buildLine.Line.Trim()) }
    foreach ($t in $testLines) { Write-StageLog ("{0}: {1}" -f $b.Side.StepName, $t.Line.Trim()) }
}

$comparison = @(Compare-TestResults -Base $testCases["base"] -Head $testCases["head"])
$changedTests = @($comparison | Where-Object { $_.change -ne "same" })
Write-JsonFile (Join-Path $ArtifactsDir "test-results.json") ([ordered]@{
    schemaVersion = 1
    runId         = $RunId
    summary       = [ordered]@{
        tests       = $comparison.Count
        same        = @($comparison | Where-Object { $_.change -eq "same" }).Count
        new         = @($comparison | Where-Object { $_.change -eq "new" }).Count
        removed     = @($comparison | Where-Object { $_.change -eq "removed" }).Count
        nowFailing  = @($comparison | Where-Object { $_.change -eq "now-failing" }).Count
        nowPassing  = @($comparison | Where-Object { $_.change -eq "now-passing" }).Count
        nowSkipped  = @($comparison | Where-Object { $_.change -eq "now-skipped" }).Count
        nowRunning  = @($comparison | Where-Object { $_.change -eq "now-running" }).Count
    }
    tests         = $comparison
})
foreach ($c in $changedTests) { Write-StageLog ("  test {0}: {1} (base {2}, head {3})" -f $c.change, $c.test, $(if ($c.base) { $c.base } else { "-" }), $(if ($c.head) { $c.head } else { "-" })) }

$allDone = (@($results | Where-Object { $_.state -ne "done" }).Count -eq 0)
$anyTimeout = (@($builds | Where-Object { $_.TimedOut }).Count -gt 0)
Write-JsonFile (Join-Path $ArtifactsDir "build-results.json") ([ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    goals         = ($goals -join ' ')
    settingsFile  = $settings
    parallel      = $true
    ok            = $allDone
    sides         = $sideResults
})

# Keep Phase 0 steps; replace the build steps. Phase 1 never removes earlier steps.
foreach ($r in $results) {
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($old in @($status.steps)) { if ($old.name -ne $r.name) { $list.Add($old) } }
    $list.Add($r)
    $status.steps = $list.ToArray()
}
if ($allDone) { Set-RunState $status "phase1-done" "phase1" }
elseif ($anyTimeout) { Set-RunState $status "failed-tool" "phase1" }
else { Set-RunState $status "failed-build" "phase1" }
Write-JsonFile $StatusFile $status

if ($allDone) {
    Write-StageLog "phase 1 done: both branches built and tested. Logs in $LogsDir"
    exit 0
}
if ($anyTimeout) {
    Write-StageLog "phase 1 stopped: a build timed out. Logs in $LogsDir"
    exit 3
}
foreach ($r in @($results | Where-Object { $_.state -ne "done" })) {
    Write-StageLog "FAILED $($r.branch): $($r.buildResult) $($r.testSummary). Log: $($r.log)"
    foreach ($e in @($sideResults[$(if ($r.branch -eq $cfg.baseBranch) { 'base' } else { 'head' })].errors | Select-Object -First 5)) { Write-StageLog "  $e" }
}
Write-StageLog "phase 1 finished with a failed build or failed tests. Read the log of the failed side in $LogsDir"
exit 4

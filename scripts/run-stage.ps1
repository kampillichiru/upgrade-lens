<#
.SYNOPSIS
  Runs one UpgradeLens stage of an orchestrated run, only when next-step.ps1 says it is next.

.DESCRIPTION
  Guards:
    - order: the key must be the step next-step.ps1 prints (no stage runs early, late or twice by mistake)
    - lock: one stage at a time per run (runs\<id>\orchestration\lock.json)
    - detached: the stage runs in its own process, so a chat tool that times out a long command (a Maven build
      can take an hour) does not kill it. This call waits up to -WaitSeconds, then returns exit 10 (still
      running); run it again with -Wait until it returns.
    - failure report: when a stage fails, stage 8 writes a partial report (after a failed build, stage 2 first)
    - ledger: each run is recorded in runs\<id>\orchestration\ledger.json with its exit code

  Exit codes:
    0   the stage finished (stage 10: any verdict; read next-step.ps1 for the next action)
    2   usage, out of order, or locked by another stage
    3   tool failure in the stage          4   build or tests failed (stage 1)
    10  still running: run the same command with -Wait

.EXAMPLE
  .\scripts\run-stage.ps1 -New
  .\scripts\run-stage.ps1 -RunId 20261126-152851 -Key stage-1
  .\scripts\run-stage.ps1 -RunId 20261126-152851 -Wait
#>
param(
    [string]$RunId,
    [string]$Key,
    [switch]$New,
    [switch]$Wait,
    [ValidateRange(10, 3000)][int]$WaitSeconds = 480,
    [string]$Config,
    [switch]$Child
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
. (Join-Path $PSScriptRoot "lib\analysis.ps1")
. (Join-Path $PSScriptRoot "lib\orchestration.ps1")

$Config = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $Config)) { Write-Host "Config not found: $Config"; exit 2 }
$env:UPGRADELENS_CONFIG = $Config
$liveCfg = Read-JsonFile $Config
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir

function Invoke-StageScript([int]$Stage, [hashtable]$Arguments) {
    # Runs a stage script in this process and returns its exit code. A throw is a tool failure (3).
    $env:UPGRADELENS_INVOKER = "run-stage"
    try {
        $global:LASTEXITCODE = $null
        & (Join-Path $PSScriptRoot (Get-StageScript $Stage)) @Arguments | Out-Host
        $code = $LASTEXITCODE
    } catch {
        Write-Host "$(Get-StageScript $Stage) threw: $($_.Exception.Message)"
        $code = 3
    }
    if ($null -eq $code) { $code = 3 }
    return [int]$code
}

# ---------- stage 0: a new run, in the foreground (it is short: config check, clone, worktrees) ----------

if ($New) {
    $before = @(Get-ChildItem -LiteralPath $runsRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    $code = Invoke-StageScript 0 @{ Config = $Config }
    $id = Get-ChildItem -LiteralPath $runsRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $before -notcontains $_.Name } | Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty Name
    if (-not $id) { Write-Host "Stage 0 failed (exit $code) before a run folder was created. Fix the FAIL lines above (config\upgrade-lens.json) and start again."; exit $(if ($code -eq 0) { 3 } else { $code }) }
    $dir = Join-Path $runsRoot $id
    [void](New-Ledger -RunDir $dir -RunId $id -ConfigPath $Config)
    $state = if ($code -eq 0) { "done" } else { "failed" }
    Set-LedgerEntry -RunDir $dir -Key "stage-0" -Kind "stage" -State $state -Reason $(if ($code -eq 0) { "" } else { "setup failed; see status.json and logs\phase0.log" }) -Extra @{ exitCode = $code }
    Write-Output (ConvertTo-Json -InputObject ([ordered]@{ runId = $id; runDir = $dir; stage = 0; exitCode = $code; next = ".\scripts\next-step.ps1 -RunId $id -Config `"$Config`"" }))
    exit $code
}

if (-not $RunId) { Write-Host "-RunId is required (or -New to start a run)."; exit 2 }
$RunDir = Join-Path $runsRoot $RunId
if (-not (Read-Ledger $RunDir)) { Write-Host "Run $RunId has no orchestration ledger. Start runs with run-stage.ps1 -New."; exit 2 }
$orchDir = Join-Path $RunDir "orchestration"
$lockPath = Join-Path $orchDir "lock.json"

# ---------- child: runs the stage, records the result, releases the lock ----------

if ($Child) {
    $stage = (@(Get-RunPlan -MaxRepairRounds 9) | Where-Object { $_.key -eq $Key } | Select-Object -First 1).stage
    $code = Invoke-StageScript $stage @{ RunId = $RunId; Config = $Config }
    $ok = ($code -eq 0) -or ($stage -eq 10 -and $code -eq 1)
    $reason = ""
    if (-not $ok) {
        $st = Read-JsonFile (Join-Path $RunDir "status.json")
        $failed = @($st.steps | Where-Object { $_.state -notin @("done", "skipped") }) | Select-Object -Last 1
        $reason = if ($failed) { "$($failed.name): $($failed.reason)" } else { "exit $code; see logs\phase$stage.log" }
        if ($stage -eq 1 -and $code -eq 4) { [void](Invoke-StageScript 2 @{ RunId = $RunId; Config = $Config }) }
        $env:UPGRADELENS_INVOKER = "run-stage"
        try { & (Join-Path $PSScriptRoot "phase8-output.ps1") -RunId $RunId -ReportOnly -Config $Config | Out-Host } catch { Write-Host "Partial report failed: $($_.Exception.Message)" }
    }
    Set-LedgerEntry -RunDir $RunDir -Key $Key -Kind "stage" -State $(if ($ok) { "done" } else { "failed" }) -Reason $reason -Extra @{ exitCode = $code } -Update
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    exit $code
}

# ---------- parent: wait for a running stage ----------

function Wait-Stage([string]$StageKey, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $lk = Get-RunLock $RunDir
        if (-not $lk.active) {
            $e = Get-LedgerEntry (Read-Ledger $RunDir) $StageKey
            $outLog = Join-Path $orchDir "$StageKey.out.log"
            if (Test-Path -LiteralPath $outLog) { Get-Content -LiteralPath $outLog -Tail 25 | ForEach-Object { Write-Host $_ } }
            if (-not $e -or $e.state -eq "running") { Write-Host "Stage $StageKey ended without recording a result (the runner died). See $outLog."; return 3 }
            Write-Host "Stage $StageKey $($e.state) (exit $($e.exitCode))$(if ($e.reason) { ': ' + $e.reason }). Next: .\scripts\next-step.ps1 -RunId $RunId"
            if ($e.state -eq "done") { return 0 }
            return $(if ([int]$e.exitCode -eq 0) { 3 } else { [int]$e.exitCode })
        }
        Start-Sleep -Seconds 5
    }
    Write-Host "Stage $StageKey is still running (pid $((Get-RunLock $RunDir).pid)). Run: .\scripts\run-stage.ps1 -RunId $RunId -Wait"
    return 10
}

if ($Wait) {
    $lk = Get-RunLock $RunDir
    $k = if ($lk.key) { $lk.key } else { (@((Read-Ledger $RunDir).entries | Where-Object { $_.kind -eq "stage" }) | Select-Object -Last 1).key }
    if (-not $k) { Write-Host "No stage to wait for."; exit 2 }
    exit (Wait-Stage $k $WaitSeconds)
}

# ---------- parent: start a stage, only if it is the next step ----------

if (-not $Key) { Write-Host "-Key is required: the key next-step.ps1 printed (for example stage-1)."; exit 2 }
$runCfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$plan = Get-RunPlan -MaxRepairRounds ([int](Get-ConfigValue $runCfg "orchestration.maxTestRepairRounds" 1))
$ledger = Read-Ledger $RunDir
$facts = Get-RunFacts -RunDir $RunDir -Ledger $ledger
$next = Get-NextAction -Plan $plan -Ledger $ledger -Facts $facts -MaxAgentAttempts ([int](Get-ConfigValue $runCfg "orchestration.maxAgentAttempts" 2))
if ($next.action -eq "wait") { Write-Host "Stage $($next.key) is running. Run: .\scripts\run-stage.ps1 -RunId $RunId -Wait"; exit 2 }
if ($next.action -ne "run-stage" -or $next.key -ne $Key) {
    Write-Host "Out of order: next-step says '$($next.action) $($next.key)', not '$Key'. Run .\scripts\next-step.ps1 -RunId $RunId and do what it prints."
    exit 2
}

New-Item -ItemType Directory -Force -Path $orchDir | Out-Null
# Lock first (held by this process), so no one sees a running step without a live lock holder.
Write-JsonFile $lockPath ([ordered]@{ pid = $PID; key = $Key; startedAt = (Get-Date).ToString("o") })
Set-LedgerEntry -RunDir $RunDir -Key $Key -Kind "stage" -State "running" -Reason $next.reason
$outLog = Join-Path $orchDir "$Key.out.log"
$errLog = Join-Path $orchDir "$Key.err.log"
$childArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Child", "-RunId", $RunId, "-Key", $Key, "-Config", "`"$Config`"")
try {
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $childArgs -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
} catch {
    Set-LedgerEntry -RunDir $RunDir -Key $Key -Kind "stage" -State "failed" -Reason "could not start the stage process: $($_.Exception.Message)" -Extra @{ exitCode = 3 } -Update
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    Write-Host "Could not start ${Key}: $($_.Exception.Message)"
    exit 3
}
# Hand the lock to the child, unless it already finished and released it.
$e = Get-LedgerEntry (Read-Ledger $RunDir) $Key
if ($e.state -eq "running") { Write-JsonFile $lockPath ([ordered]@{ pid = $proc.Id; key = $Key; startedAt = (Get-Date).ToString("o") }) }
Write-Host "Started $Key ($($next.reason)) as pid $($proc.Id). Output: $outLog"
exit (Wait-Stage $Key $WaitSeconds)

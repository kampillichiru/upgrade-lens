<#
.SYNOPSIS
  Removes the clone and the worktrees of one UpgradeLens run (or of every run). The run's artifacts, logs and
  reports are kept unless -DeleteRunFolder is given.

.DESCRIPTION
  Each run clones the repository into runs\<id>\repo.git and checks out both branches under
  runs\<id>\worktrees\. The change_branch worktree is a real branch: stage 8 places the validated new tests
  there, uncommitted, for a developer to review, commit and push.

  So this script first checks each worktree, and skips a run (exit 3) whose worktrees hold work that would be
  lost:
    - uncommitted changes or untracked files (for example placed new tests not yet committed)
    - commits on the branch that are not pushed to origin
  -Force removes them anyway. artifacts\new-tests.patch keeps the new tests in every case.

  Runs made before the clone-per-run change used a shared repository; for those, the script runs
  "git worktree remove" and "git worktree prune" in that repository, and changes nothing else there.

  Exit codes:
    0  done
    2  usage problem (no such run, or neither -RunId nor -All)
    3  at least one run was skipped (work that would be lost) or git failed

.PARAMETER RunId
  Run folder name under runs\.

.PARAMETER All
  Every run folder under runs\.

.PARAMETER Force
  Remove worktrees even when they hold uncommitted or unpushed work.

.PARAMETER DeleteRunFolder
  Also delete the run folder itself (artifacts, logs, reports).

.PARAMETER Config
  Config file. Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.EXAMPLE
  .\scripts\cleanup-run.ps1 -RunId 20261126-143429
  .\scripts\cleanup-run.ps1 -All -DeleteRunFolder
#>
param(
    [string]$RunId,
    [switch]$All,
    [switch]$Force,
    [switch]$DeleteRunFolder,
    [string]$Config
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$cfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $cfg -RootDir $RootDir
if (-not $RunId -and -not $All) { Write-Host "Give -RunId <id> or -All."; exit 2 }

$runs = if ($All) { @(Get-ChildItem -LiteralPath $runsRoot -Directory -ErrorAction SilentlyContinue) } else { @(Get-Item -LiteralPath (Join-Path $runsRoot $RunId) -ErrorAction SilentlyContinue) }
if ($runs.Count -eq 0) { Write-Host "No run found under $runsRoot$(if ($RunId) { ' named ' + $RunId })."; exit 2 }

$git = if ($cfg.git -and $cfg.git.command) { $cfg.git.command } else { "git" }

function Get-UnsavedWork([string]$Worktree) {
    # Reasons the worktree cannot be removed without losing work. Empty when it is safe.
    $why = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath (Join-Path $Worktree ".git"))) { return $why.ToArray() }
    $st = Invoke-Native $git @("-C", $Worktree, "status", "--porcelain")
    if ($st.ExitCode -eq 0 -and @($st.Output | Where-Object { $_ }).Count -gt 0) {
        $why.Add("$(@($st.Output | Where-Object { $_ }).Count) uncommitted change(s) or untracked file(s)")
    }
    $up = Invoke-Native $git @("-C", $Worktree, "rev-list", "--count", "@{upstream}..HEAD")
    if ($up.ExitCode -eq 0 -and [int]($up.Output | Select-Object -First 1) -gt 0) {
        $why.Add("$($up.Output | Select-Object -First 1) commit(s) not pushed")
    }
    return $why.ToArray()
}

$failed = 0
foreach ($run in $runs) {
    $envRec = Read-JsonFile (Join-Path $run.FullName "artifacts\environment.json")
    $runCfg = Read-JsonFile (Join-Path $run.FullName "config\upgrade-lens.json")
    $ownRepo = Join-Path $run.FullName "repo.git"
    $isOwn = Test-Path -LiteralPath $ownRepo
    $repo = if ($isOwn) { $ownRepo } elseif ($envRec -and $envRec.repo) { $envRec.repo } elseif ($runCfg -and $runCfg.repo) { $runCfg.repo } else { $null }
    $wtRoot = Join-Path $run.FullName "worktrees"
    $wts = @(Get-ChildItem -LiteralPath $wtRoot -Directory -ErrorAction SilentlyContinue)

    if (-not $Force) {
        $blocked = @()
        foreach ($wt in $wts) { foreach ($w in (Get-UnsavedWork $wt.FullName)) { $blocked += "$($wt.Name): $w" } }
        if ($blocked.Count -gt 0) {
            Write-Host "$($run.Name): skipped, it holds work that would be lost: $($blocked -join '; '). Commit and push it, or use -Force."
            $failed++
            continue
        }
    }

    foreach ($wt in $wts) {
        if ($repo -and (Test-Path -LiteralPath $repo)) {
            $r = Invoke-Native $git @("-C", $repo, "worktree", "remove", "--force", $wt.FullName)
            if ($r.ExitCode -ne 0 -and ($r.Output -join ' ') -notmatch 'is not a working tree') {
                Write-Host "$($run.Name): git worktree remove failed for $($wt.FullName): $($r.Output -join ' ')"
                $failed++
                continue
            }
        }
        if (Test-Path -LiteralPath $wt.FullName) { Remove-Item -LiteralPath $wt.FullName -Recurse -Force }
        Write-Host "$($run.Name): removed worktree $($wt.Name)"
    }
    if ($isOwn) {
        Remove-Item -LiteralPath $ownRepo -Recurse -Force
        Write-Host "$($run.Name): removed its clone (repo.git)"
    } elseif ($repo -and (Test-Path -LiteralPath $repo)) {
        $prune = Invoke-Native $git @("-C", $repo, "worktree", "prune")
        if ($prune.ExitCode -ne 0) { Write-Host "git worktree prune failed in ${repo}: $($prune.Output -join ' ')"; $failed++ }
    }
    if ($DeleteRunFolder) {
        Remove-Item -LiteralPath $run.FullName -Recurse -Force
        Write-Host "$($run.Name): run folder deleted"
    }
}
if ($failed -gt 0) { exit 3 }
exit 0

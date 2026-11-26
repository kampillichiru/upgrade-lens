<#
.SYNOPSIS
  Guards around each agent step of an orchestrated UpgradeLens run: begin before the subagent runs, end
  after it returns. Also prints the input hash an agent writes into its proposal.

.DESCRIPTION
  -Action begin   (orchestrator) Only when next-step.ps1 says this agent step is next. Checks the inputs and the
                  agent budget (config agentBudget), records the call, takes a fingerprint of everything the agent
                  must not change, and prints JSON with the exact prompt for the subagent.
  -Action end     (orchestrator) After the subagent returns. Fails the step when:
                    guard   the agent changed anything outside its allowed files (the run folder outside
                            artifacts\agent, a worktree's git state, the tool's scripts, config, agents or skills,
                            or another agent's files). The run stops.
                    checks  its output is missing, stale or incomplete (lib\orchestration.ps1, Test-*Proposal):
                            for example a gap with no test and no reason, a changed method with no reason, a
                            confirmed release note with no source. next-step.ps1 allows a retry
                            (orchestration.maxAgentAttempts, default 2), with the errors in the new prompt.
                  For the reviewer, end also writes artifacts\agent\review.json with each test file's hash.
  -Action hash    (agents) prints the SHA-256 of a step's inputs (lib\core.ps1, Get-AgentInputs).

  Exit codes: 0 done; 2 usage, out of order or budget spent; 5 guard violation (run stops); 6 checks failed.

.EXAMPLE
  .\scripts\agent-step.ps1 -RunId 20261126-152851 -Key agent-analyst -Action begin
  .\scripts\agent-step.ps1 -RunId 20261126-152851 -Key agent-analyst -Action end
  .\scripts\agent-step.ps1 -RunId 20261126-152851 -Step tests -Action hash
#>
param(
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$Key,
    [ValidateSet("analyst", "release-notes", "tests", "reviewer")][string]$Step,
    [ValidateSet("hash", "begin", "end", "record", "review")][string]$Action = "hash",
    [string]$Config
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
. (Join-Path $PSScriptRoot "lib\analysis.ps1")
. (Join-Path $PSScriptRoot "lib\orchestration.ps1")

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$RunDir = Join-Path $runsRoot $RunId
if (-not (Test-Path -LiteralPath (Join-Path $RunDir "status.json"))) { Write-Host "Run not found: $RunDir"; exit 2 }
$ArtifactsDir = Join-Path $RunDir "artifacts"
$AgentDir = Join-Path $ArtifactsDir "agent"
$cfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$ledger = Read-Ledger $RunDir

if ($Key) {
    if ($Key -match '^agent-(analyst|release-notes|reviewer)$') { $Step = $Matches[1] }
    elseif ($Key -match '^agent-tests(-repair-\d+)?$') { $Step = "tests" }
    else { Write-Host "Unknown agent key '$Key'."; exit 2 }
}
if (-not $Step) { Write-Host "-Key (begin, end) or -Step (hash) is required."; exit 2 }

# ---------- hash: for the agents ----------

if ($Action -eq "hash") {
    if ($Step -eq "reviewer") { Write-Host "The reviewer has no input hash."; exit 2 }
    $inputs = @(Get-AgentInputs -RunDir $RunDir -Step $Step)
    foreach ($p in $inputs) { if (-not (Test-Path -LiteralPath $p)) { Write-Host "Missing input for ${Step}: $p. Run the earlier stages first."; exit 2 } }
    Write-Output (Get-InputHash $inputs)
    exit 0
}

if (@("record", "review") -contains $Action) {
    Write-Host "-Action $Action is replaced by -Action begin / end, run by the orchestrator (next-step.ps1 prints both commands). An agent never records its own call."
    exit 2
}
if (-not $ledger) { Write-Host "Run $RunId has no orchestration ledger. Agent steps run only in orchestrated runs (run-stage.ps1 -New)."; exit 2 }
if (-not $Key) { Write-Host "-Key is required for -Action $Action (the key next-step.ps1 printed)."; exit 2 }

$info = $script:AgentFiles[$Step]
$plan = Get-RunPlan -MaxRepairRounds ([int](Get-ConfigValue $cfg "orchestration.maxTestRepairRounds" 1))
$maxAttempts = [int](Get-ConfigValue $cfg "orchestration.maxAgentAttempts" 2)
$headWt = (Read-JsonFile (Join-Path $ArtifactsDir "environment.json")).branches.head.worktree
$gitCmd = if ($cfg.git.command) { $cfg.git.command } else { "git" }

function Get-ValidatedTestIds {
    $b = Read-JsonFile (Join-Path $ArtifactsDir "behavior-results.json")
    # Validated tests, and tests that prove a behavior change (pass on master, fail on change_branch).
    return @($(if ($b) { $b.tests } else { @() }) | Where-Object { $_ -and ($_.validated -or ($_.base.passed -and -not $_.head.passed)) } | ForEach-Object { [string]$_.id })
}

# ---------- begin ----------

if ($Action -eq "begin") {
    $next = Get-NextAction -Plan $plan -Ledger $ledger -Facts (Get-RunFacts -RunDir $RunDir -Ledger $ledger) -MaxAgentAttempts $maxAttempts
    if ($next.action -ne "run-agent" -or $next.key -ne $Key) {
        Write-Host "Out of order: next-step says '$($next.action) $($next.key)', not 'run-agent $Key'. Run .\scripts\next-step.ps1 -RunId $RunId and do what it prints."
        exit 2
    }
    $hash = ""
    if ($Step -ne "reviewer") {
        $inputs = @(Get-AgentInputs -RunDir $RunDir -Step $Step)
        foreach ($p in $inputs) { if (-not (Test-Path -LiteralPath $p)) { Write-Host "Missing input for ${Step}: $p."; exit 2 } }
        $hash = Get-InputHash $inputs
    }
    # Budget: one call per begin, retries included.
    $budget = [int](Get-ConfigValue $cfg "agentBudget" 12)
    $usagePath = Join-Path $AgentDir "usage.json"
    $usage = Read-JsonFile $usagePath
    $rows = New-Object System.Collections.Generic.List[object]
    $total = 0
    if ($usage) { foreach ($r in @($usage.agents)) { if ($r) { $rows.Add($r); $total += [int]$r.calls } } }
    if ($total + 1 -gt $budget) {
        Set-LedgerEntry -RunDir $RunDir -Key $Key -Kind "agent" -State "failed" -Reason "budget: $total of $budget agent calls used. Raise agentBudget in the config and start a new run."
        Write-Host "Agent budget spent: $total of $budget calls. The run stops here."
        exit 2
    }
    $rows.Add([pscustomobject]@{ agent = $info.agent; step = $Step; key = $Key; calls = 1; recordedAt = (Get-Date).ToString("o") })
    Write-JsonFile $usagePath ([ordered]@{ schemaVersion = 1; runId = $RunId; budget = $budget; totalCalls = $total + 1; agents = $rows.ToArray() })
    # A retry starts from a clean answer file for the reviewer; other agents may improve their earlier files.
    if ($Step -eq "reviewer") { Remove-Item -LiteralPath (Join-Path $AgentDir "review-answer.json") -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null

    $previous = @($ledger.entries | Where-Object { $_.key -eq $Key -and $_.state -eq "failed" }) | Select-Object -Last 1
    $prevErrors = @()
    if ($previous -and $previous.errorsFile -and (Test-Path -LiteralPath $previous.errorsFile)) { $prevErrors = @((Read-JsonFile $previous.errorsFile).errors) }
    $reviewIds = if ($Step -eq "reviewer") { Get-ValidatedTestIds } else { @() }
    $prompt = Get-AgentPrompt -Step $Step -RunId $RunId -RunDir $RunDir -InputHash $hash -Key $Key -ReviewTestIds $reviewIds
    if ($prevErrors.Count -gt 0) { $prompt += "`nYour previous attempt was rejected by the checks. Fix every item:`n- " + ($prevErrors -join "`n- ") }

    $attempt = @($ledger.entries | Where-Object { $_.key -eq $Key }).Count + 1
    $guardFile = Join-Path $RunDir "orchestration\$Key-$attempt.guard.json"
    $guard = [ordered]@{
        key = $Key; step = $Step; attempt = $attempt; inputHash = $hash; createdAt = (Get-Date).ToString("o")
        protected = Get-ProtectedFingerprint -RunDir $RunDir -ToolRoot $RootDir -GitCmd $gitCmd
        agentFiles = Get-FileTreeHashes -Root $AgentDir
    }
    Write-JsonFile $guardFile $guard
    Set-LedgerEntry -RunDir $RunDir -Key $Key -Kind "agent" -State "running" -Reason $next.reason -Extra @{
        agent = $info.agent; inputHash = $hash; guardFile = $guardFile; guardSha256 = (Get-FileSha256 $guardFile) }
    Write-Output (ConvertTo-Json -Depth 4 -InputObject ([ordered]@{
        runId = $RunId; key = $Key; step = $Step; attempt = $attempt; subagent = $info.agent; inputHash = $hash
        prompt = $prompt
        endCommand = ".\scripts\agent-step.ps1 -RunId $RunId -Key $Key -Action end -Config `"$localConfig`""
    }))
    exit 0
}

# ---------- end ----------

$entry = Get-LedgerEntry $ledger $Key
if (-not $entry -or $entry.state -ne "running") { Write-Host "Agent step $Key was not begun (state: $(if ($entry) { $entry.state } else { 'none' })). Run its beginCommand first."; exit 2 }

function Stop-Step([string]$State, [string]$Reason, [string[]]$Errors, [int]$Code) {
    $errFile = Join-Path $RunDir "orchestration\$Key-$($entry.attempt).errors.json"
    Write-JsonFile $errFile ([ordered]@{ key = $Key; attempt = $entry.attempt; errors = @($Errors) })
    Set-LedgerEntry -RunDir $RunDir -Key $Key -Kind "agent" -State $State -Reason $Reason -Extra @{ errorsFile = $errFile } -Update
    Write-Host "Agent step $Key $State. $Reason"
    foreach ($e in $Errors) { Write-Host "  - $e" }
    Write-Host "Next: .\scripts\next-step.ps1 -RunId $RunId"
    exit $Code
}

# 1. write scope
$guardFile = [string]$entry.guardFile
if (-not (Test-Path -LiteralPath $guardFile) -or (Get-FileSha256 $guardFile) -ne $entry.guardSha256) {
    Stop-Step "failed" "guard: the guard record of this step was changed or removed" @("guard file $guardFile changed") 5
}
$g = Read-JsonFile $guardFile
$toHash = { param($o) $h = @{}; if ($o) { foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = [string]$p.Value } }; $h }
$protectedNow = Get-ProtectedFingerprint -RunDir $RunDir -ToolRoot $RootDir -GitCmd $gitCmd
$bad = @(Compare-Fingerprint -Before (& $toHash $g.protected) -After $protectedNow)
$agentChanged = @(Compare-Fingerprint -Before (& $toHash $g.agentFiles) -After (Get-FileTreeHashes -Root $AgentDir))
$bad += @(Test-AgentWriteScope -ChangedAgentFiles $agentChanged -Step $Step | ForEach-Object { "artifacts\agent\$_" })
if ($bad.Count -gt 0) {
    Stop-Step "failed" "guard: $($info.agent) changed files it must not change: $(@($bad | Select-Object -First 5) -join ', ')" $bad 5
}

# 2. content checks
$out = Join-Path $AgentDir $info.output
$doc = $null
if (Test-Path -LiteralPath $out) { try { $doc = Read-JsonFile $out } catch { $doc = $null } }
$expected = if ($Step -ne "reviewer") { Get-InputHash (Get-AgentInputs -RunDir $RunDir -Step $Step) } else { "" }
switch ($Step) {
    "analyst" {
        $errors = @(Test-AnalystProposal -Doc $doc -ExpectedHash $expected -Inventory (Read-JsonFile (Join-Path $ArtifactsDir "change-inventory.json")) -Impact (Read-JsonFile (Join-Path $ArtifactsDir "impact-findings.json")))
    }
    "release-notes" {
        $refs = @(Get-ReleaseNotesRefs -LibApi (Read-JsonFile (Join-Path $ArtifactsDir "library-api-diff.json")) -DepDiff (Read-JsonFile (Join-Path $ArtifactsDir "dependency-diff.json")))
        $errors = @(Test-ReleaseNotesProposal -Doc $doc -ExpectedHash $expected -NeededRefs $refs)
    }
    "tests" {
        $gaps = @((Read-JsonFile (Join-Path $ArtifactsDir "test-gaps.json")).gaps | Where-Object { $_ })
        $errors = @(Test-TestsProposal -Doc $doc -ExpectedHash $expected -Gaps $gaps -TestsDir (Join-Path $AgentDir "tests") -HeadWorktree $headWt `
            -MaxTests ([int](Get-ConfigValue $cfg "orchestration.maxTests" 15)))
    }
    "reviewer" {
        $manifest = Read-JsonFile (Join-Path $AgentDir "tests-manifest.json")
        $ids = @(Get-ValidatedTestIds)
        $errors = @(Test-ReviewAnswer -Doc $doc -TestIds $ids)
        if ($manifest -and [string]$manifest.writer -eq $info.agent) { $errors += "the reviewer wrote these tests; a review needs an independent reviewer" }
    }
}
if ($errors.Count -gt 0) {
    $attempts = @($ledger.entries | Where-Object { $_.key -eq $Key }).Count
    $left = $maxAttempts - $attempts
    Stop-Step "failed" "checks: $($errors.Count) problem(s) in $($info.output)$(if ($left -gt 0) { "; $left retry left" } else { '; no retry left' })" $errors 6
}

# 3. the reviewer's answer becomes review.json, with each reviewed file's hash (stage 9 checks them)
if ($Step -eq "reviewer") {
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($r in @($doc.reviews | Where-Object { $_ })) {
        $t = @($manifest.tests | Where-Object { $_.id -eq $r.testId }) | Select-Object -First 1
        $file = Join-Path $AgentDir ("tests\" + ($t.relativePath -replace '/', '\'))
        $rows.Add([pscustomobject]@{ testId = $r.testId; fileSha256 = (Get-FileSha256 $file); writer = $manifest.writer
            reviewer = $info.agent; verdict = $r.verdict; notes = $r.notes })
    }
    Write-JsonFile (Join-Path $AgentDir "review.json") ([ordered]@{ schemaVersion = 1; runId = $RunId; reviews = $rows.ToArray() })
}

Set-LedgerEntry -RunDir $RunDir -Key $Key -Kind "agent" -State "done" -Reason "output accepted" -Update
Write-Host "Agent step $Key done: $out accepted. Next: .\scripts\next-step.ps1 -RunId $RunId"
exit 0

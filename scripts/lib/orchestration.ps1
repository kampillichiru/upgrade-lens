<#
  UpgradeLens orchestration: the fixed plan of a run, the decision of the next step, the test gaps, and the
  guards around each agent step.

  next-step.ps1, run-stage.ps1 and agent-step.ps1 dot-source this file (after core.ps1 and analysis.ps1).
  The decision and validation functions are pure: they take values and return values, so
  tests\upgradelens.Tests.ps1 checks them without a build, a server or an agent.

  The orchestrator (an agent) never decides the order. It asks next-step.ps1, does exactly that one action,
  and asks again. Windows PowerShell 5.1 compatible.
#>

$script:AgentFiles = @{
    "analyst"       = @{ agent = "upgradelens-analyst";       output = "hypotheses.json";     allowed = @("hypotheses.json") }
    "release-notes" = @{ agent = "upgradelens-release-notes"; output = "release-notes.json";  allowed = @("release-notes.json") }
    "tests"         = @{ agent = "upgradelens-test-writer";   output = "tests-manifest.json"; allowed = @("tests-manifest.json", "tests\*") }
    "reviewer"      = @{ agent = "upgradelens-test-reviewer"; output = "review-answer.json";  allowed = @("review-answer.json") }
}

# ---------- the plan ----------

function Get-RunPlan {
    # The ordered steps of an orchestrated run. Every step is either run, or skipped with a recorded reason
    # (a step that is not needed). Nothing is left out silently.
    param([int]$MaxRepairRounds = 1)
    $plan = New-Object System.Collections.Generic.List[object]
    function Add-Step($Key, $Kind, $Stage = $null, $Step = $null, $Title = "") {
        $plan.Add([pscustomobject]@{ key = $Key; kind = $Kind; stage = $Stage; step = $Step; title = $Title })
    }
    Add-Step "stage-0" "stage" 0 $null "setup: config check, clone, worktrees, merge base"
    Add-Step "stage-1" "stage" 1 $null "build and test both branches"
    Add-Step "stage-2" "stage" 2 $null "changed files and methods"
    Add-Step "stage-3" "stage" 3 $null "dependencies, application API and library API"
    Add-Step "agent-release-notes" "agent" $null "release-notes" "validate library changes against release notes"
    Add-Step "stage-4" "stage" 4 $null "impact rules, flows, reach, coverage"
    Add-Step "agent-analyst" "agent" $null "analyst" "hypotheses, plain explanations, reason for each changed method"
    Add-Step "stage-4-merge" "stage" 4 $null "impact again, with the analyst's hypotheses and reasons"
    Add-Step "stage-5-probes" "stage" 5 $null "startup, dumps and probes on both branches"
    Add-Step "agent-tests" "agent" $null "tests" "a test for every gap (untested change, untraced flow, unverified impact)"
    Add-Step "stage-5-tests" "stage" 5 $null "run the new tests on both branches, with injected regressions"
    for ($k = 1; $k -le $MaxRepairRounds; $k++) {
        Add-Step "agent-tests-repair-$k" "agent" $null "tests" "repair the tests that did not validate (round $k)"
        Add-Step "stage-5-repair-$k" "stage" 5 $null "run the repaired tests on both branches (round $k)"
    }
    Add-Step "stage-6" "stage" 6 $null "flow selection"
    Add-Step "stage-7" "stage" 7 $null "manual review items"
    Add-Step "stage-8" "stage" 8 $null "report; validated tests placed uncommitted"
    Add-Step "agent-reviewer" "agent" $null "reviewer" "independent review of the validated tests"
    Add-Step "stage-9" "stage" 9 $null "agent review and manual decisions"
    Add-Step "stage-10" "stage" 10 $null "verdict"
    return $plan.ToArray()
}

function Get-StageScript([int]$Stage) {
    $names = @{
        0 = "phase0-setup.ps1"; 1 = "phase1-build.ps1"; 2 = "phase2-changes.ps1"; 3 = "phase3-deps.ps1"
        4 = "phase4-impact.ps1"; 5 = "phase5-behavior.ps1"; 6 = "phase6-flows.ps1"; 7 = "phase7-manual.ps1"
        8 = "phase8-output.ps1"; 9 = "phase9-review.ps1"; 10 = "phase10-verdict.ps1"
    }
    return $names[$Stage]
}

function Get-LedgerEntry {
    # The last entry for a key, or $null.
    param($Ledger, [string]$Key)
    return (@($Ledger.entries | Where-Object { $_.key -eq $Key }) | Select-Object -Last 1)
}

function Test-StepNeeded {
    # Whether a plan step must run, from the facts of the run at the time the step is reached.
    # Returns @{ needed; reason }. The reason is recorded for a skipped step.
    param($Step, $Ledger, $Facts)
    $done = { param($k) $e = Get-LedgerEntry $Ledger $k; $null -ne $e -and $e.state -eq "done" }
    switch -Regex ($Step.key) {
        '^agent-release-notes$' {
            if ([int]$Facts.notesNeeded -gt 0) { return @{ needed = $true; reason = "$($Facts.notesNeeded) library change(s) or major direct dependency change(s) to validate" } }
            return @{ needed = $false; reason = "no high or medium library API change and no major direct dependency change" }
        }
        '^agent-analyst$' {
            if ([int]$Facts.inventoryFiles -gt 0) { return @{ needed = $true; reason = "$($Facts.inventoryFiles) changed file(s)" } }
            return @{ needed = $false; reason = "no changed file between the branches" }
        }
        '^stage-4-merge$' {
            if (& $done "agent-analyst") { return @{ needed = $true; reason = "merge the analyst proposal" } }
            return @{ needed = $false; reason = "the analyst did not run" }
        }
        '^agent-tests$' {
            if ([int]$Facts.gapCount -gt 0) { return @{ needed = $true; reason = "$($Facts.gapCount) gap(s) in artifacts\test-gaps.json" } }
            return @{ needed = $false; reason = "no gap: every changed method and reached symbol is executed by a test, and every high or medium impact is checked" }
        }
        '^stage-5-tests$' {
            if (& $done "agent-tests") { return @{ needed = $true; reason = "run the new tests" } }
            return @{ needed = $false; reason = "no tests were written" }
        }
        '^agent-tests-repair-(\d+)$' {
            $k = [int]$Matches[1]
            $prevStage = if ($k -eq 1) { "stage-5-tests" } else { "stage-5-repair-$($k - 1)" }
            if (-not (& $done $prevStage)) { return @{ needed = $false; reason = "no tests ran in the previous step" } }
            if ([int]$Facts.testsNotValidated -gt 0) { return @{ needed = $true; reason = "$($Facts.testsNotValidated) test(s) did not validate" } }
            return @{ needed = $false; reason = "every test validated" }
        }
        '^stage-5-repair-(\d+)$' {
            if (& $done ("agent-tests-repair-" + $Matches[1])) { return @{ needed = $true; reason = "run the repaired tests" } }
            return @{ needed = $false; reason = "no repair" }
        }
        '^agent-reviewer$' {
            $n = [int]$Facts.validatedTests + [int]$Facts.regressionsFound
            if ($n -gt 0) { return @{ needed = $true; reason = "$($Facts.validatedTests) validated test(s) and $([int]$Facts.regressionsFound) test(s) that prove a behavior change, to review" } }
            return @{ needed = $false; reason = "no validated test and no test that proves a behavior change" }
        }
        default { return @{ needed = $true; reason = "" } }
    }
}

function Get-NextAction {
    # The one next action of a run: run-stage, run-agent, end-agent, wait, skip, stop or done.
    # Pure: the ledger, the facts and the limits in, one action out.
    #   skip      the caller records the skip and asks again
    #   end-agent an agent step was begun and never ended: run its end guard
    #   stop      a stage failed, an agent failed its guard too often, the budget is spent, or a guard tripped
    param($Plan, $Ledger, $Facts, [int]$MaxAgentAttempts = 2)
    if ($Facts.lockActive) {
        return [pscustomobject]@{ action = "wait"; key = $Facts.lockKey; reason = "stage $($Facts.lockKey) is running (pid $($Facts.lockPid))" }
    }
    foreach ($s in $Plan) {
        $e = Get-LedgerEntry $Ledger $s.key
        if ($e -and @("done", "skipped") -contains $e.state) { continue }
        if ($e -and $e.state -eq "running") {
            if ($s.kind -eq "agent") { return [pscustomobject]@{ action = "end-agent"; key = $s.key; step = $s.step; reason = "the agent step was begun and not ended" } }
            return [pscustomobject]@{ action = "stop"; key = $s.key; reason = "stage $($s.key) is marked running but no runner holds the lock: the runner died. Start a new run." }
        }
        if ($e -and $e.state -eq "failed") {
            if ($s.kind -eq "stage") { return [pscustomobject]@{ action = "stop"; key = $s.key; reason = "stage $($s.key) failed (exit $($e.exitCode)): $($e.reason)" } }
            if ([string]$e.reason -like "budget:*") { return [pscustomobject]@{ action = "stop"; key = $s.key; reason = $e.reason } }
            if ([string]$e.reason -like "guard:*") { return [pscustomobject]@{ action = "stop"; key = $s.key; reason = "agent $($s.key) wrote outside its allowed files: $($e.reason). The run is stopped; start a new run." } }
            $attempts = @($Ledger.entries | Where-Object { $_.key -eq $s.key -and $_.state -eq "failed" }).Count
            if ($attempts -ge $MaxAgentAttempts) {
                return [pscustomobject]@{ action = "stop"; key = $s.key; reason = "agent step $($s.key) failed its checks $attempts time(s). Last: $($e.reason)" }
            }
            return [pscustomobject]@{ action = "run-agent"; key = $s.key; step = $s.step; attempt = $attempts + 1; reason = "retry after: $($e.reason)" }
        }
        $need = Test-StepNeeded -Step $s -Ledger $Ledger -Facts $Facts
        if (-not $need.needed) { return [pscustomobject]@{ action = "skip"; key = $s.key; reason = $need.reason } }
        if ($s.kind -eq "stage") { return [pscustomobject]@{ action = "run-stage"; key = $s.key; stage = $s.stage; reason = $s.title } }
        return [pscustomobject]@{ action = "run-agent"; key = $s.key; step = $s.step; attempt = 1; reason = $need.reason }
    }
    return [pscustomobject]@{ action = "done"; key = ""; reason = "every step ran or was skipped with a reason" }
}

# ---------- facts ----------

function Test-TestNeedsRepair {
    # A proposed test is broken when it fails on master (it does not pin master's behavior), or when it passes
    # on both branches but misses an injected regression (it detects nothing). A test that passes on master and
    # fails on change_branch is not broken: it proves a behavior change, and is kept as evidence.
    param($Test)
    if (-not $Test.base.passed) { return $true }
    if ($Test.head.passed -and -not $Test.validated) { return $true }
    return $false
}

function Get-ReleaseNotesRefs {
    # The library changes the release-notes agent must cover: high or medium library API items, and direct
    # dependencies that changed by a major version. Stage 10 uses the same list.
    param($LibApi, $DepDiff)
    $refs = New-Object System.Collections.Generic.List[string]
    if ($LibApi -and $LibApi.available) { foreach ($i in @($LibApi.items | Where-Object { $_ -and @("high", "medium") -contains [string]$_.severity })) { $refs.Add([string]$i.id) } }
    if ($DepDiff) { foreach ($c in @($DepDiff.changed | Where-Object { $_ -and $_.direct -and $_.major })) { $refs.Add([string]$c.ga) } }
    return @($refs | Select-Object -Unique)
}

function Get-TestGaps {
    # Every item a test must cover, or the test writer must explain why it cannot (requirement: write a test
    # for each potential impact). Ids are stable for one input: G-01, G-02, ... in this order:
    #   untested-change     a changed main-code method or type that no test executes on change_branch
    #   untraced-flow       a flow that reaches code no test executes
    #   unverified-impact   a high or medium finding with production scope that no probe or test checked
    param($ChangedMethods, $Reach, $Impact, $Checks = @())
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($m in @($ChangedMethods.methods | Where-Object { $_ -and -not $_.tested -and $_.kind -ne "file" } | Sort-Object file, name)) {
        $rows.Add([pscustomobject]@{ type = "untested-change"; ref = "$($m.file)|$($m.kind):$($m.name)"; severity = "high"
            detail = "changed $($m.kind) $($m.name) in $($m.file) is not executed by any test on change_branch" })
    }
    foreach ($f in @($Reach.flows | Where-Object { $_ -and @($_.untraced).Count -gt 0 } | Sort-Object id)) {
        $rows.Add([pscustomobject]@{ type = "untraced-flow"; ref = [string]$f.id; severity = "high"
            detail = "flow $($f.id) reaches code no test executes: $(@($f.untraced) -join ', ')" })
    }
    foreach ($f in @($Impact.findings | Where-Object { $_ } | Sort-Object id)) {
        if (@("high", "medium") -notcontains [string]$f.severity) { continue }
        if (@("global", "symbol") -notcontains [string]$f.scope) { continue }
        $st = Get-FindingStatus -Finding $f -Checks @($Checks)
        if ($st -ne "open") { continue }
        $rows.Add([pscustomobject]@{ type = "unverified-impact"; ref = [string]$f.id; severity = [string]$f.severity
            detail = "$($f.id) [$($f.ruleId)] $($f.title): $($f.impact)" })
    }
    $i = 0
    foreach ($r in $rows) { $i++; $r | Add-Member -NotePropertyName id -NotePropertyValue ("G-{0:D2}" -f $i) -Force }
    return @($rows | ForEach-Object { [pscustomobject]@{ id = $_.id; type = $_.type; ref = $_.ref; severity = $_.severity; detail = $_.detail } })
}

# ---------- agent output checks (pure) ----------

function Test-NonEmpty($Value) { return ($null -ne $Value -and ([string]$Value).Trim().Length -gt 0) }

function Test-AnalystProposal {
    # Errors in the analyst proposal. Empty array = accepted.
    # Requires: matching input hash; at most 8 hypotheses, each with a plain explanation, an example and the
    # production risk; a reason for every changed main-code method or type (including added ones); a plain
    # explanation for every rule behind a high or medium finding.
    param($Doc, [string]$ExpectedHash, $Inventory, $Impact)
    $err = New-Object System.Collections.Generic.List[string]
    if (-not $Doc) { $err.Add("hypotheses.json is missing or not valid JSON"); return $err.ToArray() }
    if ([string]$Doc.agent -ne "upgradelens-analyst") { $err.Add("agent must be 'upgradelens-analyst'") }
    if ([string]$Doc.inputHash -ne $ExpectedHash) { $err.Add("inputHash '$($Doc.inputHash)' does not match the inputs ($ExpectedHash)") }
    $hyps = @($Doc.hypotheses | Where-Object { $_ })
    if ($hyps.Count -gt 8) { $err.Add("$($hyps.Count) hypotheses; at most 8") }
    $ids = @{}
    foreach ($h in $hyps) {
        $hid = [string]$h.id
        if ($hid -notmatch '^H-\d{2,}$') { $err.Add("hypothesis id '$hid' must look like H-01") }
        if ($ids.ContainsKey($hid)) { $err.Add("duplicate hypothesis id $hid") }; $ids[$hid] = $true
        foreach ($field in @("claim", "plain", "example", "productionRisk", "probeIdea")) {
            if (-not (Test-NonEmpty $h.$field)) { $err.Add("hypothesis $hid has no $field") }
        }
        if (@("high", "medium", "low") -notcontains [string]$h.severity) { $err.Add("hypothesis $hid severity must be high, medium or low") }
        if (@("global", "symbol", "none") -notcontains [string]$h.scope) { $err.Add("hypothesis $hid scope must be global, symbol or none") }
        if ([string]$h.scope -eq "symbol" -and @($h.symbols | Where-Object { $_ -like "*#*" }).Count -eq 0) { $err.Add("hypothesis $hid has scope symbol but no Class#method symbol") }
    }
    $reasons = @{}
    foreach ($r in @($Doc.changeReasons | Where-Object { $_ })) {
        $reasons["$($r.file)|$($r.symbol)"] = $r
        if (-not (Test-NonEmpty $r.reason)) { $err.Add("change reason for $($r.file) $($r.symbol) is empty") }
        if (-not (Test-NonEmpty $r.evidence)) { $err.Add("change reason for $($r.file) $($r.symbol) has no evidence") }
        if (-not (Test-NonEmpty $r.consequence)) { $err.Add("change reason for $($r.file) $($r.symbol) has no consequence") }
    }
    foreach ($f in @($Inventory.files | Where-Object { $_ -and $_.category -eq "main-code" })) {
        foreach ($s in @($f.symbols | Where-Object { $_ })) {
            if (-not $reasons.ContainsKey("$($f.path)|$($s.symbol)")) { $err.Add("no change reason for $($f.path) $($s.symbol)") }
        }
    }
    $expl = @{}
    foreach ($x in @($Doc.explanations | Where-Object { $_ })) {
        $expl[[string]$x.ruleId] = $x
        foreach ($field in @("plain", "example", "productionRisk")) {
            if (-not (Test-NonEmpty $x.$field)) { $err.Add("explanation for $($x.ruleId) has no $field") }
        }
    }
    $rules = @($Impact.findings | Where-Object { $_ -and @("high", "medium") -contains [string]$_.severity -and [string]$_.source -ne "analyst" } |
        ForEach-Object { [string]$_.ruleId } | Select-Object -Unique)
    foreach ($rid in $rules) { if (-not $expl.ContainsKey($rid)) { $err.Add("no plain explanation for rule $rid (a high or medium finding)") } }
    return $err.ToArray()
}

function Test-ReleaseNotesProposal {
    param($Doc, [string]$ExpectedHash, [string[]]$NeededRefs)
    $err = New-Object System.Collections.Generic.List[string]
    if (-not $Doc) { $err.Add("release-notes.json is missing or not valid JSON"); return $err.ToArray() }
    if ([string]$Doc.agent -ne "upgradelens-release-notes") { $err.Add("agent must be 'upgradelens-release-notes'") }
    if ([string]$Doc.inputHash -ne $ExpectedHash) { $err.Add("inputHash '$($Doc.inputHash)' does not match the inputs ($ExpectedHash)") }
    $byRef = @{}
    foreach ($i in @($Doc.items | Where-Object { $_ })) {
        $byRef[[string]$i.ref] = $i
        $res = [string]$i.result
        if (@("confirmed", "contradicted", "not-found") -notcontains $res) { $err.Add("item $($i.ref): result must be confirmed, contradicted or not-found") ; continue }
        if ($res -ne "not-found") {
            if ([string]$i.source.url -notmatch '^https://') { $err.Add("item $($i.ref) is $res but has no https source url") }
            if (-not (Test-NonEmpty $i.quote)) { $err.Add("item $($i.ref) is $res but has no quote") }
            elseif (@(([string]$i.quote).Trim() -split '\s+').Count -gt 40) { $err.Add("item $($i.ref): the quote is longer than 40 words") }
        }
    }
    foreach ($r in @($NeededRefs)) { if (-not $byRef.ContainsKey([string]$r)) { $err.Add("no item for $r") } }
    return $err.ToArray()
}

function Test-TestsProposal {
    # Errors in the test writer's manifest. Every gap must be covered by a test or listed in 'uncovered' with a
    # reason. Each test file must exist where the manifest says, under src/test, with a class name that matches
    # the file, and each injection must hit exactly one place in the head source.
    param($Doc, [string]$ExpectedHash, $Gaps, [string]$TestsDir, [string]$HeadWorktree, [int]$MaxTests = 15)
    $err = New-Object System.Collections.Generic.List[string]
    if (-not $Doc) { $err.Add("tests-manifest.json is missing or not valid JSON"); return $err.ToArray() }
    if ([string]$Doc.writer -ne "upgradelens-test-writer") { $err.Add("writer must be 'upgradelens-test-writer'") }
    if ([string]$Doc.inputHash -ne $ExpectedHash) { $err.Add("inputHash '$($Doc.inputHash)' does not match the inputs ($ExpectedHash)") }
    $gapIds = @{}; foreach ($g in @($Gaps)) { $gapIds[[string]$g.id] = $true }
    $covered = @{}
    $tests = @($Doc.tests | Where-Object { $_ })
    if ($tests.Count -gt $MaxTests) { $err.Add("$($tests.Count) tests; at most $MaxTests (orchestration.maxTests)") }
    $seen = @{}; $paths = @{}
    foreach ($t in $tests) {
        $tid = [string]$t.id
        if ($tid -notmatch '^T-\d{2,}$') { $err.Add("test id '$tid' must look like T-01") }
        if ($seen.ContainsKey($tid)) { $err.Add("duplicate test id $tid") }; $seen[$tid] = $true
        $rel = [string]$t.relativePath
        if ($rel -notmatch '^src/test/' -or $rel -notmatch '\.java$' -or $rel -match '\.\.' -or $rel -match '\\') {
            $err.Add("test ${tid}: relativePath '$rel' must be a forward-slash path under src/test/ ending in .java"); continue
        }
        $paths[$rel.ToLowerInvariant()] = $true
        $leaf = [System.IO.Path]::GetFileNameWithoutExtension($rel)
        if ([string]$t.className -ne $leaf) { $err.Add("test ${tid}: className '$($t.className)' does not match the file name '$leaf'") }
        if ($TestsDir -and -not (Test-Path -LiteralPath (Join-Path $TestsDir ($rel -replace '/', '\')))) { $err.Add("test ${tid}: file not found under artifacts\agent\tests: $rel") }
        if (-not (Test-NonEmpty $t.purpose)) { $err.Add("test $tid has no purpose") }
        $tg = @($t.gapIds | Where-Object { $_ })
        if ($tg.Count -eq 0) { $err.Add("test $tid names no gapIds") }
        foreach ($g in $tg) { if (-not $gapIds.ContainsKey([string]$g)) { $err.Add("test $tid names unknown gap $g") } else { $covered[[string]$g] = $true } }
        $inj = @($t.injections | Where-Object { $_ })
        if ($inj.Count -eq 0) { $err.Add("test $tid has no injected regression") }
        foreach ($i in $inj) {
            if (-not (Test-NonEmpty $i.find) -or [string]$i.find -eq [string]$i.replace) { $err.Add("test ${tid}: an injection has an empty find or a replace equal to find"); continue }
            if (-not (Test-NonEmpty $i.note)) { $err.Add("test ${tid}: an injection has no note") }
            $f = [string]$i.file
            if ($f -match '\.\.' -or $f -notmatch '^src/main/') { $err.Add("test ${tid}: injection file '$f' must be under src/main/"); continue }
            if ($HeadWorktree) {
                $p = Join-Path $HeadWorktree ($f -replace '/', '\')
                if (-not (Test-Path -LiteralPath $p)) { $err.Add("test ${tid}: injection file not found on change_branch: $f"); continue }
                $text = [System.IO.File]::ReadAllText($p)
                $n = ([regex]::Matches($text, [regex]::Escape([string]$i.find))).Count
                if ($n -ne 1) { $err.Add("test ${tid}: injection text occurs $n time(s) in $f; it must occur exactly once") }
            }
        }
    }
    foreach ($u in @($Doc.uncovered | Where-Object { $_ })) {
        if (-not $gapIds.ContainsKey([string]$u.gapId)) { $err.Add("uncovered names unknown gap $($u.gapId)"); continue }
        if (([string]$u.reason).Trim().Length -lt 20) { $err.Add("uncovered gap $($u.gapId) needs a reason of at least 20 characters") ; continue }
        $covered[[string]$u.gapId] = $true
    }
    foreach ($g in $gapIds.Keys) { if (-not $covered.ContainsKey($g)) { $err.Add("gap $g is neither covered by a test nor listed in 'uncovered' with a reason") } }
    if ($TestsDir -and (Test-Path -LiteralPath $TestsDir)) {
        foreach ($file in @(Get-ChildItem -LiteralPath $TestsDir -Recurse -File)) {
            $rel = $file.FullName.Substring($TestsDir.TrimEnd('\').Length + 1) -replace '\\', '/'
            if (-not $paths.ContainsKey($rel.ToLowerInvariant())) { $err.Add("file in artifacts\agent\tests is not in the manifest: $rel") }
        }
    }
    return $err.ToArray()
}

function Test-ReviewAnswer {
    # Errors in the reviewer's answer: a pass or fail with notes for every validated test, no unknown test.
    param($Doc, [string[]]$TestIds)
    $err = New-Object System.Collections.Generic.List[string]
    if (-not $Doc) { $err.Add("review-answer.json is missing or not valid JSON"); return $err.ToArray() }
    $by = @{}
    foreach ($r in @($Doc.reviews | Where-Object { $_ })) {
        $by[[string]$r.testId] = $r
        if (@("pass", "fail") -notcontains [string]$r.verdict) { $err.Add("review of $($r.testId): verdict must be pass or fail") }
        if (-not (Test-NonEmpty $r.notes)) { $err.Add("review of $($r.testId) has no notes") }
        if (@($TestIds) -notcontains [string]$r.testId) { $err.Add("review names unknown or unvalidated test $($r.testId)") }
    }
    foreach ($t in @($TestIds)) { if (-not $by.ContainsKey([string]$t)) { $err.Add("no review for $t") } }
    return $err.ToArray()
}

# ---------- write scope (pure part) ----------

function Compare-Fingerprint {
    # Keys added, removed or changed between two fingerprints (hashtables key -> hash).
    param($Before, $After)
    $changed = New-Object System.Collections.Generic.List[string]
    foreach ($k in $Before.Keys) { if (-not $After.ContainsKey($k) -or $After[$k] -ne $Before[$k]) { $changed.Add([string]$k) } }
    foreach ($k in $After.Keys) { if (-not $Before.ContainsKey($k)) { $changed.Add([string]$k) } }
    return @($changed | Sort-Object -Unique)
}

function Test-AgentWriteScope {
    # Files under artifacts\agent that one step may create or change. Everything else it touched is a violation.
    param([string[]]$ChangedAgentFiles, [string]$Step)
    $allowed = $script:AgentFiles[$Step].allowed
    $bad = New-Object System.Collections.Generic.List[string]
    foreach ($c in @($ChangedAgentFiles)) {
        $ok = $false
        foreach ($a in $allowed) { if ($c -like $a) { $ok = $true; break } }
        if (-not $ok) { $bad.Add($c) }
    }
    return $bad.ToArray()
}

# ---------- fingerprints (file system) ----------

function Get-FileTreeHashes {
    # relative path -> SHA-256 for every file under $Root, skipping relative paths that match an $Exclude
    # pattern (-like, backslashes).
    param([string]$Root, [string[]]$Exclude = @(), [string]$Prefix = "")
    $map = @{}
    if (-not (Test-Path -LiteralPath $Root)) { return $map }
    $rootFull = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    foreach ($f in @(Get-ChildItem -LiteralPath $rootFull -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $rel = $f.FullName.Substring($rootFull.Length + 1)
        $skip = $false
        foreach ($x in $Exclude) { if ($rel -like $x) { $skip = $true; break } }
        if ($skip) { continue }
        $map[$Prefix + $rel] = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
    }
    return $map
}

function Get-ProtectedFingerprint {
    # What an agent must not change: the run folder outside artifacts\agent (status, config snapshot, stage
    # artifacts such as test-gaps.json, reports, review decisions), the git state of both worktrees, and the
    # tool itself (scripts, config, agent and skill definitions). The orchestration folder is written by the
    # guards themselves around the agent; its guard record is protected by the hash kept in the ledger.
    param([string]$RunDir, [string]$ToolRoot, [string]$GitCmd = "git")
    $map = Get-FileTreeHashes -Root $RunDir -Prefix "run:" -Exclude @(
        "artifacts\agent\*", "worktrees\*", "repo.git\*", "logs\*", "orchestration\*")
    foreach ($d in @("scripts", "config", ".claude\agents", ".claude\skills", ".github\agents")) {
        $part = Get-FileTreeHashes -Root (Join-Path $ToolRoot $d) -Prefix "tool:$d\"
        foreach ($k in $part.Keys) { $map[$k] = $part[$k] }
    }
    $wtRoot = Join-Path $RunDir "worktrees"
    if (Test-Path -LiteralPath $wtRoot) {
        foreach ($wt in @(Get-ChildItem -LiteralPath $wtRoot -Directory)) {
            $st = Invoke-Native $GitCmd @("-C", $wt.FullName, "status", "--porcelain=v1", "--untracked-files=all")
            $hd = Invoke-Native $GitCmd @("-C", $wt.FullName, "rev-parse", "HEAD")
            $map["worktree:$($wt.Name)"] = (@($hd.Output) + @($st.Output)) -join "`n"
        }
    }
    return $map
}

# ---------- ledger (file system) ----------
# runs\<id>\orchestration\ledger.json: one entry per attempt of a plan step, in order. Written only by
# run-stage.ps1, next-step.ps1 (skips) and agent-step.ps1 (begin and end). An agent that writes it stops the run.

function Get-LedgerPath([string]$RunDir) { return (Join-Path $RunDir "orchestration\ledger.json") }

function Read-Ledger([string]$RunDir) {
    $l = Read-JsonFile (Get-LedgerPath $RunDir)
    if ($l -and -not $l.entries) { $l.entries = @() }
    return $l
}

function New-Ledger {
    param([string]$RunDir, [string]$RunId, [string]$ConfigPath)
    $l = [pscustomobject]@{ schemaVersion = 1; runId = $RunId; config = $ConfigPath; createdAt = (Get-Date).ToString("o"); entries = @() }
    Write-JsonFile (Get-LedgerPath $RunDir) $l
    return $l
}

function Set-LedgerEntry {
    # Adds an entry, or with -Update replaces the last entry of the key (the same attempt).
    param([string]$RunDir, [string]$Key, [string]$Kind, [string]$State, [string]$Reason = "", [hashtable]$Extra = @{}, [switch]$Update)
    $l = Read-Ledger $RunDir
    if (-not $l) { throw "No ledger in $RunDir" }
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($e in @($l.entries)) { if ($e) { $list.Add($e) } }
    $now = (Get-Date).ToString("o")
    $attempt = @($list | Where-Object { $_.key -eq $Key }).Count + 1
    if ($Update) {
        for ($i = $list.Count - 1; $i -ge 0; $i--) {
            if ($list[$i].key -eq $Key) {
                $old = $list[$i]
                $entry = [ordered]@{ key = $Key; kind = $Kind; attempt = $old.attempt; state = $State; reason = $Reason; startedAt = $old.startedAt; finishedAt = $now }
                foreach ($p in $old.PSObject.Properties) { if (-not $entry.Contains($p.Name)) { $entry[$p.Name] = $p.Value } }
                foreach ($k in $Extra.Keys) { $entry[$k] = $Extra[$k] }
                $list[$i] = [pscustomobject]$entry
                break
            }
        }
    } else {
        $entry = [ordered]@{ key = $Key; kind = $Kind; attempt = $attempt; state = $State; reason = $Reason; startedAt = $now; finishedAt = $(if ($State -eq "running") { $null } else { $now }) }
        foreach ($k in $Extra.Keys) { $entry[$k] = $Extra[$k] }
        $list.Add([pscustomobject]$entry)
    }
    $l.entries = $list.ToArray()
    Write-JsonFile (Get-LedgerPath $RunDir) $l
}

function Get-RunLock([string]$RunDir) {
    # The stage lock: @{ active; pid; key }. A lock whose process is gone is removed (the ledger keeps the
    # step as running, and next-step reports the dead runner).
    $p = Join-Path $RunDir "orchestration\lock.json"
    $lk = Read-JsonFile $p
    if (-not $lk) { return [pscustomobject]@{ active = $false; pid = $null; key = $null } }
    $proc = Get-Process -Id ([int]$lk.pid) -ErrorAction SilentlyContinue
    if ($proc) { return [pscustomobject]@{ active = $true; pid = $lk.pid; key = $lk.key } }
    Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ active = $false; pid = $lk.pid; key = $lk.key }
}

function Get-RunFacts {
    # The facts Get-NextAction needs, read from the run's artifacts. Writes artifacts\test-gaps.json when the
    # test writer is next and has not started (the gap list is fixed once the writer begins).
    param([string]$RunDir, $Ledger)
    $a = Join-Path $RunDir "artifacts"
    $lock = Get-RunLock $RunDir
    $inventory = Read-JsonFile (Join-Path $a "change-inventory.json")
    $libApi = Read-JsonFile (Join-Path $a "library-api-diff.json")
    $depDiff = Read-JsonFile (Join-Path $a "dependency-diff.json")
    $behavior = Read-JsonFile (Join-Path $a "behavior-results.json")
    $gapsPath = Join-Path $a "test-gaps.json"
    $gapCount = 0
    $probes = Get-LedgerEntry $Ledger "stage-5-probes"
    $writer = Get-LedgerEntry $Ledger "agent-tests"
    if ($probes -and $probes.state -eq "done" -and -not $writer) {
        $gaps = @(Get-TestGaps -ChangedMethods (Read-JsonFile (Join-Path $a "changed-methods.json")) `
                    -Reach (Read-JsonFile (Join-Path $a "reach.json")) `
                    -Impact (Read-JsonFile (Join-Path $a "impact-findings.json")) `
                    -Checks @($(if ($behavior) { $behavior.checks } else { @() })))
        Write-JsonFile $gapsPath ([ordered]@{ schemaVersion = 1; runId = $Ledger.runId; createdAt = (Get-Date).ToString("o"); gaps = $gaps })
        $gapCount = $gaps.Count
    } elseif (Test-Path -LiteralPath $gapsPath) {
        $gapCount = @((Read-JsonFile $gapsPath).gaps | Where-Object { $_ }).Count
    }
    $tests = @($(if ($behavior) { $behavior.tests } else { @() }) | Where-Object { $_ })
    return [pscustomobject]@{
        testsNotValidated = @($tests | Where-Object { Test-TestNeedsRepair $_ }).Count
        regressionsFound  = @($tests | Where-Object { $_.base.passed -and -not $_.head.passed }).Count
        lockActive        = $lock.active
        lockPid           = $lock.pid
        lockKey           = $lock.key
        inventoryFiles    = $(if ($inventory) { @($inventory.files | Where-Object { $_ }).Count } else { 0 })
        notesNeeded       = @(Get-ReleaseNotesRefs -LibApi $libApi -DepDiff $depDiff).Count
        gapCount          = $gapCount
        validatedTests    = @($tests | Where-Object { $_.validated }).Count
    }
}

# ---------- prompts ----------

function Get-AgentPrompt {
    # The exact prompt the orchestrator hands to the subagent. The orchestrator passes it unchanged.
    param([string]$Step, [string]$RunId, [string]$RunDir, [string]$InputHash, [string]$Key, [string[]]$ReviewTestIds = @())
    $agent = $script:AgentFiles[$Step].agent
    $out = Join-Path $RunDir ("artifacts\agent\" + $script:AgentFiles[$Step].output)
    $common = @(
        "UpgradeLens run $RunId. Run folder: $RunDir",
        "You are $agent. Follow your agent instructions (.claude\agents\$agent.md) exactly.",
        "Write only: $(@($script:AgentFiles[$Step].allowed | ForEach-Object { Join-Path $RunDir ('artifacts\agent\' + $_) }) -join ', ').",
        "Anything else you write (the worktrees, repo.git, config, the stage artifacts, the review folder, the tool's scripts) stops the run.",
        "Do not run Maven, git commands that change state, or scripts\run-stage.ps1, next-step.ps1 or agent-step.ps1 begin/end/record: the orchestrator records your call."
    )
    switch ($Step) {
        "analyst" {
            $common += "Put this value in inputHash: $InputHash"
            $common += "Write $out with hypotheses (each with plain, example, productionRisk), explanations (one per rule behind a high or medium finding in artifacts\impact-findings.json) and changeReasons (one per changed main-code symbol, each with reason, evidence and consequence)."
        }
        "release-notes" {
            $common += "Put this value in inputHash: $InputHash"
            $common += "Write $out with one item for every high or medium item of artifacts\library-api-diff.json and every direct major dependency change of artifacts\dependency-diff.json."
        }
        "tests" {
            $common += "Put this value in inputHash: $InputHash"
            $common += "Follow the skill upgradelens-missing-tests. Your work list is $(Join-Path $RunDir 'artifacts\test-gaps.json'): cover every gap with a test (gapIds) or list it in 'uncovered' with the reason."
            if ($Key -like "agent-tests-repair-*") {
                $common += "This is a repair round. Read artifacts\behavior-results.json (tests[]): fix only the tests that fail on master, and the tests that pass on both branches but miss an injected regression (injections[].detected false). Keep the ids and the gap coverage. Do not touch a test that passes on master and fails on change_branch: it proves a behavior change and is kept as evidence."
            }
        }
        "reviewer" {
            $common += "Review these validated tests: $($ReviewTestIds -join ', '). Write your answer (reviews with testId, verdict pass|fail, reviewer, notes) to $out and nothing else."
        }
    }
    return ($common -join "`n")
}

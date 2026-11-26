# Unit tests for the UpgradeLens orchestration and guards (scripts\lib\orchestration.ps1, Resolve-RunId).
# Pester 3 syntax. Run from the repository root:
#   Invoke-Pester .\tests
# No Maven, no server, no git and no agent are needed.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
. (Join-Path $root "scripts\lib\core.ps1")
. (Join-Path $root "scripts\lib\analysis.ps1")
. (Join-Path $root "scripts\lib\orchestration.ps1")

function New-TestLedger([object[]]$Entries) { return [pscustomobject]@{ runId = "r"; entries = @($Entries) } }
function New-TestEntry([string]$Key, [string]$State, [string]$Reason = "", [string]$Kind = "stage") {
    return [pscustomobject]@{ key = $Key; kind = $Kind; state = $State; reason = $Reason; exitCode = 0 }
}
function New-TestFacts([hashtable]$Over = @{}) {
    $f = @{ lockActive = $false; lockPid = $null; lockKey = $null; inventoryFiles = 3; notesNeeded = 0; gapCount = 0; testsNotValidated = 0; validatedTests = 0; regressionsFound = 0 }
    foreach ($k in $Over.Keys) { $f[$k] = $Over[$k] }
    return [pscustomobject]$f
}
function New-DoneLedger([string[]]$Keys) { return (New-TestLedger @($Keys | ForEach-Object { New-TestEntry $_ "done" })) }

Describe "Get-RunPlan" {
    It "orders release notes before stage 4, the analyst between stage 4 and its merge, tests after the probes, the reviewer after stage 8" {
        $keys = @(Get-RunPlan -MaxRepairRounds 1 | ForEach-Object { $_.key })
        $keys[0] | Should Be "stage-0"
        $keys[-1] | Should Be "stage-10"
        [array]::IndexOf($keys, "agent-release-notes") | Should BeLessThan ([array]::IndexOf($keys, "stage-4"))
        [array]::IndexOf($keys, "agent-analyst") | Should BeGreaterThan ([array]::IndexOf($keys, "stage-4"))
        [array]::IndexOf($keys, "stage-4-merge") | Should Be ([array]::IndexOf($keys, "agent-analyst") + 1)
        [array]::IndexOf($keys, "agent-tests") | Should Be ([array]::IndexOf($keys, "stage-5-probes") + 1)
        [array]::IndexOf($keys, "agent-reviewer") | Should Be ([array]::IndexOf($keys, "stage-8") + 1)
    }
    It "adds one test repair pair per round" {
        @(Get-RunPlan -MaxRepairRounds 0 | Where-Object { $_.key -like "*repair*" }).Count | Should Be 0
        @(Get-RunPlan -MaxRepairRounds 2 | Where-Object { $_.key -like "*repair*" }).Count | Should Be 4
    }
}

Describe "Get-NextAction" {
    $plan = Get-RunPlan -MaxRepairRounds 1
    $upTo3 = @("stage-0", "stage-1", "stage-2", "stage-3")
    $upToProbes = @("stage-0", "stage-1", "stage-2", "stage-3", "agent-release-notes", "stage-4", "agent-analyst", "stage-4-merge", "stage-5-probes")

    It "starts with stage 0 on an empty ledger" {
        (Get-NextAction -Plan $plan -Ledger (New-TestLedger @()) -Facts (New-TestFacts)).key | Should Be "stage-0"
    }
    It "waits while a stage holds the lock" {
        (Get-NextAction -Plan $plan -Ledger (New-TestLedger @()) -Facts (New-TestFacts @{ lockActive = $true; lockKey = "stage-1" })).action | Should Be "wait"
    }
    It "skips the release-notes agent with a reason when nothing needs validating" {
        $a = Get-NextAction -Plan $plan -Ledger (New-DoneLedger $upTo3) -Facts (New-TestFacts)
        $a.action | Should Be "skip"
        $a.key | Should Be "agent-release-notes"
        $a.reason | Should Not BeNullOrEmpty
    }
    It "runs the release-notes agent when library changes need it" {
        $a = Get-NextAction -Plan $plan -Ledger (New-DoneLedger $upTo3) -Facts (New-TestFacts @{ notesNeeded = 4 })
        $a.action | Should Be "run-agent"
        $a.step | Should Be "release-notes"
    }
    It "stops on a failed stage" {
        $l = New-TestLedger @((New-TestEntry "stage-0" "done"), (New-TestEntry "stage-1" "failed" "build failed"))
        (Get-NextAction -Plan $plan -Ledger $l -Facts (New-TestFacts)).action | Should Be "stop"
    }
    It "retries an agent whose checks failed, then stops after the attempt limit" {
        $base = @(@("stage-0", "stage-1", "stage-2", "stage-3", "agent-release-notes", "stage-4") | ForEach-Object { New-TestEntry $_ "done" })
        $one = New-TestLedger ($base + @(New-TestEntry "agent-analyst" "failed" "checks: 2 problem(s)" "agent"))
        $a = Get-NextAction -Plan $plan -Ledger $one -Facts (New-TestFacts) -MaxAgentAttempts 2
        $a.action | Should Be "run-agent"
        $a.attempt | Should Be 2
        $two = New-TestLedger ($base + @((New-TestEntry "agent-analyst" "failed" "checks: x" "agent"), (New-TestEntry "agent-analyst" "failed" "checks: y" "agent")))
        (Get-NextAction -Plan $plan -Ledger $two -Facts (New-TestFacts) -MaxAgentAttempts 2).action | Should Be "stop"
    }
    It "stops at once on a write-scope violation or a spent budget" {
        $base = @($upTo3 | ForEach-Object { New-TestEntry $_ "done" })
        $g = New-TestLedger ($base + @(New-TestEntry "agent-release-notes" "failed" "guard: changed run:status.json" "agent"))
        (Get-NextAction -Plan $plan -Ledger $g -Facts (New-TestFacts @{ notesNeeded = 1 })).action | Should Be "stop"
        $b = New-TestLedger ($base + @(New-TestEntry "agent-release-notes" "failed" "budget: 12 of 12" "agent"))
        (Get-NextAction -Plan $plan -Ledger $b -Facts (New-TestFacts @{ notesNeeded = 1 })).action | Should Be "stop"
    }
    It "asks for the end guard when an agent step was begun and not ended" {
        $l = New-TestLedger (@($upTo3 | ForEach-Object { New-TestEntry $_ "done" }) + @(New-TestEntry "agent-release-notes" "running" "" "agent"))
        (Get-NextAction -Plan $plan -Ledger $l -Facts (New-TestFacts)).action | Should Be "end-agent"
    }
    It "stops when a stage is marked running but no process holds the lock" {
        $l = New-TestLedger @((New-TestEntry "stage-0" "done"), (New-TestEntry "stage-1" "running"))
        (Get-NextAction -Plan $plan -Ledger $l -Facts (New-TestFacts)).action | Should Be "stop"
    }
    It "runs the test writer after the probes when there are gaps" {
        $a = Get-NextAction -Plan $plan -Ledger (New-DoneLedger $upToProbes) -Facts (New-TestFacts @{ gapCount = 3 })
        $a.action | Should Be "run-agent"
        $a.key | Should Be "agent-tests"
    }
    It "skips the test run when the test writer was skipped" {
        $l = New-TestLedger (@($upToProbes | ForEach-Object { New-TestEntry $_ "done" }) + @(New-TestEntry "agent-tests" "skipped" "no gap" "agent"))
        $a = Get-NextAction -Plan $plan -Ledger $l -Facts (New-TestFacts)
        $a.key | Should Be "stage-5-tests"
        $a.action | Should Be "skip"
    }
    It "repairs tests that did not validate, and skips the repair when all validated" {
        $keys = $upToProbes + @("agent-tests", "stage-5-tests")
        $a = Get-NextAction -Plan $plan -Ledger (New-DoneLedger $keys) -Facts (New-TestFacts @{ testsNotValidated = 1 })
        $a.key | Should Be "agent-tests-repair-1"
        $a.action | Should Be "run-agent"
        (Get-NextAction -Plan $plan -Ledger (New-DoneLedger $keys) -Facts (New-TestFacts)).action | Should Be "skip"
    }
    It "runs the reviewer only when tests validated" {
        $keys = @($plan | ForEach-Object { $_.key } | Where-Object { $_ -ne "agent-reviewer" -and $_ -ne "stage-9" -and $_ -ne "stage-10" })
        (Get-NextAction -Plan $plan -Ledger (New-DoneLedger $keys) -Facts (New-TestFacts @{ validatedTests = 2 })).action | Should Be "run-agent"
        (Get-NextAction -Plan $plan -Ledger (New-DoneLedger $keys) -Facts (New-TestFacts)).action | Should Be "skip"
    }
    It "is done when every step is done or skipped" {
        $l = New-DoneLedger @($plan | ForEach-Object { $_.key })
        (Get-NextAction -Plan $plan -Ledger $l -Facts (New-TestFacts)).action | Should Be "done"
    }
}

Describe "Test-TestNeedsRepair" {
    function New-T($Base, $Head, $Validated) { [pscustomobject]@{ base = [pscustomobject]@{ passed = $Base }; head = [pscustomobject]@{ passed = $Head }; validated = $Validated } }
    It "repairs a test that fails on master" { Test-TestNeedsRepair (New-T $false $false $false) | Should Be $true }
    It "repairs a test that passes on both but misses an injection" { Test-TestNeedsRepair (New-T $true $true $false) | Should Be $true }
    It "keeps a test that passes on master and fails on change_branch: it proves a behavior change" { Test-TestNeedsRepair (New-T $true $false $false) | Should Be $false }
    It "keeps a validated test" { Test-TestNeedsRepair (New-T $true $true $true) | Should Be $false }
}

Describe "Get-TestGaps" {
    $cm = [pscustomobject]@{ methods = @(
        [pscustomobject]@{ file = "src/main/java/a/B.java"; kind = "method"; name = "run"; tested = $false },
        [pscustomobject]@{ file = "src/main/java/a/B.java"; kind = "method"; name = "ok"; tested = $true }) }
    $reach = [pscustomobject]@{ flows = @(
        [pscustomobject]@{ id = "POST /x"; untraced = @("B#run") },
        [pscustomobject]@{ id = "GET /y"; untraced = @() }) }
    $impact = [pscustomobject]@{ findings = @(
        [pscustomobject]@{ id = "F-001"; ruleId = "R-A"; severity = "high"; scope = "global"; title = "a"; impact = "i" },
        [pscustomobject]@{ id = "F-002"; ruleId = "R-B"; severity = "high"; scope = "global"; title = "b"; impact = "i" },
        [pscustomobject]@{ id = "F-003"; ruleId = "R-C"; severity = "low"; scope = "global"; title = "c"; impact = "i" },
        [pscustomobject]@{ id = "F-004"; ruleId = "R-D"; severity = "high"; scope = "test"; title = "d"; impact = "i" }) }
    $checks = @([pscustomobject]@{ ruleId = "R-B"; outcome = "same" })
    $g = @(Get-TestGaps -ChangedMethods $cm -Reach $reach -Impact $impact -Checks $checks)

    It "lists untested changes, untraced flows and open high or medium production findings, in that order" {
        $g.Count | Should Be 3
        $g[0].type | Should Be "untested-change"
        $g[1].type | Should Be "untraced-flow"
        $g[2].ref | Should Be "F-001"
        $g[0].id | Should Be "G-01"
        $g[2].id | Should Be "G-03"
    }
    It "leaves out verified, low and test-only findings" {
        @($g | Where-Object { @("F-002", "F-003", "F-004") -contains $_.ref }).Count | Should Be 0
    }
}

Describe "Test-AnalystProposal" {
    $inv = [pscustomobject]@{ files = @([pscustomobject]@{ path = "src/main/java/a/B.java"; category = "main-code"; symbols = @([pscustomobject]@{ symbol = "method:run" }) }) }
    $imp = [pscustomobject]@{ findings = @([pscustomobject]@{ ruleId = "R-A"; severity = "high"; source = "rule" }, [pscustomobject]@{ ruleId = "R-L"; severity = "low"; source = "rule" }) }
    function New-Good {
        return [pscustomobject]@{ agent = "upgradelens-analyst"; inputHash = "H"
            hypotheses = @([pscustomobject]@{ id = "H-01"; claim = "c"; severity = "high"; scope = "global"; probeIdea = "p"; plain = "p"; example = "e"; productionRisk = "r" })
            explanations = @([pscustomobject]@{ ruleId = "R-A"; plain = "p"; example = "e"; productionRisk = "r" })
            changeReasons = @([pscustomobject]@{ file = "src/main/java/a/B.java"; symbol = "method:run"; reason = "why"; evidence = "commit"; consequence = "c" }) }
    }
    It "accepts a complete proposal" {
        @(Test-AnalystProposal -Doc (New-Good) -ExpectedHash "H" -Inventory $inv -Impact $imp).Count | Should Be 0
    }
    It "rejects a stale hash, a missing change reason and a missing plain explanation" {
        $bad = New-Good
        $bad.changeReasons = @()
        $bad.explanations = @()
        $e = @(Test-AnalystProposal -Doc $bad -ExpectedHash "OTHER" -Inventory $inv -Impact $imp)
        @($e | Where-Object { $_ -like "*inputHash*" }).Count | Should Be 1
        @($e | Where-Object { $_ -like "no change reason*" }).Count | Should Be 1
        @($e | Where-Object { $_ -like "no plain explanation for rule R-A*" }).Count | Should Be 1
    }
    It "rejects a hypothesis without an example, and more than 8 hypotheses" {
        $bad = New-Good
        $bad.hypotheses = @([pscustomobject]@{ id = "H-01"; claim = "c"; severity = "high"; scope = "global"; probeIdea = "p"; plain = "p"; example = ""; productionRisk = "r" })
        @(Test-AnalystProposal -Doc $bad -ExpectedHash "H" -Inventory $inv -Impact $imp | Where-Object { $_ -like "*no example*" }).Count | Should Be 1
        $many = New-Good
        $many.hypotheses = @(1..9 | ForEach-Object { [pscustomobject]@{ id = ("H-{0:D2}" -f $_); claim = "c"; severity = "low"; scope = "none"; probeIdea = "p"; plain = "p"; example = "e"; productionRisk = "r" } })
        @(Test-AnalystProposal -Doc $many -ExpectedHash "H" -Inventory $inv -Impact $imp | Where-Object { $_ -like "*at most 8*" }).Count | Should Be 1
    }
}

Describe "Test-ReleaseNotesProposal" {
    It "needs an item for every ref, and an https source for a confirmed item" {
        $doc = [pscustomobject]@{ agent = "upgradelens-release-notes"; inputHash = "H"; items = @(
            [pscustomobject]@{ ref = "LIB-001"; result = "confirmed"; source = [pscustomobject]@{ url = "http://x" }; quote = "q" }) }
        $e = @(Test-ReleaseNotesProposal -Doc $doc -ExpectedHash "H" -NeededRefs @("LIB-001", "g:a"))
        @($e | Where-Object { $_ -like "*https*" }).Count | Should Be 1
        @($e | Where-Object { $_ -eq "no item for g:a" }).Count | Should Be 1
    }
    It "accepts not-found without a source" {
        $doc = [pscustomobject]@{ agent = "upgradelens-release-notes"; inputHash = "H"; items = @([pscustomobject]@{ ref = "LIB-001"; result = "not-found" }) }
        @(Test-ReleaseNotesProposal -Doc $doc -ExpectedHash "H" -NeededRefs @("LIB-001")).Count | Should Be 0
    }
}

Describe "Test-TestsProposal" {
    $testsDir = Join-Path $TestDrive "tests"
    $head = Join-Path $TestDrive "head"
    New-Item -ItemType Directory -Force -Path (Join-Path $testsDir "src\test\java\a") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $head "src\main\java\a") | Out-Null
    Set-Content -LiteralPath (Join-Path $testsDir "src\test\java\a\BCharacterizationTest.java") -Value "class BCharacterizationTest {}"
    Set-Content -LiteralPath (Join-Path $head "src\main\java\a\B.java") -Value @('return status("CANCELLED");', 'return other;')
    $gaps = @([pscustomobject]@{ id = "G-01" }, [pscustomobject]@{ id = "G-02" })
    $reasonG2 = [pscustomobject]@{ gapId = "G-02"; reason = "runtime only: needs the real database server" }
    function New-Manifest($Uncovered) {
        return [pscustomobject]@{ writer = "upgradelens-test-writer"; inputHash = "H"; uncovered = @($Uncovered); tests = @(
            [pscustomobject]@{ id = "T-01"; className = "BCharacterizationTest"; relativePath = "src/test/java/a/BCharacterizationTest.java"
                purpose = "pins cancel"; gapIds = @("G-01")
                injections = @([pscustomobject]@{ file = "src/main/java/a/B.java"; find = 'status("CANCELLED")'; replace = 'status("NEW")'; note = "wrong status" }) }) }
    }

    It "accepts a manifest that covers every gap with a test or a reason" {
        @(Test-TestsProposal -Doc (New-Manifest @($reasonG2)) -ExpectedHash "H" -Gaps $gaps -TestsDir $testsDir -HeadWorktree $head).Count | Should Be 0
    }
    It "rejects a gap with neither a test nor a reason" {
        $e = @(Test-TestsProposal -Doc (New-Manifest @()) -ExpectedHash "H" -Gaps $gaps -TestsDir $testsDir -HeadWorktree $head)
        @($e | Where-Object { $_ -like "gap G-02*" }).Count | Should Be 1
    }
    It "rejects an injection whose text is not found exactly once, and an unlisted test file" {
        $m = New-Manifest @($reasonG2)
        $m.tests[0].injections[0].find = "return"
        $extra = Join-Path $testsDir "src\test\java\a\Extra.java"
        Set-Content -LiteralPath $extra -Value "x"
        $e = @(Test-TestsProposal -Doc $m -ExpectedHash "H" -Gaps $gaps -TestsDir $testsDir -HeadWorktree $head)
        Remove-Item -LiteralPath $extra
        @($e | Where-Object { $_ -like "*occurs 2 time(s)*" }).Count | Should Be 1
        @($e | Where-Object { $_ -like "*not in the manifest*Extra.java" }).Count | Should Be 1
    }
    It "rejects a test path outside src/test" {
        $m = New-Manifest @($reasonG2)
        $m.tests[0].relativePath = "src/main/java/a/BCharacterizationTest.java"
        @(Test-TestsProposal -Doc $m -ExpectedHash "H" -Gaps $gaps -TestsDir $testsDir -HeadWorktree $head | Where-Object { $_ -like "*under src/test/*" }).Count | Should Be 1
    }
}

Describe "Test-ReviewAnswer" {
    It "needs a verdict with notes for every validated test and nothing else" {
        $doc = [pscustomobject]@{ reviews = @([pscustomobject]@{ testId = "T-01"; verdict = "pass"; notes = "ok" }, [pscustomobject]@{ testId = "T-09"; verdict = "maybe"; notes = "" }) }
        $e = @(Test-ReviewAnswer -Doc $doc -TestIds @("T-01", "T-02"))
        @($e | Where-Object { $_ -eq "no review for T-02" }).Count | Should Be 1
        @($e | Where-Object { $_ -like "*T-09*" }).Count | Should BeGreaterThan 0
    }
}

Describe "Compare-Fingerprint and Test-AgentWriteScope" {
    It "finds added, removed and changed keys" {
        $c = @(Compare-Fingerprint -Before @{ a = "1"; b = "2"; c = "3" } -After @{ a = "1"; b = "X"; d = "4" })
        ($c -join ",") | Should Be "b,c,d"
    }
    It "lets each agent write only its own files" {
        @(Test-AgentWriteScope -ChangedAgentFiles @("tests-manifest.json", "tests\src\test\java\A.java") -Step "tests").Count | Should Be 0
        @(Test-AgentWriteScope -ChangedAgentFiles @("hypotheses.json", "usage.json") -Step "tests").Count | Should Be 2
        @(Test-AgentWriteScope -ChangedAgentFiles @("review.json") -Step "reviewer").Count | Should Be 1
    }
}

Describe "Get-FileTreeHashes" {
    It "hashes files and honours the exclude patterns" {
        $d = Join-Path $TestDrive "tree"
        New-Item -ItemType Directory -Force -Path (Join-Path $d "artifacts\agent") | Out-Null
        Set-Content -LiteralPath (Join-Path $d "status.json") -Value "{}"
        Set-Content -LiteralPath (Join-Path $d "artifacts\agent\x.json") -Value "{}"
        $m = Get-FileTreeHashes -Root $d -Exclude @("artifacts\agent\*") -Prefix "run:"
        $m.ContainsKey("run:status.json") | Should Be $true
        $m.ContainsKey("run:artifacts\agent\x.json") | Should Be $false
    }
}

Describe "Resolve-RunId guards" {
    $runs = Join-Path $TestDrive "runs"
    New-Item -ItemType Directory -Force -Path (Join-Path $runs "r1\orchestration") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $runs "r2") | Out-Null
    Write-JsonFile (Join-Path $runs "r1\status.json") ([pscustomobject]@{ state = "phase3-done" })
    Write-JsonFile (Join-Path $runs "r1\orchestration\ledger.json") ([pscustomobject]@{ entries = @() })
    Write-JsonFile (Join-Path $runs "r2\status.json") ([pscustomobject]@{ state = "phase2-done" })
    $saved = $env:UPGRADELENS_INVOKER

    It "refuses a stage of an orchestrated run that does not come from run-stage.ps1" {
        $env:UPGRADELENS_INVOKER = $null
        Resolve-RunId -RunsRoot $runs -RunId "r1" -States @("phase3-done") | Should BeNullOrEmpty
        $env:UPGRADELENS_INVOKER = "run-stage"
        Resolve-RunId -RunsRoot $runs -RunId "r1" -States @("phase3-done") | Should Be "r1"
    }
    It "refuses a run in the wrong state, unless -AnyState" {
        $env:UPGRADELENS_INVOKER = $null
        Resolve-RunId -RunsRoot $runs -RunId "r2" -States @("phase7-done") | Should BeNullOrEmpty
        Resolve-RunId -RunsRoot $runs -RunId "r2" -States @("phase7-done") -AnyState | Should Be "r2"
    }
    $env:UPGRADELENS_INVOKER = $saved
}

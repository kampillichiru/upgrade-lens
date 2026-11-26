<#
.SYNOPSIS
  Stage 5 of UpgradeLens (behavior checks): starts the application on both branches, compares what it
  does, runs the proposed tests on master and on change_branch, and records what cannot be run here.

.DESCRIPTION
  Steps, in order:
    1. startup     starts each side's jar with its own JDK on a free port, waits for /actuator/health
                   to report UP, records startup time and heap (jcmd GC.heap_info)
    2. dumps       /actuator/beans (bean names and types) and /actuator/env (application config),
                   diffed between the two sides
    3. probes      config\probes.json, one request at a time on a fresh server per side; compares status,
                   content type and body (timestamps masked). Probe errors are recorded as errors.
    4. tests       the proposed tests (artifacts\agent\tests-manifest.json, written by the test writer).
                   Each test must pass on master. On change_branch it is run unchanged, and then once per
                   injected regression (a small find-and-replace on a main file, restored afterwards); the
                   test must fail for each injection to count as a regression detector.
    5. gaps        what this stage did not run, and why (Testcontainers, mutation testing, migrations)
  Writes artifacts\behavior-results.json and artifacts\gaps.json.

  The servers run from the worktrees of the run. The proposed tests are copied into the worktrees for their
  run and removed afterwards (tests that stage 8 placed in the head worktree on an earlier pass are taken out
  first, and stage 8 places them again). Injected regressions are restored byte for byte (checked by hash).

  Exit codes:
    0  stage ran (the verdict decides what the results mean)
    2  usage or run problem (no run, stage 4 not done)
    3  a tool could not run (no JDK tool, Maven, or no jar)

.PARAMETER RunId
  Run folder under runs\. Defaults to the newest run whose stage 4 is done.

.PARAMETER StartupTimeoutSeconds
  How long to wait for /actuator/health to report UP. Default: timeouts.startupSeconds, else 120.

.PARAMETER Config
  Config file (for the runs folder). Defaults to $env:UPGRADELENS_CONFIG, then config\upgrade-lens.json.

.EXAMPLE
  .\scripts\phase5-behavior.ps1 -RunId 20261126-131717
#>
param(
    [string]$RunId,
    [int]$StartupTimeoutSeconds = 0,
    [string]$Config
)

$ErrorActionPreference = "Stop"
$ToolVersion = "1.0.0"
$RootDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "lib\core.ps1")
. (Join-Path $PSScriptRoot "lib\analysis.ps1")

$localConfig = Resolve-ConfigPath -Config $Config -RootDir $RootDir
if (-not (Test-Path -LiteralPath $localConfig)) { Write-Host "Config not found: $localConfig"; exit 2 }
$liveCfg = Read-JsonFile $localConfig
$runsRoot = Get-RunsRoot -Cfg $liveCfg -RootDir $RootDir
$RunId = Resolve-RunId -RunsRoot $runsRoot -RunId $RunId -States @("phase4-done", "phase5-done", "phase6-done", "phase7-done", "phase8-done", "phase9-done", "verdict-blocked", "verdict-listed", "verdict-safe")
if (-not $RunId) { Write-Host "No run with stage 4 done under $runsRoot. Run phase4-impact.ps1 first."; exit 2 }

$RunDir = Join-Path $runsRoot $RunId
$ArtifactsDir = Join-Path $RunDir "artifacts"
$LogsDir = Join-Path $RunDir "logs"
$StatusFile = Join-Path $RunDir "status.json"
$script:StageLog = Join-Path $LogsDir "phase5.log"
$cfg = Read-JsonFile (Join-Path $RunDir "config\upgrade-lens.json")
$envRec = Read-JsonFile (Join-Path $ArtifactsDir "environment.json")
$status = Read-JsonFile $StatusFile
$impact = Read-JsonFile (Join-Path $ArtifactsDir "impact-findings.json")
$reach = Read-JsonFile (Join-Path $ArtifactsDir "reach.json")
$inventory = Read-JsonFile (Join-Path $ArtifactsDir "change-inventory.json")
if (-not $cfg -or -not $envRec -or -not $status -or -not $impact -or -not $reach) { Write-Host "Run incomplete: $RunDir (needs stage 4)"; exit 2 }
if ($StartupTimeoutSeconds -le 0) { $StartupTimeoutSeconds = [int](Get-ConfigValue $cfg "timeouts.startupSeconds" 120) }
# The probe manifest: the configured file, snapshotted into the run so the report shows what was run.
$probesSrc = Resolve-ProjectPath -Path ([string](Get-ConfigValue $liveCfg "probesFile" "config\probes.json")) -RootDir $RootDir
$probesRun = Join-Path $RunDir "config\probes.json"
if (Test-Path -LiteralPath $probesSrc) { Copy-Item -LiteralPath $probesSrc -Destination $probesRun -Force }
$probeManifest = Read-JsonFile $probesRun
if (-not $probeManifest) { Write-Host "Probe manifest not found: $probesSrc"; exit 2 }
$settingsArg = $null
if ($cfg.maven.settingsFile -and (Test-Path -LiteralPath $cfg.maven.settingsFile)) { $settingsArg = $cfg.maven.settingsFile }

function Stop-Stage([int]$Code, [string]$Reason) {
    Write-StageLog $Reason
    Set-StageStep $status "behavior" "failed" $Reason
    Set-RunState $status "failed-tool" "phase5"
    Write-JsonFile $StatusFile $status
    exit $Code
}

Write-StageLog "phase 5 behavior checks for run $RunId"
$sides = @(
    [pscustomobject]@{ Side = "base"; Branch = $cfg.baseBranch; Jdk = $cfg.java.base; Worktree = $envRec.branches.base.worktree },
    [pscustomobject]@{ Side = "head"; Branch = $cfg.headBranch; Jdk = $cfg.java.head; Worktree = $envRec.branches.head.worktree }
)
foreach ($s in $sides) {
    $jar = @(Get-ChildItem -LiteralPath (Join-Path $s.Worktree "target") -Filter *.jar -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike "*.original" })
    if ($jar.Count -eq 0) { Stop-Stage 3 "$($s.Side): no jar in $($s.Worktree)\target. Run stage 1 first." }
    $s | Add-Member -NotePropertyName Jar -NotePropertyValue $jar[0].FullName
    $s | Add-Member -NotePropertyName Java -NotePropertyValue (Join-Path $s.Jdk "bin\java.exe")
    $s | Add-Member -NotePropertyName Jcmd -NotePropertyValue (Join-Path $s.Jdk "bin\jcmd.exe")
    if (-not (Test-Path -LiteralPath $s.Java)) { Stop-Stage 3 "$($s.Side): no java.exe under $($s.Jdk)" }
}

# ---------- helpers ----------

function Get-FreePort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Get-ResponseText($Content) {
    # Windows PowerShell 5.1 returns byte[] for some JSON responses without a charset. Decode as UTF-8.
    if ($Content -is [byte[]]) { return [System.Text.Encoding]::UTF8.GetString($Content) }
    return [string]$Content
}

function Get-MaskedBody([string]$Text) {
    # Timestamps differ between runs by design, so they are masked before comparing.
    if ($null -eq $Text) { return "" }
    $t = $Text -replace '"timestamp"\s*:\s*"[^"]*"', '"timestamp":"<masked>"'
    $t = $t -replace '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?', '<masked-time>'
    return $t.Trim()
}

function Invoke-Probe([int]$Port, $Probe) {
    # Returns status, content type and masked body. Connection failures are returned as an error.
    $uri = "http://localhost:$Port$($Probe.path)"
    $headers = @{}
    if ($Probe.headers) { foreach ($p in $Probe.headers.PSObject.Properties) { $headers[$p.Name] = $p.Value } }
    $params = @{ Uri = $uri; Method = $Probe.method; Headers = $headers; UseBasicParsing = $true; TimeoutSec = 15 }
    if ($Probe.body) { $params.Body = $Probe.body; $params.ContentType = "application/json" }
    try {
        $r = Invoke-WebRequest @params
        return [pscustomobject]@{ status = [int]$r.StatusCode; contentType = [string]$r.Headers["Content-Type"]; body = (Get-MaskedBody (Get-ResponseText $r.Content)) }
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($null -eq $resp) { return [pscustomobject]@{ error = $_.Exception.Message } }
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $text = $reader.ReadToEnd()
        return [pscustomobject]@{ status = [int]$resp.StatusCode; contentType = [string]$resp.ContentType; body = (Get-MaskedBody $text) }
    } catch {
        return [pscustomobject]@{ error = $_.Exception.Message }
    }
}

function Start-AppSide($Side, [int]$Port) {
    # Starts the jar, waits for UP. Returns an object with the process, startup ms, and an error if any.
    $outLog = Join-Path $LogsDir "app-$($Side.Side).out.log"
    $errLog = Join-Path $LogsDir "app-$($Side.Side).err.log"
    $argLine = "-jar `"$($Side.Jar)`" --server.port=$Port --management.endpoints.web.exposure.include=health,beans,env --management.endpoint.health.show-details=never"
    $started = Get-Date
    $proc = Start-Process -FilePath $Side.Java -ArgumentList $argLine -WorkingDirectory $Side.Worktree `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $deadline = $started.AddSeconds($StartupTimeoutSeconds)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { break }
        try {
            $h = Invoke-WebRequest -Uri "http://localhost:$Port/actuator/health" -UseBasicParsing -TimeoutSec 3
            if ((Get-ResponseText $h.Content) -match '"status"\s*:\s*"UP"') { $up = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    $ms = [int]((Get-Date) - $started).TotalMilliseconds
    $heap = ""
    if ($up -and (Test-Path -LiteralPath $Side.Jcmd)) {
        $hr = Invoke-Native $Side.Jcmd @([string]$proc.Id, "GC.heap_info")
        $heap = (@($hr.Output | Where-Object { $_ -match 'total' }) | Select-Object -First 1)
    }
    $err = $null
    if (-not $up) {
        $err = if ($proc.HasExited) { "process exited with code $($proc.ExitCode); see $errLog" } else { "not UP after $StartupTimeoutSeconds s; see $errLog" }
    }
    return [pscustomobject]@{ Process = $proc; Up = $up; StartupMs = $ms; Heap = $heap; Error = $err; Port = $Port }
}

function Stop-AppSide($Handle) {
    if ($Handle -and $Handle.Process -and -not $Handle.Process.HasExited) {
        Stop-Process -Id $Handle.Process.Id -Force -ErrorAction SilentlyContinue
        $Handle.Process.WaitForExit(15000) | Out-Null
    }
}

function Get-BeanSet($Json) {
    # Bean name -> type. A name can repeat across contexts only with the same type in practice; the first wins.
    $map = @{}
    if ($null -eq $Json.contexts) { return $map }
    foreach ($ctx in $Json.contexts.PSObject.Properties) {
        if ($null -eq $ctx.Value.beans) { continue }
        foreach ($b in $ctx.Value.beans.PSObject.Properties) {
            if (-not $map.ContainsKey($b.Name)) { $map[$b.Name] = [string]$b.Value.type }
        }
    }
    return $map
}

function Get-AppProperties($Json) {
    $map = @{}
    foreach ($src in @($Json.propertySources)) {
        if ($src.name -notlike "applicationConfig*") { continue }
        foreach ($p in $src.properties.PSObject.Properties) { $map[$p.Name] = [string]$p.Value.value }
    }
    return $map
}

# ---------- 1 to 3: startup, dumps, probes ----------

$port = @{}
$handles = @{}
$serverResults = @{}
$probeRows = New-Object System.Collections.Generic.List[object]
$probeResults = @{}
$dumps = @{}
$startupOk = $true

try {
    foreach ($s in $sides) {
        $port[$s.Side] = Get-FreePort
        Write-StageLog "$($s.Side): starting $($s.Jar) on port $($port[$s.Side]) with $($s.Jdk)"
        $h = Start-AppSide -Side $s -Port $port[$s.Side]
        $handles[$s.Side] = $h
        $serverResults[$s.Side] = [ordered]@{
            port = $h.Port; up = $h.Up; startupMs = $h.StartupMs; heap = $h.Heap; error = $h.Error; jdk = $s.Jdk
        }
        if (-not $h.Up) {
            $startupOk = $false
            Write-StageLog "$($s.Side): startup failed: $($h.Error)"
            continue
        }
        Write-StageLog "$($s.Side): UP in $($h.StartupMs) ms. $($h.Heap)"

        $beans = Invoke-RestMethod -Uri "http://localhost:$($h.Port)/actuator/beans" -TimeoutSec 30
        $env = Invoke-RestMethod -Uri "http://localhost:$($h.Port)/actuator/env" -TimeoutSec 30
        $dumps[$s.Side] = [pscustomobject]@{ beans = (Get-BeanSet $beans); props = (Get-AppProperties $env) }

        $results = New-Object System.Collections.Generic.List[object]
        foreach ($p in @($probeManifest.probes)) {
            $r = Invoke-Probe -Port $h.Port -Probe $p
            $results.Add([pscustomobject]@{ id = $p.id; result = $r })
        }
        $probeResults[$s.Side] = $results.ToArray()
        Write-StageLog "$($s.Side): $($results.Count) probe(s) run"
    }
} catch {
    Write-StageLog "stage 5 server step threw: $($_.Exception.Message)"
    $startupOk = $false
} finally {
    foreach ($k in @($handles.Keys)) { Stop-AppSide $handles[$k] }
}

# Compare the two sides, probe by probe.
$checks = New-Object System.Collections.Generic.List[object]
$blockingErrors = New-Object System.Collections.Generic.List[string]
foreach ($p in @($probeManifest.probes)) {
    $b = $null; $hd = $null
    if ($probeResults.ContainsKey("base")) { $b = @($probeResults["base"]) | Where-Object { $_.id -eq $p.id } | Select-Object -First 1 }
    if ($probeResults.ContainsKey("head")) { $hd = @($probeResults["head"]) | Where-Object { $_.id -eq $p.id } | Select-Object -First 1 }
    $diffs = New-Object System.Collections.Generic.List[string]
    $outcome = "same"
    if (-not $b -or -not $hd -or $b.result.error -or $hd.result.error) {
        $outcome = "error"
        $blockingErrors.Add("probe $($p.id): " + ((@($b.result.error, $hd.result.error) | Where-Object { $_ }) -join "; "))
    } else {
        if ($b.result.status -ne $hd.result.status) { $diffs.Add("status $($b.result.status) -> $($hd.result.status)") }
        if (($b.result.contentType -replace '\s', '').ToLower() -ne ($hd.result.contentType -replace '\s', '').ToLower()) {
            $diffs.Add("content-type '$($b.result.contentType)' -> '$($hd.result.contentType)'")
        }
        if ($b.result.body -ne $hd.result.body) { $diffs.Add("body differs") }
        if ($diffs.Count -gt 0) { $outcome = "different" }
    }
    foreach ($rule in @($p.linkedRules)) {
        $checks.Add([pscustomobject]@{ ruleId = $rule; source = "probe"; id = $p.id; outcome = $outcome })
    }
    $probeRows.Add([pscustomobject]@{
        id         = $p.id
        flowId     = $p.flowId
        request    = "$($p.method) $($p.path)"
        linkedRules = @($p.linkedRules)
        outcome    = $outcome
        differences = $diffs.ToArray()
        base       = $(if ($b) { $b.result } else { $null })
        head       = $(if ($hd) { $hd.result } else { $null })
    })
}

# Dump differences: beans and application properties.
$dumpDiff = [ordered]@{ available = ($dumps.ContainsKey("base") -and $dumps.ContainsKey("head")) }
if ($dumpDiff.available) {
    # Beans are compared by name. A bean whose name stays but whose class moved is a type change.
    $bb = $dumps["base"].beans; $hb = $dumps["head"].beans
    $dumpDiff.beans = [ordered]@{
        base        = $bb.Count
        head        = $hb.Count
        added       = @($hb.Keys | Where-Object { -not $bb.ContainsKey($_) } | Sort-Object)
        removed     = @($bb.Keys | Where-Object { -not $hb.ContainsKey($_) } | Sort-Object)
        typeChanged = @($bb.Keys | Where-Object { $hb.ContainsKey($_) -and $bb[$_] -ne $hb[$_] } | Sort-Object)
    }
    $bp = $dumps["base"].props; $hp = $dumps["head"].props
    $dumpDiff.properties = [ordered]@{
        added   = @($hp.Keys | Where-Object { -not $bp.ContainsKey($_) } | Sort-Object)
        removed = @($bp.Keys | Where-Object { -not $hp.ContainsKey($_) } | Sort-Object)
        changed = @($bp.Keys | Where-Object { $hp.ContainsKey($_) -and $bp[$_] -ne $hp[$_] } | Sort-Object)
    }
    Write-StageLog ("dumps: beans {0} -> {1} (added {2}, removed {3}, type changed {4}); app properties changed {5}" -f `
        $bb.Count, $hb.Count, $dumpDiff.beans.added.Count, $dumpDiff.beans.removed.Count, $dumpDiff.beans.typeChanged.Count, $dumpDiff.properties.changed.Count)
}

# ---------- 4. proposed tests ----------

$manifestPath = Join-Path $ArtifactsDir "agent\tests-manifest.json"
$testRows = New-Object System.Collections.Generic.List[object]
$testState = "not-proposed"

# Tests that stage 8 placed in the head worktree on an earlier pass: take them out so the run below starts
# from change_branch's own files (stage 8 places them again). Only files whose bytes are still the ones
# stage 8 wrote are removed; anything else is left alone and reported.
$placedRecord = Read-JsonFile (Join-Path $ArtifactsDir "new-tests.json")
if ($placedRecord -and $placedRecord.placed) {
    foreach ($p in @($placedRecord.placed)) {
        $dst = Join-Path $envRec.branches.head.worktree ($p.relativePath -replace '/', '\')
        if (-not (Test-Path -LiteralPath $dst)) { continue }
        if ((Get-FileSha256 $dst) -eq $p.sha256) { Remove-Item -LiteralPath $dst -Force; Write-StageLog "tests: removed $($p.relativePath) placed by stage 8, before the run" }
        else { Write-StageLog "tests: $($p.relativePath) in the head worktree was edited after stage 8 placed it; left as is" }
    }
}

function Invoke-TestClasses($Side, [string[]]$ClassNames) {
    # Runs the named test classes on one worktree; returns per-class results from the surefire XML.
    $wt = $Side.Worktree
    $log = Join-Path $LogsDir "tests-$($Side.Side)-$(Get-Date -Format 'HHmmssfff').log"
    $mvnArgs = @("-B", "-ntp")
    if ($settingsArg) { $mvnArgs += @("-s", $settingsArg) }
    $mvnArgs += @("-f", (Join-Path $wt "pom.xml"), "test", "-Dtest=$($ClassNames -join ',')", "-Dsurefire.failIfNoSpecifiedTests=false")
    $r = Invoke-WithJdk -JdkHome $Side.Jdk -Action { Invoke-Native $cfg.maven.command $mvnArgs }
    [System.IO.File]::WriteAllLines($log, [string[]]$r.Output)
    $result = @{}
    foreach ($c in $ClassNames) {
        # Surefire names the report with the package-qualified class: TEST-com.example.orders.Foo.xml
        $found = @(Get-ChildItem -LiteralPath (Join-Path $wt "target\surefire-reports") -Filter "TEST-*.$c.xml" -File -ErrorAction SilentlyContinue)
        $xml = if ($found.Count -gt 0) { $found[0].FullName } else { Join-Path $wt "target\surefire-reports\TEST-$c.xml" }
        if (-not (Test-Path -LiteralPath $xml)) { $result[$c] = [pscustomobject]@{ ran = $false; passed = $false; detail = "no surefire report (mvn exit $($r.ExitCode)); see $log" }; continue }
        [xml]$x = Get-Content -LiteralPath $xml -Raw
        $suite = $x.testsuite
        $failed = [int]$suite.failures + [int]$suite.errors
        $detail = if ($failed -gt 0) { (@($suite.testcase | Where-Object { $_.failure -or $_.error } | ForEach-Object { $_.name }) -join ", ") } else { "$($suite.tests) test(s) passed" }
        $result[$c] = [pscustomobject]@{ ran = $true; passed = ($failed -eq 0 -and [int]$suite.tests -gt 0); detail = $detail; log = $log }
    }
    return $result
}

if (Test-Path -LiteralPath $manifestPath) {
    $proposal = Get-AgentProposal -RunDir $RunDir -Step "tests" -FileName "tests-manifest.json"
    $manifest = $proposal.doc
    if ($proposal.state -ne "used") {
        $testState = "stale"
        Write-StageLog "tests: the test writer's output is stale (findings or reach changed). Not run."
    } else {
        $testState = "run"
        $placed = New-Object System.Collections.Generic.List[object]
        try {
            # Copy the proposed test files into both worktrees. A path that already exists is a conflict.
            # Several tests can share one class file: copy each file once per side.
            $relPaths = @($manifest.tests | ForEach-Object { $_.relativePath } | Sort-Object -Unique)
            foreach ($rel in $relPaths) {
                $src = Join-Path $ArtifactsDir ("agent\tests\" + ($rel -replace '/', '\'))
                if (-not (Test-Path -LiteralPath $src)) { throw "proposed test file missing: $src" }
                foreach ($s in $sides) {
                    $dst = Join-Path $s.Worktree ($rel -replace '/', '\')
                    if (Test-Path -LiteralPath $dst) { throw "$($s.Side): $rel already exists in the worktree" }
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
                    Copy-Item -LiteralPath $src -Destination $dst
                    $placed.Add($dst)
                }
            }
            $classes = @($manifest.tests | ForEach-Object { $_.className } | Sort-Object -Unique)
            $baseSide = $sides[0]; $headSide = $sides[1]
            $baseRes = Invoke-TestClasses -Side $baseSide -ClassNames $classes
            $headRes = Invoke-TestClasses -Side $headSide -ClassNames $classes

            foreach ($t in @($manifest.tests)) {
                $injections = New-Object System.Collections.Generic.List[object]
                foreach ($inj in @($t.injections)) {
                    $target = Join-Path $headSide.Worktree ($inj.file -replace '/', '\')
                    if (-not (Test-Path -LiteralPath $target)) {
                        $injections.Add([pscustomobject]@{ note = $inj.note; applied = $false; detected = $false; detail = "file not found in the head worktree: $($inj.file)" })
                        continue
                    }
                    # Keep the exact bytes (BOM, line endings) so the restore is byte for byte.
                    $originalBytes = [System.IO.File]::ReadAllBytes($target)
                    $original = [System.IO.File]::ReadAllText($target, $script:Utf8NoBom)
                    $occurrences = ([regex]::Matches($original, [regex]::Escape($inj.find))).Count
                    if ($occurrences -ne 1) {
                        $injections.Add([pscustomobject]@{ note = $inj.note; applied = $false; detected = $false; detail = "find text occurs $occurrences time(s), needs exactly 1" })
                        continue
                    }
                    $originalHash = Get-FileSha256 $target
                    try {
                        [System.IO.File]::WriteAllText($target, $original.Replace($inj.find, $inj.replace), $script:Utf8NoBom)
                        $ir = Invoke-TestClasses -Side $headSide -ClassNames @($t.className)
                        $res = $ir[$t.className]
                        $detected = ($res.ran -and -not $res.passed)
                        $injections.Add([pscustomobject]@{ note = $inj.note; applied = $true; detected = $detected; detail = $res.detail })
                    } finally {
                        [System.IO.File]::WriteAllBytes($target, $originalBytes)
                        if ((Get-FileSha256 $target) -ne $originalHash) { throw "could not restore $($inj.file) after injection" }
                    }
                }

                $b = $baseRes[$t.className]; $h = $headRes[$t.className]
                $testRows.Add([pscustomobject]@{
                    id          = $t.id
                    className   = $t.className
                    relativePath = $t.relativePath
                    purpose     = [string]$t.purpose
                    gap         = [string]$t.gap
                    linkedRules = @($t.linkedRules)
                    flowIds     = @($t.flowIds)
                    methods     = @($t.methods)
                    base        = [pscustomobject]@{ passed = $b.passed; detail = $b.detail }
                    head        = [pscustomobject]@{ passed = $h.passed; detail = $h.detail }
                    injections  = $injections.ToArray()
                    # Validated: passes on master, passes unchanged on change_branch, and fails for every injected regression
                    # (at least one injection is required; a test that cannot detect a regression proves nothing).
                    detectsInjected = ($injections.Count -gt 0 -and @($injections | Where-Object { -not ($_.applied -and $_.detected) }).Count -eq 0)
                    validated   = ($b.passed -and $h.passed -and $injections.Count -gt 0 -and @($injections | Where-Object { -not ($_.applied -and $_.detected) }).Count -eq 0)
                })
                # Checks for the findings the test is linked to. A difference needs master to pass and change_branch
                # to fail. A test that fails on master is "unverified" (not written against master), not a change.
                foreach ($rule in @($t.linkedRules)) {
                    $outcome = if (-not $h.ran -or -not $b.ran) { "error" }
                               elseif ($b.passed -and -not $h.passed) { "different" }
                               elseif (-not $b.passed) { "unverified" }
                               else { "same" }
                    $checks.Add([pscustomobject]@{ ruleId = $rule; source = "test"; id = $t.id; outcome = $outcome })
                }
                if (-not $h.ran) { $blockingErrors.Add("test $($t.id) did not run on head: $($h.detail)") }
            }
        } catch {
            Write-StageLog "tests: $($_.Exception.Message)"
            $blockingErrors.Add("test run: $($_.Exception.Message)")
        } finally {
            foreach ($p in $placed) { if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force } }
        }
    }
} else {
    Write-StageLog "tests: no proposed tests (artifacts\agent\tests-manifest.json missing)"
}

# ---------- 5. gaps ----------

$changedMain = @($inventory.files | Where-Object { $_.category -eq "main-code" }).Count
$gaps = New-Object System.Collections.Generic.List[object]
$gaps.Add([pscustomobject]@{
    stage    = "behavior"
    item     = "Integration tests with Testcontainers"
    reason   = "Not run. Docker is not used on this machine. The approach (section 6) leaves the Testcontainers method open, to be decided."
    blocking = $false
})
if ($changedMain -gt 0) {
    $gaps.Add([pscustomobject]@{
        stage    = "behavior"
        item     = "Mutation testing on changed classes"
        reason   = "Not run. $changedMain changed main file(s) exist, but the PIT step is not wired yet."
        blocking = $false
    })
}
$libDiff = Read-JsonFile (Join-Path $ArtifactsDir "library-api-diff.json")
if (-not $libDiff -or -not $libDiff.available) {
    $gaps.Add([pscustomobject]@{
        stage    = "dependencies"
        item     = "Library API diff"
        reason   = "Not available: $(if ($libDiff) { $libDiff.reason } else { 'stage 3 did not write library-api-diff.json' })."
        blocking = $false
    })
}
$depDiffDoc = Read-JsonFile (Join-Path $ArtifactsDir "dependency-diff.json")
if ($depDiffDoc -and -not $depDiffDoc.verboseTree) {
    $gaps.Add([pscustomobject]@{
        stage    = "dependencies"
        item     = "Dependency version conflicts"
        reason   = "Not listed: the maven-dependency-plugin of this build cannot print a verbose tree."
        blocking = $false
    })
}
$buildsDoc = Read-JsonFile (Join-Path $ArtifactsDir "build-results.json")
foreach ($sideName in @("base", "head")) {
    if ($buildsDoc -and $buildsDoc.sides.$sideName.integrationTests.mode -eq "not-run") {
        $gaps.Add([pscustomobject]@{
            stage    = "build"
            item     = "Integration tests on $sideName"
            reason   = "Not run: the pom binds no maven-failsafe-plugin and build.integrationTests is failsafe-only."
            blocking = $false
        })
    }
}
$gapsOut = $gaps.ToArray()

# ---------- write ----------

$behavior = [ordered]@{
    schemaVersion = 1
    toolVersion   = $ToolVersion
    runId         = $RunId
    servers       = $serverResults
    startupOk     = $startupOk
    dumps         = $dumpDiff
    probes        = $probeRows.ToArray()
    probeSummary  = [ordered]@{
        total     = $probeRows.Count
        same      = @($probeRows | Where-Object { $_.outcome -eq "same" }).Count
        different = @($probeRows | Where-Object { $_.outcome -eq "different" }).Count
        error     = @($probeRows | Where-Object { $_.outcome -eq "error" }).Count
    }
    testState     = $testState
    tests         = $testRows.ToArray()
    checks        = $checks.ToArray()
    blockingErrors = $blockingErrors.ToArray()
}
Write-JsonFile (Join-Path $ArtifactsDir "behavior-results.json") $behavior
Write-JsonFile (Join-Path $ArtifactsDir "gaps.json") ([ordered]@{ schemaVersion = 1; runId = $RunId; gaps = $gapsOut })

Write-StageLog ("probes: {0} same, {1} different, {2} error. Tests: state {3}, {4} proposed." -f `
    $behavior.probeSummary.same, $behavior.probeSummary.different, $behavior.probeSummary.error, $testState, $testRows.Count)
foreach ($row in @($probeRows | Where-Object { $_.outcome -ne "same" })) {
    Write-StageLog ("  {0} {1}: {2} [{3}]" -f $row.outcome, $row.id, ($row.differences -join "; "), ($row.linkedRules -join ","))
}
foreach ($g in $gapsOut) { Write-StageLog ("gap: {0}: {1}" -f $g.item, $g.reason) }

$stageState = if ($startupOk) { "done" } else { "done-with-errors" }
Set-StageStep $status "behavior" $stageState "" (Join-Path $ArtifactsDir "behavior-results.json")
Set-RunState $status "phase5-done" "phase5"
Write-JsonFile $StatusFile $status
Write-StageLog "phase 5 done: behavior results written for run $RunId"
exit 0

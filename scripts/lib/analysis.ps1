<#
  UpgradeLens analysis functions for stages 3 to 10: parsers, rule matching, flow reach and
  coverage, finding status and report helpers. Pure functions, so tests\upgradelens.Tests.ps1 can call them.
  Dot-source after lib\core.ps1.
#>

function ConvertFrom-DependencyAnalyze {
    # maven-dependency-plugin prints two blocks of "[WARNING]    group:artifact:type:version:scope" lines.
    param([string[]]$Lines)
    $unused = New-Object System.Collections.Generic.List[string]
    $undeclared = New-Object System.Collections.Generic.List[string]
    $mode = ""
    foreach ($l in $Lines) {
        if ($l -match 'Unused declared dependencies found') { $mode = "unused"; continue }
        if ($l -match 'Used undeclared dependencies found') { $mode = "undeclared"; continue }
        if ($mode -and $l -match '^\[WARNING\]\s+([A-Za-z0-9_.\-]+:\S+)\s*$') {
            if ($mode -eq "unused") { $unused.Add($Matches[1]) } else { $undeclared.Add($Matches[1]) }
            continue
        }
        if ($mode) { $mode = "" }
    }
    return [pscustomobject]@{ unused = $unused.ToArray(); undeclared = $undeclared.ToArray() }
}

function Read-UnifiedPatch {
    # Parses "git diff -U0" output. Returns a hashtable: path -> @{ Added = @({Line;Text}); Removed = @(...) }.
    # Line numbers are on each side's own numbering (new for added, old for removed).
    param([string[]]$Lines)
    $files = @{}
    $entry = $null
    $inHunk = $false
    $oldLn = 0
    $newLn = 0
    foreach ($raw in $Lines) {
        $l = ([string]$raw).TrimEnd("`r")
        if ($l.StartsWith("diff --git ")) { $entry = $null; $inHunk = $false; continue }
        if ($inHunk -and $entry) {
            if ($l.StartsWith("+")) { $entry.Added.Add([pscustomobject]@{ Line = $newLn; Text = $l.Substring(1) }); $newLn++; continue }
            if ($l.StartsWith("-")) { $entry.Removed.Add([pscustomobject]@{ Line = $oldLn; Text = $l.Substring(1) }); $oldLn++; continue }
            if ($l.StartsWith("\")) { continue }
        }
        if ($l -match '^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@') {
            $oldLn = [int]$Matches[1]; $newLn = [int]$Matches[2]; $inHunk = $true; continue
        }
        if ($l -match '^\+\+\+ b/(.+)$') {
            $path = $Matches[1]
            $entry = @{ Added = (New-Object System.Collections.Generic.List[object]); Removed = (New-Object System.Collections.Generic.List[object]) }
            $files[$path] = $entry
            $inHunk = $false
            continue
        }
    }
    return $files
}

function ConvertTo-GroupKey([string]$Coordinate) {
    # "g:a:jar:v:scope" -> "g:a"
    $p = $Coordinate.Split(':')
    if ($p.Count -ge 2) { return "$($p[0]):$($p[1])" }
    return $Coordinate
}

function ConvertTo-SymbolKey {
    # "method:X" in a file Foo.java -> "Foo#X"; "type:X" -> "X#*"; $null -> none.
    param([string]$Symbol, [string]$FileSimpleName)
    if (-not $Symbol) { return @() }
    if ($Symbol -like "method:*") { return @("$FileSimpleName#" + $Symbol.Substring(7)) }
    if ($Symbol -like "type:*") { return @($Symbol.Substring(5) + "#*") }
    return @()
}

function New-RuleFinding {
    param($Rule, [string]$File, $Line, [string]$Evidence, [string[]]$Symbols = @(), [string]$Scope)
    $effective = $Scope
    if ($effective -eq "symbol" -and $Symbols.Count -eq 0) { $effective = "global" }
    return [pscustomobject]@{
        ruleId   = $Rule.id
        title    = $Rule.title
        severity = $Rule.severity
        scope    = $effective
        file     = $File
        line     = $Line
        symbols  = @($Symbols)
        evidence = $Evidence
        impact   = $Rule.impact
        check    = $Rule.check
        source   = "rule"
    }
}

function Get-RuleFindings {
    # One rule against the context. Context fields:
    #   Patch (Read-UnifiedPatch output), HeadLines and BaseLines (path -> lines, for symbol lookup),
    #   Inventory (change-inventory files), InventoryFindings, DepDiff, DepAnalyze (base and head),
    #   ApiDiff, ItFiles (relative paths of *IT.java in head), HasFailsafe.
    param($Rule, $Ctx)
    $out = New-Object System.Collections.Generic.List[object]

    switch ($Rule.source) {
        { $_ -in @("diff-added", "diff-removed", "diff-any") } {
            $sides = switch ($Rule.source) {
                "diff-added" { @("added") }
                "diff-removed" { @("removed") }
                default { @("added", "removed") }
            }
            foreach ($path in @($Ctx.Patch.Keys | Sort-Object)) {
                if (-not (Test-RuleApplies -Rule $Rule -Path $path)) { continue }
                $entry = $Ctx.Patch[$path]
                $simple = [System.IO.Path]::GetFileNameWithoutExtension($path)
                foreach ($side in $sides) {
                    $items = if ($side -eq "added") { $entry.Added } else { $entry.Removed }
                    $lines = if ($side -eq "added") { $Ctx.HeadLines[$path] } else { $Ctx.BaseLines[$path] }
                    foreach ($ln in $items) {
                        if ($ln.Text -notmatch $Rule.pattern) { continue }
                        $sym = if ($path -like "*.java") { Get-SymbolForLine -Lines $lines -LineNo $ln.Line } else { $null }
                        $symKeys = ConvertTo-SymbolKey -Symbol $sym -FileSimpleName $simple
                        $evidence = "{0} line {1}: {2}" -f $side, $ln.Line, $ln.Text.Trim()
                        $out.Add((New-RuleFinding -Rule $Rule -File $path -Line $ln.Line -Evidence $evidence -Symbols $symKeys -Scope $Rule.scope))
                    }
                }
            }
        }
        "inventory" {
            foreach ($f in @($Ctx.Inventory | Where-Object { $_.category -eq $Rule.category })) {
                $items = @($f.($Rule.field))
                if ($items.Count -eq 0) { continue }
                $simple = [System.IO.Path]::GetFileNameWithoutExtension($f.path)
                $symKeys = @(@($f.changedMethods) | ForEach-Object { "$simple#$_" })
                if ($symKeys.Count -eq 0) { $symKeys = @("$simple#*") }
                $evidence = ($items | Select-Object -First 4) -join "; "
                $out.Add((New-RuleFinding -Rule $Rule -File $f.path -Line $null -Evidence $evidence -Symbols $symKeys -Scope $Rule.scope))
            }
        }
        "inventory-findings" {
            foreach ($f in @($Ctx.InventoryFindings | Where-Object { $Rule.types -contains $_.type })) {
                $out.Add((New-RuleFinding -Rule $Rule -File $f.file -Line $null -Evidence "$($f.type): $($f.detail)" -Scope $Rule.scope))
            }
        }
        "dependency" {
            $items = switch ($Rule.match) {
                "major-direct" { @($Ctx.DepDiff.changed | Where-Object { $_.direct -and $_.major }) }
                "major-transitive" { @($Ctx.DepDiff.changed | Where-Object { -not $_.direct -and $_.major }) }
                "added-direct" { @($Ctx.DepDiff.added | Where-Object { $_.direct }) }
                "removed-direct" { @($Ctx.DepDiff.removed | Where-Object { $_.direct }) }
                default { @() }
            }
            if ($items.Count -gt 0) {
                # Dependency findings are grouped per rule: one row lists every artifact in the group.
                $evidence = ($items | ForEach-Object {
                    if ($_.PSObject.Properties.Name -contains "base") { "$($_.ga) $($_.base) -> $($_.head)" }
                    else { "$($_.ga) $($_.version)" }
                }) -join "; "
                $out.Add((New-RuleFinding -Rule $Rule -File "pom.xml" -Line $null -Evidence "$($items.Count) artifact(s): $evidence" -Scope $Rule.scope))
            }
        }
        "dependency-analyze" {
            $baseKeys = @($Ctx.DepAnalyze.base.undeclared | ForEach-Object { ConvertTo-GroupKey $_ })
            $baseUnused = @($Ctx.DepAnalyze.base.unused | ForEach-Object { ConvertTo-GroupKey $_ })
            if ($Rule.match -eq "undeclared-new") {
                $items = @($Ctx.DepAnalyze.head.undeclared | Where-Object { $baseKeys -notcontains (ConvertTo-GroupKey $_) })
            } else {
                $items = @($Ctx.DepAnalyze.head.unused | Where-Object { $baseUnused -notcontains (ConvertTo-GroupKey $_) })
            }
            if ($items.Count -gt 0) {
                $out.Add((New-RuleFinding -Rule $Rule -File "pom.xml" -Line $null -Evidence "$($items.Count) new: $($items -join '; ')" -Scope $Rule.scope))
            }
        }
        "api" {
            foreach ($c in @($Ctx.ApiDiff.classes)) {
                $raw = if ($Rule.match -eq "removed") { $c.removedMembers } else { $c.addedMembers }
                $members = @($raw | Where-Object { $_ })
                $simple = ($c.fqcn -split '\.')[-1]
                foreach ($m in $members) {
                    $name = if ($m -match '(\w+)\s*\(') { $Matches[1] } else { "*" }
                    $out.Add((New-RuleFinding -Rule $Rule -File $c.fqcn -Line $null -Evidence $m -Symbols @("$simple#$name") -Scope $Rule.scope))
                }
            }
        }
        "it-not-run" {
            # ItMode comes from stage 1 (build-results.json): failsafe, surefire (run by UpgradeLens because the
            # pom has no failsafe plugin), not-run, or none (no IT classes).
            if ($Ctx.ItMode -eq "not-run") {
                foreach ($it in @($Ctx.ItFiles)) {
                    $out.Add((New-RuleFinding -Rule $Rule -File $it -Line $null -Evidence "no maven-failsafe-plugin in pom.xml and build.integrationTests is 'failsafe-only', so $([System.IO.Path]::GetFileName($it)) did not run in stage 1" -Scope $Rule.scope))
                }
            }
        }
        "library-api" {
            # Library API changes (stage 3, library-api-diff.json) that the application code touches.
            #   removed-used   a library class or member that master's code used is gone on change_branch
            #   changed-used   a library class that change_branch's code uses has a changed public API
            foreach ($item in @($Ctx.LibraryApi.items)) {
                $hit = $false
                if ($Rule.match -eq "removed-used") { $hit = ($item.severity -eq "high") }
                elseif ($Rule.match -eq "changed-used") { $hit = ($item.severity -eq "medium") }
                if (-not $hit) { continue }
                $users = @(@($item.usage.base) + @($item.usage.head))
                $symKeys = @($users | ForEach-Object { "$(($_.appClass -split '[.$]')[-1])#$($_.method)" } | Where-Object { $_ -notmatch '#$' } | Sort-Object -Unique)
                $first = $users | Select-Object -First 1
                $file = if ($first -and $first.source) { $first.source } else { $item.class }
                $line = if ($first) { $first.line } else { $null }
                $evidence = "$($item.id) $($item.class) [$($item.artifact.base) -> $($item.artifact.head)]: $($item.summary)"
                $out.Add((New-RuleFinding -Rule $Rule -File $file -Line $line -Evidence $evidence -Symbols $symKeys -Scope $Rule.scope))
            }
        }
        default { }
    }
    return $out.ToArray()
}

function Get-TreeDescendants {
    # Artifacts below one tree entry (the lines after it that are deeper), from ConvertFrom-DependencyTree output.
    param($Tree, [string]$Ga)
    $items = @($Tree)
    for ($i = 0; $i -lt $items.Count; $i++) {
        if ($items[$i].ga -ne $Ga -or $items[$i].depth -lt 1) { continue }
        $d = $items[$i].depth
        $out = New-Object System.Collections.Generic.List[object]
        for ($j = $i + 1; $j -lt $items.Count -and $items[$j].depth -gt $d; $j++) { $out.Add($items[$j]) }
        return $out.ToArray()
    }
    return @()
}

function Test-StarterArtifact([string]$Ga) {
    # A starter is an aggregator pom-jar: it has no classes of its own, so "unused declared" says nothing.
    return (($Ga.Split(':')[-1]) -like '*-starter*')
}

function Get-ReplacementScore {
    # How likely the added artifact replaces the removed one. 0 = not related. Higher is closer.
    #   same artifactId, new groupId (a relocation, e.g. com.fasterxml.jackson.core -> tools.jackson.core)
    #   same groupId and one artifactId extends the other (spring-boot-starter-web -> spring-boot-starter-webmvc)
    #   same groupId and the artifactIds differ only in a trailing number
    param([string]$Added, [string]$Removed)
    $ag, $aa = $Added.Split(':')
    $rg, $ra = $Removed.Split(':')
    if ($aa -eq $ra -and $ag -ne $rg) { return 1000 }
    if ($ag -ne $rg) { return 0 }
    if (($aa -replace '\d+$', '') -eq ($ra -replace '\d+$', '')) { return 900 }
    if ($aa.StartsWith($ra) -or $ra.StartsWith($aa)) {
        # A test variant of a main artifact is not its replacement.
        if (($aa -like '*-test') -ne ($ra -like '*-test')) { return 0 }
        return 500 - [Math]::Abs($aa.Length - $ra.Length)
    }
    return 0
}

function Get-ReplacementPairs {
    # Pairs each removed direct artifact with at most one added direct artifact (best score first, each
    # artifact used once). Returns a hashtable removed ga -> added ga.
    param([string[]]$AddedGas, [string[]]$RemovedGas)
    $cands = New-Object System.Collections.Generic.List[object]
    foreach ($a in @($AddedGas)) {
        foreach ($r in @($RemovedGas)) {
            $s = Get-ReplacementScore -Added $a -Removed $r
            if ($s -gt 0) { $cands.Add([pscustomobject]@{ added = $a; removed = $r; score = $s }) }
        }
    }
    $pairs = @{}
    $usedAdded = @{}
    foreach ($c in @($cands | Sort-Object -Property @{ e = { $_.score }; Descending = $true }, added, removed)) {
        if ($pairs.ContainsKey($c.removed) -or $usedAdded.ContainsKey($c.added)) { continue }
        $pairs[$c.removed] = $c.added
        $usedAdded[$c.added] = $true
    }
    return $pairs
}

function Get-DependencyAssessment {
    # One row per added or removed direct dependency, and per direct version change: is it required,
    # unnecessary, a replacement, or does it need a person to look; and what it can affect.
    #   required     code uses its classes (dependency:analyze), or code uses an artifact a starter brings
    #   replacement  it takes the place of a removed direct dependency
    #   unnecessary  declared, not a starter, and no compiled code uses it
    #   review       cannot be decided from the build: a starter whose artifacts no code calls, which works
    #                through auto-configuration at runtime (the probes and the bean dump show its effect)
    # $Analyze: @{ base = { unused; undeclared }; head = { ... } } from ConvertFrom-DependencyAnalyze.
    param($Diff, $BaseTree, $HeadTree, $Analyze, [string[]]$IgnoreUnused = @())
    $rows = New-Object System.Collections.Generic.List[object]
    $baseGas = @{}; foreach ($b in @($BaseTree | Where-Object { $_.depth -ge 1 })) { $baseGas[$b.ga] = $true }
    $headUnused = @($Analyze.head.unused | ForEach-Object { ConvertTo-GroupKey $_ })
    $headUsedUndeclared = @($Analyze.head.undeclared | ForEach-Object { ConvertTo-GroupKey $_ })
    $baseUnused = @($Analyze.base.unused | ForEach-Object { ConvertTo-GroupKey $_ })
    $baseUsedUndeclared = @($Analyze.base.undeclared | ForEach-Object { ConvertTo-GroupKey $_ })
    $removedDirect = @($Diff.removed | Where-Object { $_.direct } | ForEach-Object { $_.ga })
    $addedDirect = @($Diff.added | Where-Object { $_.direct } | ForEach-Object { $_.ga })
    $pairs = Get-ReplacementPairs -AddedGas $addedDirect -RemovedGas $removedDirect
    $replacesByAdded = @{}
    foreach ($k in $pairs.Keys) { $replacesByAdded[$pairs[$k]] = $k }

    foreach ($a in @($Diff.added | Where-Object { $_.direct })) {
        $entry = @($HeadTree | Where-Object { $_.ga -eq $a.ga -and $_.depth -eq 1 }) | Select-Object -First 1
        $scope = if ($entry) { $entry.scope } else { "" }
        $desc = @(Get-TreeDescendants -Tree $HeadTree -Ga $a.ga)
        $introduces = @($desc | Where-Object { -not $baseGas.ContainsKey($_.ga) } | ForEach-Object { "$($_.ga):$($_.version)" } | Sort-Object -Unique)
        $usedThrough = @($desc | Where-Object { $headUsedUndeclared -contains $_.ga } | ForEach-Object { $_.ga } | Sort-Object -Unique)
        $starter = Test-StarterArtifact $a.ga
        $ignored = @($IgnoreUnused | Where-Object { $a.ga -like $_ }).Count -gt 0
        $unused = ($headUnused -contains $a.ga) -and -not $ignored
        $replaces = if ($replacesByAdded.ContainsKey($a.ga)) { $replacesByAdded[$a.ga] } else { $null }

        $reasons = New-Object System.Collections.Generic.List[string]
        if (-not $starter -and -not $unused) { $assessment = "required"; $reasons.Add("compiled code uses its classes (dependency:analyze)") }
        elseif ($starter -and $usedThrough.Count -gt 0) { $assessment = "required"; $reasons.Add("code uses $($usedThrough.Count) artifact(s) it brings: $($usedThrough -join ', ')") }
        elseif ($starter) { $assessment = "review"; $reasons.Add("starter whose artifacts no compiled code calls; it works through auto-configuration at runtime, so only the probes and the bean dump show whether it is needed") }
        elseif ($scope -eq "runtime") { $assessment = "review"; $reasons.Add("runtime scope: loaded by configuration, reflection or ServiceLoader, which dependency:analyze cannot see") }
        else { $assessment = "unnecessary"; $reasons.Add("declared, but no compiled main or test code uses it (dependency:analyze). Remove it, or keep it only if it is loaded at runtime") }
        if ($replaces) {
            $reasons.Add("replaces $replaces, which change_branch removed")
            if ($assessment -ne "required") { $assessment = "replacement" }
        }

        $impact = if ($introduces.Count -gt 0) { "Adds $($introduces.Count) artifact(s) that master does not have: $(@($introduces | Select-Object -First 8) -join ', ')$(if ($introduces.Count -gt 8) { ', ...' })." } else { "Adds no artifact that master does not already resolve." }
        if ($starter) { $impact += " Its auto-configuration can register beans and change defaults for every flow." }
        $rows.Add([pscustomobject]@{
            change     = "added"
            ga         = $a.ga
            version    = $a.version
            scope      = $scope
            starter    = $starter
            assessment = $assessment
            reason     = ($reasons -join "; ")
            impact     = $impact
            replaces   = $replaces
            introduces = $introduces
        })
    }

    foreach ($r in @($Diff.removed | Where-Object { $_.direct })) {
        $desc = @(Get-TreeDescendants -Tree $BaseTree -Ga $r.ga)
        $usedThrough = @($desc | Where-Object { $baseUsedUndeclared -contains $_.ga } | ForEach-Object { $_.ga } | Sort-Object -Unique)
        $wasUsed = if (Test-StarterArtifact $r.ga) { $usedThrough.Count -gt 0 } else { $baseUnused -notcontains $r.ga }
        $replacedBy = if ($pairs.ContainsKey($r.ga)) { $pairs[$r.ga] } else { $null }
        $assessment = if ($replacedBy) { "replaced" } elseif ($wasUsed) { "review" } else { "safe-to-remove" }
        $reason = if ($replacedBy) { "replaced by $replacedBy" }
                  elseif ($wasUsed) { "master code used it$(if ($usedThrough.Count -gt 0) { ' (through ' + ($usedThrough -join ', ') + ')' }); change_branch compiles without it, so check that the replacement classes behave the same" }
                  else { "no compiled code on master used it" }
        $rows.Add([pscustomobject]@{
            change     = "removed"
            ga         = $r.ga
            version    = $r.version
            scope      = ""
            starter    = (Test-StarterArtifact $r.ga)
            assessment = $assessment
            reason     = $reason
            impact     = "Classes and auto-configuration it brought are gone unless the replacement brings them. Runtime-only use is visible only in the probes and the bean dump."
            replaces   = $null
            introduces = @()
        })
    }

    foreach ($c in @($Diff.changed | Where-Object { $_.direct })) {
        $rows.Add([pscustomobject]@{
            change     = "version"
            ga         = $c.ga
            version    = "$($c.base) -> $($c.head)"
            scope      = ""
            starter    = (Test-StarterArtifact $c.ga)
            assessment = $(if ($c.major) { "review" } else { "required" })
            reason     = $(if ($c.major) { "major version change" } else { "minor or patch version change" })
            impact     = $(if ($c.major) { "A major version can remove APIs and change defaults. See the library API changes and the release-notes validation." } else { "Usually compatible; the library API diff lists any public change that the code touches." })
            replaces   = $null
            introduces = @()
        })
    }
    return $rows.ToArray()
}

function Get-DependencySuggestions {
    # Commands for deeper analysis, each with the reason it is suggested for this run.
    param($Conflicts, $Assessment, $Analyze, [bool]$VerboseAvailable)
    $s = New-Object System.Collections.Generic.List[object]
    foreach ($c in @($Conflicts | Where-Object { $_.major } | Select-Object -First 10)) {
        $s.Add([pscustomobject]@{ command = "mvn dependency:tree -Dverbose -Dincludes=$($c.ga)"; why = "$($c.ga) $($c.requested) lost to $($c.resolved) (major); see which path brings each version" })
    }
    if (@($Conflicts).Count -gt 0) {
        $s.Add([pscustomobject]@{ command = "mvn enforcer:enforce -Drules=dependencyConvergence"; why = "$(@($Conflicts).Count) version conflict(s) in the head tree; convergence lists each one that is not pinned" })
    }
    foreach ($a in @($Assessment | Where-Object { $_.assessment -eq "unnecessary" })) {
        $s.Add([pscustomobject]@{ command = "mvn dependency:analyze -DignoreNonCompile=true"; why = "$($a.ga) looks unused; confirm before removing it" })
        break
    }
    $newUndeclared = @($Analyze.head.undeclared | Where-Object { @($Analyze.base.undeclared) -notcontains $_ })
    if ($newUndeclared.Count -gt 0) {
        $s.Add([pscustomobject]@{ command = "mvn dependency:analyze -DfailOnWarning=true"; why = "$($newUndeclared.Count) used-but-undeclared artifact(s) are new on change_branch; declare them so a BOM change cannot remove them" })
    }
    $s.Add([pscustomobject]@{ command = "mvn dependency:analyze-duplicate"; why = "finds duplicate dependency declarations in the pom" })
    if (-not $VerboseAvailable) {
        $s.Add([pscustomobject]@{ command = "upgrade maven-dependency-plugin to 3.7.0 or later"; why = "this run could not produce a verbose tree, so version conflicts were not listed" })
    }
    return $s.ToArray()
}

function ConvertFrom-SurefireReport {
    # One row per test case from surefire or failsafe TEST-*.xml files.
    param([string[]]$Paths)
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($p in $Paths) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        try { [xml]$x = Get-Content -LiteralPath $p -Raw -Encoding UTF8 } catch { continue }
        foreach ($tc in @($x.testsuite.testcase)) {
            if ($null -eq $tc) { continue }
            $status = "passed"; $message = ""
            if ($tc.SelectSingleNode("failure")) { $status = "failed"; $message = [string]$tc.SelectSingleNode("failure").GetAttribute("message") }
            elseif ($tc.SelectSingleNode("error")) { $status = "error"; $message = [string]$tc.SelectSingleNode("error").GetAttribute("message") }
            elseif ($tc.SelectSingleNode("skipped")) { $status = "skipped" }
            $rows.Add([pscustomobject]@{ id = "$($tc.classname)#$($tc.name)"; status = $status; message = $message })
        }
    }
    return $rows.ToArray()
}

function Compare-TestResults {
    # Test-by-test comparison of the two builds. change: same, new (head only), removed (base only),
    # now-failing, now-passing, now-skipped, now-running.
    param($Base, $Head)
    $bm = @{}; foreach ($r in @($Base)) { $bm[$r.id] = $r }
    $hm = @{}; foreach ($r in @($Head)) { $hm[$r.id] = $r }
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($id in @(@($bm.Keys) + @($hm.Keys) | Sort-Object -Unique)) {
        $b = $bm[$id]; $h = $hm[$id]
        $bs = if ($b) { $b.status } else { "" }
        $hs = if ($h) { $h.status } else { "" }
        $okB = $bs -eq "passed"; $okH = $hs -eq "passed"
        $change = if (-not $b) { "new" } elseif (-not $h) { "removed" }
                  elseif ($bs -eq $hs) { "same" }
                  elseif ($okB -and ($hs -eq "failed" -or $hs -eq "error")) { "now-failing" }
                  elseif ($okH) { if ($bs -eq "skipped") { "now-running" } else { "now-passing" } }
                  elseif ($hs -eq "skipped") { "now-skipped" }
                  else { "now-failing" }
        $rows.Add([pscustomobject]@{ test = $id; base = $bs; head = $hs; change = $change; message = $(if ($h) { $h.message } else { "" }) })
    }
    return $rows.ToArray()
}

function New-AddedFilePatch {
    # A git-style patch that adds one new file, so a reviewer can "git apply" the new tests.
    param([string]$RelativePath, [string[]]$Lines)
    $p = $RelativePath -replace '\\', '/'
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append("diff --git a/$p b/$p`n")
    [void]$sb.Append("new file mode 100644`n")
    [void]$sb.Append("--- /dev/null`n")
    [void]$sb.Append("+++ b/$p`n")
    [void]$sb.Append("@@ -0,0 +1,$($Lines.Count) @@`n")
    foreach ($l in $Lines) { [void]$sb.Append("+" + ([string]$l).TrimEnd("`r") + "`n") }
    return $sb.ToString()
}

function Get-FlowCoverage {
    # For each symbol a flow reaches: JaCoCo covered instructions on the head side (null when the
    # report has no entry). A symbol with no covered instruction is untraced.
    param([string[]]$Reach, $Jacoco, $FqcnBySimple)
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($item in $Reach) {
        $parts = $item.Split('#')
        $cls = $parts[0]; $method = $parts[1]
        $fq = if ($FqcnBySimple.ContainsKey($cls)) { $FqcnBySimple[$cls] -replace '\.', '/' } else { $null }
        $covered = $null
        if ($fq) {
            $key = if ($method -eq "*") { "$fq|*" } else { "$fq|$method" }
            if ($Jacoco.ContainsKey($key)) { $covered = [int]$Jacoco[$key] }
        }
        $rows.Add([pscustomobject]@{ symbol = $item; covered = $covered; traced = ($null -ne $covered -and $covered -gt 0) })
    }
    return $rows.ToArray()
}

function Test-FindingAffectsFlow {
    # global findings touch every flow. symbol findings touch the flows whose reach holds the symbol
    # ("Class#*" holds every method of the class). Other scopes do not change production flows.
    param($Finding, $Flow)
    switch ($Finding.scope) {
        "global" { return $true }
        "symbol" {
            foreach ($sym in @($Finding.symbols)) {
                if ($sym -like "*#*") {
                    $cls = $sym.Split('#')[0]
                    if ($sym.EndsWith("#*")) {
                        if (@($Flow.reach | Where-Object { $_ -like "$cls#*" }).Count -gt 0) { return $true }
                    } elseif (@($Flow.reach) -contains $sym) { return $true }
                }
            }
            return $false
        }
        default { return $false }
    }
}

function Get-FindingStatus {
    # Effective status of one finding after the behavior checks (stage 6 reads it).
    #   accepted  a person accepted the difference (manual decision)
    #   confirmed a linked check shows a difference or a failure on head: a behavior change
    #   verified  at least one linked check ran and matched master
    #   open      no linked check ran
    # $Checks: rows { ruleId; outcome = "same" | "different" | "error" }.
    param($Finding, $Checks, $Decisions = @())
    $accepted = @($Decisions | Where-Object { $_.findingId -eq $Finding.id -and $_.decision -eq "accept" })
    if ($accepted.Count -gt 0) { return "accepted" }
    $linked = @($Checks | Where-Object { $_.ruleId -eq $Finding.ruleId })
    # A "different" outcome is a behavior change. An "error" is a check that did not run: not a change.
    if (@($linked | Where-Object { $_.outcome -eq "different" }).Count -gt 0) { return "confirmed" }
    # A check that did not run, or a test that does not hold on master, verifies nothing.
    if (@($linked | Where-Object { $_.outcome -eq "error" -or $_.outcome -eq "unverified" }).Count -gt 0) { return "open" }
    if ($linked.Count -gt 0) { return "verified" }
    return "open"
}

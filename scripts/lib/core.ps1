<#
  UpgradeLens shared functions for stages 3 to 10.

  Dot-source from a stage script:
      . (Join-Path $PSScriptRoot "lib\core.ps1")

  Parsers, matchers and the verdict rule are pure functions: they take values and return values,
  so tests\upgradelens.Tests.ps1 can call them without a Maven build or a running server.
  Windows PowerShell 5.1 compatible.
#>

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:StageLog = $null

# ---------- files and hashes ----------

function Write-JsonFile {
    param([string]$Path, $Value, [int]$Depth = 12)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    # -InputObject keeps arrays whole (piping would send each element separately).
    $json = ConvertTo-Json -InputObject $Value -Depth $Depth
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $script:Utf8NoBom)
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Get-InputHash {
    # SHA-256 over the bytes of the given files, in order. A missing file contributes its path,
    # so a missing input changes the hash too. Used to tie agent proposals to their inputs.
    param([string[]]$Paths)
    $ms = New-Object System.IO.MemoryStream
    foreach ($p in $Paths) {
        if (Test-Path -LiteralPath $p) {
            $bytes = [System.IO.File]::ReadAllBytes($p)
        } else {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes("missing:" + $p)
        }
        $ms.Write($bytes, 0, $bytes.Length)
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hex = [BitConverter]::ToString($sha.ComputeHash($ms.ToArray())).Replace("-", "")
    $ms.Dispose()
    return $hex
}

# ---------- logging and native commands ----------

function Write-StageLog([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line
    if ($script:StageLog) {
        [System.IO.File]::AppendAllText($script:StageLog, $line + [Environment]::NewLine, $script:Utf8NoBom)
    }
}

function Invoke-Native {
    # Runs an external command and returns its exit code and output as plain strings.
    # Native stderr does not stop the script (Windows PowerShell 5.1 would make it an error).
    param([string]$Exe, [string[]]$Arguments = @())
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& $Exe @Arguments 2>&1 | ForEach-Object { "$_" })
        $code = $LASTEXITCODE
    } catch {
        $output = @($_.Exception.Message)
        $code = -1
    } finally {
        $ErrorActionPreference = $previous
    }
    [pscustomobject]@{ ExitCode = $code; Output = $output }
}

function Invoke-WithJdk {
    # Runs a script block with JAVA_HOME and PATH pointing at one JDK, then restores them.
    param([string]$JdkHome, [scriptblock]$Action)
    $savedJava = $env:JAVA_HOME
    $savedPath = $env:Path
    try {
        $env:JAVA_HOME = $JdkHome
        $env:Path = (Join-Path $JdkHome "bin") + ";" + $savedPath
        & $Action
    } finally {
        $env:JAVA_HOME = $savedJava
        $env:Path = $savedPath
    }
}

# ---------- configuration ----------

function Resolve-ConfigPath {
    # The config file a stage uses: -Config if given, else $env:UPGRADELENS_CONFIG (set by run-pipeline.ps1
    # so every stage and agent step of one run reads the same file), else config\upgrade-lens.json.
    param([string]$Config, [string]$RootDir)
    $p = if ($Config) { $Config } elseif ($env:UPGRADELENS_CONFIG) { $env:UPGRADELENS_CONFIG } else { Join-Path $RootDir "config\upgrade-lens.json" }
    if (-not [System.IO.Path]::IsPathRooted($p)) { $p = Join-Path (Get-Location).Path $p }
    return [System.IO.Path]::GetFullPath($p)
}

function Get-ConfigValue {
    # Reads a dotted key ("timeouts.buildMinutes") from a config object. Returns $Default when the key is
    # missing, null or an empty string.
    param($Cfg, [string]$Path, $Default = $null)
    $node = $Cfg
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $node) { return $Default }
        if ($node -is [System.Collections.IDictionary]) { $node = $node[$part] }
        elseif ($node.PSObject.Properties.Name -contains $part) { $node = $node.$part }
        else { return $Default }
    }
    if ($null -eq $node) { return $Default }
    if ($node -is [string] -and $node.Trim().Length -eq 0) { return $Default }
    return $node
}

function Resolve-ProjectPath {
    # A config path relative to the UpgradeLens folder, or an absolute path as is.
    param([string]$Path, [string]$RootDir)
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return (Join-Path $RootDir $Path)
}

# ---------- agent proposals ----------

function Get-AgentInputs {
    # The files whose bytes an agent proposal is tied to. agent-step.ps1 prints their hash for the agent;
    # the stages recompute it and use a proposal only when the hashes match. One list, used by both.
    param([string]$RunDir, [ValidateSet("analyst", "tests", "release-notes")][string]$Step)
    $a = Join-Path $RunDir "artifacts"
    switch ($Step) {
        "analyst" {
            $rulesDir = Join-Path $RunDir "config\rules"
            $rules = @()
            if (Test-Path -LiteralPath $rulesDir) {
                $rules = @(Get-ChildItem -LiteralPath $rulesDir -Filter *.json -File | Sort-Object Name | ForEach-Object { $_.FullName })
            }
            return @((Join-Path $a "change-inventory.json"), (Join-Path $a "dependency-diff.json"),
                     (Join-Path $a "api-diff.json"), (Join-Path $a "library-api-diff.json")) + $rules
        }
        "tests" {
            # test-gaps.json is written by next-step.ps1 before the test writer runs (outside artifacts\agent,
            # so the writer cannot change its own work list).
            return @((Join-Path $a "impact-findings.json"), (Join-Path $a "reach.json"), (Join-Path $a "changed-methods.json"),
                     (Join-Path $a "test-gaps.json"))
        }
        "release-notes" {
            return @((Join-Path $a "dependency-diff.json"), (Join-Path $a "library-api-diff.json"))
        }
    }
}

function Get-AgentProposal {
    # State of one agent proposal file: not-run (no file), stale (inputs changed since it was written),
    # or used. Only a "used" proposal may change a result.
    param([string]$RunDir, [string]$Step, [string]$FileName)
    $path = Join-Path $RunDir ("artifacts\agent\" + $FileName)
    if (-not (Test-Path -LiteralPath $path)) { return [pscustomobject]@{ state = "not-run"; doc = $null; path = $path } }
    $doc = Read-JsonFile $path
    $expected = Get-InputHash (Get-AgentInputs -RunDir $RunDir -Step $Step)
    $state = if ($doc -and $doc.inputHash -eq $expected) { "used" } else { "stale" }
    return [pscustomobject]@{ state = $state; doc = $doc; path = $path }
}

# ---------- run folder and status ----------

function Get-RunsRoot {
    param($Cfg, [string]$RootDir)
    if ([System.IO.Path]::IsPathRooted($Cfg.runsFolder)) { return $Cfg.runsFolder }
    return (Join-Path $RootDir $Cfg.runsFolder)
}

function Test-OrchestratedRun([string]$RunDir) {
    # A run started by the orchestrator (scripts\run-stage.ps1 stage 0) has a ledger. Its stages may only be
    # run through run-stage.ps1, so the order, the lock and the agent gates cannot be bypassed.
    return (Test-Path -LiteralPath (Join-Path $RunDir "orchestration\ledger.json"))
}

function Resolve-RunId {
    # The given run id, or the newest run folder whose status.json state is one of $States.
    # Guards (a stage prints the reason and gets $null back, so it exits 2):
    #   - a given run must be in one of $States (unless -AnyState): no stage runs out of order
    #   - an orchestrated run accepts a stage only from run-stage.ps1 ($env:UPGRADELENS_INVOKER)
    param([string]$RunsRoot, [string]$RunId, [string[]]$States = @(), [switch]$AnyState)
    if ($RunId) {
        $runDir = Join-Path $RunsRoot $RunId
        $st = Read-JsonFile (Join-Path $runDir "status.json")
        if (-not $st) { Write-Host "Run not found: $runDir"; return $null }
        if ((Test-OrchestratedRun $runDir) -and $env:UPGRADELENS_INVOKER -ne "run-stage") {
            Write-Host "Run $RunId is run by the UpgradeLens orchestrator. Run its stages through the upgradelens-orchestrator agent (scripts\next-step.ps1 and scripts\run-stage.ps1), not directly: a direct call would bypass the order, the lock and the agent gates."
            return $null
        }
        if (-not $AnyState -and $States.Count -gt 0 -and $States -notcontains [string]$st.state) {
            Write-Host "Run $RunId is in state '$($st.state)'. This stage needs one of: $($States -join ', '). Run the earlier stages first."
            return $null
        }
        return $RunId
    }
    $hit = Get-ChildItem -LiteralPath $RunsRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Where-Object {
            $st = Read-JsonFile (Join-Path $_.FullName "status.json")
            $null -ne $st -and ($States -contains $st.state)
        } | Select-Object -First 1
    if ($hit) { return $hit.Name }
    return $null
}

function Set-StageStep {
    # Replaces the step with the same name (so a rerun of a stage does not duplicate it).
    param($Status, [string]$Name, [string]$State, [string]$Reason = "", [string]$Artifact = "")
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($s in @($Status.steps)) { if ($s.name -ne $Name) { $list.Add($s) } }
    $list.Add([pscustomobject]@{
        name       = $Name
        state      = $State
        reason     = $Reason
        artifact   = $Artifact
        finishedAt = (Get-Date).ToString("o")
    })
    $Status.steps = $list.ToArray()
}

function Set-RunState {
    param($Status, [string]$State, [string]$Phase)
    $Status.state = $State
    if ($Phase) { $Status.phase = $Phase }
}

# ---------- small helpers ----------

function Get-MajorVersion([string]$Version) {
    if ($Version -match '^(\d+)') { return [int]$Matches[1] }
    return -1
}

function Test-GlobMatch {
    # Simple glob for file paths: * matches anything, including "/". Case-insensitive.
    param([string]$Path, [string]$Glob)
    $p = $Path -replace '\\', '/'
    $g = $Glob -replace '\\', '/'
    return ($p -like $g)
}

# ---------- dependency tree ----------

function ConvertFrom-MavenCoordinate {
    # groupId:artifactId:type[:classifier]:version[:scope] -> ga, version, scope. $null when not a coordinate.
    param([string]$Coordinate)
    $parts = $Coordinate.Split(':')
    switch ($parts.Count) {
        4 { return [pscustomobject]@{ ga = "$($parts[0]):$($parts[1])"; version = $parts[3]; scope = "" } }
        5 { return [pscustomobject]@{ ga = "$($parts[0]):$($parts[1])"; version = $parts[3]; scope = $parts[4] } }
        6 { return [pscustomobject]@{ ga = "$($parts[0]):$($parts[1])"; version = $parts[4]; scope = $parts[5] } }
        default { return $null }
    }
}

function ConvertFrom-DependencyTree {
    # Parses "mvn dependency:tree -DoutputType=text" (plain or -Dverbose). Each resolved line: tree prefix,
    # then groupId:artifactId:type[:classifier]:version[:scope]. Depth comes from the prefix width
    # (3 characters per level); depth 0 is the project itself. "parent" is the ga one level up.
    # Verbose-only lines, "(g:a:... - omitted for ...)", are not resolved artifacts and are skipped here
    # (ConvertFrom-DependencyTreeVerbose reads them).
    param([string[]]$Lines)
    $items = New-Object System.Collections.Generic.List[object]
    $rx = '^(?<prefix>[ |+\\-]*)(?<coord>[A-Za-z0-9_.\-]+:\S+)'
    $stack = @{}
    foreach ($raw in $Lines) {
        $line = ([string]$raw).TrimEnd()
        if ($line -notmatch $rx) { continue }
        $depth = [int]($Matches['prefix'].Length / 3)
        $c = ConvertFrom-MavenCoordinate $Matches['coord']
        if (-not $c) { continue }
        $stack[$depth] = $c.ga
        $parent = if ($depth -ge 1 -and $stack.ContainsKey($depth - 1)) { $stack[$depth - 1] } else { "" }
        $items.Add([pscustomobject]@{
            ga      = $c.ga
            version = $c.version
            scope   = $c.scope
            depth   = $depth
            direct  = ($depth -eq 1)
            parent  = $parent
        })
    }
    return $items.ToArray()
}

function ConvertFrom-DependencyTreeVerbose {
    # Reads the "-Dverbose" annotations of a dependency tree:
    #   conflicts   "(g:a:jar:1.0:compile - omitted for conflict with 2.0)": a dependency asked for a version
    #               that lost to another one. Grouped per artifact and requested version, with who asked.
    #   duplicates  count of "omitted for duplicate" lines
    #   managed     resolved artifacts whose version a BOM or parent changed ("version managed from X")
    param([string[]]$Lines)
    $conflicts = @{}
    $managed = New-Object System.Collections.Generic.List[object]
    $duplicates = 0
    $stack = @{}
    $omittedRx = '^(?<prefix>[ |+\\-]*)\((?<coord>[A-Za-z0-9_.\-]+:[^\s)]+) - (?<note>[^)]*)\)\s*$'
    $keptRx = '^(?<prefix>[ |+\\-]*)(?<coord>[A-Za-z0-9_.\-]+:\S+)(?:\s+\((?<note>[^)]*)\))?\s*$'
    foreach ($raw in $Lines) {
        $line = ([string]$raw).TrimEnd()
        if ($line -match $omittedRx) {
            $depth = [int]($Matches['prefix'].Length / 3)
            $note = $Matches['note']
            $c = ConvertFrom-MavenCoordinate $Matches['coord']
            if (-not $c) { continue }
            $parent = if ($stack.ContainsKey($depth - 1)) { $stack[$depth - 1] } else { "" }
            if ($note -match 'omitted for conflict with ([^;\s)]+)') {
                $winner = $Matches[1]
                $key = "$($c.ga)|$($c.version)|$winner"
                if (-not $conflicts.ContainsKey($key)) {
                    $conflicts[$key] = [pscustomobject]@{
                        ga         = $c.ga
                        requested  = $c.version
                        resolved   = $winner
                        major      = ((Get-MajorVersion $c.version) -ne (Get-MajorVersion $winner))
                        requiredBy = (New-Object System.Collections.Generic.List[string])
                    }
                }
                if ($parent -and -not $conflicts[$key].requiredBy.Contains($parent)) { $conflicts[$key].requiredBy.Add($parent) }
            } elseif ($note -match 'omitted for duplicate') {
                $duplicates++
            }
            continue
        }
        if ($line -match $keptRx) {
            $depth = [int]($Matches['prefix'].Length / 3)
            $note = [string]$Matches['note']
            $c = ConvertFrom-MavenCoordinate $Matches['coord']
            if (-not $c) { continue }
            $stack[$depth] = $c.ga
            if ($note -match 'version managed from ([^;\s)]+)' -and $Matches[1] -ne $c.version) {
                $managed.Add([pscustomobject]@{ ga = $c.ga; declared = $Matches[1]; managed = $c.version })
            }
        }
    }
    $conflictRows = @($conflicts.Keys | Sort-Object | ForEach-Object {
        $x = $conflicts[$_]
        [pscustomobject]@{ ga = $x.ga; requested = $x.requested; resolved = $x.resolved; major = $x.major; requiredBy = @($x.requiredBy | Sort-Object) }
    })
    return [pscustomobject]@{
        conflicts  = $conflictRows
        duplicates = $duplicates
        managed    = @($managed | Sort-Object ga)
    }
}

function Compare-DependencyTrees {
    # Compares two parsed trees by groupId:artifactId. Keeps the shallowest occurrence of each artifact.
    param($Base, $Head)
    function Get-Map($items) {
        $map = @{}
        foreach ($d in @($items | Where-Object { $_.depth -ge 1 })) {
            if (-not $map.ContainsKey($d.ga) -or $d.depth -lt $map[$d.ga].depth) { $map[$d.ga] = $d }
        }
        return $map
    }
    $bm = Get-Map $Base
    $hm = Get-Map $Head
    $added = New-Object System.Collections.Generic.List[object]
    $removed = New-Object System.Collections.Generic.List[object]
    $changed = New-Object System.Collections.Generic.List[object]

    foreach ($ga in ($hm.Keys | Sort-Object)) {
        if (-not $bm.ContainsKey($ga)) {
            $added.Add([pscustomobject]@{ ga = $ga; version = $hm[$ga].version; direct = $hm[$ga].direct })
        }
    }
    foreach ($ga in ($bm.Keys | Sort-Object)) {
        if (-not $hm.ContainsKey($ga)) {
            $removed.Add([pscustomobject]@{ ga = $ga; version = $bm[$ga].version; direct = $bm[$ga].direct })
        }
    }
    foreach ($ga in ($hm.Keys | Sort-Object)) {
        if ($bm.ContainsKey($ga) -and $bm[$ga].version -ne $hm[$ga].version) {
            $bv = $bm[$ga].version; $hv = $hm[$ga].version
            $changed.Add([pscustomobject]@{
                ga     = $ga
                base   = $bv
                head   = $hv
                major  = ((Get-MajorVersion $bv) -ne (Get-MajorVersion $hv))
                direct = ($bm[$ga].direct -or $hm[$ga].direct)
            })
        }
    }
    return [pscustomobject]@{
        added   = $added.ToArray()
        removed = $removed.ToArray()
        changed = $changed.ToArray()
    }
}

# ---------- JaCoCo ----------

function ConvertFrom-JacocoXml {
    # Returns a hashtable of covered INSTRUCTION counts:
    #   "com/example/Foo|method"  one entry per method name (overloads are summed)
    #   "com/example/Foo|*"       class total
    # An empty hashtable means the report is missing.
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    [xml]$doc = Get-Content -LiteralPath $Path -Raw
    foreach ($pkg in @($doc.report.package)) {
        foreach ($cls in @($pkg.class)) {
            $cname = [string]$cls.name
            $classCovered = 0
            foreach ($counter in @($cls.counter)) {
                if ($counter.type -eq "INSTRUCTION") { $classCovered = [int]$counter.covered }
            }
            $map["$cname|*"] = $classCovered
            foreach ($m in @($cls.method)) {
                $covered = 0
                foreach ($counter in @($m.counter)) {
                    if ($counter.type -eq "INSTRUCTION") { $covered = [int]$counter.covered }
                }
                $key = "$cname|$($m.name)"
                if ($map.ContainsKey($key)) { $map[$key] = $map[$key] + $covered } else { $map[$key] = $covered }
            }
        }
    }
    return $map
}

# ---------- Java source reading ----------

$script:JavaNonDeclarationWords = @(
    'if', 'for', 'while', 'switch', 'catch', 'return', 'new', 'throw', 'else', 'do', 'try', 'synchronized',
    'case', 'assert', 'yield', 'super', 'this', 'instanceof', 'record', 'class', 'interface', 'enum', 'var'
)

function Remove-JavaLiterals {
    # Blanks string and char literals and drops a trailing // comment, so braces, parentheses and keywords
    # inside them do not count. Text blocks and /* */ comments are not handled (rare on declaration lines).
    param([string]$Line)
    $t = [regex]::Replace([string]$Line, '"(?:\\.|[^"\\])*"', '""')
    $t = [regex]::Replace($t, "'(?:\\.|[^'\\])+'", "''")
    $t = [regex]::Replace($t, '//.*$', '')
    return $t
}

function Get-TypeDeclarationName {
    # Name of the class, interface, enum, record or annotation type declared on this line, or $null.
    param([string]$Line)
    $t = Remove-JavaLiterals ([string]$Line).TrimEnd("`r")
    $rx = '^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*(?:record|class|interface|enum|@interface)\s+([\w$]+)'
    if ($t -match $rx) { return $Matches[1] }
    return $null
}

function Get-MethodDeclarationName {
    # Name of the method or constructor declared on this line, or $null. Covers every access level
    # (package-private too), generic methods, annotations on the same line, and declarations whose
    # parameters continue on the next lines. A call statement, a local variable or a control statement
    # is not a declaration. A constructor without an access modifier is accepted when its name is
    # capitalized and the line has no ";" (a call statement ends with one).
    param([string]$Line)
    $t = Remove-JavaLiterals ([string]$Line).TrimEnd("`r")
    if ($t -notmatch '^\s+[\w@<]') { return $null }
    $rx = '^\s+(?:@[\w.]+(?:\([^)]*\))?\s+)*(?<mods>(?:(?:public|protected|private|static|final|abstract|synchronized|default|native|strictfp)\s+)*)(?:<[^()]*?>\s+)?(?:(?<type>[\w$.]+(?:<[^()]*>)?(?:\[\])*)\s+)?(?<name>[\w$]+)\s*\('
    $m = [regex]::Match($t, $rx)
    if (-not $m.Success) { return $null }
    $name = $m.Groups['name'].Value
    $type = $m.Groups['type'].Value
    if ($script:JavaNonDeclarationWords -contains $name) { return $null }
    if ($type) {
        if ($script:JavaNonDeclarationWords -contains $type) { return $null }
        return $name
    }
    # No return type: only a constructor.
    if ($m.Groups['mods'].Value.Trim()) { return $name }
    if ($name -cmatch '^[A-Z]' -and $t -notmatch ';') { return $name }
    return $null
}

function Get-SymbolForLine {
    # The symbol a changed line belongs to: "method:Name", "type:Name", or $null (package, imports).
    #   1. A declaration line belongs to itself.
    #   2. An annotation line belongs to the declaration right below it (only annotations or blank lines
    #      in between).
    #   3. Any other line belongs to the nearest declaration above it whose block or parameter list is
    #      still open at that line (brace and parenthesis balance). So a field between two methods belongs
    #      to its type, not to the method above it. The search stops at a column-0 line.
    param([string[]]$Lines, [int]$LineNo)
    if ($null -eq $Lines -or $LineNo -lt 1 -or $LineNo -gt $Lines.Count) { return $null }
    $i = $LineNo - 1
    $text = ([string]$Lines[$i]).TrimEnd("`r")

    $t = Get-TypeDeclarationName $text
    if ($t) { return "type:$t" }
    $m = Get-MethodDeclarationName $text
    if ($m) { return "method:$m" }

    if ($text -match '^\s*@') {
        for ($j = $i + 1; $j -lt [Math]::Min($Lines.Count, $i + 8); $j++) {
            $l = ([string]$Lines[$j]).TrimEnd("`r")
            $t = Get-TypeDeclarationName $l
            if ($t) { return "type:$t" }
            $m = Get-MethodDeclarationName $l
            if ($m) { return "method:$m" }
            if ($l -notmatch '^\s*(@.*)?$') { break }
        }
    }

    $braceNet = 0
    $parenNet = 0
    for ($j = $i - 1; $j -ge 0; $j--) {
        $raw = ([string]$Lines[$j]).TrimEnd("`r")
        $l = Remove-JavaLiterals $raw
        $braceNet += ([regex]::Matches($l, '\{')).Count - ([regex]::Matches($l, '\}')).Count
        $parenNet += ([regex]::Matches($l, '\(')).Count - ([regex]::Matches($l, '\)')).Count
        if ($braceNet -ge 1 -or $parenNet -ge 1) {
            $t = Get-TypeDeclarationName $raw
            if ($t) { return "type:$t" }
            $m = Get-MethodDeclarationName $raw
            if ($m) { return "method:$m" }
        }
        if ($raw -match '^\S') { break }
    }
    return $null
}

function Get-BlockEnd {
    # Index of the "}" that closes the "{" at $Start, skipping string and char literals and comments.
    param([string]$Text, [int]$Start)
    $depth = 0
    $k = $Start
    while ($k -lt $Text.Length) {
        $c = $Text[$k]
        if ($c -eq '"' -or $c -eq "'") {
            $q = $c; $k++
            while ($k -lt $Text.Length -and $Text[$k] -ne $q) { if ($Text[$k] -eq '\') { $k++ }; $k++ }
        } elseif ($c -eq '/' -and $k + 1 -lt $Text.Length -and $Text[$k + 1] -eq '/') {
            while ($k -lt $Text.Length -and $Text[$k] -ne "`n") { $k++ }
        } elseif ($c -eq '/' -and $k + 1 -lt $Text.Length -and $Text[$k + 1] -eq '*') {
            $end = $Text.IndexOf('*/', $k + 2)
            $k = if ($end -lt 0) { $Text.Length } else { $end + 1 }
        } elseif ($c -eq '{') {
            $depth++
        } elseif ($c -eq '}') {
            $depth--
            if ($depth -eq 0) { return $k }
        }
        $k++
    }
    return -1
}

function Get-MethodBody {
    # Source text of one method body (from its "{" to the matching "}"), or $null. The first declaration
    # with that name that has a body wins (abstract and interface declarations are skipped).
    param([string]$Text, [string]$MethodName)
    $offset = 0
    foreach ($line in ($Text -split "`n")) {
        if ((Get-MethodDeclarationName $line) -eq $MethodName) {
            $open = $Text.IndexOf('{', $offset)
            $semi = $Text.IndexOf(';', $offset)
            if ($open -ge 0 -and ($semi -lt 0 -or $open -lt $semi)) {
                $end = Get-BlockEnd -Text $Text -Start $open
                if ($end -gt $open) { return $Text.Substring($open, $end - $open + 1) }
            }
        }
        $offset += $line.Length + 1
    }
    return $null
}

function Get-SourceIndex {
    # Index of the main classes: simple name -> fqcn, text, field types, declared method names.
    param([string]$SourceRoot)
    $index = @{}
    if (-not (Test-Path -LiteralPath $SourceRoot)) { return $index }
    foreach ($f in @(Get-ChildItem -LiteralPath $SourceRoot -Recurse -Filter *.java -File)) {
        $text = Get-Content -LiteralPath $f.FullName -Raw
        $simple = [System.IO.Path]::GetFileNameWithoutExtension($f.Name)
        $pkgMatch = [regex]::Match($text, '(?m)^\s*package\s+([\w.]+)\s*;')
        $pkg = if ($pkgMatch.Success) { $pkgMatch.Groups[1].Value } else { "" }
        $fields = @{}
        foreach ($fm in [regex]::Matches($text, '(?m)^\s+(?:(?:private|protected|public|final|static|transient|volatile)\s+)*([A-Z][\w.]*)(?:<[^>;=]*>)?\s+(\w+)\s*(?:;|=)')) {
            $fields[$fm.Groups[2].Value] = $fm.Groups[1].Value
        }
        $ctor = [regex]::Match($text, '(?m)^\s+public\s+' + [regex]::Escape($simple) + '\s*\(([^)]*)\)')
        if ($ctor.Success) {
            foreach ($param in ($ctor.Groups[1].Value -split ',')) {
                $tokens = @(($param -replace '@\w+(\([^)]*\))?', '').Trim() -split '\s+' | Where-Object { $_ })
                if ($tokens.Count -ge 2) {
                    $fields[$tokens[-1]] = ($tokens[-2] -replace '<.*$', '')
                }
            }
        }
        $methods = @{}
        foreach ($line in ($text -split "`n")) {
            $mn = Get-MethodDeclarationName $line
            if ($mn) { $methods[$mn] = $true }
        }
        $index[$simple] = @{
            fqcn   = if ($pkg) { "$pkg.$simple" } else { $simple }
            path   = $f.FullName
            text   = $text
            fields = $fields
            methods = $methods
        }
    }
    return $index
}

function Get-SourceFlows {
    # Flows that start a path through the code: HTTP endpoints, @Scheduled jobs and listeners.
    # Each flow: id, kind, class, method, and for HTTP the verb and full path.
    param([string]$Text, [string]$ClassName)
    $flows = New-Object System.Collections.Generic.List[object]
    $lines = $Text -split "`r?`n"
    $classIdx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if (Get-TypeDeclarationName $lines[$i]) { $classIdx = $i; break }
    }
    if ($classIdx -lt 0) { return @() }

    $basePath = ""
    for ($i = 0; $i -lt $classIdx; $i++) {
        if ($lines[$i] -match '@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?"([^"]*)"') { $basePath = $Matches[1] }
    }

    $pending = $null
    for ($i = $classIdx + 1; $i -lt $lines.Count; $i++) {
        $l = $lines[$i]
        if ($l -match '^\s*@(Get|Post|Put|Delete|Patch)Mapping\b(\((.*)\))?') {
            $verb = $Matches[1].ToUpper()
            $annArgs = [string]$Matches[3]
            $pending = @{ kind = "http"; verb = $verb; path = (Get-MappingPath $annArgs) }
        } elseif ($l -match '^\s*@RequestMapping\b(\((.*)\))?') {
            $annArgs = [string]$Matches[2]
            $verb = "GET"
            if ($annArgs -match 'RequestMethod\.(\w+)') { $verb = $Matches[1].ToUpper() }
            $pending = @{ kind = "http"; verb = $verb; path = (Get-MappingPath $annArgs) }
        } elseif ($l -match '^\s*@Scheduled\b') {
            $pending = @{ kind = "job"; verb = ""; path = "" }
        } elseif ($l -match '^\s*@\w*Listener\b') {
            $pending = @{ kind = "listener"; verb = ""; path = "" }
        } elseif ($pending -and (Get-MethodDeclarationName $l)) {
            $method = Get-MethodDeclarationName $l
            if ($pending.kind -eq "http") {
                $full = Join-UrlPath $basePath $pending.path
                $id = "$($pending.verb) $full"
            } else {
                $full = ""
                $id = "$($pending.kind) $ClassName.$method"
            }
            $flows.Add([pscustomobject]@{
                id = $id; kind = $pending.kind; class = $ClassName; method = $method
                verb = $pending.verb; path = $full
            })
            $pending = $null
        }
    }
    return $flows.ToArray()
}

function Get-MappingPath([string]$AnnotationArgs) {
    # Not named $Args: that is a PowerShell automatic variable and would bind to nothing.
    if ($AnnotationArgs -match '(?:value|path)\s*=\s*"([^"]*)"') { return $Matches[1] }
    if ($AnnotationArgs -match '^\s*"([^"]*)"') { return $Matches[1] }
    return ""
}

function Join-UrlPath([string]$Base, [string]$Path) {
    $b = ([string]$Base).TrimEnd('/')
    $p = ([string]$Path).TrimStart('/')
    if (-not $p) { if ($b) { return $b } else { return "/" } }
    return "$b/$p"
}

function Get-ReachSet {
    # Symbols reached from one method, following calls through injected fields and same-class calls.
    # Returns "Class#method" strings. Calls into JDK or library types are not followed.
    param($Index, [string]$ClassName, [string]$MethodName, [int]$MaxDepth = 4)
    $seen = @{}
    $result = New-Object System.Collections.Generic.List[string]
    Add-ReachItem -Index $Index -ClassName $ClassName -MethodName $MethodName -Depth 0 -MaxDepth $MaxDepth -Seen $seen -Result $result
    return $result.ToArray()
}

function Add-ReachItem {
    param($Index, [string]$ClassName, [string]$MethodName, [int]$Depth, [int]$MaxDepth, $Seen, $Result)
    $key = "$ClassName#$MethodName"
    if ($Seen.ContainsKey($key)) { return }
    $Seen[$key] = $true
    $Result.Add($key)
    if ($Depth -ge $MaxDepth) { return }
    if (-not $Index.ContainsKey($ClassName)) { return }
    $entry = $Index[$ClassName]
    $body = Get-MethodBody -Text $entry.text -MethodName $MethodName
    if (-not $body) { return }
    foreach ($c in [regex]::Matches($body, '(?<![\w.])(\w+)\.(\w+)\s*\(')) {
        $recv = $c.Groups[1].Value
        $meth = $c.Groups[2].Value
        $target = $null
        if ($recv -eq "this") { $target = $ClassName }
        elseif ($entry.fields.ContainsKey($recv)) { $target = $entry.fields[$recv] }
        if ($target -and $Index.ContainsKey($target) -and $Index[$target].methods.ContainsKey($meth)) {
            Add-ReachItem -Index $Index -ClassName $target -MethodName $meth -Depth ($Depth + 1) -MaxDepth $MaxDepth -Seen $Seen -Result $Result
        }
    }
    foreach ($c in [regex]::Matches($body, '(?<![\w.])(\w+)\s*\(')) {
        $name = $c.Groups[1].Value
        if ($name -ne $MethodName -and $entry.methods.ContainsKey($name)) {
            Add-ReachItem -Index $Index -ClassName $ClassName -MethodName $name -Depth ($Depth + 1) -MaxDepth $MaxDepth -Seen $Seen -Result $Result
        }
    }
}

# ---------- rules ----------

function Test-RuleApplies {
    # Source-based rules run only on files that match the rule's fileGlob (default: every file).
    param($Rule, [string]$Path)
    if (-not $Rule.fileGlob) { return $true }
    return (Test-GlobMatch -Path $Path -Glob $Rule.fileGlob)
}

# ---------- verdict and flow scope ----------

function Get-FlowScope {
    # Approach section 6 and 7 (Phase 6). "full" keeps the whole regression for the flow;
    # "targeted" is the reduced scope (targeted tests plus a smoke suite); "smoke" is smoke only.
    # A flow is reduced only when every affecting finding is verified, all reached code is covered
    # and traced, and no finding is confirmed as a behavior change.
    param([string[]]$AffectingStatuses, [int]$UntracedCount, [bool]$AnyConfirmed)
    if ($AffectingStatuses.Count -eq 0 -and -not $AnyConfirmed -and $UntracedCount -eq 0) { return "smoke" }
    if ($AnyConfirmed) { return "full" }
    if ($UntracedCount -gt 0) { return "full" }
    $allVerified = @($AffectingStatuses | Where-Object { $_ -ne "verified" }).Count -eq 0
    if ($allVerified) { return "targeted" }
    return "full"
}

function Get-Verdict {
    # Approach section 10. Input is the list of blocking reasons and listed items, and whether
    # every required stage ran. Output: verdict (blocked | safe-with-listed-tests | safe) and exit code.
    param([string[]]$BlockingReasons = @(), [string[]]$ListedItems = @(), [bool]$RequiredStagesRan = $true)
    if ($BlockingReasons.Count -gt 0) {
        return [pscustomobject]@{ verdict = "blocked"; exitCode = 1 }
    }
    if (-not $RequiredStagesRan -or $ListedItems.Count -gt 0) {
        return [pscustomobject]@{ verdict = "safe-with-listed-tests"; exitCode = 0 }
    }
    return [pscustomobject]@{ verdict = "safe"; exitCode = 0 }
}

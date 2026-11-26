<#
  UpgradeLens library API functions (stage 3, library API diff).

  The method, without any tool beyond the JDK:
    1. jdeps -verbose:class on each side's compiled classes lists every library class the code refers to.
    2. javap -c -p -l on each side's compiled classes lists every library method and field the code calls,
       with the source line (from the LineNumberTable).
    3. Each referenced library class is found in the jars of that side's classpath, and javap -public -s
       prints its public API with JVM descriptors, so overloads compare exactly.
    4. The two APIs are compared per class; a removed class is matched by simple name to a moved one.
    5. Each change is tied to the code that uses it, on master and on change_branch.

  Parsers and the comparison are pure functions (tests\upgradelens.Tests.ps1 calls them).
  Get-JarClassIndex reads jar files. Dot-source after lib\core.ps1.
#>

function ConvertFrom-JdepsVerbose {
    # "   a.b.App   -> x.y.Lib   not found" (or a module or jar name as the location).
    param([string[]]$Lines)
    $rows = New-Object System.Collections.Generic.List[object]
    foreach ($l in $Lines) {
        if (([string]$l) -match '^\s+(?<from>[\w$.]+)\s+->\s+(?<to>[\w$.]+)\s+(?<loc>\S.*?)\s*$') {
            $rows.Add([pscustomobject]@{ from = $Matches['from']; to = $Matches['to']; location = $Matches['loc'] })
        }
    }
    return $rows.ToArray()
}

function Get-JavapMemberName {
    # Member name from a javap declaration line (without the trailing ";"). Constructors are "<init>".
    param([string]$Declaration, [string]$ClassName)
    $d = $Declaration -replace '\s+throws\s+.*$', ''
    if ($d -match '^\s*static\s*\{\s*\}\s*$' -or $d -match '^\s*static\s*\{\}\s*$') { return "<clinit>" }
    $paren = $d.IndexOf('(')
    if ($paren -ge 0) {
        $before = $d.Substring(0, $paren).TrimEnd()
        $token = ($before -split '\s+')[-1]
        if ($token -eq $ClassName) { return "<init>" }
        return $token
    }
    $tokens = @(($d -replace '\s*=.*$', '').Trim() -split '\s+')
    return $tokens[-1]
}

function Get-JavapClassName {
    # fqcn from a javap class header line ("public final class a.b.C<T> extends ... {").
    param([string]$Header)
    if ($Header -match '(?:^|\s)(?:class|interface|enum|record|@interface)\s+(?<n>[\w$.]+)') { return $Matches['n'] }
    return $null
}

function ConvertFrom-JavapApi {
    # Output of "javap -public -s" for one or more classes. Returns a hashtable fqcn -> class API:
    #   header   the class declaration line (modifiers, supertypes)
    #   members  list of { key = name + descriptor; name; descriptor; declaration }
    param([string[]]$Lines)
    $classes = @{}
    $cur = $null
    $pending = $null
    foreach ($raw in $Lines) {
        $l = ([string]$raw).TrimEnd()
        if ($l -match '^Compiled from' -or $l -match '^Error:' -or $l -match '^Warning:') { continue }
        if (-not $cur -and $l -match '^\S.*\{$') {
            $name = Get-JavapClassName $l
            if ($name) {
                $cur = [pscustomobject]@{ fqcn = $name; header = $l.TrimEnd('{').Trim(); members = (New-Object System.Collections.Generic.List[object]) }
            }
            continue
        }
        if ($cur -and $l -eq '}') {
            # Stored as an array: Windows PowerShell 5.1 cannot @()-wrap a generic List held in a property.
            $classes[$cur.fqcn] = [pscustomobject]@{ fqcn = $cur.fqcn; header = $cur.header; members = $cur.members.ToArray() }
            $cur = $null; $pending = $null
            continue
        }
        if (-not $cur) { continue }
        if ($l -match '^\s+descriptor:\s+(?<d>\S+)\s*$') {
            if ($pending) {
                $name = Get-JavapMemberName -Declaration $pending -ClassName $cur.fqcn
                $cur.members.Add([pscustomobject]@{ key = "$name$($Matches['d'])"; name = $name; descriptor = $Matches['d']; declaration = $pending })
                $pending = $null
            }
            continue
        }
        if ($l -match '^\s{2}(?<d>\S.*);$') { $pending = $Matches['d'].Trim() }
    }
    return $classes
}

function ConvertFrom-JavapCode {
    # Output of "javap -c -p -l" for application classes. Returns one row per library or application
    # reference made from code: { appClass; method; line; kind (method|field|class); owner; member; descriptor }.
    # "method" is the source method: lambdas (lambda$name$0) map to their enclosing method, constructors to
    # the simple class name, static initializers to "<clinit>".
    param([string[]]$Lines)
    $rows = New-Object System.Collections.Generic.List[object]
    $cls = $null
    $method = $null
    $refs = New-Object System.Collections.Generic.List[object]
    $table = New-Object System.Collections.Generic.List[object]
    $mode = ""

    $flush = {
        foreach ($r in $refs) {
            $line = $null; $best = -1
            foreach ($t in $table) { if ($t.offset -le $r.offset -and $t.offset -gt $best) { $best = $t.offset; $line = $t.line } }
            $rows.Add([pscustomobject]@{ appClass = $cls; method = $method; line = $line; kind = $r.kind; owner = $r.owner; member = $r.member; descriptor = $r.descriptor })
        }
        $refs.Clear(); $table.Clear()
    }

    foreach ($raw in $Lines) {
        $l = ([string]$raw).TrimEnd()
        if ($l -match '^Compiled from') { continue }
        if (-not $cls -and $l -match '^\S.*\{$') { $cls = Get-JavapClassName $l; $method = $null; $mode = ""; continue }
        if ($cls -and $l -eq '}') { & $flush; $cls = $null; $method = $null; continue }
        if (-not $cls) { continue }
        if ($l -match '^\s{2}(?<d>\S.*);$') {
            & $flush
            $name = Get-JavapMemberName -Declaration $Matches['d'] -ClassName $cls
            if ($name -eq "<init>") { $name = ($cls -split '[.$]')[-1] }
            if ($name -match '^lambda\$(.+)\$\d+$') { $name = $Matches[1] }
            $method = $name
            $mode = ""
            continue
        }
        if ($l -match '^\s+LineNumberTable:') { $mode = "lnt"; continue }
        if ($l -match '^\s+(LocalVariableTable|LocalVariableTypeTable|StackMapTable|Exceptions|RuntimeVisible\w*):') { $mode = ""; continue }
        if ($l -match '^\s+Code:') { $mode = "code"; continue }
        if ($mode -eq "lnt" -and $l -match '^\s+line (?<l>\d+): (?<o>\d+)') {
            $table.Add([pscustomobject]@{ line = [int]$Matches['l']; offset = [int]$Matches['o'] })
            continue
        }
        if ($mode -eq "code" -and $l -match '^\s+(?<off>\d+):\s+\w+.*?//\s+(?<kind>Method|InterfaceMethod|Field|class)\s+(?<ref>.+)$') {
            $off = [int]$Matches['off']; $kind = $Matches['kind']; $ref = $Matches['ref'].Trim()
            if ($ref.StartsWith('"[')) { continue }
            if ($kind -eq "class") {
                if ($ref.StartsWith('"')) { continue }
                $refs.Add([pscustomobject]@{ offset = $off; kind = "class"; owner = ($ref -replace '/', '.'); member = ""; descriptor = "" })
                continue
            }
            if ($ref -match '^(?:(?<owner>[\w/$]+)\.)?"?(?<name>[\w$<>]+)"?:(?<desc>\S+)$') {
                $owner = if ($Matches['owner']) { $Matches['owner'] -replace '/', '.' } else { $cls }
                $refs.Add([pscustomobject]@{ offset = $off; kind = $(if ($kind -eq "Field") { "field" } else { "method" }); owner = $owner; member = $Matches['name']; descriptor = $Matches['desc'] })
            }
        }
    }
    & $flush
    return $rows.ToArray()
}

function ConvertFrom-JarPath {
    # Maven coordinates from a jar path in a local repository (".../repository/g/r/p/artifact/version/artifact-version.jar").
    # A jar outside a repository layout is named by its file name.
    param([string]$JarPath)
    $p = $JarPath -replace '\\', '/'
    if ($p -match '/repository/(?<g>.+)/(?<a>[^/]+)/(?<v>[^/]+)/[^/]+\.jar$') {
        return [pscustomobject]@{ ga = (($Matches['g'] -replace '/', '.') + ":" + $Matches['a']); version = $Matches['v']; label = (($Matches['g'] -replace '/', '.') + ":" + $Matches['a'] + ":" + $Matches['v']) }
    }
    $leaf = [System.IO.Path]::GetFileNameWithoutExtension($p)
    return [pscustomobject]@{ ga = $leaf; version = ""; label = $leaf }
}

function Get-JarClassIndex {
    # Finds the jar of each wanted class, and the classes whose simple name is in $SimpleNames (to match a
    # removed class with a moved one). Reads the jar directories only; no class is loaded.
    param([string[]]$Jars, [string[]]$Classes = @(), [string[]]$SimpleNames = @())
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $wanted = @{}
    foreach ($c in $Classes) { $wanted[(($c -replace '\.', '/') + ".class")] = $c }
    $simple = @{}
    foreach ($s in $SimpleNames) { $simple[$s] = $true }
    $found = @{}
    $bySimple = @{}
    foreach ($jar in $Jars) {
        if (-not $jar -or -not $jar.EndsWith(".jar") -or -not (Test-Path -LiteralPath $jar)) { continue }
        $zip = $null
        try {
            $zip = [System.IO.Compression.ZipFile]::OpenRead($jar)
            foreach ($e in $zip.Entries) {
                $n = $e.FullName
                if (-not $n.EndsWith(".class") -or $n.StartsWith("META-INF/")) { continue }
                if ($wanted.ContainsKey($n) -and -not $found.ContainsKey($wanted[$n])) { $found[$wanted[$n]] = $jar }
                if ($simple.Count -gt 0) {
                    $leaf = [System.IO.Path]::GetFileNameWithoutExtension(($n -split '/')[-1])
                    if ($simple.ContainsKey($leaf)) {
                        $fq = ($n.Substring(0, $n.Length - 6)) -replace '/', '.'
                        if (-not $bySimple.ContainsKey($leaf)) { $bySimple[$leaf] = New-Object System.Collections.Generic.List[string] }
                        if (-not $bySimple[$leaf].Contains($fq)) { $bySimple[$leaf].Add($fq) }
                    }
                }
            }
        } catch {
            # An unreadable jar is skipped; its classes count as not found, which the diff reports.
        } finally {
            if ($zip) { $zip.Dispose() }
        }
    }
    return [pscustomobject]@{ jarByClass = $found; bySimpleName = $bySimple }
}

function Compare-LibraryClassApi {
    # Compares one class's public API on the two sides. State: same, changed, removed, added.
    # Members with the same name on both sides but a different descriptor are "changed" (an overload whose
    # signature changed), not removed plus added.
    param($Base, $Head)
    if ($null -eq $Base -and $null -eq $Head) { return $null }
    if ($null -eq $Head) { return [pscustomobject]@{ state = "removed"; headerChanged = $false; removed = @($Base.members); added = @(); changed = @() } }
    if ($null -eq $Base) { return [pscustomobject]@{ state = "added"; headerChanged = $false; removed = @(); added = @($Head.members); changed = @() } }
    $bk = @{}; foreach ($m in @($Base.members)) { $bk[$m.key] = $m }
    $hk = @{}; foreach ($m in @($Head.members)) { $hk[$m.key] = $m }
    $removed = @($Base.members | Where-Object { -not $hk.ContainsKey($_.key) })
    $added = @($Head.members | Where-Object { -not $bk.ContainsKey($_.key) })
    $changed = New-Object System.Collections.Generic.List[object]
    $removedNames = @($removed | ForEach-Object { $_.name } | Sort-Object -Unique)
    foreach ($n in $removedNames) {
        $a = @($added | Where-Object { $_.name -eq $n })
        if ($a.Count -gt 0) {
            $changed.Add([pscustomobject]@{
                name = $n
                base = @($removed | Where-Object { $_.name -eq $n } | ForEach-Object { $_.declaration })
                head = @($a | ForEach-Object { $_.declaration })
                baseKeys = @($removed | Where-Object { $_.name -eq $n } | ForEach-Object { $_.key })
            })
        }
    }
    $changedNames = @($changed | ForEach-Object { $_.name })
    $removedOnly = @($removed | Where-Object { $changedNames -notcontains $_.name })
    $addedOnly = @($added | Where-Object { $changedNames -notcontains $_.name })
    $headerChanged = ($Base.header -ne $Head.header)
    $state = if ($removed.Count -eq 0 -and $added.Count -eq 0 -and -not $headerChanged) { "same" } else { "changed" }
    return [pscustomobject]@{ state = $state; headerChanged = $headerChanged; removed = $removedOnly; added = $addedOnly; changed = $changed.ToArray() }
}

function Get-LibraryApiItems {
    # Builds the library API change list. Inputs, per side ("base", "head"):
    #   Deps      jdeps rows (ConvertFrom-JdepsVerbose)
    #   Code      code references (ConvertFrom-JavapCode)
    #   Api       hashtable fqcn -> class API (ConvertFrom-JavapApi)
    #   Jars      hashtable fqcn -> jar path
    #   Sources   hashtable outer application fqcn -> @{ path = relative source path; lines = string[] }
    # MovedCandidates: hashtable removed fqcn -> list of head fqcn with the same simple name.
    # Returns the items (state other than "same"), ordered by severity then class, and the counts.
    param($Deps, $Code, $Api, $Jars, $Sources, $MovedCandidates = @{}, [int]$MaxMembers = 40)
    $sevRank = @{ high = 0; medium = 1; low = 2; info = 3 }
    $appClasses = @{}
    foreach ($side in @("base", "head")) {
        foreach ($r in @($Deps[$side])) { $appClasses[$r.from] = $true }
    }
    $libClasses = @{}
    foreach ($side in @("base", "head")) {
        foreach ($r in @($Deps[$side])) {
            if ($r.location -eq "not found" -and -not $appClasses.ContainsKey($r.to)) { $libClasses[$r.to] = $true }
        }
    }

    function Get-Usage($Side, [string]$LibClass) {
        $out = New-Object System.Collections.Generic.List[object]
        $seen = @{}
        foreach ($c in @($Code[$Side] | Where-Object { $_.owner -eq $LibClass })) {
            $outer = ($c.appClass -split '\$')[0]
            $src = if ($Sources[$Side] -and $Sources[$Side].ContainsKey($outer)) { $Sources[$Side][$outer].path } else { "" }
            $key = "$($c.appClass)|$($c.method)|$($c.line)|$($c.member)$($c.descriptor)"
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            $out.Add([pscustomobject]@{ appClass = $c.appClass; method = $c.method; source = $src; line = $c.line; member = $c.member; descriptor = $c.descriptor })
        }
        # Class-level references (annotations, field and parameter types) that no code line shows: the
        # source line that names the class (usually the import) is the usage site.
        $simple = ($LibClass -split '[.$]')[-1]
        foreach ($r in @($Deps[$Side] | Where-Object { $_.to -eq $LibClass })) {
            if (@($out | Where-Object { $_.appClass -eq $r.from }).Count -gt 0) { continue }
            $outer = ($r.from -split '\$')[0]
            $src = ""; $line = $null
            if ($Sources[$Side] -and $Sources[$Side].ContainsKey($outer)) {
                $src = $Sources[$Side][$outer].path
                $lines = @($Sources[$Side][$outer].lines)
                for ($i = 0; $i -lt $lines.Count; $i++) {
                    if ($lines[$i] -match ('\b' + [regex]::Escape($simple) + '\b')) { $line = $i + 1; break }
                }
            }
            $key = "$($r.from)|class"
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            $out.Add([pscustomobject]@{ appClass = $r.from; method = ""; source = $src; line = $line; member = ""; descriptor = "" })
        }
        return $out.ToArray()
    }

    function Get-Artifact($Side, [string]$Class) {
        if ($Jars[$Side] -and $Jars[$Side].ContainsKey($Class)) { return (ConvertFrom-JarPath $Jars[$Side][$Class]).label }
        return "-"
    }

    $movedTargets = @{}
    $counts = @{ same = 0; changed = 0; removed = 0; moved = 0; added = 0 }
    $items = New-Object System.Collections.Generic.List[object]
    foreach ($cls in @($libClasses.Keys | Sort-Object)) {
        $b = $Api["base"][$cls]; $h = $Api["head"][$cls]
        $cmp = Compare-LibraryClassApi -Base $b -Head $h
        if ($null -eq $cmp) { continue }
        if ($cmp.state -eq "same") { $counts.same++; continue }
        $usage = [pscustomobject]@{ base = @(Get-Usage "base" $cls); head = @(Get-Usage "head" $cls) }
        $state = $cmp.state
        $movedTo = @()
        if ($state -eq "removed" -and $MovedCandidates.ContainsKey($cls)) {
            $movedTo = @($MovedCandidates[$cls] | Where-Object { $_ -ne $cls } | Sort-Object)
            if ($movedTo.Count -gt 0) { $state = "moved"; foreach ($t in $movedTo) { $movedTargets[$t] = $cls } }
        }
        $counts[$state]++

        $removedUsed = New-Object System.Collections.Generic.List[object]
        $removedKeys = @{}
        foreach ($m in @($cmp.removed)) { $removedKeys[$m.key] = $m.declaration }
        foreach ($c in @($cmp.changed)) { foreach ($k in @($c.baseKeys)) { $removedKeys[$k] = "$($c.name) (signature changed)" } }
        foreach ($u in @($usage.base | Where-Object { $_.member })) {
            if ($removedKeys.ContainsKey("$($u.member)$($u.descriptor)")) { $removedUsed.Add($u) }
        }

        $severity = "info"
        $summary = ""
        # A class that a removed class moved to is reported with the moved class, not again as "added".
        # (Classes sort by name, so the target may come first: it is filtered again after the loop.)
        if ($state -eq "added" -and $movedTargets.ContainsKey($cls)) { $counts.added--; continue }
        switch ($state) {
            { $_ -in @("removed", "moved") } {
                if (@($usage.base).Count -gt 0) {
                    $severity = "high"
                    $summary = "not on the change_branch classpath$(if ($movedTo.Count -gt 0) { '; moved to ' + ($movedTo -join ', ') }). Master code used it in $(@($usage.base | ForEach-Object { $_.appClass } | Sort-Object -Unique).Count) class(es)."
                } else {
                    $summary = "not on the change_branch classpath; no master code uses it directly."
                }
            }
            "added" {
                $summary = "new library class used by change_branch code."
            }
            "changed" {
                if ($removedUsed.Count -gt 0) {
                    $severity = "high"
                    $summary = "$($removedUsed.Count) member call(s) on master use a member that is removed or changed on change_branch."
                } elseif (@($usage.head).Count -gt 0 -and (@($cmp.removed).Count -gt 0 -or @($cmp.changed).Count -gt 0 -or $cmp.headerChanged)) {
                    $severity = "medium"
                    $summary = "public API changed ($(@($cmp.removed).Count) removed, $(@($cmp.changed).Count) changed, $(@($cmp.added).Count) added$(if ($cmp.headerChanged) { ', class declaration changed' })); change_branch code uses this class, but not the removed members."
                } elseif (@($usage.head).Count -gt 0) {
                    $severity = "low"
                    $summary = "only additions ($(@($cmp.added).Count) member(s)); the members the code uses are unchanged."
                } else {
                    $summary = "public API changed; no change_branch code uses this class directly."
                }
            }
        }

        $items.Add([pscustomobject]@{
            class          = $cls
            state          = $state
            severity       = $severity
            summary        = $summary
            artifact       = [pscustomobject]@{ base = (Get-Artifact "base" $cls); head = (Get-Artifact "head" $(if ($movedTo.Count -gt 0) { $movedTo[0] } else { $cls })) }
            movedTo        = $movedTo
            header         = [pscustomobject]@{ base = $(if ($b) { $b.header } else { "" }); head = $(if ($h) { $h.header } else { "" }); changed = $cmp.headerChanged }
            removedMembers = @($cmp.removed | Select-Object -First $MaxMembers | ForEach-Object { $_.declaration })
            changedMembers = @($cmp.changed | Select-Object -First $MaxMembers | ForEach-Object { [pscustomobject]@{ name = $_.name; base = @($_.base); head = @($_.head) } })
            addedMembers   = @($cmp.added | Select-Object -First $MaxMembers | ForEach-Object { $_.declaration })
            memberCounts   = [pscustomobject]@{ removed = @($cmp.removed).Count; changed = @($cmp.changed).Count; added = @($cmp.added).Count }
            removedUsed    = @($removedUsed | ForEach-Object { [pscustomobject]@{ appClass = $_.appClass; method = $_.method; source = $_.source; line = $_.line; member = $_.member; declaration = $removedKeys["$($_.member)$($_.descriptor)"] } })
            usage          = $usage
        })
    }

    # Drop "added" items that turned out to be the target of a moved class later in the loop.
    $kept = @($items | Where-Object { -not ($_.state -eq "added" -and $movedTargets.ContainsKey($_.class)) })
    $counts.added -= ($items.Count - $kept.Count)
    $sorted = @($kept | Sort-Object -Property @{ e = { $sevRank[$_.severity] } }, class)
    $n = 0
    foreach ($it in $sorted) { $n++; $it | Add-Member -NotePropertyName id -NotePropertyValue ("LIB-{0:D3}" -f $n) -Force }
    return [pscustomobject]@{
        referencedClasses = $libClasses.Count
        counts            = [pscustomobject]$counts
        items             = $sorted
    }
}

# Unit tests for the UpgradeLens pure functions (scripts\lib\core.ps1 and scripts\lib\analysis.ps1).
# Pester 3 syntax (installed here). Run from the repository root:
#   Invoke-Pester .\tests\upgradelens.Tests.ps1
# No Maven, no server and no git are needed: the functions take values.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
. (Join-Path $root "scripts\lib\core.ps1")
. (Join-Path $root "scripts\lib\analysis.ps1")
. (Join-Path $root "scripts\lib\library.ps1")
. (Join-Path $root "scripts\lib\orchestration.ps1")

Describe "ConvertFrom-DependencyTree" {
    It "reads depth from the tree prefix and the direct flag" {
        $lines = @(
            "com.example:orders-service:jar:0.0.1-SNAPSHOT",
            "+- org.springframework.boot:spring-boot-starter-webmvc:jar:4.2.0:compile",
            "|  \- org.springframework:spring-web:jar:7.1.0:compile",
            "\- org.springframework.boot:spring-boot-starter-test:jar:4.2.0:test"
        )
        $t = @(ConvertFrom-DependencyTree $lines)
        $t.Count | Should Be 4
        ($t | Where-Object { $_.ga -eq "org.springframework:spring-web" }).depth | Should Be 2
        ($t | Where-Object { $_.ga -eq "org.springframework:spring-web" }).direct | Should Be $false
        ($t | Where-Object { $_.ga -eq "org.springframework.boot:spring-boot-starter-test" }).direct | Should Be $true
        ($t | Where-Object { $_.ga -eq "org.springframework.boot:spring-boot-starter-test" }).scope | Should Be "test"
    }

    It "reads the classifier form (six parts)" {
        $t = @(ConvertFrom-DependencyTree @("+- com.x:lib:jar:tests:1.2.3:test"))
        $t[0].version | Should Be "1.2.3"
        $t[0].scope | Should Be "test"
    }
}

Describe "Compare-DependencyTrees" {
    It "reports added, removed, changed, major and direct changes" {
        $base = @(
            [pscustomobject]@{ ga = "a:starter-web"; version = "3.2.5"; depth = 1; direct = $true },
            [pscustomobject]@{ ga = "x:lib"; version = "2.0.0"; depth = 2; direct = $false },
            [pscustomobject]@{ ga = "y:old"; version = "1.0"; depth = 1; direct = $true }
        )
        $head = @(
            [pscustomobject]@{ ga = "a:starter-webmvc"; version = "4.2.0"; depth = 1; direct = $true },
            [pscustomobject]@{ ga = "x:lib"; version = "3.0.0"; depth = 2; direct = $false },
            [pscustomobject]@{ ga = "y:old"; version = "1.0"; depth = 1; direct = $true }
        )
        $d = Compare-DependencyTrees -Base $base -Head $head
        @($d.added).Count | Should Be 1
        @($d.removed).Count | Should Be 1
        @($d.changed).Count | Should Be 1
        $d.changed[0].ga | Should Be "x:lib"
        $d.changed[0].major | Should Be $true
        $d.changed[0].direct | Should Be $false
    }

    It "keeps the shallowest occurrence of an artifact" {
        $base = @([pscustomobject]@{ ga = "p:q"; version = "1"; depth = 3; direct = $false },
                  [pscustomobject]@{ ga = "p:q"; version = "2"; depth = 1; direct = $true })
        $head = @([pscustomobject]@{ ga = "p:q"; version = "2"; depth = 1; direct = $true })
        $d = Compare-DependencyTrees -Base $base -Head $head
        @($d.changed).Count | Should Be 0
    }
}

Describe "ConvertFrom-DependencyAnalyze" {
    It "splits unused and undeclared blocks" {
        $lines = @(
            "[WARNING] Used undeclared dependencies found:",
            "[WARNING]    org.springframework:spring-core:jar:7.1.0:compile",
            "[WARNING] Unused declared dependencies found:",
            "[WARNING]    org.springframework.boot:spring-boot-starter-test:jar:4.2.0:test",
            "[INFO] BUILD SUCCESS"
        )
        $a = ConvertFrom-DependencyAnalyze $lines
        @($a.undeclared).Count | Should Be 1
        @($a.unused).Count | Should Be 1
        $a.unused[0] | Should Match "starter-test"
    }
}

Describe "Read-UnifiedPatch" {
    It "keeps new and old line numbers for added and removed lines" {
        $lines = @(
            "diff --git a/src/A.java b/src/A.java",
            "--- a/src/A.java",
            "+++ b/src/A.java",
            "@@ -3,1 +3,2 @@",
            "-old line",
            "+new line",
            "+extra"
        )
        $p = Read-UnifiedPatch $lines
        $p.ContainsKey("src/A.java") | Should Be $true
        $p["src/A.java"].Removed[0].Line | Should Be 3
        $p["src/A.java"].Added[0].Line | Should Be 3
        $p["src/A.java"].Added[1].Text | Should Be "extra"
    }
}

Describe "Get-SymbolForLine" {
    $src = @(
        "public class Svc {",
        "    public String find(long id) {",
        "        return x;",
        "    }",
        "    @PostMapping(`"/x`")",
        "    public Order create(Req r) {",
        "    }",
        "}"
    )
    It "finds the enclosing method of a body line" {
        Get-SymbolForLine -Lines $src -LineNo 3 | Should Be "method:find"
    }
    It "attributes an annotation line to the declaration below it" {
        Get-SymbolForLine -Lines $src -LineNo 5 | Should Be "method:create"
    }
    It "reports a record declaration as a type" {
        $r = @("public record CreateOrderRequest(", "    @Positive long amountCents,", ")")
        Get-SymbolForLine -Lines $r -LineNo 2 | Should Be "type:CreateOrderRequest"
    }
    It "returns null outside any declaration" {
        Get-SymbolForLine -Lines @("package a;", "import b;") -LineNo 2 | Should BeNullOrEmpty
    }
}

Describe "Get-MappingPath (regression: a parameter named Args never binds)" {
    It "reads a bare quoted path" {
        Get-MappingPath '"/{id}"' | Should Be "/{id}"
    }
    It "reads value= before other attributes" {
        Get-MappingPath 'value = "/report.csv", produces = "text/csv"' | Should Be "/report.csv"
    }
    It "returns empty when there is no path" {
        Get-MappingPath '' | Should Be ""
    }
}

Describe "Get-SourceFlows and Get-ReachSet" {
    $controller = @(
        "package com.example.orders;",
        "@RestController",
        "@RequestMapping(`"/api/orders`")",
        "public class OrderController {",
        "    private final OrderService orders;",
        "    public OrderController(OrderService orders) { this.orders = orders; }",
        "    @GetMapping(`"/{id}`")",
        "    public Order get(@PathVariable long id) {",
        "        return orders.find(id).orElseThrow();",
        "    }",
        "    @PostMapping",
        "    public Order create(@RequestBody Req r) {",
        "        return orders.create(r);",
        "    }",
        "    @Scheduled(fixedRate = 1000)",
        "    public void tick() { }",
        "}"
    ) -join "`n"
    $service = @(
        "package com.example.orders;",
        "public class OrderService {",
        "    private final OrderRepository repository;",
        "    public OrderService(OrderRepository repository) { this.repository = repository; }",
        "    public Order create(Req r) { return repository.save(r); }",
        "    public Optional<Order> find(long id) { return repository.findById(id); }",
        "}"
    ) -join "`n"
    $repo = @(
        "package com.example.orders;",
        "public class OrderRepository {",
        "    public Order save(Req r) { return null; }",
        "    public Optional<Order> findById(long id) { return null; }",
        "}"
    ) -join "`n"

    $dir = Join-Path $TestDrive "src"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $dir "OrderController.java"), $controller)
    [System.IO.File]::WriteAllText((Join-Path $dir "OrderService.java"), $service)
    [System.IO.File]::WriteAllText((Join-Path $dir "OrderRepository.java"), $repo)
    $index = Get-SourceIndex -SourceRoot $dir

    It "builds the flow ids from class and method mappings" {
        $flows = @(Get-SourceFlows -Text $controller -ClassName "OrderController")
        ($flows | Where-Object { $_.method -eq "get" }).id | Should Be "GET /api/orders/{id}"
        ($flows | Where-Object { $_.method -eq "create" }).id | Should Be "POST /api/orders"
    }
    It "finds scheduled jobs as flows" {
        $flows = @(Get-SourceFlows -Text $controller -ClassName "OrderController")
        ($flows | Where-Object { $_.kind -eq "job" }).id | Should Be "job OrderController.tick"
    }
    It "follows injected fields down to the repository" {
        $reach = @(Get-ReachSet -Index $index -ClassName "OrderController" -MethodName "get")
        ($reach -contains "OrderController#get") | Should Be $true
        ($reach -contains "OrderService#find") | Should Be $true
        ($reach -contains "OrderRepository#findById") | Should Be $true
    }
    It "does not follow calls into library types" {
        $reach = @(Get-ReachSet -Index $index -ClassName "OrderController" -MethodName "get")
        @($reach | Where-Object { $_ -like "*Optional*" }).Count | Should Be 0
    }
}

Describe "Get-FlowCoverage and Test-FindingAffectsFlow" {
    $jac = @{ "com/example/A|run" = 12; "com/example/A|*" = 40; "com/example/A|dead" = 0 }
    $fqcn = @{ "A" = "com.example.A" }
    It "marks a method with zero covered instructions as untraced" {
        $rows = @(Get-FlowCoverage -Reach @("A#run", "A#dead", "A#missing") -Jacoco $jac -FqcnBySimple $fqcn)
        ($rows | Where-Object { $_.symbol -eq "A#run" }).traced | Should Be $true
        ($rows | Where-Object { $_.symbol -eq "A#dead" }).traced | Should Be $false
        ($rows | Where-Object { $_.symbol -eq "A#missing" }).traced | Should Be $false
    }
    It "treats a class-level symbol as traced when the class has coverage" {
        $rows = @(Get-FlowCoverage -Reach @("A#*") -Jacoco $jac -FqcnBySimple $fqcn)
        $rows[0].traced | Should Be $true
    }
    $flow = [pscustomobject]@{ id = "GET /x"; reach = @("Ctl#get", "Svc#find") }
    It "global findings touch every flow" {
        Test-FindingAffectsFlow -Finding ([pscustomobject]@{ scope = "global"; symbols = @() }) -Flow $flow | Should Be $true
    }
    It "symbol findings touch only flows that reach the symbol" {
        Test-FindingAffectsFlow -Finding ([pscustomobject]@{ scope = "symbol"; symbols = @("Svc#find") }) -Flow $flow | Should Be $true
        Test-FindingAffectsFlow -Finding ([pscustomobject]@{ scope = "symbol"; symbols = @("Svc#other") }) -Flow $flow | Should Be $false
    }
    It "a class-level symbol touches flows that reach any method of the class" {
        Test-FindingAffectsFlow -Finding ([pscustomobject]@{ scope = "symbol"; symbols = @("Svc#*") }) -Flow $flow | Should Be $true
    }
    It "test and none scopes never touch production flows" {
        Test-FindingAffectsFlow -Finding ([pscustomobject]@{ scope = "test"; symbols = @() }) -Flow $flow | Should Be $false
        Test-FindingAffectsFlow -Finding ([pscustomobject]@{ scope = "none"; symbols = @() }) -Flow $flow | Should Be $false
    }
}

Describe "Get-FlowScope" {
    It "is smoke when nothing affects the flow" {
        Get-FlowScope -AffectingStatuses @() -UntracedCount 0 -AnyConfirmed $false | Should Be "smoke"
    }
    It "is full when code is untraced, even if every finding is verified" {
        Get-FlowScope -AffectingStatuses @("verified") -UntracedCount 1 -AnyConfirmed $false | Should Be "full"
    }
    It "is full when a behavior change is confirmed" {
        Get-FlowScope -AffectingStatuses @("confirmed") -UntracedCount 0 -AnyConfirmed $true | Should Be "full"
    }
    It "is full while a finding is still open" {
        Get-FlowScope -AffectingStatuses @("verified", "open") -UntracedCount 0 -AnyConfirmed $false | Should Be "full"
    }
    It "is targeted only when every finding is verified and nothing is untraced" {
        Get-FlowScope -AffectingStatuses @("verified", "verified") -UntracedCount 0 -AnyConfirmed $false | Should Be "targeted"
    }
}

Describe "Get-FindingStatus" {
    $f = [pscustomobject]@{ id = "F-001"; ruleId = "R-X" }
    It "is open when no check is linked" {
        Get-FindingStatus -Finding $f -Checks @() | Should Be "open"
    }
    It "is verified when linked checks match master" {
        $c = @([pscustomobject]@{ ruleId = "R-X"; outcome = "same" })
        Get-FindingStatus -Finding $f -Checks $c | Should Be "verified"
    }
    It "is confirmed when a linked check differs" {
        $c = @([pscustomobject]@{ ruleId = "R-X"; outcome = "same" }, [pscustomobject]@{ ruleId = "R-X"; outcome = "different" })
        Get-FindingStatus -Finding $f -Checks $c | Should Be "confirmed"
    }
    It "is open, not confirmed, when a linked check did not run (error)" {
        $c = @([pscustomobject]@{ ruleId = "R-X"; outcome = "error" })
        Get-FindingStatus -Finding $f -Checks $c | Should Be "open"
    }
    It "is open when a linked test does not hold on master (unverified)" {
        $c = @([pscustomobject]@{ ruleId = "R-X"; outcome = "unverified" }, [pscustomobject]@{ ruleId = "R-X"; outcome = "same" })
        Get-FindingStatus -Finding $f -Checks $c | Should Be "open"
    }
    It "is accepted when a person accepted it, even if confirmed" {
        $c = @([pscustomobject]@{ ruleId = "R-X"; outcome = "different" })
        $d = @([pscustomobject]@{ findingId = "F-001"; decision = "accept" })
        Get-FindingStatus -Finding $f -Checks $c -Decisions $d | Should Be "accepted"
    }
}

Describe "Get-Verdict" {
    It "is blocked with exit 1 when any blocking reason exists" {
        $v = Get-Verdict -BlockingReasons @("x") -ListedItems @() -RequiredStagesRan $true
        $v.verdict | Should Be "blocked"
        $v.exitCode | Should Be 1
    }
    It "is safe-with-listed-tests when something is listed" {
        $v = Get-Verdict -BlockingReasons @() -ListedItems @("gap") -RequiredStagesRan $true
        $v.verdict | Should Be "safe-with-listed-tests"
        $v.exitCode | Should Be 0
    }
    It "is safe only when nothing blocks, nothing is listed and every required stage ran" {
        $v = Get-Verdict -BlockingReasons @() -ListedItems @() -RequiredStagesRan $true
        $v.verdict | Should Be "safe"
        $v.exitCode | Should Be 0
    }
    It "never gives safe when a required stage did not run" {
        $v = Get-Verdict -BlockingReasons @() -ListedItems @() -RequiredStagesRan $false
        $v.verdict | Should Be "safe-with-listed-tests"
    }
}

Describe "Get-InputHash and Get-MajorVersion" {
    It "changes when an input changes and is stable otherwise" {
        $a = Join-Path $TestDrive "a.txt"
        [System.IO.File]::WriteAllText($a, "one")
        $h1 = Get-InputHash @($a)
        $h2 = Get-InputHash @($a)
        $h1 | Should Be $h2
        [System.IO.File]::WriteAllText($a, "two")
        (Get-InputHash @($a)) | Should Not Be $h1
    }
    It "reads the major version" {
        Get-MajorVersion "4.2.0" | Should Be 4
        Get-MajorVersion "2.0" | Should Be 2
    }
}

Describe "Get-Category-like glob matching" {
    It "matches a path glob, with forward or back slashes" {
        Test-GlobMatch -Path "src\main\java\A.java" -Glob "src/main/*" | Should Be $true
        Test-GlobMatch -Path "pom.xml" -Glob "*.java" | Should Be $false
    }
}

# ---------- configuration ----------

Describe "Resolve-ConfigPath and Get-ConfigValue" {
    It "prefers -Config, then UPGRADELENS_CONFIG, then the default file" {
        $saved = $env:UPGRADELENS_CONFIG
        try {
            $env:UPGRADELENS_CONFIG = "C:\x\env.json"
            Resolve-ConfigPath -Config "C:\x\given.json" -RootDir "C:\root" | Should Be "C:\x\given.json"
            Resolve-ConfigPath -Config "" -RootDir "C:\root" | Should Be "C:\x\env.json"
            $env:UPGRADELENS_CONFIG = $null
            Resolve-ConfigPath -Config "" -RootDir "C:\root" | Should Be "C:\root\config\upgrade-lens.json"
        } finally { $env:UPGRADELENS_CONFIG = $saved }
    }
    It "reads dotted keys and falls back to the default for missing or empty values" {
        $cfg = '{ "timeouts": { "buildMinutes": 30 }, "maven": { "settingsFile": "" } }' | ConvertFrom-Json
        Get-ConfigValue $cfg "timeouts.buildMinutes" 60 | Should Be 30
        Get-ConfigValue $cfg "timeouts.startupSeconds" 120 | Should Be 120
        Get-ConfigValue $cfg "maven.settingsFile" "none" | Should Be "none"
        Get-ConfigValue $cfg "no.such.key" $null | Should BeNullOrEmpty
    }
}

# ---------- Java declarations and hunk-to-method mapping ----------

Describe "Get-MethodDeclarationName" {
    It "finds methods at every access level, generic and annotated" {
        Get-MethodDeclarationName "    Order create(CreateOrderRequest r) {" | Should Be "create"
        Get-MethodDeclarationName "    public Map<String, Long> totalsByCurrency() {" | Should Be "totalsByCurrency"
        Get-MethodDeclarationName "    protected static <T> List<T> copy(List<T> in) {" | Should Be "copy"
        Get-MethodDeclarationName "    @Override public String toString() {" | Should Be "toString"
        Get-MethodDeclarationName "    void createReturnsCreatedOrder() throws Exception {" | Should Be "createReturnsCreatedOrder"
        Get-MethodDeclarationName "    public Order create(" | Should Be "create"
    }
    It "finds constructors with or without an access modifier" {
        Get-MethodDeclarationName "    public OrderController(OrderService orders) {" | Should Be "OrderController"
        Get-MethodDeclarationName "    OrderService(OrderRepository repository) {" | Should Be "OrderService"
    }
    It "rejects calls, control statements, returns and local variables" {
        Get-MethodDeclarationName "        foo(bar);" | Should BeNullOrEmpty
        Get-MethodDeclarationName "        if (x) {" | Should BeNullOrEmpty
        Get-MethodDeclarationName "        return orders.find(id);" | Should BeNullOrEmpty
        Get-MethodDeclarationName "        Order first = service.create(req);" | Should BeNullOrEmpty
        Get-MethodDeclarationName "        throw new IllegalStateException(`"x`");" | Should BeNullOrEmpty
        Get-MethodDeclarationName "        } catch (Exception e) {" | Should BeNullOrEmpty
        Get-MethodDeclarationName "        Objects.requireNonNull(x);" | Should BeNullOrEmpty
        Get-MethodDeclarationName "        Foo(x);" | Should BeNullOrEmpty
        Get-MethodDeclarationName "public record CreateOrderRequest(" | Should BeNullOrEmpty
    }
}

Describe "Get-SymbolForLine (brace-aware)" {
    $src = @(
        "class Svc {",
        "    private int count = 0;",
        "    void tick() {",
        "        if (count > 0) {",
        "            count++;",
        "        }",
        "    }",
        "    private String name = `"{x}`";",
        "    public Order create(",
        "            String customer,",
        "            long amount) {",
        "        return null;",
        "    }",
        "}"
    )
    It "maps a body line of a package-private method to the method" {
        Get-SymbolForLine -Lines $src -LineNo 5 | Should Be "method:tick"
    }
    It "maps a field between two methods to the type, not to the method above" {
        Get-SymbolForLine -Lines $src -LineNo 8 | Should Be "type:Svc"
    }
    It "maps a parameter line of a multi-line declaration to the method" {
        Get-SymbolForLine -Lines $src -LineNo 10 | Should Be "method:create"
    }
    It "maps a declaration line to itself" {
        Get-SymbolForLine -Lines $src -LineNo 9 | Should Be "method:create"
    }
}

Describe "Get-MethodBody" {
    It "returns the body of a package-private method and ignores braces in strings" {
        $text = "class A {`n    void run() {`n        String s = `"}`";`n        go();`n    }`n    void other() { }`n}"
        $body = Get-MethodBody -Text $text -MethodName "run"
        $body | Should Match 'go\(\);'
        $body | Should Not Match 'other'
    }
}

# ---------- dependencies ----------

Describe "ConvertFrom-DependencyTree parent and ConvertFrom-DependencyTreeVerbose" {
    $lines = @(
        "com.example:orders-service:jar:0.0.1-SNAPSHOT",
        "+- org.springframework.boot:spring-boot-starter-webmvc:jar:4.2.0:compile",
        "|  +- org.springframework:spring-web:jar:7.1.0:compile (version managed from 7.0.0)",
        "|  \- (org.slf4j:slf4j-api:jar:2.0.20:compile - version managed from 2.0.19; omitted for duplicate)",
        "\- com.x:client:jar:1.0:compile",
        "   \- (com.fasterxml.jackson.core:jackson-databind:jar:2.17.0:compile - omitted for conflict with 3.1.0)"
    )
    It "records the parent of each resolved artifact and skips omitted ones" {
        $t = @(ConvertFrom-DependencyTree $lines)
        $t.Count | Should Be 4
        ($t | Where-Object { $_.ga -eq "org.springframework:spring-web" }).parent | Should Be "org.springframework.boot:spring-boot-starter-webmvc"
        @($t | Where-Object { $_.ga -eq "org.slf4j:slf4j-api" }).Count | Should Be 0
    }
    It "lists conflicts with who asked, duplicates and managed versions" {
        $v = ConvertFrom-DependencyTreeVerbose $lines
        @($v.conflicts).Count | Should Be 1
        $v.conflicts[0].ga | Should Be "com.fasterxml.jackson.core:jackson-databind"
        $v.conflicts[0].requested | Should Be "2.17.0"
        $v.conflicts[0].resolved | Should Be "3.1.0"
        $v.conflicts[0].major | Should Be $true
        @($v.conflicts[0].requiredBy) -contains "com.x:client" | Should Be $true
        $v.duplicates | Should Be 1
        ($v.managed | Where-Object { $_.ga -eq "org.springframework:spring-web" }).declared | Should Be "7.0.0"
    }
}

Describe "Get-ReplacementPairs" {
    It "pairs a removed starter with its renamed successor, not with the test starter" {
        $p = Get-ReplacementPairs -AddedGas @("org.springframework.boot:spring-boot-starter-webmvc", "org.springframework.boot:spring-boot-starter-webmvc-test") -RemovedGas @("org.springframework.boot:spring-boot-starter-web")
        $p["org.springframework.boot:spring-boot-starter-web"] | Should Be "org.springframework.boot:spring-boot-starter-webmvc"
    }
    It "pairs a relocated artifact (same artifactId, new groupId)" {
        $p = Get-ReplacementPairs -AddedGas @("tools.jackson.core:jackson-databind") -RemovedGas @("com.fasterxml.jackson.core:jackson-databind")
        $p["com.fasterxml.jackson.core:jackson-databind"] | Should Be "tools.jackson.core:jackson-databind"
    }
}

Describe "Get-DependencyAssessment" {
    $base = @(
        [pscustomobject]@{ ga = "org.springframework.boot:spring-boot-starter-web"; version = "3.2.5"; scope = "compile"; depth = 1; direct = $true; parent = "app" },
        [pscustomobject]@{ ga = "org.springframework:spring-web"; version = "6.1.6"; scope = "compile"; depth = 2; direct = $false; parent = "org.springframework.boot:spring-boot-starter-web" }
    )
    $head = @(
        [pscustomobject]@{ ga = "org.springframework.boot:spring-boot-starter-webmvc"; version = "4.2.0"; scope = "compile"; depth = 1; direct = $true; parent = "app" },
        [pscustomobject]@{ ga = "org.springframework:spring-web"; version = "7.1.0"; scope = "compile"; depth = 2; direct = $false; parent = "org.springframework.boot:spring-boot-starter-webmvc" },
        [pscustomobject]@{ ga = "org.new:thing"; version = "1.0"; scope = "compile"; depth = 2; direct = $false; parent = "org.springframework.boot:spring-boot-starter-webmvc" },
        [pscustomobject]@{ ga = "com.acme:unused-lib"; version = "2.0"; scope = "compile"; depth = 1; direct = $true; parent = "app" }
    )
    $diff = Compare-DependencyTrees -Base $base -Head $head
    $analyze = @{
        base = [pscustomobject]@{ unused = @("org.springframework.boot:spring-boot-starter-web:jar:3.2.5:compile"); undeclared = @("org.springframework:spring-web:jar:6.1.6:compile") }
        head = [pscustomobject]@{ unused = @("org.springframework.boot:spring-boot-starter-webmvc:jar:4.2.0:compile", "com.acme:unused-lib:jar:2.0:compile"); undeclared = @("org.springframework:spring-web:jar:7.1.0:compile") }
    }
    $rows = @(Get-DependencyAssessment -Diff $diff -BaseTree $base -HeadTree $head -Analyze $analyze)
    It "marks a starter as required when code uses an artifact it brings, and names what it replaces" {
        $r = $rows | Where-Object { $_.ga -eq "org.springframework.boot:spring-boot-starter-webmvc" }
        $r.assessment | Should Be "required"
        $r.replaces | Should Be "org.springframework.boot:spring-boot-starter-web"
        $r.introduces -contains "org.new:thing:1.0" | Should Be $true
    }
    It "marks a declared library that no code uses as unnecessary" {
        ($rows | Where-Object { $_.ga -eq "com.acme:unused-lib" }).assessment | Should Be "unnecessary"
    }
    It "marks a removed starter that has a successor as replaced" {
        ($rows | Where-Object { $_.change -eq "removed" }).assessment | Should Be "replaced"
    }
    It "honours the ignore list for unused artifacts" {
        $r2 = @(Get-DependencyAssessment -Diff $diff -BaseTree $base -HeadTree $head -Analyze $analyze -IgnoreUnused @("com.acme:*"))
        ($r2 | Where-Object { $_.ga -eq "com.acme:unused-lib" }).assessment | Should Be "required"
    }
}

Describe "Get-DependencySuggestions" {
    It "suggests a targeted tree per major conflict and convergence when conflicts exist" {
        $c = @([pscustomobject]@{ ga = "a:b"; requested = "1.0"; resolved = "2.0"; major = $true; requiredBy = @("x:y") })
        $an = @{ base = [pscustomobject]@{ undeclared = @() }; head = [pscustomobject]@{ undeclared = @() } }
        $s = @(Get-DependencySuggestions -Conflicts $c -Assessment @() -Analyze $an -VerboseAvailable $true)
        @($s | Where-Object { $_.command -like "*-Dincludes=a:b*" }).Count | Should Be 1
        @($s | Where-Object { $_.command -like "*dependencyConvergence*" }).Count | Should Be 1
    }
}

# ---------- rules on the new sources ----------

Describe "Get-RuleFindings for it-not-run and library-api" {
    It "reports IT classes only when stage 1 did not run them" {
        $rule = [pscustomobject]@{ id = "R-IT"; title = "t"; severity = "high"; source = "it-not-run"; scope = "global"; impact = "i"; check = "c" }
        @(Get-RuleFindings -Rule $rule -Ctx @{ ItMode = "surefire"; ItFiles = @("src/test/java/AIT.java") }).Count | Should Be 0
        @(Get-RuleFindings -Rule $rule -Ctx @{ ItMode = "not-run"; ItFiles = @("src/test/java/AIT.java") }).Count | Should Be 1
    }
    It "turns a high library item into a symbol finding on the code that used it" {
        $rule = [pscustomobject]@{ id = "R-LIB"; title = "t"; severity = "high"; source = "library-api"; match = "removed-used"; scope = "symbol"; impact = "i"; check = "c" }
        $item = [pscustomobject]@{ id = "LIB-001"; class = "com.fasterxml.jackson.databind.ObjectMapper"; severity = "high"; summary = "moved"
            artifact = [pscustomobject]@{ base = "a"; head = "b" }
            usage = [pscustomobject]@{ base = @([pscustomobject]@{ appClass = "com.example.orders.OrderControllerIT"; method = "create"; source = "src/test/java/com/example/orders/OrderControllerIT.java"; line = 40 }); head = @() } }
        $f = @(Get-RuleFindings -Rule $rule -Ctx @{ LibraryApi = [pscustomobject]@{ items = @($item) } })
        $f.Count | Should Be 1
        $f[0].symbols[0] | Should Be "OrderControllerIT#create"
        $f[0].line | Should Be 40
    }
}

# ---------- library API ----------

Describe "ConvertFrom-JdepsVerbose" {
    It "reads class-level dependencies with their location" {
        $rows = @(ConvertFrom-JdepsVerbose @(
            "classes -> java.base",
            "   com.example.orders.OrderController                 -> org.springframework.web.bind.annotation.GetMapping not found",
            "   com.example.orders.OrderController                 -> java.lang.Object                                    java.base"
        ))
        $rows.Count | Should Be 2
        $rows[0].to | Should Be "org.springframework.web.bind.annotation.GetMapping"
        $rows[0].location | Should Be "not found"
    }
}

Describe "ConvertFrom-JavapApi and Compare-LibraryClassApi" {
    $baseOut = @(
        'Compiled from "Mapper.java"',
        'public class a.b.Mapper {',
        '  public a.b.Mapper();',
        '    descriptor: ()V',
        '  public java.lang.String write(java.lang.Object);',
        '    descriptor: (Ljava/lang/Object;)Ljava/lang/String;',
        '  public void gone();',
        '    descriptor: ()V',
        '}'
    )
    $headOut = @(
        'Compiled from "Mapper.java"',
        'public final class a.b.Mapper {',
        '  public a.b.Mapper();',
        '    descriptor: ()V',
        '  public java.lang.String write(java.lang.Object, int);',
        '    descriptor: (Ljava/lang/Object;I)Ljava/lang/String;',
        '}'
    )
    $b = (ConvertFrom-JavapApi $baseOut)["a.b.Mapper"]
    $h = (ConvertFrom-JavapApi $headOut)["a.b.Mapper"]
    It "parses members with descriptors and names constructors <init>" {
        @($b.members).Count | Should Be 3
        ($b.members | Where-Object { $_.name -eq "<init>" }).descriptor | Should Be "()V"
    }
    It "reports a changed overload, a removed member and a changed class declaration" {
        $c = Compare-LibraryClassApi -Base $b -Head $h
        $c.state | Should Be "changed"
        $c.headerChanged | Should Be $true
        @($c.changed).Count | Should Be 1
        $c.changed[0].name | Should Be "write"
        @($c.removed | ForEach-Object { $_.name }) -contains "gone" | Should Be $true
    }
}

Describe "ConvertFrom-JavapCode" {
    $out = @(
        'Compiled from "OrderService.java"',
        'public class com.example.orders.OrderService {',
        '  public com.example.orders.OrderService(com.example.orders.OrderRepository);',
        '    Code:',
        '         0: aload_0',
        '         1: invokespecial #1                  // Method java/lang/Object."<init>":()V',
        '         4: return',
        '      LineNumberTable:',
        '        line 12: 0',
        '  public java.lang.String json(java.lang.Object);',
        '    Code:',
        '         0: aload_0',
        '         1: getfield      #7                  // Field mapper:Lcom/fasterxml/jackson/databind/ObjectMapper;',
        '         4: aload_1',
        '         5: invokevirtual #9                  // Method com/fasterxml/jackson/databind/ObjectMapper.writeValueAsString:(Ljava/lang/Object;)Ljava/lang/String;',
        '         8: areturn',
        '      LineNumberTable:',
        '        line 20: 0',
        '        line 21: 4',
        '  private static void lambda$list$0(java.lang.Object);',
        '    Code:',
        '         0: ldc           #11                 // class com/fasterxml/jackson/databind/JsonNode',
        '         2: return',
        '      LineNumberTable:',
        '        line 30: 0',
        '}'
    )
    $rows = @(ConvertFrom-JavapCode $out)
    It "maps each library call to its source method and line" {
        $call = $rows | Where-Object { $_.member -eq "writeValueAsString" }
        $call.owner | Should Be "com.fasterxml.jackson.databind.ObjectMapper"
        $call.method | Should Be "json"
        $call.line | Should Be 21
        $call.descriptor | Should Be "(Ljava/lang/Object;)Ljava/lang/String;"
    }
    It "uses the current class for unqualified fields, the class name for constructors, and the enclosing method for lambdas" {
        ($rows | Where-Object { $_.member -eq "mapper" }).owner | Should Be "com.example.orders.OrderService"
        ($rows | Where-Object { $_.member -eq "<init>" }).method | Should Be "OrderService"
        ($rows | Where-Object { $_.kind -eq "class" }).method | Should Be "list"
    }
}

Describe "ConvertFrom-JarPath" {
    It "reads Maven coordinates from a local repository path" {
        $j = ConvertFrom-JarPath "E:\m2\repository\org\springframework\spring-web\7.1.0\spring-web-7.1.0.jar"
        $j.ga | Should Be "org.springframework:spring-web"
        $j.version | Should Be "7.1.0"
    }
}

Describe "Get-LibraryApiItems" {
    $deps = @{
        base = @([pscustomobject]@{ from = "app.Svc"; to = "old.pkg.Mapper"; location = "not found" })
        head = @([pscustomobject]@{ from = "app.Svc"; to = "new.pkg.Mapper"; location = "not found" })
    }
    $code = @{
        base = @([pscustomobject]@{ appClass = "app.Svc"; method = "json"; line = 21; kind = "method"; owner = "old.pkg.Mapper"; member = "write"; descriptor = "()V" })
        head = @([pscustomobject]@{ appClass = "app.Svc"; method = "json"; line = 22; kind = "method"; owner = "new.pkg.Mapper"; member = "write"; descriptor = "()V" })
    }
    $m = { param($n) [pscustomobject]@{ fqcn = $n; header = "public class $n"; members = @([pscustomobject]@{ key = "write()V"; name = "write"; descriptor = "()V"; declaration = "public void write()" }) } }
    $api = @{ base = @{ "old.pkg.Mapper" = (& $m "old.pkg.Mapper") }; head = @{ "new.pkg.Mapper" = (& $m "new.pkg.Mapper") } }
    $jars = @{ base = @{ "old.pkg.Mapper" = "C:\r\repository\old\pkg\mapper\2.0\mapper-2.0.jar" }; head = @{ "new.pkg.Mapper" = "C:\r\repository\new\pkg\mapper\3.0\mapper-3.0.jar" } }
    $sources = @{ base = @{ "app.Svc" = @{ path = "src/main/java/app/Svc.java"; lines = @("import old.pkg.Mapper;") } }; head = @{} }
    $r = Get-LibraryApiItems -Deps $deps -Code $code -Api $api -Jars $jars -Sources $sources -MovedCandidates @{ "old.pkg.Mapper" = @("new.pkg.Mapper") }
    It "reports a class master used that moved package as one high item" {
        @($r.items).Count | Should Be 1
        $r.items[0].id | Should Be "LIB-001"
        $r.items[0].state | Should Be "moved"
        $r.items[0].severity | Should Be "high"
        $r.items[0].movedTo[0] | Should Be "new.pkg.Mapper"
        $r.items[0].artifact.head | Should Be "new.pkg:mapper:3.0"
        $r.items[0].usage.base[0].source | Should Be "src/main/java/app/Svc.java"
    }
}

# ---------- test results and patches ----------

Describe "ConvertFrom-SurefireReport and Compare-TestResults" {
    $xml = '<?xml version="1.0"?><testsuite name="a.T" tests="3"><testcase classname="a.T" name="ok"/><testcase classname="a.T" name="bad"><failure message="boom"/></testcase><testcase classname="a.T" name="off"><skipped/></testcase></testsuite>'
    $p = Join-Path $TestDrive "TEST-a.T.xml"
    [System.IO.File]::WriteAllText($p, $xml)
    It "reads each test case status" {
        $rows = @(ConvertFrom-SurefireReport -Paths @($p))
        ($rows | Where-Object { $_.id -eq "a.T#bad" }).status | Should Be "failed"
        ($rows | Where-Object { $_.id -eq "a.T#off" }).status | Should Be "skipped"
    }
    It "classifies differences between the two builds" {
        $b = @([pscustomobject]@{ id = "t1"; status = "passed" }, [pscustomobject]@{ id = "t2"; status = "passed" }, [pscustomobject]@{ id = "t3"; status = "passed" })
        $h = @([pscustomobject]@{ id = "t1"; status = "passed" }, [pscustomobject]@{ id = "t2"; status = "failed"; message = "x" }, [pscustomobject]@{ id = "t4"; status = "passed" })
        $c = @(Compare-TestResults -Base $b -Head $h)
        ($c | Where-Object { $_.test -eq "t1" }).change | Should Be "same"
        ($c | Where-Object { $_.test -eq "t2" }).change | Should Be "now-failing"
        ($c | Where-Object { $_.test -eq "t3" }).change | Should Be "removed"
        ($c | Where-Object { $_.test -eq "t4" }).change | Should Be "new"
    }
}

Describe "New-AddedFilePatch" {
    It "writes a git patch that adds one file" {
        $p = New-AddedFilePatch -RelativePath "src\test\java\A.java" -Lines @("class A {", "}")
        $p | Should Match 'diff --git a/src/test/java/A.java b/src/test/java/A.java'
        $p | Should Match '@@ -0,0 \+1,2 @@'
        $p | Should Match '\+class A \{'
    }
}

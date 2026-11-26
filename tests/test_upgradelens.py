"""Unit tests for the UpgradeLens pure functions (scripts\\lib\\core.py, analysis.py and library.py).

Run from the repository root:
    python -m pytest tests
No Maven, no server and no git are needed: the functions take values.
"""

import os

import pytest

from lib.analysis import (compare_test_results, dependency_assessment, dependency_suggestions, finding_affects_flow,
                          finding_status, flow_coverage, new_added_file_patch, parse_dependency_analyze,
                          parse_surefire_reports, read_unified_patch, replacement_pairs, rule_findings)
from lib.core import (compare_dependency_trees, config_value, flow_scope, glob_match, input_hash, major_version,
                      mapping_path, method_body, method_declaration_name, parse_dependency_tree,
                      parse_dependency_tree_verbose, ps_sorted, reach_set, resolve_config_path, source_flows, source_index,
                      split_lines, symbol_for_line, verdict)
from lib.library import (compare_library_class_api, jar_coordinates, library_api_items, parse_javap_api,
                         parse_javap_code, parse_jdeps_verbose)


def one(rows, **match):
    hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, f"expected one row matching {match}, found {len(hits)}"
    return hits[0]


# ---------- dependency tree ----------

class TestParseDependencyTree:
    def test_reads_depth_from_the_tree_prefix_and_the_direct_flag(self):
        lines = [
            "com.example:orders-service:jar:0.0.1-SNAPSHOT",
            "+- org.springframework.boot:spring-boot-starter-webmvc:jar:4.2.0:compile",
            "|  \\- org.springframework:spring-web:jar:7.1.0:compile",
            "\\- org.springframework.boot:spring-boot-starter-test:jar:4.2.0:test",
        ]
        t = parse_dependency_tree(lines)
        assert len(t) == 4
        web = one(t, ga="org.springframework:spring-web")
        assert web["depth"] == 2
        assert web["direct"] is False
        test = one(t, ga="org.springframework.boot:spring-boot-starter-test")
        assert test["direct"] is True
        assert test["scope"] == "test"

    def test_reads_the_classifier_form_six_parts(self):
        t = parse_dependency_tree(["+- com.x:lib:jar:tests:1.2.3:test"])
        assert t[0]["version"] == "1.2.3"
        assert t[0]["scope"] == "test"


class TestCompareDependencyTrees:
    def test_reports_added_removed_changed_major_and_direct_changes(self):
        base = [
            {"ga": "a:starter-web", "version": "3.2.5", "depth": 1, "direct": True},
            {"ga": "x:lib", "version": "2.0.0", "depth": 2, "direct": False},
            {"ga": "y:old", "version": "1.0", "depth": 1, "direct": True},
        ]
        head = [
            {"ga": "a:starter-webmvc", "version": "4.2.0", "depth": 1, "direct": True},
            {"ga": "x:lib", "version": "3.0.0", "depth": 2, "direct": False},
            {"ga": "y:old", "version": "1.0", "depth": 1, "direct": True},
        ]
        d = compare_dependency_trees(base, head)
        assert len(d["added"]) == 1
        assert len(d["removed"]) == 1
        assert len(d["changed"]) == 1
        assert d["changed"][0]["ga"] == "x:lib"
        assert d["changed"][0]["major"] is True
        assert d["changed"][0]["direct"] is False

    def test_keeps_the_shallowest_occurrence_of_an_artifact(self):
        base = [{"ga": "p:q", "version": "1", "depth": 3, "direct": False},
                {"ga": "p:q", "version": "2", "depth": 1, "direct": True}]
        head = [{"ga": "p:q", "version": "2", "depth": 1, "direct": True}]
        assert compare_dependency_trees(base, head)["changed"] == []


class TestParseDependencyAnalyze:
    def test_splits_unused_and_undeclared_blocks(self):
        lines = [
            "[WARNING] Used undeclared dependencies found:",
            "[WARNING]    org.springframework:spring-core:jar:7.1.0:compile",
            "[WARNING] Unused declared dependencies found:",
            "[WARNING]    org.springframework.boot:spring-boot-starter-test:jar:4.2.0:test",
            "[INFO] BUILD SUCCESS",
        ]
        a = parse_dependency_analyze(lines)
        assert len(a["undeclared"]) == 1
        assert len(a["unused"]) == 1
        assert "starter-test" in a["unused"][0]


class TestReadUnifiedPatch:
    def test_keeps_new_and_old_line_numbers_for_added_and_removed_lines(self):
        lines = [
            "diff --git a/src/A.java b/src/A.java",
            "--- a/src/A.java",
            "+++ b/src/A.java",
            "@@ -3,1 +3,2 @@",
            "-old line",
            "+new line",
            "+extra",
        ]
        p = read_unified_patch(lines)
        assert "src/A.java" in p
        assert p["src/A.java"]["removed"][0]["line"] == 3
        assert p["src/A.java"]["added"][0]["line"] == 3
        assert p["src/A.java"]["added"][1]["text"] == "extra"


# ---------- Java source reading ----------

class TestSymbolForLine:
    src = [
        "public class Svc {",
        "    public String find(long id) {",
        "        return x;",
        "    }",
        '    @PostMapping("/x")',
        "    public Order create(Req r) {",
        "    }",
        "}",
    ]

    def test_finds_the_enclosing_method_of_a_body_line(self):
        assert symbol_for_line(self.src, 3) == "method:find"

    def test_attributes_an_annotation_line_to_the_declaration_below_it(self):
        assert symbol_for_line(self.src, 5) == "method:create"

    def test_reports_a_record_declaration_as_a_type(self):
        r = ["public record CreateOrderRequest(", "    @Positive long amountCents,", ")"]
        assert symbol_for_line(r, 2) == "type:CreateOrderRequest"

    def test_returns_none_outside_any_declaration(self):
        assert symbol_for_line(["package a;", "import b;"], 2) is None


class TestMappingPath:
    def test_reads_a_bare_quoted_path(self):
        assert mapping_path('"/{id}"') == "/{id}"

    def test_reads_value_before_other_attributes(self):
        assert mapping_path('value = "/report.csv", produces = "text/csv"') == "/report.csv"

    def test_returns_empty_when_there_is_no_path(self):
        assert mapping_path("") == ""


CONTROLLER = "\n".join([
    "package com.example.orders;",
    "@RestController",
    '@RequestMapping("/api/orders")',
    "public class OrderController {",
    "    private final OrderService orders;",
    "    public OrderController(OrderService orders) { this.orders = orders; }",
    '    @GetMapping("/{id}")',
    "    public Order get(@PathVariable long id) {",
    "        return orders.find(id).orElseThrow();",
    "    }",
    "    @PostMapping",
    "    public Order create(@RequestBody Req r) {",
    "        return orders.create(r);",
    "    }",
    "    @Scheduled(fixedRate = 1000)",
    "    public void tick() { }",
    "}",
])
SERVICE = "\n".join([
    "package com.example.orders;",
    "public class OrderService {",
    "    private final OrderRepository repository;",
    "    public OrderService(OrderRepository repository) { this.repository = repository; }",
    "    public Order create(Req r) { return repository.save(r); }",
    "    public Optional<Order> find(long id) { return repository.findById(id); }",
    "}",
])
REPOSITORY = "\n".join([
    "package com.example.orders;",
    "public class OrderRepository {",
    "    public Order save(Req r) { return null; }",
    "    public Optional<Order> findById(long id) { return null; }",
    "}",
])


class TestSourceFlowsAndReachSet:
    @pytest.fixture
    def index(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        (d / "OrderController.java").write_text(CONTROLLER)
        (d / "OrderService.java").write_text(SERVICE)
        (d / "OrderRepository.java").write_text(REPOSITORY)
        return source_index(str(d))

    def test_builds_the_flow_ids_from_class_and_method_mappings(self):
        flows = source_flows(CONTROLLER, "OrderController")
        assert one(flows, method="get")["id"] == "GET /api/orders/{id}"
        assert one(flows, method="create")["id"] == "POST /api/orders"

    def test_finds_scheduled_jobs_as_flows(self):
        flows = source_flows(CONTROLLER, "OrderController")
        assert one(flows, kind="job")["id"] == "job OrderController.tick"

    def test_follows_injected_fields_down_to_the_repository(self, index):
        reach = reach_set(index, "OrderController", "get")
        assert "OrderController#get" in reach
        assert "OrderService#find" in reach
        assert "OrderRepository#findById" in reach

    def test_does_not_follow_calls_into_library_types(self, index):
        reach = reach_set(index, "OrderController", "get")
        assert not [r for r in reach if "Optional" in r]


# ---------- coverage, flows, findings ----------

class TestFlowCoverageAndFindingAffectsFlow:
    jac = {"com/example/A|run": 12, "com/example/A|*": 40, "com/example/A|dead": 0}
    fqcn = {"A": "com.example.A"}
    flow = {"id": "GET /x", "reach": ["Ctl#get", "Svc#find"]}

    def test_marks_a_method_with_zero_covered_instructions_as_untraced(self):
        rows = flow_coverage(["A#run", "A#dead", "A#missing"], self.jac, self.fqcn)
        assert one(rows, symbol="A#run")["traced"] is True
        assert one(rows, symbol="A#dead")["traced"] is False
        assert one(rows, symbol="A#missing")["traced"] is False

    def test_treats_a_class_level_symbol_as_traced_when_the_class_has_coverage(self):
        assert flow_coverage(["A#*"], self.jac, self.fqcn)[0]["traced"] is True

    def test_global_findings_touch_every_flow(self):
        assert finding_affects_flow({"scope": "global", "symbols": []}, self.flow) is True

    def test_symbol_findings_touch_only_flows_that_reach_the_symbol(self):
        assert finding_affects_flow({"scope": "symbol", "symbols": ["Svc#find"]}, self.flow) is True
        assert finding_affects_flow({"scope": "symbol", "symbols": ["Svc#other"]}, self.flow) is False

    def test_a_class_level_symbol_touches_flows_that_reach_any_method_of_the_class(self):
        assert finding_affects_flow({"scope": "symbol", "symbols": ["Svc#*"]}, self.flow) is True

    def test_test_and_none_scopes_never_touch_production_flows(self):
        assert finding_affects_flow({"scope": "test", "symbols": []}, self.flow) is False
        assert finding_affects_flow({"scope": "none", "symbols": []}, self.flow) is False


class TestFlowScope:
    def test_is_smoke_when_nothing_affects_the_flow(self):
        assert flow_scope([], 0, False) == "smoke"

    def test_is_full_when_code_is_untraced_even_if_every_finding_is_verified(self):
        assert flow_scope(["verified"], 1, False) == "full"

    def test_is_full_when_a_behavior_change_is_confirmed(self):
        assert flow_scope(["confirmed"], 0, True) == "full"

    def test_is_full_while_a_finding_is_still_open(self):
        assert flow_scope(["verified", "open"], 0, False) == "full"

    def test_is_targeted_only_when_every_finding_is_verified_and_nothing_is_untraced(self):
        assert flow_scope(["verified", "verified"], 0, False) == "targeted"


class TestFindingStatus:
    f = {"id": "F-001", "ruleId": "R-X"}

    def test_is_open_when_no_check_is_linked(self):
        assert finding_status(self.f, []) == "open"

    def test_is_verified_when_linked_checks_match_master(self):
        assert finding_status(self.f, [{"ruleId": "R-X", "outcome": "same"}]) == "verified"

    def test_is_confirmed_when_a_linked_check_differs(self):
        c = [{"ruleId": "R-X", "outcome": "same"}, {"ruleId": "R-X", "outcome": "different"}]
        assert finding_status(self.f, c) == "confirmed"

    def test_is_open_not_confirmed_when_a_linked_check_did_not_run(self):
        assert finding_status(self.f, [{"ruleId": "R-X", "outcome": "error"}]) == "open"

    def test_is_open_when_a_linked_test_does_not_hold_on_master(self):
        c = [{"ruleId": "R-X", "outcome": "unverified"}, {"ruleId": "R-X", "outcome": "same"}]
        assert finding_status(self.f, c) == "open"

    def test_is_accepted_when_a_person_accepted_it_even_if_confirmed(self):
        c = [{"ruleId": "R-X", "outcome": "different"}]
        d = [{"findingId": "F-001", "decision": "accept"}]
        assert finding_status(self.f, c, d) == "accepted"


class TestVerdict:
    def test_is_blocked_with_exit_1_when_any_blocking_reason_exists(self):
        assert verdict(["x"], [], True) == {"verdict": "blocked", "exitCode": 1}

    def test_is_safe_with_listed_tests_when_something_is_listed(self):
        assert verdict([], ["gap"], True) == {"verdict": "safe-with-listed-tests", "exitCode": 0}

    def test_is_safe_only_when_nothing_blocks_nothing_is_listed_and_every_required_stage_ran(self):
        assert verdict([], [], True) == {"verdict": "safe", "exitCode": 0}

    def test_never_gives_safe_when_a_required_stage_did_not_run(self):
        assert verdict([], [], False)["verdict"] == "safe-with-listed-tests"


class TestInputHashAndMajorVersion:
    def test_changes_when_an_input_changes_and_is_stable_otherwise(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("one")
        h1 = input_hash([str(a)])
        assert input_hash([str(a)]) == h1
        a.write_text("two")
        assert input_hash([str(a)]) != h1

    def test_a_missing_file_changes_the_hash(self, tmp_path):
        assert input_hash([str(tmp_path / "x")]) != input_hash([str(tmp_path / "y")])

    def test_reads_the_major_version(self):
        assert major_version("4.2.0") == 4
        assert major_version("2.0") == 2
        assert major_version("x") == -1


class TestPowerShellCompatibility:
    def test_sorts_like_sort_object_hyphens_ignored_and_punctuation_first(self):
        assert ps_sorted(["spring-boot-web-server", "spring-boot-webmvc"]) == ["spring-boot-webmvc", "spring-boot-web-server"]
        assert ps_sorted(["GET /api/orders/report.csv", "GET /api/orders/{id}", "GET /api/orders"]) == \
            ["GET /api/orders", "GET /api/orders/{id}", "GET /api/orders/report.csv"]
        assert ps_sorted(["b", "A", "a"]) == ["A", "a", "b"]

    def test_keeps_git_record_separators_inside_a_line(self):
        assert split_lines("\x1eabc\tme\tsubject\npom.xml\r\n") == ["\x1eabc\tme\tsubject", "pom.xml"]


class TestGlobMatch:
    def test_matches_a_path_glob_with_forward_or_back_slashes(self):
        assert glob_match("src\\main\\java\\A.java", "src/main/*") is True
        assert glob_match("pom.xml", "*.java") is False


# ---------- configuration ----------

class TestConfig:
    def test_prefers_config_then_the_environment_then_the_default_file(self, monkeypatch):
        given = os.path.abspath(os.path.join(os.sep, "x", "given.json"))
        from_env = os.path.abspath(os.path.join(os.sep, "x", "env.json"))
        root = os.path.abspath(os.path.join(os.sep, "root"))
        monkeypatch.setenv("UPGRADELENS_CONFIG", from_env)
        assert resolve_config_path(given, root) == given
        assert resolve_config_path("", root) == from_env
        monkeypatch.delenv("UPGRADELENS_CONFIG")
        assert resolve_config_path("", root) == os.path.join(root, "config", "upgrade-lens.json")

    def test_reads_dotted_keys_and_falls_back_to_the_default_for_missing_or_empty_values(self):
        cfg = {"timeouts": {"buildMinutes": 30}, "maven": {"settingsFile": ""}}
        assert config_value(cfg, "timeouts.buildMinutes", 60) == 30
        assert config_value(cfg, "timeouts.startupSeconds", 120) == 120
        assert config_value(cfg, "maven.settingsFile", "none") == "none"
        assert config_value(cfg, "no.such.key", None) is None


# ---------- Java declarations and hunk-to-method mapping ----------

class TestMethodDeclarationName:
    @pytest.mark.parametrize("line,name", [
        ("    Order create(CreateOrderRequest r) {", "create"),
        ("    public Map<String, Long> totalsByCurrency() {", "totalsByCurrency"),
        ("    protected static <T> List<T> copy(List<T> in) {", "copy"),
        ("    @Override public String toString() {", "toString"),
        ("    void createReturnsCreatedOrder() throws Exception {", "createReturnsCreatedOrder"),
        ("    public Order create(", "create"),
    ])
    def test_finds_methods_at_every_access_level_generic_and_annotated(self, line, name):
        assert method_declaration_name(line) == name

    def test_finds_constructors_with_or_without_an_access_modifier(self):
        assert method_declaration_name("    public OrderController(OrderService orders) {") == "OrderController"
        assert method_declaration_name("    OrderService(OrderRepository repository) {") == "OrderService"

    @pytest.mark.parametrize("line", [
        "        foo(bar);",
        "        if (x) {",
        "        return orders.find(id);",
        "        Order first = service.create(req);",
        '        throw new IllegalStateException("x");',
        "        } catch (Exception e) {",
        "        Objects.requireNonNull(x);",
        "        Foo(x);",
        "public record CreateOrderRequest(",
    ])
    def test_rejects_calls_control_statements_returns_and_local_variables(self, line):
        assert method_declaration_name(line) is None


class TestSymbolForLineBraceAware:
    src = [
        "class Svc {",
        "    private int count = 0;",
        "    void tick() {",
        "        if (count > 0) {",
        "            count++;",
        "        }",
        "    }",
        '    private String name = "{x}";',
        "    public Order create(",
        "            String customer,",
        "            long amount) {",
        "        return null;",
        "    }",
        "}",
    ]

    def test_maps_a_body_line_of_a_package_private_method_to_the_method(self):
        assert symbol_for_line(self.src, 5) == "method:tick"

    def test_maps_a_field_between_two_methods_to_the_type_not_to_the_method_above(self):
        assert symbol_for_line(self.src, 8) == "type:Svc"

    def test_maps_a_parameter_line_of_a_multi_line_declaration_to_the_method(self):
        assert symbol_for_line(self.src, 10) == "method:create"

    def test_maps_a_declaration_line_to_itself(self):
        assert symbol_for_line(self.src, 9) == "method:create"


class TestMethodBody:
    def test_returns_the_body_of_a_package_private_method_and_ignores_braces_in_strings(self):
        text = 'class A {\n    void run() {\n        String s = "}";\n        go();\n    }\n    void other() { }\n}'
        body = method_body(text, "run")
        assert "go();" in body
        assert "other" not in body


# ---------- dependencies ----------

VERBOSE_TREE = [
    "com.example:orders-service:jar:0.0.1-SNAPSHOT",
    "+- org.springframework.boot:spring-boot-starter-webmvc:jar:4.2.0:compile",
    "|  +- org.springframework:spring-web:jar:7.1.0:compile (version managed from 7.0.0)",
    "|  \\- (org.slf4j:slf4j-api:jar:2.0.20:compile - version managed from 2.0.19; omitted for duplicate)",
    "\\- com.x:client:jar:1.0:compile",
    "   \\- (com.fasterxml.jackson.core:jackson-databind:jar:2.17.0:compile - omitted for conflict with 3.1.0)",
]


class TestDependencyTreeVerbose:
    def test_records_the_parent_of_each_resolved_artifact_and_skips_omitted_ones(self):
        t = parse_dependency_tree(VERBOSE_TREE)
        assert len(t) == 4
        assert one(t, ga="org.springframework:spring-web")["parent"] == "org.springframework.boot:spring-boot-starter-webmvc"
        assert not [x for x in t if x["ga"] == "org.slf4j:slf4j-api"]

    def test_lists_conflicts_with_who_asked_duplicates_and_managed_versions(self):
        v = parse_dependency_tree_verbose(VERBOSE_TREE)
        assert len(v["conflicts"]) == 1
        c = v["conflicts"][0]
        assert c["ga"] == "com.fasterxml.jackson.core:jackson-databind"
        assert c["requested"] == "2.17.0"
        assert c["resolved"] == "3.1.0"
        assert c["major"] is True
        assert "com.x:client" in c["requiredBy"]
        assert v["duplicates"] == 1
        assert one(v["managed"], ga="org.springframework:spring-web")["declared"] == "7.0.0"


class TestReplacementPairs:
    def test_pairs_a_removed_starter_with_its_renamed_successor_not_with_the_test_starter(self):
        p = replacement_pairs(["org.springframework.boot:spring-boot-starter-webmvc",
                               "org.springframework.boot:spring-boot-starter-webmvc-test"],
                              ["org.springframework.boot:spring-boot-starter-web"])
        assert p["org.springframework.boot:spring-boot-starter-web"] == "org.springframework.boot:spring-boot-starter-webmvc"

    def test_pairs_a_relocated_artifact_same_artifact_id_new_group_id(self):
        p = replacement_pairs(["tools.jackson.core:jackson-databind"], ["com.fasterxml.jackson.core:jackson-databind"])
        assert p["com.fasterxml.jackson.core:jackson-databind"] == "tools.jackson.core:jackson-databind"


class TestDependencyAssessment:
    base = [
        {"ga": "org.springframework.boot:spring-boot-starter-web", "version": "3.2.5", "scope": "compile", "depth": 1, "direct": True, "parent": "app"},
        {"ga": "org.springframework:spring-web", "version": "6.1.6", "scope": "compile", "depth": 2, "direct": False,
         "parent": "org.springframework.boot:spring-boot-starter-web"},
    ]
    head = [
        {"ga": "org.springframework.boot:spring-boot-starter-webmvc", "version": "4.2.0", "scope": "compile", "depth": 1, "direct": True, "parent": "app"},
        {"ga": "org.springframework:spring-web", "version": "7.1.0", "scope": "compile", "depth": 2, "direct": False,
         "parent": "org.springframework.boot:spring-boot-starter-webmvc"},
        {"ga": "org.new:thing", "version": "1.0", "scope": "compile", "depth": 2, "direct": False,
         "parent": "org.springframework.boot:spring-boot-starter-webmvc"},
        {"ga": "com.acme:unused-lib", "version": "2.0", "scope": "compile", "depth": 1, "direct": True, "parent": "app"},
    ]
    analyze = {
        "base": {"unused": ["org.springframework.boot:spring-boot-starter-web:jar:3.2.5:compile"],
                 "undeclared": ["org.springframework:spring-web:jar:6.1.6:compile"]},
        "head": {"unused": ["org.springframework.boot:spring-boot-starter-webmvc:jar:4.2.0:compile", "com.acme:unused-lib:jar:2.0:compile"],
                 "undeclared": ["org.springframework:spring-web:jar:7.1.0:compile"]},
    }

    def rows(self, ignore=()):
        diff = compare_dependency_trees(self.base, self.head)
        return dependency_assessment(diff, self.base, self.head, self.analyze, ignore)

    def test_marks_a_starter_as_required_when_code_uses_an_artifact_it_brings_and_names_what_it_replaces(self):
        r = one(self.rows(), ga="org.springframework.boot:spring-boot-starter-webmvc")
        assert r["assessment"] == "required"
        assert r["replaces"] == "org.springframework.boot:spring-boot-starter-web"
        assert "org.new:thing:1.0" in r["introduces"]

    def test_marks_a_declared_library_that_no_code_uses_as_unnecessary(self):
        assert one(self.rows(), ga="com.acme:unused-lib")["assessment"] == "unnecessary"

    def test_marks_a_removed_starter_that_has_a_successor_as_replaced(self):
        assert one(self.rows(), change="removed")["assessment"] == "replaced"

    def test_honours_the_ignore_list_for_unused_artifacts(self):
        assert one(self.rows(["com.acme:*"]), ga="com.acme:unused-lib")["assessment"] == "required"


class TestDependencySuggestions:
    def test_suggests_a_targeted_tree_per_major_conflict_and_convergence_when_conflicts_exist(self):
        c = [{"ga": "a:b", "requested": "1.0", "resolved": "2.0", "major": True, "requiredBy": ["x:y"]}]
        an = {"base": {"undeclared": []}, "head": {"undeclared": []}}
        sug = dependency_suggestions(c, [], an, True)
        assert len([x for x in sug if "-Dincludes=a:b" in x["command"]]) == 1
        assert len([x for x in sug if "dependencyConvergence" in x["command"]]) == 1


# ---------- rules on the new sources ----------

class TestRuleFindings:
    def test_reports_it_classes_only_when_stage_1_did_not_run_them(self):
        rule = {"id": "R-IT", "title": "t", "severity": "high", "source": "it-not-run", "scope": "global", "impact": "i", "check": "c"}
        assert rule_findings(rule, {"it_mode": "surefire", "it_files": ["src/test/java/AIT.java"]}) == []
        assert len(rule_findings(rule, {"it_mode": "not-run", "it_files": ["src/test/java/AIT.java"]})) == 1

    def test_turns_a_high_library_item_into_a_symbol_finding_on_the_code_that_used_it(self):
        rule = {"id": "R-LIB", "title": "t", "severity": "high", "source": "library-api", "match": "removed-used",
                "scope": "symbol", "impact": "i", "check": "c"}
        item = {"id": "LIB-001", "class": "com.fasterxml.jackson.databind.ObjectMapper", "severity": "high", "summary": "moved",
                "artifact": {"base": "a", "head": "b"},
                "usage": {"base": [{"appClass": "com.example.orders.OrderControllerIT", "method": "create",
                                    "source": "src/test/java/com/example/orders/OrderControllerIT.java", "line": 40}], "head": []}}
        f = rule_findings(rule, {"library_api": {"items": [item]}})
        assert len(f) == 1
        assert f[0]["symbols"][0] == "OrderControllerIT#create"
        assert f[0]["line"] == 40

    def test_diff_rules_match_case_insensitively_and_honour_the_file_glob(self):
        rule = {"id": "R-J", "title": "t", "severity": "high", "source": "diff-added", "fileGlob": "*.java",
                "pattern": "com\\.fasterxml", "scope": "global", "impact": "i", "check": "c"}
        patch = {"src/A.java": {"added": [{"line": 1, "text": "import COM.FASTERXML.X;"}], "removed": []},
                 "pom.xml": {"added": [{"line": 1, "text": "com.fasterxml"}], "removed": []}}
        f = rule_findings(rule, {"patch": patch, "head_lines": {"src/A.java": ["import COM.FASTERXML.X;"]}})
        assert [x["file"] for x in f] == ["src/A.java"]


# ---------- library API ----------

class TestParseJdepsVerbose:
    def test_reads_class_level_dependencies_with_their_location(self):
        rows = parse_jdeps_verbose([
            "classes -> java.base",
            "   com.example.orders.OrderController                 -> org.springframework.web.bind.annotation.GetMapping not found",
            "   com.example.orders.OrderController                 -> java.lang.Object                                    java.base",
        ])
        assert len(rows) == 2
        assert rows[0]["to"] == "org.springframework.web.bind.annotation.GetMapping"
        assert rows[0]["location"] == "not found"


class TestJavapApi:
    base_out = [
        'Compiled from "Mapper.java"',
        "public class a.b.Mapper {",
        "  public a.b.Mapper();",
        "    descriptor: ()V",
        "  public java.lang.String write(java.lang.Object);",
        "    descriptor: (Ljava/lang/Object;)Ljava/lang/String;",
        "  public void gone();",
        "    descriptor: ()V",
        "}",
    ]
    head_out = [
        'Compiled from "Mapper.java"',
        "public final class a.b.Mapper {",
        "  public a.b.Mapper();",
        "    descriptor: ()V",
        "  public java.lang.String write(java.lang.Object, int);",
        "    descriptor: (Ljava/lang/Object;I)Ljava/lang/String;",
        "}",
    ]

    def test_parses_members_with_descriptors_and_names_constructors_init(self):
        b = parse_javap_api(self.base_out)["a.b.Mapper"]
        assert len(b["members"]) == 3
        assert one(b["members"], name="<init>")["descriptor"] == "()V"

    def test_reports_a_changed_overload_a_removed_member_and_a_changed_class_declaration(self):
        b = parse_javap_api(self.base_out)["a.b.Mapper"]
        h = parse_javap_api(self.head_out)["a.b.Mapper"]
        c = compare_library_class_api(b, h)
        assert c["state"] == "changed"
        assert c["headerChanged"] is True
        assert len(c["changed"]) == 1
        assert c["changed"][0]["name"] == "write"
        assert "gone" in [m["name"] for m in c["removed"]]


class TestJavapCode:
    out = [
        'Compiled from "OrderService.java"',
        "public class com.example.orders.OrderService {",
        "  public com.example.orders.OrderService(com.example.orders.OrderRepository);",
        "    Code:",
        "         0: aload_0",
        '         1: invokespecial #1                  // Method java/lang/Object."<init>":()V',
        "         4: return",
        "      LineNumberTable:",
        "        line 12: 0",
        "  public java.lang.String json(java.lang.Object);",
        "    Code:",
        "         0: aload_0",
        "         1: getfield      #7                  // Field mapper:Lcom/fasterxml/jackson/databind/ObjectMapper;",
        "         4: aload_1",
        "         5: invokevirtual #9                  // Method com/fasterxml/jackson/databind/ObjectMapper.writeValueAsString:(Ljava/lang/Object;)Ljava/lang/String;",
        "         8: areturn",
        "      LineNumberTable:",
        "        line 20: 0",
        "        line 21: 4",
        "  private static void lambda$list$0(java.lang.Object);",
        "    Code:",
        "         0: ldc           #11                 // class com/fasterxml/jackson/databind/JsonNode",
        "         2: return",
        "      LineNumberTable:",
        "        line 30: 0",
        "}",
    ]

    def test_maps_each_library_call_to_its_source_method_and_line(self):
        call = one(parse_javap_code(self.out), member="writeValueAsString")
        assert call["owner"] == "com.fasterxml.jackson.databind.ObjectMapper"
        assert call["method"] == "json"
        assert call["line"] == 21
        assert call["descriptor"] == "(Ljava/lang/Object;)Ljava/lang/String;"

    def test_uses_the_current_class_for_fields_the_class_name_for_constructors_and_the_enclosing_method_for_lambdas(self):
        rows = parse_javap_code(self.out)
        assert one(rows, member="mapper")["owner"] == "com.example.orders.OrderService"
        assert one(rows, member="<init>")["method"] == "OrderService"
        assert one(rows, kind="class")["method"] == "list"


class TestJarCoordinates:
    def test_reads_maven_coordinates_from_a_local_repository_path(self):
        j = jar_coordinates("E:\\m2\\repository\\org\\springframework\\spring-web\\7.1.0\\spring-web-7.1.0.jar")
        assert j["ga"] == "org.springframework:spring-web"
        assert j["version"] == "7.1.0"


class TestLibraryApiItems:
    def test_reports_a_class_master_used_that_moved_package_as_one_high_item(self):
        deps = {"base": [{"from": "app.Svc", "to": "old.pkg.Mapper", "location": "not found"}],
                "head": [{"from": "app.Svc", "to": "new.pkg.Mapper", "location": "not found"}]}
        code = {"base": [{"appClass": "app.Svc", "method": "json", "line": 21, "kind": "method", "owner": "old.pkg.Mapper",
                          "member": "write", "descriptor": "()V"}],
                "head": [{"appClass": "app.Svc", "method": "json", "line": 22, "kind": "method", "owner": "new.pkg.Mapper",
                          "member": "write", "descriptor": "()V"}]}

        def api_of(n):
            return {"fqcn": n, "header": f"public class {n}",
                    "members": [{"key": "write()V", "name": "write", "descriptor": "()V", "declaration": "public void write()"}]}

        api = {"base": {"old.pkg.Mapper": api_of("old.pkg.Mapper")}, "head": {"new.pkg.Mapper": api_of("new.pkg.Mapper")}}
        jars = {"base": {"old.pkg.Mapper": "C:\\r\\repository\\old\\pkg\\mapper\\2.0\\mapper-2.0.jar"},
                "head": {"new.pkg.Mapper": "C:\\r\\repository\\new\\pkg\\mapper\\3.0\\mapper-3.0.jar"}}
        sources = {"base": {"app.Svc": {"path": "src/main/java/app/Svc.java", "lines": ["import old.pkg.Mapper;"]}}, "head": {}}
        r = library_api_items(deps, code, api, jars, sources, {"old.pkg.Mapper": ["new.pkg.Mapper"]})
        assert len(r["items"]) == 1
        it = r["items"][0]
        assert it["id"] == "LIB-001"
        assert it["state"] == "moved"
        assert it["severity"] == "high"
        assert it["movedTo"][0] == "new.pkg.Mapper"
        assert it["artifact"]["head"] == "new.pkg:mapper:3.0"
        assert it["usage"]["base"][0]["source"] == "src/main/java/app/Svc.java"
        assert r["counts"]["added"] == 0


# ---------- test results and patches ----------

class TestSurefireAndCompareTestResults:
    def test_reads_each_test_case_status(self, tmp_path):
        p = tmp_path / "TEST-a.T.xml"
        p.write_text('<?xml version="1.0"?><testsuite name="a.T" tests="3"><testcase classname="a.T" name="ok"/>'
                     '<testcase classname="a.T" name="bad"><failure message="boom"/></testcase>'
                     '<testcase classname="a.T" name="off"><skipped/></testcase></testsuite>')
        rows = parse_surefire_reports([str(p)])
        assert one(rows, id="a.T#bad")["status"] == "failed"
        assert one(rows, id="a.T#bad")["message"] == "boom"
        assert one(rows, id="a.T#off")["status"] == "skipped"

    def test_classifies_differences_between_the_two_builds(self):
        b = [{"id": "t1", "status": "passed"}, {"id": "t2", "status": "passed"}, {"id": "t3", "status": "passed"}]
        h = [{"id": "t1", "status": "passed"}, {"id": "t2", "status": "failed", "message": "x"}, {"id": "t4", "status": "passed"}]
        c = compare_test_results(b, h)
        assert one(c, test="t1")["change"] == "same"
        assert one(c, test="t2")["change"] == "now-failing"
        assert one(c, test="t3")["change"] == "removed"
        assert one(c, test="t4")["change"] == "new"


class TestNewAddedFilePatch:
    def test_writes_a_git_patch_that_adds_one_file(self):
        p = new_added_file_patch("src\\test\\java\\A.java", ["class A {", "}"])
        assert "diff --git a/src/test/java/A.java b/src/test/java/A.java" in p
        assert "@@ -0,0 +1,2 @@" in p
        assert "+class A {" in p

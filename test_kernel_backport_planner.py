#!/usr/bin/env python3
import importlib.machinery
import json
import types
import unittest
from unittest import mock
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("kbp", str(HERE / "kernel-backport-planner"))
kbp = types.ModuleType(loader.name)
kbp.__file__ = str(HERE / "kernel-backport-planner")
sys.modules[loader.name] = kbp
loader.exec_module(kbp)


class DependencyEvidenceTests(unittest.TestCase):
    def planner(self, context_score=5):
        planner = object.__new__(kbp.Planner)
        planner.args = types.SimpleNamespace(context_score=context_score)
        return planner

    def test_context_only_evidence_is_always_tentative(self):
        planner = self.planner(context_score=5)
        self.assertTrue(planner.is_tentative_dependency_reason("conflict-hunk-context:1"))
        self.assertTrue(planner.is_tentative_dependency_reason("conflict-hunk-context:82"))
        self.assertTrue(planner.is_tentative_dependency_reason("hunk-context:82"))
        self.assertFalse(planner.is_tentative_dependency_reason("conflict-hunk-preimage:1"))
        self.assertFalse(planner.is_tentative_dependency_reason("symbol:foo"))

    def test_tentative_relation_is_promoted_by_strong_evidence(self):
        node = kbp.CommitNode("a" * 40, "candidate")
        parent = "b" * 40
        node.add_reason("conflict-hunk-context:1", parent, tentative=True)
        self.assertEqual(node.triggered_by, {parent})
        self.assertEqual(node.required_by, set())
        self.assertEqual(kbp.Planner.dependency_kind(node), "dependency-candidate")

        node.add_reason("conflict-hunk-preimage:4", parent, tentative=False)
        self.assertEqual(node.triggered_by, set())
        self.assertEqual(node.required_by, {parent})
        self.assertEqual(kbp.Planner.dependency_kind(node), "dependency")


class CandidateSelectionTests(unittest.TestCase):
    def test_candidate_is_not_actionable_or_ordered(self):
        planner = object.__new__(kbp.Planner)
        strong = kbp.CommitNode("a" * 40, "strong")
        strong.add_reason("symbol:VM_NONE", "c" * 40)
        candidate = kbp.CommitNode("b" * 40, "candidate")
        candidate.add_reason("hunk-context:82", "d" * 40, tentative=True)
        self.assertTrue(kbp.Planner.is_actionable_node(strong))
        self.assertFalse(kbp.Planner.is_actionable_node(candidate))

    def test_candidate_can_be_promoted_by_strong_evidence(self):
        node = kbp.CommitNode("a" * 40, "candidate")
        node.add_reason("conflict-hunk-context:82", "b" * 40, tentative=True)
        self.assertFalse(kbp.Planner.is_actionable_node(node))
        node.add_reason("conflict-hunk-preimage:4", "b" * 40, tentative=False)
        self.assertTrue(kbp.Planner.is_actionable_node(node))

    def test_dependency_closure_does_not_expand_candidate_only_nodes(self):
        planner = object.__new__(kbp.Planner)
        planner.args = types.SimpleNamespace(max_dependency_rounds=2)
        direct = kbp.CommitNode("a" * 40, "direct", direct=True)
        candidate = kbp.CommitNode("b" * 40, "candidate")
        candidate.add_reason("hunk-context:82", direct.sha, tentative=True)
        planner.nodes = {direct.sha: direct, candidate.sha: candidate}
        planner._dependency_expanded = set()
        planner.info = lambda _msg: None
        expanded = []
        planner.add_explicit_fixes_closure = lambda frontier: expanded.extend(n.sha for n in frontier) or 0
        planner.add_static_dependencies = lambda frontier: 0

        kbp.Planner.dependency_closure(planner)
        self.assertEqual(expanded, [direct.sha])
        self.assertIn(direct.sha, planner._dependency_expanded)
        self.assertNotIn(candidate.sha, planner._dependency_expanded)


class FinalReplayVerificationTests(unittest.TestCase):
    def test_candidate_only_discovery_does_not_trigger_another_replay(self):
        planner = object.__new__(kbp.Planner)
        planner.args = types.SimpleNamespace(simulate=True, max_replay_rounds=3)
        planner.nodes = {"a" * 40: kbp.CommitNode("a" * 40, "requested", direct=True)}
        planner.replay_rounds_run = 0
        planner.replay_final_verification = False
        planner.info = lambda _msg: None
        planner.dependency_closure = lambda: self.fail("candidate-only discovery must not recurse")

        calls = []
        def replay_once():
            calls.append("replay")
            return False, [("a" * 40, ["file.c"])]

        planner.replay_once = replay_once
        planner.add_conflict_dependencies = lambda _conflicts: (0, 1)

        kbp.Planner.replay_with_discovery(planner)
        self.assertEqual(calls, ["replay"])
        self.assertFalse(planner.replay_final_verification)

    def test_last_round_discovery_gets_verification_only_replay(self):
        planner = object.__new__(kbp.Planner)
        planner.args = types.SimpleNamespace(simulate=True, max_replay_rounds=1)
        planner.nodes = {"a" * 40: kbp.CommitNode("a" * 40, "requested")}
        planner.replay_rounds_run = 0
        planner.replay_final_verification = False
        planner.info = lambda _msg: None
        planner.dependency_closure = lambda: None

        calls = []

        def replay_once():
            calls.append("replay")
            if len(calls) == 1:
                return False, [("a" * 40, ["file.c"])]
            return True, []

        def add_conflict_dependencies(_conflicts):
            dep = kbp.CommitNode("b" * 40, "new dependency")
            dep.add_reason("conflict-hunk-preimage:4", "a" * 40)
            planner.nodes[dep.sha] = dep
            return 1, 0

        planner.replay_once = replay_once
        planner.add_conflict_dependencies = add_conflict_dependencies

        kbp.Planner.replay_with_discovery(planner)

        self.assertEqual(len(calls), 2)
        self.assertEqual(planner.replay_rounds_run, 1)
        self.assertTrue(planner.replay_final_verification)


class ReportSemanticsTests(unittest.TestCase):
    def test_report_distinguishes_strong_and_tentative_dependencies(self):
        planner = object.__new__(kbp.Planner)
        planner.base = "1" * 40
        planner.target = "2" * 40
        planner.downstream = "3" * 40
        planner.replay_start = "4" * 40
        planner.replay_start_source = "cs10/main"
        planner.replay_rounds_run = 1
        planner.replay_final_verification = False
        planner.paths = ["arch/arm64/"]
        planner.args = types.SimpleNamespace(base="v6.19", show_dependency_candidates=False)
        planner.series_groups = {}
        planner.git = types.SimpleNamespace(is_ancestor=lambda _a, _b: False)

        strong = kbp.CommitNode("a" * 40, "strong")
        strong.add_reason("conflict-hunk-preimage:4", "c" * 40)
        tentative = kbp.CommitNode("b" * 40, "tentative")
        tentative.add_reason("conflict-hunk-context:1", "d" * 40, tentative=True)
        planner.nodes = {strong.sha: strong, tentative.sha: tentative}
        planner.ordered_missing = lambda: [strong.sha, tentative.sha]

        report = kbp.Planner.report_text(planner)
        self.assertIn("replay-start: 444444444444 (cs10/main)", report)
        self.assertIn("aaaaaaaaaaaa [DEPENDENCY]", report)
        self.assertIn("required-by: cccccccccccc", report)
        self.assertNotIn("bbbbbbbbbbbb", report)

        planner.args.show_dependency_candidates = True
        report = kbp.Planner.report_text(planner)
        self.assertIn("DEPENDENCY CANDIDATES (informational; not selected or replayed)", report)
        self.assertIn("bbbbbbbbbbbb [DEPENDENCY-CANDIDATE]", report)
        self.assertIn("triggered-by: dddddddddddd", report)
        self.assertIn("evidence: conflict-hunk-context:1", report)


class AdvisorTests(unittest.TestCase):
    def minimal_planner(self):
        planner = object.__new__(kbp.Planner)
        planner.base = "1" * 40
        planner.target = "2" * 40
        planner.downstream = "3" * 40
        planner.downstream_ref = "cs10/main"
        planner.paths = ["arch/arm64/", "include/asm-generic/"]
        planner.advisor_model = "gemma4-kernel:128k"
        planner.advisor_calls = 1
        planner.advisor_cache_hits = 0
        planner.series_groups = {}
        planner.args = types.SimpleNamespace(
            base="v6.19", show_dependency_candidates=False,
            downstream="cs10", advisor_product="auto",
            advisor_base_url="http://127.0.0.1:11434",
            advisor_max_message_chars=6000, advisor_max_diff_chars=12000,
            advisor_max_peer_diff_chars=6000, advisor_max_peer_diffs=6,
            advisor_required_by_depth=3, advisor_max_related_commits=8,
            advisor_max_related_diff_chars=5000, advisor_retries=2,
            advisor_retry_delay=0.0,
        )
        planner.repo = Path("/tmp/centos-stream-10")
        planner.git = types.SimpleNamespace(is_ancestor=lambda _a, _b: False)
        planner.replay_start = "4" * 40
        planner.replay_start_source = "cs10/main"
        planner.replay_rounds_run = 1
        planner.replay_final_verification = False
        return planner

    def test_report_adds_advisor_annotation_without_replacing_evidence(self):
        planner = self.minimal_planner()
        node = kbp.CommitNode("a" * 40, "arm64: useful change", direct=True)
        node.advisor_decision = "TAKE"
        node.advisor_rationale = "Touches the ARM64 focus area and no exclusion is evident."
        planner.nodes = {node.sha: node}
        planner.ordered_missing = lambda: [node.sha]

        report = kbp.Planner.report_text(planner)
        self.assertIn('// Take', report)
        self.assertIn('advisor: TAKE', report)
        self.assertIn('advisor-rationale:', report)

    def test_advisor_prerequisite_must_be_bound_to_deterministic_evidence(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("a" * 40, "selected", direct=True)
        planner.nodes = {selected.sha: selected}
        peer = "b" * 40
        evidence_id = "ev-test-evidence"
        planner.advisor_prerequisite_evidence = lambda _sha: [{
            "evidence_id": evidence_id,
            "peer_sha": peer,
            "peer_subject": "provider",
            "kind": "dts-label",
            "object": "uart18",
            "selected_use": "&uart18",
            "peer_provides": "uart18:",
        }]
        planner.advisor_inbound_take_recommendations = lambda _sha: []

        result = planner.validate_advisor_result(selected.sha, {
            "decision": "TAKE",
            "rationale": "Needs prior peer.",
            "prerequisite_evidence_ids": [evidence_id],
            "inbound_prerequisite_assessments": [],
            "risks": [],
        })
        self.assertEqual(result["prerequisite_peers"][0]["sha"], peer)
        self.assertEqual(result["prerequisite_peers"][0]["evidence_id"], evidence_id)

        with self.assertRaises(kbp.AdvisorError):
            planner.validate_advisor_result(selected.sha, {
                "decision": "TAKE",
                "rationale": "Invented prerequisite.",
                "prerequisite_evidence_ids": ["ev-invented"],
                "inbound_prerequisite_assessments": [],
                "risks": [],
            })

    def test_ollama_client_uses_structured_schema_and_parses_response(self):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({
                    "message": {
                        "content": json.dumps({
                            "decision": "TAKE",
                            "rationale": "Relevant ARM64 change.",
                            "prerequisite_evidence_ids": [],
                            "inbound_prerequisite_assessments": [],
                            "risks": [],
                        })
                    }
                }).encode()

        client = kbp.OllamaAdvisor(
            "gemma4-kernel:128k", "127.0.0.1:11434", 5, 0.1, "10m"
        )
        captured = {}
        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(kbp.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = client.advise("system", {"commit": {"sha": "a" * 40}})

        self.assertEqual(result["decision"], "TAKE")
        self.assertEqual(captured["payload"]["format"], kbp.ADVISOR_RESPONSE_SCHEMA)
        self.assertFalse(captured["payload"]["stream"])
        self.assertEqual(captured["timeout"], 5)

    def test_ollama_client_tolerates_yaml_separator_and_json_fence(self):
        base_result = {
            "decision": "TAKE",
            "rationale": "Relevant ARM64 change.",
            "prerequisite_evidence_ids": [],
            "inbound_prerequisite_assessments": [],
            "risks": [],
        }

        class FakeResponse:
            def __init__(self, content):
                self.content = content
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"message": {"content": self.content}}).encode()

        client = kbp.OllamaAdvisor(
            "gemma4-kernel:128k", "127.0.0.1:11434", 5, 0.1, "10m"
        )
        payload = json.dumps(base_result, indent=2)
        variants = [
            "---\n" + payload,
            "```json\n" + payload + "\n```",
            "---\n```json\n" + payload + "\n```",
        ]
        for content in variants:
            with self.subTest(content=content[:16]):
                with mock.patch.object(
                    kbp.urllib.request, "urlopen",
                    return_value=FakeResponse(content),
                ):
                    result = client.advise("system", {"commit": {"sha": "a" * 40}})
                self.assertEqual(result, base_result)

    def test_ollama_client_does_not_extract_json_from_arbitrary_prose(self):
        result = {
            "decision": "TAKE",
            "rationale": "Relevant ARM64 change.",
            "prerequisite_evidence_ids": [],
            "inbound_prerequisite_assessments": [],
            "risks": [],
        }

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                content = "Here is the JSON you requested:\n" + json.dumps(result)
                return json.dumps({"message": {"content": content}}).encode()

        client = kbp.OllamaAdvisor(
            "gemma4-kernel:128k", "127.0.0.1:11434", 5, 0.1, "10m"
        )
        with mock.patch.object(kbp.urllib.request, "urlopen", return_value=FakeResponse()):
            with self.assertRaises(kbp.AdvisorError) as ctx:
                client.advise("system", {"commit": {"sha": "a" * 40}})
        self.assertEqual(ctx.exception.kind, "invalid-response")
        self.assertTrue(ctx.exception.retryable)

    def test_advisor_packet_includes_missing_earlier_series_peer_patch(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("a" * 40, "Add board", direct=True)
        selected.files = ["arch/arm64/boot/dts/qcom/board.dts"]
        selected.series_ids.add("S001")
        peer = "b" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(5, peer), (6, selected.sha)]}
        planner.match = lambda sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "Add uart18" if sha == peer else "Add board"
        planner.changed_files = lambda sha: ["arch/arm64/boot/dts/qcom/sc8280xp.dtsi"] if sha == peer else selected.files
        planner.patch_excerpt = lambda sha, limit: "+	uart18: serial@..." if sha == peer else "+	&uart18 { status = \"okay\"; };"
        planner.body = lambda sha: planner.subject(sha)

        packet = planner.advisor_packet(selected.sha)
        members = packet["commit"]["same_series"][0]["members"]
        earlier = next(x for x in members if x["sha"] == peer)
        self.assertEqual(earlier["relation"], "earlier")
        self.assertIn("uart18", earlier["patch_excerpt"])
        self.assertIn("&uart18", packet["commit"]["patch_excerpt"])
        consumed = packet["commit"]["same_series_consumed_objects"]
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0]["peer_sha"], peer)
        self.assertEqual(consumed[0]["kind"], "dts-label")
        self.assertEqual(consumed[0]["object"], "uart18")
        self.assertEqual(consumed[0]["selected_use"], "&uart18")
        self.assertEqual(consumed[0]["peer_provides"], "uart18:")
        self.assertEqual(selected.advisor_consumed_objects, consumed)
        self.assertEqual(packet["commit"]["default_policy_bias"], "TAKE_UNLESS_CONCRETE_EXCLUSION")

    def test_same_series_consumed_object_finds_macro_provider(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("a" * 40, "arm64: consume macro", direct=True)
        selected.files = ["arch/arm64/boot/dts/qcom/board.dts"]
        selected.series_ids.add("S001")
        peer = "b" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "dt-bindings: add FOO_CLK" if sha == peer else selected.subject
        planner.changed_files = lambda sha: ["include/dt-bindings/clock/foo.h"] if sha == peer else selected.files
        planner.patch_excerpt = lambda sha, limit: "+#define FOO_CLK 7" if sha == peer else "+ clocks = <&gcc FOO_CLK>;"
        planner.body = lambda sha: planner.subject(sha)

        evidence = planner.advisor_same_series_consumed_objects(selected.sha)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["kind"], "macro")
        self.assertEqual(evidence[0]["object"], "FOO_CLK")
        self.assertEqual(evidence[0]["peer_sha"], peer)

    def test_direct_focus_commit_has_take_unless_excluded_bias(self):
        planner = self.minimal_planner()
        node = kbp.CommitNode("a" * 40, "arm64: dts: add obscure board feature", direct=True)
        node.files = ["arch/arm64/boot/dts/vendor/obscure-board.dts"]
        planner.nodes = {node.sha: node}
        planner.subject = lambda sha: node.subject
        planner.changed_files = lambda sha: node.files
        planner.patch_excerpt = lambda sha, limit: "+ status = \"okay\";"
        planner.body = lambda sha: node.subject

        packet = planner.advisor_packet(node.sha)
        self.assertEqual(packet["commit"]["default_policy_bias"], "TAKE_UNLESS_CONCRETE_EXCLUSION")
        self.assertIn("not an exclusion reason", packet["planning_context"]["policy"])

    def test_direct_focus_bias_is_subsystem_generic(self):
        cases = [
            (["arch/arm64/"], "arch/arm64/kernel/foo.c"),
            (["arch/x86/"], "arch/x86/kernel/foo.c"),
            (["mm/"], "mm/page_alloc.c"),
            (["drivers/net/"], "drivers/net/ethernet/vendor/foo.c"),
        ]
        for paths, changed_file in cases:
            with self.subTest(paths=paths):
                planner = self.minimal_planner()
                planner.paths = paths
                node = kbp.CommitNode("a" * 40, "direct focus change", direct=True)
                node.files = [changed_file]
                planner.nodes = {node.sha: node}
                planner.subject = lambda _sha: node.subject
                planner.changed_files = lambda _sha: node.files
                planner.patch_excerpt = lambda _sha, _limit: "+int changed;"
                planner.body = lambda _sha: node.subject

                packet = planner.advisor_packet(node.sha)
                self.assertEqual(
                    packet["commit"]["default_policy_bias"],
                    "TAKE_UNLESS_CONCRETE_EXCLUSION",
                )
                if changed_file.startswith("mm/") or changed_file.startswith("drivers/"):
                    self.assertEqual(packet["commit"]["scope"]["architecture_scope"], "common")
                    self.assertIsNone(packet["commit"]["scope"]["touches_supported_architecture"])

    def test_generic_product_does_not_apply_rhel_architecture_allowlist(self):
        planner = self.minimal_planner()
        planner.args.advisor_product = "generic"
        planner.paths = ["arch/loongarch/"]
        node = kbp.CommitNode("a" * 40, "loongarch: direct focus change", direct=True)
        node.files = ["arch/loongarch/kernel/foo.c"]
        planner.nodes = {node.sha: node}
        planner.subject = lambda _sha: node.subject
        planner.changed_files = lambda _sha: node.files
        planner.patch_excerpt = lambda _sha, _limit: "+int changed;"
        planner.body = lambda _sha: node.subject

        packet = planner.advisor_packet(node.sha)
        self.assertIsNone(
            packet["planning_context"]["product"]["supported_architectures"]
        )
        self.assertFalse(packet["commit"]["scope"]["unsupported_architecture_only"])
        self.assertEqual(packet["commit"]["scope"]["architecture_scope"], "unrestricted")
        self.assertEqual(
            packet["commit"]["default_policy_bias"],
            "TAKE_UNLESS_CONCRETE_EXCLUSION",
        )


    def test_rhel10_product_context_and_unsupported_arch_scope(self):
        planner = self.minimal_planner()
        product = planner.advisor_product_context()
        self.assertEqual(product["product"], "Red Hat Enterprise Linux 10 / CentOS Stream 10")
        self.assertEqual(
            set(product["supported_kernel_arch_dirs"]),
            {"x86", "arm64", "powerpc", "s390"},
        )
        scope = planner.advisor_scope_context(["arch/loongarch/Kconfig"])
        self.assertEqual(scope["architecture_dirs"], ["loongarch"])
        self.assertTrue(scope["unsupported_architecture_only"])
        arm_scope = planner.advisor_scope_context(["arch/arm64/kernel/ptrace.c"])
        self.assertFalse(arm_scope["unsupported_architecture_only"])
        self.assertTrue(arm_scope["touches_focus_paths"])

    def test_unsupported_arch_scope_uses_architecture_owned_library_paths(self):
        planner = self.minimal_planner()
        arm32 = planner.advisor_scope_context(["lib/crypto/arm/nh.h"])
        self.assertEqual(arm32["architecture_dirs"], ["arm"])
        self.assertTrue(arm32["unsupported_architecture_only"])
        self.assertEqual(arm32["architecture_scope"], "unsupported-only")

        arm64 = planner.advisor_scope_context(["lib/crypto/arm64/nh.h"])
        self.assertEqual(arm64["architecture_dirs"], ["arm64"])
        self.assertFalse(arm64["unsupported_architecture_only"])
        self.assertEqual(arm64["architecture_scope"], "supported-or-mixed")

        mixed = planner.advisor_scope_context(
            ["lib/crypto/arm/nh.h", "include/crypto/nh.h"]
        )
        self.assertFalse(mixed["unsupported_architecture_only"])
        self.assertEqual(mixed["architecture_scope"], "common-plus-unsupported")

    def test_dynamic_schema_constrains_prerequisite_shas_and_evidence_ids(self):
        a = "a" * 40
        b = "b" * 40
        evidence = [
            {"peer_sha": a, "evidence_id": "ev-a"},
            {"peer_sha": b, "evidence_id": "ev-b"},
        ]
        inbound = [{"evidence_id": "ev-inbound"}]
        schema = kbp.advisor_response_schema(evidence, inbound)
        prereqs = schema["properties"]["prerequisite_evidence_ids"]
        self.assertEqual(prereqs["maxItems"], 2)
        self.assertEqual(prereqs["items"]["enum"], ["ev-a", "ev-b"])
        self.assertTrue(prereqs["uniqueItems"])
        inbound_schema = schema["properties"]["inbound_prerequisite_assessments"]
        self.assertEqual(inbound_schema["minItems"], 1)
        self.assertEqual(inbound_schema["maxItems"], 1)
        self.assertEqual(
            inbound_schema["items"]["properties"]["evidence_id"]["enum"],
            ["ev-inbound"],
        )
        empty = kbp.advisor_response_schema([])
        self.assertEqual(empty["properties"]["prerequisite_evidence_ids"]["maxItems"], 0)
        self.assertEqual(
            empty["properties"]["inbound_prerequisite_assessments"]["maxItems"], 0
        )

    def test_extended_prerequisite_evidence_finds_declaration_include_binding_and_type(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("a" * 40, "consumer", direct=True)
        selected.series_ids.add("S001")
        provider = "b" * 40
        binding = "c" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, provider), (2, binding), (3, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: {
            provider: "provide common declarations",
            binding: "dt-bindings: add board properties",
            selected.sha: selected.subject,
        }[sha]
        planner.changed_files = lambda sha: {
            provider: ["include/linux/mm.h", "include/linux/new_api.h"],
            binding: ["Documentation/devicetree/bindings/vendor/board.yaml"],
            selected.sha: ["arch/arm64/boot/dts/vendor/board.dts", "mm/init.c"],
        }[sha]
        planner.patch_excerpt = lambda sha, _limit: {
            provider: (
                "diff --git a/include/linux/new_api.h b/include/linux/new_api.h\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/include/linux/new_api.h\n"
                "+/* new header contents */\n"
                "diff --git a/include/linux/mm.h b/include/linux/mm.h\n"
                "+void __init arch_zone_limits_init(void);\n"
                "+struct widget { int value; };\n"
            ),
            binding: (
                "+  compatible:\n"
                "+    const: vendor,board\n"
                "+  power-domains:\n"
                "+    maxItems: 1\n"
            ),
            selected.sha: (
                "diff --git a/mm/init.c b/mm/init.c\n"
                "+#include <linux/new_api.h>\n"
                "+void __init arch_zone_limits_init(void)\n"
                "+{\n"
                "+    struct widget *w;\n"
                "+}\n"
                "diff --git a/arch/arm64/boot/dts/vendor/board.dts "
                "b/arch/arm64/boot/dts/vendor/board.dts\n"
                "+compatible = \"vendor,board\";\n"
                "+power-domains = <&gcc 1>;\n"
            ),
        }[sha]

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        kinds = {(x["kind"], x["object"]) for x in evidence}
        self.assertIn(("function-declaration", "arch_zone_limits_init"), kinds)
        self.assertIn(("included-file", "linux/new_api.h"), kinds)
        self.assertIn(("compatible-binding", "vendor,board"), kinds)
        self.assertIn(("dt-property-binding", "vendor,board:power-domains"), kinds)
        self.assertIn(("type-definition", "struct widget"), kinds)
        self.assertTrue(all(x["evidence_id"].startswith("ev-") for x in evidence))

    def test_pure_cross_arch_same_named_function_is_not_prerequisite_evidence(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64 consumer", direct=True)
        selected.files = ["arch/arm64/kernel/foo.c"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "alpha provider" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["arch/alpha/kernel/foo.c"] if sha == peer else selected.files
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "+void shared_name(void)\n+{\n+}\n" if sha == peer
            else "+shared_name();\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(x["object"] == "shared_name" for x in evidence))

    def test_same_named_arch_callback_uses_shared_declaration_not_peer_implementation(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "powerpc callback")
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "alpha callback" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["arch/alpha/mm/init.c", "include/linux/mm.h"]
            if sha == peer else ["arch/powerpc/mm/mem.c"]
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/arch/alpha/mm/init.c b/arch/alpha/mm/init.c\n"
            "+void __init arch_zone_limits_init(unsigned long *p)\n"
            "+{\n"
            "+}\n"
            "diff --git a/include/linux/mm.h b/include/linux/mm.h\n"
            "+void arch_zone_limits_init(unsigned long *p);\n"
            if sha == peer else
            "diff --git a/arch/powerpc/mm/mem.c b/arch/powerpc/mm/mem.c\n"
            "+void __init arch_zone_limits_init(unsigned long *p)\n"
            "+{\n"
            "+}\n"
            "+arch_zone_limits_init(p);\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        matching = [x for x in evidence if x["object"] == "arch_zone_limits_init"]
        self.assertEqual([x["kind"] for x in matching], ["function-declaration"])

    def test_common_call_prefers_peer_declaration_over_arch_implementation(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "common mm consumer")
        selected.files = ["mm/page_alloc.c"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "alpha plus shared declaration" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["arch/alpha/mm/init.c", "include/linux/mm.h"] if sha == peer else selected.files
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/arch/alpha/mm/init.c b/arch/alpha/mm/init.c\n"
            "+void __init arch_zone_limits_init(unsigned long *p)\n"
            "+{\n"
            "+}\n"
            "diff --git a/include/linux/mm.h b/include/linux/mm.h\n"
            "+void arch_zone_limits_init(unsigned long *p);\n"
            if sha == peer else
            "diff --git a/mm/page_alloc.c b/mm/page_alloc.c\n"
            "+arch_zone_limits_init(max_zone_pfns);\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        matching = [x for x in evidence if x["object"] == "arch_zone_limits_init"]
        self.assertEqual([x["kind"] for x in matching], ["function-declaration"])
        self.assertEqual(matching[0]["selected_use"], "call to arch_zone_limits_init(...)")
        self.assertEqual(matching[0]["selected_path"], "mm/page_alloc.c")
        self.assertEqual(matching[0]["provider_path"], "include/linux/mm.h")

    def test_generic_dt_property_without_matching_compatible_is_not_evidence(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64: dts: add display clocks", direct=True)
        selected.files = ["arch/arm64/boot/dts/qcom/display.dts"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "dt-bindings: camera clocks" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["Documentation/devicetree/bindings/clock/qcom,camera.yaml"]
            if sha == peer else selected.files
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/Documentation/devicetree/bindings/clock/qcom,camera.yaml "
            "b/Documentation/devicetree/bindings/clock/qcom,camera.yaml\n"
            "+  compatible:\n+    const: qcom,camera-cc\n+  clocks:\n+    maxItems: 4\n"
            if sha == peer else
            "diff --git a/arch/arm64/boot/dts/qcom/display.dts b/arch/arm64/boot/dts/qcom/display.dts\n"
            "+compatible = \"qcom,display-cc\";\n+clocks = <&gcc 1>;\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(x["kind"] == "dt-property-binding" for x in evidence))
        self.assertFalse(any(x["peer_sha"] == peer for x in evidence))

    def test_dt_property_can_use_compatible_from_earlier_peer_in_same_binding_schema(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("c" * 40, "arm64: dts: mediatek: add xsphy", direct=True)
        selected.files = ["arch/arm64/boot/dts/mediatek/mt7988a.dtsi"]
        selected.series_ids.add("S001")
        compat_peer = "a" * 40
        property_peer = "b" * 40
        binding = "Documentation/devicetree/bindings/phy/mediatek,mtk-xsphy.yaml"
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {
            "S001": [(4, compat_peer), (5, property_peer), (8, selected.sha)]
        }
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: {
            compat_peer: "dt-bindings: phy: add mt7988 compatible",
            property_peer: "dt-bindings: phy: add syscon type switch",
            selected.sha: selected.subject,
        }[sha]
        planner.changed_files = lambda sha: (
            [binding] if sha in {compat_peer, property_peer} else selected.files
        )
        patches = {
            compat_peer: (
                f"diff --git a/{binding} b/{binding}\n"
                "+      - mediatek,mt7988-xsphy\n"
            ),
            property_peer: (
                f"diff --git a/{binding} b/{binding}\n"
                "+  mediatek,syscon-type:\n"
                "+    $ref: /schemas/types.yaml#/definitions/phandle-array\n"
            ),
            selected.sha: (
                "diff --git a/arch/arm64/boot/dts/mediatek/mt7988a.dtsi "
                "b/arch/arm64/boot/dts/mediatek/mt7988a.dtsi\n"
                "+compatible = \"mediatek,mt7988-xsphy\";\n"
                "+mediatek,syscon-type = <&topmisc 0>;\n"
            ),
        }
        planner.patch_excerpt = lambda sha, _limit: patches[sha]

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        property_evidence = [
            item for item in evidence
            if item["peer_sha"] == property_peer
            and item["kind"] == "dt-property-binding-series"
        ]
        self.assertEqual(len(property_evidence), 1)
        self.assertEqual(
            property_evidence[0]["object"],
            "mediatek,mt7988-xsphy:mediatek,syscon-type",
        )
        self.assertIn(binding, property_evidence[0]["peer_provides"])
        self.assertIn(compat_peer[:12], property_evidence[0]["peer_provides"])

    def test_dt_property_same_series_compatible_must_use_same_binding_path(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("c" * 40, "arm64: dts: add device", direct=True)
        selected.files = ["arch/arm64/boot/dts/vendor/device.dts"]
        selected.series_ids.add("S001")
        compat_peer = "a" * 40
        property_peer = "b" * 40
        compat_binding = "Documentation/devicetree/bindings/clock/vendor,device.yaml"
        other_binding = "Documentation/devicetree/bindings/clock/vendor,other.yaml"
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {
            "S001": [(1, compat_peer), (2, property_peer), (3, selected.sha)]
        }
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: sha
        planner.changed_files = lambda sha: (
            [compat_binding] if sha == compat_peer
            else [other_binding] if sha == property_peer
            else selected.files
        )
        patches = {
            compat_peer: (
                f"diff --git a/{compat_binding} b/{compat_binding}\n"
                "+      - vendor,device\n"
            ),
            property_peer: (
                f"diff --git a/{other_binding} b/{other_binding}\n"
                "+  clocks:\n+    maxItems: 4\n"
            ),
            selected.sha: (
                "diff --git a/arch/arm64/boot/dts/vendor/device.dts "
                "b/arch/arm64/boot/dts/vendor/device.dts\n"
                "+compatible = \"vendor,device\";\n+clocks = <&gcc 1>;\n"
            ),
        }
        planner.patch_excerpt = lambda sha, _limit: patches[sha]

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(
            item["peer_sha"] == property_peer
            and item["kind"].startswith("dt-property-binding")
            for item in evidence
        ))

    def test_talos_binding_example_label_is_not_dts_provider(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64: dts: qcom: talos: add displayport", direct=True)
        selected.files = ["arch/arm64/boot/dts/qcom/talos.dtsi"]
        selected.series_ids.add("S101")
        peer = "a" * 40
        binding = "Documentation/devicetree/bindings/display/msm/qcom,sm6150-mdss.yaml"
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S101": [(2, peer), (3, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: (
            "dt-bindings: display: fix example" if sha == peer else selected.subject
        )
        planner.changed_files = lambda sha: [binding] if sha == peer else selected.files
        planner.patch_excerpt = lambda sha, _limit: (
            f"diff --git a/{binding} b/{binding}\n"
            "+examples:\n"
            "+  - |\n"
            "+    dpu_intf0_out: endpoint { };\n"
            if sha == peer else
            "diff --git a/arch/arm64/boot/dts/qcom/talos.dtsi "
            "b/arch/arm64/boot/dts/qcom/talos.dtsi\n"
            "+remote-endpoint = <&dpu_intf0_out>;\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(
            item["peer_sha"] == peer and item["kind"] == "dts-label"
            for item in evidence
        ))

    def test_static_tpdm_binding_example_label_is_not_dts_provider(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64: dts: qcom: lemans: enable static TPDM", direct=True)
        selected.files = ["arch/arm64/boot/dts/qcom/sa8775p.dtsi"]
        selected.series_ids.add("S115")
        peer = "a" * 40
        binding = "Documentation/devicetree/bindings/arm/qcom,coresight-tpdm.yaml"
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S115": [(1, peer), (3, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: (
            "dt-bindings: arm: document static TPDM" if sha == peer else selected.subject
        )
        planner.changed_files = lambda sha: [binding] if sha == peer else selected.files
        planner.patch_excerpt = lambda sha, _limit: (
            f"diff --git a/{binding} b/{binding}\n"
            "+examples:\n"
            "+  - |\n"
            "+    turing_llm_tpdm_out: endpoint { };\n"
            if sha == peer else
            "diff --git a/arch/arm64/boot/dts/qcom/sa8775p.dtsi "
            "b/arch/arm64/boot/dts/qcom/sa8775p.dtsi\n"
            "+remote-endpoint = <&turing_llm_tpdm_out>;\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(
            item["peer_sha"] == peer and item["kind"] == "dts-label"
            for item in evidence
        ))

    def test_binding_example_compatible_assignment_is_not_schema_evidence(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64: dts: add board", direct=True)
        selected.files = ["arch/arm64/boot/dts/vendor/board.dts"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        binding = "Documentation/devicetree/bindings/arm/vendor,board.yaml"
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "dt-bindings: edit example" if sha == peer else selected.subject
        planner.changed_files = lambda sha: [binding] if sha == peer else selected.files
        planner.patch_excerpt = lambda sha, _limit: (
            f"diff --git a/{binding} b/{binding}\n"
            "+examples:\n"
            "+  - |\n"
            "+    compatible = \"vendor,board\";\n"
            if sha == peer else
            "diff --git a/arch/arm64/boot/dts/vendor/board.dts "
            "b/arch/arm64/boot/dts/vendor/board.dts\n"
            "+compatible = \"vendor,board\";\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(
            item["peer_sha"] == peer and item["kind"] == "compatible-binding"
            for item in evidence
        ))

    def test_binding_items_tuple_only_exposes_specific_compatible(self):
        text = (
            "      - items:\n"
            "          - const: medion,sprchrgd14s1\n"
            "          - const: qcom,x1e80100\n"
        )
        self.assertEqual(
            kbp.Planner._yaml_binding_compatible_candidates(text),
            {"medion,sprchrgd14s1"},
        )

    def test_medion_board_does_not_depend_on_tuxedo_fallback_compatible(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("c" * 40, "arm64: dts: qcom: add Medion board", direct=True)
        selected.files = ["arch/arm64/boot/dts/qcom/x1e80100-medion.dts"]
        selected.series_ids.add("S001")
        tuxedo = "a" * 40
        medion = "b" * 40
        binding = "Documentation/devicetree/bindings/arm/qcom.yaml"
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(3, tuxedo), (5, medion), (6, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: {
            tuxedo: "dt-bindings: arm: qcom: add TUXEDO device",
            medion: "dt-bindings: arm: qcom: add Medion device",
            selected.sha: selected.subject,
        }[sha]
        planner.changed_files = lambda sha: [binding] if sha in {tuxedo, medion} else selected.files
        patches = {
            tuxedo: (
                f"diff --git a/{binding} b/{binding}\n"
                "+      - items:\n"
                "+          - const: tuxedo,qualia-xa1510\n"
                "+          - const: qcom,x1e80100\n"
            ),
            medion: (
                f"diff --git a/{binding} b/{binding}\n"
                "+      - items:\n"
                "+          - const: medion,sprchrgd14s1\n"
                "+          - const: qcom,x1e80100\n"
            ),
            selected.sha: (
                "diff --git a/arch/arm64/boot/dts/qcom/x1e80100-medion.dts "
                "b/arch/arm64/boot/dts/qcom/x1e80100-medion.dts\n"
                "+compatible = \"medion,sprchrgd14s1\", \"qcom,x1e80100\";\n"
            ),
        }
        planner.patch_excerpt = lambda sha, _limit: patches[sha]

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        compat = [item for item in evidence if item["kind"] == "compatible-binding"]
        self.assertEqual([(item["peer_sha"], item["object"]) for item in compat], [
            (medion, "medion,sprchrgd14s1")
        ])

    def test_armsom_board_does_not_use_rk3576_fallback_for_peripheral_bindings(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("d" * 40, "arm64: dts: rockchip: add ArmSoM Sige5", direct=True)
        selected.files = ["arch/arm64/boot/dts/rockchip/rk3576-armsom-sige5.dts"]
        selected.series_ids.add("S001")
        board = "a" * 40
        pmu = "b" * 40
        watchdog = "c" * 40
        board_binding = "Documentation/devicetree/bindings/arm/rockchip.yaml"
        pmu_binding = "Documentation/devicetree/bindings/soc/rockchip/pmu.yaml"
        wdt_binding = "Documentation/devicetree/bindings/watchdog/snps,dw-wdt.yaml"
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {
            "S001": [(2, board), (3, pmu), (7, watchdog), (10, selected.sha)]
        }
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: sha
        planner.changed_files = lambda sha: {
            board: [board_binding], pmu: [pmu_binding], watchdog: [wdt_binding],
            selected.sha: selected.files,
        }[sha]
        patches = {
            board: (
                f"diff --git a/{board_binding} b/{board_binding}\n"
                "+      - items:\n"
                "+          - const: armsom,sige5\n"
                "+          - const: rockchip,rk3576\n"
            ),
            pmu: (
                f"diff --git a/{pmu_binding} b/{pmu_binding}\n"
                "+      - rockchip,rk3576\n"
            ),
            watchdog: (
                f"diff --git a/{wdt_binding} b/{wdt_binding}\n"
                "+      - rockchip,rk3576\n"
            ),
            selected.sha: (
                "diff --git a/arch/arm64/boot/dts/rockchip/rk3576-armsom-sige5.dts "
                "b/arch/arm64/boot/dts/rockchip/rk3576-armsom-sige5.dts\n"
                "+compatible = \"armsom,sige5\", \"rockchip,rk3576\";\n"
            ),
        }
        planner.patch_excerpt = lambda sha, _limit: patches[sha]

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        compat = [item for item in evidence if item["kind"] == "compatible-binding"]
        self.assertEqual([(item["peer_sha"], item["object"]) for item in compat], [
            (board, "armsom,sige5")
        ])

    def test_cross_arch_local_declaration_is_not_common_prerequisite_evidence(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64 consumer", direct=True)
        selected.files = ["arch/arm64/kernel/foo.c"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "alpha helper plus unrelated common change" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["arch/alpha/include/asm/foo.h", "include/linux/unrelated.h"]
            if sha == peer else selected.files
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/arch/alpha/include/asm/foo.h b/arch/alpha/include/asm/foo.h\n"
            "+void shared_name(void);\n"
            "diff --git a/include/linux/unrelated.h b/include/linux/unrelated.h\n"
            "+#define UNRELATED_COMMON 1\n"
            if sha == peer else
            "diff --git a/arch/arm64/kernel/foo.c b/arch/arm64/kernel/foo.c\n"
            "+shared_name();\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(
            x["kind"] == "function-declaration" and x["object"] == "shared_name"
            for x in evidence
        ))

    def test_multiple_selected_evidence_ids_for_same_peer_collapse(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "consumer", direct=True)
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.advisor_prerequisite_evidence = lambda _sha: [
            {
                "evidence_id": "ev-one", "peer_sha": peer, "peer_subject": "provider",
                "kind": "compatible-binding", "object": "vendor,board",
                "selected_use": 'compatible = "vendor,board"',
                "peer_provides": "binding text for compatible vendor,board",
            },
            {
                "evidence_id": "ev-two", "peer_sha": peer, "peer_subject": "provider",
                "kind": "dt-property-binding", "object": "vendor,board:clocks",
                "selected_use": "DTS property clocks on compatible vendor,board",
                "peer_provides": "binding property clocks: for compatible vendor,board",
            },
        ]
        planner.advisor_inbound_take_recommendations = lambda _sha: []
        result = planner.validate_advisor_result(selected.sha, {
            "decision": "TAKE",
            "rationale": "Binding is needed.",
            "prerequisite_evidence_ids": ["ev-one", "ev-two"],
            "inbound_prerequisite_assessments": [],
            "risks": [],
        })
        self.assertEqual(len(result["prerequisite_peers"]), 1)
        self.assertEqual(result["prerequisite_peers"][0]["sha"], peer)
        self.assertEqual(result["prerequisite_peers"][0]["evidence_id"], "ev-one")

    def test_advisor_error_diagnostic_preserves_bounded_raw_response(self):
        planner = self.minimal_planner()
        node = kbp.CommitNode("a" * 40, "arm64 test", direct=True)
        planner.nodes = {node.sha: node}
        planner.info = lambda _msg: None
        with tempfile.TemporaryDirectory() as tmp:
            planner.advisor_error_log_path = Path(tmp) / "advisor-errors.jsonl"
            planner.advisor_error_log_entries = 0
            raw = "x" * 10000
            exc = kbp.AdvisorError(
                "bad structured output", kind="invalid-response", retryable=True,
                raw_response=raw,
            )
            planner._write_advisor_error_diagnostic(node.sha, 2, exc)
            records = planner.advisor_error_log_path.read_text().splitlines()
            self.assertEqual(len(records), 1)
            record = json.loads(records[0])
            self.assertEqual(record["sha"], node.sha)
            self.assertEqual(record["attempt"], 2)
            self.assertEqual(record["kind"], "invalid-response")
            self.assertLessEqual(len(record["raw_response"]), 4096 + 128)
            self.assertIn("truncated", record["raw_response"])
            self.assertEqual(planner.advisor_error_log_entries, 1)

    def test_skip_cannot_claim_inbound_take_prerequisite_is_required(self):
        planner = self.minimal_planner()
        node = kbp.CommitNode("a" * 40, "candidate")
        planner.nodes = {node.sha: node}
        planner.advisor_prerequisite_evidence = lambda _sha: []
        planner.advisor_inbound_take_recommendations = lambda _sha: [{
            "consumer_sha": "b" * 40,
            "consumer_decision": "TAKE",
            "evidence_id": "ev-inbound",
            "evidence_kind": "function-declaration",
            "evidence_object": "arch_zone_limits_init",
            "reason": "Consumer needs the shared declaration.",
        }]
        with self.assertRaises(kbp.AdvisorError):
            planner.validate_advisor_result(node.sha, {
                "decision": "SKIP",
                "rationale": "Unsupported architecture implementation.",
                "prerequisite_evidence_ids": [],
                "inbound_prerequisite_assessments": [{
                    "evidence_id": "ev-inbound",
                    "disposition": "REQUIRED",
                    "reason": "The consumer still needs the declaration.",
                }],
                "risks": [],
            })

        result = planner.validate_advisor_result(node.sha, {
            "decision": "SKIP",
            "rationale": "The declaration is already supplied by another selected patch.",
            "prerequisite_evidence_ids": [],
            "inbound_prerequisite_assessments": [{
                "evidence_id": "ev-inbound",
                "disposition": "NOT_REQUIRED",
                "reason": "Another selected patch supplies the identical shared declaration.",
            }],
            "risks": [],
        })
        self.assertEqual(result["decision"], "SKIP")

    def test_take_consumer_recommendation_is_recorded_on_actionable_peer(self):
        planner = self.minimal_planner()
        consumer = kbp.CommitNode("b" * 40, "powerpc consumer")
        consumer.direct = True
        peer = kbp.CommitNode("a" * 40, "alpha provider")
        peer.add_reason("symbol:arch_zone_limits_init", consumer.sha)
        planner.nodes = {consumer.sha: consumer, peer.sha: peer}
        consumer.advisor_decision = "TAKE"
        consumer.advisor_prerequisites = [{
            "sha": peer.sha,
            "evidence_id": "ev-decl",
            "evidence_kind": "function-declaration",
            "evidence_object": "arch_zone_limits_init",
            "reason": "Provides shared declaration.",
        }]
        planner.advisor_prerequisite_evidence = lambda sha: [{
            "evidence_id": "ev-decl",
            "peer_sha": peer.sha,
            "peer_subject": peer.subject,
            "kind": "function-declaration",
            "object": "arch_zone_limits_init",
            "selected_use": "definition of arch_zone_limits_init(...)",
            "peer_provides": "declaration of arch_zone_limits_init(...)",
        }] if sha == consumer.sha else []

        planner._record_advisor_prerequisite_recommendations(consumer.sha)
        self.assertEqual(len(peer.advisor_recommended_by), 1)
        self.assertEqual(peer.advisor_recommended_by[0]["consumer_sha"], consumer.sha)
        self.assertEqual(peer.advisor_recommended_by[0]["evidence_id"], "ev-decl")

    def test_reconciliation_revisits_earlier_skip_after_late_take_prerequisite(self):
        planner = self.minimal_planner()
        provider = kbp.CommitNode("a" * 40, "alpha provider")
        provider.add_reason("symbol:arch_zone_limits_init", "b" * 40)
        consumer = kbp.CommitNode("b" * 40, "powerpc consumer", direct=True)
        provider.advisor_decision = "SKIP"
        provider.advisor_recommended_by = [{
            "consumer_sha": consumer.sha,
            "consumer_subject": consumer.subject,
            "consumer_decision": "TAKE",
            "evidence_id": "ev-decl",
            "evidence_kind": "function-declaration",
            "evidence_object": "arch_zone_limits_init",
            "consumer_use": "definition of arch_zone_limits_init(...)",
            "candidate_provides": "declaration of arch_zone_limits_init(...)",
            "reason": "Provides shared declaration.",
        }]
        consumer.advisor_decision = "TAKE"
        planner.nodes = {provider.sha: provider, consumer.sha: consumer}
        planner.advisor_reconsiderations = 0
        planner.info = lambda _msg: None
        calls = []

        def fake_evaluate(_client, _prompt, sha, reconsideration=False):
            calls.append((sha, reconsideration))
            provider.advisor_decision = "TAKE"
            provider.advisor_inbound_assessments = [{
                "evidence_id": "ev-decl",
                "disposition": "REQUIRED",
                "reason": "Shared declaration is required by the TAKE consumer.",
            }]

        planner._advisor_evaluate_one = fake_evaluate
        planner._advisor_reconciliation_pass(object(), "prompt", [provider.sha, consumer.sha])
        self.assertEqual(calls, [(provider.sha, True)])
        self.assertEqual(planner.advisor_reconsiderations, 1)
        self.assertEqual(provider.advisor_decision, "TAKE")

    def test_consistency_check_flags_take_to_skip_prerequisite(self):
        planner = self.minimal_planner()
        consumer = kbp.CommitNode("b" * 40, "consumer", direct=True)
        peer = kbp.CommitNode("a" * 40, "provider")
        peer.add_reason("symbol:FOO", consumer.sha)
        consumer.advisor_decision = "TAKE"
        consumer.advisor_prerequisites = [{
            "sha": peer.sha,
            "evidence_id": "ev-foo",
            "evidence_kind": "macro",
            "evidence_object": "FOO",
            "reason": "Provides FOO.",
        }]
        peer.advisor_decision = "SKIP"
        planner.nodes = {consumer.sha: consumer, peer.sha: peer}
        planner.advisor_consistency_issues = []

        planner._advisor_check_consistency()
        self.assertEqual(len(planner.advisor_consistency_issues), 1)
        issue = planner.advisor_consistency_issues[0]
        self.assertEqual(issue["kind"], "TAKE->SKIP")
        self.assertEqual(issue["consumer_sha"], consumer.sha)
        self.assertEqual(issue["prerequisite_sha"], peer.sha)
        self.assertEqual(len(consumer.advisor_consistency_warnings), 1)
        self.assertEqual(len(peer.advisor_consistency_warnings), 1)

    def test_advisor_order_is_consumer_first(self):
        planner = self.minimal_planner()
        direct = kbp.CommitNode("d" * 40, "arm64 direct", direct=True)
        dep1 = kbp.CommitNode("b" * 40, "near dependency")
        dep1.add_reason("symbol:FOO", direct.sha)
        dep2 = kbp.CommitNode("a" * 40, "deep dependency")
        dep2.add_reason("symbol:BAR", dep1.sha)
        planner.nodes = {direct.sha: direct, dep1.sha: dep1, dep2.sha: dep2}
        ordered = planner.advisor_order([dep2.sha, dep1.sha, direct.sha])
        self.assertEqual(ordered, [direct.sha, dep1.sha, dep2.sha])

    def test_loongarch_dependency_packet_includes_arm64_consumer_chain(self):
        planner = self.minimal_planner()
        arm = kbp.CommitNode("c" * 40, "arm64/ptrace: Split report_syscall()", direct=True)
        arm.files = ["arch/arm64/kernel/ptrace.c"]
        loong_generic = kbp.CommitNode("b" * 40, "loongarch: Use generic TIF bits")
        loong_generic.files = ["arch/loongarch/include/asm/thread_info.h"]
        loong_generic.add_reason("symbol:TIF_SINGLESTEP", arm.sha)
        loong_lazy = kbp.CommitNode("a" * 40, "LoongArch: Allow to enable PREEMPT_LAZY")
        loong_lazy.files = ["arch/loongarch/Kconfig"]
        loong_lazy.add_reason("conflict-hunk-preimage:20", loong_generic.sha)
        arm.advisor_decision = "TAKE"
        planner.nodes = {
            arm.sha: arm,
            loong_generic.sha: loong_generic,
            loong_lazy.sha: loong_lazy,
        }
        planner.subject = lambda sha: planner.nodes[sha].subject
        planner.changed_files = lambda sha: planner.nodes[sha].files
        planner.patch_excerpt = lambda sha, limit: {
            arm.sha: "+ static void report_syscall(void)",
            loong_generic.sha: "+ #define TIF_SINGLESTEP ...",
            loong_lazy.sha: "+ select ARCH_HAS_PREEMPT_LAZY",
        }[sha]
        planner.body = lambda sha: planner.nodes[sha].subject
        packet = planner.advisor_packet(loong_lazy.sha)
        self.assertTrue(packet["commit"]["scope"]["unsupported_architecture_only"])
        context = packet["commit"]["deterministic_consumer_context"]
        self.assertEqual(context["relationship_status"], "provenance_only")
        self.assertNotIn("required_by", packet["commit"])
        consumers = context["consumers"]
        self.assertTrue(all(x["relationship_status"] == "provenance_only" for x in consumers))
        self.assertTrue(all("requires_sha" not in x for x in consumers))
        self.assertEqual(packet["commit"]["actual_inbound_take_prerequisite_count"], 0)
        self.assertEqual(packet["commit"]["actual_inbound_take_prerequisites"], [])
        self.assertEqual([x["depth"] for x in consumers], [1, 2])
        self.assertEqual(consumers[0]["sha"], loong_generic.sha)
        self.assertEqual(consumers[1]["sha"], arm.sha)
        self.assertTrue(consumers[1]["scope"]["touches_focus_paths"])
        self.assertEqual(consumers[1]["advisor_decision"], "TAKE")

    def test_advisor_prompt_rejects_weak_same_series_justifications(self):
        planner = self.minimal_planner()
        prompt = planner.advisor_system_prompt()
        self.assertIn("Mere adjacency", prompt)
        self.assertIn("touching the same file/subsystem", prompt)
        self.assertIn("Architecture ownership can also appear outside arch/*", prompt)
        self.assertIn("TAKE_UNLESS_CONCRETE_EXCLUSION", prompt)
        self.assertIn("product-target evidence is not", prompt)
        self.assertIn("an exclusion reason", prompt)
        self.assertIn("same_series_consumed_objects", prompt)
        self.assertIn("selected_path/provider_path", prompt)
        self.assertIn("deterministic_consumer_context", prompt)
        self.assertIn("provenance-only", prompt)
        self.assertIn("actual_inbound_take_prerequisite_count", prompt)
        self.assertIn("there are NO inbound TAKE prerequisite recommendations", prompt)
        self.assertIn("prerequisite_evidence_ids", prompt)
        self.assertIn("Apply this uniformly to direct commits in any requested focus path", prompt)
        self.assertNotIn("direct arm64 DTS/platform commits", prompt)
        self.assertIn("SKIP_UNSUPPORTED_ARCHITECTURE_ONLY", prompt)
        self.assertIn("SKIP_OBSOLETED_BY_TAKE_CONSUMER", prompt)
        self.assertIn("REQUIRE_SEMANTIC_PROOF_FOR_DT_BINDING_SYMBOL", prompt)
        self.assertIn("namespace-local", prompt)

        self.assertIn("replay: conflict is an integration-cost signal", prompt)
        self.assertIn("not by itself a reason for REVIEW or SKIP", prompt)
        self.assertIn("If your decision is SKIP, return no prerequisite evidence", prompt)

    def test_unsupported_architecture_only_dependency_gets_policy_skip(self):
        planner = self.minimal_planner()
        consumer = kbp.CommitNode("c" * 40, "arm64 crypto migration", direct=True)
        consumer.files = ["lib/crypto/arm64/nh.h"]
        consumer.advisor_decision = "TAKE"
        dep = kbp.CommitNode("a" * 40, "ARM/simd: add scoped guard")
        dep.files = ["arch/arm/include/asm/simd.h"]
        dep.add_reason("symbol:scoped_ksimd", consumer.sha)
        planner.nodes = {consumer.sha: consumer, dep.sha: dep}
        planner.subject = lambda sha: planner.nodes[sha].subject
        planner.changed_files = lambda sha: planner.nodes[sha].files
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/lib/crypto/arm64/nh.h b/lib/crypto/arm64/nh.h\n"
            "+scoped_ksimd();\n"
            if sha == consumer.sha else
            "diff --git a/arch/arm/include/asm/simd.h b/arch/arm/include/asm/simd.h\n"
            "+#define scoped_ksimd() ...\n"
        )
        planner.body = lambda sha: planner.nodes[sha].subject

        packet = planner.advisor_packet(dep.sha)
        self.assertTrue(packet["commit"]["scope"]["unsupported_architecture_only"])
        self.assertEqual(
            packet["commit"]["default_policy_bias"],
            "SKIP_UNSUPPORTED_ARCHITECTURE_ONLY",
        )
        self.assertEqual(packet["commit"]["actual_inbound_take_prerequisite_count"], 0)
        result = planner.advisor_policy_override(dep.sha, packet)
        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "SKIP")
        self.assertIn("unsupported kernel architecture", result["rationale"])

    def test_policy_skip_defers_when_take_consumer_has_evidence_bound_inbound(self):
        planner = self.minimal_planner()
        dep = kbp.CommitNode("a" * 40, "unsupported architecture candidate")
        dep.files = ["arch/arm/kernel/foo.c"]
        dep.advisor_recommended_by = [{
            "consumer_sha": "c" * 40,
            "consumer_subject": "supported consumer",
            "consumer_decision": "TAKE",
            "evidence_id": "ev-real",
            "evidence_kind": "function-declaration",
            "evidence_object": "foo",
            "consumer_use": "foo()",
            "candidate_provides": "foo()",
            "reason": "evidence-bound recommendation",
        }]
        planner.nodes = {dep.sha: dep}
        planner.subject = lambda _sha: dep.subject
        planner.changed_files = lambda _sha: dep.files
        planner.patch_excerpt = lambda _sha, _limit: "+int foo;"
        planner.body = lambda _sha: dep.subject

        packet = planner.advisor_packet(dep.sha)
        self.assertEqual(packet["commit"]["actual_inbound_take_prerequisite_count"], 1)
        self.assertEqual(
            packet["commit"]["default_policy_bias"],
            "RECONCILE_UNSUPPORTED_ARCHITECTURE_INBOUND",
        )
        self.assertIsNone(planner.advisor_policy_override(dep.sha, packet))

    def test_obsoleted_dependency_gets_policy_skip(self):
        planner = self.minimal_planner()
        old = kbp.CommitNode("a" * 40, "old board fix")
        old.files = [
            "arch/arm64/boot/dts/vendor/old-board.dts",
            "arch/arm64/boot/dts/vendor/old-board.dtsi",
        ]
        consumer = kbp.CommitNode("c" * 40, "remove obsolete board", direct=True)
        consumer.files = list(old.files)
        consumer.advisor_decision = "TAKE"
        old.add_reason("conflict-hunk-preimage:8", consumer.sha)
        planner.nodes = {old.sha: old, consumer.sha: consumer}
        planner.subject = lambda sha: planner.nodes[sha].subject
        planner.changed_files = lambda sha: planner.nodes[sha].files
        planner.body = lambda sha: planner.nodes[sha].subject

        def patch_excerpt(sha, _limit):
            if sha == consumer.sha:
                return (
                    "diff --git a/arch/arm64/boot/dts/vendor/old-board.dts "
                    "b/arch/arm64/boot/dts/vendor/old-board.dts\n"
                    "deleted file mode 100644\n"
                    "+++ /dev/null\n"
                    "diff --git a/arch/arm64/boot/dts/vendor/old-board.dtsi "
                    "b/arch/arm64/boot/dts/vendor/old-board.dtsi\n"
                    "deleted file mode 100644\n"
                    "+++ /dev/null\n"
                )
            return "+ status = \"okay\";\n"

        planner.patch_excerpt = patch_excerpt
        packet = planner.advisor_packet(old.sha)
        self.assertTrue(
            packet["commit"]["obsolescence"]["all_changed_files_deleted_by_take_consumers"]
        )
        self.assertEqual(
            packet["commit"]["default_policy_bias"],
            "SKIP_OBSOLETED_BY_TAKE_CONSUMER",
        )
        result = planner.advisor_policy_override(old.sha, packet)
        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "SKIP")
        self.assertIn("historical", planner.advisor_system_prompt())

    def test_partial_deletion_does_not_force_obsolescence_skip(self):
        planner = self.minimal_planner()
        old = kbp.CommitNode("a" * 40, "mixed dependency")
        old.files = ["drivers/foo/a.c", "drivers/foo/b.c"]
        consumer = kbp.CommitNode("c" * 40, "delete one file", direct=True)
        consumer.files = ["drivers/foo/a.c"]
        consumer.advisor_decision = "TAKE"
        old.add_reason("conflict-hunk-preimage:4", consumer.sha)
        planner.nodes = {old.sha: old, consumer.sha: consumer}
        planner.subject = lambda sha: planner.nodes[sha].subject
        planner.changed_files = lambda sha: planner.nodes[sha].files
        planner.body = lambda sha: planner.nodes[sha].subject
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/drivers/foo/a.c b/drivers/foo/a.c\n"
            "deleted file mode 100644\n+++ /dev/null\n"
            if sha == consumer.sha else "+int changed;\n"
        )

        packet = planner.advisor_packet(old.sha)
        self.assertFalse(
            packet["commit"]["obsolescence"]["all_changed_files_deleted_by_take_consumers"]
        )
        self.assertIsNone(planner.advisor_policy_override(old.sha, packet))

    def test_dt_binding_symbol_only_dependency_requires_semantic_proof(self):
        planner = self.minimal_planner()
        dep = kbp.CommitNode("a" * 40, "dt-bindings: clock: add unrelated controller")
        dep.files = ["include/dt-bindings/clock/rockchip,rk3506-cru.h"]
        dep.reasons.add("symbol:CLK_SPI2")
        planner.nodes = {dep.sha: dep}
        planner.subject = lambda _sha: dep.subject
        planner.changed_files = lambda _sha: dep.files
        planner.patch_excerpt = lambda _sha, _limit: "+#define CLK_SPI2 42\n"
        planner.body = lambda _sha: dep.subject

        packet = planner.advisor_packet(dep.sha)
        self.assertTrue(packet["commit"]["dependency_policy"]["symbol_only_dependency"])
        self.assertTrue(packet["commit"]["dependency_policy"]["dt_binding_header_only"])
        self.assertEqual(
            packet["commit"]["default_policy_bias"],
            "REQUIRE_SEMANTIC_PROOF_FOR_DT_BINDING_SYMBOL",
        )

    def test_extract_symbol_uses_retains_changed_file_path(self):
        planner = self.minimal_planner()
        planner.args.max_symbols = 32
        planner.parse_hunks = lambda _sha: [
            types.SimpleNamespace(
                path="arch/arm64/boot/dts/rockchip/rk3588s.dtsi",
                added_lines=["clocks = <&cru CLK_SPI2>;"],
            ),
            types.SimpleNamespace(
                path="drivers/foo/example.c",
                added_lines=["ret = helper_fn(dev);"],
            ),
        ]

        uses = planner.extract_symbol_uses("a" * 40)
        self.assertEqual(
            uses["CLK_SPI2"],
            {"arch/arm64/boot/dts/rockchip/rk3588s.dtsi"},
        )
        self.assertEqual(uses["helper_fn"], {"drivers/foo/example.c"})

    def test_dt_binding_include_closure_follows_dtsi_to_binding_header(self):
        planner = self.minimal_planner()
        start = "arch/arm64/boot/dts/qcom/board.dts"
        dtsi = "arch/arm64/boot/dts/qcom/sm8750.dtsi"
        header = "include/dt-bindings/clock/qcom,sm8750-gcc.h"
        files = {
            start: '#include "sm8750.dtsi"\n',
            dtsi: '#include <dt-bindings/clock/qcom,sm8750-gcc.h>\n',
            header: "#define GCC_VIDEO_AHB_CLK 1\n",
        }
        planner._tree_file_text = lambda _sha, path: files.get(path)

        closure = planner.dt_binding_include_closure("a" * 40, start)
        self.assertEqual(closure, {header})

    def _symbol_definition_test_planner(
        self,
        *,
        symbol: str,
        provider_path: str,
        use_path: str,
        reachable_headers: set[str],
    ):
        planner = self.minimal_planner()
        selected = "b" * 40
        parent = "1" * 40
        provider = "a" * 40
        grep_output = f"{parent}:{provider_path}:10:#define {symbol} 42\n"
        planner.git = types.SimpleNamespace(
            parents=lambda _sha: [parent],
            run=lambda args, check=False: grep_output if args[0] == "grep" else "",
        )
        planner.blame_commits = lambda _parent, path, _lo, _hi: (
            kbp.collections.Counter({provider: 1}) if path == provider_path else kbp.collections.Counter()
        )
        planner.upstream_before = lambda _dep, _sha: True
        planner.match = lambda _dep, check_ancestry=True: kbp.Match(status="missing")
        planner.dt_binding_include_closure = lambda _sha, path: (
            set(reachable_headers) if path == use_path else set()
        )
        return planner, selected, provider

    def test_rk3588_symbol_does_not_select_rk3506_binding_header(self):
        symbol = "CLK_SPI2"
        provider_path = "include/dt-bindings/clock/rockchip,rk3506-cru.h"
        use_path = "arch/arm64/boot/dts/rockchip/rk3588s.dtsi"
        planner, selected, _provider = self._symbol_definition_test_planner(
            symbol=symbol,
            provider_path=provider_path,
            use_path=use_path,
            reachable_headers={"include/dt-bindings/clock/rockchip,rk3588-cru.h"},
        )

        deps = planner.definition_dependencies(
            selected, {symbol}, {symbol: {use_path}}
        )
        self.assertEqual(deps, {})

    def test_sm8750_symbol_does_not_select_sc8180x_binding_header(self):
        symbol = "GCC_VIDEO_AHB_CLK"
        provider_path = "include/dt-bindings/clock/qcom,gcc-sc8180x.h"
        use_path = "arch/arm64/boot/dts/qcom/sm8750.dtsi"
        planner, selected, _provider = self._symbol_definition_test_planner(
            symbol=symbol,
            provider_path=provider_path,
            use_path=use_path,
            reachable_headers={"include/dt-bindings/clock/qcom,sm8750-gcc.h"},
        )

        deps = planner.definition_dependencies(
            selected, {symbol}, {symbol: {use_path}}
        )
        self.assertEqual(deps, {})

    def test_dt_binding_symbol_from_reachable_header_remains_dependency(self):
        symbol = "GCC_VIDEO_AHB_CLK"
        provider_path = "include/dt-bindings/clock/qcom,sm8750-gcc.h"
        use_path = "arch/arm64/boot/dts/qcom/sm8750.dtsi"
        planner, selected, provider = self._symbol_definition_test_planner(
            symbol=symbol,
            provider_path=provider_path,
            use_path=use_path,
            reachable_headers={provider_path},
        )

        deps = planner.definition_dependencies(
            selected, {symbol}, {symbol: {use_path}}
        )
        self.assertEqual(deps, {symbol: provider})

    def test_skip_discards_outbound_prerequisite_recommendations(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "unsupported dependency")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.advisor_prerequisite_evidence = lambda _sha: [{
            "evidence_id": "ev-one",
            "peer_sha": peer,
            "peer_subject": "earlier peer",
            "kind": "macro",
            "object": "FOO",
            "selected_use": "FOO",
            "peer_provides": "#define FOO",
        }]
        planner.advisor_inbound_take_recommendations = lambda _sha: []
        result = planner.validate_advisor_result(selected.sha, {
            "decision": "SKIP",
            "rationale": "Unsupported architecture-only code.",
            "prerequisite_evidence_ids": ["ev-one"],
            "inbound_prerequisite_assessments": [],
            "risks": [],
        })
        self.assertEqual(result["prerequisite_peers"], [])

    def test_not_required_inbound_assessment_resolves_consistency_edge(self):
        planner = self.minimal_planner()
        consumer = kbp.CommitNode("b" * 40, "supported consumer", direct=True)
        peer = kbp.CommitNode("a" * 40, "unsupported provider")
        peer.add_reason("symbol:FOO", consumer.sha)
        consumer.advisor_decision = "TAKE"
        consumer.advisor_prerequisites = [{
            "sha": peer.sha,
            "evidence_id": "ev-foo",
            "evidence_kind": "function",
            "evidence_object": "foo",
            "reason": "Candidate initially looked required.",
        }]
        peer.advisor_decision = "SKIP"
        peer.advisor_inbound_assessments = [{
            "evidence_id": "ev-foo",
            "disposition": "NOT_REQUIRED",
            "reason": "The relationship is confined to an unsupported architecture.",
        }]
        planner.nodes = {consumer.sha: consumer, peer.sha: peer}
        planner.advisor_consistency_issues = []

        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues, [])
        self.assertEqual(consumer.advisor_consistency_warnings, [])
        self.assertEqual(peer.advisor_consistency_warnings, [])

    def test_not_required_prerequisite_moves_to_rejected_audit_list(self):
        planner = self.minimal_planner()
        consumer = kbp.CommitNode("b" * 40, "supported consumer", direct=True)
        peer = kbp.CommitNode("a" * 40, "unsupported provider")
        consumer.advisor_decision = "TAKE"
        consumer.advisor_prerequisites = [{
            "sha": peer.sha,
            "evidence_id": "ev-foo",
            "evidence_kind": "function",
            "evidence_object": "foo",
            "reason": "Candidate initially looked required.",
        }]
        peer.advisor_decision = "SKIP"
        peer.advisor_inbound_assessments = [{
            "evidence_id": "ev-foo",
            "disposition": "NOT_REQUIRED",
            "reason": "Unsupported architecture-local relationship.",
        }]
        planner.nodes = {consumer.sha: consumer, peer.sha: peer}

        planner._advisor_resolve_rejected_prerequisites()
        self.assertEqual(consumer.advisor_prerequisites, [])
        self.assertEqual(len(consumer.advisor_rejected_prerequisites), 1)
        rejected = consumer.advisor_rejected_prerequisites[0]
        self.assertEqual(rejected["sha"], peer.sha)
        self.assertEqual(rejected["assessment_disposition"], "NOT_REQUIRED")
        self.assertIn("Unsupported architecture", rejected["assessment_reason"])

    def test_architecture_owned_crypto_library_paths_are_classified(self):
        planner = self.minimal_planner()
        self.assertEqual(
            planner._path_architecture_dirs("lib/crypto/arm/nh.h"), {"arm"}
        )
        self.assertEqual(
            planner._path_architecture_dirs("lib/crypto/arm64/nh.h"), {"arm64"}
        )
        self.assertFalse(planner._advisor_path_product_relevant("lib/crypto/arm/nh.h"))
        self.assertTrue(planner._advisor_path_product_relevant("lib/crypto/arm64/nh.h"))

    def test_arm64_crypto_does_not_depend_on_arm32_same_named_symbol(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64 crypto consumer", direct=True)
        selected.files = ["lib/crypto/arm64/nh.h"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "arm32 crypto provider" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["lib/crypto/arm/nh.h"] if sha == peer else selected.files
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/lib/crypto/arm/nh.h b/lib/crypto/arm/nh.h\n"
            "+void shared_name(void);\n"
            if sha == peer else
            "diff --git a/lib/crypto/arm64/nh.h b/lib/crypto/arm64/nh.h\n"
            "+shared_name();\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(x["object"] == "shared_name" for x in evidence))

    def test_arm64_crypto_macro_invocation_does_not_use_arm32_false_declaration(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "arm64 NH migration", direct=True)
        selected.files = ["lib/crypto/arm64/nh.h"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "arm32 NH migration" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["lib/crypto/arm/nh.h"] if sha == peer else selected.files
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/lib/crypto/arm/nh.h b/lib/crypto/arm/nh.h\n"
            "+static DEFINE_STATIC_KEY_FALSE(nhpoly1305_use_simd);\n"
            if sha == peer else
            "diff --git a/lib/crypto/arm64/nh.h b/lib/crypto/arm64/nh.h\n"
            "+static DEFINE_STATIC_KEY_FALSE(nhpoly1305_use_simd);\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(
            x["object"] == "DEFINE_STATIC_KEY_FALSE" for x in evidence
        ))

    def test_unsupported_arch_local_use_in_global_patch_is_not_prerequisite_evidence(self):
        planner = self.minimal_planner()
        selected = kbp.CommitNode("b" * 40, "global consolidation", direct=True)
        selected.files = ["arch/mips/mm/init.c", "mm/page_alloc.c"]
        selected.series_ids.add("S001")
        peer = "a" * 40
        planner.nodes = {selected.sha: selected}
        planner.series_groups = {"S001": [(1, peer), (2, selected.sha)]}
        planner.match = lambda _sha: kbp.Match(status="missing")
        planner.subject = lambda sha: "mips local provider" if sha == peer else selected.subject
        planner.changed_files = lambda sha: (
            ["arch/mips/mm/init.c"] if sha == peer else selected.files
        )
        planner.patch_excerpt = lambda sha, _limit: (
            "diff --git a/arch/mips/mm/init.c b/arch/mips/mm/init.c\n"
            "+void paging_init(void)\n+{\n+}\n"
            if sha == peer else
            "diff --git a/arch/mips/mm/init.c b/arch/mips/mm/init.c\n"
            "+paging_init();\n"
            "diff --git a/mm/page_alloc.c b/mm/page_alloc.c\n"
            "+int common_change;\n"
        )

        evidence = planner.advisor_prerequisite_evidence(selected.sha)
        self.assertFalse(any(x["object"] == "paging_init" for x in evidence))

    def test_report_shows_rejected_prerequisite_audit(self):
        planner = self.minimal_planner()
        consumer = kbp.CommitNode("b" * 40, "consumer", direct=True)
        peer = kbp.CommitNode("a" * 40, "rejected provider")
        consumer.advisor_decision = "TAKE"
        consumer.advisor_rationale = "Keep consumer."
        consumer.advisor_rejected_prerequisites = [{
            "sha": peer.sha,
            "evidence_id": "ev-rejected",
            "assessment_disposition": "NOT_REQUIRED",
            "assessment_reason": "Unsupported architecture-local relationship.",
        }]
        planner.nodes = {consumer.sha: consumer, peer.sha: peer}
        planner.subject = lambda sha: planner.nodes[sha].subject
        planner.ordered_missing = lambda: [consumer.sha]

        report = planner.report_text()
        self.assertIn("advisor-rejected-prerequisite:", report)
        self.assertIn("NOT_REQUIRED", report)
        self.assertIn("Unsupported architecture-local relationship", report)

    def test_advisor_retries_transient_error_then_succeeds(self):
        planner = self.minimal_planner()
        node = kbp.CommitNode("a" * 40, "arm64 test", direct=True)
        planner.nodes = {node.sha: node}
        planner.advisor_calls = 0
        planner.advisor_retries_used = 0
        planner.info = lambda _msg: None
        planner.advisor_prerequisite_evidence = lambda _sha: []
        planner.advisor_inbound_take_recommendations = lambda _sha: []

        class FlakyClient:
            def __init__(self):
                self.calls = 0
            def advise(self, prompt, packet, schema):
                self.calls += 1
                if self.calls == 1:
                    raise kbp.AdvisorError("timed out", kind="timeout", retryable=True)
                return {
                    "decision": "TAKE",
                    "rationale": "ARM64 focus change.",
                    "prerequisite_evidence_ids": [],
                    "inbound_prerequisite_assessments": [],
                    "risks": [],
                }

        result = planner._advisor_call_with_retries(
            FlakyClient(), "prompt", node.sha, {"commit": {"sha": node.sha}}
        )
        self.assertEqual(result["decision"], "TAKE")
        self.assertEqual(planner.advisor_calls, 2)
        self.assertEqual(planner.advisor_retries_used, 1)
        self.assertEqual(node.advisor_attempts, 2)

    def test_report_distinguishes_advisor_failure_from_substantive_review(self):
        planner = self.minimal_planner()
        planner.advisor_calls = 2
        planner.advisor_retries_used = 1
        planner.advisor_cache_hits = 0
        planner.advisor_failure_kinds = kbp.collections.Counter({"timeout": 1})
        take = kbp.CommitNode("a" * 40, "take", direct=True)
        take.advisor_decision = "TAKE"
        review = kbp.CommitNode("b" * 40, "review", direct=True)
        review.advisor_decision = "REVIEW"
        review.advisor_rationale = "Ambiguous generic-code impact."
        failed = kbp.CommitNode("c" * 40, "failed", direct=True)
        failed.advisor_decision = "REVIEW"
        failed.advisor_rationale = "Advisor failed (timeout); manual review required."
        failed.advisor_error = "timed out"
        failed.advisor_error_kind = "timeout"
        planner.nodes = {take.sha: take, review.sha: review, failed.sha: failed}
        planner.ordered_missing = lambda: [take.sha, review.sha, failed.sha]
        report = planner.report_text()
        self.assertIn("1 TAKE, 0 SKIP, 1 REVIEW", report)
        self.assertIn("1 advisor failure(s) -> REVIEW", report)
        self.assertIn("advisor-failures: timeout=1", report)
        self.assertIn("advisor-error: timeout: timed out", report)


if __name__ == "__main__":
    unittest.main()

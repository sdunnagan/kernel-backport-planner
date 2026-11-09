#!/usr/bin/env python3
import importlib.machinery
import io
import collections
import json
import subprocess
import zipfile
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
        self.assertNotIn('worksheet-format:', report)
        self.assertIn('aaaaaaaaaaaa ("arm64: useful change")\n', report)
        self.assertNotIn('// Take', report)
        self.assertIn('advisor: TAKE', report)
        self.assertIn('advisor-rationale:', report)

    def test_worksheet_schema_separates_recommendations_from_status(self):
        planner = self.minimal_planner()
        nodes = []
        for char, decision in (("a", "TAKE"), ("b", "SKIP"), ("c", "REVIEW")):
            node = kbp.CommitNode(char * 40, f"arm64: {decision} candidate", direct=True)
            node.advisor_decision = decision
            node.advisor_rationale = f"Evidence for {decision}."
            nodes.append(node)
        planner.nodes = {node.sha: node for node in nodes}
        planner.ordered_missing = lambda: [node.sha for node in nodes]

        text = planner.report_text()
        self.assertNotIn("worksheet-format:", text)
        for node in nodes:
            header = f'{node.sha[:12]} ("{node.subject}")'
            self.assertIn(header + "\n", text)
            self.assertIn(f"      advisor: {node.advisor_decision}\n", text)
            self.assertIn(f"      advisor-rationale: {node.advisor_rationale}\n", text)
            self.assertNotIn(header + " //", text)
        self.assertNotIn("backporter-status:", text)
        self.assertNotIn("downstream-commit:", text)
        self.assertNotIn("engineer-decision:", text)
        self.assertNotIn("engineer-rationale:", text)

    def test_worksheet_generation_leaves_json_plan_unchanged(self):
        planner = self.minimal_planner()
        planner.advisor_model = None
        node = kbp.CommitNode("a" * 40, "board update", direct=True)
        node.advisor_decision = "TAKE"
        node.advisor_rationale = "Upstream alignment."
        planner.nodes = {node.sha: node}
        planner.ordered_missing = lambda: [node.sha]

        plan_before = planner.report_json()
        worksheet = planner.report_text()
        plan_after = planner.report_json()
        self.assertEqual(plan_before, plan_after)
        self.assertNotIn("worksheet-format:", worksheet)
        self.assertEqual(
            json.loads(plan_after)["commits"][node.sha]["advisor_decision"],
            "TAKE",
        )

    def test_worksheet_schema_has_no_legacy_cli_option(self):
        args = kbp.parse_args(["--base", "v6.19", "--target", "v7.0"])
        self.assertFalse(hasattr(args, "worksheet_format"))
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                kbp.parse_args(["--base", "v6.19", "--target", "v7.0",
                                "--worksheet-format", "legacy"])
        self.assertEqual(caught.exception.code, 2)

    def test_worksheet_wraps_advisor_commentary_without_changing_content(self):
        planner = self.minimal_planner()
        node = kbp.CommitNode("a" * 40, "arm64: updated device tree", direct=True)
        node.advisor_decision = "TAKE"
        rationale = (
            "The commit changes several ARM64 device tree nodes while preserving "
            "downstream kernel interfaces. " * 3
        )
        node.advisor_rationale = "  " + rationale.replace(" while ", "\nwhile ")
        reason = "The DTS consumer needs this provider to avoid an unresolved reference. " * 3
        peer = "b" * 40
        node.advisor_prerequisites = [{
            "sha": peer, "evidence_id": "ev-123", "evidence_kind": "dts-label",
            "evidence_object": "uart18", "reason": reason,
        }]
        node.advisor_binding_assessments = [{
            "evidence_id": "ev-234", "disposition": "REQUIRED", "reason": reason,
        }]
        node.advisor_rejected_prerequisites = [{
            "sha": peer, "evidence_id": "ev-345", "assessment_reason": reason,
        }]
        node.advisor_recommended_by = [{
            "consumer_sha": "c" * 40,
            "consumer_subject": "arm64: dts: qcom: Add another long board description",
            "consumer_decision": "TAKE", "evidence_id": "ev-456",
            "evidence_kind": "dts-label", "evidence_object": "uart18",
        }]
        node.advisor_inbound_assessments = [{
            "evidence_id": "ev-567", "disposition": "REQUIRED", "reason": reason,
        }]
        node.advisor_consistency_warnings = [{
            "kind": "TAKE->MISSING_DT_INCLUDE", "include_path": "arch/arm64/board.dtsi",
            "reason": reason,
        }]
        node.advisor_risks = [reason]
        node.advisor_error = "Advisor returned a detailed diagnostic. " * 4
        node.advisor_error_kind = "invalid-response"
        planner.nodes = {node.sha: node}
        planner.ordered_missing = lambda: [node.sha]
        planner.subject = lambda _sha: "arm64: dts: qcom: Add long provider board title"

        report = kbp.Planner.report_text(planner)
        lines = report.splitlines()
        labels = [
            "advisor-rationale", "advisor-prerequisite", "advisor-binding-assessment",
            "advisor-rejected-prerequisite", "advisor-recommended-by",
            "advisor-inbound-assessment", "advisor-consistency-warning",
            "advisor-risk", "advisor-error",
        ]
        for label in labels:
            with self.subTest(label=label):
                prefix = f"      {label}: "
                indexes = [i for i, line in enumerate(lines) if line.startswith(prefix)]
                self.assertEqual(len(indexes), 1)
                index = indexes[0]
                self.assertLessEqual(len(lines[index]), 80)
                continuation = []
                for line in lines[index + 1:]:
                    if not line.startswith("          "):
                        break
                    continuation.append(line)
                self.assertTrue(continuation, f"{label} did not wrap")
                self.assertTrue(all(len(line) <= 80 for line in continuation))
                self.assertTrue(all(not line.strip().startswith("advisor-")
                                    for line in continuation))
        index = next(i for i, line in enumerate(lines)
                     if line.startswith("      advisor-rationale: "))
        content = [lines[index].split(": ", 1)[1]]
        for line in lines[index + 1:]:
            if not line.startswith("          "):
                break
            content.append(line.strip())
        self.assertEqual(" ".join(content), kbp.Planner._one_line(node.advisor_rationale))

    def test_worksheet_advisor_wrapping_preserves_short_and_long_tokens(self):
        short = kbp.Planner._worksheet_advisor_lines("advisor-rationale", "Short reason.")
        self.assertEqual(short, ["      advisor-rationale: Short reason."])
        token = "long/" + "x" * 110
        long_lines = kbp.Planner._worksheet_advisor_lines("advisor-risk", token)
        self.assertIn(token, " ".join(long_lines))
        self.assertEqual(kbp.Planner._worksheet_advisor_lines("advisor-risk", ""),
                         ["      advisor-risk:"])

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
            "binding_evidence_assessments": [
                {"evidence_id": "ev-one", "disposition": "REQUIRED",
                 "reason": "Board binding is needed."},
                {"evidence_id": "ev-two", "disposition": "REQUIRED",
                 "reason": "Property definition is needed."},
            ],
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
        planner.ordered_missing = lambda: [peer.sha, consumer.sha]

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
        self.assertIn("TRUST_DT_BINDING_NAMESPACE_PROOF", prompt)
        self.assertIn("Do NOT choose REVIEW or SKIP merely because", prompt)
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
        self.assertFalse(packet["commit"]["dependency_policy"]["dt_binding_namespace_verified"])
        self.assertEqual(
            packet["commit"]["default_policy_bias"],
            "REQUIRE_SEMANTIC_PROOF_FOR_DT_BINDING_SYMBOL",
        )

    def test_verified_mt8188_binding_symbol_trusts_namespace_proof_despite_truncated_excerpt(self):
        planner = self.minimal_planner()
        provider = kbp.CommitNode(
            "a" * 40,
            "dt-bindings: clock: mediatek,mt8188: Add VDO1_DPI1_HDMI clock",
        )
        provider.files = ["include/dt-bindings/clock/mediatek,mt8188-clk.h"]
        consumer = kbp.CommitNode(
            "b" * 40,
            "arm64: dts: mediatek: mt8188: Add DPI1, HDMI, HDMI PHY/DDC nodes",
            direct=True,
        )
        consumer.files = ["arch/arm64/boot/dts/mediatek/mt8188.dtsi"]
        consumer.advisor_decision = "TAKE"
        provider.add_reason("symbol:CLK_VDO1_DPI1_HDMI", consumer.sha)
        provider.add_symbol_dependency_proof({
            "symbol": "CLK_VDO1_DPI1_HDMI",
            "provider_path": "include/dt-bindings/clock/mediatek,mt8188-clk.h",
            "consumer_use_paths": ["arch/arm64/boot/dts/mediatek/mt8188.dtsi"],
            "verification": "dt-include-closure",
            "consumer_sha": consumer.sha,
        })
        planner.nodes = {provider.sha: provider, consumer.sha: consumer}
        planner.subject = lambda sha: planner.nodes[sha].subject
        planner.changed_files = lambda sha: planner.nodes[sha].files
        planner.body = lambda sha: planner.nodes[sha].subject
        planner.patch_excerpt = lambda sha, _limit: (
            "+#define CLK_VDO1_DPI1_HDMI 66\n"
            if sha == provider.sha
            else "+hdmi0: hdmi@14000000 { /* excerpt truncates clock line */ };\n"
        )

        packet = planner.advisor_packet(provider.sha)
        policy = packet["commit"]["dependency_policy"]
        self.assertTrue(policy["dt_binding_namespace_verified"])
        self.assertEqual(
            packet["commit"]["default_policy_bias"],
            "TRUST_DT_BINDING_NAMESPACE_PROOF",
        )
        self.assertEqual(policy["dt_binding_namespace_proofs"][0]["consumer_sha"], consumer.sha)
        self.assertIn("truncated advisor patch excerpt", policy["note"])

        planner.ordered_missing = lambda: [provider.sha]
        report = kbp.Planner.report_text(planner)
        self.assertIn("symbol-namespace-proof: CLK_VDO1_DPI1_HDMI", report)
        self.assertIn("verification=dt-include-closure", report)

    def test_verified_namespace_excerpt_only_objection_overridden(self):
        planner = self.minimal_planner()
        provider = kbp.CommitNode("a" * 40, "dt-bindings: clock: qcom: Add GCC clocks for QCS8300")
        provider.files = ["include/dt-bindings/clock/qcom,qcs8300-gcc.h", "Documentation/devicetree/bindings/clock/qcom,qcs8300-gcc.yaml"]
        provider.add_reason("symbol:GCC_PCIE_1_BCR", "b" * 40)
        provider.add_symbol_dependency_proof({
            "symbol": "GCC_PCIE_1_BCR",
            "provider_path": provider.files[0],
            "consumer_sha": "b" * 40,
            "consumer_use_paths": ["arch/arm64/boot/dts/qcom/qcs8300.dtsi"],
            "verification": "dt-include-closure",
        })
        planner.nodes = {provider.sha: provider}
        planner.subject = lambda sha: provider.subject
        planner.changed_files = lambda sha: provider.files
        planner.body = lambda sha: provider.subject
        planner.patch_excerpt = lambda sha, limit: ""
        planner.advisor_allowed_prerequisite_peers = lambda sha: []
        planner.advisor_prerequisite_evidence = lambda sha: []
        planner.advisor_inbound_take_recommendations = lambda sha: []
        planner.advisor_scope_context = lambda files: {"unsupported_architecture_only": False, "touches_focus_paths": False}
        planner.advisor_obsolescence_context = lambda sha, files: {"all_changed_files_deleted_by_take_consumers": False}
        planner.advisor_product_context = lambda: {}
        planner.info = lambda *args: None
        self.assertEqual(planner.advisor_packet(provider.sha)["commit"]["default_policy_bias"], "TRUST_DT_BINDING_NAMESPACE_PROOF")
        result = {
            "decision": "REVIEW",
            "rationale": (
                "The bounded consumer patch excerpt omits the symbol-use line. "
                "Consequently, the DT-binding namespace cannot be verified from that excerpt."
            ),
            "risks": ["The truncated consumer patch excerpt does not show the header-include line."],
            "prerequisite_peers": [],
            "inbound_prerequisite_assessments": [],
        }
        revised = planner._enforce_verified_dt_namespace(provider, result)
        self.assertEqual(revised["decision"], "TAKE")
        self.assertEqual(revised["risks"], [])
        self.assertIn("Original advisor rationale", revised["rationale"])
        self.assertEqual(result["decision"], "REVIEW")

    def test_namespace_guardrail_does_not_override_qcs8300_compatible_objection(self):
        # The real 0.4.10 QCS8300 objection asked for the *compatible* value.
        # Include closure proves macro use and header reachability, not that
        # the DTS node's compatible value is correct.
        planner = self.minimal_planner()
        planner.advisor_packet = lambda sha: {"commit": {
            "default_policy_bias": "TRUST_DT_BINDING_NAMESPACE_PROOF"}}
        node = kbp.CommitNode("a" * 40, "dt-bindings: clock: qcom: Add GCC clocks for QCS8300")
        result = {
            "decision": "REVIEW",
            "rationale": (
                "The provided evidence does not include the 'compatible' property for the gcc node. "
                "Consequently, it cannot be verified that the consumer uses the specific "
                "qcom,qcs8300-gcc binding rather than a legacy namespace."
            ),
            "risks": [
                "The lack of explicit evidence linking the consumer's gcc node to the "
                "qcom,qcs8300-gcc compatible string prevents verification of the binding namespace."
            ],
        }
        self.assertIs(planner._enforce_verified_dt_namespace(node, result), result)

    def test_namespace_guardrail_rejects_independent_objections(self):
        planner = self.minimal_planner()
        planner.advisor_packet = lambda sha: {"commit": {
            "default_policy_bias": "TRUST_DT_BINDING_NAMESPACE_PROOF"}}
        node = kbp.CommitNode("a" * 40, "binding")
        excerpt_only = "The bounded consumer patch excerpt omits the symbol-use line."
        cases = [
            ("The compatible string is wrong for the target controller, and the driver lacks support for it.", []),
            (excerpt_only + " The compatible string is wrong for the target controller.", []),
            (excerpt_only + " The downstream driver lacks support for this hardware.", []),
            (excerpt_only + " The downstream target file is missing.", []),
            (excerpt_only + " A required downstream header is absent.", []),
            (excerpt_only + " The integration breaks the API.", []),
            (excerpt_only + " The patch fails to compile.", []),
            (excerpt_only + " A separate architecture exclusion applies.", []),
            (excerpt_only + " The compatible property is invalid.", []),
            ("The truncated consumer patch excerpt omits the compatible property.", []),
            ("The namespace cannot be verified from that excerpt.", []),
            (excerpt_only, ["The downstream file is missing."]),
            (excerpt_only, ["The compatible value does not match the hardware."]),
            (excerpt_only, ["The patch cannot apply downstream."]),
            (excerpt_only, ["The bounded consumer patch excerpt omits the symbol-use line. "
                            "There is also a device regression."]),
        ]
        for decision in ("REVIEW", "SKIP"):
            for rationale, risks in cases:
                with self.subTest(decision=decision, rationale=rationale, risks=risks):
                    result = {"decision": decision, "rationale": rationale, "risks": risks}
                    self.assertIs(planner._enforce_verified_dt_namespace(node, result), result)

    def test_namespace_guardrail_allows_only_explicit_excerpt_objections(self):
        planner = self.minimal_planner()
        planner.advisor_packet = lambda sha: {"commit": {
            "default_policy_bias": "TRUST_DT_BINDING_NAMESPACE_PROOF"}}
        planner.info = lambda *args: None
        node = kbp.CommitNode("a" * 40, "binding")
        cases = [
            "The bounded consumer patch excerpt does not show the symbol-use line.",
            "The truncated consumer diff excerpt is truncated before the macro-use line.",
            "The consumer patch excerpt lacks the exact provider header include line.",
            "The consumer patch excerpt omits the symbol-use line. "
            "Therefore, the binding namespace cannot be verified from this excerpt.",
        ]
        for decision in ("REVIEW", "SKIP"):
            for rationale in cases:
                with self.subTest(decision=decision, rationale=rationale):
                    result = {"decision": decision, "rationale": rationale, "risks": [],
                              "prerequisite_peers": [], "inbound_prerequisite_assessments": []}
                    revised = planner._enforce_verified_dt_namespace(node, result)
                    self.assertEqual(revised["decision"], "TAKE")
                    self.assertIn(rationale, revised["rationale"])
                    self.assertEqual(result["decision"], decision)

    def test_namespace_guardrail_preserves_independent_review(self):
        planner = self.minimal_planner()
        planner.advisor_packet = lambda sha: {"commit": {
            "default_policy_bias": "TRUST_DT_BINDING_NAMESPACE_PROOF"}}
        planner.info = lambda *args: None
        node = kbp.CommitNode("a" * 40, "binding")
        cases = [
            ("The namespace cannot be verified, and there is an API incompatibility.", []),
            ("The namespace cannot be verified.", ["Possible build failure due to changed ABI"]),
            ("Build error from unresolved dependencies", []),
        ]
        for rationale, risks in cases:
            with self.subTest(rationale=rationale):
                result = {"decision": "REVIEW", "rationale": rationale, "risks": risks}
                self.assertIs(planner._enforce_verified_dt_namespace(node, result), result)
        planner.advisor_packet = lambda sha: {"commit": {
            "default_policy_bias": "REQUIRE_SEMANTIC_PROOF_FOR_DT_BINDING_SYMBOL"}}
        result = {"decision": "REVIEW", "rationale": "The namespace cannot be verified.", "risks": []}
        self.assertIs(planner._enforce_verified_dt_namespace(node, result), result)

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

        details = planner.definition_dependency_details(
            selected, {symbol}, {symbol: {use_path}}
        )
        self.assertEqual(details[symbol]["dependency_sha"], provider)
        self.assertEqual(details[symbol]["provider_path"], provider_path)
        self.assertEqual(details[symbol]["namespace_proof"], {
            "symbol": symbol,
            "provider_path": provider_path,
            "consumer_use_paths": [use_path],
            "verification": "dt-include-closure",
        })
        self.assertEqual(
            planner.definition_dependencies(selected, {symbol}, {symbol: {use_path}}),
            {symbol: provider},
        )

    def test_static_dependency_records_dt_binding_namespace_proof_on_provider(self):
        planner = self.minimal_planner()
        planner.args.context_deps = False
        planner.args.symbol_deps = True
        consumer = kbp.CommitNode("b" * 40, "arm64: dts: add HDMI", direct=True)
        provider = kbp.CommitNode("a" * 40, "dt-bindings: clock: add HDMI clock")
        symbol = "CLK_VDO1_DPI1_HDMI"
        use_path = "arch/arm64/boot/dts/mediatek/mt8188.dtsi"
        provider_path = "include/dt-bindings/clock/mediatek,mt8188-clk.h"
        planner.nodes = {consumer.sha: consumer, provider.sha: provider}
        planner.is_pre_base_dependency = lambda _node: False
        planner.git = types.SimpleNamespace(rev_parse=lambda sha: sha)
        planner.extract_symbol_uses = lambda _sha: {symbol: {use_path}}
        planner.definition_dependency_details = lambda _sha, _symbols, _uses: {
            symbol: {
                "dependency_sha": provider.sha,
                "provider_path": provider_path,
                "namespace_proof": {
                    "symbol": symbol,
                    "provider_path": provider_path,
                    "consumer_use_paths": [use_path],
                    "verification": "dt-include-closure",
                },
            }
        }

        planner.add_static_dependencies([consumer])
        self.assertIn(f"symbol:{symbol}", provider.reasons)
        self.assertIn(consumer.sha, provider.required_by)
        self.assertEqual(provider.symbol_dependency_proofs, [{
            "symbol": symbol,
            "provider_path": provider_path,
            "consumer_use_paths": [use_path],
            "verification": "dt-include-closure",
            "consumer_sha": consumer.sha,
        }])

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
        planner.ordered_missing = lambda: [peer.sha, consumer.sha]

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
        self.assertIn("BACKPORT WORKSHEET (oldest first)", report)
        self.assertIn("advisor-rejected-prerequisite:", report)
        self.assertIn("NOT_REQUIRED", report)
        # Human-readable advisor annotations may wrap between words.
        self.assertIn("Unsupported architecture-local relationship", " ".join(report.split()))

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


class DualReportOutputTests(unittest.TestCase):
    """Exercise the CLI write path without running Git or the Ollama advisor."""

    def fake_planner(self):
        planner = mock.Mock()
        planner.nodes = {}
        planner.run_check_command.return_value = (0, "check passed")
        planner.report_text.return_value = "text plan\n"
        planner.report_json.return_value = json.dumps({
            "order": ["a" * 40, "b" * 40],
            "commits": {"a" * 40: {"advisor_decision": "TAKE"}},
            "check": {"returncode": 0, "output": "check passed"},
        }) + "\n"
        return planner

    def run_mocked(self, argv, planner):
        with mock.patch.object(kbp, "Planner", return_value=planner):
            with mock.patch.object(kbp.sys, "stdout", new_callable=io.StringIO) as stdout:
                result = kbp.main(["--base", "v6.19", "--target", "v7.0", *argv])
                return result, stdout.getvalue()

    def test_dual_output_runs_analysis_once_and_shares_check_result(self):
        with tempfile.TemporaryDirectory() as temp:
            text_path = Path(temp) / "plan.txt"
            json_path = Path(temp) / "plan.json"
            planner = self.fake_planner()
            rc, stdout = self.run_mocked(
                ["--worksheet-output", str(text_path), "--json-output", str(json_path)], planner
            )
            self.assertEqual(rc, 0)
            self.assertIn("Results archive:", stdout)
            self.assertEqual(text_path.read_text(), "text plan\n")
            self.assertEqual(json.loads(json_path.read_text())["order"], ["a" * 40, "b" * 40])
            planner.collect.assert_called_once_with()
            planner.run_advisor.assert_called_once_with()
            planner.run_check_command.assert_called_once_with()
            planner.report_text.assert_called_once_with((0, "check passed"))
            planner.report_json.assert_called_once_with((0, "check passed"))
            self.assertEqual(
                [call[0] for call in planner.method_calls],
                ["collect", "run_advisor", "run_check_command", "report_text", "report_json"],
            )

    def test_primary_stdout_still_works_with_json_output(self):
        with tempfile.TemporaryDirectory() as temp:
            json_path = Path(temp) / "plan.json"
            planner = self.fake_planner()
            rc, stdout = self.run_mocked(["--json-output", str(json_path)], planner)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "text plan\n")
            self.assertEqual(json.loads(json_path.read_text())["order"], ["a" * 40, "b" * 40])

    def test_existing_json_only_output_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            json_path = Path(temp) / "plan.json"
            planner = self.fake_planner()
            rc, stdout = self.run_mocked(
                ["--format", "json", "--worksheet-output", str(json_path)], planner
            )
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "")
            self.assertEqual(json.loads(json_path.read_text())["order"], ["a" * 40, "b" * 40])
            planner.report_text.assert_not_called()
            planner.report_json.assert_called_once_with((0, "check passed"))

    def test_json_primary_with_second_json_output_renders_once(self):
        with tempfile.TemporaryDirectory() as temp:
            primary = Path(temp) / "primary.json"
            secondary = Path(temp) / "secondary.json"
            planner = self.fake_planner()
            rc, _ = self.run_mocked(
                ["--format", "json", "--worksheet-output", str(primary), "--json-output", str(secondary)],
                planner,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(primary.read_bytes(), secondary.read_bytes())
            planner.report_json.assert_called_once()
            planner.report_text.assert_not_called()

    def test_same_output_path_is_rejected_before_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            output_path = Path(temp) / "plan.txt"
            planner = self.fake_planner()
            with mock.patch.object(kbp, "Planner", return_value=planner):
                with mock.patch.object(kbp.sys, "stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as raised:
                        kbp.main(["--base", "v6.19", "--target", "v7.0",
                                  "--worksheet-output", str(output_path),
                                  "--json-output", str(Path(temp) / "." / "plan.txt")])
            self.assertEqual(raised.exception.code, 2)
            planner.collect.assert_not_called()
            self.assertFalse(output_path.exists())

    def test_conflicts_still_return_two_after_writing_both_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            text_path = Path(temp) / "plan.txt"
            json_path = Path(temp) / "plan.json"
            planner = self.fake_planner()
            node = kbp.CommitNode("a" * 40, "conflicting backport", replay="conflict")
            planner.nodes = {node.sha: node}
            rc, _ = self.run_mocked(
                ["--worksheet-output", str(text_path), "--json-output", str(json_path)], planner
            )
            self.assertEqual(rc, 2)
            self.assertTrue(text_path.exists())
            self.assertEqual(json.loads(json_path.read_text())["order"][0], node.sha)


class AutoPackagingTests(unittest.TestCase):
    """Automatic packaging must close the log before creating the ZIP."""

    def planner(self, *, error=False, conflict=False):
        planner = mock.Mock()
        node = kbp.CommitNode("a" * 40, "patch", replay="conflict" if conflict else "clean")
        planner.nodes = {node.sha: node}
        planner.collect.side_effect = lambda: print("progress: collecting upstream", file=sys.stderr)
        planner.run_advisor.side_effect = lambda: print("progress: advisor finished")
        planner.run_check_command.return_value = ((5, "check failed") if error else None)
        planner.report_text.return_value = "text plan\n"
        planner.report_json.return_value = json.dumps({"advisor": None, "order": [node.sha]}) + "\n"
        return planner

    def test_auto_creates_complete_log_directory_zip_in_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "backport_plan_19"
            planner = self.planner()
            with mock.patch.object(kbp, "Planner", return_value=planner):
                self.assertEqual(kbp.main([
                    "--base", "v6.19", "--target", "v7.0",
                    "--worksheet-output", str(kbp.worksheet_path_for_plan(prefix)),
                    "--json-output", str(prefix) + ".json",
                ]), 0)
            folder = prefix
            archive = prefix.with_suffix(".zip")
            self.assertTrue(archive.is_file())
            self.assertEqual(sorted(f.name for f in folder.iterdir()), sorted([
                "backport_worksheet_19.txt", "backport_plan_19.json", "backport_plan_19.log",
                "backport_plan_19.perf.json", "backport_plan_19.perf.jsonl",
            ]))
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(sorted(zf.namelist()), sorted([
                    "backport_plan_19/backport_worksheet_19.txt",
                    "backport_plan_19/backport_plan_19.json",
                    "backport_plan_19/backport_plan_19.log",
                    "backport_plan_19/backport_plan_19.perf.json",
                    "backport_plan_19/backport_plan_19.perf.jsonl",
                ]))
                log = zf.read("backport_plan_19/backport_plan_19.log").decode()
                self.assertIn("progress: collecting upstream", log)
                self.assertIn("progress: advisor finished", log)
                self.assertIn(kbp.VERSION, log)
                self.assertEqual(log, prefix.with_suffix(".log").read_text())

    def test_package_preserves_analysis_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for suffix, planner, expected in (
                ("conflict", self.planner(conflict=True), 2),
                ("check", self.planner(error=True), 3),
            ):
                prefix = Path(tmp) / suffix
                with mock.patch.object(kbp, "Planner", return_value=planner):
                    rc = kbp.main([
                        "--base", "v6.19", "--target", "v7.0",
                        "--worksheet-output", str(kbp.worksheet_path_for_plan(prefix)),
                        "--json-output", str(prefix) + ".json",
                    ])
                self.assertEqual(rc, expected)
                self.assertTrue(prefix.with_suffix(".zip").is_file())

    def test_preflight_refuses_overwrite_without_running_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "plan"
            prefix.with_suffix(".zip").write_text("do not overwrite")
            planner = self.planner()
            with mock.patch.object(kbp, "Planner", return_value=planner):
                rc = kbp.main([
                    "--base", "v6.19", "--target", "v7.0",
                    "--worksheet-output", str(kbp.worksheet_path_for_plan(prefix)),
                    "--json-output", str(prefix) + ".json",
                ])
            self.assertEqual(rc, 1)
            planner.collect.assert_not_called()
            self.assertEqual(prefix.with_suffix(".zip").read_text(), "do not overwrite")
            self.assertFalse(prefix.with_suffix(".log").exists())

    def test_preflight_protects_existing_source_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "plan"
            kbp.worksheet_path_for_plan(prefix).write_text("original report\n")
            planner = self.planner()
            with mock.patch.object(kbp, "Planner", return_value=planner):
                rc = kbp.main([
                    "--base", "v6.19", "--target", "v7.0",
                    "--worksheet-output", str(kbp.worksheet_path_for_plan(prefix)),
                    "--json-output", str(prefix) + ".json",
                ])
            self.assertEqual(rc, 1)
            self.assertEqual(kbp.worksheet_path_for_plan(prefix).read_text(),
                             "original report\n")
            planner.collect.assert_not_called()
            self.assertFalse(prefix.with_suffix(".log").exists())

    def test_inconsistent_basename_fails_before_expensive_analysis(self):
        with self.assertRaises(SystemExit) as raised:
            kbp.parse_args([
                "--base", "v6.19", "--target", "v7.0",
                "--worksheet-output", "plan_18.txt", "--json-output", "plan_19.json",
            ])
        self.assertEqual(raised.exception.code, 2)

    def test_disable_auto_packaging_restores_independent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            text = path / "plan_18.txt"
            js = path / "plan_19.json"
            planner = self.planner()
            with mock.patch.object(kbp, "Planner", return_value=planner):
                rc = kbp.main([
                    "--base", "v6.19", "--target", "v7.0",
                    "--worksheet-output", str(text), "--json-output", str(js),
                    "--no-auto-package",
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(text.is_file())
            self.assertTrue(js.is_file())
            self.assertFalse(text.with_suffix(".log").exists())
            self.assertFalse(text.with_suffix(".zip").exists())


class DependencyClosure0414Tests(unittest.TestCase):
    """Exercise 0.4.14 executable-plan closure, not just advisor labels."""

    @staticmethod
    def edge(peer):
        return {"sha": peer, "evidence_id": "ev-binding",
                "evidence_kind": "compatible-binding", "evidence_object": "armsom,sige5",
                "reason": "uses compatible armsom,sige5"}

    @staticmethod
    def graph():
        planner = object.__new__(kbp.Planner)
        consumer = kbp.CommitNode("c" * 40, "ArmSoM Sige 5 board", direct=True)
        consumer.advisor_decision = "TAKE"
        provider = kbp.CommitNode("b" * 40, "ArmSoM Sige 5 binding")
        consumer.advisor_prerequisites = [DependencyClosure0414Tests.edge(provider.sha)]
        planner.nodes = {consumer.sha: consumer}
        planner.ordered_missing = lambda: [
            sha for sha in (provider.sha, consumer.sha)
            if sha in planner.nodes and planner.is_actionable_node(planner.nodes[sha])
        ]
        return planner, consumer, provider

    def test_missing_peer_is_not_silently_consistent(self):
        planner, consumer, provider = self.graph()
        planner._advisor_check_consistency()
        self.assertEqual([x["kind"] for x in planner.advisor_consistency_issues],
                         ["TAKE->ABSENT"])
        self.assertEqual(planner.advisor_consistency_issues[0]["prerequisite_sha"], provider.sha)
        self.assertEqual(len(consumer.advisor_consistency_warnings), 1)

    def test_candidate_and_unevaluated_peer_are_blockers(self):
        planner, consumer, provider = self.graph()
        provider.add_reason("hunk-context:1", consumer.sha, tentative=True)
        planner.nodes[provider.sha] = provider
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues[0]["kind"], "TAKE->UNSELECTED")
        provider.add_reason("advisor-prerequisite:compatible-binding", consumer.sha)
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues[0]["kind"], "TAKE->UNEVALUATED")

    def test_take_peer_must_precede_consumer(self):
        planner, consumer, provider = self.graph()
        provider.add_reason("advisor-prerequisite:compatible-binding", consumer.sha)
        provider.advisor_decision = "TAKE"
        planner.nodes[provider.sha] = provider
        planner.ordered_missing = lambda: [consumer.sha, provider.sha]
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues[0]["kind"], "TAKE->OUT_OF_ORDER")
        planner.ordered_missing = lambda: [provider.sha, consumer.sha]
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues, [])

    def test_present_peer_does_not_block(self):
        planner, consumer, provider = self.graph()
        provider.match = kbp.Match(status="present")
        planner.nodes[provider.sha] = provider
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues, [])

    def test_promotes_verified_binding_without_force_take(self):
        planner, consumer, provider = self.graph()
        planner.advisor_prerequisite_evidence = lambda sha: [{
            "evidence_id": "ev-binding", "peer_sha": provider.sha,
            "kind": "compatible-binding", "object": "armsom,sige5",
            "selected_use": "board compatible", "peer_provides": "binding",
        }]
        planner.advisor_allowed_prerequisite_peers = lambda sha: [provider.sha]
        planner.info = lambda msg: None
        planner._record_advisor_prerequisite_recommendations = lambda sha: None
        # Stand-in ensure_node must perform the actionability transition.
        def ensure(sha, reason, required_by):
            provider.add_reason(reason, required_by)
            planner.nodes[sha] = provider
            return provider
        planner.ensure_node = ensure
        count = planner._advisor_promote_prerequisites()
        self.assertEqual(count, 1)
        self.assertTrue(kbp.Planner.is_actionable_node(provider))
        self.assertIsNone(provider.advisor_decision)
        self.assertEqual(planner._advisor_promote_prerequisites(), 0)

    def test_no_promotion_from_unverified_evidence(self):
        planner, consumer, provider = self.graph()
        planner.advisor_prerequisite_evidence = lambda sha: []
        planner.advisor_allowed_prerequisite_peers = lambda sha: [provider.sha]
        planner.ensure_node = lambda *args, **kwargs: self.fail("unverified promotion")
        planner._record_advisor_prerequisite_recommendations = lambda sha: None
        self.assertEqual(planner._advisor_promote_prerequisites(), 0)

    def test_run_advisor_evaluates_promoted_peer(self):
        planner, consumer, provider = self.graph()
        planner.advisor_model = "dummy"
        planner.args = types.SimpleNamespace(
            advisor_commit=None, advisor_limit=None, advisor_base_url="http://localhost",
            advisor_timeout=1, advisor_temperature=0, advisor_keep_alive="1m",
            debug=False, simulate=False,
        )
        planner.info = lambda msg: None
        planner.advisor_reconsiderations = 0
        planner.advisor_order = lambda shas: list(shas)
        planner.advisor_system_prompt = lambda: "prompt"
        planner.advisor_prerequisite_evidence = lambda sha: ([{
            "evidence_id": "ev-binding", "peer_sha": provider.sha,
            "kind": "compatible-binding", "object": "armsom,sige5",
            "selected_use": "board compatible", "peer_provides": "schema",
        }] if sha == consumer.sha else [])
        planner.advisor_allowed_prerequisite_peers = lambda sha: [provider.sha]
        planner.ensure_node = lambda sha, reason, required_by: provider
        def ensure(sha, reason, required_by):
            provider.add_reason(reason, required_by)
            planner.nodes[sha] = provider
            return provider
        planner.ensure_node = ensure
        planner._record_advisor_prerequisite_recommendations = lambda sha: None
        planner.dependency_closure = lambda: None
        planner.build_series_index = lambda: None
        planner._advisor_reconciliation_pass = lambda *args: None
        seen = []
        def evaluate(_client, _prompt, sha):
            seen.append(sha)
            planner.nodes[sha].advisor_decision = "TAKE"
            if sha == consumer.sha:
                planner.nodes[sha].advisor_binding_assessments = [
                    {"evidence_id": "ev-binding", "disposition": "REQUIRED",
                     "reason": "Board integration requires schema."}]
        planner._advisor_evaluate_one = evaluate
        with mock.patch.object(kbp, "OllamaAdvisor"):
            planner.run_advisor()
        self.assertEqual(seen, [consumer.sha, provider.sha])
        self.assertTrue(provider.required_by)
        self.assertEqual(planner.advisor_consistency_issues, [])

    def test_missing_dts_provider_is_hard_dependency(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, text=True,
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
            def commit(message):
                git("add", ".")
                git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", message)
                return git("rev-parse", "HEAD")
            git("init", "-q")
            (root / "README").write_text("baseline\n")
            baseline = commit("base")
            dts = root / "arch/arm64/boot/dts/rockchip"
            dts.mkdir(parents=True)
            (dts / "rk3576.dtsi").write_text("/ { soc { }; };\n")
            provider_sha = commit("arm64: dts: rockchip: Add rk3576 SoC base DT")
            (dts / "rk3576-armsom-sige5.dts").write_text(
                '/dts-v1/;\n#include "rk3576.dtsi"\n/ { model = "Sige5"; };\n')
            consumer_sha = commit("arm64: dts: rockchip: Add rk3576-armsom-sige5 board")
            planner = object.__new__(kbp.Planner)
            planner.git = kbp.Git(root)
            planner.replay_start = baseline
            consumer = kbp.CommitNode(consumer_sha, "board", direct=True,
                files=["arch/arm64/boot/dts/rockchip/rk3576-armsom-sige5.dts"])
            provider = kbp.CommitNode(provider_sha, "base DTS",
                files=["arch/arm64/boot/dts/rockchip/rk3576.dtsi"])
            planner.nodes = {consumer_sha: consumer}
            def ensure(sha, reason, required_by):
                self.assertEqual(sha, provider_sha)
                provider.add_reason(reason, required_by)
                planner.nodes[sha] = provider
                return provider
            planner.ensure_node = ensure
            self.assertEqual(planner.add_dt_include_dependencies([consumer]), 1)
            self.assertEqual(provider.required_by, {consumer_sha})
            self.assertIn("dt-include:arch/arm64/boot/dts/rockchip/rk3576.dtsi", provider.reasons)
            self.assertEqual(planner.add_dt_include_dependencies([consumer]), 0)
            consumer.advisor_decision = "TAKE"
            provider.advisor_decision = "SKIP"
            planner.ordered_missing = lambda: [provider_sha, consumer_sha]
            planner._advisor_check_consistency()
            self.assertEqual(planner.advisor_consistency_issues[0]["kind"], "DT_INCLUDE->SKIP")
            provider.advisor_decision = "TAKE"
            planner._advisor_check_consistency()
            self.assertEqual(planner.advisor_consistency_issues, [])


class DependencyClosure0415Tests(unittest.TestCase):
    """Regression coverage for false DT dependencies and stable reorder."""

    def git_repo(self, tmp):
        root = Path(tmp)
        def git(*args):
            return subprocess.run(["git", *args], cwd=root, text=True, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
        def commit(message):
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", message)
            return git("rev-parse", "HEAD")
        git("init", "-q")
        (root / "README").write_text("base\n")
        baseline = commit("base")
        return root, git, commit, baseline

    def test_same_commit_introduces_dts_and_dtsi_without_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, git, commit, baseline = self.git_repo(tmp)
            folder = root / "arch/arm64/boot/dts/realtek"
            folder.mkdir(parents=True)
            dtsi = folder / "soc.dtsi"
            dts = folder / "board.dts"
            dtsi.write_text('/ { soc { }; };\n')
            dts.write_text('/dts-v1/;\n#include "soc.dtsi"\n')
            sha = commit("Add board with SoC DT")
            node = kbp.CommitNode(sha, "board", direct=True,
                 files=["arch/arm64/boot/dts/realtek/board.dts",
                        "arch/arm64/boot/dts/realtek/soc.dtsi"])
            planner = object.__new__(kbp.Planner)
            planner.git = kbp.Git(root)
            planner.replay_start = baseline
            planner.nodes = {sha: node}
            planner.ensure_node = lambda *a, **kw: self.fail("same-commit prerequisite")
            self.assertEqual(planner.add_dt_include_dependencies([node]), 0)
            self.assertEqual(planner.dt_include_unresolved, [])
            node.advisor_decision = "TAKE"
            planner.ordered_missing = lambda: [sha]
            planner._advisor_check_consistency()
            self.assertEqual(planner.advisor_consistency_issues, [])

    def test_file_modified_by_consumer_still_requires_older_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, git, commit, baseline = self.git_repo(tmp)
            folder = root / "arch/arm64/boot/dts/rockchip"
            folder.mkdir(parents=True)
            (folder / "soc.dtsi").write_text('/ { soc { }; };\n')
            provider_sha = commit("Add SoC DT")
            (folder / "soc.dtsi").write_text('/ { soc { serial { }; }; };\n')
            (folder / "board.dts").write_text('/dts-v1/;\n#include "soc.dtsi"\n')
            consumer_sha = commit("Add board and update SoC DT")
            consumer = kbp.CommitNode(consumer_sha, "board", direct=True,
                files=["arch/arm64/boot/dts/rockchip/board.dts",
                       "arch/arm64/boot/dts/rockchip/soc.dtsi"])
            provider = kbp.CommitNode(provider_sha, "SoC")
            planner = object.__new__(kbp.Planner)
            planner.git = kbp.Git(root)
            planner.replay_start = baseline
            planner.nodes = {consumer_sha: consumer}
            def ensure(sha, reason, required_by):
                self.assertEqual(sha, provider_sha)
                provider.add_reason(reason, required_by)
                planner.nodes[sha] = provider
                return provider
            planner.ensure_node = ensure
            self.assertEqual(planner.add_dt_include_dependencies([consumer]), 1)
            self.assertIn(consumer_sha, provider.required_by)
            self.assertEqual(planner.dt_include_unresolved, [])

    def test_binding_and_selected_ancestors_move_before_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, git, commit, baseline = self.git_repo(tmp)
            git("checkout", "-qb", "board")
            (root / "board").write_text("board\n")
            consumer_sha = commit("board")
            git("checkout", "-qb", "binding", baseline)
            (root / "binding-ancestor").write_text("ancestor\n")
            ancestor_sha = commit("binding ancestor")
            (root / "binding").write_text("binding\n")
            provider_sha = commit("binding")
            planner = object.__new__(kbp.Planner)
            planner.git = kbp.Git(root)
            consumer = kbp.CommitNode(consumer_sha, "board", direct=True)
            consumer.advisor_decision = "TAKE"
            consumer.advisor_prerequisites = [{"sha": provider_sha}]
            ancestor = kbp.CommitNode(ancestor_sha, "ancestor", direct=True)
            ancestor.advisor_decision = "TAKE"
            provider = kbp.CommitNode(provider_sha, "binding", direct=True)
            provider.advisor_decision = "TAKE"
            planner.nodes = {n.sha: n for n in (consumer, ancestor, provider)}
            self.assertEqual(planner._dependency_order(
                [consumer_sha, ancestor_sha, provider_sha]),
                [ancestor_sha, provider_sha, consumer_sha])

    def test_git_ancestry_prevents_impossible_reorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, git, commit, baseline = self.git_repo(tmp)
            (root / "board").write_text("board\n")
            consumer_sha = commit("board")
            (root / "binding").write_text("binding\n")
            provider_sha = commit("binding")
            planner = object.__new__(kbp.Planner)
            planner.git = kbp.Git(root)
            consumer = kbp.CommitNode(consumer_sha, "board", direct=True)
            consumer.advisor_decision = "TAKE"
            consumer.advisor_prerequisites = [{"sha": provider_sha}]
            provider = kbp.CommitNode(provider_sha, "binding", direct=True)
            provider.advisor_decision = "TAKE"
            planner.nodes = {n.sha: n for n in (consumer, provider)}
            self.assertEqual(planner._dependency_order([consumer_sha, provider_sha]),
                             [consumer_sha, provider_sha])
            planner.ordered_missing = lambda: planner._dependency_order(
                [consumer_sha, provider_sha])
            planner._advisor_check_consistency()
            self.assertEqual(planner.advisor_consistency_issues[0]["kind"],
                             "TAKE->OUT_OF_ORDER")

    def test_dt_source_dependency_also_reorders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, git, commit, baseline = self.git_repo(tmp)
            git("checkout", "-qb", "board")
            (root / "board").write_text("board\n")
            consumer_sha = commit("board")
            git("checkout", "-qb", "soc", baseline)
            (root / "soc").write_text("soc\n")
            provider_sha = commit("soc")
            planner = object.__new__(kbp.Planner)
            planner.git = kbp.Git(root)
            consumer = kbp.CommitNode(consumer_sha, "board", direct=True)
            consumer.advisor_decision = "TAKE"
            provider = kbp.CommitNode(provider_sha, "soc", direct=True)
            provider.advisor_decision = "TAKE"
            provider.add_reason("dt-include:arch/arm64/boot/dts/soc.dtsi", consumer_sha)
            planner.nodes = {n.sha: n for n in (consumer, provider)}
            self.assertEqual(planner._dependency_order([consumer_sha, provider_sha]),
                             [provider_sha, consumer_sha])


class Packaging0415Tests(unittest.TestCase):
    def fixtures(self, root):
        prefix = Path(root) / "backport_plan_19"
        kbp.worksheet_path_for_plan(prefix).write_text("text plan\n")
        prefix.with_suffix(".log").write_text("complete tee log\n")
        cache = Path(root) / "backport_plan_19.advisor-cache.json"
        cache.write_text('{"test": true}\n')
        diag = Path(root) / "backport_plan_19.advisor-errors.jsonl"
        diag.write_text('{"kind": "timeout"}\n')
        prefix.with_suffix(".json").write_text(json.dumps({"advisor": {
            "cache_path": str(cache), "error_log_path": str(diag)}}) + "\n")
        return prefix

    def test_package_finished_results_and_reject_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.fixtures(tmp)
            rc = kbp.main(["--package-results", str(kbp.worksheet_path_for_plan(prefix))])
            self.assertEqual(rc, 0)
            folder = prefix
            archive = prefix.with_suffix(".zip")
            self.assertTrue(folder.is_dir())
            self.assertEqual(len(list(folder.iterdir())), 5)
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(sorted(zf.namelist()), sorted(
                    f"{prefix.name}/{f.name}" for f in folder.iterdir()))
                self.assertEqual(zf.read(f"{prefix.name}/{prefix.name}.log"),
                                 b"complete tee log\n")
            self.assertEqual(kbp.main(["--package-results", str(prefix)]), 1)
            self.assertEqual((folder / (kbp.worksheet_path_for_plan(prefix).name)).read_text(), "text plan\n")

    def test_missing_log_prevents_incomplete_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.fixtures(tmp)
            prefix.with_suffix(".log").unlink()
            self.assertEqual(kbp.main(["--package-results", str(prefix)]), 1)
            self.assertFalse(prefix.exists())
            self.assertFalse(prefix.with_suffix(".zip").exists())

    def test_packaging_mode_does_not_require_base_target(self):
        self.assertEqual(kbp.parse_args(["--package-results", "plan"]).package_results,
                         "plan")
        with self.assertRaises(SystemExit) as raised:
            kbp.parse_args(["--package-results", "plan", "--base", "v6.19"])
        self.assertEqual(raised.exception.code, 2)


class Performance0416Tests(unittest.TestCase):
    """Performance measurements must be durable and avoid payload leakage."""

    def test_partial_run_journal_survives_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.perf.jsonl"
            profiler = kbp.PerformanceRecorder(path)
            with self.assertRaises(KeyboardInterrupt):
                try:
                    with profiler.phase("collect.replay"):
                        raise KeyboardInterrupt()
                finally:
                    profiler.abort()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r["event"] for r in records],
                             ["run_start", "phase_start", "phase_end"])
            self.assertEqual(records[-1]["name"], "collect.replay")

    def test_advisor_metrics_are_aggregated_without_prompt_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.perf.jsonl"
            profiler = kbp.PerformanceRecorder(path)
            profiler.advisor("a" * 40, 1, 1.25, input_bytes=12345,
                             ollama={"prompt_eval_count": 300,
                                     "eval_count": 100,
                                     "eval_duration": 500000000,
                                     "message": "secret kernel code"})
            profiler.advisor("b" * 40, 2, 2.0, status="failed")
            profiler.cache_hit("c" * 40)
            profiler.git("log", 0.25)
            profiler.git("log", 0.75)
            summary = profiler.finish(Path(tmp) / "plan.perf.json")
            self.assertEqual(summary["advisor"]["requests"], 2)
            self.assertEqual(summary["advisor"]["failed_attempts"], 1)
            self.assertEqual(summary["advisor"]["cache_hits"], 1)
            self.assertEqual(summary["advisor"]["ollama"]["prompt_eval_count"], 300)
            self.assertEqual(summary["git_commands"]["log"]["count"], 2)
            self.assertNotIn("secret kernel code", path.read_text())
            self.assertNotIn("secret kernel code", (Path(tmp) / "plan.perf.json").read_text())

    def test_ollama_client_exposes_server_duration_fields(self):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({
                    "message": {"content": json.dumps({"decision": "TAKE"})},
                    "total_duration": 2000000000, "load_duration": 100000000,
                    "prompt_eval_count": 900, "eval_count": 250,
                    "eval_duration": 1400000000,
                }).encode()
        client = kbp.OllamaAdvisor("gemma4-kernel:128k", "127.0.0.1:11434",
                                   3.0, 0.1, "10m")
        with mock.patch.object(kbp.urllib.request, "urlopen", return_value=FakeResponse()):
            self.assertEqual(client.advise("prompt", {"commit": {"sha": "a" * 40}}),
                             {"decision": "TAKE"})
        self.assertEqual(client.last_telemetry["total_duration"], 2000000000)
        self.assertEqual(client.last_telemetry["eval_count"], 250)

    def test_mocked_advisor_retries_record_attempts_without_sleep_time(self):
        planner = object.__new__(kbp.Planner)
        planner.args = types.SimpleNamespace(advisor_retries=1, advisor_retry_delay=0.0)
        node = kbp.CommitNode("a" * 40, "arm64 change")
        planner.nodes = {node.sha: node}
        planner.advisor_calls = 0
        planner.advisor_retries_used = 0
        planner.info = lambda message: None
        planner.advisor_prerequisite_evidence = lambda _sha: []
        planner.advisor_inbound_take_recommendations = lambda _sha: []
        planner.validate_advisor_result = lambda sha, result: result
        class Client:
            calls = 0
            def advise(self, prompt, packet, schema):
                self.calls += 1
                if self.calls == 1:
                    raise kbp.AdvisorError("failed", kind="timeout", retryable=True)
                return {"decision": "TAKE"}
        with tempfile.TemporaryDirectory() as tmp:
            profiler = kbp.PerformanceRecorder(Path(tmp) / "plan.perf.jsonl")
            planner.performance = profiler
            result = planner._advisor_call_with_retries(
                Client(), "prompt", node.sha, {"commit": {"sha": node.sha}})
            summary = profiler.finish()
        self.assertEqual(result["decision"], "TAKE")
        self.assertEqual(summary["advisor"]["requests"], 2)
        self.assertEqual(summary["advisor"]["failed_attempts"], 1)

    def test_auto_package_includes_performance_summary_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "plan"
            planner = mock.Mock()
            planner.nodes = {}
            planner.run_check_command.return_value = None
            planner.report_text.return_value = "text\n"
            planner.report_json.return_value = json.dumps({"advisor": None}) + "\n"
            with mock.patch.object(kbp, "Planner", return_value=planner):
                self.assertEqual(kbp.main([
                    "--base", "v6.19", "--target", "v7.0",
                    "--worksheet-output", str(kbp.worksheet_path_for_plan(prefix)),
                    "--json-output", str(prefix) + ".json",
                ]), 0)
            summary = json.loads(prefix.with_suffix(".perf.json").read_text())
            self.assertGreaterEqual(summary["phases"]["collect"]["count"], 1)
            with zipfile.ZipFile(prefix.with_suffix(".zip")) as archive:
                members = archive.namelist()
                self.assertIn("plan/plan.perf.json", members)
                self.assertIn("plan/plan.perf.jsonl", members)
                self.assertEqual(json.loads(archive.read("plan/plan.perf.json")), summary)

    def test_preflight_prevents_reuse_of_partial_perf_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "plan"
            prefix.with_suffix(".perf.jsonl").write_text('{"event":"run_start"}\n')
            with self.assertRaisesRegex(ValueError, "already exists"):
                kbp.ensure_package_destinations_available(prefix)

class AdvisorBudget0417Tests(unittest.TestCase):
    """Context capping must fail closed and retain dependency evidence."""

    @staticmethod
    def packet():
        return {
            "planning_context": {"product": "rhel10"},
            "commit": {
                "sha": "a" * 40, "subject": "board DTS",
                "patch_excerpt": "+" * 600,
                "message": "M" * 1000,
                "eligible_prerequisite_evidence": [{
                    "evidence_id": "ev-test", "peer_sha": "b" * 40,
                    "kind": "included-file", "object": "rk3576.dtsi",
                }],
                "actual_inbound_take_prerequisites": [{
                    "evidence_id": "ev-inbound", "consumer_sha": "c" * 40,
                }],
                "same_series": [{"sha": "b" * 40,
                                 "patch_excerpt": "P" * 3000}],
                "deterministic_consumer_context": {
                    "consumers": [{"sha": "c" * 40,
                                   "patch_excerpt": "Q" * 2500}]},
            },
        }

    def test_smaller_packet_is_unchanged(self):
        packet = self.packet()
        original = json.dumps(packet)
        output, meta = kbp.budget_advisor_packet(packet, "prompt", {}, 20000)
        self.assertIs(output, packet)
        self.assertEqual(meta["omitted"], [])
        self.assertEqual(json.dumps(packet), original)

    def test_removes_optional_context_not_required_prerequisites(self):
        packet = self.packet()
        original = json.dumps(packet)
        output, meta = kbp.budget_advisor_packet(packet, "prompt", {}, 5000)
        self.assertLessEqual(meta["final_chars"], 5000)
        self.assertTrue(meta["omitted"])
        self.assertEqual(output["commit"]["patch_excerpt"], "+" * 600)
        self.assertEqual(output["commit"]["eligible_prerequisite_evidence"],
                         packet["commit"]["eligible_prerequisite_evidence"])
        self.assertEqual(output["commit"]["actual_inbound_take_prerequisites"],
                         packet["commit"]["actual_inbound_take_prerequisites"])
        self.assertEqual(json.dumps(packet), original)

    def test_oversized_required_evidence_fails_closed(self):
        packet = self.packet()
        packet["commit"]["eligible_prerequisite_evidence"] = [
            {"evidence_id": "ev-test", "source": "X" * 12000}]
        with self.assertRaisesRegex(kbp.AdvisorError, "required advisor evidence") as result:
            kbp.budget_advisor_packet(packet, "prompt", {}, 2000)
        self.assertEqual(result.exception.kind, "evidence-budget")
        self.assertFalse(result.exception.retryable)

    def test_evidence_overflow_does_not_call_model_and_sets_review(self):
        planner = object.__new__(kbp.Planner)
        sha = "a" * 40
        node = kbp.CommitNode(sha, "DTS consumer")
        planner.nodes = {sha: node}
        planner.args = types.SimpleNamespace(advisor_max_request_chars=100,
                                              no_advisor_cache=True)
        planner.advisor_packet = lambda x: self.packet()
        planner.advisor_policy_override = lambda sha, packet: None
        planner.advisor_prerequisite_evidence = lambda x: []
        planner.advisor_inbound_take_recommendations = lambda x: []
        planner._advisor_note_failure = lambda n, error: (
            setattr(n, "advisor_decision", "REVIEW"),
            setattr(n, "advisor_error_kind", error.kind))
        planner.info = lambda msg: None
        class Client:
            def advise(self, *args):
                raise AssertionError("model should not be called")
        planner._advisor_evaluate_one(Client(), "prompt", sha)
        self.assertEqual(node.advisor_decision, "REVIEW")
        self.assertEqual(node.advisor_error_kind, "evidence-budget")
        self.assertTrue(node.advisor_budget["overflow"])

    def test_context_limit_rejects_response_that_might_be_truncated(self):
        planner = object.__new__(kbp.Planner)
        sha = "a" * 40
        planner.nodes = {sha: kbp.CommitNode(sha, "DTS")}
        planner.args = types.SimpleNamespace(advisor_retries=0,
                advisor_retry_delay=0, advisor_context_tokens=32768,
                advisor_output_reserve=1024)
        planner.advisor_calls = 0
        planner.advisor_prerequisite_evidence = lambda _sha: []
        planner.advisor_inbound_take_recommendations = lambda _sha: []
        planner.validate_advisor_result = lambda _sha, result: result
        planner._write_advisor_error_diagnostic = lambda *args: None
        class Client:
            last_telemetry = None
            def advise(self, *args):
                self.last_telemetry = {"prompt_eval_count": 32500}
                return {"decision": "TAKE"}
        with self.assertRaisesRegex(kbp.AdvisorError, "possible truncated") as caught:
            planner._advisor_call_with_retries(Client(), "prompt", sha, self.packet())
        self.assertEqual(caught.exception.kind, "context-limit")

    def test_benchmark_reads_saved_plan_without_full_planner_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sha = "a" * 40
            plan = tmp / "plan.json"
            plan.write_text(json.dumps({
                "base": "v6.19", "target": "v7.0", "downstream": "base",
                "paths": ["arch/arm64"], "order": [sha],
                "commits": {sha: {"subject": "board", "direct": True,
                                  "files": ["arch/arm64/boot/dts/test.dts"],
                                  "advisor_decision": "TAKE",
                                  "advisor_prerequisite_evidence": [],
                                  "advisor_prerequisites": []}},
            }))
            target = tmp / "results.jsonl"
            mock_result = {"decision": "TAKE", "rationale": "matches",
                           "prerequisite_evidence_ids": [],
                           "inbound_prerequisite_assessments": [], "risks": []}
            with mock.patch.object(kbp.Git, "run", return_value="kernel evidence"), \
                 mock.patch.object(kbp.OllamaAdvisor, "advise", return_value=mock_result), \
                 mock.patch.object(kbp.Planner, "__init__", side_effect=AssertionError(
                     "benchmark must not initialize planner")):
                rc = kbp.main(["--advisor-benchmark-plan", str(plan),
                    "--benchmark-output", str(target),
                    "--advisor-model", "kernel-planner:32k",
                    "--advisor-commit", sha[:12], "--repo", str(tmp)])
            self.assertEqual(rc, 0)
            record = json.loads(target.read_text().splitlines()[0])
            self.assertEqual(record["status"], "ok")
            self.assertTrue(record["agrees_with_baseline"])
            self.assertIn("reconstructed-from-JSON", record["benchmark_packet"])


class AdvisorPolicy0418Tests(unittest.TestCase):
    """Policy remediation must not invent a TAKE or suppress technical risk."""

    @staticmethod
    def original_bad_review():
        return {
            "decision": "REVIEW",
            "rationale": (
                "The ArmSoM LM7 SoM has no concrete proof that it is an "
                "officially supported product platform in RHEL 10. A manual "
                "review of potential integration risks is appropriate."
            ),
            "risks": [],
        }

    def test_policy_only_product_review_detected(self):
        self.assertTrue(kbp.policy_only_review_objection(self.original_bad_review()))
        common_of_fdt = {
            "decision": "REVIEW", "risks": [],
            "rationale": (
                "Changes to early_init_dt_scan have no concrete evidence "
                "that they are necessary or beneficial for the supported "
                "architectures. Manual review is required."
            ),
        }
        self.assertTrue(kbp.policy_only_review_objection(common_of_fdt))

    def test_concrete_technical_issue_does_not_trigger_policy_retry(self):
        response = self.original_bad_review()
        response["rationale"] += " The include has an unresolved symbol dependency."
        self.assertFalse(kbp.policy_only_review_objection(response))
        response["rationale"] = "The board uses an undefined symbol X."
        self.assertFalse(kbp.policy_only_review_objection(response))
        response["decision"] = "TAKE"
        response["rationale"] = self.original_bad_review()["rationale"]
        self.assertFalse(kbp.policy_only_review_objection(response))

    def test_model_policy_review_gets_one_targeted_retry(self):
        sha = "a" * 40
        planner = object.__new__(kbp.Planner)
        planner.nodes = {sha: kbp.CommitNode(sha, "ArmSoM LM7")}
        planner.args = types.SimpleNamespace(
            advisor_retries=2, advisor_retry_delay=0,
            advisor_context_tokens=32768, advisor_output_reserve=1024)
        planner.advisor_calls = 0
        planner.advisor_retries_used = 0
        planner.advisor_prerequisite_evidence = lambda _sha: []
        planner.advisor_inbound_take_recommendations = lambda _sha: []
        planner.validate_advisor_result = lambda _sha, raw: raw
        planner._write_advisor_error_diagnostic = lambda *args: None
        planner.info = lambda _line: None
        class FakeOllama:
            last_telemetry = None
            def __init__(self): self.prompts = []
            def advise(self, prompt, packet, schema):
                self.prompts.append(prompt)
                return (AdvisorPolicy0418Tests.original_bad_review()
                        if len(self.prompts) == 1 else
                        {"decision": "TAKE", "rationale": "In-scope arm64 support",
                         "risks": []})
        client = FakeOllama()
        packet = {"commit": {"sha": sha}}
        result = planner._advisor_call_with_retries(client, "BASE PROMPT", sha, packet)
        self.assertEqual(result["decision"], "TAKE")
        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(client.prompts[0], "BASE PROMPT")
        self.assertIn("POLICY RECHECK", client.prompts[1])
        self.assertEqual(planner.advisor_retries_used, 1)

    def test_repeated_unsupported_product_review_fails_closed(self):
        sha = "b" * 40
        planner = object.__new__(kbp.Planner)
        planner.nodes = {sha: kbp.CommitNode(sha, "board")}
        planner.args = types.SimpleNamespace(
            advisor_retries=5, advisor_retry_delay=0,
            advisor_context_tokens=0, advisor_output_reserve=1024)
        planner.advisor_calls = planner.advisor_retries_used = 0
        planner.advisor_prerequisite_evidence = lambda _sha: []
        planner.advisor_inbound_take_recommendations = lambda _sha: []
        planner.validate_advisor_result = lambda _sha, raw: raw
        planner._write_advisor_error_diagnostic = lambda *args: None
        planner.info = lambda _line: None
        class BadOllama:
            def __init__(self): self.calls = 0
            def advise(self, *args):
                self.calls += 1
                return AdvisorPolicy0418Tests.original_bad_review()
        client = BadOllama()
        with self.assertRaises(kbp.AdvisorError) as caught:
            planner._advisor_call_with_retries(client, "prompt", sha, {})
        self.assertEqual(caught.exception.kind, "policy-review")
        self.assertEqual(client.calls, 2)

    def test_policy_guard_does_not_override_valid_technical_review(self):
        result = {"decision": "REVIEW", "rationale": (
            "The commit introduces a new DTSI include but its provider "
            "header is missing from the selected tree."), "risks": []}
        self.assertFalse(kbp.policy_only_review_objection(result))

    def test_prompt_has_updated_policy_and_cache_version(self):
        self.assertEqual(kbp.ADVISOR_PROMPT_VERSION, "21")
        prompt = kbp.Planner.advisor_system_prompt(None)
        self.assertIn("SKIP or REVIEW merely because", prompt)
        self.assertIn("unresolved technical or dependency question", prompt)


class AdvisorEvidence0418Tests(unittest.TestCase):
    def test_compact_encoding_matches_model_payload(self):
        packet = {"commit": {"sha": "a" * 40, "patch_excerpt": "+x" * 300}}
        wire = kbp.serialize_advisor_packet(packet)
        self.assertNotIn('"commit": {', wire)
        self.assertEqual(json.loads(wire), packet)
        self.assertLess(len(wire), len(json.dumps(packet, indent=2)))
        self.assertLess(kbp.advisor_request_size("prompt", packet, {}), 1250)

    def test_duplicate_consumed_objects_are_omitted_but_evidence_preserved(self):
        evidence = [{"evidence_id": "ev-binding", "peer_sha": "b" * 40,
                     "object": "armsom,sige5", "detail": "X" * 2000}]
        packet = {"commit": {
            "sha": "a" * 40, "patch_excerpt": "+foo",
            "same_series_consumed_objects": evidence,
            "eligible_prerequisite_evidence": evidence,
            "actual_inbound_take_prerequisites": [],
            "deterministic_consumer_context": {"consumers": []},
        }}
        before = json.dumps(packet)
        size_without_duplicate = kbp.advisor_request_size(
            "prompt", {"commit": {k: v for k, v in packet["commit"].items()
                if k != "same_series_consumed_objects"}}, {})
        output, meta = kbp.budget_advisor_packet(packet, "prompt", {},
                                                  size_without_duplicate + 320)
        self.assertEqual(output["commit"]["eligible_prerequisite_evidence"], evidence)
        self.assertNotIn("same_series_consumed_objects", output["commit"])
        self.assertIn("duplicate-consumed-object-evidence", meta["omitted"])
        self.assertEqual(before, json.dumps(packet))

    def test_benchmark_reference_prioritizes_disagreements(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            a, b = "a" * 40, "b" * 40
            def record(sha, decision):
                return {"subject": "board", "direct": True,
                        "files": ["arch/arm64/boot/dts/board.dts"],
                        "advisor_decision": decision,
                        "advisor_prerequisite_evidence": [],
                        "advisor_prerequisites": []}
            primary = {"base": "v6.19", "target": "v7.0", "downstream": "base",
                       "paths": ["arch/arm64"], "order": [a, b],
                       "commits": {a: record(a, "TAKE"), b: record(b, "REVIEW")}}
            reference = {"target": "v7.0", "downstream": "base",
                         "commits": {a: record(a, "TAKE"), b: record(b, "TAKE")}}
            (path / "run21.json").write_text(json.dumps(primary))
            (path / "run20.json").write_text(json.dumps(reference))
            response = {"decision": "TAKE", "rationale": "in scope",
                        "prerequisite_evidence_ids": [],
                        "inbound_prerequisite_assessments": [], "risks": []}
            with mock.patch.object(kbp.Git, "run", return_value="patch") as run, \
                 mock.patch.object(kbp.OllamaAdvisor, "advise", return_value=response):
                code = kbp.main([
                    "--advisor-benchmark-plan", str(path / "run21.json"),
                    "--benchmark-reference-plan", str(path / "run20.json"),
                    "--advisor-model", "kernel-planner:32k",
                    "--advisor-limit", "1", "--benchmark-output", str(path / "out.jsonl"),
                    "--repo", str(path)])
            self.assertEqual(code, 0)
            lines = (path / "out.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["sha"], b)
            self.assertEqual(entry["reference_decision"], "TAKE")
            self.assertEqual(entry["baseline_decision"], "REVIEW")
            self.assertTrue(entry["agrees_with_reference"])

class AdvisorBinding0419Tests(unittest.TestCase):
    """0.4.19: an unassessed compatible binding must not disappear."""

    @staticmethod
    def fixture():
        planner = object.__new__(kbp.Planner)
        sha = "c" * 40
        peer = "b" * 40
        proof = {
            "evidence_id": "ev-26b76f5fd77f3164",
            "peer_sha": peer,
            "peer_subject": "ArmSoM binding",
            "kind": "compatible-binding", "object": "armsom,sige5",
            "selected_use": 'compatible = "armsom,sige5"',
            "peer_provides": "rockchip.yaml binding armsom,sige5",
        }
        consumer = kbp.CommitNode(sha, "ArmSoM Sige 5 board", direct=True)
        consumer.advisor_decision = "TAKE"
        planner.nodes = {sha: consumer}
        planner.advisor_prerequisite_evidence = lambda _sha: [proof]
        planner.advisor_inbound_take_recommendations = lambda _sha: []
        planner.ordered_missing = lambda: [sha]
        return planner, consumer, proof

    @staticmethod
    def answer(ids=(), assessments=(), decision="TAKE"):
        return {
            "decision": decision, "rationale": "Arm64 convergence change",
            "prerequisite_evidence_ids": list(ids),
            "binding_evidence_assessments": list(assessments),
            "inbound_prerequisite_assessments": [], "risks": [],
        }

    def test_missing_binding_disposition_rejected(self):
        planner, consumer, evidence = self.fixture()
        with self.assertRaises(kbp.AdvisorError) as ctx:
            planner.validate_advisor_result(consumer.sha, self.answer())
        self.assertEqual(ctx.exception.kind, "binding-unassessed")
        self.assertTrue(ctx.exception.retryable)

    def test_binding_required_is_selected_and_cached(self):
        planner, consumer, proof = self.fixture()
        assessment = {"evidence_id": proof["evidence_id"],
                      "disposition": "REQUIRED",
                      "reason": "Board compatible should be declared in schema."}
        result = planner.validate_advisor_result(consumer.sha,
             self.answer([proof["evidence_id"]], [assessment]))
        self.assertEqual(result["prerequisite_peers"][0]["sha"], proof["peer_sha"])
        self.assertEqual(result["binding_evidence_assessments"], [assessment])
        # A normalized cached result must survive the same validation.
        roundtrip = planner.validate_advisor_result(consumer.sha, result)
        self.assertEqual(roundtrip["prerequisite_peers"], result["prerequisite_peers"])

    def test_binding_not_required_has_recorded_reason(self):
        planner, consumer, proof = self.fixture()
        assessment = {"evidence_id": proof["evidence_id"],
                      "disposition": "NOT_REQUIRED",
                      "reason": "Schema annotation is not required for this staged change."}
        result = planner.validate_advisor_result(consumer.sha,
                                                 self.answer([], [assessment]))
        self.assertEqual(result["prerequisite_peers"], [])
        consumer.advisor_binding_assessments = result["binding_evidence_assessments"]
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues, [])

    def test_required_binding_without_selected_edge_rejected(self):
        planner, consumer, proof = self.fixture()
        with self.assertRaises(kbp.AdvisorError) as ctx:
            planner.validate_advisor_result(consumer.sha, self.answer([], [{
                "evidence_id": proof["evidence_id"], "disposition": "REQUIRED",
                "reason": "Schema needed for DT validation."}]))
        self.assertEqual(ctx.exception.kind, "binding-unassessed")

    def test_unselected_binding_audit_detects_run22_pattern(self):
        planner, consumer, proof = self.fixture()
        consumer.advisor_prerequisites = []
        consumer.advisor_binding_assessments = []
        planner._advisor_check_consistency()
        issues = planner.advisor_consistency_issues
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "DT_BINDING->UNASSESSED")
        self.assertEqual(issues[0]["prerequisite_sha"], proof["peer_sha"])

    def test_schema_requires_explicit_binding_field(self):
        _, _, proof = self.fixture()
        schema = kbp.advisor_response_schema([proof])
        self.assertIn("binding_evidence_assessments", schema["required"])
        item = schema["properties"]["binding_evidence_assessments"]["items"]
        self.assertEqual(item["properties"]["evidence_id"]["enum"],
                         [proof["evidence_id"]])

    def test_optional_notice_does_not_create_spurious_review(self):
        evidence = [{"evidence_id": "ev-test", "peer_sha": "b" * 40,
                     "kind": "included-file", "object": "board.dtsi"}]
        packet = {"commit": {"sha": "a" * 40, "patch_excerpt": "+" * 1500,
                              "same_series_consumed_objects": evidence,
                              "eligible_prerequisite_evidence": evidence}}
        reduced = {"commit": {"sha": "a" * 40, "patch_excerpt": "+" * 1500,
                              "eligible_prerequisite_evidence": evidence}}
        cap = kbp.advisor_request_size("prompt", reduced, {}) + 2
        output, meta = kbp.budget_advisor_packet(packet, "prompt", {}, cap)
        self.assertEqual(output["commit"]["eligible_prerequisite_evidence"], evidence)
        self.assertNotIn("evidence_budget_notice", output)
        self.assertFalse(meta["notice_embedded"])
        self.assertLessEqual(meta["final_chars"], cap)

    def test_oversized_mandatory_patch_still_fails_closed(self):
        packet = {"commit": {"sha": "a" * 40, "patch_excerpt": "+" * 50000,
                              "eligible_prerequisite_evidence": []}}
        with self.assertRaises(kbp.AdvisorError) as ctx:
            kbp.budget_advisor_packet(packet, "prompt", {}, 48000)
        self.assertEqual(ctx.exception.kind, "evidence-budget")

    def test_binding_recheck_prompt_is_bounded(self):
        extra = len(kbp.binding_retry_prompt("base")) - len("base")
        self.assertLess(extra, 600)
        self.assertIn("BINDING EVIDENCE RECHECK", kbp.binding_retry_prompt("base"))

class MultipleBindingEvidence0419Tests(unittest.TestCase):
    def test_two_required_evidence_ids_for_one_peer_are_consistent(self):
        planner = object.__new__(kbp.Planner)
        consumer = kbp.CommitNode("c" * 40, "board", direct=True)
        provider = kbp.CommitNode("b" * 40, "binding")
        consumer.advisor_decision = provider.advisor_decision = "TAKE"
        provider.add_reason("advisor-prerequisite:compatible-binding", consumer.sha)
        consumer.advisor_prerequisites = [{
            "sha": provider.sha, "evidence_id": "ev-compatible",
            "evidence_kind": "compatible-binding", "evidence_object": "vendor,board",
            "reason": "required",
        }]
        consumer.advisor_binding_assessments = [
            {"evidence_id": "ev-compatible", "disposition": "REQUIRED",
             "reason": "schema"},
            {"evidence_id": "ev-property", "disposition": "REQUIRED",
             "reason": "property"},
        ]
        planner.nodes = {consumer.sha: consumer, provider.sha: provider}
        planner.ordered_missing = lambda: [provider.sha, consumer.sha]
        planner.advisor_prerequisite_evidence = lambda sha: ([
            {"evidence_id": "ev-compatible", "peer_sha": provider.sha,
             "kind": "compatible-binding", "object": "vendor,board"},
            {"evidence_id": "ev-property", "peer_sha": provider.sha,
             "kind": "dt-property-binding", "object": "vendor,board:clocks"},
        ] if sha == consumer.sha else [])
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues, [])

class BindingReasonPolicy0419Tests(unittest.TestCase):
    def test_official_support_only_binding_dismissal_is_not_valid(self):
        planner, consumer, proof = AdvisorBinding0419Tests.fixture()
        assessment = {"evidence_id": proof["evidence_id"],
                      "disposition": "NOT_REQUIRED",
                      "reason": "This board is not officially supported by RHEL."}
        with self.assertRaises(kbp.AdvisorError) as caught:
            planner.validate_advisor_result(
                consumer.sha, AdvisorBinding0419Tests.answer([], [assessment]))
        self.assertEqual(caught.exception.kind, "binding-unassessed")


class BindingDisagreement0420Tests(unittest.TestCase):
    """The real run 23 ArmSoM binding was REQUIRED / NOT_REQUIRED."""

    @staticmethod
    def fixture(kind="compatible-binding"):
        planner = object.__new__(kbp.Planner)
        consumer = kbp.CommitNode("c" * 40, "Sige 5 board", direct=True)
        provider = kbp.CommitNode("b" * 40, "ArmSoM compatible binding")
        consumer.advisor_decision = provider.advisor_decision = "TAKE"
        eid = "ev-26b76f5fd77f3164"
        consumer.advisor_prerequisites = [{
            "sha": provider.sha, "evidence_id": eid,
            "evidence_kind": kind, "evidence_object": "armsom,sige5",
            "reason": "schema provides exact compatible",
        }]
        consumer.advisor_binding_assessments = [{
            "evidence_id": eid, "disposition": "REQUIRED",
            "reason": "DTS declares an armsom,sige5 compatible requiring schema",
        }] if "binding" in kind else []
        provider.advisor_inbound_assessments = [{
            "evidence_id": eid, "disposition": "NOT_REQUIRED",
            "reason": "rk3576 already covers it",
        }]
        planner.nodes = {consumer.sha: consumer, provider.sha: provider}
        planner.ordered_missing = lambda: [provider.sha, consumer.sha]
        planner.advisor_prerequisite_evidence = lambda sha: ([{
            "evidence_id": eid, "peer_sha": provider.sha,
            "kind": kind, "object": "armsom,sige5",
        }] if sha == consumer.sha else [])
        return planner, consumer, provider

    def test_run23_binding_conflict_preserves_edge_and_is_explicit(self):
        planner, consumer, provider = self.fixture()
        conflicts = planner._advisor_binding_conflicts()
        self.assertEqual(len(conflicts[provider.sha]), 1)
        self.assertIn("DTS declares", conflicts[provider.sha][0]["consumer_reason"])
        planner._advisor_resolve_rejected_prerequisites()
        self.assertEqual(len(consumer.advisor_prerequisites), 1)
        self.assertEqual(consumer.advisor_rejected_prerequisites, [])
        planner._advisor_check_consistency()
        self.assertEqual([x["kind"] for x in planner.advisor_consistency_issues],
                         ["DT_BINDING->DISAGREEMENT"])
        issue = planner.advisor_consistency_issues[0]
        self.assertEqual(issue["assessment_disposition"], "NOT_REQUIRED")
        self.assertIn("rk3576", issue["assessment_reason"])
        self.assertIn("schema", issue["consumer_assessment_reason"])

    def test_peer_recheck_can_resolve_disagreement(self):
        planner, consumer, provider = self.fixture()
        seen = []
        planner.performance = None
        planner.info = lambda msg: None
        def evaluate(client, prompt, sha, **kwargs):
            seen.append((sha, kwargs["binding_conflict_context"]))
            provider.advisor_inbound_assessments[0]["disposition"] = "REQUIRED"
        planner._advisor_evaluate_one = evaluate
        planner._advisor_reconcile_binding_conflicts(None, "prompt")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], provider.sha)
        self.assertEqual(planner.advisor_binding_rechecks, 1)
        planner._advisor_resolve_rejected_prerequisites()
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues, [])

    def test_disagreement_persisting_after_recheck_still_blocks(self):
        planner, consumer, provider = self.fixture()
        planner.info = lambda msg: None
        planner.performance = None
        calls = []
        planner._advisor_evaluate_one = lambda *a, **kw: calls.append(kw)
        planner._advisor_reconcile_binding_conflicts(None, "prompt")
        planner._advisor_resolve_rejected_prerequisites()
        planner._advisor_check_consistency()
        self.assertEqual(len(calls), 1)
        self.assertEqual(planner.advisor_consistency_issues[0]["kind"],
                         "DT_BINDING->DISAGREEMENT")
        self.assertEqual(len(consumer.advisor_prerequisites), 1)

    def test_nonbinding_not_required_retains_old_rejection_semantics(self):
        planner, consumer, provider = self.fixture(kind="symbol-definition")
        planner._advisor_resolve_rejected_prerequisites()
        self.assertEqual(consumer.advisor_prerequisites, [])
        self.assertEqual(len(consumer.advisor_rejected_prerequisites), 1)
        planner._advisor_check_consistency()
        self.assertEqual(planner.advisor_consistency_issues, [])

    def test_recheck_cache_packet_is_distinct_from_ordinary_packet(self):
        planner = object.__new__(kbp.Planner)
        planner.base, planner.target, planner.downstream = "a", "b", "c"
        planner.advisor_model = "kernel-planner:32k"
        sha = "e" * 40
        packet = {"commit": {"sha": sha}}
        original = planner.advisor_cache_key(sha, packet)
        revised = {"commit": {**packet["commit"], "binding_conflict_context": [
            {"evidence_id": "ev-test", "consumer_disposition": "REQUIRED"}]}}
        self.assertNotEqual(original, planner.advisor_cache_key(sha, revised))

    def test_two_binding_evidence_ids_one_peer_still_reports_conflict(self):
        planner, consumer, provider = self.fixture()
        consumer.advisor_binding_assessments.append({
            "evidence_id": "ev-property", "disposition": "REQUIRED",
            "reason": "schema includes required property",
        })
        old = planner.advisor_prerequisite_evidence
        planner.advisor_prerequisite_evidence = lambda sha: (
            old(sha) + [{"evidence_id": "ev-property", "peer_sha": provider.sha,
                         "kind": "dt-property-binding", "object": "foo"}]
            if sha == consumer.sha else [])
        planner._advisor_resolve_rejected_prerequisites()
        planner._advisor_check_consistency()
        self.assertEqual([x["kind"] for x in planner.advisor_consistency_issues],
                         ["DT_BINDING->DISAGREEMENT"])


class EvidenceBudget0420Tests(unittest.TestCase):
    def test_optional_consumer_provenance_removed_before_required_patch(self):
        evidence = [{"evidence_id": "ev-1", "peer_sha": "b" * 40,
                     "kind": "included-file", "object": "board.dtsi"}]
        packet = {"commit": {
            "sha": "a" * 40, "patch_excerpt": "+" * 2000,
            "eligible_prerequisite_evidence": evidence,
            "actual_inbound_take_prerequisites": [
                {"evidence_id": "ev-in", "consumer_sha": "d" * 40}],
            "deterministic_consumer_context": {
                "relationship_status": "provenance_only",
                "consumers": [{"sha": "c" * 40,
                               "changed_files": ["arch/arm64/boot/dts/board.dts"],
                               "patch_excerpt": "x" * 60000,
                               "other_provenance": "y" * 60000}],
            },
        }}
        out, meta = kbp.budget_advisor_packet(packet, "prompt", {}, 4000)
        commit = out["commit"]
        self.assertEqual(commit["patch_excerpt"], packet["commit"]["patch_excerpt"])
        self.assertEqual(commit["eligible_prerequisite_evidence"], evidence)
        self.assertEqual(commit["actual_inbound_take_prerequisites"],
                         packet["commit"]["actual_inbound_take_prerequisites"])
        self.assertEqual(commit["deterministic_consumer_context"]["consumers"], [])
        self.assertTrue(any(s.startswith("optional-consumer-provenance")
                            for s in meta["omitted"]))
        self.assertLessEqual(meta["final_chars"], 4000)

    def test_optional_series_peer_scope_removed_without_losing_paths(self):
        peer = {"sha": "b" * 40, "subject": "binding",
                "changed_files": ["Documentation/devicetree/bindings/arm/a.yaml"],
                "scope": {"verbose_context": "x" * 8000}}
        packet = {"commit": {"sha": "a" * 40, "patch_excerpt": "+" * 1200,
                              "eligible_prerequisite_evidence": [],
                              "same_series": [peer]}}
        out, meta = kbp.budget_advisor_packet(packet, "prompt", {}, 3000)
        kept = out["commit"]["same_series"][0]
        self.assertEqual(kept["changed_files"], peer["changed_files"])
        self.assertNotIn("scope", kept)
        self.assertIn("optional-series-peer-scope", meta["omitted"])

    def test_large_mandatory_evidence_still_fails_closed(self):
        packet = {"commit": {"sha": "a" * 40, "patch_excerpt": "p" * 1300,
            "eligible_prerequisite_evidence": [
                {"evidence_id": "ev", "peer_sha": "b" * 40,
                 "selected_use": "s" * 6000}]}}
        with self.assertRaises(kbp.AdvisorError) as e:
            kbp.budget_advisor_packet(packet, "prompt", {}, 4000)
        self.assertEqual(e.exception.kind, "evidence-budget")

class BindingCoverage0421Tests(unittest.TestCase):
    """Exercise cross-series provider indexing with real temporary Git history."""

    def fixture(self, *, downstream_has_compatible=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        def git(*args):
            cp = subprocess.run(["git", *args], cwd=root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True)
            return cp.stdout.strip()
        git("init", "-q")
        git("config", "user.name", "Planner Tests")
        git("config", "user.email", "planner@example.invalid")
        bpath = root / "Documentation/devicetree/bindings/vendor,board.yaml"
        bpath.parent.mkdir(parents=True)
        if downstream_has_compatible:
            bpath.write_text("properties:\n  compatible:\n    const: vendor,new-board\n")
        else:
            bpath.write_text("properties:\n  compatible:\n    const: vendor,old-board\n")
        git("add", ".")
        git("commit", "-qm", "Downstream baseline")
        base = git("rev-parse", "HEAD")
        bpath.write_text("properties:\n  compatible:\n    enum:\n"
                         "      - vendor,old-board\n"
                         "      - vendor,new-board\n")
        git("add", ".")
        git("commit", "-qm", "Add optional vendor,new-board binding")
        provider = git("rev-parse", "HEAD")
        dpath = root / "arch/arm64/boot/dts/vendor/new-board.dts"
        dpath.parent.mkdir(parents=True)
        dpath.write_text('/dts-v1/;\n/ {\n  compatible = "vendor,new-board", '
                         '"vendor,old-board";\n};\n')
        git("add", ".")
        git("commit", "-qm", "Add a new ARM64 board")
        board = git("rev-parse", "HEAD")
        planner = object.__new__(kbp.Planner)
        planner.git = kbp.Git(root)
        planner.base = base
        planner.target = board
        planner.replay_start = base
        planner.nodes = {board: kbp.CommitNode(
            board, "ARM64 board", direct=True,
            files=["arch/arm64/boot/dts/vendor/new-board.dts"])}
        planner.series_groups = {}
        planner.dt_binding_candidate_links = {}
        planner._dt_binding_provider_index_cache = None
        planner._dt_binding_audit_scanned = set()
        planner._tree_file_text_cache = {}
        planner.info = lambda msg: None
        planner._advisor_path_product_relevant = lambda path: True
        planner.patch_excerpt = lambda sha, limit: planner.git.run([
            "show", "--pretty=format:", "--unified=0", sha,
        ])[:limit]
        planner.args = types.SimpleNamespace(
            advisor_max_diff_chars=12000, advisor_max_peer_diff_chars=6000,
        )
        planner.subject = planner.git.subject
        planner.changed_files = planner.git.changed_files
        def ensure(sha, reason, required_by):
            node = planner.nodes.get(sha)
            if node is None:
                node = kbp.CommitNode(sha, "binding", files=[
                    "Documentation/devicetree/bindings/vendor,board.yaml"
                ])
                planner.nodes[sha] = node
            node.add_reason(reason, required_by,
                            tentative=planner.is_tentative_dependency_reason(reason))
            return node
        planner.ensure_node = ensure
        return planner, board, provider

    def test_discovers_cross_series_provider_without_force_take(self):
        planner, board, provider = self.fixture()
        index = planner._dt_binding_provider_index()
        self.assertIn((provider,
            "Documentation/devicetree/bindings/vendor,board.yaml"),
            index["vendor,new-board"])
        planner._audit_dt_binding_candidates(board)
        self.assertEqual(planner.dt_binding_candidate_links[board][0]["peer_sha"],
                         provider)
        self.assertFalse(planner.is_actionable_node(planner.nodes[provider]))
        self.assertIn(provider, planner.advisor_allowed_prerequisite_peers(board))
        evidence = planner.advisor_prerequisite_evidence(board)
        self.assertTrue(any(item["kind"] == "compatible-binding" and
                            item["peer_sha"] == provider for item in evidence))

    def test_present_downstream_compatible_is_not_a_candidate(self):
        planner, board, provider = self.fixture(downstream_has_compatible=True)
        planner._audit_dt_binding_candidates(board)
        self.assertFalse(planner.dt_binding_candidate_links.get(board))
        self.assertNotIn(provider, planner.nodes)

    def test_selected_provider_is_promoted_only_after_required(self):
        planner, board, provider = self.fixture()
        planner._audit_dt_binding_candidates(board)
        evidence = next(item for item in planner.advisor_prerequisite_evidence(board)
                        if item["peer_sha"] == provider and
                        item["kind"] == "compatible-binding")
        consumer = planner.nodes[board]
        consumer.advisor_decision = "TAKE"
        consumer.advisor_prerequisites = [{
            "sha": provider, "evidence_id": evidence["evidence_id"],
            "evidence_kind": evidence["kind"], "reason": "schema required",
        }]
        planner._record_advisor_prerequisite_recommendations = lambda sha: None
        self.assertEqual(planner._advisor_promote_prerequisites(), 1)
        self.assertTrue(planner.is_actionable_node(planner.nodes[provider]))
        self.assertIsNone(planner.nodes[provider].advisor_decision)

    def test_compatible_evidence_survives_short_peer_excerpt(self):
        planner, board, provider = self.fixture()
        planner._audit_dt_binding_candidates(board)
        planner.args.advisor_max_peer_diff_chars = 40
        evidence = planner.advisor_prerequisite_evidence(board)
        matches = [x for x in evidence if x["peer_sha"] == provider and
                   x["kind"] == "compatible-binding"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["object"], "vendor,new-board")

class BindingCoverageSafety0421Tests(unittest.TestCase):
    def test_coverage_exceeding_evidence_cap_blocks_take(self):
        planner = object.__new__(kbp.Planner)
        consumer = kbp.CommitNode("a" * 40, "consumer", direct=True)
        consumer.advisor_decision = "TAKE"
        planner.nodes = {consumer.sha: consumer}
        planner.dt_binding_coverage_overflow = {consumer.sha: 70}
        planner.ordered_missing = lambda: [consumer.sha]
        planner.dt_include_unresolved = []
        planner.advisor_prerequisite_evidence = lambda _sha: []
        planner._advisor_check_consistency()
        self.assertEqual(len(planner.advisor_consistency_issues), 1)
        self.assertEqual(planner.advisor_consistency_issues[0]["kind"],
                         "DT_BINDING->COVERAGE_OVERFLOW")

    def test_optional_coverage_summary_can_be_budgeted_away(self):
        packet = {"commit": {
            "sha": "a" * 40, "patch_excerpt": "+required patch\n",
            "changed_files": ["arch/arm64/boot/dts/test.dts"],
            "eligible_prerequisite_evidence": [{
                "evidence_id": "ev-required", "peer_sha": "b" * 40,
                "kind": "compatible-binding", "object": "vendor,board",
            }],
            "binding_coverage_candidates": [{"detail": "x" * 600}],
        }}
        prompt, schema = "prompt", {}
        minimum = kbp.advisor_request_size(prompt, {
            "commit": {k: v for k, v in packet["commit"].items()
                       if k != "binding_coverage_candidates"}}, schema)
        reduced, budget = kbp.budget_advisor_packet(
            packet, prompt, schema, minimum + 40)
        self.assertNotIn("binding_coverage_candidates", reduced["commit"])
        self.assertIn("eligible_prerequisite_evidence", reduced["commit"])
        self.assertIn("optional-binding-coverage-summary", budget["omitted"])

    def test_take_consumer_dt_include_provider_evidence_is_explicit(self):
        planner = object.__new__(kbp.Planner)
        planner.args = types.SimpleNamespace(
            advisor_product="rhel10", advisor_max_message_chars=5000,
            advisor_max_diff_chars=8000, advisor_required_by_depth=1,
            advisor_max_related_commits=3, advisor_max_related_diff_chars=0,
        )
        planner.base = "a" * 40
        planner.target = "b" * 40
        planner.downstream = "c" * 40
        planner.downstream_ref = "cs10/main"
        planner.paths = ["arch/arm64/"]
        consumer = kbp.CommitNode("d" * 40, "board", direct=True)
        consumer.advisor_decision = "TAKE"
        provider = kbp.CommitNode("e" * 40, "SoC dtsi", files=[
            "arch/arm64/boot/dts/test/soc.dtsi"])
        provider.add_reason("dt-include:arch/arm64/boot/dts/test/soc.dtsi",
                            consumer.sha)
        planner.nodes = {consumer.sha: consumer, provider.sha: provider}
        planner.git = types.SimpleNamespace(is_ancestor=lambda *a: True)
        planner.series_groups = {}
        planner.body = lambda sha: "test"
        planner.patch_excerpt = lambda sha, count: "+test"
        planner.advisor_obsolescence_context = lambda sha, files: {
            "all_changed_files_deleted_by_take_consumers": False}
        planner.advisor_required_by_context = lambda sha: []
        planner.advisor_series_context = lambda sha: []
        planner.advisor_prerequisite_evidence = lambda sha: []
        planner.advisor_inbound_take_recommendations = lambda sha: []
        planner.advisor_allowed_prerequisite_peers = lambda sha: []
        planner.advisor_product_context = lambda: {
            "product": "rhel10", "supported_kernel_arch_dirs": ["arm64"]}
        result = planner.advisor_packet(provider.sha)
        paths = result["commit"]["deterministic_dt_include_requirements"]
        self.assertEqual(paths[0]["include_path"],
                         "arch/arm64/boot/dts/test/soc.dtsi")
        self.assertEqual(paths[0]["take_consumer_shas"], [consumer.sha])

class VerifiedSourceClaims0422Tests(unittest.TestCase):
    """Use actual run-25 explanation patterns without inventing schema facts."""

    REALTEK_REASON = (
        "The compatible string 'realtek,krypton' is already covered by the "
        "existing binding schema for RTD1501s SoC based boards."
    )
    BINDING = "Documentation/devicetree/bindings/arm/realtek.yaml"

    def test_realtek_false_downstream_schema_claim_requires_recheck(self):
        feedback = kbp.binding_claim_feedback(
            self.REALTEK_REASON, "realtek,krypton", "const: realtek,old",
            self.BINDING, {"realtek,old"})
        self.assertIn("exact compatible is absent", feedback)
        self.assertIn(self.BINDING, feedback)

    def test_existing_exact_downstream_compatible_is_not_rejected(self):
        self.assertIsNone(kbp.binding_claim_feedback(
            self.REALTEK_REASON, "realtek,krypton",
            "enum: [realtek,krypton]", self.BINDING, {"realtek,krypton"}))

    def test_other_schema_coverage_is_not_declared_impossible(self):
        feedback = kbp.binding_claim_feedback(
            self.REALTEK_REASON, "realtek,krypton", None, self.BINDING, set())
        self.assertIn("A broader pattern or a different schema may cover it", feedback)

    def test_dts_compatible_is_not_equivalent_to_binding_schema(self):
        feedback = kbp.binding_claim_feedback(
            "The DTS already contains the compatible string, so no new binding "
            "is required.", "qnap,ts133", None, self.BINDING, set())
        self.assertIn("not binding-schema coverage", feedback)

    def test_not_required_source_claim_fails_closed_in_validation(self):
        planner, consumer, evidence = AdvisorBinding0419Tests.fixture()
        evidence["provider_path"] = self.BINDING
        evidence["object"] = "realtek,krypton"
        planner.replay_start = "a" * 40
        planner._tree_file_text = lambda tree, path: "const: realtek,old"
        assessment = {"evidence_id": evidence["evidence_id"],
                      "disposition": "NOT_REQUIRED", "reason": self.REALTEK_REASON}
        with self.assertRaises(kbp.AdvisorError) as caught:
            planner.validate_advisor_result(
                consumer.sha, AdvisorBinding0419Tests.answer([], [assessment]))
        self.assertEqual(caught.exception.kind, "binding-source-claim")
        self.assertTrue(caught.exception.retryable)

    def test_verified_include_fact_does_not_infer_yaml_requirement(self):
        planner = object.__new__(kbp.Planner)
        consumer = kbp.CommitNode("a" * 40, "TAKE board")
        consumer.advisor_decision = "TAKE"
        provider = kbp.CommitNode("b" * 40, "DTSI provider")
        path = "arch/arm64/boot/dts/rockchip/rk3576.dtsi"
        provider.add_reason("dt-include:" + path, consumer.sha)
        planner.nodes = {consumer.sha: consumer, provider.sha: provider}
        planner.replay_start = "c" * 40
        planner._tree_file_text = lambda rev, path: (
            None if rev == planner.replay_start else "/ { test; };"
        )
        facts = planner._advisor_hard_dt_include_facts(provider.sha)
        self.assertEqual(facts[0]["include_path"], path)
        self.assertEqual(facts[0]["take_consumer_shas"], [consumer.sha])
        self.assertEqual(facts[0]["downstream_file"], "absent")
        self.assertIn("source #include", kbp.dt_include_retry_prompt("base", facts))

    def test_include_provider_review_gets_targeted_retry_not_forced_take(self):
        planner = object.__new__(kbp.Planner)
        sha = "b" * 40
        planner.nodes = {sha: kbp.CommitNode(sha, "DTSI provider")}
        planner.args = types.SimpleNamespace(advisor_retries=2,
                         advisor_retry_delay=0, advisor_context_tokens=0)
        planner.advisor_calls = planner.advisor_retries_used = 0
        planner.advisor_prerequisite_evidence = lambda _: []
        planner.advisor_inbound_take_recommendations = lambda _: []
        planner.validate_advisor_result = lambda _, response: response
        planner._advisor_hard_dt_include_facts = lambda _: [{
            "include_path": "arch/arm64/boot/dts/test.dtsi",
            "take_consumer_shas": ["a" * 40]}]
        planner._write_advisor_error_diagnostic = lambda *args: None
        planner.info = lambda _: None
        class Client:
            last_telemetry = None
            def __init__(self): self.prompts = []
            def advise(self, prompt, packet, schema):
                self.prompts.append(prompt)
                return {"decision": "REVIEW", "rationale":
                        "Not sure if the platform needs the DTSI source.",
                        "risks": []}
        client = Client()
        result = planner._advisor_call_with_retries(client, "base", sha, {})
        self.assertEqual(result["decision"], "REVIEW")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("HARD DT INCLUDE RECHECK", client.prompts[1])


class ExactTrace0422Tests(unittest.TestCase):
    def test_capture_and_git_free_exact_replay(self):
        with tempfile.TemporaryDirectory() as td:
            trace = Path(td) / "requests.jsonl"
            output = Path(td) / "results.jsonl"
            packet = {"commit": {"sha": "a" * 40, "patch_excerpt": "+x"}}
            schema = {"type": "object"}
            response = {"decision": "TAKE", "rationale": "works"}
            planner = object.__new__(kbp.Planner)
            planner.advisor_trace_path = trace
            planner._write_advisor_trace({
                "trace_format": "exact-advisor-request-v1", "sha": "a" * 40,
                "attempt": 1, "model": "kernel-planner:32k",
                "prompt_version": "18", "system_prompt": "original prompt",
                "packet": packet, "response_schema": schema,
                "response": response, "error_kind": None})
            self.assertEqual(json.loads(trace.read_text().splitlines()[0])
                             ["packet"], packet)
            calls = []
            class FakeClient:
                def __init__(self, *args): self.last_telemetry = None
                def advise(self, prompt, packet_arg, schema_arg):
                    calls.append((prompt, packet_arg, schema_arg))
                    return response
            with mock.patch.object(kbp, "OllamaAdvisor", FakeClient), \
                 mock.patch.object(kbp, "run_planner", side_effect=AssertionError(
                     "must not run collection")):
                rc = kbp.main(["--advisor-replay-trace", str(trace),
                               "--advisor-model", "kernel-planner:32k",
                               "--advisor-commit", "a" * 12,
                               "--benchmark-output", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [("original prompt", packet, schema)])
            replay = json.loads(output.read_text().splitlines()[0])
            self.assertTrue(replay["matches_original"])
            self.assertEqual(replay["replay_type"],
                             "exact-saved-prompt-packet-schema")
            self.assertEqual(replay["status"], "ok")
            with mock.patch.object(kbp, "OllamaAdvisor", FakeClient):
                self.assertEqual(kbp.main(["--advisor-replay-trace", str(trace),
                    "--advisor-model", "kernel-planner:32k",
                    "--benchmark-output", str(output)]), 1)

    def test_trace_cli_requires_output_and_model(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                kbp.parse_args(["--advisor-replay-trace", "/tmp/trace.jsonl"])

class ExactTracePackaging0422Tests(unittest.TestCase):
    def test_trace_is_included_in_auto_package(self):
        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "backport_plan_26"
            trace = Path(td) / "backport_plan_26.advisor-trace.jsonl"
            trace.write_text('{"sha":"example"}\n')
            kbp.worksheet_path_for_plan(prefix).write_text("plan\n")
            prefix.with_suffix(".log").write_text("log\n")
            prefix.with_suffix(".json").write_text(json.dumps({
                "advisor": {"trace_path": str(trace)}
            }))
            _, archive = kbp.package_results(str(prefix))
            with zipfile.ZipFile(archive) as result:
                names = result.namelist()
                self.assertIn("backport_plan_26/" + trace.name, names)
                self.assertEqual(result.read(
                    "backport_plan_26/" + trace.name), trace.read_bytes())


class DualAdvisor0423Tests(unittest.TestCase):
    def make_planner(self):
        planner = object.__new__(kbp.Planner)
        planner.nodes = {
            "a" * 40: kbp.CommitNode("a" * 40, "missing include", direct=True),
            "b" * 40: kbp.CommitNode("b" * 40, "binding conflict", direct=True),
        }
        for node in planner.nodes.values():
            node.match = kbp.Match(status="missing")
            node.advisor_decision = "REVIEW"
            node.advisor_rationale = "Unresolved API compatibility with downstream"
            node.advisor_decision_model = "kernel-planner:32k"
        planner.advisor_model = "kernel-planner:32k"
        planner.advisor_primary_model = planner.advisor_model
        planner.advisor_fallback_model = "gemma4-kernel:128k"
        planner.advisor_calls = 0
        planner.advisor_failures = 0
        planner.advisor_failure_kinds = collections.Counter()
        planner.advisor_fallback_evaluated = 0
        planner.advisor_fallback_accepted = 0
        planner.advisor_fallback_changed = 0
        planner.advisor_fallback_take = 0
        planner.advisor_fallback_failures = 0
        planner.advisor_fallback_unprocessed = 0
        planner.advisor_consistency_issues = [{
            "kind": "DT_INCLUDE->REVIEW", "consumer_sha": "b" * 40,
            "prerequisite_sha": "a" * 40,
        }]
        planner.args = types.SimpleNamespace(
            advisor_fallback_limit=0, advisor_base_url="http://gondor:11434",
            advisor_temperature=0.0, advisor_keep_alive="10m", debug=False,
            advisor_max_request_chars=48000, advisor_context_tokens=32768,
            advisor_fallback_max_request_chars=120000,
            advisor_fallback_context_tokens=131072,
            advisor_fallback_timeout=900,
        )
        planner.performance = None
        planner._advisor_stage = "primary"
        planner.info = lambda msg: None
        planner.advisor_system_prompt = lambda: "policy"
        planner._record_advisor_prerequisite_recommendations = lambda sha: None
        planner._advisor_promote_prerequisites = lambda: 0
        planner._advisor_resolve_rejected_prerequisites = lambda: None
        planner._advisor_check_consistency = lambda: setattr(
            planner, "advisor_consistency_issues", [])
        planner._advisor_binding_conflicts = lambda: {}
        return planner

    def test_prioritizes_blocked_provider_then_other_reviews(self):
        p = self.make_planner()
        candidates = p._advisor_fallback_candidates()
        self.assertEqual([s for s, _ in candidates], ["a" * 40, "b" * 40])
        self.assertIn("dependency:DT_INCLUDE->REVIEW", candidates[0][1])

    def test_full_two_phase_changes_valid_review_to_take(self):
        p = self.make_planner()
        calls = []
        class Primary:
            def unload(self): calls.append("unload-primary")
        def fake_evaluate(client, prompt, sha, **kwargs):
            calls.append((p.advisor_model, sha))
            self.assertEqual(p.args.advisor_max_request_chars, 120000)
            self.assertEqual(p.args.advisor_context_tokens, 131072)
            p.nodes[sha].advisor_decision = "TAKE"
            p.nodes[sha].advisor_rationale = "verified source prerequisites"
            p.nodes[sha].advisor_error = None
            p.nodes[sha].advisor_error_kind = None
            p.nodes[sha].advisor_decision_model = p.advisor_model
        p._advisor_evaluate_one = fake_evaluate
        with mock.patch.object(kbp, "OllamaAdvisor") as client:
            p._advisor_fallback_pass(Primary())
        self.assertEqual(calls[0], "unload-primary")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(item[0] == "gemma4-kernel:128k"
                            for item in calls[1:]))
        self.assertEqual(p.advisor_fallback_evaluated, 2)
        self.assertEqual(p.advisor_fallback_accepted, 2)
        self.assertEqual(p.advisor_fallback_changed, 2)
        self.assertEqual(p.advisor_fallback_take, 2)
        self.assertEqual(p.nodes["a" * 40].advisor_primary_result["advisor_decision"],
                         "REVIEW")
        self.assertEqual(p.nodes["a" * 40].advisor_decision_model,
                         "gemma4-kernel:128k")
        self.assertEqual(p.advisor_model, "kernel-planner:32k")
        self.assertEqual(p.args.advisor_max_request_chars, 48000)
        self.assertEqual(p.args.advisor_context_tokens, 32768)
        client.assert_called_once()

    def test_fallback_failure_preserves_primary_error_and_decision(self):
        p = self.make_planner()
        first = p.nodes["a" * 40]
        first.advisor_error_kind = "policy-review"
        first.advisor_error = "Qwen policy objection"
        p.advisor_failures = 1
        p.advisor_failure_kinds["policy-review"] = 1
        p.advisor_model = p.advisor_fallback_model
        def fake_evaluate(client, prompt, sha, **kwargs):
            p._advisor_note_failure(p.nodes[sha], kbp.AdvisorError(
                "Gemma timeout", kind="timeout"))
        p._advisor_evaluate_one = fake_evaluate
        p._advisor_fallback_one(None, "policy", "a" * 40, ["review"])
        self.assertEqual(first.advisor_error_kind, "policy-review")
        self.assertEqual(first.advisor_rationale, "Unresolved API compatibility with downstream")
        self.assertEqual(first.advisor_decision_model, "kernel-planner:32k")
        self.assertEqual(first.advisor_fallback_result["advisor_error_kind"],
                         "timeout")
        self.assertEqual(p.advisor_fallback_failures, 1)
        self.assertEqual(p.advisor_failures, 1)
        self.assertEqual(dict(p.advisor_failure_kinds), {"policy-review": 1})

    def test_fallback_skip_does_not_override_review(self):
        p = self.make_planner()
        p.advisor_model = p.advisor_fallback_model
        def fake_evaluate(client, prompt, sha, **kwargs):
            p.nodes[sha].advisor_decision = "SKIP"
            p.nodes[sha].advisor_decision_model = p.advisor_model
        p._advisor_evaluate_one = fake_evaluate
        p._advisor_fallback_one(None, "policy", "a" * 40, ["review"])
        self.assertEqual(p.nodes["a" * 40].advisor_decision, "REVIEW")
        self.assertEqual(p.nodes["a" * 40].advisor_fallback_result["advisor_decision"],
                         "SKIP")
        self.assertEqual(p.advisor_fallback_accepted, 0)

    def test_limit_does_not_silently_make_report_execution_ready(self):
        p = self.make_planner()
        p.args.advisor_fallback_limit = 1
        p._advisor_evaluate_one = lambda *a, **kw: None
        class Primary:
            def unload(self): pass
        with mock.patch.object(kbp, "OllamaAdvisor"):
            p._advisor_fallback_pass(Primary())
        self.assertEqual(p.advisor_fallback_unprocessed, 1)
        self.assertIn("ADVISOR_FALLBACK->INCOMPLETE",
                      [x["kind"] for x in p.advisor_consistency_issues])

    def test_model_specific_cache_fingerprint(self):
        p = self.make_planner()
        p.base, p.target, p.downstream = "base", "target", "downstream"
        packet = {"commit": {"sha": "a" * 40}}
        key1 = p.advisor_cache_key("a" * 40, packet)
        p.advisor_model = p.advisor_fallback_model
        key2 = p.advisor_cache_key("a" * 40, packet)
        self.assertNotEqual(key1, key2)

    def test_unload_uses_ollama_api_without_generating(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{}'
        def urlopen(request, timeout):
            self.assertEqual(request.full_url, "http://gondor:11434/api/chat")
            body = json.loads(request.data)
            self.assertEqual(body, {"model": "kernel-planner:32k",
                "messages": [], "keep_alive": 0, "stream": False})
            return Response()
        client = kbp.OllamaAdvisor("kernel-planner:32k",
            "http://gondor:11434", 100, 0, "10m")
        with mock.patch.object(kbp.urllib.request, "urlopen", urlopen):
            client.unload()

    def test_fallback_model_is_verified_before_collection(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({"models": [
                {"name": "gemma4-kernel:128k"}]}).encode()
        with mock.patch.object(kbp.urllib.request, "urlopen",
                               return_value=Response()):
            kbp.verify_advisor_fallback_model("http://gondor:11434",
                                              "gemma4-kernel:128k")
            with self.assertRaises(SystemExit):
                kbp.verify_advisor_fallback_model("http://gondor:11434",
                                                  "missing:latest")

    def test_cli_requires_distinct_primary_and_fallback(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                kbp.parse_args(["--advisor-fallback-model", "gemma4:26b"])
            with self.assertRaises(SystemExit):
                kbp.parse_args(["--advisor-model", "x",
                                "--advisor-fallback-model", "x"])

    def test_per_model_performance_counters(self):
        p = kbp.PerformanceRecorder()
        p.advisor("a" * 40, 1, 2.5, model="kernel-planner:32k",
                  stage="primary")
        p.advisor("b" * 40, 1, 7.5, model="gemma4-kernel:128k",
                  stage="fallback", status="failed")
        summary = p.summary()
        stats = summary["advisor"]["per_model"]
        self.assertEqual(stats["kernel-planner:32k"]["requests"], 1)
        self.assertEqual(stats["gemma4-kernel:128k"]["failed_attempts"], 1)
        self.assertEqual(stats["gemma4-kernel:128k"]["seconds"], 7.5)

    def test_secondary_failure_without_primary_error_does_not_invent_one(self):
        p = self.make_planner()
        p.advisor_model = p.advisor_fallback_model
        def fake_evaluate(client, prompt, sha, **kwargs):
            p._advisor_note_failure(p.nodes[sha], kbp.AdvisorError(
                "No response", kind="timeout"))
        p._advisor_evaluate_one = fake_evaluate
        p._advisor_fallback_one(None, "policy", "a" * 40, ["review"])
        node = p.nodes["a" * 40]
        self.assertEqual(node.advisor_decision, "REVIEW")
        self.assertIsNone(node.advisor_error_kind)
        self.assertEqual(p.advisor_failures, 0)
        self.assertFalse(p.advisor_failure_kinds)


class FallbackRecovery0424Tests(unittest.TestCase):
    """Real run 27 failure pattern: early HTTP 500, later healthy Gemma."""

    def make_planner(self):
        planner = DualAdvisor0423Tests().make_planner()
        planner.advisor_fallback_deferred_retries = 0
        planner.advisor_fallback_recovered = 0
        planner.advisor_fallback_warmup_error = None
        planner.args.advisor_fallback_context_tokens = 65536
        return planner

    def test_warmup_preloads_selected_context_and_no_generation(self):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"done": true}'

        def urlopen(req, timeout):
            requests.append((req.full_url, json.loads(req.data), timeout))
            return Response()

        client = kbp.OllamaAdvisor("gemma4-kernel:64k",
                                  "http://gondor:11434", 900, 0, "10m")
        with mock.patch.object(kbp.urllib.request, "urlopen", urlopen):
            client.warmup(65536)
        self.assertEqual(requests, [("http://gondor:11434/api/generate", {
            "model": "gemma4-kernel:64k", "prompt": "", "stream": False,
            "keep_alive": "10m", "options": {"num_ctx": 65536},
        }, 900)])

    def test_http_errors_include_ollama_diagnostic_body(self):
        from urllib.error import HTTPError

        error = HTTPError("http://gondor:11434/api/generate", 500,
                          "Internal Server Error", {},
                          io.BytesIO(b'{"error":"CUDA error: out of memory"}'))
        client = kbp.OllamaAdvisor("gemma4-kernel:64k",
                                  "http://gondor:11434", 900, 0, "10m")
        with mock.patch.object(kbp.urllib.request, "urlopen",
                               side_effect=error):
            with self.assertRaises(kbp.AdvisorError) as caught:
                client.warmup(65536)
        self.assertIn("CUDA error: out of memory", str(caught.exception))

    def test_deferred_retry_recovers_early_http_failure_after_success(self):
        p = self.make_planner()
        calls = []

        class Primary:
            def unload(self): calls.append("unload")

        def fake_evaluate(client, prompt, sha, **kwargs):
            calls.append(sha)
            node = p.nodes[sha]
            if sha == "a" * 40 and calls.count(sha) == 1:
                p._advisor_note_failure(node, kbp.AdvisorError(
                    "Ollama HTTP error 500: CUDA out of memory", kind="http"))
            else:
                node.advisor_decision = "TAKE"
                node.advisor_error = None
                node.advisor_error_kind = None
                node.advisor_decision_model = p.advisor_model

        p._advisor_evaluate_one = fake_evaluate
        with mock.patch.object(kbp, "OllamaAdvisor") as fallback:
            p._advisor_fallback_pass(Primary())
            fallback.return_value.warmup.assert_called_once_with(65536)
        self.assertEqual(calls, ["unload", "a" * 40, "b" * 40,
                                 "a" * 40])
        self.assertEqual(p.advisor_fallback_deferred_retries, 1)
        self.assertEqual(p.advisor_fallback_recovered, 1)
        self.assertEqual(p.advisor_fallback_failures, 0)
        self.assertEqual(p.advisor_fallback_take, 2)
        self.assertEqual(p.advisor_fallback_accepted, 2)
        self.assertEqual(len(p.nodes["a" * 40].advisor_fallback_history), 2)
        self.assertEqual(p.nodes["a" * 40].advisor_primary_result[
            "advisor_decision"], "REVIEW")
        self.assertEqual(p.advisor_model, "kernel-planner:32k")

    def test_no_repeat_if_all_fallback_requests_fail(self):
        p = self.make_planner()
        calls = []

        class Primary:
            def unload(self): pass

        def fake_evaluate(client, prompt, sha, **kwargs):
            calls.append(sha)
            p._advisor_note_failure(p.nodes[sha], kbp.AdvisorError(
                "Ollama HTTP error 500", kind="http"))

        p._advisor_evaluate_one = fake_evaluate
        with mock.patch.object(kbp, "OllamaAdvisor"):
            p._advisor_fallback_pass(Primary())
        self.assertEqual(len(calls), 2)
        self.assertEqual(p.advisor_fallback_deferred_retries, 0)
        self.assertEqual(p.advisor_fallback_failures, 2)
        self.assertTrue(all(n.advisor_decision == "REVIEW"
                            for n in p.nodes.values()))

    def test_validation_failures_are_not_late_retried(self):
        p = self.make_planner()
        calls = []

        class Primary:
            def unload(self): pass

        def fake_evaluate(client, prompt, sha, **kwargs):
            calls.append(sha)
            node = p.nodes[sha]
            if sha == "a" * 40:
                p._advisor_note_failure(node, kbp.AdvisorError(
                    "binding evidence unassessed", kind="binding-unassessed"))
            else:
                node.advisor_decision = "TAKE"
                node.advisor_error_kind = None
                node.advisor_error = None

        p._advisor_evaluate_one = fake_evaluate
        with mock.patch.object(kbp, "OllamaAdvisor"):
            p._advisor_fallback_pass(Primary())
        self.assertEqual(calls, ["a" * 40, "b" * 40])
        self.assertEqual(p.advisor_fallback_deferred_retries, 0)
        self.assertEqual(p.advisor_fallback_failures, 1)

    def test_deferred_failure_remains_failed_and_is_bounded(self):
        p = self.make_planner()
        calls = []

        class Primary:
            def unload(self): pass

        def fake_evaluate(client, prompt, sha, **kwargs):
            calls.append(sha)
            node = p.nodes[sha]
            if sha == "a" * 40:
                p._advisor_note_failure(node, kbp.AdvisorError(
                    "CUDA out of memory", kind="http"))
            else:
                node.advisor_decision = "TAKE"
                node.advisor_error_kind = None
                node.advisor_error = None

        p._advisor_evaluate_one = fake_evaluate
        with mock.patch.object(kbp, "OllamaAdvisor"):
            p._advisor_fallback_pass(Primary())
        self.assertEqual(calls, ["a" * 40, "b" * 40, "a" * 40])
        self.assertEqual(p.advisor_fallback_deferred_retries, 1)
        self.assertEqual(p.advisor_fallback_recovered, 0)
        self.assertEqual(p.advisor_fallback_failures, 1)
        self.assertEqual(len(p.nodes["a" * 40].advisor_fallback_history), 2)

    def test_binding_retry_names_exact_missing_evidence_ids(self):
        message = ("binding evidence marked REQUIRED without prerequisite "
                   "selection: ev-123456")
        prompt = kbp.binding_retry_prompt("base prompt", message)
        self.assertIn(message, prompt)
        self.assertIn("Include REQUIRED", prompt)

    def test_binding_validator_feedback_reaches_retry_request(self):
        p = object.__new__(kbp.Planner)
        p.args = types.SimpleNamespace(
            advisor_retries=1, advisor_retry_delay=0,
            advisor_context_tokens=0, advisor_output_reserve=1024)
        sha = "a" * 40
        p.nodes = {sha: kbp.CommitNode(sha, "board")}
        p.advisor_calls = 0
        p.advisor_retries_used = 0
        p.info = lambda msg: None
        p.advisor_prerequisite_evidence = lambda _sha: []
        p.advisor_inbound_take_recommendations = lambda _sha: []
        p.validate_advisor_result = lambda _sha, result: result
        p._advisor_hard_dt_include_facts = lambda _sha: []
        p.performance = None
        p.advisor_trace_path = None
        p.advisor_error_log_path = None

        class Client:
            def __init__(self):
                self.prompts = []
            def advise(self, prompt, packet, schema):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    raise kbp.AdvisorError(
                        "binding evidence marked REQUIRED without "
                        "prerequisite selection: ev-abc123",
                        kind="binding-unassessed", retryable=True)
                return {"decision": "TAKE", "rationale": "valid", "risks": []}

        client = Client()
        result = p._advisor_call_with_retries(
            client, "original", sha, {"commit": {"sha": sha}})
        self.assertEqual(result["decision"], "TAKE")
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("ev-abc123", client.prompts[1])


class WorksheetNaming0425Tests(unittest.TestCase):
    """The worksheet is a text file paired with an immutable JSON plan."""

    def test_standard_run_uses_distinct_worksheet_and_plan_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "backport_plan_29"
            worksheet = root / "backport_worksheet_29.txt"
            args = kbp.parse_args([
                "--base", "v6.19", "--target", "v7.0",
                "--worksheet-output", str(worksheet),
                "--json-output", str(plan.with_suffix(".json")),
            ])
            self.assertEqual(kbp.auto_package_prefix(args), plan)
            self.assertEqual(kbp.worksheet_path_for_plan(plan), worksheet)
            self.assertEqual(kbp.plan_prefix_from_argument(str(worksheet)), plan)

    def test_other_run_number_is_rejected_before_git_collection(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                kbp.parse_args([
                    "--base", "v6.19", "--target", "v7.0",
                    "--worksheet-output", "backport_worksheet_28.txt",
                    "--json-output", "backport_plan_29.json",
                ])
        self.assertEqual(caught.exception.code, 2)

    def test_worksheet_must_be_plain_text_extension(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                kbp.parse_args([
                    "--base", "v6.19", "--target", "v7.0",
                    "--worksheet-output", "backport_worksheet_29.md",
                    "--json-output", "backport_plan_29.json",
                ])
        self.assertEqual(caught.exception.code, 2)

    def test_package_from_worksheet_uses_plan_archive_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "backport_plan_29"
            worksheet = root / "backport_worksheet_29.txt"
            worksheet.write_text("editable worksheet\n")
            plan.with_suffix(".json").write_text('{"advisor": null}\n')
            plan.with_suffix(".log").write_text("completed run\n")
            directory, archive = kbp.package_results(str(worksheet))
            self.assertEqual(directory, plan)
            self.assertEqual(archive, plan.with_suffix(".zip"))
            with zipfile.ZipFile(archive) as zf:
                self.assertIn("backport_plan_29/backport_worksheet_29.txt",
                              zf.namelist())
                self.assertIn("backport_plan_29/backport_plan_29.json",
                              zf.namelist())
                self.assertEqual(zf.read(
                    "backport_plan_29/backport_worksheet_29.txt"),
                    b"editable worksheet\n")
            worksheet.write_text("engineer annotation\n")
            self.assertEqual(zf_bytes_from_archive(archive),
                             b"editable worksheet\n")
            self.assertEqual(worksheet.read_text(), "engineer annotation\n")


def zf_bytes_from_archive(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read("backport_plan_29/backport_worksheet_29.txt")


class WorkspaceResults0426Tests(unittest.TestCase):
    """Workspace-owned, numbered, self-contained planning runs (0.4.26)."""

    @staticmethod
    def fake_planner():
        planner = mock.Mock()
        planner.nodes = {}
        planner.run_check_command.return_value = None
        planner.report_text.return_value = (
            'abcdef123456 ("arm64: sample")\n'
            '      advisor: TAKE\n      advisor-rationale: preserve evidence\n'
        )
        planner.report_json.return_value = json.dumps({
            "advisor": None, "order": [], "commits": {},
            "baseline": "a" * 40,
        }) + "\n"
        return planner

    def run_auto(self, workspace, extra=()):
        with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(workspace)}):
            with mock.patch.object(kbp, "Planner", return_value=self.fake_planner()):
                with mock.patch.object(kbp.sys, "stdout", new_callable=io.StringIO):
                    return kbp.main(["--base", "v6.19", "--target", "v7.0", *extra])

    def test_legacy_flat_runs_count_toward_new_run_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backport_plan_27.json").touch()
            (root / "backport_plan_28.zip").touch()
            self.assertEqual(self.run_auto(root), 0)
            folder = root / "backport-plans" / "backport_plan_29"
            archive = root / "backport-plans" / "backport_plan_29.zip"
            self.assertTrue(folder.is_dir())
            self.assertTrue(archive.is_file())
            expected = {
                "backport_plan.json", "backport_worksheet.txt", "backport_plan.log",
                "performance.json", "performance.jsonl",
            }
            self.assertEqual({p.name for p in folder.iterdir()}, expected)
            self.assertIn("advisor-rationale: preserve evidence",
                          (folder / "backport_worksheet.txt").read_text())
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(set(zf.namelist()), {
                    f"backport_plan_29/{name}" for name in expected
                })
                self.assertIn("Planner elapsed:", zf.read(
                    "backport_plan_29/backport_plan.log").decode())

    def test_next_run_does_not_overwrite_previous_engineer_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_auto(root), 0)
            first = root / "backport-plans" / "backport_plan_01"
            worksheet = first / "backport_worksheet.txt"
            worksheet.write_text("manually resolved conflict in drivers/foo.c\n")
            self.assertEqual(self.run_auto(root), 0)
            self.assertTrue((root / "backport-plans" / "backport_plan_02").is_dir())
            self.assertEqual(worksheet.read_text(),
                             "manually resolved conflict in drivers/foo.c\n")
            with zipfile.ZipFile(first.with_suffix(".zip")) as zf:
                self.assertNotIn("manually resolved conflict", zf.read(
                    "backport_plan_01/backport_worksheet.txt").decode())

    def test_existing_incomplete_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial = root / "backport-plans" / "backport_plan_03"
            partial.mkdir(parents=True)
            (partial / "backport_worksheet.txt").write_text("interrupted\n")
            self.assertEqual(self.run_auto(root), 0)
            self.assertTrue((root / "backport-plans" / "backport_plan_04").exists())
            self.assertEqual((partial / "backport_worksheet.txt").read_text(),
                             "interrupted\n")

    def test_explicit_paths_remain_legacy_mode_even_with_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = root / "test.txt"
            plan = root / "test.json"
            self.assertEqual(self.run_auto(root, [
                "--worksheet-output", str(text), "--json-output", str(plan)
            ]), 0)
            self.assertTrue(text.is_file())
            self.assertTrue(plan.is_file())
            self.assertTrue((root / "test.zip").is_file())
            self.assertFalse((root / "backport-plans").exists())

    def test_advisor_trace_short_flag_uses_workspace_generic_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = kbp.parse_args(["--base", "v6.19", "--target", "v7.0",
                                   "--advisor-trace"])
            self.assertEqual(args.advisor_trace, "auto")
            path = kbp.reserve_workspace_run(args, root)
            self.assertEqual(Path(args.advisor_trace), path / "advisor-trace.jsonl")
            self.assertEqual(Path(args.advisor_cache), path / "advisor-cache.json")
            self.assertEqual(Path(args.advisor_error_log), path / "advisor-errors.jsonl")
            self.assertEqual(Path(args.performance_event_path), path / "performance.jsonl")
            self.assertEqual(Path(args.performance_summary_path), path / "performance.json")
            self.assertIsNone(kbp.auto_package_prefix(args))

    def test_explicit_trace_path_rejected_in_automatic_mode_before_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(root)}):
                with mock.patch.object(kbp, "Planner") as planner:
                    self.assertEqual(kbp.main([
                        "--base", "v6.19", "--target", "v7.0",
                        "--advisor-trace", str(root / "outside.jsonl"),
                    ]), 1)
                planner.assert_not_called()
            self.assertFalse((root / "backport-plans").exists())

    def test_status_and_benchmark_modes_do_not_allocate_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = kbp.parse_args(["--package-results", "not-present"])
            self.assertEqual(args.package_results, "not-present")
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(root)}):
                self.assertEqual(kbp.main(["--package-results", "not-present"]), 1)
            self.assertFalse((root / "backport-plans").exists())

    def test_post_run_repackage_workspace_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_auto(root), 0)
            folder = root / "backport-plans" / "backport_plan_01"
            archive = folder.with_suffix(".zip")
            archive.unlink()
            self.assertEqual(kbp.main(["--package-results", str(folder)]), 0)
            self.assertTrue(archive.exists())
            self.assertEqual(kbp.main(["--package-results", str(folder)]), 1)

    def test_missing_workspace_env_falls_back_to_standard_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": ""}):
                with mock.patch.object(kbp, "Planner", return_value=self.fake_planner()):
                    with mock.patch.object(kbp.sys, "stdout", new_callable=io.StringIO) as out:
                        rc = kbp.main(["--base", "v6.19", "--target", "v7.0"])
                self.assertEqual(rc, 0)
                self.assertIn('abcdef123456 ("arm64: sample")', out.getvalue())
            self.assertFalse((Path(tmp) / "backport-plans").exists())

    def test_invalid_workspace_fails_without_running_git_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "missing"
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(invalid)}):
                with mock.patch.object(kbp, "Planner") as planner:
                    self.assertEqual(kbp.main(["--base", "v6.19", "--target", "v7.0"]), 1)
                planner.assert_not_called()
            self.assertFalse(invalid.exists())

    def test_numbered_dir_packaging_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "backport_plan_01"
            result.mkdir()
            for name in (kbp.WORKSPACE_PLAN, kbp.WORKSPACE_WORKSHEET,
                         kbp.WORKSPACE_LOG, kbp.WORKSPACE_PERFORMANCE,
                         kbp.WORKSPACE_PERFORMANCE_EVENTS):
                (result / name).write_text("data\n")
            (result / kbp.WORKSPACE_PLAN).write_text("{}\n")
            (result / "outside").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "symlink"):
                kbp.package_workspace_run(result)
            self.assertFalse(result.with_suffix(".zip").exists())

    def test_no_auto_package_opt_out_leaves_workspace_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(root)}):
                with mock.patch.object(kbp, "Planner", return_value=self.fake_planner()):
                    with mock.patch.object(kbp.sys, "stdout", new_callable=io.StringIO) as out:
                        rc = kbp.main(["--base", "v6.19", "--target", "v7.0",
                                       "--no-auto-package"])
            self.assertEqual(rc, 0)
            self.assertIn('abcdef123456 ("arm64: sample")', out.getvalue())
            self.assertFalse((root / "backport-plans").exists())

    def test_invalid_plan_json_is_not_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "backport_plan_01"
            folder.mkdir()
            for name in (kbp.WORKSPACE_PLAN, kbp.WORKSPACE_WORKSHEET,
                         kbp.WORKSPACE_LOG, kbp.WORKSPACE_PERFORMANCE,
                         kbp.WORKSPACE_PERFORMANCE_EVENTS):
                (folder / name).write_text("not a valid plan\n")
            with self.assertRaisesRegex(ValueError, "cannot read plan JSON"):
                kbp.package_workspace_run(folder)
            self.assertFalse(folder.with_suffix(".zip").exists())

    def test_workspace_capture_and_cache_are_archived_under_generic_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = self.fake_planner()

            def construct(args):
                self.assertEqual(Path(args.advisor_trace).name,
                                 "advisor-trace.jsonl")
                Path(args.advisor_trace).write_text('{"event":"request"}\n')
                Path(args.advisor_cache).write_text('{}\n')
                Path(args.advisor_error_log).write_text('')
                return planner

            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(root)}):
                with mock.patch.object(kbp, "Planner", side_effect=construct):
                    self.assertEqual(kbp.main([
                        "--base", "v6.19", "--target", "v7.0",
                        "--advisor-trace",
                    ]), 0)
            zippath = root / "backport-plans" / "backport_plan_01.zip"
            with zipfile.ZipFile(zippath) as zf:
                names = set(zf.namelist())
                for name in ("advisor-trace.jsonl", "advisor-cache.json",
                             "advisor-errors.jsonl"):
                    self.assertIn("backport_plan_01/" + name, names)

    def test_interrupted_run_keeps_partial_artifacts_and_new_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(root)}):
                with mock.patch.object(kbp, "Planner", side_effect=RuntimeError("interrupted")):
                    with self.assertRaisesRegex(RuntimeError, "interrupted"):
                        kbp.main(["--base", "v6.19", "--target", "v7.0"])
                failed = root / "backport-plans" / "backport_plan_01"
                self.assertTrue((failed / "backport_plan.log").is_file())
                self.assertTrue((failed / "performance.jsonl").is_file())
                self.assertFalse(failed.with_suffix(".zip").exists())
                with mock.patch.object(kbp, "Planner", return_value=self.fake_planner()):
                    self.assertEqual(kbp.main(["--base", "v6.19",
                                               "--target", "v7.0"]), 0)
                self.assertTrue((root / "backport-plans" /
                                 "backport_plan_02.zip").exists())


class WorkspaceCleanup0427Tests(unittest.TestCase):
    """Only old, archived, byte-identical copies may be removed."""

    @staticmethod
    def old_run(root, number, name, data):
        stem = f"backport_plan_{number:02d}"
        folder = root / stem
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(data)
        (root / name).write_bytes(data)
        with zipfile.ZipFile(root / f"{stem}.zip", "w") as zf:
            zf.writestr(f"{stem}/{name}", data)
        return root / name

    def test_preview_keeps_files_and_apply_removes_only_verified_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copied = self.old_run(root, 28, "backport_plan_28.json", b"plan\n")
            edited = self.old_run(root, 27, "backport_plan_27.c", b"original\n")
            edited.write_text("engineer's worksheet notes\n")
            unrelated = root / "qwen-policy-test-01.jsonl"
            unrelated.write_text('benchmark data\n')
            # Original ZIP archive and results directory remain untouched.
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": str(root)}):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(kbp.main(["--cleanup-workspace-duplicates"]), 0)
                self.assertIn("Would remove:", output.getvalue())
                self.assertTrue(copied.is_file())
                with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(kbp.main([
                        "--cleanup-workspace-duplicates", "--apply-cleanup"
                    ]), 0)
                self.assertIn("Removed verified duplicate", output.getvalue())
            self.assertFalse(copied.exists())
            self.assertTrue(edited.is_file())
            self.assertTrue(unrelated.is_file())
            self.assertTrue((root / "backport_plan_28.zip").is_file())
            self.assertEqual((root / "backport_plan_28" /
                              "backport_plan_28.json").read_bytes(), b"plan\n")

    def test_missing_archive_or_mismatch_is_never_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            no_zip = root / "backport_plan_25.log"
            folder = root / "backport_plan_25"
            folder.mkdir()
            (folder / no_zip.name).write_text("matching\n")
            no_zip.write_text("matching\n")
            mismatched = self.old_run(root, 26, "backport_plan_26.perf.jsonl", b"old\n")
            # The destination changed after the archive was created.
            (root / "backport_plan_26" / mismatched.name).write_bytes(b"new\n")
            # Do not traverse symlinks even when the target would match.
            symlink = root / "backport_plan_26.json"
            symlink.symlink_to(no_zip)
            eligible, removed = kbp.cleanup_workspace_duplicates(root, apply=True)
            self.assertEqual(eligible, [])
            self.assertEqual(removed, [])
            self.assertTrue(no_zip.is_file())
            self.assertTrue(mismatched.is_file())
            self.assertTrue(symlink.is_symlink())

    def test_apply_cleanup_requires_explicit_preview_mode(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as exc:
                kbp.parse_args(["--apply-cleanup"])
        self.assertEqual(exc.exception.code, 2)


class PlannerCompletionSound0427Tests(unittest.TestCase):
    """Builder-compatible sounds must never influence planner exit status."""

    def test_success_and_failure_files_and_player_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "complete.oga").write_bytes(b"sound")
            (root / "service-logout.oga").write_bytes(b"sound")
            results = [mock.Mock(returncode=1), mock.Mock(returncode=0)]
            with mock.patch.dict(kbp.os.environ, {"KBP_SOUND_DIR": str(root)}):
                with mock.patch.object(kbp.shutil, "which", side_effect=lambda n: "/bin/" + n):
                    with mock.patch.object(kbp.subprocess, "run", side_effect=results) as player:
                        kbp.play_planner_completion_sound(True)
                        self.assertEqual(player.call_count, 2)
                        self.assertEqual(player.call_args_list[0].args[0][-1],
                                         str(root / "complete.oga"))
                        self.assertEqual(player.call_args_list[1].args[0][-1],
                                         str(root / "complete.oga"))
                    with mock.patch.object(kbp.subprocess, "run", return_value=mock.Mock(returncode=0)) as player:
                        kbp.play_planner_completion_sound(False)
                        self.assertEqual(player.call_args.args[0][-1],
                                         str(root / "service-logout.oga"))

    def test_missing_audio_is_silent(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(kbp.os.environ, {"KBP_SOUND_DIR": temp}):
                with mock.patch.object(kbp.subprocess, "run") as player:
                    kbp.play_planner_completion_sound(True)
                    player.assert_not_called()

    def test_one_sound_for_normal_run_and_nonzero_or_exception(self):
        argv = ["--base", "v6.19", "--target", "v7.0"]
        with mock.patch.object(kbp, "_run_normal_planning", return_value=0):
            with mock.patch.object(kbp, "play_planner_completion_sound") as sound:
                self.assertEqual(kbp.main(argv), 0)
                sound.assert_called_once_with(True)
        with mock.patch.object(kbp, "_run_normal_planning", return_value=2):
            with mock.patch.object(kbp, "play_planner_completion_sound") as sound:
                self.assertEqual(kbp.main(argv), 2)
                sound.assert_called_once_with(False)
        with mock.patch.object(kbp, "_run_normal_planning", side_effect=RuntimeError("failed")):
            with mock.patch.object(kbp, "play_planner_completion_sound") as sound:
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    kbp.main(argv)
                sound.assert_called_once_with(False)

    def test_maintenance_mode_does_not_play(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(kbp.os.environ, {"KERNEL_WORKSPACE_DIR": temp}):
                with mock.patch.object(kbp, "play_planner_completion_sound") as sound:
                    self.assertEqual(kbp.main(["--cleanup-workspace-duplicates"]), 0)
                    sound.assert_not_called()

class UserPlatformProfile0429Tests(unittest.TestCase):
    """A user-maintained scope must not turn path provenance into proof."""

    @staticmethod
    def profile_data():
        return {
            "version": 1, "name": "example public/product scope",
            "unmatched_board": "SKIP",
            "platforms": [
                {"name": "Selected ARM board", "architecture": "arm64",
                 "paths": ["arch/arm64/boot/dts/vendor/selected-board*.dts"],
                 "identifiers": {"dt_compatible": ["vendor,selected-board"]}},
                {"name": "Selected x86 system", "architecture": "x86",
                 "paths": ["drivers/platform/x86/selected-*.c"],
                 "identifiers": {"acpi_hid": ["VEND0001"],
                                 "pci_id": ["1234:abcd"]}},
            ],
        }

    def load(self, data=None, ext=".json"):
        data = self.profile_data() if data is None else data
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("platforms" + ext)
            if ext == ".json":
                path.write_text(json.dumps(data))
            else:
                try:
                    import yaml
                except ImportError:
                    self.skipTest("PyYAML is not installed")
                path.write_text(yaml.safe_dump(data))
            return kbp.load_platform_profile(path)

    def planner(self, patch=""):
        import types
        p = object.__new__(kbp.Planner)
        p.platform_profile = self.load()
        p._platform_match_cache = {}
        p._platform_patch_line_cache = {}
        p.git = types.SimpleNamespace(run=lambda args: patch)
        p.nodes = {}
        p.changed_files = lambda sha: p.nodes[sha].files
        p._tree_file_text = lambda sha, path: None
        return p

    def test_json_profile_validates_and_hashes(self):
        profile = self.load()
        self.assertEqual(len(profile.platforms), 2)
        self.assertEqual(profile.unmatched_board, "SKIP")
        self.assertEqual(len(profile.sha256), 64)
        self.assertEqual(profile.platforms[1].pci_id, ("1234:abcd",))

    def test_yaml_profile_matches_json_schema(self):
        self.assertEqual(self.load(ext=".yaml").platforms,
                         self.load(ext=".json").platforms)

    def test_invalid_profiles_fail_closed(self):
        cases = [
            {**self.profile_data(), "version": 2},
            {**self.profile_data(), "version": True},
            {**self.profile_data(), "platforms": []},
            {**self.profile_data(), "unmatched_board": "MAYBE"},
            {**self.profile_data(), "platforms": [
                {"name": "Test", "architecture": "arm64", "paths": ["../secret"]}]},
            {**self.profile_data(), "platforms": [
                {"name": "Test", "architecture": "arm64", "identifiers":
                 {"pci_id": ["not-a-device"]}}]},
            {**self.profile_data(), "platforms": [
                {"name": "Test", "architecture": "mystery", "paths": ["foo.c"]}]},
            {**self.profile_data(), "platforms": [
                {"name": "Test", "architecture": "arm64", "paths": ["a.c"]},
                {"name": "Test", "architecture": "arm64", "paths": ["b.c"]}]},
            {**self.profile_data(), "extra_setting": "silently ignore me"},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)

    def test_path_match_works_in_selected_arch(self):
        p = self.planner()
        sha = "a" * 40
        p.nodes[sha] = kbp.CommitNode(sha, "selected board", direct=True,
                                     files=["arch/arm64/boot/dts/vendor/selected-board-a.dts"])
        result = p.platform_relevance(sha)
        self.assertEqual(result["status"], "DIRECT")
        self.assertEqual(result["matches"][0]["platform"], "Selected ARM board")

    def test_added_compatible_match_but_removed_compatible_not_match(self):
        sha = "b" * 40
        path = "arch/arm64/boot/dts/vendor/other-board.dts"
        p = self.planner('''diff --git a/file b/file
--- a/file
+++ b/file
@@ -1,1 +1,1 @@
- compatible = "vendor,selected-board";
+ compatible = "vendor,other-board";
''')
        p.nodes[sha] = kbp.CommitNode(sha, "remove compat", files=[path])
        self.assertEqual(p.platform_relevance(sha)["status"], "UNMATCHED_BOARD")
        p._platform_match_cache.clear()
        p._platform_patch_line_cache.clear()
        p.git.run = lambda _: '+++ b/file\n+compatible = "vendor,selected-board";'
        self.assertEqual(p.platform_relevance(sha)["status"], "DIRECT")

    def test_existing_board_compatible_is_detected_without_added_line(self):
        sha = "0" * 40
        path = "arch/arm64/boot/dts/vendor/legacy-board.dts"
        p = self.planner('+ status = "okay";')
        p.nodes[sha] = kbp.CommitNode(sha, "fix selected board", files=[path])
        p._tree_file_text = lambda s, f: '''
            / {
                compatible = "vendor,selected-board", "vendor,legacy-soc";
                status = "okay";
            };
        '''
        result = p.platform_relevance(sha)
        self.assertEqual(result["status"], "DIRECT")
        self.assertEqual(result["matches"][0]["evidence"],
                         "source-dt-compatible:vendor,selected-board")

    def test_acpi_and_pci_are_not_arm_specific(self):
        sha = "c" * 40
        p = self.planner('+ { "VEND0001", 0 },\n+{ PCI_DEVICE(0x1234, 0xabcd) },')
        p.nodes[sha] = kbp.CommitNode(
            sha, "x86 identifiers", files=["drivers/platform/x86/unrelated.c"])
        matches = p.platform_relevance(sha)["matches"]
        self.assertEqual({m["evidence"] for m in matches},
                         {"added-acpi-hid:VEND0001", "added-pci-id:1234:abcd"})

    def test_board_leaf_out_of_scope_but_shared_dtsi_unknown(self):
        p = self.planner()
        board = "d" * 40
        shared = "e" * 40
        p.nodes[board] = kbp.CommitNode(
            board, "unselected board", files=["arch/arm64/boot/dts/vendor/other-board.dts"])
        p.nodes[shared] = kbp.CommitNode(
            shared, "shared soc", files=["arch/arm64/boot/dts/vendor/soc.dtsi"])
        self.assertEqual(p.platform_relevance(board)["status"], "UNMATCHED_BOARD")
        self.assertEqual(p.platform_relevance(shared)["status"], "SHARED_OR_UNKNOWN")

    def test_provenance_consumer_is_candidate_not_verified(self):
        p = self.planner()
        dep, consumer = "f" * 40, "a" * 40
        p.nodes[dep] = kbp.CommitNode(dep, "generic prereq", files=["drivers/pci/host.c"])
        p.nodes[consumer] = kbp.CommitNode(
            consumer, "selected board", files=["arch/arm64/boot/dts/vendor/selected-board-a.dts"])
        p.nodes[dep].add_reason("conflict-hunk-preimage:1", consumer)
        result = p.platform_relevance(dep)
        self.assertEqual(result["status"], "DEPENDENCY_CANDIDATE")
        self.assertEqual(result["candidate_roots"][0]["confidence"], "provenance_only")
        p.nodes[consumer].advisor_decision = "SKIP"
        self.assertEqual(p.platform_relevance(dep)["status"], "SHARED_OR_UNKNOWN")

    def test_deterministic_skip_only_unmatched_board_without_inbound(self):
        p = self.planner()
        packet = {"commit": {"platform_relevance": {"status": "UNMATCHED_BOARD"},
                             "scope": {}, "actual_inbound_take_prerequisite_count": 0}}
        result = p.advisor_policy_override("a" * 40, packet)
        self.assertEqual(result["decision"], "SKIP")
        packet["commit"]["actual_inbound_take_prerequisite_count"] = 1
        self.assertIsNone(p.advisor_policy_override("a" * 40, packet))
        p.platform_profile = kbp.PlatformProfile("test", "a" * 64, "test", (), "REVIEW")
        packet["commit"]["actual_inbound_take_prerequisite_count"] = 0
        self.assertIsNone(p.advisor_policy_override("a" * 40, packet))

    def test_mixed_or_architecture_only_commit_does_not_auto_skip(self):
        p = self.planner()
        for status in ("DIRECT", "DEPENDENCY_CANDIDATE", "SHARED_OR_UNKNOWN"):
            with self.subTest(status=status):
                packet = {"commit": {"platform_relevance": {"status": status},
                                     "scope": {}, "actual_inbound_take_prerequisite_count": 0}}
                self.assertIsNone(p.advisor_policy_override("a" * 40, packet))

    def test_model_prompt_changes_only_with_profile(self):
        p = self.planner()
        self.assertIn("USER-OWNED PLATFORM SCOPE MODE", p.advisor_system_prompt())
        self.assertIn("VERIF", p.advisor_system_prompt())
        p.platform_profile = None
        self.assertNotIn("USER-OWNED PLATFORM SCOPE MODE", p.advisor_system_prompt())
        self.assertIn("Upstream convergence", p.advisor_system_prompt())

    def test_large_profile_packet_preview_is_bounded(self):
        p = self.planner()
        example = p.platform_profile.platforms[0]
        p.platform_profile = kbp.PlatformProfile("many", "0" * 64, "file", (example,) * 40)
        info = p._platform_packet_context("a" * 40,
                                          ["arch/arm64/boot/dts/vendor/a.dts"])
        self.assertEqual(info["applicable_platform_count"], 40)
        self.assertEqual(len(info["applicable_platforms"]), 32)
        self.assertTrue(info["applicable_platforms_truncated"])

    def test_cli_requires_model_when_platform_file_supplied(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                kbp.parse_args(["--platforms", "/tmp/example.json"])


class ScopeAndFallback0431Tests(unittest.TestCase):
    """No inference of lack of product support from an incomplete allowlist."""

    def profile(self, **changes):
        data = UserPlatformProfile0429Tests.profile_data()
        data.update(changes)
        return UserPlatformProfile0429Tests().load(data)

    def planner(self, *, excluded=(), unmatched="TAKE", path=None):
        test = UserPlatformProfile0429Tests()
        data = test.profile_data()
        data["unmatched_board"] = unmatched
        data["excluded_platforms"] = list(excluded)
        p = test.planner()
        p.platform_profile = test.load(data)
        p._platform_match_cache = {}
        p._platform_excluded_match_cache = {}
        if path is not None:
            p.nodes["a" * 40] = kbp.CommitNode("a" * 40, "board change", files=[path])
        return p

    @staticmethod
    def excluded_board(pattern="arch/arm64/boot/dts/vendor/other-*.dts"):
        return {"name": "Excluded arm board", "architecture": "arm64",
                "paths": [pattern]}

    def test_unmatched_defaults_to_take_not_a_disallowlist(self):
        p = self.profile(unmatched_board="TAKE")
        self.assertEqual(p.unmatched_board, "TAKE")
        raw = UserPlatformProfile0429Tests.profile_data()
        raw.pop("unmatched_board")
        self.assertEqual(UserPlatformProfile0429Tests().load(raw).unmatched_board,
                         "TAKE")

    def test_explicit_exclusion_is_negative_board_leaf_evidence(self):
        p = self.planner(excluded=[self.excluded_board()],
                         path="arch/arm64/boot/dts/vendor/other-model.dts")
        relevance = p.platform_relevance("a" * 40)
        self.assertEqual(relevance["status"], "EXCLUDED_BOARD")
        self.assertEqual(relevance["excluded_matches"][0]["platform"],
                         "Excluded arm board")
        packet = {"commit": {"platform_relevance": relevance,
                             "scope": {}, "actual_inbound_take_prerequisite_count": 0}}
        self.assertEqual(p.advisor_policy_override("a" * 40, packet)["decision"],
                         "SKIP")
        packet["commit"]["actual_inbound_take_prerequisite_count"] = 1
        self.assertIsNone(p.advisor_policy_override("a" * 40, packet))

    def test_identifier_in_added_comment_cannot_exclude_board(self):
        p = self.planner(excluded=[{
            "name": "Excluded by DT compatible", "architecture": "arm64",
            "identifiers": {"dt_compatible": ["vendor,excluded-board"]}}],
            path="arch/arm64/boot/dts/vendor/other-board.dts")
        p.git.run = lambda args: '+ /* "vendor,excluded-board" */'
        self.assertEqual(p.platform_relevance("a" * 40)["status"],
                         "UNMATCHED_BOARD")
        p._platform_excluded_match_cache.clear()
        p._tree_file_text = lambda sha, path: (
            '/ { compatible = "vendor,excluded-board"; };')
        self.assertEqual(p.platform_relevance("a" * 40)["status"],
                         "EXCLUDED_BOARD")

    def test_unmatched_review_is_not_forced_review(self):
        p = self.planner(unmatched="REVIEW",
                         path="arch/arm64/boot/dts/vendor/other-model.dts")
        relevance = p.platform_relevance("a" * 40)
        self.assertEqual(relevance["status"], "UNMATCHED_BOARD")
        self.assertIsNone(p.advisor_policy_override("a" * 40, {
            "commit": {"platform_relevance": relevance,
                       "scope": {}, "actual_inbound_take_prerequisite_count": 0}}))
        self.assertIn("missing from the allowlist is not proof",
                      p.advisor_system_prompt().lower())

    def test_old_aggressive_unmatched_skip_is_still_opt_in(self):
        p = self.planner(unmatched="SKIP",
                         path="arch/arm64/boot/dts/vendor/other-model.dts")
        packet = {"commit": {"platform_relevance": p.platform_relevance("a" * 40),
                             "scope": {}, "actual_inbound_take_prerequisite_count": 0}}
        self.assertEqual(p.advisor_policy_override("a" * 40, packet)["decision"],
                         "SKIP")

    def test_exclusion_does_not_skip_shared_code_or_mixed_changes(self):
        p = self.planner(excluded=[self.excluded_board()])
        sha = "a" * 40
        p.nodes[sha] = kbp.CommitNode(sha, "mixed board", files=[
            "arch/arm64/boot/dts/vendor/other-model.dts",
            "arch/arm64/boot/dts/vendor/unlisted.dts"])
        self.assertEqual(p.platform_relevance(sha)["status"], "UNMATCHED_BOARD")
        p.nodes[sha].files = ["arch/arm64/boot/dts/vendor/other-model.dtsi"]
        p._platform_match_cache.clear()
        p._platform_excluded_match_cache.clear()
        self.assertEqual(p.platform_relevance(sha)["status"], "SHARED_OR_UNKNOWN")

    def test_selected_and_excluded_scope_conflict_is_never_silently_taken(self):
        p = self.planner(excluded=[self.excluded_board(
            "arch/arm64/boot/dts/vendor/selected-board*.dts")],
            path="arch/arm64/boot/dts/vendor/selected-board-a.dts")
        relevance = p.platform_relevance("a" * 40)
        self.assertEqual(relevance["status"], "SCOPE_CONFLICT")
        packet = {"commit": {"platform_relevance": relevance,
                             "scope": {}, "actual_inbound_take_prerequisite_count": 0}}
        result = p.advisor_policy_override("a" * 40, packet)
        self.assertEqual(result["decision"], "REVIEW")
        self.assertIn("conflicting", result["rationale"].lower())

    def test_exclusion_profile_is_strict_and_hash_changes(self):
        original = self.profile()
        changed = self.profile(excluded_platforms=[self.excluded_board()])
        self.assertEqual(len(changed.excluded_platforms), 1)
        self.assertNotEqual(original.sha256, changed.sha256)
        bad = UserPlatformProfile0429Tests.profile_data()
        bad["excluded_platforms"] = [{"name": "Unsafe", "architecture": "arm64",
                                      "paths": ["../secrets"]}]
        with self.assertRaisesRegex(ValueError, "unsafe repository-relative"):
            UserPlatformProfile0429Tests().load(bad)
        bad["excluded_platforms"] = [{"name": "Selected ARM board",
                                      "architecture": "arm64", "paths": ["board.dts"]}]
        with self.assertRaisesRegex(ValueError, "duplicate platform name"):
            UserPlatformProfile0429Tests().load(bad)
        bad["excluded_platforms"] = {"name": "not a list"}
        with self.assertRaisesRegex(ValueError, "excluded_platforms must be a list"):
            UserPlatformProfile0429Tests().load(bad)

    def test_technical_review_escalates_but_platform_only_does_not(self):
        p = DualAdvisor0423Tests().make_planner()
        p.advisor_consistency_issues = []
        a, b = "a" * 40, "b" * 40
        p.nodes[a].advisor_rationale = (
            "Unknown whether the board has RHEL certification or product support")
        p.nodes[b].advisor_rationale = "Unresolved API compatibility with downstream"
        cases = p._advisor_fallback_candidates()
        self.assertEqual([sha for sha, reasons in cases], [b])
        self.assertIn("technical-review", cases[0][1])

    def test_dependency_issue_and_validator_failure_still_escalate(self):
        p = DualAdvisor0423Tests().make_planner()
        p.nodes["a" * 40].advisor_rationale = "Hardware certification unknown"
        p.nodes["b" * 40].advisor_rationale = "Hardware certification unknown"
        p.nodes["b" * 40].advisor_error_kind = "evidence-budget"
        cases = p._advisor_fallback_candidates()
        self.assertEqual([sha for sha, _ in cases], ["a" * 40, "b" * 40])
        self.assertIn("advisor-error:evidence-budget", cases[1][1])

    def test_profile_prompt_bias_and_policy_recheck(self):
        p = self.planner()
        prompt = p.advisor_system_prompt()
        self.assertIn("explicit negative", prompt)
        self.assertIn("unknown board", prompt.lower())
        self.assertIn("PLATFORM POLICY RECHECK",
                      kbp.platform_policy_retry_prompt("original"))

class OpenAIAdvisor0432Tests(unittest.TestCase):
    def sample_response(self, content=None, status='completed'):
        if content is None:
            content = {
                'decision': 'TAKE', 'rationale': 'Confirmed upstream dependency.',
                'prerequisite_evidence_ids': [], 'binding_evidence_assessments': [],
                'inbound_prerequisite_assessments': [], 'risks': [],
            }
        return {
            'status': status,
            'usage': {'input_tokens': 1234, 'output_tokens': 456},
            'output': [{'type': 'reasoning', 'summary': []},
                       {'type': 'message', 'content': [
                           {'type': 'output_text', 'text': json.dumps(content)}]}],
        }

    def fake_urlopen(self, result, requests):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(result).encode('utf-8')
        def fn(request, timeout):
            requests.append((request, timeout))
            return Response()
        return fn

    def test_provider_flag_and_model_defaults(self):
        args = kbp.parse_args(['--base', 'v6.19', '--target', 'v7.0', '--advisor=openai', '--advisor-limit', '3'])
        self.assertEqual(args.advisor, 'openai')
        self.assertEqual(args.advisor_model, 'gpt-6.1-terra')
        self.assertEqual(args.advisor_fallback_provider, 'ollama')
        args = kbp.parse_args(['--base', 'v6.19', '--target', 'v7.0', '--advisor-model', 'kernel-planner:32k',
                               '--advisor-fallback-provider', 'openai',
                               '--advisor-fallback-model', 'gpt-6.1-sol'])
        self.assertIsInstance(kbp.make_advisor_client(args, args.advisor_model),
                              kbp.OllamaAdvisor)
        self.assertIsInstance(kbp.make_advisor_client(args, args.advisor_fallback_model,
                                                     fallback=True), kbp.OpenAIAdvisor)
        with mock.patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                kbp.parse_args(['--base', 'v6.19', '--target', 'v7.0', '--advisor=openai', '--openai-max-output-tokens', '0'])

    def test_request_stateless_secret_not_in_payload_and_strict_schema(self):
        sent = []
        advisor = kbp.OpenAIAdvisor('gpt-6.1-terra', 30, 5000)
        response = self.sample_response()
        schema = kbp.advisor_response_schema([], [])
        with mock.patch.dict(kbp.os.environ, {'OPENAI_API_KEY': 'secret-test-key'}):
            with mock.patch.object(kbp.urllib.request, 'urlopen',
                                   side_effect=self.fake_urlopen(response, sent)):
                answer = advisor.advise('Follow evidence.',
                                        {'commit': {'sha': 'a'*40}}, schema)
        self.assertEqual(answer['decision'], 'TAKE')
        req, timeout = sent[0]
        self.assertEqual(req.full_url, 'https://api.openai.com/v1/responses')
        self.assertEqual(timeout, 30)
        self.assertEqual(req.get_header('Authorization'), 'Bearer secret-test-key')
        payload = json.loads(req.data)
        self.assertNotIn('secret-test-key', json.dumps(payload))
        self.assertIs(payload['store'], False)
        self.assertEqual(payload['model'], 'gpt-6.1-terra')
        self.assertEqual(payload['max_output_tokens'], 5000)
        self.assertTrue(payload['text']['format']['strict'])
        self.assertNotIn('uniqueItems', json.dumps(payload['text']['format']['schema']))
        self.assertNotIn('maxItems', json.dumps(payload['text']['format']['schema']))
        self.assertEqual(advisor.last_telemetry['prompt_eval_count'], 1234)
        self.assertEqual(advisor.last_telemetry['eval_count'], 456)
        self.assertIn('schema', json.dumps(payload))
        self.assertIn('Follow evidence.', payload['instructions'])

    def test_missing_api_key_fails_before_call(self):
        advisor = kbp.OpenAIAdvisor('gpt-6.1-terra', 10)
        with mock.patch.dict(kbp.os.environ, {}, clear=True):
            with mock.patch.object(kbp.urllib.request, 'urlopen') as network:
                with self.assertRaises(kbp.AdvisorError) as ctx:
                    advisor.advise('test', {})
        self.assertEqual(ctx.exception.kind, 'configuration')
        network.assert_not_called()

    def test_incomplete_response_requires_retry_or_review(self):
        advisor = kbp.OpenAIAdvisor('gpt-6.1-terra', 10)
        with mock.patch.dict(kbp.os.environ, {'OPENAI_API_KEY': 'test'}):
            with mock.patch.object(kbp.urllib.request, 'urlopen',
                    side_effect=self.fake_urlopen(self.sample_response(status='incomplete'), [])):
                with self.assertRaises(kbp.AdvisorError) as ctx:
                    advisor.advise('test', {})
        self.assertEqual(ctx.exception.kind, 'invalid-response')

    def test_http_quota_not_retryable_but_rate_limit_is(self):
        import urllib.error
        advisor = kbp.OpenAIAdvisor('gpt-6.1-terra', 10)
        for code, body, retriable in [(429, b'{"error":{"code":"slow_down"}}', True),
                                      (429, b'{"error":{"code":"credit_balance_exhausted"}}', False),
                                      (401, b'{"error":{"code":"invalid_api_key"}}', False)]:
            exc = urllib.error.HTTPError('https://api.openai.com/v1/responses', code,
                                          'error', {}, io.BytesIO(body))
            with mock.patch.dict(kbp.os.environ, {'OPENAI_API_KEY': 'test'}):
                with mock.patch.object(kbp.urllib.request, 'urlopen', side_effect=exc):
                    with self.assertRaises(kbp.AdvisorError) as ctx:
                        advisor.advise('test', {})
            self.assertEqual(ctx.exception.retryable, retriable)
            if not retriable:
                self.assertEqual(ctx.exception.kind, 'configuration')

    def test_cloud_fallback_skips_ollama_model_discovery(self):
        args = kbp.parse_args(['--base', 'v6.19', '--target', 'v7.0', '--advisor-model', 'kernel-planner:32k',
            '--advisor-fallback-provider', 'openai',
            '--advisor-fallback-model', 'gpt-6.1-terra'])
        with mock.patch.dict(kbp.os.environ, {'OPENAI_API_KEY': 'test'}):
            with mock.patch.object(kbp, 'verify_advisor_fallback_model') as verify:
                with mock.patch.object(kbp, 'PerformanceRecorder'):
                    # Trigger early exit after preflight to avoid an expensive run.
                    with mock.patch.object(kbp, 'Git', side_effect=RuntimeError('stop')):
                        with self.assertRaises(Exception):
                            kbp.run_planner(args)
                verify.assert_not_called()

    def test_cache_identity_separates_providers(self):
        planner = object.__new__(kbp.Planner)
        planner.advisor_model = 'same-model-name'
        planner.base, planner.target, planner.downstream = 'a', 'b', 'c'
        planner._advisor_stage = 'primary'
        planner.args = types.SimpleNamespace(advisor='ollama',
                                             advisor_fallback_provider='openai')
        local = planner.advisor_cache_key('a' * 40, {'evidence': 'same'})
        planner.args.advisor = 'openai'
        cloud = planner.advisor_cache_key('a' * 40, {'evidence': 'same'})
        self.assertNotEqual(local, cloud)
        planner._advisor_stage = 'fallback'
        self.assertEqual(cloud, planner.advisor_cache_key('a'*40, {'evidence': 'same'}))

    def test_openai_preflight_before_git_work(self):
        args = kbp.parse_args(['--base', 'v6.19', '--target', 'v7.0',
                               '--advisor=openai'])
        with mock.patch.dict(kbp.os.environ, {}, clear=True):
            with mock.patch.object(kbp, 'Git') as git:
                with self.assertRaises(SystemExit) as ctx:
                    kbp.run_planner(args)
                self.assertIn('OPENAI_API_KEY', str(ctx.exception))
                git.assert_not_called()


class OpenAIAdvisor0433PreflightTests(unittest.TestCase):
    @staticmethod
    def response(content=None, status="completed"):
        if content is None:
            content = {"decision": "REVIEW", "rationale": "API preflight successful",
                "prerequisite_evidence_ids": [],
                "binding_evidence_assessments": [],
                "inbound_prerequisite_assessments": [], "risks": []}
        return {"status": status, "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(content)}]}]}

    @staticmethod
    def fake_urlopen(payload, sent):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(payload).encode("utf-8")
        def call(request, timeout):
            sent.append((request, timeout))
            return Response()
        return call

    def test_preflight_only_sends_real_schema_not_kernel_data(self):
        args = kbp.parse_args(["--advisor=openai", "--advisor-model", "gpt-6.1-sol",
                               "--advisor-preflight-only"])
        sent = []
        with mock.patch.dict(kbp.os.environ, {"OPENAI_API_KEY": "secret-test-key"}):
            with mock.patch.object(kbp.urllib.request, "urlopen",
                    side_effect=self.fake_urlopen(self.response(), sent)):
                with mock.patch.object(kbp, "Git") as git:
                    with mock.patch.object(kbp, "reserve_workspace_run") as reserve:
                        self.assertEqual(kbp.main(["--advisor=openai", "--advisor-model",
                            "gpt-6.1-sol", "--advisor-preflight-only"]), 0)
                        git.assert_not_called()
                        reserve.assert_not_called()
        self.assertEqual(len(sent), 1)
        request, timeout = sent[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gpt-6.1-sol")
        self.assertIs(body["store"], False)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"],
                         kbp.openai_compatible_schema(kbp.advisor_response_schema([], [])))
        self.assertTrue(body["input"].find('"preflight":true') >= 0
                        or '"preflight": true' in body["input"])
        self.assertNotIn("secret-test-key", json.dumps(body))
        self.assertEqual(timeout, args.advisor_timeout)

    def test_normal_cloud_run_preflights_before_planning(self):
        args = kbp.parse_args(["--base", "v6.19", "--target", "v7.0",
                               "--advisor=openai", "--advisor-model", "gpt-6.1-sol"])
        events = []
        response = self.response()
        def fake_network(request, timeout):
            events.append("preflight")
            return self.fake_urlopen(response, [])(request, timeout)
        def fake_planner(_args):
            events.append("planner")
            return 0
        with mock.patch.dict(kbp.os.environ, {"OPENAI_API_KEY": "test"}, clear=True):
            with mock.patch.object(kbp.urllib.request, "urlopen", side_effect=fake_network):
                with mock.patch.object(kbp, "run_planner", side_effect=fake_planner):
                    self.assertEqual(kbp._run_normal_planning(args), 0)
        self.assertEqual(events, ["preflight", "planner"])

    def test_preflight_failure_aborts_before_git_or_workspace_reservation(self):
        args = kbp.parse_args(["--base", "v6.19", "--target", "v7.0",
                               "--advisor=openai", "--advisor-model", "gpt-6.1-sol"])
        import urllib.error
        failures = [
            (429, b'{"error":{"code":"credit_balance_exhausted"}}',
             "credit_balance_exhausted"),
            (400, b'{"error":{"message":"schema invalid"}}', "schema invalid"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(kbp.os.environ,
                    {"OPENAI_API_KEY": "test", "KERNEL_WORKSPACE_DIR": tmp}):
                for code, body, message in failures:
                    with self.subTest(code=code):
                        error = urllib.error.HTTPError(kbp.OpenAIAdvisor.API_URL,
                            code, "error", {}, io.BytesIO(body))
                        with mock.patch.object(kbp.urllib.request, "urlopen", side_effect=error):
                            with mock.patch.object(kbp, "Git") as git:
                                with mock.patch.object(kbp, "reserve_workspace_run") as reserve:
                                    with self.assertRaises(SystemExit) as caught:
                                        kbp._run_normal_planning(args)
                                    self.assertIn(message, str(caught.exception))
                                    self.assertIn("No Git analysis started", str(caught.exception))
                                    git.assert_not_called()
                                    reserve.assert_not_called()

    def test_preflight_fails_on_invalid_structured_result_before_git(self):
        args = kbp.parse_args(["--base", "v6.19", "--target", "v7.0",
                               "--advisor=openai", "--advisor-model", "gpt-6.1-sol"])
        invalid = self.response(content={"decision": "TAKE", "rationale": "",
            "prerequisite_evidence_ids": [], "binding_evidence_assessments": [],
            "inbound_prerequisite_assessments": [], "risks": []})
        with mock.patch.dict(kbp.os.environ, {"OPENAI_API_KEY": "test"}):
            with mock.patch.object(kbp.urllib.request, "urlopen",
                    side_effect=self.fake_urlopen(invalid, [])):
                with mock.patch.object(kbp, "Git") as git:
                    with self.assertRaisesRegex(SystemExit, "preflight field validation"):
                        kbp._run_normal_planning(args)
                    git.assert_not_called()

    def test_openai_fallback_is_preflighted_too(self):
        args = kbp.parse_args(["--base", "v6.19", "--target", "v7.0",
            "--advisor-model", "kernel-planner:32k",
            "--advisor-fallback-provider", "openai",
            "--advisor-fallback-model", "gpt-6.1-sol"])
        sent = []
        with mock.patch.dict(kbp.os.environ, {"OPENAI_API_KEY": "test"}):
            with mock.patch.object(kbp.urllib.request, "urlopen",
                    side_effect=self.fake_urlopen(self.response(), sent)):
                self.assertTrue(kbp.preflight_openai_advisors(args))
        self.assertEqual(len(sent), 1)
        self.assertEqual(json.loads(sent[0][0].data)["model"], "gpt-6.1-sol")
        self.assertEqual(sent[0][1], args.advisor_fallback_timeout)

    def test_local_only_never_uses_cloud(self):
        args = kbp.parse_args(["--base", "v6.19", "--target", "v7.0",
            "--advisor-model", "kernel-planner:32k"])
        with mock.patch.dict(kbp.os.environ, {}, clear=True):
            with mock.patch.object(kbp.urllib.request, "urlopen") as network:
                self.assertFalse(kbp.preflight_openai_advisors(args))
                network.assert_not_called()

    def test_preflight_only_requires_a_cloud_provider(self):
        with mock.patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                kbp.parse_args(["--advisor-preflight-only"])


if __name__ == "__main__":
    unittest.main()

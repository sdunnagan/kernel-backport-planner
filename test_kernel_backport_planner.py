#!/usr/bin/env python3
import importlib.machinery
import types
import unittest
import sys
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


if __name__ == "__main__":
    unittest.main()

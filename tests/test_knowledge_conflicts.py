"""Usability must distinguish fresh evidence from unresolved alternatives."""
from contextlib import closing
import hashlib
from pathlib import Path
import tempfile
import unittest

from ase.knowledge import Knowledge
from ase.projects import ProjectRegistry
from ase.store import Store, now


class KnowledgeConflictTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-conflicts-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.home = self.base / "state"
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        self.api = Knowledge(self.store)
        self.evidence = self.source("evidence")

    def source(self, source_id):
        content = ("Evidence for " + source_id).encode()
        (self.repo / (source_id + ".txt")).write_bytes(content)
        observation = {"source_id": source_id, "locator": "local:" + source_id + ".txt",
            "version": "sha256:" + hashlib.sha256(content).hexdigest(), "kind": "file",
            "observed_at_utc": now(), "availability": "AVAILABLE"}
        self.api.record_observations([observation], [],
            expected_revision=self.api.retrieve(scope={}, refresh=False)["revision"])
        return observation

    def claim(self, statement, *, topic="timeout", source=None, **extra):
        source = source or self.evidence
        return self.api.record({"topic": topic, "statement": statement,
            "observed_at": "2026-01-02T00:00:00Z", "source_refs": [source["locator"]],
            "dependencies": [{"source_id": source["source_id"], "observed_version": source["version"]}],
            **extra})

    def rows(self, **kwargs):
        return {claim["id"]: claim for claim in self.api.retrieve(scope={}, **kwargs)["claims"]}

    def test_source_current_opposite_claims_require_review(self):
        first = self.claim("Timeout is 100 ms")
        second = self.claim("Timeout is 1000 ms")
        result = self.api.retrieve(scope={})
        self.assertEqual([claim["validity"] for claim in result["claims"]], ["CONFLICT", "CONFLICT"])
        for claim, peer in zip(result["claims"], (second, first)):
            self.assertEqual(claim.get("source_validity"), "CURRENT")
            self.assertTrue(claim.get("review_required"))
            self.assertEqual(claim.get("conflicts"), [{"claim_id": claim["id"], "alternative_claim_ids": [peer["id"]]}])
        self.assertEqual(result["refresh_required"], [first["id"], second["id"]])
        self.assertIs(result.get("semantic_validation"), False)

    def test_conservative_alternatives_do_not_require_semantic_opposition(self):
        self.claim("Timeout is configured")
        self.claim("Timeout is documented")
        self.assertEqual([claim["validity"] for claim in self.rows().values()], ["CONFLICT", "CONFLICT"])

    def test_no_source_alternatives_still_require_review_with_unknown_freshness(self):
        self.claim("First historical summary", topic="task-summary:one", kind="task_summary", dependencies=[])
        self.claim("Alternative historical summary", topic="task-summary:one", kind="task_summary", dependencies=[])
        claims = list(self.rows().values())
        self.assertEqual([claim["validity"] for claim in claims], ["CONFLICT", "CONFLICT"])
        self.assertEqual([claim.get("source_validity") for claim in claims], ["UNKNOWN", "UNKNOWN"])

    def test_superseding_historical_summary_never_becomes_current_without_evidence(self):
        first = self.claim("First historical summary", topic="task-summary:one", kind="task_summary", dependencies=[])
        second = self.claim("Updated historical summary", topic="task-summary:one", kind="task_summary",
            dependencies=[], supersedes=first["id"])
        rows = self.rows()
        self.assertEqual(rows[first["id"]]["validity"], "SUPERSEDED")
        self.assertEqual(rows[second["id"]]["validity"], "UNKNOWN")
        self.assertEqual(rows[second["id"]].get("source_validity"), "UNKNOWN")
        self.assertFalse(rows[second["id"]].get("review_required", False))

    def test_identical_statements_are_legitimate_duplicates(self):
        self.claim("Timeout is 100 ms")
        self.claim("Timeout is 100 ms")
        result = self.api.retrieve(scope={})
        self.assertEqual([claim["validity"] for claim in result["claims"]], ["CURRENT", "CURRENT"])
        self.assertFalse(any(claim.get("review_required", False) for claim in result["claims"]))
        self.assertEqual(result["refresh_required"], [])

    def test_different_topics_remain_independent(self):
        self.claim("Timeout is 100 ms")
        self.claim("Retry count is 3", topic="retry")
        self.assertEqual([claim["validity"] for claim in self.rows().values()], ["CURRENT", "CURRENT"])

    def test_claim_id_selection_cannot_hide_active_peer(self):
        first = self.claim("Timeout is 100 ms")
        second = self.claim("Timeout is 1000 ms")
        result = self.api.retrieve(scope={"claim_ids": [first["id"]]})
        self.assertEqual([claim["id"] for claim in result["claims"]], [first["id"]])
        self.assertEqual(result["claims"][0]["validity"], "CONFLICT")
        self.assertEqual(result["claims"][0].get("conflicts"),
            [{"claim_id": first["id"], "alternative_claim_ids": [second["id"]]}])

    def test_source_id_selection_cannot_hide_active_peer_or_read_unselected_source(self):
        other = self.source("other")
        first = self.claim("Timeout is 100 ms")
        second = self.claim("Timeout is 1000 ms", source=other)
        (self.repo / "other.txt").unlink()
        result = self.api.retrieve(scope={"source_ids": ["evidence"]})
        self.assertEqual([claim["id"] for claim in result["claims"]], [first["id"]])
        self.assertEqual(result["claims"][0]["validity"], "CONFLICT")
        self.assertEqual([source["source_id"] for source in result["observations"]], ["evidence"])
        self.assertEqual(result["claims"][0].get("conflicts"),
            [{"claim_id": first["id"], "alternative_claim_ids": [second["id"]]}])

    def test_scope_claim_and_source_ids_do_not_disambiguate_applicability(self):
        self.claim("Timeout is 100 ms", scope={"source_ids": ["one"], "claim_ids": [1]})
        self.claim("Timeout is 1000 ms", scope={"source_ids": ["two"], "claim_ids": [2]})
        self.assertEqual([claim["validity"] for claim in self.rows().values()], ["CONFLICT", "CONFLICT"])

    def test_disjoint_declared_paths_remain_usable_with_shared_evidence(self):
        self.claim("Timeout is 100 ms", scope={"paths": ["service-a/config.py"]})
        self.claim("Timeout is 1000 ms", scope={"paths": ["service-b/config.py"]})
        self.assertEqual([claim["validity"] for claim in self.rows().values()], ["CURRENT", "CURRENT"])

    def test_normalized_parent_child_paths_overlap_even_when_query_selects_child(self):
        first = self.claim("Timeout is 100 ms", scope={"paths": ["service"]})
        second = self.claim("Timeout is 1000 ms", scope={"paths": ["./service/config.py"]})
        result = self.api.retrieve(scope={"paths": ["service/config.py"]})
        self.assertEqual([claim["id"] for claim in result["claims"]], [second["id"]])
        self.assertEqual(result["claims"][0]["validity"], "CONFLICT")
        self.assertEqual(result["claims"][0].get("conflicts"),
            [{"claim_id": second["id"], "alternative_claim_ids": [first["id"]]}])

    def test_unbounded_scope_overlaps_a_bounded_path_scope(self):
        self.claim("Timeout is 100 ms")
        self.claim("Timeout is 1000 ms", scope={"paths": ["service/config.py"]})
        self.assertEqual([claim["validity"] for claim in self.rows().values()], ["CONFLICT", "CONFLICT"])

    def test_disjoint_resource_scopes_remain_usable(self):
        self.claim("Timeout is 100 ms", scope={"resource_ids": ["vm-1"]})
        self.claim("Timeout is 1000 ms", scope={"resource_ids": ["vm-2"]})
        self.assertEqual([claim["validity"] for claim in self.rows().values()], ["CURRENT", "CURRENT"])

    def test_overlapping_resource_scopes_require_review(self):
        self.claim("Timeout is 100 ms", scope={"resource_ids": ["vm-1", "vm-2"]})
        second = self.claim("Timeout is 1000 ms", scope={"resource_ids": ["vm-2", "vm-3"]})
        result = self.api.retrieve(scope={"resource_ids": ["vm-3"]})
        self.assertEqual([claim["id"] for claim in result["claims"]], [second["id"]])
        self.assertEqual(result["claims"][0]["validity"], "CONFLICT")

    def test_disjoint_applicability_dimension_prevents_false_conflict(self):
        self.claim("Timeout is 100 ms", scope={"paths": ["config.py"], "resource_ids": ["vm-1"]})
        self.claim("Timeout is 1000 ms", scope={"paths": ["config.py"], "resource_ids": ["vm-2"]})
        self.assertEqual([claim["validity"] for claim in self.rows().values()], ["CURRENT", "CURRENT"])

    def test_transitive_dependent_cannot_be_current_through_conflicting_prerequisite(self):
        first = self.claim("Timeout is 100 ms")
        second = self.claim("Timeout is 1000 ms")
        derived = self.claim("Retry budget follows timeout", topic="retry",
            dependencies=[{"depends_on_claim_id": first["id"]}])
        final = self.claim("Schedule follows retry budget", topic="schedule",
            dependencies=[{"depends_on_claim_id": derived["id"]}])
        result = self.api.retrieve(topic="schedule", scope={"claim_ids": [final["id"]]})
        claim = result["claims"][0]
        self.assertEqual(claim["validity"], "CONFLICT")
        self.assertEqual(claim.get("source_validity"), "CURRENT")
        self.assertTrue(claim.get("review_required"))
        self.assertEqual(claim.get("conflicts"),
            [{"claim_id": first["id"], "alternative_claim_ids": [second["id"]]}])

    def test_refresh_false_reports_conflict_without_promoting_source_freshness(self):
        first = self.claim("Timeout is 100 ms")
        self.claim("Timeout is 1000 ms")
        fresh = self.api.retrieve(scope={})
        cached = self.api.retrieve(scope={"claim_ids": [first["id"]]}, refresh=False)
        claim = cached["claims"][0]
        self.assertEqual(claim["validity"], "CONFLICT")
        self.assertEqual(claim.get("source_validity"), "UNKNOWN")
        self.assertEqual(claim.get("last_verified_source_validity"), "CURRENT")
        self.assertEqual(claim["last_verified_validity"], "CONFLICT")
        self.assertEqual(cached["revision"], fresh["revision"])

    def test_changed_source_does_not_hide_unresolved_alternatives(self):
        self.claim("Timeout is 100 ms")
        self.claim("Timeout is 1000 ms")
        (self.repo / "evidence.txt").write_bytes(b"Changed evidence")
        claims = list(self.rows().values())
        self.assertEqual([claim["validity"] for claim in claims], ["CONFLICT", "CONFLICT"])
        self.assertEqual([claim.get("source_validity") for claim in claims], ["STALE", "STALE"])

    def test_partial_supersession_leaves_conflict_until_active_statements_converge(self):
        first = self.claim("Timeout is 100 ms")
        second = self.claim("Timeout is 1000 ms")
        conflicting = self.api.retrieve(scope={})
        replacement = self.claim("Timeout is 200 ms", supersedes=first["id"])
        rows = self.rows()
        self.assertEqual(rows[first["id"]]["validity"], "SUPERSEDED")
        self.assertEqual(rows[second["id"]]["validity"], "CONFLICT")
        self.assertEqual(rows[replacement["id"]]["validity"], "CONFLICT")
        resolved = self.claim("Timeout is 200 ms", supersedes=second["id"])
        rows = self.rows()
        self.assertEqual(rows[second["id"]]["validity"], "SUPERSEDED")
        self.assertEqual(rows[replacement["id"]]["validity"], "CURRENT")
        self.assertEqual(rows[resolved["id"]]["validity"], "CURRENT")
        self.assertFalse(rows[replacement["id"]].get("review_required", False))
        self.assertIn("CONFLICT", [event["validity"] for event in rows[first["id"]]["validity_history"]])
        self.assertEqual([claim["statement"] for claim in self.api.query()["claims"]],
            ["Timeout is 100 ms", "Timeout is 1000 ms", "Timeout is 200 ms", "Timeout is 200 ms"])
        with self.assertRaisesRegex(ValueError, "Stale knowledge revision"):
            self.api.record({"topic": "timeout", "statement": "Stale writer", "observed_at": "2026-01-02T00:00:00Z",
                "source_refs": ["local:evidence.txt"]}, expected_revision=conflicting["revision"])

    def test_stable_conflict_reads_preserve_cas_revision_and_history(self):
        first = self.claim("Timeout is 100 ms")
        self.claim("Timeout is 1000 ms")
        original = self.api.retrieve(scope={})
        repeated = self.api.retrieve(scope={})
        self.assertEqual(repeated["revision"], original["revision"])
        self.assertEqual(repeated["claims"][0]["validity_history"], original["claims"][0]["validity_history"])
        event = repeated["claims"][0]["validity_history"][-1]
        self.assertEqual(event["validity"], "CONFLICT")
        self.assertEqual(event.get("source_validity"), "CURRENT")
        self.assertEqual(event["claim_id"], first["id"])

    def test_other_workspace_alternative_does_not_conflict_with_current_workspace(self):
        original = self.claim("Timeout is 100 ms")
        other = self.base / "other-workspace"
        other.mkdir()
        with closing(ProjectRegistry(self.home)) as registry:
            context = registry.attach(self.store.project_id, other, expected_revision=self.store.project.revision)
        with closing(Store(context)) as other_store:
            api = Knowledge(other_store)
            observation = dict(self.evidence, workspace_id=other_store.workspace_id)
            (other / "evidence.txt").write_bytes((self.repo / "evidence.txt").read_bytes())
            api.record_observations([observation], [], expected_revision=0)
            replacement = api.record({"topic": "timeout", "statement": "Timeout is 1000 ms",
                "observed_at": "2026-01-02T00:00:00Z", "source_refs": ["local:evidence.txt"],
                "dependencies": [{"source_id": "evidence", "observed_version": self.evidence["version"]}]})
            rows = {claim["id"]: claim for claim in api.retrieve(scope={})["claims"]}
            self.assertEqual(rows[original["id"]]["validity"], "UNKNOWN")
            self.assertEqual(rows[replacement["id"]]["validity"], "CURRENT")
            self.assertFalse(rows[replacement["id"]].get("review_required", False))
        self.assertEqual(self.rows()[original["id"]]["validity"], "CURRENT")

    def test_other_project_alternatives_are_not_visible(self):
        self.claim("Timeout is 100 ms")
        self.claim("Timeout is 1000 ms")
        other = self.base / "other-project"
        other.mkdir()
        with closing(Store(other, self.home)) as other_store:
            self.assertEqual(Knowledge(other_store).retrieve(scope={})["claims"], [])

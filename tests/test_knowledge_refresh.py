"""Real source bytes, isolated memory, and conservative knowledge refresh."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from ase.knowledge import Knowledge
from ase.projects import ProjectRegistry
from ase.store import Store, now


class KnowledgeRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-refresh-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.home = self.base / "state"
        self.store = Store(self.repo, self.home)
        self.addCleanup(self.store.close)
        self.api = Knowledge(self.store)

    def claim(self, topic, **extra):
        return self.api.record({"topic": topic, "statement": "Recorded " + topic,
            "observed_at": "2026-01-01T00:00:00Z", "source_refs": ["local:" + topic + ".txt"], **extra})

    def source(self, source_id, content=b"original"):
        (self.repo / (source_id + ".txt")).write_bytes(content)
        return {"source_id": source_id, "locator": "local:" + source_id + ".txt",
                "version": "sha256:" + hashlib.sha256(content).hexdigest(),
                "observed_at_utc": now(), "availability": "AVAILABLE", "kind": "file"}

    def bind(self, sources, dependencies):
        return self.api.record_observations(sources, dependencies,
            expected_revision=self.api.retrieve(scope={}, refresh=False)["revision"])

    def claims(self, **kwargs):
        return {c["id"]: c for c in self.api.retrieve(scope={}, **kwargs)["claims"]}

    def test_changed_dependency_invalidates_only_related_claims(self):
        a, b, c = self.claim("a"), self.claim("b"), self.claim("c")
        sa, sc = self.source("a"), self.source("c")
        self.bind([sa, sc], [
            {"claim_id": a["id"], "source_id": "a", "observed_version": sa["version"], "relation": "evidence"},
            {"claim_id": b["id"], "depends_on_claim_id": a["id"], "relation": "derived"},
            {"claim_id": c["id"], "source_id": "c", "observed_version": sc["version"], "relation": "evidence"}])
        self.assertEqual([x["validity"] for x in self.claims().values()], ["CURRENT"] * 3)
        (self.repo / "a.txt").write_bytes(b"changed")
        result = self.api.retrieve(scope={})
        self.assertEqual([x["validity"] for x in result["claims"]], ["STALE", "STALE", "CURRENT"])
        self.assertEqual(result["refresh_required"], [a["id"], b["id"]])
        self.assertEqual(result["claims"][0]["statement"], "Recorded a")

    def test_dirty_missing_and_unrecorded_dependencies(self):
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        source = self.source("a")
        subprocess.run(["git", "-C", str(self.repo), "add", "a.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline"], check=True, capture_output=True)
        def head():
            return subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"])
        before = head()
        claim = self.claim("a")
        unknown = self.claim("unrecorded")
        self.bind([source], [{"claim_id": claim["id"], "source_id": "a", "observed_version": source["version"]}])
        self.assertEqual(self.claims()[claim["id"]]["validity"], "CURRENT")
        (self.repo / "a.txt").write_bytes(b"dirty")
        self.assertEqual(before, head())
        self.assertEqual(self.claims()[claim["id"]]["validity"], "STALE")
        (self.repo / "a.txt").unlink()
        rows = self.claims()
        self.assertEqual(rows[claim["id"]]["validity"], "UNAVAILABLE")
        self.assertEqual(rows[unknown["id"]]["validity"], "UNKNOWN")
        result = self.api.retrieve(scope={})
        self.assertFalse(result["dependency_discovery_complete"])
        self.assertTrue(any("declared" in line.lower() for line in result["limitations"]))

    def test_refresh_false_never_promotes_cached_validity(self):
        source = self.source("a")
        claim = self.claim("a")
        self.bind([source], [{"claim_id": claim["id"], "source_id": "a", "observed_version": source["version"]}])
        self.assertEqual(self.claims()[claim["id"]]["validity"], "CURRENT")
        (self.repo / "a.txt").write_bytes(b"new")
        cached = self.claims(refresh=False)[claim["id"]]
        self.assertEqual(cached["validity"], "UNKNOWN")
        self.assertEqual(cached["last_verified_validity"], "CURRENT")
        self.assertEqual(self.claims()[claim["id"]]["validity"], "STALE")

    def test_claim_history_cas_and_cycles(self):
        a, b, c = self.claim("a"), self.claim("b"), self.claim("c")
        source = self.source("a")
        self.bind([source], [
            {"claim_id": a["id"], "source_id": "a", "observed_version": source["version"]},
            {"claim_id": a["id"], "depends_on_claim_id": b["id"]},
            {"claim_id": b["id"], "depends_on_claim_id": a["id"]},
            {"claim_id": c["id"], "depends_on_claim_id": c["id"]}])
        self.assertEqual([x["validity"] for x in self.claims().values()], ["CURRENT", "CURRENT", "UNKNOWN"])
        (self.repo / "a.txt").write_bytes(b"changed")
        result = self.api.retrieve(scope={})
        self.assertEqual([x["validity"] for x in result["claims"]], ["STALE", "STALE", "UNKNOWN"])
        revision = result["revision"]
        barrier = threading.Barrier(2)
        outcomes, errors = [], []
        def writer():
            try:
                with closing(Store(self.repo, self.home)) as store:
                    barrier.wait(timeout=10)
                    try:
                        Knowledge(store).record({"topic": "a", "statement": "Replacement", "observed_at": "2026-01-02T00:00:00Z", "source_refs": ["local:a.txt"], "supersedes": a["id"]}, expected_revision=revision)
                        outcomes.append("saved")
                    except ValueError:
                        outcomes.append("stale")
            except Exception as exc:
                errors.append(str(exc))
        workers = [threading.Thread(target=writer) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=20)
        self.assertFalse(errors)
        self.assertEqual(sorted(outcomes), ["saved", "stale"])
        old = self.claims()[a["id"]]
        self.assertEqual(old["validity"], "SUPERSEDED")
        self.assertEqual(old["statement"], "Recorded a")
        self.assertIn("STALE", [event["validity"] for event in old["validity_history"]])

    def test_observation_cas_is_atomic_across_competing_writers(self):
        claim = self.claim("a")
        source = self.source("a")
        revision = self.api.retrieve(scope={}, refresh=False)["revision"]
        barrier = threading.Barrier(2)
        outcomes, errors = [], []
        def writer():
            try:
                with closing(Store(self.repo, self.home)) as store:
                    barrier.wait(timeout=10)
                    try:
                        Knowledge(store).record_observations([source], [{"claim_id": claim["id"], "source_id": "a", "observed_version": source["version"]}], expected_revision=revision)
                        outcomes.append("saved")
                    except ValueError:
                        outcomes.append("stale")
            except Exception as exc:
                errors.append(str(exc))
        workers = [threading.Thread(target=writer) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=20)
        self.assertFalse(errors)
        self.assertEqual(sorted(outcomes), ["saved", "stale"])
        self.assertEqual(self.api.retrieve(scope={}, refresh=False)["revision"], revision + 1)

    def test_workspace_changes_do_not_revive_current_and_decisions_survive(self):
        source = self.source("a")
        claim = self.claim("a", kind="user_decision", scope={"paths": ["a.txt"]})
        self.bind([source], [{"claim_id": claim["id"], "source_id": "a", "observed_version": source["version"]}])
        self.assertEqual(self.claims()[claim["id"]]["validity"], "CURRENT")
        other = self.base / "other-workspace"
        other.mkdir()
        (other / "a.txt").write_bytes(b"original")
        with closing(ProjectRegistry(self.home)) as registry:
            context = registry.attach(self.store.project_id, other, expected_revision=self.store.project.revision)
        with closing(Store(context)) as other_store:
            result = Knowledge(other_store).retrieve(scope={})
            self.assertEqual(result["claims"][0]["validity"], "UNKNOWN")
            self.assertEqual(result["claims"][0]["kind"], "user_decision")
            self.assertEqual(result["claims"][0]["scope"]["paths"], ["a.txt"])
            self.assertEqual(result["claims"][0]["statement"], "Recorded a")
        unrelated = self.base / "other-project"
        unrelated.mkdir()
        with closing(Store(unrelated, self.home)) as store:
            self.assertEqual(Knowledge(store).retrieve(scope={})["claims"], [])

    def test_foreign_workspace_cannot_supersede_claim_with_its_own_zero_revision(self):
        original = self.claim("a", scope={"paths": ["a.txt"]})
        original_revision = self.api.retrieve(scope={}, refresh=False)["revision"]
        event_count = self.store.db.execute("SELECT count(*) FROM runtime_events").fetchone()[0]
        other = self.base / "other-workspace"
        other.mkdir()
        with closing(ProjectRegistry(self.home)) as registry:
            context = registry.attach(self.store.project_id, other, expected_revision=self.store.project.revision)
        with closing(Store(context)) as other_store:
            api = Knowledge(other_store)
            self.assertEqual(api.retrieve(scope={}, refresh=False)["revision"], 0)
            with self.assertRaises(ValueError):
                api.record({"topic": "a", "statement": "Other workspace decision",
                    "observed_at": "2026-01-02T00:00:00Z", "source_refs": ["local:a.txt"],
                    "scope": {"paths": ["a.txt"]}, "supersedes": original["id"]}, expected_revision=0)
            self.assertEqual(api.retrieve(scope={}, refresh=False)["revision"], 0)
        old = self.api.query("a")["claims"]
        self.assertEqual(len(old), 1)
        self.assertIsNone(old[0]["superseded_by"])
        self.assertEqual(self.api.retrieve(scope={}, refresh=False)["revision"], original_revision)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runtime_events").fetchone()[0], event_count)

    def test_scoped_refresh_follows_declared_dependencies_only(self):
        a, b = self.claim("a"), self.claim("b")
        sa, sb = self.source("a"), self.source("b")
        self.bind([sa, sb], [
            {"claim_id": a["id"], "source_id": "a", "observed_version": sa["version"]},
            {"claim_id": b["id"], "source_id": "b", "observed_version": sb["version"]}])
        (self.repo / "b.txt").unlink()
        selected = self.api.retrieve(topic="a", scope={})
        self.assertEqual([x["id"] for x in selected["claims"]], [a["id"]])
        self.assertEqual([x["source_id"] for x in selected["observations"]], ["a"])
        self.assertEqual(selected["claims"][0]["validity"], "CURRENT")
        self.assertEqual(self.claims()[b["id"]]["validity"], "UNAVAILABLE")

    def test_path_scope_selects_claim_applicability_and_refreshes_its_evidence(self):
        claim = self.claim("decision", source_refs=["https://example.invalid/decision"], scope={"paths": ["a.txt"]})
        unrelated = self.claim("unrelated")
        evidence, other = self.source("evidence"), self.source("unrelated")
        self.bind([evidence, other], [
            {"claim_id": claim["id"], "source_id": "evidence", "observed_version": evidence["version"]},
            {"claim_id": unrelated["id"], "source_id": "unrelated", "observed_version": other["version"]}])
        (self.repo / "evidence.txt").write_bytes(b"changed")
        (self.repo / "unrelated.txt").unlink()
        result = self.api.retrieve(scope={"paths": ["./a.txt"]})
        self.assertEqual([item["id"] for item in result["claims"]], [claim["id"]])
        self.assertEqual(result["claims"][0]["validity"], "STALE")
        self.assertEqual([item["source_id"] for item in result["observations"]], ["evidence"])
        self.assertEqual(result["refresh_required"], [claim["id"]])

    def test_path_scope_selects_dependency_locator_without_local_claim_reference(self):
        claim = self.claim("external-reference", source_refs=["https://example.invalid/decision"])
        source = self.source("a")
        self.bind([source], [{"claim_id": claim["id"], "source_id": "a", "observed_version": source["version"]}])
        (self.repo / "a.txt").write_bytes(b"changed")
        result = self.api.retrieve(scope={"paths": ["a.txt"]})
        self.assertEqual([item["id"] for item in result["claims"]], [claim["id"]])
        self.assertEqual(result["claims"][0]["validity"], "STALE")
        self.assertEqual([item["source_id"] for item in result["observations"]], ["a"])

    def test_live_resources_expire_and_always_require_pre_mutation_probe(self):
        claim = self.claim("vm", kind="infrastructure", refresh_policy={"max_age_seconds": 300})
        live = {"source_id": "vm", "locator": "resource:vm-1", "kind": "live", "version": "generation-1",
                "observed_at_utc": now(), "availability": "AVAILABLE"}
        self.bind([live], [{"claim_id": claim["id"], "source_id": "vm", "observed_version": "generation-1"}])
        result = self.api.retrieve(scope={})
        self.assertEqual(result["claims"][0]["validity"], "CURRENT")
        self.assertTrue(result["pre_mutation_probe_required"])
        live["observed_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.bind([live], [{"claim_id": claim["id"], "source_id": "vm", "observed_version": "generation-1"}])
        expired = self.claims()[claim["id"]]
        self.assertEqual(expired["validity"], "STALE")
        self.assertTrue(expired["pre_mutation_probe_required"])

    def test_plain_claim_with_live_dependency_still_requires_probe_without_refresh(self):
        claim = self.claim("vm")
        live = {"source_id": "vm", "locator": "resource:vm-1", "kind": "live", "version": "one",
                "observed_at_utc": now(), "availability": "AVAILABLE"}
        self.bind([live], [{"claim_id": claim["id"], "source_id": "vm", "observed_version": "one"}])
        self.assertTrue(self.api.retrieve(scope={}, refresh=False)["pre_mutation_probe_required"])

    def test_changed_hash_observations_remain_in_history_after_source_recovers(self):
        source = self.source("a")
        claim = self.claim("a")
        self.bind([source], [{"claim_id": claim["id"], "source_id": "a", "observed_version": source["version"]}])
        self.api.retrieve(scope={})
        (self.repo / "a.txt").write_bytes(b"changed")
        self.api.retrieve(scope={})
        (self.repo / "a.txt").write_bytes(b"original")
        result = self.api.retrieve(scope={})
        self.assertEqual(result["claims"][0]["validity"], "CURRENT")
        versions = [item["observation"]["version"] for item in result["observation_history"]]
        self.assertIn("sha256:d67e2e944994496c8d8ec76eed0cf9f09679448d584b532bebf941852a37f5ed", versions)

    def test_record_dependency_metadata_and_invalid_edges_roll_back_claim(self):
        source = self.source("a")
        self.bind([source], [])
        revision = self.api.retrieve(scope={}, refresh=False)["revision"]
        claim = self.claim("a", dependencies=[{"source_id": "a", "observed_version": source["version"]}])
        self.assertEqual(self.claims()[claim["id"]]["validity"], "CURRENT")
        revision = self.api.retrieve(scope={}, refresh=False)["revision"]
        with self.assertRaises(ValueError):
            self.claim("bad", dependencies=["not an edge"])
        self.assertEqual(len(self.api.query()["claims"]), 1)
        self.assertEqual(self.api.retrieve(scope={}, refresh=False)["revision"], revision)

    def test_encoded_and_short_url_credentials_are_not_saved_as_references(self):
        for reference in ("https://example.invalid/doc?access_token=x", "https://example.invalid/doc?%74oken=x"):
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):
                    self.claim("a", source_refs=[reference])
                with self.assertRaises(ValueError):
                    self.api.record_observations([{"source_id": "live", "locator": reference, "kind": "live",
                        "version": "one", "observed_at_utc": now(), "availability": "AVAILABLE"}], [], expected_revision=0)
        self.assertEqual(self.api.query()["claims"], [])

    def test_shared_inode_source_is_unavailable_instead_of_crossing_workspace_boundary(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"outside")
        os.link(outside, self.repo / "a.txt")
        source = {"source_id": "a", "locator": "local:a.txt", "kind": "file",
            "version": "sha256:" + hashlib.sha256(b"outside").hexdigest(),
            "observed_at_utc": now(), "availability": "AVAILABLE"}
        claim = self.claim("a")
        self.bind([source], [{"claim_id": claim["id"], "source_id": "a", "observed_version": source["version"]}])
        self.assertEqual(self.claims()[claim["id"]]["validity"], "UNAVAILABLE")

    def test_bad_source_and_foreign_scope_rejected_without_partial_changes(self):
        claim = self.claim("a")
        source = self.source("a")
        revision = self.api.retrieve(scope={}, refresh=False)["revision"]
        bad_sources = [dict(source, locator="local:../outside.txt"),
            dict(source, workspace_id="other"), dict(source, locator="https://u:password@example.invalid/doc"),
            dict(source, locator="https://example.invalid/doc?token=credential"), dict(source, version="not-a-file-hash")]
        for bad in bad_sources:
            with self.subTest(source=bad), self.assertRaises(ValueError):
                self.api.record_observations([bad], [], expected_revision=revision)
        with self.assertRaises(ValueError):
            self.api.record_observations([source], [{"claim_id": 999, "source_id": "a", "observed_version": source["version"]}], expected_revision=revision)
        with self.assertRaises(ValueError):
            self.api.retrieve(scope={"workspace_id": "foreign"})
        self.assertEqual(self.api.retrieve(scope={}, refresh=False)["revision"], revision)
        self.assertEqual(self.claims()[claim["id"]]["validity"], "UNKNOWN")

    def test_migrated_unknown_claim_does_not_get_invented_source_baseline(self):
        self.source("a")
        with self.store.db:
            self.store.db.execute("INSERT INTO knowledge_claims(topic,statement,observed_at,source_refs,created) VALUES(?,?,?,?,?)",
                ("old", "Historical decision", "2026-01-01T00:00:00Z", json.dumps(["local:a.txt"]), now()))
            claim_id = self.store.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            self.store.db.execute("INSERT INTO runtime_records VALUES(?,?,?,?,?)",
                ("knowledge_validity", str(claim_id), 1, json.dumps({"validity": "UNKNOWN"}), now()))
        result = self.api.retrieve(scope={})
        self.assertEqual(result["claims"][0]["validity"], "UNKNOWN")
        self.assertEqual(result["observations"], [])

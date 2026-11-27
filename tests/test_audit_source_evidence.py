"""Real source packets and disposable stores; no semantic model judgments."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ase.adapters.codex import CodexAdapter
from ase.discovery import discover_capabilities, record_discovery, validate_discovery, validate_intake
from ase.gates import GateService, source_resources, source_snapshot
from ase.protocol import canonical, validate_packet
from ase.runner import _parse
from ase.state import read_record, transaction
from ase.store import Store
from ase.tasks import TaskService
from ase.workflow import AC_SCHEMA, WorkflowController, apply_edits
from test_task_contract_v2 import contract


class SourceEvidenceAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-source-audit-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "work"
        self.repo.mkdir()
        self.store = Store(self.repo, self.base / "state")
        self.addCleanup(self.store.close)
        self.tasks = TaskService(self.store)
        self.controller = WorkflowController(self.store)
        self.write("guide.md", "Confirmed guide")

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def task(self, artifact="guide.md", extra=()):
        value = contract("source-audit")
        value["unknowns"] = []
        value["stages"][1]["unknown_ids"] = []
        value["stages"].append({"id": "follow-up", "acceptance_ids": [], "depends_on": ["publish"]})
        value["scope"]["resources"] = [artifact, *extra]
        for stage in value["stages"][:2]:
            stage["scope"]["resources"] = [artifact, *extra]
        value["evidence"][0]["path"] = artifact
        task = self.tasks.create(value)
        task = self.tasks.propose("source-audit", {"summary": "Prepare the requested guide", "stage_ids": ["draft", "publish", "follow-up"]}, expected_revision=task["revision"])
        self.tasks.approve("source-audit", proposal_hash=task["proposal_hash"], approved_stage_ids=["draft", "publish", "follow-up"], user_message_ref="fixture:user-assignment")
        return self.tasks.snapshot("source-audit")["contract"]

    def discovery(self, *, dependencies=(), resources=("guide.md",), outputs=()):
        scope = {"resources": list(resources), "dependencies": list(dependencies)}
        if outputs:
            scope["outputs"] = list(outputs)
        return discover_capabilities(self.store.project, scope=scope, backend_config={})

    def save(self, payload, revision=0):
        return record_discovery(self.store, "source-audit", payload, expected_revision=revision)

    def context(self, packet, name):
        return json.loads(next(item["content"] for item in packet["files"] if item["path"] == "_ase_context/" + name + ".json"))

    def intake(self, status):
        return {"source_refs": [{"id": "policy", "kind": "document", "reference": "local:policy.md", "observed_at": "2026-11-13T10:00:00Z", "status": status}],
                "outcomes": [{"id": "guide", "text": "Prepare the guide", "source_refs": ["policy"]}], "gaps": []}

    def test_every_case_of_incomplete_status_requires_attributed_gap(self):
        for status in ("unavailable", "UNAVAILABLE", " Partial ", "BLOCKED", "INAUDIBLE", "MISSING", "UNKNOWN"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "Incomplete source"):
                validate_intake(self.intake(status))

    def test_source_status_is_normalized_and_unknown_vocabulary_rejected(self):
        payload = self.intake(" UNAVAILABLE ")
        payload["gaps"] = [{"id": "missing-policy", "text": "Policy is unavailable", "source_refs": ["policy"]}]
        self.assertEqual(validate_intake(payload)["source_refs"][0]["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "source status"):
            validate_intake(self.intake("not-a-source-status"))

    def test_disappeared_and_refreshed_missing_dependency_block_packet_and_readiness(self):
        value = self.task()
        policy = self.write("policy.md", "Mandatory policy")
        dependency = {"path": "policy.md", "reason": "Governs the guide"}
        self.save(self.discovery(dependencies=[dependency]))
        before = source_snapshot(self.store, value, "source-audit")
        policy.unlink()
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                if refresh:
                    self.save(self.discovery(dependencies=[dependency]), 1)
                packet = self.controller._packet("source-audit")
                validate_packet(packet)
                self.assertIn("policy.md", source_resources(self.store, value, "source-audit"))
                self.assertTrue(any("policy.md" in gap for gap in packet["coverage"]["missing"]))
                availability = self.context(packet, "source_availability")
                self.assertTrue(any(item["path"] == "policy.md" and item["status"] == "MISSING" for item in availability))
                self.assertEqual(self.context(packet, "discovery")["dependencies"][0]["reason"], "Governs the guide")
                self.assertEqual(self.tasks.readiness("source-audit")["clarity"], "BLOCKED")
                with self.assertRaisesRegex(ValueError, "source|gap"):
                    self.tasks.require_approval("source-audit", "draft")
        self.assertNotEqual(before["fingerprint"], source_snapshot(self.store, value, "source-audit")["fingerprint"])

    def test_optional_missing_dependency_is_disclosed_without_becoming_required(self):
        self.task()
        self.save(self.discovery(dependencies=[{"path": "optional.md", "reason": "Optional background", "required": False}]))
        packet = self.controller._packet("source-audit")
        self.assertEqual(packet["coverage"]["missing"], [])
        row = next(item for item in self.context(packet, "source_availability") if item["path"] == "optional.md")
        self.assertFalse(row["required"])
        self.assertEqual(row["status"], "MISSING")
        self.assertEqual(self.tasks.readiness("source-audit")["required_source_gaps"], [])
        self.tasks.require_approval("source-audit", "draft")

    def test_required_dependency_only_blocks_mapped_stage_and_dependents(self):
        self.task()
        self.save(self.discovery(dependencies=[{"path": "release-policy.md", "reason": "Needed for publication", "blocking_stage_ids": ["publish"]}]))
        self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])
        self.tasks.require_approval("source-audit", "draft")
        self.assertEqual(self.controller._packet("source-audit", stage_id="draft")["coverage"]["missing"], [])
        for stage in ("publish", "follow-up"):
            with self.subTest(stage=stage):
                self.assertTrue(self.tasks.readiness("source-audit", stage_id=stage)["required_source_gaps"])
                self.assertTrue(self.controller._packet("source-audit", stage_id=stage)["coverage"]["missing"])

    def test_missing_future_artifact_and_explicit_deleted_output_are_not_input_gaps(self):
        self.task("future.docx", extra=["obsolete.md"])
        obsolete = self.write("obsolete.md", "retired")
        payload = self.discovery(resources=["future.docx", "obsolete.md"], outputs=["obsolete.md"])
        self.save(payload)
        apply_edits(self.repo, {"resources": ["obsolete.md"], "effects": ["file_write"]}, [{"path": "obsolete.md", "before_sha256": hashlib.sha256(obsolete.read_bytes()).hexdigest(), "operation": "delete", "content": ""}])
        self.assertEqual(self.tasks.readiness("source-audit")["required_source_gaps"], [])
        self.assertEqual(self.controller._packet("source-audit")["coverage"]["missing"], [])

    def test_missing_approved_mutation_target_is_workspace_state_without_input_obligation(self):
        self.task(extra=["resource.counter"])
        self.save(self.discovery(resources=["guide.md", "resource.counter"]))
        self.assertEqual(self.tasks.readiness("source-audit")["required_source_gaps"], [])
        self.tasks.require_approval("source-audit", "draft")
        packet = self.controller._packet("source-audit")
        self.assertEqual(packet["coverage"]["missing"], [])
        row = next(item for item in self.context(packet, "source_availability") if item["path"] == "resource.counter")
        self.assertEqual(row["role"], "workspace")

    def test_explicit_required_input_overrides_artifact_output_classification(self):
        self.task("future.docx")
        self.save(self.discovery(resources=["future.docx"], dependencies=[{"path": "future.docx", "reason": "An existing signed guide is also mandatory input"}]))
        self.assertTrue(self.tasks.readiness("source-audit")["required_source_gaps"])
        self.assertTrue(self.controller._packet("source-audit")["coverage"]["missing"])

    def test_unknown_dependency_stage_mapping_rejects_instead_of_hiding_missing_source(self):
        self.task()
        with self.assertRaisesRegex(ValueError, "stage"):
            self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Mandatory policy", "blocking_stage_ids": ["unknown-stage"]}]))

    def test_discovered_transitive_dependency_preserves_optional_or_stage_impact(self):
        self.task()
        self.write("policy/Policy.csproj", '<Project><ItemGroup><ProjectReference Include="Missing.csproj" /></ItemGroup></Project>')
        for index, metadata in enumerate(({"required": False}, {"blocking_stage_ids": ["publish"]})):
            with self.subTest(metadata=metadata):
                payload = self.discovery(dependencies=[{"path": "policy/Policy.csproj", "reason": "Publication-only policy", **metadata}])
                self.save(payload, index)
                self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])
                self.assertEqual(self.controller._packet("source-audit", stage_id="draft")["coverage"]["missing"], [])
                missing = next(item for item in payload["dependencies"] if item["path"] == "policy/Missing.csproj")
                for key, expected in metadata.items():
                    self.assertEqual(missing.get(key), expected)

    def test_late_stronger_route_propagates_to_already_discovered_missing_descendant(self):
        self.task()
        self.write("a/Optional.csproj", '<Project><ProjectReference Include="../common/Shared.csproj" /></Project>')
        self.write("b/Required.csproj", '<Project><ProjectReference Include="../chain/Chain.csproj" /></Project>')
        self.write("chain/Chain.csproj", '<Project><ProjectReference Include="../common/Shared.csproj" /></Project>')
        self.write("common/Shared.csproj", '<Project><ProjectReference Include="Missing.csproj" /></Project>')
        for revision, early in enumerate(({"required": False}, {"blocking_stage_ids": ["publish"]})):
            with self.subTest(early=early):
                late_stages = ["publish"] if revision == 0 else ["draft"]
                payload = self.discovery(dependencies=[
                    {"path": "a/Optional.csproj", "reason": "Early route", **early},
                    {"path": "b/Required.csproj", "reason": "Later required route", "blocking_stage_ids": late_stages}])
                self.save(payload, revision)
                stage = "publish" if revision == 0 else "draft"
                gaps = self.tasks.readiness("source-audit", stage_id=stage)["required_source_gaps"]
                self.assertTrue(any(gap["id"] == "source:common/Missing.csproj" for gap in gaps))
                packet = self.controller._packet("source-audit", stage_id=stage)
                self.assertTrue(any("common/Missing.csproj" in gap for gap in packet["coverage"]["missing"]))
                missing = next(item for item in payload["dependencies"] if item["path"] == "common/Missing.csproj")
                self.assertTrue(missing.get("required", True))
                self.assertEqual(set(missing.get("blocking_stage_ids", [])), set(["publish"] if revision == 0 else ["draft", "publish"]))
                if revision == 0:
                    self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])

    def test_explicit_optional_duplicate_cannot_erase_independently_discovered_graph_edge(self):
        self.task()
        self.write("a/Optional.csproj", '<Project><ProjectReference Include="../common/Shared.csproj" /></Project>')
        self.write("b/Required.csproj", '<Project><ProjectReference Include="../chain/Chain.csproj" /></Project>')
        self.write("chain/Chain.csproj", '<Project><ProjectReference Include="../common/Shared.csproj" /></Project>')
        self.write("common/Shared.csproj", '<Project><ProjectReference Include="Missing.csproj" /></Project>')
        self.save(self.discovery(dependencies=[
            {"path": "a/Optional.csproj", "reason": "Early optional route", "required": False},
            {"path": "b/Required.csproj", "reason": "Late required route", "blocking_stage_ids": ["publish"]},
            {"path": "common/Missing.csproj", "from": "common/Shared.csproj", "reason": ".NET project/solution reference", "required": False}]))
        self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])
        self.assertTrue(any(gap["id"] == "source:common/Missing.csproj" for gap in self.tasks.readiness("source-audit", stage_id="publish")["required_source_gaps"]))
        self.assertTrue(any("common/Missing.csproj" in gap for gap in self.controller._packet("source-audit", stage_id="publish")["coverage"]["missing"]))

    def test_irrelevant_stage_bodies_are_omitted_with_current_hashes_before_packet_bound(self):
        self.task()
        dependencies = []
        for name in ("one.txt", "two.txt", "three.txt"):
            self.write(name, "x" * 360000)
            dependencies.append({"path": name, "reason": "Publication annex", "blocking_stage_ids": ["publish"]})
        self.save(self.discovery(dependencies=dependencies))
        packet = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertIn("guide.md", packet["coverage"]["included"])
        omissions = self.context(packet, "omitted_sources")
        for name in ("one.txt", "two.txt", "three.txt"):
            self.assertNotIn(name, packet["coverage"]["included"])
            entry = next(item for item in omissions if item["path"] == name)
            self.assertEqual(entry["sha256"], hashlib.sha256(b"x" * 360000).hexdigest())
            self.assertEqual(entry["status"], "AVAILABLE")
            self.assertEqual(entry["blocking_stage_ids"], ["publish"])
            self.assertEqual(entry["body_status"], "OMITTED_IRRELEVANT_STAGE")
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.controller._packet("source-audit", stage_id="publish")

    def test_large_optional_bodies_are_transparently_omitted_without_required_coverage_loss(self):
        self.task()
        dependencies = []
        for name in ("one.txt", "two.txt", "three.txt"):
            self.write(name, "x" * 360000)
            dependencies.append({"path": name, "reason": "Optional background", "required": False})
        self.save(self.discovery(dependencies=dependencies))
        packet = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertIn("guide.md", packet["coverage"]["included"])
        omissions = self.context(packet, "omitted_sources")
        self.assertEqual({entry["path"] for entry in omissions}, {"one.txt", "two.txt", "three.txt"})
        self.assertTrue(all(entry["body_status"] == "OMITTED_OPTIONAL_INPUT" for entry in omissions))

    def test_optional_native_inputs_omit_large_bound_representations_with_binding_metadata(self):
        self.task()
        dependencies, bindings = [], []
        for name in ("one", "two", "three"):
            native = self.repo / (name + ".bin")
            native.write_bytes(b"\xff\x00" + name.encode())
            readable = self.write(name + ".txt", "x" * 360000)
            dependencies.append({"path": name + ".bin", "reason": "Optional native background", "required": False})
            bindings.append({"artifact_path": name + ".bin", "artifact_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
                             "path": name + ".txt", "sha256": hashlib.sha256(readable.read_bytes()).hexdigest(), "method": "Controlled readable native extraction"})
        payload = self.discovery(dependencies=dependencies)
        payload["representations"] = bindings
        self.save(payload)
        packet = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertIn("guide.md", packet["coverage"]["included"])
        metadata = self.context(packet, "artifact_representations")
        omissions = self.context(packet, "omitted_sources")
        for binding in bindings:
            self.assertNotIn(binding["path"], packet["coverage"]["included"])
            omitted = next(item for item in omissions if item["path"] == binding["path"])
            self.assertEqual(omitted["body_status"], "OMITTED_OPTIONAL_INPUT")
            recorded = next(item for item in metadata if item["artifact_path"] == binding["artifact_path"])
            for key, value in binding.items():
                self.assertEqual(recorded[key], value)
            self.assertEqual(recorded["body_status"], "OMITTED_OPTIONAL_INPUT")
        payload["scope"]["dependencies"][0]["required"] = True
        payload["dependencies"][0]["required"] = True
        self.save(payload, 1)
        required = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertIn("one.txt", required["coverage"]["included"])
        self.assertNotIn("two.txt", required["coverage"]["included"])

    def test_stage_artifacts_and_actual_owned_changes_keep_review_bodies_despite_input_mapping(self):
        value = self.task(extra=["scratch.md"])
        self.write("scratch.md", "Original draft work")
        self.save(self.discovery(resources=["guide.md", "scratch.md"], dependencies=[
            {"path": "guide.md", "reason": "Used again at publication", "blocking_stage_ids": ["publish"]},
            {"path": "scratch.md", "reason": "Used again at publication", "blocking_stage_ids": ["publish"]}]))
        self.controller._save("baseline", "source-audit", source_snapshot(self.store, value, "source-audit"))
        self.write("scratch.md", "Current draft change")
        packet = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertIn("guide.md", packet["coverage"]["included"])
        self.assertIn("scratch.md", packet["coverage"]["included"])
        self.assertEqual(self.context(packet, "omitted_sources"), [])

    def test_unreadable_input_remains_fingerprinted_and_only_blocks_required_stage(self):
        value = self.task()
        policy = self.write("policy.md", "Mandatory publication policy")
        original = Path.open
        def denied(path, *args, **kwargs):
            if path == policy:
                raise PermissionError(13, "fixture source denied", str(path))
            return original(path, *args, **kwargs)
        for index, metadata in enumerate(({"required": False}, {"blocking_stage_ids": ["publish"]})):
            with self.subTest(metadata=metadata):
                self.save(self.discovery(dependencies=[{"path": "policy.md", "reason": "Publication policy", **metadata}]), index)
                before = source_snapshot(self.store, value, "source-audit")
                with patch.object(Path, "open", denied):
                    observed = source_snapshot(self.store, value, "source-audit")
                    self.assertNotEqual(before["fingerprint"], observed["fingerprint"])
                    self.assertTrue(observed["unavailable"])
                    packet = self.controller._packet("source-audit", stage_id="draft")
                    self.assertEqual(packet["coverage"]["missing"], [])
                    self.assertTrue(any(item["path"] == "policy.md" and item["status"] == "UNAVAILABLE" for item in self.context(packet, "source_availability")))
                    self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])
                    if index:
                        self.assertTrue(self.tasks.readiness("source-audit", stage_id="publish")["required_source_gaps"])
                        self.assertTrue(self.controller._packet("source-audit", stage_id="publish")["coverage"]["missing"])

    def test_optional_redirected_source_is_still_a_path_safety_error(self):
        value = self.task()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "policy.md").write_text("External canary", encoding="utf-8")
        alias = self.repo / "redirect"
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(outside)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(outside, target_is_directory=True)
            self.addCleanup(alias.unlink)
        self.save(self.discovery(dependencies=[{"path": "redirect/policy.md", "reason": "Optional background", "required": False}]))
        with self.assertRaisesRegex(ValueError, "Source|source|redirect"):
            source_snapshot(self.store, value, "source-audit")

    def represented_document(self):
        self.task("guide.docx")
        artifact = self.repo / "guide.docx"
        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
            package.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Confirmed guide</w:t></w:r></w:p></w:body></w:document>')
        readable = self.write("guide.txt", "Confirmed guide")
        payload = self.discovery(resources=["guide.docx"])
        payload["representations"] = [{"artifact_path": "guide.docx", "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                       "path": "guide.txt", "sha256": hashlib.sha256(readable.read_bytes()).hexdigest(), "method": "OOXML text extraction by the document author"}]
        return payload

    def test_bound_readable_docx_representation_clears_only_its_binary_gap(self):
        payload = self.represented_document()
        payload["scope"]["dependencies"].append({"path": "guide.docx", "reason": "Optional prior version input; current-stage native output is mandatory", "required": False})
        self.save(payload)
        self.controller.check("source-audit", "draft")
        request = self.controller._request("source-audit", "ac", AC_SCHEMA)
        packet = validate_packet(request["source_packet"])
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertIn("guide.txt", packet["coverage"]["included"])
        metadata = self.context(packet, "artifact_representations")
        self.assertEqual(metadata[0]["artifact_sha256"], payload["representations"][0]["artifact_sha256"])
        self.assertEqual(metadata[0]["sha256"], payload["representations"][0]["sha256"])
        judgment = {"criteria": [{"id": "AC1", "expected": request["source_packet"]["requirements"][1].split(": ", 1)[1], "status": "PASS", "evidence": ["guide.txt and bound native artifact"], "method": "Controlled parser proof only", "limitations": [], "missing_coverage": []}]}
        events = [{"type": "thread.started", "thread_id": "protocol-control"}, {"type": "turn.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(judgment)}}, {"type": "turn.completed", "usage": {}}]
        parsed = _parse(CodexAdapter(), request, b"\n".join(canonical(event) for event in events), canonical(judgment))
        self.assertEqual(parsed["status"], "COMPLETE", "Parser control proves transport coverage only, never independent semantic acceptance")

    def test_stale_artifact_or_representation_keeps_binary_coverage_blocked(self):
        payload = self.represented_document()
        self.save(payload)
        original = (self.repo / "guide.docx").read_bytes()
        for path, changed in (("guide.docx", original + b"changed"), ("guide.txt", b"Different guide")):
            with self.subTest(path=path):
                (self.repo / "guide.docx").write_bytes(original)
                (self.repo / "guide.txt").write_text("Confirmed guide", encoding="utf-8")
                before = source_snapshot(self.store, self.tasks.snapshot("source-audit")["contract"], "source-audit")
                (self.repo / path).write_bytes(changed)
                packet = self.controller._packet("source-audit")
                self.assertTrue(any("guide.docx" in gap for gap in packet["coverage"]["missing"]))
                self.assertNotEqual(before["fingerprint"], source_snapshot(self.store, self.tasks.snapshot("source-audit")["contract"], "source-audit")["fingerprint"])

    def test_undeclared_sidecar_does_not_suppress_binary_gap(self):
        self.represented_document()
        self.save(self.discovery(resources=["guide.docx", "guide.txt"]))
        self.assertTrue(any("guide.docx" in gap for gap in self.controller._packet("source-audit")["coverage"]["missing"]))

    def test_representation_paths_hashes_and_current_content_are_validated(self):
        payload = self.represented_document()
        for update in ({"path": "../escape.txt"}, {"path": "C:/escape.txt"}, {"path": ".git/config"}, {"sha256": "wrong"}, {"sha256": "a" * 64}, {"artifact_sha256": "b" * 64}):
            bad = copy.deepcopy(payload)
            bad["representations"][0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.save(bad)

    def test_duplicate_or_unscoped_representation_is_rejected(self):
        payload = self.represented_document()
        duplicate = copy.deepcopy(payload)
        duplicate["representations"].append(copy.deepcopy(duplicate["representations"][0]))
        with self.assertRaises(ValueError):
            validate_discovery(duplicate)
        unscoped = copy.deepcopy(payload)
        unscoped["representations"][0]["artifact_path"] = "unrelated.docx"
        with self.assertRaises(ValueError):
            self.save(unscoped)

    def test_representation_per_file_and_total_packet_bounds_fail_closed(self):
        payload = self.represented_document()
        self.write("guide.txt", "x" * 512001)
        payload["representations"][0]["sha256"] = hashlib.sha256((self.repo / "guide.txt").read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "bound"):
            self.save(payload)
        self.write("guide.txt", "Confirmed guide")
        payload["representations"][0]["sha256"] = hashlib.sha256(b"Confirmed guide").hexdigest()
        self.save(payload)
        # Three individually bounded real files exceed the one-megabyte packet.
        for name in ("one.txt", "two.txt", "three.txt"):
            self.write(name, "x" * 400000)
            payload["scope"]["dependencies"].append({"path": name, "reason": "Mandatory appendix"})
        self.save(payload, 1)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.controller._packet("source-audit")


    def legacy_save(self, intake):
        # The original 3eb3ec9 validator accepted this exact uppercase payload.
        # Model that historical validation seam, while exercising the actual
        # current append/CAS writer and all subsequent real read boundaries.
        payload = self.discovery()
        payload["intake"] = copy.deepcopy(intake)
        with patch("ase.discovery.validate_intake", return_value=copy.deepcopy(intake)):
            self.save(payload)

    def test_legacy_incomplete_intake_blocks_reads_without_mutating_saved_attribution(self):
        self.task()
        raw_intake = self.intake("UNAVAILABLE")
        self.legacy_save(raw_intake)
        before = self.store.db.execute("SELECT payload FROM task_discoveries WHERE task_id=?", ("source-audit",)).fetchone()[0]
        with self.assertRaisesRegex(ValueError, "Incomplete source"):
            validate_intake(raw_intake)
        readiness = self.tasks.readiness("source-audit", stage_id="draft")
        self.assertEqual(readiness["clarity"], "BLOCKED")
        gap = next(item for item in readiness["required_source_gaps"] if item["id"] == "compatibility:intake-validation")
        self.assertIn("policy", gap["source_refs"])
        with self.assertRaisesRegex(ValueError, "source|gap"):
            self.tasks.require_approval("source-audit", "draft")
        packet = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertTrue(any("compatibility:intake-validation" in gap for gap in packet["coverage"]["missing"]))
        validation = self.context(packet, "intake_validation")
        self.assertEqual(validation["status"], "INVALID_LEGACY_INTAKE")
        self.assertEqual(validation["source_statuses"], [{"id": "policy", "status": "unavailable"}])
        self.assertEqual(self.context(packet, "discovery")["intake"]["source_refs"][0]["status"], "UNAVAILABLE")
        after = self.store.db.execute("SELECT payload FROM task_discoveries WHERE task_id=?", ("source-audit",)).fetchone()[0]
        self.assertEqual(after, before)

    def test_legacy_intake_stays_blocked_on_plain_refresh_and_repairs_through_normal_api(self):
        self.task()
        self.legacy_save(self.intake("UNAVAILABLE"))
        self.save(self.discovery(), 1)
        self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["clarity"], "BLOCKED")
        repaired = self.discovery()
        repaired["intake"] = self.intake("AVAILABLE")
        repaired["intake"]["source_refs"][0]["reference"] = "local:policy.md#newly-acquired"
        self.save(repaired, 2)
        self.assertEqual(self.tasks.require_approval("source-audit", "draft")["state"], "READY")
        packet = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(self.context(packet, "intake_validation")["status"], "VALID")

    def test_valid_legacy_optional_and_scoped_gaps_keep_their_current_readiness(self):
        self.task()
        intake = self.intake(" UNAVAILABLE ")
        intake["gaps"] = [{"id": "optional-policy", "text": "Optional background is unavailable", "source_refs": ["policy"], "required": False},
                          {"id": "publication-policy", "text": "Publication needs policy", "source_refs": ["policy"], "required": True, "blocking_stage_ids": ["publish"]}]
        self.legacy_save(intake)
        self.assertEqual(self.tasks.readiness("source-audit", stage_id="draft")["required_source_gaps"], [])
        self.tasks.require_approval("source-audit", "draft")
        publish = self.tasks.readiness("source-audit", stage_id="publish")["required_source_gaps"]
        self.assertEqual([item["id"] for item in publish], ["publication-policy"])
        packet = validate_packet(self.controller._packet("source-audit", stage_id="draft"))
        self.assertEqual(packet["coverage"]["missing"], [])
        self.assertEqual(self.context(packet, "intake_validation")["status"], "VALID")
        self.assertEqual(self.context(packet, "intake_validation")["source_statuses"][0]["status"], "unavailable")

    def retained_v1_task(self):
        """A v1 task as the retired v1 engine stored it (row, transition, contract and baseline files)."""
        from legacy_v1 import make_legacy_task
        subprocess.run(["git", "init", "-b", "feature/legacy-status"], cwd=self.repo, check=True, capture_output=True)
        make_legacy_task(self.store, {"id": "legacy-status", "request": "Retain the original task history", "risk": "MEDIUM", "max_attempts": 2,
                          "acceptance": [{"id": "OLD-AC", "expected": "Original configured result", "commands": ["old-check"]}],
                          "commands": [{"id": "old-check", "kind": "test", "required": True, "parser": "unittest",
                                        "argv": [sys.executable, "-c", "from pathlib import Path;Path('legacy-command-ran').write_text('unexpected')"]}]})
        return self.tasks.snapshot("legacy-status")

    def legacy_public_cli(self, *arguments):
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run([sys.executable, "-B", "-m", "ase", "--home", str(self.store.state_home), "--repo", str(self.repo), *arguments],
                             cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, timeout=20)
        return run.returncode, json.loads(run.stdout)

    def test_public_legacy_status_routes_preserve_readable_history_without_rewriting_it(self):
        legacy = self.retained_v1_task()
        before = tuple(self.store.db.execute("SELECT * FROM tasks WHERE id='legacy-status'").fetchone())
        transitions = [tuple(row) for row in self.store.db.execute("SELECT * FROM transitions WHERE task_id='legacy-status' ORDER BY rowid")]
        records = [tuple(row) for row in self.store.db.execute("SELECT * FROM runtime_records ORDER BY kind,key")]
        for arguments in (("status", "legacy-status"), ("task", "status", "legacy-status")):
            with self.subTest(arguments=arguments):
                exit_code, result = self.legacy_public_cli(*arguments)
                self.assertEqual(exit_code, 0, result)
                self.assertTrue(result["legacy_v1"])
                self.assertEqual(result["contract"], legacy["contract"])
                self.assertEqual(result["state"], "PLANNED")
                self.assertEqual(result["readiness"]["clarity"], "UNASSESSED")
                self.assertFalse(result["readiness"]["execution_authorized"])
        self.assertEqual(tuple(self.store.db.execute("SELECT * FROM tasks WHERE id='legacy-status'").fetchone()), before)
        self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM transitions WHERE task_id='legacy-status' ORDER BY rowid")], transitions)
        self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM runtime_records ORDER BY kind,key")], records)

    def test_legacy_readiness_has_no_v2_stage_or_public_execution_authority(self):
        self.retained_v1_task()
        with self.assertRaisesRegex(ValueError, "legacy|v2"):
            self.tasks.readiness("legacy-status", stage_id="draft")
        with self.assertRaisesRegex(ValueError, "legacy|approval"):
            self.tasks.require_approval("legacy-status", "draft")
        for arguments in (("task", "check", "legacy-status"), ("task", "run", "legacy-status"),
                          ("task", "discover", "legacy-status", "--expected-revision", "0")):
            with self.subTest(arguments=arguments):
                exit_code, result = self.legacy_public_cli(*arguments)
                self.assertEqual(exit_code, 3, result)
                self.assertIn("v2", result["error"])
        self.assertFalse((self.repo / "legacy-command-ran").exists())

    def test_legacy_readiness_guard_stops_after_normal_reviewed_v2_migration(self):
        self.retained_v1_task()
        revised = self.tasks.revise("legacy-status", contract("legacy-status"), expected_revision=0)
        self.assertFalse(revised["legacy_v1"])
        self.assertNotIn("execution_authorized", self.tasks.readiness("legacy-status"))
        proposed = self.tasks.propose("legacy-status", {"summary": "Review the migrated draft scope", "stage_ids": ["draft"]}, expected_revision=revised["revision"])
        self.tasks.approve("legacy-status", proposal_hash=proposed["proposal_hash"], user_message_ref="fixture:explicit-migrated-v2-approval", approved_stage_ids=["draft"])
        self.assertEqual(self.tasks.require_approval("legacy-status", "draft")["state"], "READY")
        self.assertFalse((self.repo / "legacy-command-ran").exists())

    def test_completed_readonly_stage_gate_does_not_reject_terminal_state(self):
        self.task()
        self.controller.check("source-audit", "draft")
        # Persisted-state compatibility fixture only: no semantic gate or
        # independent approval is fabricated. Missing AC still blocks below.
        record = read_record(self.store.db, "task", "source-audit")
        with transaction(self.store.db):
            self.tasks._save({**record["payload"], "state": "COMPLETED"}, record["revision"])
        result = GateService(self.store).gate("source-audit", stage_id="draft")
        self.assertNotIn("Task is terminal", result["blocked_reasons"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(any("Independent" in reason for reason in result["blocked_reasons"]))
        self.assertEqual(self.tasks.snapshot("source-audit")["state"], "COMPLETED")
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.tasks.require_approval("source-audit", "draft")

    def test_cancelled_stage_gate_still_rejects_readonly_authority(self):
        self.task()
        task = self.tasks.snapshot("source-audit")
        self.tasks.transition("source-audit", "CANCELLED", expected_revision=task["revision"], reason="Owner cancellation")
        result = GateService(self.store).gate("source-audit", stage_id="draft")
        self.assertIn("Task is cancelled", result["blocked_reasons"])
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.tasks.require_approval("source-audit", "draft", allow_terminal=True)

    def test_stopped_stage_gate_still_rejects_readonly_authority(self):
        self.task()
        self.controller.request_stop("source-audit", reason="Owner pause")
        result = GateService(self.store).gate("source-audit", stage_id="draft")
        self.assertTrue(any("paused" in reason for reason in result["blocked_reasons"]))
        with self.assertRaisesRegex(ValueError, "paused"):
            self.tasks.require_approval("source-audit", "draft", allow_terminal=True)


if __name__ == "__main__":
    unittest.main()

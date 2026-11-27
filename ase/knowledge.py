"""Dated project knowledge. Append history; never infer present truth or authority."""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
from urllib.parse import parse_qsl, urlsplit
import uuid

from .continuity import _strings, _text
from .memory.conflicts import conflicting_alternatives
from .store import now
from .state import append_event, read_record, text, transaction, write_record


def _date(value, name):
    text(value, name, limit=80)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(name + " requires a timezone-aware date")
    return parsed.astimezone(timezone.utc)


def _reference(value):
    text(value, "source reference", limit=2048)
    parsed = urlsplit(value)
    credential_key = re.compile(r"(?i)(?:^|[-_])(?:token|secret|password|authorization|credential|signature|api[-_]?key)(?:$|[-_])|^sig$")
    if parsed.username is not None or any(credential_key.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("Source references must not contain credentials")
    return value


class Knowledge:
    def __init__(self, store):
        self.store = store

    def record(self, payload, *, expected_revision=None):
        allowed = {"topic", "statement", "observed_at", "source_refs", "task_id", "repository", "repository_revision", "supersedes", "kind", "scope", "refresh_policy", "dependencies"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("Unknown knowledge fields")
        value = dict(payload)
        for key in ("topic", "statement", "observed_at"):
            _text(value.get(key), key)
        if len(value["topic"]) > 200 or len(value["statement"]) > 4000 or len(value["observed_at"]) > 80:
            raise ValueError("Use a concise topic, statement and observed_at")
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("observed_at requires a timezone-aware date")
        refs = _strings(value.get("source_refs"), "source_refs")
        if not refs or len(refs) > 20 or any(len(ref) > 2048 for ref in refs):
            raise ValueError("Knowledge needs 1 to20 concise source references")
        for ref in refs:
            _reference(ref)
        metadata = self._new_metadata(value)
        task_id = value.get("task_id")
        if task_id is not None:
            _text(task_id, "task_id")
            self.store.task(task_id)
        repository, repository_revision = value.get("repository"), value.get("repository_revision")
        if repository is not None or repository_revision is not None:
            _reference(repository)
            if not isinstance(repository_revision, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", repository_revision):
                raise ValueError("Repository observations require a full commit SHA")
        predecessor = value.get("supersedes")
        if predecessor is not None and (type(predecessor) is not int or predecessor <= 0):
            raise ValueError("supersedes must identify a prior claim")
        db = self.store.db
        with transaction(db):
            revision = self._revision()
            self._require_revision(expected_revision, revision)
            if predecessor is not None:
                previous = db.execute("SELECT * FROM knowledge_claims WHERE id=?", (predecessor,)).fetchone()
                if previous is None or previous["topic"] != value["topic"]:
                    raise ValueError("Superseded claim must exist in the same project and topic")
                previous_metadata = self._metadata(predecessor)
                if previous_metadata and previous_metadata["workspace_id"] != self.store.workspace_id:
                    raise ValueError("Superseded claim belongs to another workspace; explicit cross-workspace authority is required")
                if db.execute("SELECT 1 FROM knowledge_claims WHERE supersedes=?", (predecessor,)).fetchone():
                    raise ValueError("Claim already superseded; query and reconcile newer history")
                if observed < datetime.fromisoformat(previous["observed_at"]):
                    raise ValueError("Superseding observation cannot predate its predecessor")
            row = db.execute("INSERT INTO knowledge_claims(topic,statement,observed_at,source_refs,task_id,repository,repository_revision,supersedes,created) VALUES(?,?,?,?,?,?,?,?,?)", (
                value["topic"], value["statement"], observed.isoformat(), json.dumps(refs), task_id, repository, repository_revision, predecessor, now()))
            claim_id = row.lastrowid
            self._save("knowledge_claim", str(claim_id), metadata)
            if "dependencies" in value:
                dependencies = []
                for edge in self._list(value["dependencies"], "dependencies"):
                    if not isinstance(edge, dict) or "claim_id" in edge:
                        raise ValueError("New claim dependencies must be objects without a claim_id")
                    dependencies.append({**edge, "claim_id": claim_id})
                self._write_dependencies(dependencies)
            self._save_validity(claim_id, "UNKNOWN", ["Declared dependencies have not been verified"])
            revision = self._bump(revision)
            self._event("knowledge_claim", {"claim_id": claim_id, "metadata": metadata, "supersedes": predecessor})
        claim = next(claim for claim in self.query(value["topic"])["claims"] if claim["id"] == claim_id)
        return {**claim, **metadata, "validity": "UNKNOWN", "revision": revision}

    def _revision(self):
        return read_record(self.store.db, "knowledge_revision", self.store.workspace_id)["revision"]

    @staticmethod
    def _require_revision(expected, actual):
        if expected is not None:
            if type(expected) is not int or expected < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            if expected != actual:
                raise ValueError("Stale knowledge revision; reload and reconcile")

    def _bump(self, revision):
        return write_record(self.store.db, "knowledge_revision", self.store.workspace_id,
                            {"workspace_id": self.store.workspace_id}, expected_revision=revision)["revision"]

    def _save(self, kind, key, payload):
        previous = read_record(self.store.db, kind, key)
        if previous["payload"] == payload:
            return False
        write_record(self.store.db, kind, key, payload, expected_revision=previous["revision"])
        return True

    def _event(self, kind, payload):
        append_event(self.store.db, "knowledge:" + str(uuid.uuid4()), kind, None,
                     {"workspace_id": self.store.workspace_id, **payload})

    @staticmethod
    def _list(value, name):
        if not isinstance(value, list) or len(value) > 1000:
            raise ValueError(name + " must be a bounded list")
        return value

    def _scope(self, scope):
        allowed = {"project_id", "workspace_id", "paths", "resource_ids", "source_ids", "claim_ids"}
        if not isinstance(scope, dict) or set(scope) - allowed:
            raise ValueError("Unknown knowledge scope fields")
        for key in ("project_id", "workspace_id"):
            if key in scope and scope[key] != getattr(self.store, key):
                raise ValueError("Knowledge scope belongs to another project or workspace")
        result = {"project_id": self.store.project_id, "workspace_id": self.store.workspace_id, **scope}
        for key in ("paths", "resource_ids", "source_ids"):
            if key in result:
                result[key] = _strings(result[key], key)
                for item in result[key]:
                    text(item, key, limit=2048)
                    if key == "paths":
                        self._local_path("local:" + item, allow_shared_reference=True)
        if "claim_ids" in result:
            self._list(result["claim_ids"], "claim_ids")
            if any(type(item) is not int or item < 1 for item in result["claim_ids"]):
                raise ValueError("claim_ids must contain positive integers")
        return result

    def _new_metadata(self, value):
        kind = text(value.get("kind", "fact"), "kind", limit=80)
        policy = value.get("refresh_policy", {})
        if not isinstance(policy, dict) or set(policy) - {"max_age_seconds", "pre_mutation_probe"}:
            raise ValueError("Unknown refresh_policy fields")
        if "max_age_seconds" in policy and (type(policy["max_age_seconds"]) is not int or not 1 <= policy["max_age_seconds"] <= 31536000):
            raise ValueError("max_age_seconds must be a positive bounded integer")
        if "pre_mutation_probe" in policy and type(policy["pre_mutation_probe"]) is not bool:
            raise ValueError("pre_mutation_probe must be boolean")
        return {"kind": kind, "scope": self._scope(value.get("scope", {})),
                "workspace_id": self.store.workspace_id, "refresh_policy": dict(policy)}

    def _metadata(self, claim_id):
        return read_record(self.store.db, "knowledge_claim", str(claim_id))["payload"]

    def _local_path(self, locator, *, allow_shared_reference=False):
        from .project_local import is_private_path
        if not locator.startswith("local:") or not locator[6:]:
            raise ValueError("File source requires a local: reference")
        if is_private_path(locator[6:]):
            raise ValueError("Private project-local state is not a knowledge source")
        path = (self.store.repo / locator[6:]).resolve()
        if not path.is_relative_to(self.store.repo):
            raise ValueError("Local source is outside the selected workspace")
        if is_private_path(path.relative_to(self.store.repo)):
            raise ValueError("Private project-local state is not a knowledge source")
        # Metadata may retain an unavailable source reference and its history.
        # Actual verification/reading still rejects shared inodes before bytes.
        if not allow_shared_reference and path.is_file() and path.stat().st_nlink > 1:
            raise ValueError("Hardlinked files are not knowledge sources")
        return path

    def _source_key(self, source_id):
        return self.store.workspace_id + ":" + source_id

    def _source(self, source_id):
        return read_record(self.store.db, "knowledge_source", self._source_key(source_id))["payload"]

    def _observation(self, value):
        allowed = {"source_id", "workspace_id", "locator", "kind", "version", "observed_at_utc", "availability", "expires_at_utc"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Unknown source observation fields")
        source_id = text(value.get("source_id"), "source_id", limit=200)
        if value.get("workspace_id", self.store.workspace_id) != self.store.workspace_id:
            raise ValueError("Source observation belongs to another workspace")
        locator = _reference(value.get("locator"))
        kind = value.get("kind", "file" if locator.startswith("local:") else "live")
        if kind not in {"file", "live"}:
            raise ValueError("Source kind must be file or live")
        if kind == "file":
            self._local_path(locator, allow_shared_reference=True)
        elif locator.startswith("local:"):
            raise ValueError("Local file observations cannot be recorded as live probes")
        availability = value.get("availability")
        if availability not in {"AVAILABLE", "UNKNOWN", "UNAVAILABLE"}:
            raise ValueError("Invalid source availability")
        version = value.get("version")
        if availability == "AVAILABLE" or version is not None:
            text(version, "version", limit=2048)
            if kind == "file" and not re.fullmatch(r"sha256:[a-f0-9]{64}", version):
                raise ValueError("File version requires sha256 content hash")
        observed = _date(value.get("observed_at_utc"), "observed_at_utc")
        if observed > datetime.now(timezone.utc):
            raise ValueError("Source observation cannot be in the future")
        result = {**value, "source_id": source_id, "workspace_id": self.store.workspace_id,
                  "kind": kind, "version": version, "observed_at_utc": observed.isoformat()}
        if "expires_at_utc" in value:
            expires = _date(value["expires_at_utc"], "expires_at_utc")
            if expires < observed:
                raise ValueError("Source expiry cannot predate observation")
            result["expires_at_utc"] = expires.isoformat()
        return result

    def _write_dependencies(self, dependencies):
        grouped = {}
        for value in self._list(dependencies, "dependencies"):
            allowed = {"claim_id", "source_id", "depends_on_claim_id", "observed_version", "relation"}
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError("Unknown dependency fields")
            claim_id = value.get("claim_id")
            if type(claim_id) is not int or claim_id < 1:
                raise ValueError("Dependency requires a claim_id")
            if not self.store.db.execute("SELECT 1 FROM knowledge_claims WHERE id=?", (claim_id,)).fetchone():
                raise ValueError("Dependency claim is absent from this project")
            metadata = self._metadata(claim_id)
            if metadata is None:
                metadata = self._new_metadata({})
                self._save("knowledge_claim", str(claim_id), metadata)
            if metadata["workspace_id"] != self.store.workspace_id:
                raise ValueError("Dependency claim belongs to another workspace")
            source_id, target = value.get("source_id"), value.get("depends_on_claim_id")
            if (source_id is None) == (target is None):
                raise ValueError("Dependency requires exactly one source_id or depends_on_claim_id")
            edge = dict(value, relation=text(value.get("relation", "evidence"), "relation", limit=200))
            if source_id is not None:
                text(source_id, "source_id", limit=200)
                text(value.get("observed_version"), "observed_version", limit=2048)
            else:
                if type(target) is not int or target < 1:
                    raise ValueError("depends_on_claim_id must be a positive integer")
                if not self.store.db.execute("SELECT 1 FROM knowledge_claims WHERE id=?", (target,)).fetchone():
                    raise ValueError("Dependent claim is absent from this project")
                target_metadata = self._metadata(target)
                if target_metadata and target_metadata["workspace_id"] != self.store.workspace_id:
                    raise ValueError("Dependent claim belongs to another workspace")
                if "observed_version" in edge:
                    raise ValueError("Claim dependencies use immutable claim IDs, not source versions")
            if edge in grouped.setdefault(claim_id, []):
                raise ValueError("Duplicate dependency")
            grouped[claim_id].append(edge)
        # For each named owner the batch replaces its entire declared edge list.
        # Claims omitted from this batch retain their independent dependencies.
        for claim_id, edges in grouped.items():
            self._save("knowledge_dependencies", str(claim_id), edges)
            self._event("knowledge_dependencies", {"claim_id": claim_id, "dependencies": edges})

    def record_observations(self, observations, dependencies, *, expected_revision):
        """Save explicit source versions and replacement edge sets atomically.

        This records supplied probe results; it never executes a locator or an
        infrastructure command. Retrieval independently hashes local files.
        """
        observations = [self._observation(item) for item in self._list(observations, "observations")]
        if len({item["source_id"] for item in observations}) != len(observations):
            raise ValueError("Duplicate source observations")
        if expected_revision is None:
            raise ValueError("expected_revision is required for observations")
        with transaction(self.store.db):
            revision = self._revision()
            self._require_revision(expected_revision, revision)
            for observation in observations:
                previous = self._source(observation["source_id"])
                if previous and (previous["locator"], previous["kind"]) != (observation["locator"], observation["kind"]):
                    raise ValueError("Source locator and kind cannot change; use a new source_id")
                self._save("knowledge_source", self._source_key(observation["source_id"]), observation)
                self._event("knowledge_observation", {"observation": observation})
            self._write_dependencies(dependencies)
            revision = self._bump(revision)
        return {"revision": revision, "workspace_id": self.store.workspace_id,
                "observations": observations, "dependency_discovery_complete": False}

    def _save_validity(self, claim_id, validity, reasons, *, source_validity=None, conflicts=None):
        payload = {"workspace_id": self.store.workspace_id, "validity": validity, "reasons": sorted(set(reasons)),
                   "source_validity": source_validity or validity, "conflicts": conflicts or [],
                   "review_required": bool(conflicts)}
        if self._save("knowledge_validity", str(claim_id), payload):
            self._event("knowledge_validity", {"claim_id": claim_id, **payload})
            return True
        return False

    def _verify_source(self, observation):
        if observation is None:
            return None
        if observation["kind"] != "file":
            return dict(observation)
        checked = dict(observation, observed_at_utc=now())
        try:
            path = self._local_path(observation["locator"])
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
                    raise ValueError("Source must be an unshared regular file")
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError("Source changed while it was read")
            checked.update(availability="AVAILABLE", version="sha256:" + digest.hexdigest())
        except (OSError, ValueError):
            checked.update(availability="UNAVAILABLE", version=None)
        return checked

    def retrieve(self, *, topic=None, scope, refresh=True):
        """Check declared dependencies without interpreting or rewriting claims.

        Scope selects roots; their explicit claim dependencies are followed even
        outside the selected topic. Active alternatives are checked independently
        of root selection. Source freshness is separate from unresolved review.
        No semantic contradiction detection or implicit source discovery occurs.
        """
        if type(refresh) is not bool:
            raise ValueError("refresh must be boolean")
        selected_scope = self._scope(scope)
        if topic is not None:
            _text(topic, "topic")
        with transaction(self.store.db):
            all_claims = {claim["id"]: claim for claim in self.query()["claims"]}
            metadata = {key: self._metadata(key) for key in all_claims}
            dependencies = {key: read_record(self.store.db, "knowledge_dependencies", str(key))["payload"] or [] for key in all_claims}
            selected_paths = {self._local_path("local:" + item, allow_shared_reference=True) for item in scope.get("paths", [])}
            source_metadata = {}
            selected = []
            for claim_id, claim in all_claims.items():
                if topic is not None and claim["topic"] != topic:
                    continue
                if "claim_ids" in scope and claim_id not in scope["claim_ids"]:
                    continue
                if "source_ids" in scope and not any(edge.get("source_id") in scope["source_ids"] for edge in dependencies[claim_id]):
                    continue
                if "paths" in scope:
                    meta = metadata[claim_id] or {}
                    refs = list(claim["source_refs"])
                    refs.extend("local:" + item for item in meta.get("scope", {}).get("paths", []))
                    if meta.get("workspace_id") == self.store.workspace_id:
                        for edge in dependencies[claim_id]:
                            if "source_id" in edge:
                                source_id = edge["source_id"]
                                if source_id not in source_metadata:
                                    source_metadata[source_id] = self._source(source_id)
                                if source_metadata[source_id]:
                                    refs.append(source_metadata[source_id]["locator"])
                    declared_paths = set()
                    for ref in refs:
                        if ref.startswith("local:"):
                            try:
                                declared_paths.add(self._local_path(ref, allow_shared_reference=True))
                            except (OSError, ValueError):
                                # Legacy out-of-scope links remain historical
                                # references; they cannot expand a read scope.
                                continue
                    if not selected_paths.intersection(declared_paths):
                        continue
                if "resource_ids" in scope:
                    declared = (metadata[claim_id] or {}).get("scope", {}).get("resource_ids", [])
                    if not set(scope["resource_ids"]).intersection(declared):
                        continue
                selected.append(claim_id)
            needed, pending = set(), list(selected)
            while pending:
                claim_id = pending.pop()
                if claim_id in needed:
                    continue
                needed.add(claim_id)
                if (metadata.get(claim_id) or {}).get("workspace_id") == self.store.workspace_id:
                    pending.extend(edge["depends_on_claim_id"] for edge in dependencies.get(claim_id, []) if "depends_on_claim_id" in edge)
            # Select relevant topics first, but retain every active peer in
            # them: a claim/source selector cannot hide an alternative. Old or
            # unrelated paths must not break a healthy scoped retrieval.
            topics = {all_claims[key]["topic"] for key in needed if key in all_claims}
            candidates, unresolved_scopes = [], []
            for claim_id, claim in all_claims.items():
                meta = metadata[claim_id]
                if (claim["superseded_by"] is not None or claim["topic"] not in topics
                        or not meta or meta["workspace_id"] != self.store.workspace_id):
                    continue
                applicability = dict(meta["scope"])
                try:
                    applicability["paths"] = [str(self._local_path("local:" + item, allow_shared_reference=True))
                                              for item in applicability.get("paths", [])]
                except (OSError, ValueError):
                    # Keep canonical in-workspace aliases when resolvable.
                    # Drift outside the workspace never authorizes a read and
                    # cannot establish disjointness; broaden this dimension.
                    applicability["paths"] = []
                    unresolved_scopes.append(claim_id)
                candidates.append({**claim, "scope": applicability})
            peers = conflicting_alternatives(candidates)
            source_ids = {edge["source_id"] for claim_id in needed
                          if (metadata.get(claim_id) or {}).get("workspace_id") == self.store.workspace_id
                          for edge in dependencies.get(claim_id, []) if "source_id" in edge}
            observations = {key: self._source(key) for key in sorted(source_ids)}
            dirty = False
            if refresh:
                observations = {key: self._verify_source(value) for key, value in observations.items()}
                for source_id, observation in observations.items():
                    if observation is None or observation["kind"] != "file":
                        continue
                    key = self._source_key(source_id)
                    previous = read_record(self.store.db, "knowledge_verification", key)["payload"]
                    # Unchanged reads need neither another history row nor a CAS
                    # revision bump. Actual changed versions remain auditable.
                    fields = ("version", "availability", "locator")
                    if previous is None or any(previous[field] != observation[field] for field in fields):
                        self._save("knowledge_verification", key, observation)
                        self._event("knowledge_observation", {"observation": observation, "verification": True})
                        dirty = True
            verified_at = datetime.now(timezone.utc)

            def evaluate(root):
                if all_claims[root]["superseded_by"] is not None:
                    return "SUPERSEDED", ["A newer recorded claim supersedes this claim"], False
                seen, pending, states, reasons = set(), [root], [], []
                has_source, live = False, False
                while pending:
                    claim_id = pending.pop()
                    if claim_id in seen:
                        continue
                    seen.add(claim_id)
                    claim = all_claims.get(claim_id)
                    meta = metadata.get(claim_id)
                    if not claim or not meta or meta["workspace_id"] != self.store.workspace_id:
                        states.append("UNKNOWN")
                        reasons.append("Claim " + str(claim_id) + " has no observations in this workspace")
                        continue
                    if claim["superseded_by"] is not None:
                        states.append("STALE")
                        reasons.append("Dependent claim " + str(claim_id) + " was superseded")
                    edges = dependencies.get(claim_id, [])
                    if not edges:
                        states.append("UNKNOWN")
                        reasons.append("Claim " + str(claim_id) + " has no declared dependencies")
                    live |= meta["kind"] == "infrastructure" or meta["refresh_policy"].get("pre_mutation_probe", False)
                    for edge in edges:
                        if "depends_on_claim_id" in edge:
                            pending.append(edge["depends_on_claim_id"])
                            continue
                        has_source = True
                        source_id = edge["source_id"]
                        source = observations.get(source_id)
                        prefix = "Source " + source_id + ": "
                        if source is None or source["availability"] == "UNKNOWN":
                            states.append("UNKNOWN")
                            reasons.append(prefix + "not observed")
                        elif source["availability"] == "UNAVAILABLE":
                            states.append("UNAVAILABLE" if claim_id == root else "STALE")
                            reasons.append(prefix + "unavailable")
                        elif source["version"] != edge["observed_version"]:
                            states.append("STALE")
                            reasons.append(prefix + "version changed")
                        if source and source["kind"] == "live":
                            live = True
                            age = (verified_at - _date(source["observed_at_utc"], "observed_at_utc")).total_seconds()
                            expired = age > meta["refresh_policy"].get("max_age_seconds", 300)
                            expired |= bool(source.get("expires_at_utc") and verified_at >= _date(source["expires_at_utc"], "expires_at_utc"))
                            if expired and source["availability"] == "AVAILABLE":
                                states.append("STALE")
                                reasons.append(prefix + "live observation expired")
                if not refresh:
                    return "UNKNOWN", ["Current dependencies were not checked for this retrieval"], live
                if not has_source:
                    states.append("UNKNOWN")
                    reasons.append("No source evidence anchors the declared dependency graph")
                validity = next((item for item in ("UNAVAILABLE", "STALE", "UNKNOWN") if item in states), "CURRENT")
                if validity == "CURRENT":
                    reasons.append("Verified declared source versions match; discovery completeness is unknown")
                return validity, sorted(set(reasons)), live

            annotated = []
            source_evaluated = {key: evaluate(key) for key in needed if key in all_claims}
            evaluated = {}
            for root, (source_validity, source_reasons, live) in source_evaluated.items():
                conflicts, seen, pending = [], set(), [root]
                while pending and source_validity != "SUPERSEDED":
                    claim_id = pending.pop()
                    if claim_id in seen:
                        continue
                    seen.add(claim_id)
                    if (metadata.get(claim_id) or {}).get("workspace_id") != self.store.workspace_id:
                        continue
                    if claim_id in peers:
                        conflicts.append({"claim_id": claim_id, "alternative_claim_ids": peers[claim_id]})
                    pending.extend(edge["depends_on_claim_id"] for edge in dependencies.get(claim_id, [])
                                   if "depends_on_claim_id" in edge)
                conflicts.sort(key=lambda item: item["claim_id"])
                reasons = list(source_reasons)
                for conflict in conflicts:
                    reasons.append("Claim " + str(conflict["claim_id"]) + " has unresolved overlapping same-topic alternatives: "
                                   + ", ".join(map(str, conflict["alternative_claim_ids"])) + "; review required")
                evaluated[root] = ("CONFLICT" if conflicts else source_validity, sorted(set(reasons)), live,
                                   source_validity, conflicts)
            for claim_id in sorted(needed):
                if claim_id not in evaluated:
                    continue
                validity, reasons, live, source_validity, conflicts = evaluated[claim_id]
                meta = metadata[claim_id]
                if refresh and meta and meta["workspace_id"] == self.store.workspace_id:
                    dirty |= self._save_validity(claim_id, validity, reasons,
                                                source_validity=source_validity, conflicts=conflicts)
            revision = self._revision()
            if dirty:
                revision = self._bump(revision)
            histories = {}
            for row in self.store.db.execute("SELECT sequence,payload,created FROM runtime_events WHERE kind='knowledge_validity' ORDER BY sequence"):
                event = json.loads(row["payload"])
                histories.setdefault(event["claim_id"], []).append({"sequence": row["sequence"], "recorded_at_utc": row["created"], **event})
            observation_history = []
            for row in self.store.db.execute("SELECT sequence,payload,created FROM runtime_events WHERE kind='knowledge_observation' ORDER BY sequence"):
                event = json.loads(row["payload"])
                if event["workspace_id"] == self.store.workspace_id and event["observation"]["source_id"] in source_ids:
                    observation_history.append({"sequence": row["sequence"], "recorded_at_utc": row["created"], **event})
            for claim_id in selected:
                validity, reasons, live, source_validity, conflicts = evaluated[claim_id]
                cached = read_record(self.store.db, "knowledge_validity", str(claim_id))["payload"] or {}
                meta = metadata[claim_id] or {"kind": "fact", "scope": {}, "workspace_id": None, "refresh_policy": {}}
                annotated.append({**all_claims[claim_id], **meta, "validity": validity, "reasons": reasons,
                    "source_validity": source_validity, "review_required": bool(conflicts), "conflicts": conflicts,
                    "dependencies": dependencies[claim_id], "pre_mutation_probe_required": live or meta["kind"] == "infrastructure",
                    "last_verified_source_validity": cached.get("source_validity", cached.get("validity", "UNKNOWN")),
                    "last_verified_validity": cached.get("validity", "UNKNOWN"), "validity_history": histories.get(claim_id, [])})
        return {"repository": str(self.store.repo), "project_id": self.store.project_id,
                "workspace_id": self.store.workspace_id, "revision": revision, "scope": selected_scope,
                "claims": annotated, "refresh_required": [claim["id"] for claim in annotated if claim["validity"] not in {"CURRENT", "SUPERSEDED"}],
                "observations": [value for value in observations.values() if value is not None],
                "observation_history": observation_history,
                "pre_mutation_probe_required": any(claim["pre_mutation_probe_required"] for claim in annotated),
                "dependency_discovery_complete": False,
                "semantic_validation": False,
                "unresolved_applicability_claim_ids": sorted(unresolved_scopes),
                "limitations": ["CURRENT checks declared dependencies only; it does not establish complete discovery or semantic truth",
                    "CONFLICT requires review of distinct overlapping same-topic alternatives; it does not prove semantic contradiction",
                    "Historical decisions retain their recorded scope; current user instructions take precedence",
                    "Live resource state must be probed immediately before each mutation; cached observations grant no authority"],
                "authority": "Historical recorded claims only; verify current code and user authority before acting"}

    def query(self, topic=None):
        if topic is not None:
            _text(topic, "topic")
        where, args = (" WHERE k.topic=?", (topic,)) if topic is not None else ("", ())
        rows = self.store.db.execute("SELECT k.*, (SELECT id FROM knowledge_claims WHERE supersedes=k.id) AS superseded_by FROM knowledge_claims k" + where + " ORDER BY k.id", args).fetchall()
        claims = []
        for row in rows:
            claim = dict(row)
            claim["source_refs"] = json.loads(claim["source_refs"])
            claims.append(claim)
        return {"repository": str(self.store.repo), "claims": claims,
                "authority": "Historical recorded claims only; verify current code and user authority before acting"}

    def lint(self):
        claims = self.query()["claims"]
        findings, topics = [], {}
        for claim in claims:
            if claim["superseded_by"] is None:
                topics.setdefault(claim["topic"], []).append(claim["id"])
            for ref in claim["source_refs"]:
                if ref.startswith("local:"):
                    path = (self.store.repo / ref[6:]).resolve()
                    if not path.is_relative_to(self.store.repo):
                        findings.append({"kind": "out_of_scope_reference", "claim_id": claim["id"], "reference": ref})
                    elif not path.is_file():
                        findings.append({"kind": "missing_local_reference", "claim_id": claim["id"], "reference": ref})
        for topic, ids in topics.items():
            if len(ids) > 1:
                findings.append({"kind": "parallel_claims", "topic": topic, "claim_ids": ids,
                                 "meaning": "Review unsuperseded claims; coexistence does not prove a semantic contradiction"})
        return {"status": "UNCONFIGURED" if not claims else "REVIEW_NEEDED" if findings else "NO_STRUCTURAL_FINDINGS", "findings": findings,
                "semantic_validation": False, "limitation": "Local link structure only; external sources not fetched and current truth not inferred"}

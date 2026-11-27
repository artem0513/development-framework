"""Read-only evidence packet construction with explicit source coverage."""
import hashlib
import json
from pathlib import Path
import subprocess
from ..execution.ports import PacketContext
from ..gates import source_snapshot, source_resources, stage_checks
from ..native_paths import logical_path, state_io
from ..runner import verify_receipt
from ..state import read_record, encode, hash_value
from .compact import compact_packet, expand_packet
from ..protocol import PacketTooLarge, lossless_transport
from .representation import BEFORE_PREFIX, WHOLE_DIFF_PATH, policy as review_policy, size_breakdown

# Unchanged paths kept in source_changes.json under an approved compact review; the count is always exact.
UNCHANGED_LIST_BYTES = 32768
# JSON quotes and comma around each listed path (encode() writes no spaces).
UNCHANGED_PATH_OVERHEAD = 3


def _baseline_check_packet(store, task, check, specs, snapshot):
    from ..testing.bugfix import BASELINE_LOG_BYTES, baseline_check_binding, baseline_log, baseline_test_manifest

    history = check["baseline_failures"]
    _, identity = baseline_check_binding(store, task)
    metadata = {key: value for key, value in check.items() if key != "baseline_failures"}
    if identity is None or history.get("identity") != identity:
        return metadata, []
    files, results, run_counts = [], [], {}
    remaining = BASELINE_LOG_BYTES
    for result in history["results"]:
        spec = specs.get(result["id"])
        if spec is None or result.get("check_spec_sha256") != hash_value(spec):
            continue
        row = dict(result)
        results.append(row)
        try:
            if result.get("check_inputs_sha256") != hash_value(snapshot.get("check_inputs")):
                raise ValueError("Approved check inputs changed after the baseline run")
            sources = result.get("executed_test_sources")
            if not sources or result["test_sources_sha256"] != hash_value(baseline_test_manifest(spec, snapshot, sources)):
                raise ValueError("Captured test source coverage changed after the baseline run")
            raw = baseline_log(store, task["task_id"], result, limit=remaining)
            content = raw.decode("utf-8")
        except (OSError, ValueError, KeyError, TypeError) as error:
            row["log_coverage"] = str(error)
            continue
        check_id = result["id"]
        run_counts[check_id] = ordinal = run_counts.get(check_id, 0) + 1
        name = check_id if ordinal == 1 else f"{check_id}/{ordinal}"
        path = "_ase_context/baseline_logs/" + name + ".txt"
        row["log_packet_path"] = path
        files.append({"path": path, "content": content, "sha256": result["log_sha256"]})
        remaining -= len(raw)
    if results:
        metadata["baseline_failures"] = {**history, "results": results}
    return metadata, files


class _BaselineBlobs:
    """Baseline commit bytes for one packet build.

    The task repository is resolved and validated once per build (a failure is kept and
    reported for every path, as a per-file check would), and prefetched paths are read in
    one batch. Nothing outlives the build, so the next build validates the current tree again.
    """

    def __init__(self, root, baseline):
        self.root, self.baseline = root, baseline
        self._repository = None
        self._blobs, self._oversized = {}, set()

    def repository(self):
        """Validated Git directory of the baseline's repository (the workspace root when not nested)."""
        if self._repository is None:
            from ..repositories import repository_path
            try:
                self._repository = (repository_path(self.root, self.baseline.get("repository")), None)
            except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as ex:
                self._repository = (None, ex)
        path, error = self._repository
        if error is not None:
            raise error.with_traceback(None)  # Reported per file; earlier raises do not accumulate frames.
        return path

    def _location(self, path):
        """(Git directory, repository-relative name); keys of a nested clone carry its prefix."""
        commit = self.baseline.get("commit")
        if not isinstance(commit, str) or not commit:
            raise ValueError("the first capture postdates edits and the baseline records no Git commit")
        if not self.baseline.get("repository"):
            return self.root, path
        prefix = self.baseline["repository"] + "/"
        if not path.startswith(prefix):
            raise ValueError("the path lies outside the task repository")
        return self.repository(), path[len(prefix):]

    def prefetch(self, paths):
        """Read these paths in one batch; any failure leaves them to the single read, which reports it.

        Only an optimization: blob() repeats every check, so nothing here may raise or change a result.
        """
        from ..inspection import safe_blobs
        from ..source_coverage import PACKET_FILE_LIMIT
        names, repo = {}, None
        try:
            for path in paths:
                try:
                    repo, name = self._location(path)
                except Exception:  # noqa: BLE001 - blob() reports it for this path
                    continue
                if path not in self._blobs and path not in self._oversized:
                    names[name] = path
            if not names:
                return
            blobs, oversized = safe_blobs(repo, self.baseline["commit"], list(names), PACKET_FILE_LIMIT)
        except Exception:  # noqa: BLE001 - the single reads report any failure
            return
        self._blobs.update({names[name]: data for name, data in blobs.items()})
        self._oversized.update(names[name] for name in oversized)

    def blob(self, path, expected):
        """Committed bytes of path at the baseline commit, only when they hash to the recorded baseline."""
        from ..inspection import safe_blob
        from ..source_coverage import PACKET_FILE_LIMIT
        repo, name = self._location(path)
        if path in self._oversized:
            raise ValueError("the baseline file exceeds the packet file limit")
        # A prefetched body is used once and released, so the build never holds more than the batch.
        data = self._blobs.pop(path) if path in self._blobs else safe_blob(repo, self.baseline["commit"], name)
        if len(data) > PACKET_FILE_LIMIT:
            raise ValueError("the baseline file exceeds the packet file limit")
        # The blob holds Git's stored bytes; a CRLF checkout (core.autocrlf, eol=crlf) differs only by line endings.
        candidates = [data] + ([data.replace(b"\n", b"\r\n")] if b"\r" not in data and b"\n" in data else [])
        data = next((c for c in candidates if hashlib.sha256(c).hexdigest() == expected), None)
        if data is None:
            raise ValueError("the first capture postdates edits and the baseline commit's bytes differ from the recorded baseline hash")
        if len(data) > PACKET_FILE_LIMIT:
            raise ValueError("the baseline file exceeds the packet file limit")
        content = data.decode("utf-8")
        if "\0" in content:
            raise ValueError("source contains binary NUL bytes")
        return {"path": path, "content": content, "sha256": expected}


def _baseline_blob(repo, baseline, path, expected):
    """Committed bytes of path at the baseline commit, only when they hash to the recorded baseline."""
    return _BaselineBlobs(repo, baseline).blob(path, expected)


def _branch_diff(baseline_blobs, baseline, missing):
    """Supply the whole branch patch when the packet uses the default representation."""
    from ..inspection import safe_git_bytes
    from ..source_coverage import PACKET_FILE_LIMIT
    try:
        repo = baseline_blobs.repository()
        diff = safe_git_bytes(repo, "diff", "--no-renames", baseline["commit"])
        header = ("# git diff of repository " + (baseline.get("repository") or ".") + " against "
                  + baseline["base_ref"] + " (" + baseline["commit"] + "); working tree incl. committed branch work\n")
        if len(diff) > PACKET_FILE_LIMIT:
            missing.append("Branch diff against " + baseline["base_ref"] + " exceeds the packet file limit; changed files and their base versions are supplied")
        else:
            content = header + diff.decode("utf-8", "replace")
            return [{"path": WHOLE_DIFF_PATH, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}]
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as ex:
        missing.append("Branch diff against the task base unavailable: " + str(ex))
    return []


def _base_versions(baseline_blobs, changes, files, must_review, body_omission, missing, *, required=None):
    """The base version of each changed reviewed file."""
    added = []
    present = {item["path"] for item in files}
    wanted = []
    for entry in changes["changes"]:
        path, before = entry["path"], entry["before_sha256"]
        if (before is None or before.startswith("link:") or path not in must_review or body_omission(path) is not None
                or BEFORE_PREFIX + path in present):
            continue
        wanted.append((path, before))
    baseline_blobs.prefetch(path for path, _ in wanted)
    for path, before in wanted:
        try:
            blob = baseline_blobs.blob(path, before.removesuffix(":x"))
        except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as ex:
            if required is None or path in required:
                missing.append("Base version unavailable for " + path + ": " + str(ex))
            continue
        added.append({**blob, "path": BEFORE_PREFIX + path})
    return added


def _factor_omission_note(packet):
    """Share one identical optional-body note without changing any policy."""
    if any('encoding' in row for row in packet['files']):
        logical = expand_packet(packet)
        _factor_omission_note(logical)
        packet.clear()
        packet.update(compact_packet(logical))
        return
    entry = next((f for f in packet['files'] if f['path'] == '_ase_context/omitted_sources.json'), None)
    if entry is None:
        return
    rows = json.loads(entry['content'])
    notes = {row.get('limitation') for row in rows}
    if not rows or len(notes) != 1 or None in notes:
        return
    for row in rows:
        del row['limitation']
    entry['content'] = encode(rows)
    entry['sha256'] = hashlib.sha256(entry['content'].encode()).hexdigest()
    packet['coverage']['omitted_source_limitation'] = notes.pop()
    packet['coverage']['scope'] += ' omitted_source_limitation applies exactly to EVERY omitted_sources row.'


def _drop_caller_excerpts(packet):
    """Keep the caller index but drop its excerpts; the reviewer then states the coverage limit."""
    import json
    for row in packet.get('files', []):
        if row.get('path') != '_ase_context/change_impact.json' or not isinstance(row.get('content'), str):
            continue
        impact = json.loads(row['content'])
        for change in impact.get('changes', []):
            dropped = [int(key) for key in change.pop('excerpts', {})]
            if dropped:
                change.setdefault('excerpt_omitted', {})['OMITTED_PACKET_BUDGET'] = sorted(dropped)
        row['content'] = encode(impact)
        row['sha256'] = hashlib.sha256(row['content'].encode()).hexdigest()
        packet.setdefault('coverage', {}).setdefault('missing', []).append(
            'Caller excerpts dropped to fit the packet bound; callers outside the packet are a stated coverage limit')


def _bound_packet(packet, historical_paths=(), *, lossless=False, max_bytes=1048576):
    """Transform logical bodies before measuring; never retain stale list aliases."""
    from ..protocol import canonical, validate_packet
    if len(canonical(packet)) > max_bytes:
        _factor_omission_note(packet)
    if len(canonical(packet)) > max_bytes:
        _drop_caller_excerpts(packet)
    if lossless:
        packet = compact_packet(packet)
    if historical_paths and len(canonical(packet)) > max_bytes:
        packet = expand_packet(packet)
        packet['files'] = [row for row in packet['files'] if row['path'] not in historical_paths]
        packet['coverage']['missing'].append('Historical lead-input packet budget exceeded; exact stored inputs need a bounded review representation')
        packet['coverage']['included'] = [row['path'] for row in packet['files']]
        if lossless:
            packet = compact_packet(packet)
    return validate_packet(packet, max_bytes=max_bytes)


def amend_packet(packet, change, *, max_bytes):
    """Apply `change` to the logical packet and re-encode it as it was, within the same bound."""
    from ..protocol import validate_packet
    encoded = any('encoding' in row for row in packet['files'])
    logical = change(expand_packet(packet) if encoded else packet)
    return validate_packet(compact_packet(logical) if encoded else logical, max_bytes=max_bytes)


class PacketService:
    def __init__(self, context: PacketContext, *, target):
        self.context = context
        self.target = target

    def packet(self, task_id, stage_id=None, part_id=None, role=None):
        context = self.context
        from ..source_coverage import source_availability, availability_gaps, source_bytes, bound_representation, read_persisted_intake, readable_source
        task = context.tasks.snapshot(task_id)
        contract = task["contract"]
        declared = review_policy(contract) or {}
        row = context.store.db.execute("SELECT payload FROM task_discoveries WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        discovery = json.loads(row[0]) if row else None
        intake_state = read_persisted_intake(discovery)
        availability = source_availability(context.store, task["contract"], discovery, stage_id)
        policies = {item["path"]: item for item in availability}
        representations = {item["artifact_path"]: item for item in (discovery or {}).get("representations", [])}
        plan = context.tasks.execution_plan(task_id)
        stage = next((item for item in task["contract"]["stages"] if item["id"] == stage_id), None)
        if stage_id is not None and stage is None:
            raise ValueError("Unknown source packet stage")
        must_review = {Path(item["path"]).as_posix() for item in stage_checks(task["contract"], plan["checks"], stage_id)
                       if item["kind"] == "artifact" and item.get("required", True)}
        artifact_paths = set(must_review)
        from .representation import changed_sizes, parts as review_parts, represent, overflow_advice
        if part_id is not None and part_id not in {part["id"] for part in review_parts(task["contract"])}:
            raise ValueError("Unknown review part: " + str(part_id))

        def source_policy(path):
            matching = [item for resource, item in policies.items() if Path(path).is_relative_to(Path(resource))]
            return max(matching, key=lambda item: len(Path(item["path"]).parts)) if matching else None

        def coverage_required(path):
            if body_omission(path) is not None:
                return False
            policy = source_policy(path)
            return path in must_review or policy is None or (policy["relevant"] and (policy["required"] or policy["role"] in {"output", "workspace", "representation"}))

        def direct_body_omission(path):
            if path in must_review:
                return None
            policy = source_policy(path)
            if policy is not None:
                if not policy["relevant"]:
                    return "OMITTED_IRRELEVANT_STAGE"
                if policy["role"] == "input" and not policy["required"]:
                    return "OMITTED_OPTIONAL_INPUT"
            return None

        def body_omission(path):
            if path in must_review:
                return None
            owners = [artifact for artifact, declaration in representations.items() if declaration["path"] == path]
            if owners:
                policy = source_policy(path)
                if policy is not None and policy["role"] != "representation" and direct_body_omission(path) is None:
                    return None  # Independently required as a source or output.
                decisions = [direct_body_omission(artifact) for artifact in owners]
                if all(decisions):
                    return "OMITTED_OPTIONAL_INPUT" if "OMITTED_OPTIONAL_INPUT" in decisions else "OMITTED_IRRELEVANT_STAGE"
                return None  # At least one included artifact needs this body.
            return direct_body_omission(path)

        snapshot = source_snapshot(context.store, task["contract"], task_id=task_id)
        baseline = read_record(context.store.db, "baseline", task_id)["payload"]
        if baseline is None:
            from ..tasks import matching_approval_baseline
            baseline = matching_approval_baseline(context.store, task_id, contract)
        baseline_blobs = _BaselineBlobs(context.store.repo, baseline) if baseline else None
        required_context = set()
        if discovery and (declared.get("changed_files") == "diff_context" or declared.get("parts")):
            from ..discovery import declared_dependencies
            from ..source_coverage import stage_affected
            observed_paths = snapshot["files"].keys() | (baseline or {}).get("files", {}).keys()
            # Explicit file dependencies are read inputs for the whole relevant stage,
            # even when their changes are assigned to another review part.
            required_context = {item["path"] for item in declared_dependencies(discovery)
                                if item.get("required", True) and item["path"] in observed_paths
                                and stage_affected(contract, item.get("blocking_stage_ids"), stage_id)}
            must_review.update(required_context)
        changes = None
        if baseline:
            paths = set(baseline["files"]) | set(snapshot["files"])
            changed = sorted(p for p in paths if baseline["files"].get(p) != snapshot["files"].get(p))
            if len(changed) > 1000:
                raise ValueError("Source change inventory exceeds the bounded review packet")
            unchanged = sorted(p for p in paths if baseline["files"].get(p) == snapshot["files"].get(p))
            changes = {"baseline_fingerprint": baseline["fingerprint"], "current_fingerprint": snapshot["fingerprint"],
                       "baseline_dirty": baseline.get("dirty"), "coverage": "Actual content/mode hashes from baseline and current source observations; software covers tracked and nonignored untracked files (except untracked __pycache__ bytecode) plus declared dependencies",
                       "changes": [{"path": p, "before_sha256": baseline["files"].get(p), "after_sha256": snapshot["files"].get(p)} for p in changed],
                       "unchanged_paths": unchanged[:1000], "unchanged_path_count": len(unchanged), "unchanged_paths_truncated": len(unchanged) > 1000}
            if declared:
                # Owner-approved compact review: keep the list within a byte budget; current_fingerprint binds the inventory.
                kept, used = [], 0
                for path in changes["unchanged_paths"]:
                    used += len(path.encode()) + UNCHANGED_PATH_OVERHEAD
                    if used > UNCHANGED_LIST_BYTES:
                        break
                    kept.append(path)
                changes["unchanged_paths"], changes["unchanged_paths_truncated"] = kept, len(kept) < len(unchanged)
            review_scope = stage["scope"] if stage is not None else task["contract"]["scope"]
            if set(review_scope["effects"]) & {"file_write", "command"}:
                must_review.update(path for path in changed if any(Path(path).is_relative_to(Path(resource)) for resource in review_scope["resources"]))
        must_review.update(item["path"] for artifact, item in representations.items() if artifact in must_review)
        files = []
        missing = [gap["id"] + ": " + gap["text"] for gap in [*availability_gaps(availability), *intake_state["compatibility_gaps"]]]
        for path, observation in snapshot.get("unavailable", {}).items():
            availability.append({"path": path, **observation, "reason": "Source snapshot could not read this declared resource"})
            if coverage_required(path):
                missing.append(path + ": source snapshot unavailable (" + observation["error"] + ")")
        representation_metadata = []
        for artifact, declaration in representations.items():
            omission = body_omission(artifact)
            if omission:
                artifact_hash = snapshot["files"].get(artifact, "").removesuffix(":x")
                readable_hash = snapshot["files"].get(declaration["path"], "").removesuffix(":x")
                current = artifact_hash == declaration["artifact_sha256"] and readable_hash == declaration["sha256"]
                representation_metadata.append({**declaration, "body_status": omission,
                                                "observed_artifact_sha256": artifact_hash or None, "observed_representation_sha256": readable_hash or None,
                                                "integrity": "CURRENT_SNAPSHOT_HASHES" if current else "STALE_OR_UNAVAILABLE_SNAPSHOT_BINDING",
                                                "limitation": "Artifact/representation bodies were omitted from review; the declared binding and current source hashes remain visible"})
        omitted_sources = []
        selected = set()
        repository_root = snapshot.get("repository")
        for resource in source_resources(context.store, task["contract"], task_id):
            candidate = context.store.repo / resource
            if candidate.resolve().is_relative_to(context.store.repo.resolve()):
                if (repository_root and Path(resource.replace("\\", "/")).as_posix().rstrip("/") == repository_root):
                    # A whole nested repository: review its changed files; the rest stay in unchanged_paths.
                    selected.update(entry["path"] for entry in (changes or {}).get("changes", []) if entry["after_sha256"] is not None)
                elif candidate.is_dir():
                    selected.update(p for p in snapshot["files"] if (context.store.repo / p).is_relative_to(candidate.resolve()))
                else:
                    selected.add(resource.replace("\\", "/"))
        for path in sorted(selected):
            candidate = context.store.repo / path
            try:
                candidate = self.target(context.store.repo.resolve(), path)
                if any(p.lower() == ".git" for p in candidate.resolve().relative_to(context.store.repo.resolve()).parts):
                    raise ValueError("Private Git data is not a source packet")
                if not candidate.exists():
                    if path in required_context and path in snapshot["files"]:
                        missing.append(path + ": required review context became unavailable while building the packet")
                    continue
                omission = body_omission(path)
                if omission:
                    policy = source_policy(path)
                    observed_hash = snapshot["files"].get(path)
                    omitted_sources.append({"path": path, "sha256": observed_hash,
                                            "status": "AVAILABLE" if observed_hash is not None else "UNAVAILABLE",
                                            "body_status": omission, "required": policy["required"], "relevant": policy["relevant"],
                                            "blocking_stage_ids": policy["blocking_stage_ids"], "reason": policy["reason"],
                                            "limitation": "Body was not supplied or reviewed in this packet; current source hashes and declared stage impact remain visible"})
                    continue
                if path in representations:
                    raw = readable_source(context.store.repo, path)
                    represented = bound_representation(context.store.repo, representations[path])
                    if raw is not None and not any(item["path"] == path for item in files):
                        files.append(raw)
                    if not any(item["path"] == represented["path"] for item in files):
                        files.append(represented)
                    representation_metadata.append({**representations[path], "integrity": "Current artifact and readable representation bytes match both declared hashes",
                                                    "limitation": ("Readable raw source is supplied as authority; the representation is supplementary"
                                                        if raw is not None else "Representation adequacy requires independent domain judgment; native binary bytes are not interpreted by this transport")})
                    continue
                data = source_bytes(context.store.repo, path)
                content = data.decode("utf-8")
                if "\0" in content:
                    raise ValueError("source contains binary NUL bytes")
                digest = hashlib.sha256(data).hexdigest()
                if path in required_context and digest != snapshot["files"].get(path, "").removesuffix(":x"):
                    raise ValueError("required review context changed while building the packet")
                if not any(item["path"] == path for item in files):
                    files.append({"path": path, "content": content, "sha256": digest})
            except (UnicodeError, OSError, ValueError) as ex:
                if coverage_required(path):
                    missing.append(path + ": " + str(ex))
                else:
                    availability.append({"path": path, "status": "UNCOVERED", "required": False,
                                         "limitation": str(ex), "reason": "Optional or outside the current stage's source requirement"})
        historical = read_record(context.store.db, "baseline_implementation", task_id)["payload"]
        if historical and changes:
            try:
                if not Path(historical["artifact_root"]).resolve().is_relative_to(context.store.root.resolve()):
                    raise ValueError("Historical source capture escapes project state")
                verify_receipt(historical, require_current_policy=False)
                original = json.loads(Path(historical["artifacts"]["request"]["path"]).read_bytes())
                before_hashes = {entry["path"]: entry["before_sha256"] for entry in changes["changes"]}
                captured = expand_packet(original["source_packet"])["files"]

                def expected_hash(path):
                    return before_hashes[path].removesuffix(":x").rsplit(":", 1)[-1]
                # One batch for the edited files the loop below reads; the loop keeps every check.
                baseline_blobs.prefetch(
                    row["path"] for row in captured
                    if isinstance(row, dict) and isinstance(row.get("path"), str) and before_hashes.get(row["path"]) is not None
                    and body_omission(row["path"]) is None and row.get("sha256") != expected_hash(row["path"]))
                for before in captured:
                    path = before["path"]
                    if path not in before_hashes or body_omission(path) is not None:
                        continue
                    if before_hashes[path] is None:
                        continue  # Absent at baseline: a later capture is not its before state.
                    expected = expected_hash(path)
                    if before["sha256"] != expected:
                        # Edited after the baseline but before the first capture (e.g. a native fix
                        # after approval): only the baseline commit's verified bytes are the before state.
                        try:
                            before = baseline_blobs.blob(path, expected)
                        except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as ex:
                            missing.append("Baseline source unavailable for " + path + ": " + str(ex))
                            continue
                    files.append({**before, "path": BEFORE_PREFIX + path})
            except (ValueError, OSError, KeyError, TypeError) as ex:
                missing.append("Historical source capture unavailable: " + str(ex))
        if baseline and changes:
            if baseline.get("base_ref") and declared.get("changed_files") != "diff_context" and not declared.get("parts"):
                files.extend(_branch_diff(baseline_blobs, baseline, missing))
            if baseline.get("commit"):
                # An exact committed blob can recover the before bytes for a
                # direct review even when no implementation session ran.
                required_before = None if baseline.get("base_ref") else required_context
                files.extend(_base_versions(baseline_blobs, changes, files, must_review, body_omission, missing,
                                            required=required_before))
        stage_history, history_missing = context._stage_history(task)
        missing.extend(history_missing)
        from ..action_evidence import action_packet_evidence
        action_files, action_missing = action_packet_evidence(context.store, task, plan, snapshot, stage_id=stage_id)
        files.extend(action_files)
        missing.extend(action_missing)
        if task["contract"].get("check_inputs"):
            from .check_inputs import approval_observations, packet_files
            observed = snapshot.get("check_inputs", {})
            approved = approval_observations(context.store.db, task, plan, observed, stage_id)
            input_files, input_missing = packet_files(context.store.repo, task["contract"], observed, approved)
            files.extend(input_files)
            missing.extend(input_missing)
        from ..delegations import DelegationRegistry
        from ..testing.adequacy import TestAdequacy
        test_adequacy_plan = TestAdequacy(context.store).snapshot(task_id)
        test_adequacy_assessment = (
            {'status': 'WITHHELD_FOR_INDEPENDENCE',
             'reason': 'Prior assessment prose is not input to a fresh opted-in session. Inspect original requirements, scenario plan, actual source and executed logs; the gate separately requires current independent assessment receipts.'}
            if lossless_transport(task['contract'])
            else TestAdequacy(context.store).assessment(task_id,stage_id))
        try:
            delegation_evidence = DelegationRegistry(context.store).packet_evidence(task_id, stage_id=stage_id)
        except (ValueError, OSError, KeyError, TypeError) as ex:
            from ..core import redact
            reason = 'Native delegation evidence unavailable: ' + redact(ex)
            missing.append(reason)
            delegation_evidence = {'status': 'UNAVAILABLE', 'reason': reason}
        check = read_record(context.store.db, "checks", task_id)["payload"]
        check_metadata = check
        if check and check.get("baseline_failures"):
            specs = {spec["id"]: spec for spec in stage_checks(contract, plan["checks"], stage_id)}
            check_metadata, baseline_logs = _baseline_check_packet(context.store, task, check, specs, snapshot)
            files.extend(baseline_logs)
        for name, data in (("contract", task["contract"]), ("execution_plan", plan), ("source_changes", changes), ("stage_history", stage_history),
                           ("discovery", discovery), ("intake_validation", intake_state["validation"]), ("source_availability", availability), ("artifact_representations", representation_metadata), ("omitted_sources", omitted_sources),
                           ('delegations', delegation_evidence), ('test_adequacy_plan', test_adequacy_plan),
                           ('test_adequacy_assessment', test_adequacy_assessment),
                           ("actual_checks", check_metadata)):
            content = encode(data)
            files.append({"path": "_ase_context/" + name + ".json", "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()})
        for result in (check or {}).get("results", []):
            if result.get("log"):
                try:
                    log = logical_path(result["log"])
                    if not log.is_relative_to(context.store.root.resolve()):
                        raise ValueError("Log is outside project state")
                    physical = state_io(log)
                    if not physical.is_file():
                        raise ValueError("Log is unavailable")
                    raw = physical.read_bytes()
                    # The record names the exact run; a replaced log is not that run's evidence.
                    if result.get("log_sha256") is not None and hashlib.sha256(raw).hexdigest() != result["log_sha256"]:
                        raise ValueError("Log differs from its recorded hash")
                    content = raw.decode("utf-8", errors="replace")
                except (OSError, ValueError):
                    missing.append("Actual check log unavailable: " + result["id"])
                    continue
                files.append({"path": "_ase_context/logs/" + result["id"] + ".txt", "content": content,
                              "sha256": hashlib.sha256(content.encode()).hexdigest()})
        historical_paths = set()
        from ..lead_review import LeadReview
        lead_service = LeadReview(context.store)
        if stage_id is None and lead_service.configured(task_id):
            try:
                lead = lead_service.assessment(task_id, current_files=files)
                historical_files = lead.pop('input_files')
                missing.extend(lead.pop('input_coverage_missing'))
                contract_file = next(item for item in files if item['path'] == '_ase_context/contract.json')
                scope = task['contract']['scope']
                scope_ref = {'$ref': contract_file['path'], 'sha256': contract_file['sha256'],
                    'pointer': '/scope', 'value_sha256': hashlib.sha256(encode(scope).encode()).hexdigest()}
                reference_note = ('Whole-contract $ref resolves the complete same-packet JSON body; '
                    'a pointer /scope resolves only its scope. Remove exclude_resources from its resources in original order '
                    'when present, then verify file SHA256 and canonical value_sha256, '
                    'restore referenced values before original approval/intent hashes. Differing fields, stored '
                    'intent and prepare are unchanged; no historical contract is replaced by a different current one.')
                for intent in lead['requirements']['approved_intents']:
                    if encode(intent['contract']) == contract_file['content']:
                        # The complete identical body is already in this same
                        # bounded packet. Preserve old differing contracts and
                        # every immutable intent/approval hash, not a summary.
                        intent['contract'] = {'$ref': contract_file['path'], 'sha256': contract_file['sha256']}
                        lead['contract_reference'] = reference_note
                    else:
                        # A differing old contract stays distinct. Only exact,
                        # repeated large scope values share the current body.
                        for owner in [intent['contract'], *intent['contract']['stages']]:
                            value = owner.get('scope')
                            if not isinstance(value, dict):
                                continue
                            excluded = [path for path in scope['resources'] if path not in value.get('resources', [])]
                            restored = {**scope, 'resources': [path for path in scope['resources'] if path not in excluded]}
                            reference = {**scope_ref, 'value_sha256': hashlib.sha256(encode(value).encode()).hexdigest()}
                            if excluded:
                                reference['exclude_resources'] = excluded
                            if encode(value) == encode(restored) and len(encode(reference)) < len(encode(value)):
                                owner['scope'] = reference
                                lead['contract_reference'] = reference_note
                content = encode(lead)
                files.append({'path': '_ase_context/lead_review.json', 'content': content,
                    'sha256': hashlib.sha256(content.encode()).hexdigest()})
                files.extend(historical_files)
                historical_paths.update(item['path'] for item in historical_files)
            except (ValueError, OSError, KeyError, TypeError) as error:
                missing.append('Original-lead assessment evidence unavailable: ' + str(error))
        changed_paths = [entry["path"] for entry in (changes or {}).get("changes", [])]
        # Sizes before the representation drops bodies: a split proposal must see what each file would cost.
        sizes = changed_sizes(files, changed_paths)
        representation = represent(contract, files, changes, artifact_paths, missing, part_id,
                                   required_context=required_context)
        if changed_paths:
            # Built after the representation, so "in the packet" means a body this session really has.
            from .impact import change_impact
            index_paths = excerpt_paths = None
            if representation is not None:
                listed = {row["path"]: row["representation"] for row in representation["files"]
                          if part_id is None or not row.get("context_only")}
                # Another part's files are that part's index; files reviewed in parts keep a paths-only index here.
                index_paths = set(listed)
                # Only changes another session reviews lose excerpts; a change this session cannot see keeps them.
                excerpt_paths = {path for path, kind in listed.items() if kind != "REVIEWED_IN_PART"}
            impact = change_impact(context.store.repo, snapshot["files"], set(changed_paths),
                                   {item["path"] for item in files if not item["path"].startswith("_ase_")},
                                   snapshot_key=snapshot["fingerprint"], omitted=lambda path: body_omission(path) is not None,
                                   index_paths=index_paths, excerpt_paths=excerpt_paths)
            if impact:
                content = encode(impact)
                files.append({"path": "_ase_context/change_impact.json", "content": content,
                              "sha256": hashlib.sha256(content.encode()).hexdigest()})
        packet = {"requirements": [task["contract"]["request"], *[ac["id"] + ": " + ac["expected"] for ac in task["contract"]["acceptance"]]],
                "files": files, "coverage": {"scope": "Current stage source plus task contract and actual check logs; known irrelevant/optional bodies are explicitly disclosed in omitted_sources; undeclared dependencies are not proven complete",
                                               "included": [f["path"] for f in files], "missing": missing,
                                               "omitted": [item["path"] for item in omitted_sources]}}
        if representation is not None:
            packet["coverage"]["changed_file_mode"] = representation["mode"]
            if part_id is not None:
                packet["coverage"]["review_part"] = part_id
            packet["coverage"]["scope"] += (" Changed files use the owner-approved " + representation["mode"]
                                            + " representation; _ase_context/changed_files.json states per file what was supplied.")
        from ..model_limits import packet_budget
        limit = packet_budget(task['contract'], context.store.db, role)
        # Measured before bounding, which may shrink context files in place.
        breakdown = size_breakdown(files, changed_paths, limit) if changed_paths else ""
        try:
            return _bound_packet(packet, historical_paths, lossless=lossless_transport(task['contract']), max_bytes=limit)
        except PacketTooLarge as error:
            context_note = ""
            if required_context:
                paths = ", ".join(sorted(required_context)[:5])
                context_note = " Required whole-file review context (" + str(len(required_context)) + "): " + paths + "."
            if not changed_paths:
                if context_note:
                    raise PacketTooLarge(str(error) + context_note) from None
                raise
            from ..protocol import canonical
            advice = overflow_advice(contract, files, sizes, limit, len(canonical(packet)),
                                     required_context=required_context)
            raise PacketTooLarge(str(error) + " (" + str(len(changed_paths)) + " changed files). " + advice + " "
                                 + breakdown + context_note) from None

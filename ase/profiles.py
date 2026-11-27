"""Deterministic, domain-specific evaluation of recorded evidence."""
import hashlib
import json
from pathlib import Path, PureWindowsPath

from .state import identifier, text
from .native_paths import state_io


COMMAND_KINDS = {"command", "test", "build", "lint", "security", "smoke", "probe"}
EVIDENCE_KINDS = COMMAND_KINDS | {"artifact", "manual"}
DOMAIN_PROFILES = {"software", "infrastructure", "document", "research"}
# The runtime grants file_write and command; read_only explicitly declares neither.
SCOPE_EFFECTS = ("command", "file_write", "read_only")


def validate_scope_effects(effects):
    unknown = [effect for effect in effects if effect not in SCOPE_EFFECTS]
    if unknown:
        raise ValueError("Unknown scope effect " + ", ".join(map(repr, unknown)) + "; allowed: "
                         + ", ".join(SCOPE_EFFECTS) + " (an empty list also means read-only)")
    if "read_only" in effects and len(set(effects)) > 1:
        raise ValueError("Scope effect read_only cannot be combined with " + ", ".join(sorted(set(effects) - {"read_only"})))
    return effects


def validate_access(value):
    from .credentials import normalize_target, validate_access_ref
    if not isinstance(value, dict) or set(value) != {"credential_ref", "target"}:
        raise ValueError("Access requires a credential reference and exact target")
    validate_access_ref(value["credential_ref"])
    if normalize_target(value["target"]) != value["target"]:
        raise ValueError("Access target must be a canonical origin")
    return dict(value)


def stage_profile(contract, stage_id):
    stage = next((item for item in contract.get("stages", []) if item["id"] == stage_id), None)
    if stage is None:
        raise ValueError("Unknown domain stage")
    profile = stage.get("profile", contract.get("profile"))
    if profile not in DOMAIN_PROFILES or (contract.get("profile") != "mixed" and profile != contract.get("profile")):
        raise ValueError("MIXED_PROFILE_UNMAPPED: stage requires its concrete domain profile")
    return profile


def has_software(contract):
    return contract.get("profile") == "software" or (contract.get("profile") == "mixed"
        and any(stage.get("profile") == "software" for stage in contract.get("stages", [])))


def validate_mixed_mapping(contract):
    """Keep explicit AC/evidence/domain maps in the reviewed contract, not inference."""
    if contract.get("profile") != "mixed":
        return
    stages = contract.get("stages", [])
    baseline = contract.get("evidence", [])
    known = {spec["id"] for spec in baseline}
    acceptance = {ac["id"]: ac for ac in contract.get("acceptance", [])}
    covered_ac, covered_evidence = set(), set()
    if not stages:
        raise ValueError("MIXED_PROFILE_UNMAPPED: explicit concrete stages are required")
    for stage in stages:
        stage_profile(contract, stage["id"])
        evidence_ids = stage.get("evidence_ids")
        if (not isinstance(evidence_ids, list) or not evidence_ids or not all(isinstance(item, str) for item in evidence_ids)
                or len(evidence_ids) != len(set(evidence_ids))
                or not set(evidence_ids).issubset(known)):
            raise ValueError("MIXED_PROFILE_UNMAPPED: each stage requires known explicit evidence_ids")
        selected_ac = stage.get("acceptance_ids", [])
        if not isinstance(selected_ac, list) or not set(selected_ac).issubset(acceptance):
            raise ValueError("MIXED_PROFILE_UNMAPPED: stage acceptance mapping is invalid")
        for ac_id in selected_ac:
            ac = acceptance[ac_id]
            if not set(ac.get("evidence_ids", ac.get("commands", []))).issubset(evidence_ids):
                raise ValueError("MIXED_PROFILE_UNMAPPED: stage evidence omits a parent acceptance obligation")
        covered_ac.update(selected_ac)
        covered_evidence.update(evidence_ids)
    if any(ac.get("mandatory", True) and ac["id"] not in covered_ac for ac in acceptance.values()):
        raise ValueError("MIXED_PROFILE_UNMAPPED: mandatory parent acceptance requires a domain stage")
    if any(spec.get("required", True) and spec["id"] not in covered_evidence for spec in baseline):
        raise ValueError("MIXED_PROFILE_UNMAPPED: required evidence requires a domain stage")


def stage_evidence_specs(contract, checks, stage_id=None):
    if stage_id is None:
        return checks
    stage = next((s for s in contract["stages"] if s["id"] == stage_id), None)
    if stage is None:
        raise ValueError("Unknown check stage")
    if contract.get("profile") == "mixed":
        validate_mixed_mapping(contract)
        baseline_ids = {s["id"] for s in contract["evidence"]}
        return [s for s in checks if s["id"] in stage["evidence_ids"] or s["id"] not in baseline_ids]
    evidence_ids = {e for a in contract["acceptance"] if a["id"] in stage["acceptance_ids"] for e in a["evidence_ids"]}
    all_linked = {e for a in contract["acceptance"] for e in a["evidence_ids"]}
    # A mandatory final test does not grant its deferred owning stage's command
    # authority to another stage. Unlinked checks are explicitly task-wide;
    # shared linked checks name each criterion that relies on them.
    return [s for s in checks if s["id"] in evidence_ids or s["id"] not in all_linked]


def validate_evidence_spec(spec, profile):
    if not isinstance(spec, dict):
        raise ValueError("Evidence must be an object")
    identifier(spec.get("id"), "evidence id")
    if spec.get("kind") not in EVIDENCE_KINDS:
        raise ValueError("Unknown evidence kind: " + str(spec.get("kind")))
    if type(spec.get("required", True)) is not bool:
        raise ValueError("Evidence required must be boolean")
    if "progress_test_sources" in spec:
        paths = spec["progress_test_sources"]
        if (spec["kind"] != "test" or not isinstance(paths, list) or not paths or len(paths) > 1000
                or any(not isinstance(value, str) for value in paths) or len(set(paths)) != len(paths)):
            raise ValueError("Case progress requires an explicit nonempty test-source boundary")
        from .project_local import is_private_path
        for value in paths:
            text(value, "test source path", limit=2000)
            normalized = value.replace("\\", "/")
            if (normalized.startswith("/") or Path(normalized).is_absolute() or PureWindowsPath(value).drive
                    or ":" in normalized.split("/", 1)[0] or ".." in Path(normalized).parts
                    or any(part.lower() == ".git" for part in Path(normalized).parts)
                    or is_private_path(normalized)):
                raise ValueError("Progress test sources must remain in public project source")
    evaluator = spec.get("evaluator", spec["kind"])
    if evaluator not in EVIDENCE_KINDS | {"hash", "structured_probe", "command_receipt", "manual_attestation"}:
        raise ValueError("Unknown evidence evaluator: " + str(evaluator))
    if spec["kind"] in COMMAND_KINDS:
        if "access" in spec:
            validate_access(spec["access"])
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and "\0" not in x for x in argv):
            raise ValueError("Evidence commands require argv arrays")
        if spec.get("parser") not in {None, "unittest", "pytest", "dotnet", "node"}:
            raise ValueError("Unsupported test parser")
        timeout = spec.get("timeout", 300)
        if type(timeout) not in {int, float} or not 0 < timeout <= 3600:
            raise ValueError("Invalid command timeout")
    if spec["kind"] == "artifact":
        text(spec.get("path"), "artifact path", limit=2000)
    if "access" in spec and spec["kind"] not in COMMAND_KINDS:
        raise ValueError("Only executable evidence can use service access")
    if spec["kind"] == "probe":
        predicate = spec.get("predicate")
        if not isinstance(predicate, dict) or predicate.get("op") not in {"eq", "in", "gte", "lte"} or not isinstance(predicate.get("path"), str):
            raise ValueError("Probe requires an explicit supported predicate")
    return dict(spec)


def _predicate(value, predicate):
    for part in predicate["path"].split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    expected, op = predicate.get("value"), predicate["op"]
    if op == "eq":
        return type(value) is type(expected) and value == expected
    if op == "in":
        return isinstance(expected, list) and any(type(value) is type(item) and value == item for item in expected)
    if type(value) not in {int, float} or type(expected) not in {int, float}:
        return False
    return value >= expected if op == "gte" else value <= expected


def _evaluate(spec, receipt):
    if receipt is None:
        return {"status": "BLOCKED", "reason": "Evidence missing: " + spec["id"]}
    kind = spec["kind"]
    try:
        if kind == "artifact":
            path = Path(receipt["path"])
            if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get("sha256"):
                return {"status": "FAIL", "reason": "Artifact missing or altered: " + spec["id"]}
            return {"status": "PASS", "sha256": receipt["sha256"]}
        if kind == "manual":
            if receipt.get("confirmed") is not True or not receipt.get("user_message_ref"):
                return {"status": "BLOCKED", "reason": "Manual evidence has no recording command, so it cannot pass: " + spec["id"]
                        + "; use an artifact, test, command or probe evidence kind instead"}
            text(receipt["user_message_ref"], "manual confirmation source")
            return {"status": "PASS", "method": "explicit manual attestation"}
        log = Path(receipt["log"])
        physical = state_io(log)
        if (not physical.is_file() or hashlib.sha256(physical.read_bytes()).hexdigest() != receipt.get("log_sha256")):
            return {"status": "FAIL", "reason": "Source log missing or altered: " + spec["id"]}
        if receipt.get("configured_argv") != spec["argv"] or not receipt.get("started") or not receipt.get("ended") or not receipt.get("cwd"):
            return {"status": "FAIL", "reason": "Command receipt differs from plan: " + spec["id"]}
        if receipt.get("error") or receipt.get("exit_code") is None:
            return {"status": "BLOCKED", "reason": "Required command failed to start: " + spec["id"]}
        if receipt.get("exit_code") != 0 or receipt.get("timed_out"):
            return {"status": "FAIL", "reason": "Command failed: " + spec["id"]}
        output = physical.read_text(encoding="utf-8", errors="replace")
        if kind == "test":
            from .core import test_counts
            counts = test_counts(output, spec.get("parser"))
            if counts["skipped"] or counts["failed"]:
                return {"status": "FAIL", "reason": "Required tests skipped or failed: " + spec["id"], "counts": counts}
            if counts["total"] <= 0:
                return {"status": "BLOCKED", "reason": "No executed test count proven: " + spec["id"], "counts": counts}
            return {"status": "PASS", "counts": counts}
        if kind == "probe":
            try:
                value = json.loads(output)
            except ValueError:
                return {"status": "BLOCKED", "reason": "Probe did not produce valid JSON: " + spec["id"]}
            if not _predicate(value, spec["predicate"]):
                return {"status": "FAIL", "reason": "Postcondition is false: " + spec["id"], "observation": value}
            return {"status": "PASS", "observation": value}
        return {"status": "PASS"}
    except (OSError, KeyError, TypeError, ValueError) as ex:
        return {"status": "BLOCKED", "reason": "Invalid evidence " + spec["id"] + ": " + str(ex)}


def evaluate_profile(profile, specs, observations):
    if profile == "mixed":
        return {"status": "BLOCKED", "blocked_reasons": ["MIXED_PROFILE_UNMAPPED: evaluate explicit concrete stage domains"],
                "rejected_reasons": [], "by_id": {}}
    if profile not in {"software", "infrastructure", "document", "research", "mixed"}:
        return {"status": "BLOCKED", "blocked_reasons": ["Unknown profile"], "rejected_reasons": [], "by_id": {}}
    rejected, blocked, by_id = [], [], {}
    seen = [item.get("id") for item in observations]
    if len(seen) != len(set(seen)):
        rejected.append("Duplicate evidence receipts")
    if not specs:
        blocked.append("No evidence specifications configured")
    required_tests, required_probe = False, False
    for raw in specs:
        try:
            spec = validate_evidence_spec(raw, profile)
        except (ValueError, TypeError, KeyError) as ex:
            blocked.append(str(ex))
            continue
        matches = [r for r in observations if r.get("id") == spec["id"]]
        result = _evaluate(spec, matches[0] if len(matches) == 1 else None)
        by_id[spec["id"]] = result
        if spec.get("required", True):
            required_tests |= spec["kind"] == "test"
            required_probe |= spec["kind"] == "probe"
            if result["status"] == "FAIL":
                rejected.append(result["reason"])
            elif result["status"] != "PASS":
                blocked.append(result["reason"])
    if profile == "software" and not required_tests:
        blocked.append("No required executable tests configured")
    if profile == "infrastructure" and not required_probe:
        blocked.append("No required infrastructure postcondition probe configured")
    return {"status": "REJECTED" if rejected else "BLOCKED" if blocked else "PASS",
            "rejected_reasons": rejected, "blocked_reasons": blocked, "by_id": by_id}


def evaluate_contract_profiles(contract, checks, observations, *, stage_id=None):
    if contract.get("profile") != "mixed":
        return evaluate_profile(contract.get("profile"), checks, observations)
    try:
        validate_mixed_mapping(contract)
        if stage_id is not None and not any(s["id"] == stage_id for s in contract["stages"]):
            raise ValueError("Unknown domain stage")
    except (ValueError, TypeError, KeyError) as error:
        return {"status": "BLOCKED", "blocked_reasons": [str(error)], "rejected_reasons": [], "by_id": {}, "stage_profiles": {}}
    rejected, blocked, by_id, domains = [], [], {}, {}
    for stage in contract["stages"]:
        if stage_id is not None and stage["id"] != stage_id:
            continue
        specs = stage_evidence_specs(contract, checks, stage["id"])
        selected = {s["id"] for s in specs}
        domain = stage_profile(contract, stage["id"])
        result = evaluate_profile(domain, specs, [r for r in observations if r.get("id") in selected])
        domains[stage["id"]] = {"profile": domain, **result}
        by_id.update(result["by_id"])
        rejected.extend(stage["id"] + ": " + reason for reason in result["rejected_reasons"])
        blocked.extend(stage["id"] + ": " + reason for reason in result["blocked_reasons"])
    return {"status": "REJECTED" if rejected else "BLOCKED" if blocked else "PASS",
            "rejected_reasons": rejected, "blocked_reasons": blocked, "by_id": by_id, "stage_profiles": domains}

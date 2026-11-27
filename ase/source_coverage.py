"""Declared input availability and readable, byte-bound artifact coverage.

These observations establish local transport/integrity only. The independent
domain reviewer must judge whether a representation is substantively adequate.
"""
import codecs
import hashlib
from pathlib import Path, PureWindowsPath


PACKET_FILE_LIMIT = 512000


def source_path(root, relative):
    from .project_local import is_private_path
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or PureWindowsPath(relative).drive or Path(relative).is_absolute()
            or ".." in Path(relative).parts or is_private_path(relative)
            or any(part.lower() == ".git" for part in Path(relative).parts)):
        raise ValueError("Unsafe source path")
    root = Path(root).resolve()
    candidate = root / relative
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or is_private_path(resolved.relative_to(root)):
        raise ValueError("Source escapes project or names private state")
    for part in (candidate, *candidate.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Redirected source path")
    if candidate.is_file() and candidate.stat().st_nlink > 1:
        raise ValueError("Hardlinked source is unavailable")
    return candidate


def source_bytes(root, relative):
    path = source_path(root, relative)
    if path.stat().st_size > PACKET_FILE_LIMIT:
        raise ValueError("source exceeds per-file packet bound")
    with path.open("rb") as stream:
        data = stream.read(PACKET_FILE_LIMIT + 1)
    if len(data) > PACKET_FILE_LIMIT:
        raise ValueError("source exceeds per-file packet bound")
    return data


def readable_source(root, relative):
    """Return complete bounded raw UTF-8 authority, or None for native bytes.

    Classify with bounded memory even when native artifacts exceed packet size.
    A readable oversized source is a coverage gap, never silently replaced by a
    smaller summary. I/O/path failures also remain gaps rather than fallback.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    data, size = bytearray(), 0
    with source_path(root, relative).open("rb") as stream:
        while chunk := stream.read(65536):
            if b"\0" in chunk:
                return None
            try:
                decoder.decode(chunk)
            except UnicodeError:
                return None
            size += len(chunk)
            if size <= PACKET_FILE_LIMIT:
                data.extend(chunk)
    try:
        decoder.decode(b"", final=True)
    except UnicodeError:
        return None
    if size > PACKET_FILE_LIMIT:
        raise ValueError("readable raw source exceeds per-file packet bound")
    raw = bytes(data)
    return {"path": relative, "content": raw.decode("utf-8"), "sha256": hashlib.sha256(raw).hexdigest()}


def source_hash(root, relative):
    digest = hashlib.sha256()
    with source_path(root, relative).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def bound_representation(root, declaration):
    """Check both current byte streams, returning bounded UTF-8 review content."""
    if source_hash(root, declaration["artifact_path"]) != declaration["artifact_sha256"]:
        raise ValueError("Artifact representation is stale: artifact hash differs")
    data = source_bytes(root, declaration["path"])
    if hashlib.sha256(data).hexdigest() != declaration["sha256"]:
        raise ValueError("Artifact representation is stale: representation hash differs")
    content = data.decode("utf-8")
    if not content.strip() or "\0" in content:
        raise ValueError("Artifact representation must be nonempty readable UTF-8")
    return {"path": declaration["path"], "content": content, "sha256": declaration["sha256"]}


def stage_affected(contract, stage_ids, stage_id):
    if stage_id is None or stage_ids is None:
        return True
    stages = contract["stages"]
    known = {stage["id"] for stage in stages}
    if not stage_ids or not set(stage_ids).issubset(known):
        return True  # Saved stale/ambiguous mappings cannot conceal a gap.
    affected = set(stage_ids)
    while True:
        expanded = affected | {stage["id"] for stage in stages if set(stage.get("depends_on", [])) & affected}
        if expanded == affected:
            return stage_id in affected
        affected = expanded


def dependency_policy(declarations):
    required = [item for item in declarations if item.get("required", True)]
    relevant = required or declarations
    mappings = [item.get("blocking_stage_ids") for item in relevant]
    return {"required": bool(required),
            "blocking_stage_ids": None if any(mapping is None for mapping in mappings)
            else sorted({stage for mapping in mappings for stage in mapping})}


def source_availability(store, contract, discovery, stage_id=None):
    """Observe declared inputs; output absence does not imply a missing input.

    Artifact evidence identifies future outputs, and approved mutation scopes
    identify mutable workspace state. An explicit dependency always supplies
    its own input requirement, including on a mutable target or output.
    """
    discovery = discovery or {}
    scope = discovery.get("scope", {})
    outputs = set(scope.get("outputs", [])) | {item["path"] for item in contract.get("evidence", []) if item["kind"] == "artifact"}
    mutable = {path for stage in contract["stages"]
               for path in stage.get("scope", contract["scope"])["resources"]
               if set(stage.get("scope", contract["scope"])["effects"]) & {"file_write", "command"}}
    policies = {}
    entries = {item["path"]: item for item in discovery.get("resources", [])}
    for path in dict.fromkeys([*scope.get("resources", []), *entries]):
        role = ("output" if path in outputs else "workspace" if any(Path(path).is_relative_to(Path(target)) for target in mutable) else "input")
        policies[path] = {"path": path, "role": role, "required": role == "input",
                          "reason": "Declared discovery resource", "blocking_stage_ids": None}
    # Include both lists: older or partial manifests must not drop declarations.
    dependencies = {}
    for item in [*scope.get("dependencies", []), *discovery.get("dependencies", [])]:
        dependencies.setdefault(item["path"], []).append(item)
    for path, declarations in dependencies.items():
        policies[path] = {"path": path, "role": "input", **dependency_policy(declarations),
                          "reason": "; ".join(dict.fromkeys(item["reason"] for item in declarations))}
    for declaration in discovery.get("representations", []):
        original = policies.get(declaration["artifact_path"], {})
        policies.setdefault(declaration["path"], {"path": declaration["path"], "role": "representation",
                            "required": original.get("required", False), "reason": "Readable artifact representation",
                            "blocking_stage_ids": original.get("blocking_stage_ids")})
    result = []
    for path, policy in sorted(policies.items()):
        item = dict(policy)
        item["relevant"] = stage_affected(contract, item["blocking_stage_ids"], stage_id)
        item["discovered_status"] = entries.get(path, {}).get("status", "UNOBSERVED")
        try:
            candidate = source_path(store.repo, path)
            if not candidate.exists():
                item.update(status="MISSING", limitation="Declared source is absent")
            elif candidate.is_file():
                with candidate.open("rb") as stream:
                    stream.read(1)
                item["status"] = "AVAILABLE"
            elif candidate.is_dir():
                next(candidate.iterdir(), None)
                item["status"] = "AVAILABLE"
            else:
                item.update(status="UNAVAILABLE", limitation="Unsupported source type")
        except (OSError, ValueError, RuntimeError) as ex:
            item.update(status="UNAVAILABLE", limitation=str(ex))
        result.append(item)
    return result


def availability_gaps(availability):
    return [{"id": "source:" + item["path"], "text": item["reason"] + ": " + item["limitation"],
             "source_refs": ["discovery:" + item["path"]], "required": True,
             **({"blocking_stage_ids": item["blocking_stage_ids"]} if item["blocking_stage_ids"] is not None else {})}
            for item in availability if item["required"] and item["relevant"] and item["status"] != "AVAILABLE"]


def declared_source_resources(contract, discovery):
    resources = list(contract["scope"]["resources"])
    if discovery is not None:
        resources.extend(item["path"] for item in discovery["resources"])
        resources.extend(discovery["scope"]["resources"])
        resources.extend(item["path"] for item in [*discovery["scope"].get("dependencies", []), *discovery["dependencies"]])
        resources.extend(item["path"] for item in discovery.get("representations", []))
    return list(dict.fromkeys(resources))


def normalized_discovery_policy(discovery):
    """Coverage declarations, excluding generated capability/read observations.

    Acquisition observed_at is not new source evidence in record_discovery;
    source references, meeting cues/ranges, statuses and claims remain bound.
    """
    import copy
    from .state import encode

    def path(value):
        return Path(value.replace("\\", "/")).as_posix()

    def rows(values):
        unique = {encode(value): value for value in values}
        return [unique[key] for key in sorted(unique)]

    scope = discovery["scope"]
    dependencies = []
    for item in [*scope.get("dependencies", []), *discovery["dependencies"]]:
        dependencies.append({"path": path(item["path"]), "from": path(item["from"]) if "from" in item else None,
                             "reason": item["reason"], "required": item.get("required", True),
                             "blocking_stage_ids": sorted(set(item["blocking_stage_ids"])) if "blocking_stage_ids" in item else None})
    representations = [{**item, "artifact_path": path(item["artifact_path"]), "path": path(item["path"])}
                       for item in discovery.get("representations", [])]
    intake_state = read_persisted_intake(discovery)
    intake = copy.deepcopy(intake_state["intake"])
    for source in intake.get("source_refs", []):
        if isinstance(source, dict):
            source.pop("observed_at", None)
    for key, items in list(intake.items()):
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("source_refs", "blocking_stage_ids"):
                if isinstance(item.get(field), list):
                    item[field] = sorted(set(item[field]))
            if key in {"gaps", "contradictions"}:
                item.setdefault("required", True)
                item["status"] = item.get("status", "UNRESOLVED").upper()
        intake[key] = rows(items)
    return {"schema_version": 1,
            "scope": {"resources": sorted({path(item) for item in scope["resources"]}),
                      "outputs": sorted({path(item) for item in scope.get("outputs", [])})},
            "source_paths": sorted({path(item["path"]) for item in discovery["resources"]}),
            "dependencies": rows(dependencies), "representations": rows(representations),
            "intake": intake, "intake_validation": {key: intake_state["validation"][key]
                for key in ("status", "reason") if key in intake_state["validation"]}}


def read_persisted_intake(discovery):
    """Revalidate saved intake without rewriting its historical attribution."""
    import copy
    from .discovery import validate_intake
    if not discovery or "intake" not in discovery:
        return {"intake": {}, "compatibility_gaps": [], "validation": {"status": "UNCONFIGURED", "source_statuses": []}}
    raw = copy.deepcopy(discovery["intake"])
    sources = raw.get("source_refs", []) if isinstance(raw, dict) else []
    sources = sources if isinstance(sources, list) else []
    for source in sources:
        if isinstance(source, dict) and isinstance(source.get("status"), str):
            source["status"] = source["status"].strip().lower()
    statuses = [{"id": source.get("id"), "status": source["status"]} for source in sources
                if isinstance(source, dict) and "status" in source]
    try:
        intake = validate_intake(raw)
        return {"intake": intake, "compatibility_gaps": [], "validation": {"status": "VALID", "source_statuses": statuses}}
    except (ValueError, TypeError, KeyError) as ex:
        reason = str(ex)
    # Old APIs validated the other intake fields. Recover their valid scoped or
    # optional gap declarations when only source-status rules changed. For an
    # otherwise invalid historical structure, the compatibility gap remains
    # global; the original unmodified manifest stays in packet context.
    stripped = copy.deepcopy(raw)
    stripped_sources = stripped.get("source_refs", []) if isinstance(stripped, dict) else []
    for source in stripped_sources if isinstance(stripped_sources, list) else []:
        if isinstance(source, dict):
            source.pop("status", None)
    try:
        intake = validate_intake(stripped)
        intake["source_refs"] = sources
    except (ValueError, TypeError, KeyError):
        intake = {"source_refs": sources, "gaps": [], "contradictions": []}
    refs = [source["id"] for source in sources if isinstance(source, dict) and isinstance(source.get("id"), str) and source["id"]]
    gap = {"id": "compatibility:intake-validation", "text": "Saved intake requires refreshed source evidence: " + reason,
           "source_refs": refs or ["discovery:intake"], "required": True, "status": "UNRESOLVED"}
    return {"intake": intake, "compatibility_gaps": [gap],
            "validation": {"status": "INVALID_LEGACY_INTAKE", "reason": reason,
                           "source_statuses": statuses, "compatibility_gaps": [gap], "historical_record_preserved": True}}

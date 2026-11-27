"""Private project check scripts declared as read-only review context.

Evidence commands may run a project's own check scripts kept in the private
installation. Only a dedicated subfolder qualifies; the state database,
credentials and everything else under the private home are never review input.
The bodies go to independent sessions only (redacted, size-limited); their hashes
join the source fingerprint, so an edit after approval makes the linked check
evidence and the judgments over it stale. Nothing here stages, commits or
publishes a file: the private home stays excluded from Git and leak checks.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath

from ..project_local import DIRECTORY, STATE

# The one private subfolder whose files may be declared: the rest of the private home holds the state DB and credentials.
CHECK_INPUT_DIRECTORY = DIRECTORY + "/" + STATE + "/checks"
# A check script is small source a reviewer reads in full; larger files are not review context.
CHECK_INPUT_MAX_BYTES = 65536
# A bounded declaration keeps every packet small enough for the packet limit.
MAX_CHECK_INPUTS = 20
PACKET_PREFIX = "_ase_check_inputs/"
MANIFEST_PATH = "_ase_context/check_inputs.json"
UNAVAILABLE = "UNAVAILABLE: "
RECOMMENDATION = ("A check script should print one explicit result per rule (what it checks, the threshold, the "
                  "violations found) and have a separate sensitivity run with an injected break that must fail.")


def validate_check_inputs(value, evidence_ids):
    """[{'path': '.devframework/local/checks/<file>', 'evidence_ids': [<command evidence id>, ...]}]."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CHECK_INPUTS:
        raise ValueError("check_inputs must list 1 to " + str(MAX_CHECK_INPUTS) + " declared check scripts")
    seen = set()
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"path", "evidence_ids"} or not isinstance(item["path"], str)
                or not isinstance(item["evidence_ids"], list) or not item["evidence_ids"]
                or not all(isinstance(name, str) and name for name in item["evidence_ids"])):
            raise ValueError("Each check input needs a path and the evidence_ids of the checks that run it")
        path = item["path"]
        parts = PurePosixPath(path).parts
        if ("\\" in path or PurePosixPath(path).is_absolute() or ".." in parts or ":" in path
                or len(parts) < 4 or "/".join(parts[:3]).lower() != CHECK_INPUT_DIRECTORY):
            raise ValueError("A check input must be a file under " + CHECK_INPUT_DIRECTORY + "/: " + path)
        if path.lower() in seen:
            raise ValueError("Duplicate check input: " + path)
        seen.add(path.lower())
        unknown = sorted(set(item["evidence_ids"]) - set(evidence_ids))
        if unknown:
            raise ValueError("Check input names unknown evidence ids: " + ", ".join(unknown))
    return value


def read_check_input(root, path):
    """Exact bytes of one declared check script, refusing links and anything outside the checks folder."""
    root = Path(root).resolve()
    folder = root.joinpath(*CHECK_INPUT_DIRECTORY.split("/"))
    candidate = root.joinpath(*PurePosixPath(path).parts)
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(current)):
            raise ValueError("check input path is a link")
    if not candidate.resolve().is_relative_to(folder.resolve()) or not candidate.is_file():
        raise ValueError("check input is not a regular file in " + CHECK_INPUT_DIRECTORY)
    if candidate.stat().st_nlink > 1:
        raise ValueError("check input is hardlinked")
    if candidate.stat().st_size > CHECK_INPUT_MAX_BYTES:
        raise ValueError("check input exceeds " + str(CHECK_INPUT_MAX_BYTES) + " bytes")
    data = candidate.read_bytes()
    if len(data) > CHECK_INPUT_MAX_BYTES:
        raise ValueError("check input exceeds " + str(CHECK_INPUT_MAX_BYTES) + " bytes")
    return data


def observe(root, contract):
    """{path: sha256 or 'UNAVAILABLE: reason'} for the contract's declared check inputs."""
    observed = {}
    for item in contract.get("check_inputs", []):
        try:
            observed[item["path"]] = hashlib.sha256(read_check_input(root, item["path"])).hexdigest()
        except (OSError, ValueError) as error:
            observed[item["path"]] = UNAVAILABLE + str(error)
    return observed


def gate_blockers(observed):
    """A declared check input that cannot be read is no reviewable evidence context: the gate blocks."""
    return ["Declared check input unavailable: " + path + " (" + value[len(UNAVAILABLE):] + ")"
            for path, value in sorted(observed.items()) if value.startswith(UNAVAILABLE)]


def captured_hashes(files):
    """Read original input hashes from the manifest of an authenticated packet."""
    import json
    manifest = next((row for row in files if row["path"] == MANIFEST_PATH), None)
    if manifest is None:
        return None
    return {row["path"]: row["sha256"] for row in json.loads(manifest["content"])["inputs"]}


def approval_observations(db, task, plan, observed, stage_id):
    """For shared inputs prefer a changed baseline from any covered stage."""
    from ..profiles import stage_evidence_specs
    from ..state import read_record
    from ..tasks import approval_for_stage

    task_id, contract = task["task_id"], task["contract"]
    current = read_record(db, "approval", task_id)
    stages = [stage_id] if stage_id is not None else task["approved_stage_ids"]
    approved = {}
    for stage in stages:
        grant = approval_for_stage(db, task_id, stage, current=current)["payload"] or {}
        if stage not in task["approved_stage_ids"] or grant.get("proposal_hash") != task["proposal_hash"]:
            continue
        checks = {check["id"] for check in stage_evidence_specs(contract, plan["checks"], stage)}
        hashes = grant.get("check_inputs", {})
        for item in contract["check_inputs"]:
            path = item["path"]
            if path in hashes and checks.intersection(item["evidence_ids"]):
                if path not in approved or approved[path] == observed.get(path):
                    approved[path] = hashes[path]
    return approved


def packet_files(root, contract, observed, approved):
    """Redacted read-only bodies plus a manifest; unreadable or changed inputs are named as missing coverage.

    `approved` contains a baseline for each input covered by one of the selected stage approvals.
    """
    from ..core import redact
    files, rows, missing = [], [], []
    for item in contract.get("check_inputs", []):
        path = item["path"]
        row = {"path": path, "evidence_ids": item["evidence_ids"], "sha256": observed.get(path),
               "approved_sha256": approved.get(path),
               "changed_since_approval": path in approved and approved[path] != observed.get(path)}
        try:
            data = read_check_input(root, path)
            digest = hashlib.sha256(data).hexdigest()
            if digest != observed.get(path):
                raise ValueError("check input changed while the packet was built")
            text = data.decode("utf-8")
            if "\0" in text:
                raise ValueError("check input contains binary NUL bytes")
        except (OSError, ValueError, UnicodeError) as error:
            missing.append("Declared check input unavailable: " + path + ": " + str(error))
            rows.append({**row, "body_status": "UNAVAILABLE"})
            continue
        content = redact(text)
        files.append({"path": PACKET_PREFIX + path, "content": content,
                      "sha256": hashlib.sha256(content.encode()).hexdigest()})
        rows.append({**row, "body_status": "SUPPLIED_REDACTED" if content != text else "SUPPLIED",
                     "packet_path": PACKET_PREFIX + path})
    if rows:
        manifest = {"directory": CHECK_INPUT_DIRECTORY, "inputs": rows,
                    "note": ("Private project check scripts run by the listed evidence commands. Read-only context: "
                             "never part of the change, commit or PR. sha256 is the exact file bound into the source "
                             "fingerprint; a later edit makes the linked evidence stale. Secrets are redacted."),
                    "recommendation": RECOMMENDATION}
        from ..state import encode
        content = encode(manifest)
        files.append({"path": MANIFEST_PATH, "content": content,
                      "sha256": hashlib.sha256(content.encode()).hexdigest()})
    return files, missing

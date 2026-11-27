"""Owner-approved review representation of changed files and bounded review parts.

`full_bodies` (the default without the field) sends each changed file in full plus its
before version. `diff_context` sends a unified diff with N context lines instead, but
keeps full bodies for small files, new files and required artifacts. The mode is a
contract field bound by the contract hash: it is never switched on by size, because
that would silently weaken review of a large semantic change.

When even the compact packet does not fit, the owner may approve `parts`: each part
gets its own independent review session over its own changed files, and the gate
requires every changed file to be covered by exactly one independently reviewed part.
A missing, skipped or failed part review never becomes PASS.
"""
import difflib
import hashlib
import re
from pathlib import PurePosixPath

from ..ci_check import TEST_PATH
from ..protocol import lossless_transport
from ..state import encode, hash_value

MODES = ("full_bodies", "diff_context")
# Enough surrounding lines to judge a small edit in its function; the owner may approve more.
DEFAULT_CONTEXT_LINES = 20
MIN_CONTEXT_LINES, MAX_CONTEXT_LINES = 3, 200
# Files up to this size stay whole: a diff saves little and hides less.
DEFAULT_FULL_BODY_BYTES = 4096
MAX_FULL_BODY_BYTES = 65536
# Every part review runs at the same time as the whole-task sessions, inside one call budget; keep that bounded.
MIN_PARTS, MAX_PARTS = 2, 8
MAX_PART_RESOURCES = 200
# Share of the packet limit a proposed part may fill, leaving room for the reviewer's context files.
PART_BUDGET_SHARE = 0.85
DIFF_PREFIX = "_ase_diff/"
BEFORE_PREFIX = "_ase_before/"
MANIFEST_PATH = "_ase_context/changed_files.json"
# Same bounds the gate applies to listed reasons and quoted reviewer text.
MAX_LISTED_REASONS = 20
MAX_QUOTED_CHARACTERS = 300
WHOLE_DIFF_PATH = "_ase_context/source_diff.patch"
PART_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def validate_review_packet(value):
    """{'version': 1, 'changed_files': mode, 'context_lines': n, 'full_body_max_bytes': n, 'parts': [...]}."""
    allowed = {"version", "changed_files", "context_lines", "full_body_max_bytes", "parts"}
    if (not isinstance(value, dict) or set(value) - allowed or type(value.get("version")) is not int
            or value.get("version") != 1 or value.get("changed_files") not in MODES):
        raise ValueError("review_packet requires version 1 and changed_files " + " or ".join(MODES))
    result = {"version": 1, "changed_files": value["changed_files"]}
    if value["changed_files"] == "diff_context":
        lines = value.get("context_lines", DEFAULT_CONTEXT_LINES)
        if type(lines) is not int or not MIN_CONTEXT_LINES <= lines <= MAX_CONTEXT_LINES:
            raise ValueError("review_packet context_lines must be an integer from "
                             + str(MIN_CONTEXT_LINES) + " to " + str(MAX_CONTEXT_LINES))
        limit = value.get("full_body_max_bytes", DEFAULT_FULL_BODY_BYTES)
        if type(limit) is not int or not 0 <= limit <= MAX_FULL_BODY_BYTES:
            raise ValueError("review_packet full_body_max_bytes must be an integer from 0 to " + str(MAX_FULL_BODY_BYTES))
        result.update(context_lines=lines, full_body_max_bytes=limit)
    elif "context_lines" in value or "full_body_max_bytes" in value:
        raise ValueError("review_packet context_lines and full_body_max_bytes apply only to diff_context")
    if "parts" in value:
        parts = value["parts"]
        if not isinstance(parts, list) or not MIN_PARTS <= len(parts) <= MAX_PARTS:
            raise ValueError("review_packet parts must list " + str(MIN_PARTS) + " to " + str(MAX_PARTS) + " parts")
        seen = set()
        for part in parts:
            if (not isinstance(part, dict) or set(part) != {"id", "resources"} or not isinstance(part["id"], str)
                    or not PART_ID.fullmatch(part["id"]) or part["id"] in seen):
                raise ValueError("Each review part needs a unique id and resources")
            seen.add(part["id"])
            resources = part["resources"]
            if not isinstance(resources, list) or not 1 <= len(resources) <= MAX_PART_RESOURCES:
                raise ValueError("Review part resources must list 1 to " + str(MAX_PART_RESOURCES) + " paths")
            for resource in resources:
                if (not isinstance(resource, str) or not resource or "\\" in resource or resource.startswith("/")
                        or ":" in resource or ".." in PurePosixPath(resource).parts):
                    raise ValueError("Review part resources must be workspace-relative paths: " + str(resource))
        result["parts"] = [{"id": part["id"], "resources": [r.rstrip("/") or "." for r in part["resources"]]} for part in parts]
    return result


def policy(contract):
    return contract.get("review_packet")


def parts(contract):
    return (policy(contract) or {}).get("parts") or []


def part_hash(part):
    return hash_value({"id": part["id"], "resources": part["resources"]})


def part_key(task_id, stage_id, part_id):
    return task_id + ":review_part:" + ((stage_id + ":") if stage_id else "") + part_id


def covers(resource, path):
    return resource == "." or path == resource or path.startswith(resource + "/")


def owners(contract, path):
    """Ids of the declared parts whose resources cover a changed path."""
    return [part["id"] for part in parts(contract) if any(covers(resource, path) for resource in part["resources"])]


def _diff(path, before, after, context_lines):
    lines = []
    for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                     "a/" + path, "b/" + path, n=context_lines):
        lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(lines)


def represent(contract, files, changes, artifact_paths, coverage_missing, part_id=None, *, required_context=()):
    """Apply the approved representation to a packet's file list in place; return the per-file manifest row list.

    Without the contract field nothing changes. Every changed file is named in the manifest with what
    the reviewer got: a full body, a diff with context, or the part that reviews it.
    """
    declared = policy(contract)
    if not declared or not changes or not changes.get("changes"):
        return None
    by_path = {row["path"]: row for row in files}
    declared_parts = {part["id"]: part for part in parts(contract)}
    rows, drop, added, other_parts = [], set(), [], {}
    for entry in changes["changes"]:
        path = entry["path"]
        # Hashes stay in _ase_context/source_changes.json; repeating them here doubled the manifest.
        row = {"path": path}
        if declared_parts:
            covering = owners(contract, path)
            if len(covering) != 1:
                coverage_missing.append("Changed file is not covered by exactly one review part: " + path
                                        + " (" + (", ".join(covering) or "no part") + ")")
                drop.update({path, BEFORE_PREFIX + path})
                rows.append({**row, "representation": "NOT_COVERED_BY_EXACTLY_ONE_PART", "parts": covering})
                continue
            row["part"] = covering[0]
            if path in required_context and covering[0] != part_id:
                row["context_only"] = True
            # Whole-task acceptance and test adequacy judge tests themselves: changed tests stay in their packets.
            if (part_id is None and path not in required_context and path not in artifact_paths
                    and not TEST_PATH.search(path)):
                drop.update({path, BEFORE_PREFIX + path})
                rows.append({**row, "representation": "REVIEWED_IN_PART"})
                continue
            if part_id is not None and covering[0] != part_id and path not in required_context:
                drop.update({path, BEFORE_PREFIX + path})
                other_parts[covering[0]] = other_parts.get(covering[0], 0) + 1
                continue
        after, before = by_path.get(path), by_path.get(BEFORE_PREFIX + path)
        if after is None and entry["after_sha256"] is not None:
            rows.append({**row, "representation": "NOT_SUPPLIED", "reason": "body omitted or unavailable; see coverage"})
            continue
        if entry["after_sha256"] is None:
            rows.append({**row, "representation": "DELETED", "reason": "the before version is supplied when available"})
            continue
        if declared["changed_files"] != "diff_context":
            rows.append({**row, "representation": "FULL_BODY", "reason": "full_bodies mode"})
            continue
        reason = ("required review context" if path in required_context
                  else "required artifact" if path in artifact_paths else "new file" if entry["before_sha256"] is None
                  else "before version unavailable" if before is None
                  else "small file" if after is not None and len(after["content"].encode()) <= declared["full_body_max_bytes"]
                  else None)
        if reason:
            rows.append({**row, "representation": "FULL_BODY", "reason": reason})
            continue
        content = ("# unified diff of " + path + " with " + str(declared["context_lines"]) + " context lines; before sha256 "
                   + str(entry["before_sha256"]) + ", after sha256 " + str(entry["after_sha256"]) + "\n"
                   + _diff(path, before["content"], after["content"] if after is not None else "", declared["context_lines"]))
        drop.update({path, BEFORE_PREFIX + path})
        added.append({"path": DIFF_PREFIX + path, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()})
        rows.append({**row, "representation": "DIFF_CONTEXT", "packet_path": DIFF_PREFIX + path})
    files[:] = [row for row in files if row["path"] not in drop] + added
    manifest = {"mode": declared["changed_files"], "context_lines": declared.get("context_lines"),
                "full_body_max_bytes": declared.get("full_body_max_bytes"),
                "part": declared_parts.get(part_id), "parts": [part["id"] for part in declared_parts.values()],
                "files": rows,
                **({"in_other_parts": other_parts} if other_parts else {}),
                "note": ("Owner-approved review representation. FULL_BODY: the current file and any available "
                         "_ase_before/ version are supplied whole. context_only supplies a required file for checking "
                         "interactions. Its changes still belong to the named part. DIFF_CONTEXT: only the unified diff "
                         "under _ase_diff/ with the stated context "
                         "lines; code outside it was not supplied, so name any judgment that needs it as a limit. "
                         "REVIEWED_IN_PART / in_other_parts (a count per part): the body was NOT supplied to this session. A separate "
                         "independent part session only code-reviews it (no acceptance or test-adequacy judgment); an "
                         "acceptance or test-adequacy judgment that needs such a body is UNVERIFIED and blocks. Changed "
                         "test files stay in whole-task packets. NOT_COVERED_BY_EXACTLY_ONE_PART blocks the gate.")}
    content = encode(manifest)
    files.append({"path": MANIFEST_PATH, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()})
    return manifest


# Manifest representations whose content (body or diff) this packet carries.
CARRIED = {"FULL_BODY", "DIFF_CONTEXT", "DELETED"}


def size_breakdown(files, changed, max_bytes):
    """Which part of a packet overflows: the changed files, the context files or the rest."""
    changed = set(changed)
    sizes = {row["path"]: len(row["content"].encode()) for row in files}
    own = sum(size for path, size in sizes.items()
              if path in changed or path.removeprefix(BEFORE_PREFIX) in changed or path.removeprefix(DIFF_PREFIX) in changed)
    context = {path: size for path, size in sizes.items() if path.startswith("_ase_context/")}
    largest = ", ".join(path.removeprefix("_ase_context/") + " " + str(size)
                        for path, size in sorted(context.items(), key=lambda item: -item[1])[:3])
    return ("Size breakdown (bytes, before transport compaction; limit " + str(max_bytes) + "): changed files " + str(own)
            + ", context files " + str(sum(context.values())) + " (largest: " + largest + "), other "
            + str(sum(sizes.values()) - own - sum(context.values())) + ".")


def changed_sizes(files, changed):
    """UTF-8 bytes each changed path contributes to a packet (body, before version, diff), before any representation."""
    sizes = {}
    by_path = {row["path"]: len(row["content"].encode()) for row in files}
    for path in changed:
        sizes[path] = sum(by_path.get(name, 0) for name in (path, BEFORE_PREFIX + path, DIFF_PREFIX + path))
    return sizes


def propose_parts(sizes, budget):
    """Group changed paths by folder into parts that each fit the budget; a proposal for the owner, not a decision."""
    pending, result = [("", sorted(sizes))], []
    while pending:
        prefix, paths = pending.pop(0)
        children = {}
        for path in paths:
            rest = path[len(prefix):]
            head = rest.split("/", 1)[0]
            children.setdefault(prefix + head + ("/" if "/" in rest else ""), []).append(path)
        bins = []
        for key, members in sorted(children.items()):
            size = sum(sizes[path] for path in members)
            if size > budget and key.endswith("/") and len(members) > 1:
                pending.append((key, members))
                continue
            target = next((item for item in bins if item[0] + size <= budget), None)
            if target is None:
                target = [0, []]
                bins.append(target)
            target[0] += size
            target[1].append(key.rstrip("/"))
        result.extend(resources for _, resources in bins)
    return [{"id": "p" + str(index + 1).zfill(2), "resources": resources} for index, resources in enumerate(result)]


def overflow_advice(contract, files, sizes, max_bytes, total, *, required_context=()):
    """What the owner can approve when a review packet does not fit; never applied automatically."""
    declared = policy(contract)
    lossless_advice = ("Required context fills the uncompressed estimate. With lossless transport, measure each "
                       "proposed part before deciding whether a split fits."
                       if required_context and lossless_transport(contract) else None)
    if required_context and sum(changed_sizes(files, required_context).values()) >= max_bytes:
        return lossless_advice or "Required context files alone fill the packet limit. Splitting changed files cannot help."
    if parts(contract):
        return ("Review parts are already approved and this packet still does not fit (changed tests stay in "
                "whole-task packets); approve fewer context_lines or split the task instead.")
    if not declared or declared["changed_files"] != "diff_context":
        return ("Ask the owner to approve review_packet {\"version\": 1, \"changed_files\": \"diff_context\"} in the "
                "contract (diffs with context instead of two full copies); it is never chosen by size.")
    # Diff sizes are what a part will carry; the full bodies counted before the representation overstate them.
    current = changed_sizes(files, sizes)
    # Each required file still has one owning part, but its bytes are fixed overhead in every part.
    movable = sum(size for path, size in current.items() if path not in required_context)
    sizes = {path: 0 if path in required_context else current[path] or sizes[path] for path in sizes}
    budget = int(max_bytes * PART_BUDGET_SHARE) - (total - movable)
    if budget <= 0:
        return lossless_advice or "The context files alone exceed the packet limit. A split of changed files cannot help."
    proposal = propose_parts(sizes, budget)
    if len(proposal) > MAX_PARTS:
        return ("Even a split into " + str(MAX_PARTS) + " parts does not fit; approve fewer context_lines or split "
                "the task instead.")
    return ("Ask the owner to approve fewer context_lines, or this split as review_packet.parts (each part is reviewed by its own independent "
            "session and the gate requires every changed file in exactly one part): " + encode(proposal))


def known_sessions(store, task_id, stage_id, current, extra=()):
    """(execution ids, provider session ids) of every independent session already recorded for this gate."""
    from ..runner import verify_receipt
    from ..state import read_record
    sessions, executions = set(), {current.get("implementation_execution_id")} - {None}
    adequacy = task_id + ":adequacy" + (":" + stage_id if stage_id else "")
    for kind, key in [("verification", task_id + ":ac"), ("verification", task_id + ":review"),
                      ("verification", adequacy), *extra]:
        record = read_record(store.db, kind, key)["payload"]
        if not record:
            continue
        executions.add(record.get("execution_id"))
        try:
            session = verify_receipt(record)["result"].get("session_id")
        except (ValueError, OSError, KeyError, TypeError):
            session = None
        if session:
            sessions.add(session)
    return executions, sessions


def review_session_verdict(store, contract, current, receipt, label, executions, sessions, extra_bindings, *,
                           dismissed=frozenset()):
    """(blocked, rejected) for one additional independent review session bound to the current gate identities.

    Adds the session to `executions`/`sessions`, so sibling sessions cannot share it. A blocking finding whose
    content ID is in `dismissed` was reconciled against the requirement by the caller and does not reject.
    """
    from pathlib import Path
    from ..findings import finding_id, substantive_review
    from ..runner import verify_receipt
    blocked, rejected = [], []
    if not receipt:
        return [label + ": independent review session missing"], []
    if (receipt.get("role") != "review"
            or not Path(receipt.get("artifact_root", "")).resolve().is_relative_to(store.root.resolve())):
        return [label + ": receipt is not a review session of this project"], []
    if receipt.get("execution_id") in executions:
        return [], [label + ": reused another execution"]
    executions.add(receipt.get("execution_id"))
    captured = receipt.get("bindings") if isinstance(receipt.get("bindings"), dict) else {}
    expected = {"fingerprint": current.get("fingerprint"), "contract_sha256": current.get("contract_hash"),
                "proposal_sha256": current.get("proposal_hash"), "plan_sha256": current.get("plan_hash"),
                "check_sha256": current.get("check_hash"), **extra_bindings,
                "verification_context_sha256": (captured.get("verification_context_sha256")
                    if captured.get("verification_context_sha256") in current.get("verification_context_sha256s", ())
                    else current.get("verification_context_sha256", "missing"))}
    if current.get("workspace_id"):
        expected["workspace_id"] = current["workspace_id"]
    if "lead_review_sha256" in captured or current.get("lead_review_required"):
        expected["lead_review_sha256"] = current.get("lead_review_sha256")
    try:
        verified = verify_receipt(receipt, expected_bindings=expected)
    except (ValueError, OSError, KeyError, TypeError) as error:
        return [label + ": receipt invalid: " + str(error)], []
    if verified["evidence_kind"] != "independent_cli":
        return [label + ": protocol fixtures are not independent semantic approval"], []
    payload = verified["result"].get("payload")
    session = verified["result"].get("session_id")
    if verified.get("request_identity", {}).get("purpose") == "test_adequacy" or not substantive_review(payload):
        return [label + ": receipt is not substantive review evidence"], []
    if not isinstance(session, str) or not session:
        return [label + ": receipt has no provider session id"], []
    if session in sessions:
        return [], [label + ": reused another independent session"]
    sessions.add(session)
    if contract.get("review_format") == {"version": 2} and not isinstance(payload.get("unverified_material"), list):
        blocked.append(label + ": result lacks the review format v2 fields")
    unverified = payload.get("unverified_material")
    for item in (unverified[:MAX_LISTED_REASONS] if isinstance(unverified, list) else []):
        blocked.append(label + ": could not verify material change: " + str(item)[:MAX_QUOTED_CHARACTERS])
    for finding in payload["findings"]:
        if not isinstance(finding, dict) or finding.get("severity") not in {"P0", "P1", "P2", "P3"}:
            blocked.append(label + ": malformed review finding")
        elif finding["severity"] in {"P0", "P1", "P2"} and finding_id(finding) not in dismissed:
            rejected.append(label + ": " + finding["severity"] + ": "
                            + str(finding.get("message", "Blocking review finding"))[:MAX_QUOTED_CHARACTERS])
    return blocked, rejected


def part_gate(store, contract, task_id, stage_id, current, changed):
    """(blocked, rejected) for an owner-approved split: coverage, current binding and each part's own verdict.

    `current` holds the gate's current identities; `changed` the changed paths against the baseline.
    """
    declared = parts(contract)
    if not declared:
        return [], []
    from ..state import read_record
    blocked, rejected = [], []
    uncovered = [path for path in sorted(changed) if len(owners(contract, path)) != 1]
    for path in uncovered[:MAX_LISTED_REASONS]:
        blocked.append("Changed file is not covered by exactly one independently reviewed part: " + path)
    if len(uncovered) > MAX_LISTED_REASONS:
        blocked.append(str(len(uncovered) - MAX_LISTED_REASONS) + " more changed files are not covered by exactly one review part")
    # A part session must be its own execution: never the implementation's or another independent session's.
    executions, sessions = known_sessions(store, task_id, stage_id, current)
    for part in declared:
        receipt = read_record(store.db, "verification_part", part_key(task_id, stage_id, part["id"]))["payload"]
        part_blocked, part_rejected = review_session_verdict(store, contract, current, receipt, "Review part " + part["id"],
            executions, sessions, {"review_part_sha256": part_hash(part)})
        blocked.extend(part_blocked)
        rejected.extend(part_rejected)
    return blocked, rejected

"""Stable original-obligation identities for durable, conservative recovery."""
import hashlib
import os
from pathlib import Path
import posixpath
import re
from datetime import datetime

from .profiles import stage_evidence_specs
from .state import hash_value, read_record
from .native_paths import logical_path, state_io


def check_identity(spec):
    return hash_value({key: value for key, value in spec.items() if key != "id"})


def initial_history(contract):
    groups = {}
    for stage in contract["stages"]:
        groups[stage["id"]] = {
            "scope": stage["scope"],
            "acceptance": [ac for ac in contract["acceptance"] if ac["id"] in stage["acceptance_ids"]],
            "checks": stage_evidence_specs(contract, contract["evidence"], stage["id"]),
            "cycles": 0, "diagnostics": 0, "stagnation": 0, "probe_pending": False,
            "stopped": False, "diagnosed_generation": None, "progress_generation": 0, "probe_inflight": None,
            "diagnostic_inflight": None, "probe_authorization": None,
        }
    hard_limits = {key: value for key, value in contract["execution_policy"].items()
                   if key in {"max_stage_cycles", "max_task_cycles", "max_diagnostic_passes", "active_time_budget_seconds"}
                   and value is not None}
    return {"version": 1, "original_contract_hash": hash_value(contract), "hard_limits": hard_limits, "groups": groups,
            "aliases": {}, "high_water": [], "failures": {}, "events": []}


def _ancestor_stage(store, owner, task_id, stage_id=None):
    """Map an actual descendant's spending to one immutable ancestor owner."""
    seen = set()
    while task_id != owner:
        if task_id in seen:
            raise ValueError("Parent task cycle")
        seen.add(task_id)
        contract = store.task(task_id)["contract"]
        task_id, stage_id = contract.get("parent_task_id"), contract.get("parent_stage_id")
        if not task_id:
            return False, None
    return True, stage_id


def _created_after(task, before):
    try:
        return bool(task.get("created") and before and datetime.fromisoformat(task["created"]) > datetime.fromisoformat(before))
    except (ValueError, TypeError):
        return False


def task_bound_contexts(store, history, tasks, budget, stage_id=None, *, updated=None):
    """Keep local original allowances beside, never instead of, root history.

    Root counters deliberately include inherited obligation high-water. Separate
    owner counters prevent that earlier spending from consuming a new child's
    own allowance, while every descendant still spends its ancestors' bounds.
    """
    owners = history.setdefault("task_bounds", {})
    result = []
    for task in tasks[:-1]:  # The existing root history already owns root bounds.
        owner = task["task_id"]
        if owner not in owners:
            original = read_record(store.db, "task_version", owner + ":1")["payload"] or task
            initial = initial_history(original["contract"])
            entry = {key: initial[key] for key in ("hard_limits", "groups", "aliases")}
            entry["task_cycles"] = 0
            # Legacy stage counters may include inherited high-water. Ownership
            # is known, exact local counts are not: import them conservatively.
            for field, target in (("stages", "cycles"), ("diagnostics", "diagnostics")):
                for key, count in budget.get(field, {}).items():
                    parts = key.split(":", 1)
                    if len(parts) < 2:
                        # Bare legacy stage names carry no owner provenance.
                        # Earlier spending cannot belong to a later-created task.
                        if _created_after(task, updated):
                            continue
                        related, mapped = True, None
                    else:
                        related, mapped = _ancestor_stage(store, owner, *parts)
                    if related:
                        try:
                            groups = (bind_stage(entry, owner, task["contract"], mapped, root_id=owner)
                                      if mapped is not None else list(entry["groups"]))
                        except (ValueError, KeyError):
                            # A saved old stage name may no longer exist. Its
                            # spending cannot disappear with that ambiguity.
                            groups = list(entry["groups"])
                        for name in groups:
                            entry["groups"][name][target] += count
                        if field == "stages":
                            entry["task_cycles"] += count
            owners[owner] = entry
        entry = owners[owner]
        for key, value in task["contract"]["execution_policy"].items():
            if key in {"max_stage_cycles", "max_task_cycles", "max_diagnostic_passes", "active_time_budget_seconds"} and value is not None:
                entry["hard_limits"][key] = min(value, entry["hard_limits"].get(key, value))
        groups = bind_stage(entry, owner, task["contract"], stage_id, root_id=owner) if stage_id is not None else []
        result.append((task, entry, groups))
        stage_id = task["contract"].get("parent_stage_id") if stage_id is not None else None
    return result


def initialize_timing(budget, updated):
    """Retain old unowned time explicitly; never silently reinterpret it as zero."""
    if "active_by_task" not in budget:
        budget["active_by_task"] = {}
        budget["unattributed_active_seconds"] = budget.get("active_seconds", 0)
        budget["unattributed_before"] = updated


def task_active_seconds(store, task, budget):
    spent = sum(seconds for owner, seconds in budget.get("active_by_task", {}).items()
                if _ancestor_stage(store, task["task_id"], owner)[0])
    unknown = budget.get("unattributed_active_seconds", 0)
    if unknown:
        # A task created after the last old timing record cannot own that old
        # spending. Existing tasks retain the conservative aggregate until it
        # can be reconciled; timestamps missing in legacy state prove nothing.
        if not _created_after(task, budget.get("unattributed_before")):
            spent += unknown
    return spent


def _resource_key(value):
    normalized = value.replace("\\", "/").rstrip("/")
    # URI paths and opaque identifiers retain case. Only local file paths use
    # platform case rules; lexical aliases must not erase original obligations.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", normalized) and not re.match(r"^[A-Za-z]:/", normalized):
        return normalized
    normalized = posixpath.normpath(normalized)
    return os.path.normcase(normalized).replace("\\", "/") if os.name == "nt" else normalized


def _overlap(left, right):
    for first in left:
        for second in right:
            first, second = _resource_key(first), _resource_key(second)
            if first == second or first == "." or second == "." or first.startswith(second + "/") or second.startswith(first + "/"):
                return True
    return False


def bind_stage(history, task_id, contract, stage_id, *, root_id, parent_groups=None):
    """New labels inherit overlapping old obligations; ambiguity reduces allowance."""
    key = task_id + ":" + stage_id
    stage = next((s for s in contract["stages"] if s["id"] == stage_id), None)
    if stage is None:
        raise ValueError("Unknown recovery stage: " + str(stage_id))
    groups = history["groups"]
    selected = set(history["aliases"].get(key, []))
    if parent_groups is not None:
        selected.update(parent_groups)
    else:
        acceptance = [ac for ac in contract["acceptance"] if ac["id"] in stage["acceptance_ids"]]
        identities = {check_identity(spec) for spec in stage_evidence_specs(contract, contract["evidence"], stage_id)}
        named = groups.get(stage_id) if task_id == root_id else None
        unchanged = (named is not None and stage["scope"] == named["scope"]
                     and {hash_value(ac) for ac in acceptance} == {hash_value(ac) for ac in named["acceptance"]}
                     and identities == {check_identity(spec) for spec in named["checks"]})
        # Exact unchanged root stages retain independent allowances even when
        # they share project-wide checks or resources. For revised obligations,
        # every credible link counts: a reused criterion ID must never suppress
        # the original check or resource history. Conflicts reduce allowance.
        matched = {stage_id} if unchanged else {name for name, group in groups.items() if (
            any(ac["id"] == original["id"] or ac["expected"] == original["expected"]
                for ac in acceptance for original in group["acceptance"])
            or identities & {check_identity(spec) for spec in group["checks"]}
            or _overlap(stage["scope"]["resources"], group["scope"]["resources"]))}
        # An entirely revised goal cannot erase previously spent root history.
        selected.update(matched or groups)
    history["aliases"][key] = sorted(selected)
    return sorted(selected)


def canonical_failures(history, groups, reasons):
    """Review wording cannot change a known failing AC/check's identity."""
    obligations = set()
    for name in groups:
        group = history["groups"][name]
        for ac in group["acceptance"]:
            if any(ac["id"] in reason.split(":", 1)[-1].replace("/", " ").split() for reason in reasons if "AC" in reason):
                obligations.add("ac:" + ac["id"])
        for spec in group["checks"]:
            if any(reason.endswith(": " + spec["id"]) or reason.endswith("/" + spec["id"]) for reason in reasons):
                obligations.add("check:" + spec["id"])
    return sorted(obligations) or ["stage:" + name for name in groups]


def progress_tokens(history, groups, contract, stage_id, checked, accepted, clean_review):
    stage = next(s for s in contract["stages"] if s["id"] == stage_id)
    tokens = set()
    for name in groups:
        group = history["groups"][name]
        for original in group["checks"]:
            if any(spec["id"] in checked and check_identity(spec) == check_identity(original) for spec in contract["evidence"]):
                tokens.add("check:" + original["id"] + ":" + check_identity(original))
        for original in group["acceptance"]:
            if any(ac["id"] in stage["acceptance_ids"] and ac["id"] in accepted
                   and ac["expected"] == original["expected"] for ac in contract["acceptance"]):
                tokens.add("ac:" + original["id"])
        if clean_review:
            tokens.add("review:" + name)
    return sorted(tokens)


def group_tokens(name, group):
    return ({"ac:" + ac["id"] for ac in group["acceptance"]}
            | {"check:" + spec["id"] + ":" + check_identity(spec) for spec in group["checks"]}
            | {"review:" + name} | set(group.get("case_tokens", [])))


def _test_manifest(sources, snapshot):
    """Expand the declared immutable test/helper/config boundary prospectively."""
    if not isinstance(sources, list) or not sources or not all(isinstance(path, str) and path for path in sources):
        return None
    files, selected = snapshot.get("files", {}), {}
    for raw in sources:
        path = raw.replace("\\", "/").rstrip("/")
        if Path(path).is_absolute() or ":" in path or ".." in Path(path).parts or not path:
            return None
        matches = {key: value for key, value in files.items() if path == "." or key == path or key.startswith(path + "/")}
        if not matches:
            return None
        selected.update(matches)
    # Runner/startup configuration can change discovery without editing a case.
    configuration = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "unittest.cfg", ".coveragerc",
                     "sitecustomize.py", "usercustomize.py"}
    selected.update({key: value for key, value in files.items() if key in configuration})
    return selected


def _unittest_cases(output, exit_code):
    """Accept one complete ordinary verbose unittest run; unfamiliar forms earn nothing."""
    summaries = re.findall(r"(?m)^Ran (\d+) tests? in [0-9.]+s\s*$", output)
    terminals = re.findall(r"(?m)^(OK|FAILED \([^\r\n]+\))\s*$", output)
    if len(summaries) != 1 or len(terminals) != 1 or not output.rstrip().endswith(terminals[0]):
        return None
    lines = re.findall(r"(?m)^([A-Za-z_]\w*) \(([A-Za-z_][\w.]*)\) \.\.\. (ok|FAIL|ERROR)\s*$", output)
    cases = {}
    for method, qualified, status in lines:
        case_id = qualified if qualified.endswith("." + method) else qualified + "." + method
        if case_id in cases:
            return None
        cases[case_id] = status
    if not cases or len(cases) != int(summaries[0]):
        return None
    failed, errors = list(cases.values()).count("FAIL"), list(cases.values()).count("ERROR")
    terminal = terminals[0]
    if terminal == "OK":
        return cases if exit_code == 0 and not failed and not errors else None
    counts = re.fullmatch(r"FAILED \((?:failures=(\d+)(?:, errors=(\d+))?|errors=(\d+))\)", terminal)
    if not counts or exit_code != 1 or (int(counts[1] or 0), int(counts[2] or counts[3] or 0)) != (failed, errors) or not failed + errors:
        return None
    return cases


def _unittest_module_base(argv):
    """Resolve only explicit, unambiguous stdlib unittest command layouts."""
    try:
        module = argv.index("-m")
        if argv[module + 1] != "unittest":
            return None
        arguments = argv[module + 2:]
    except (ValueError, IndexError):
        return None
    if "discover" not in arguments:
        return "."
    arguments = arguments[arguments.index("discover") + 1:]
    values, index = {}, 0
    while index < len(arguments):
        argument = arguments[index]
        field = next((name for name, flags in (("start", ("-s", "--start-directory")),
                     ("top", ("-t", "--top-level-directory"))) if argument.split("=", 1)[0] in flags), None)
        if field:
            if field in values:
                return None
            if "=" in argument:
                values[field] = argument.split("=", 1)[1]
            elif index + 1 < len(arguments):
                index += 1
                values[field] = arguments[index]
            else:
                return None
        elif argument in {"-p", "--pattern", "-k", "--durations"}:
            index += 1
            if index >= len(arguments):
                return None
        elif argument in {"-v", "--verbose", "-q", "--quiet", "-f", "--failfast", "-c", "--catch", "-b", "--buffer", "--locals"}:
            pass
        elif not any(argument.startswith(option + "=") for option in ("--pattern", "--durations")):
            return None
        index += 1
    base = values.get("top", values.get("start", ".")).replace("\\", "/").rstrip("/")
    if not base or Path(base).is_absolute() or ":" in base or ".." in Path(base).parts:
        return None
    return base


def _original_case_modules(cases, module_base, manifest):
    for case_id in cases:
        parts = case_id.split(".")
        candidates = {str(Path(module_base) / ("/".join(parts[:index]) + ".py")).replace("\\", "/")
                      for index in range(1, len(parts) - 1)}
        if len(candidates & manifest.keys()) != 1:
            return False
    return True


def original_case_progress(store, root_id, task_id, history, groups, contract, stage_id, bundle, source, checked):
    """Recovery-only case elimination; the mandatory command still has to pass."""
    baseline = read_record(store.db, "baseline", root_id)["payload"] or {}
    selected = stage_evidence_specs(contract, contract["evidence"], stage_id)
    tokens = set()
    registry = history.setdefault("test_cases", {})
    for spec in selected:
        if "progress_test_sources" not in spec:
            continue
        # An annotated check cannot gain even whole-check recovery credit by
        # weakening its original declared test definitions or switching cases.
        checked.discard(spec["id"])
        original = [(name, candidate) for name in groups for candidate in history["groups"][name]["checks"]
                    if check_identity(candidate) == check_identity(spec)]
        if not original or spec.get("kind") != "test" or spec.get("parser") != "unittest":
            continue
        before = _test_manifest(spec["progress_test_sources"], baseline)
        current = _test_manifest(spec["progress_test_sources"], source)
        if not before or current != before:
            continue
        matches = [result for result in bundle.get("results", []) if result.get("id") == spec["id"]]
        if len(matches) != 1:
            continue
        result = matches[0]
        try:
            log = logical_path(result["log"])
            physical = state_io(log)
            if (result.get("kind") != "test" or result.get("configured_argv") != spec["argv"]
                    or not result.get("started") or not result.get("ended")
                    or Path(result["cwd"]).resolve() != store.repo.resolve()
                    or any(result.get(key) for key in ("error", "timed_out", "cancelled", "output_limited"))
                    or not log.is_relative_to((store.root / "tasks" / task_id / "checks").resolve())
                    or physical.stat().st_size > 1048576):
                continue
            data = physical.read_bytes()
            if hashlib.sha256(data).hexdigest() != result.get("log_sha256"):
                continue
            cases = _unittest_cases(data.decode("utf-8"), result.get("exit_code"))
        except (ValueError, OSError, KeyError, TypeError, UnicodeError):
            continue
        if not cases:
            continue
        # Verbose case IDs must resolve to original test modules inside the
        # declared boundary, rather than newly imported/dynamically renamed cases.
        module_base = _unittest_module_base(spec["argv"])
        if module_base is None:
            continue
        if not _original_case_modules(cases, module_base, before):
            continue
        for name, candidate in original:
            key = candidate["id"] + ":" + check_identity(candidate)
            entry = registry.setdefault(key, {"ids": sorted(cases), "initial_failures": sorted(case_id for case_id, status in cases.items() if status != "ok"),
                "test_manifest": before, "proof": {"log": result["log"], "log_sha256": result["log_sha256"],
                    "fingerprint": bundle["fingerprint"], "argv": spec["argv"]}})
            if entry["ids"] != sorted(cases) or entry["test_manifest"] != before:
                continue
            if all(status == "ok" for status in cases.values()):
                checked.add(spec["id"])
            possible = {"case:" + key + ":" + case_id for case_id in entry["initial_failures"]}
            group = history["groups"][name]
            group["case_tokens"] = sorted(set(group.get("case_tokens", [])) | possible)
            tokens.update("case:" + key + ":" + case_id for case_id in entry["initial_failures"] if cases[case_id] == "ok")
    return tokens

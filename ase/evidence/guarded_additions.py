"""Check supported additions against the exact approved stage effects."""
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
import subprocess
from typing import Callable, Collection

from ..core import digest
from ..project_local import ARM_RESOURCE_GUARD, DOTNET_PACKAGE_GUARD, EFCORE_SCHEMA_GUARD, enabled_addition_guards
from ..source_coverage import PACKET_FILE_LIMIT, source_bytes
from ..state import read_record, transaction
from . import arm_resources, dotnet_packages, efcore_schema
from .packets import _BaselineBlobs


@dataclass(frozen=True)
class Check:
    setting: str
    field: str
    label: str
    matches: Callable[[PurePosixPath], bool]
    read_targets: Callable[[str], Collection[tuple[str, ...]]]  # A Counter keeps repeated declarations.
    effect_target: Callable[[dict], tuple[str, ...]]
    display: Callable[[tuple[str, ...]], str]
    identity_fields: tuple[str, ...]
    kinds: frozenset[str] | None  # Effect kinds that may carry the target; None accepts any.
    preserve_rename: bool = False
    # A path outside the conventional location is analyzed only when its content carries the marker.
    conventional: Callable[[PurePosixPath], bool] = lambda path: True
    marker: Callable[[str], bool] = lambda content: True


CHECKS = (
    Check(DOTNET_PACKAGE_GUARD, 'dotnet_package', 'Direct .NET dependency', dotnet_packages.matches,
          dotnet_packages.read_targets, dotnet_packages.target, lambda names: names[0], ('package',),
          None, preserve_rename=True),
    Check(EFCORE_SCHEMA_GUARD, 'efcore_schema', 'EF Core schema addition', efcore_schema.matches,
          efcore_schema.read_targets, efcore_schema.target, lambda parts: '.'.join(part for part in parts if part),
          ('schema', 'table', 'column'), frozenset({'schema', 'infrastructure'}),
          conventional=efcore_schema.conventional, marker=efcore_schema.marker),
    Check(ARM_RESOURCE_GUARD, 'arm_resource', 'ARM resource addition', arm_resources.matches,
          arm_resources.read_targets, arm_resources.target, lambda parts: ': '.join(part for part in parts if part),
          ('type', 'name', 'scope'),
          frozenset({'infrastructure'}), conventional=arm_resources.conventional, marker=arm_resources.marker),
)
EFFECT_FIELDS = {check.field for check in CHECKS}


def effect_target(effect):
    checks = [check for check in CHECKS if check.field in effect]
    if not checks:
        return None
    if len(checks) != 1:
        raise ValueError('Each approval effect names one addition')
    check = checks[0]
    value = effect[check.field]
    path = value.get('path') if isinstance(value, dict) else None
    if (not isinstance(path, str) or len(path) > 500 or '\\' in path or ':' in path or '\0' in path
            or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
            or PurePosixPath(path).as_posix() != path or not check.matches(PurePosixPath(path))):
        raise ValueError(check.label + ' requires a supported workspace-relative path')
    if check.kinds is not None and effect.get('kind') not in check.kinds:
        raise ValueError(check.label + ' requires effect kind ' + ' or '.join(sorted(check.kinds)))
    return check.field, path, check.effect_target(value)


def owner_decision_required(effect):
    """Infrastructure, and a table or column addition of any kind, is the owner's decision."""
    return effect.get('kind') == 'infrastructure' or 'efcore_schema' in effect


def _lineage_targets(tasks, lineage):
    from ..tasks import approval_for_stage, decision_stages

    for owner, stage_id in lineage:
        decision = approval_for_stage(tasks.store.db, owner['task_id'], stage_id)['payload'] or {}
        if (decision.get('owner_preapproved') or stage_id not in decision_stages(decision)
                or decision.get('proposal_hash') != owner['proposal_hash']):
            continue
        for effect in (owner.get('proposal') or {}).get('effects', []):
            if stage_id in effect['stage_ids'] and (target := effect_target(effect)) is not None:
                yield target


def _approved_targets(tasks, task, stage_id):
    def eligible(stage):
        if stage_id is None or stage == stage_id:
            return True
        # Another stage's additions are in the diff once it has run. Its targets still need its approval
        # under the current proposal (collect); a proposal revision schedules a re-run but does not undo
        # the earlier completed run, so that proof still counts here.
        proof = read_record(tasks.store.db, 'stage', task['task_id'] + ':' + stage)['payload'] or {}
        return proof.get('state') == 'COMPLETED'

    def collect(subject, stage):
        try:
            lineage = tasks.approved_lineage(subject['task_id'], stage,
                allow_terminal=subject['state'] == 'COMPLETED', check_sources=False)
        except ValueError:
            return  # The regular gate reports invalid stage or inherited approval.
        current, _ = lineage[0]
        if any(current[key] != subject[key] for key in ('contract_hash', 'proposal_hash')):
            return
        authorized.update(_lineage_targets(tasks, lineage))
        # A completed required child may carry its own scoped owner decision.
        scope = next(item for item in current['contract']['stages'] if item['id'] == stage)
        child_id = scope.get('required_child_task_id')
        if child_id:
            child = read_record(tasks.store.db, 'task', child_id)['payload']
            if (child and child['state'] == 'COMPLETED'
                    and child['contract'].get('parent_task_id') == current['task_id']
                    and child['contract'].get('parent_stage_id') == stage):
                for child_stage in child['approved_stage_ids']:
                    collect(child, child_stage)

    authorized = set()
    with transaction(tasks.store.db, immediate=False):
        for stage in task['approved_stage_ids']:
            if eligible(stage):
                collect(task, stage)
    return authorized


def _whole_text(root, path, expected):
    """Unbounded, lenient read of the snapshotted file, used only to recognise its kind."""
    from ..source_coverage import source_path
    data = source_path(root, path).read_bytes()
    if digest(data) != expected.removesuffix(':x'):
        raise ValueError('File changed during comparison')
    return data.decode('utf-8', errors='replace')


def _current_text(root, path, expected):
    data = source_bytes(root, path)
    if digest(data) != expected.removesuffix(':x'):
        raise ValueError('File changed during comparison')
    return data.decode('utf-8')


def _compare(check, before, after, paths, read_before, read_after, peek_after):
    removed, created = Counter(), Counter()
    if check.preserve_rename:
        removed.update((PurePosixPath(path).parent, before[path]) for path in paths if path not in after)
        created.update((PurePosixPath(path).parent, after[path]) for path in paths if path not in before)
    added, gaps = [], []
    for path in paths:
        if path not in after:
            continue
        location = (PurePosixPath(path).parent, after[path])
        if path not in before and removed[location] == created[location] == 1:
            continue  # Exact one-to-one rename in the same directory.
        try:
            if not check.conventional(PurePosixPath(path)) and not check.marker(peek_after(path, after[path])):
                continue  # Not this kind of file, at any size or encoding.
            current = check.read_targets(read_after(path, after[path]).removeprefix('\ufeff'))
            previous = check.read_targets(read_before(path, before[path]).removeprefix('\ufeff')) if path in before else ()
            # Occurrences count: a second declaration with an existing identity is still an addition.
            added.extend((path, identity) for identity in sorted(Counter(current) - Counter(previous)))
        except (OSError, ValueError, subprocess.SubprocessError, SyntaxError, RecursionError):
            gaps.append(check.label + ' comparison unavailable: ' + path)
    return added, gaps


def _enabled_checks(root, baseline):
    enabled = enabled_addition_guards(root)
    return [check for check in CHECKS if baseline.get(check.setting) or check.setting in enabled]


def _addition_reason(check, path, identity):
    return f'{check.label} requires owner approval: {path} ({check.display(identity)})'


def candidate_blockers(tasks, task, stage_id, plan, edits):
    """Inspect an authenticated batch against its current, checked preimages."""
    paths = [entry['path'] for entry in plan['edits'] if entry['before_sha256'] != entry['after_sha256']]
    matches = [(check, [path for path in paths if check.matches(PurePosixPath(path))]) for check in CHECKS]
    if not any(paths for _, paths in matches):
        return [], []
    root = tasks.store.repo
    baseline = read_record(tasks.store.db, 'baseline', task['task_id'])['payload']
    if baseline is None:
        from ..tasks import matching_approval_baseline
        baseline = matching_approval_baseline(tasks.store, task['task_id'], task['contract'])
    checks = _enabled_checks(root, baseline or {})
    selected = [(check, paths) for check, paths in matches if paths and check in checks]
    if not selected:
        return [], []

    before = {entry['path']: entry['before_sha256'] for entry in plan['edits'] if entry['before_sha256'] is not None}
    after = {entry['path']: entry['after_sha256'] for entry in plan['edits'] if entry['after_sha256'] is not None}
    contents = {entry['path']: edit['content'] for entry, edit in zip(plan['edits'], edits)}

    def read_after(path, _expected):
        content = contents[path]
        if len(content.encode('utf-8')) > PACKET_FILE_LIMIT:
            raise ValueError('Candidate exceeds comparison limit')
        return content

    gaps, additions = [], []
    for check, paths in selected:
        found, missing = _compare(check, before, after, sorted(paths),
            lambda path, expected: _current_text(root, path, expected), read_after, lambda path, _expected: contents[path])
        gaps.extend(missing)
        additions.extend((check, path, identity) for path, identity in found)
    if not additions:
        return gaps, []

    lineage = tasks.approved_lineage(task['task_id'], stage_id, check_sources=False)
    authorized = set(_lineage_targets(tasks, lineage))
    targets = []
    for check, path, identity in additions:
        if (check.field, path, identity) in authorized:
            continue
        gaps.append(_addition_reason(check, path, identity))
        names = {key: value for key, value in zip(check.identity_fields, identity) if value}
        targets.append({check.field: {'path': path, **names}})
    return gaps, targets


def approval_blockers(tasks, task, stage_id, baseline, source, changed_paths):
    """Compare changed files with the verified task baseline and saved approval."""
    root = tasks.store.repo
    if baseline is None:
        from ..tasks import matching_approval_baseline
        baseline = matching_approval_baseline(tasks.store, task['task_id'], task['contract'])
        if baseline is None:
            return []
        changed_paths = [path for path in baseline['files'].keys() | source['files'].keys()
                         if baseline['files'].get(path) != source['files'].get(path)]
    checks = _enabled_checks(root, baseline)
    if not checks:
        return []

    before, after = baseline['files'], source['files']
    paths = sorted(set(changed_paths) - set(source.get('dependency_paths', [])))
    blobs = _BaselineBlobs(root, baseline)
    gaps, additions = [], []
    for check in checks:
        selected = [path for path in paths if check.matches(PurePosixPath(path))]
        found, missing = _compare(check, before, after, selected,
            lambda path, expected: blobs.blob(path, expected.removesuffix(':x'))['content'],
            lambda path, expected: _current_text(root, path, expected), lambda path, expected: _whole_text(root, path, expected))
        gaps.extend(missing)
        additions.extend((check, path, identity) for path, identity in found)
    approved = _approved_targets(tasks, task, stage_id) if additions else set()
    return [*gaps, *(_addition_reason(check, path, identity)
                     for check, path, identity in additions if (check.field, path, identity) not in approved)]

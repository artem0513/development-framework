"""Bugfix-specific evidence contracts; semantic judgment belongs to reviewers."""
import copy
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
import subprocess

from ..profiles import COMMAND_KINDS, stage_evidence_specs, stage_profile
from ..native_paths import logical_path, state_io
from ..state import hash_value, identifier, read_record, text


POLICY={'version':1,'required':True}
FIELDS={'stage_id','symptom','expected','first_divergence','cause_status','trace',
        'causal_evidence','fix_rationale','alternatives','regression_scenario_ids'}


BASELINE_LOG_BYTES = 131072


def baseline_check_binding(store, task):
    task_id = task["task_id"]
    baseline = (read_record(store.db, "baseline", task_id)["payload"]
                or read_record(store.db, "approval_baseline", task_id)["payload"])
    if not baseline or "files" not in baseline:
        return None, None
    return baseline, {
        "contract_hash": task["contract_hash"],
        "proposal_hash": task["proposal_hash"],
        "workspace_id": store.workspace_id,
        "baseline_files_sha256": hash_value(baseline["files"]),
    }


def baseline_log(store, task_id, result, *, limit=BASELINE_LOG_BYTES):
    log = logical_path(result["log"])
    if not log.is_relative_to((store.root / "tasks" / task_id / "checks").resolve()):
        raise ValueError("Baseline log is outside the task check directory")
    with state_io(log).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Baseline log exceeds the remaining packet budget")
    if hashlib.sha256(raw).hexdigest() != result["log_sha256"]:
        raise ValueError("Baseline log differs from its recorded hash")
    return raw


def baseline_test_manifest(spec, snapshot, executed_sources):
    from ..ci_check import TEST_PATH
    from ..recovery import _test_manifest
    # The progress declaration permits specific pre-fix edits. It cannot exclude
    # other executable test files from the identity of the original run.
    sources = set(spec.get("progress_test_sources", [])) | set(executed_sources)
    sources.update(path for path in snapshot["files"] if TEST_PATH.search(path))
    return _test_manifest(sorted(sources), snapshot)


def required(contract,stage_id=None):
    stages=contract.get('stages',[])
    selected=[s for s in stages if s['id']==stage_id] if stage_id is not None else stages
    if stage_id is not None and not selected:
        raise ValueError('Unknown bugfix evidence stage')
    if 'bugfix_evidence' not in contract:
        return False
    policy=contract['bugfix_evidence']
    if (not isinstance(policy,dict) or set(policy)!=set(POLICY)
            or type(policy.get('version')) is not int or policy['version']!=1
            or policy.get('required') is not True):
        raise ValueError('Invalid bugfix_evidence policy')
    # Coordination/delivery phases with no acceptance obligations cannot map a
    # required regression; they are not a distinct causal repair boundary.
    return any(stage_profile(contract,s['id'])=='software' and s.get('acceptance_ids') for s in selected)


def validate_contract(contract,checks=None):
    """Fail at create when no command check of a bugfix stage can prove per-case before/after outcomes."""
    if not required(contract):
        return
    from .adequacy import CASE_PROOF_REQUIRED, quiet_unittest
    for stage in contract['stages']:
        if not required(contract,stage['id']):
            continue
        commands=[spec for spec in stage_evidence_specs(contract,contract['evidence'] if checks is None else checks,stage['id'])
                  if spec.get('kind') in COMMAND_KINDS]
        quiet=[spec for spec in commands if quiet_unittest(spec['argv'])]
        # A quiet suite is fine beside a verbose unittest/pytest check; lint or build commands prove no test case.
        if quiet and not any(not quiet_unittest(spec['argv']) and _per_case_runner(spec['argv']) for spec in commands):
            raise ValueError('Bugfix stage '+stage['id']+' check '+quiet[0]['id']+' cannot show the regression test '
                'failing before and passing after ('+CASE_PROOF_REQUIRED+'): add -v to its unittest argv, '
                'or add a separate verbose unittest/pytest check for this stage')


def _per_case_runner(argv):
    """The launched program or `-m` module is unittest/pytest (pytest verbosity may come from its config)."""
    names = {'unittest', 'pytest', 'py.test'}
    program = PureWindowsPath(str(argv[0])).name.lower() if argv else ''
    if program.removesuffix('.exe') in names:
        return True
    modules = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == '-m']
    modules += [a[2:] for a in argv if isinstance(a, str) and a.startswith('-m') and len(a) > 2]
    return any(str(m).lower() in names for m in modules)


def _source_path(path):
    text(path,'bugfix trace path',limit=2000)
    if ('\\' in path or PureWindowsPath(path).drive or PurePosixPath(path).is_absolute()
            or '..' in PurePosixPath(path).parts or path.startswith('_ase_')
            or PurePosixPath(path).as_posix()!=path or path=='.'):
        raise ValueError('Bugfix trace requires a captured relative source path')


def validate_dossiers(payload,contract=None,checks=None):
    rows=payload.get('bugfixes',[])
    if not isinstance(rows,list) or len(rows)>100:
        raise ValueError('Bugfix investigations require a bounded list')
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!=FIELDS:
            raise ValueError('Invalid bugfix investigation fields')
        stage=identifier(row['stage_id'],'bugfix stage')
        if stage in seen:
            raise ValueError('Duplicate bugfix stage')
        seen.add(stage)
        if not isinstance(row['alternatives'],str):
            raise ValueError('bugfix alternatives must be one string summarising the rejected alternatives, got '
                +type(row['alternatives']).__name__+' (proposal alternatives, by contrast, are a list of option/why_not objects)')
        for field in ('symptom','expected','first_divergence','causal_evidence','fix_rationale','alternatives'):
            text(row[field],'bugfix '+field,limit=8000)
        if row['cause_status'] not in ('CONFIRMED','HYPOTHESIS','WORKAROUND'):
            raise ValueError('Invalid bugfix cause status')
        trace=row['trace']
        if not isinstance(trace,list) or not 1<=len(trace)<=30:
            raise ValueError('Bugfix requires a bounded caller/contract trace')
        for ref in trace:
            if not isinstance(ref,dict) or set(ref) not in ({'path','locator','observation'},{'path','locator','observation','version'}):
                raise ValueError('Invalid bugfix source trace')
            if ref.get('version','current') not in ('current','before'):
                raise ValueError('Bugfix trace version must be current or before')
            _source_path(ref['path'])
            for field in ('locator','observation'):
                text(ref[field],'bugfix '+field,limit=8000)
        ids=row['regression_scenario_ids']
        if (not isinstance(ids,list) or not 1<=len(ids)<=100
                or any(not isinstance(value,str) for value in ids) or len(ids)!=len(set(ids))):
            raise ValueError('Bugfix requires unique regression scenarios')
        for value in ids:identifier(value,'bugfix scenario')
    if contract is not None:
        enabled=required(contract)
        expected={s['id'] for s in contract['stages'] if enabled and required(contract,s['id'])}
        if seen!=expected:
            raise ValueError('Bugfix investigations must cover exactly the configured software stages')
        scenarios={s['id']:s for s in payload['scenarios']}
        from .adequacy import _compatible_stages
        for row in rows:
            for name in row['regression_scenario_ids']:
                scenario=scenarios.get(name)
                if (scenario is None or not scenario['required']
                        or row['stage_id'] not in _compatible_stages(contract,checks,scenario)):
                    raise ValueError('Bugfix regression needs a required compatible scenario: '+name)
    return rows


def _locator_message(path,version,locator):
    return ('Bugfix trace locator must be literal text present in '+path+' ('+version+' version); '
        'not found: '+repr(locator)+'. Quote an exact source excerpt, not a qualified name or description')


def _trace_source(store,task_id,path,version):
    """Text verification will search for this trace, or None when it cannot be resolved yet."""
    from ..source_coverage import source_bytes
    try:
        current=source_bytes(store.repo,path)
    except (OSError,ValueError):
        current=None
    if version=='before':
        # Same bytes as the packet's _ase_before image: content hashing to the recorded baseline.
        baseline=(read_record(store.db,'baseline',task_id)['payload']
            or read_record(store.db,'approval_baseline',task_id)['payload'])
        recorded=(baseline or {}).get('files',{}).get(path)
        if not isinstance(recorded,str):
            return None
        expected=recorded.removesuffix(':x').rsplit(':',1)[-1]
        if current is None or hashlib.sha256(current).hexdigest()!=expected:
            from ..evidence.packets import _baseline_blob
            try:
                return _baseline_blob(store.repo,baseline,path,expected)['content']
            except (ValueError,OSError,UnicodeError,subprocess.SubprocessError):
                return None
    if current is None:
        return None
    try:
        return current.decode('utf-8')
    except UnicodeError:
        return None


def validate_trace_locators(store,task_id,rows):
    """Reject a locator that is not literal text of the resolvable source verification will search."""
    for row in rows:
        for ref in row['trace']:
            version=ref.get('version','current')
            source=_trace_source(store,task_id,ref['path'],version)
            if source is not None and ref['locator'] not in source:
                raise ValueError(_locator_message(ref['path'],version,ref['locator']))


def assessment_schema(base,contract,stage_id=None,*,plan=None):
    if not required(contract,stage_id):
        return base
    from .adequacy import _object, _ASSESSMENT, _STRING
    result=copy.deepcopy(base)
    result['properties']['bugfixes']={'type':'array','maxItems':100,'items':_object({
        'stage_id':_STRING,**{field:_object(copy.deepcopy(_ASSESSMENT)) for field in ('cause','necessity','regression')}})}
    active=[stage['id'] for stage in contract['stages'] if (stage_id is None or stage['id']==stage_id) and required(contract,stage['id'])]
    row=result['properties']['bugfixes']['items']
    row['properties']['stage_id']={**_STRING,'enum':active}
    if plan is not None:
        scenarios={item['id']:item for item in plan['scenarios']}
        checks=sorted({check for dossier in plan['bugfixes'] if dossier['stage_id'] in active
            for name in dossier['regression_scenario_ids'] for check in scenarios[name]['evidence_ids']})
        if not checks:raise ValueError('Bugfix schema needs mapped regression check IDs')
        for field in ('cause','necessity','regression'):
            row['properties'][field]['properties']['evidence']['items']['properties']['check_id']={**_STRING,'enum':checks}
    result['required'].append('bugfixes')
    return result


def repair_targets(contract,stage_id):
    """Map an editing phase to its nearest related causal acceptance boundary."""
    if not required(contract):
        return set()
    by_id={row['id']:row for row in contract['stages']}
    if stage_id not in by_id:
        raise ValueError('Unknown bugfix repair stage')
    stage=by_id[stage_id]
    if required(contract,stage_id):
        return {stage_id}
    scope=stage.get('scope',contract.get('scope',{}))
    if (stage_profile(contract,stage_id)!='software'
            or ('file_write' not in scope.get('effects',[]) and not stage.get('actions'))):
        return set()
    def boundaries(forward):
        seen={stage_id};pending=[stage_id];found=set()
        while pending:
            current=pending.pop()
            neighbors=([row['id'] for row in by_id.values() if current in row.get('depends_on',[])]
                       if forward else by_id[current].get('depends_on',[]))
            for name in neighbors:
                if name in seen:continue
                seen.add(name)
                if required(contract,name):found.add(name)
                else:pending.append(name)
        return found
    targets=boundaries(True) or boundaries(False)
    if not targets:
        raise ValueError('Mutating bugfix stage needs a related causal acceptance boundary; refine its technical stage mapping')
    return targets


def repair_readiness(store,task_id,stage_id):
    """Before a fresh causal-repair candidate; never a recovery/diagnostic fence."""
    from .adequacy import TestAdequacy, _validate_plan
    from ..tasks import TaskService
    tasks=TaskService(store);task=tasks.snapshot(task_id)
    if not required(task['contract']):
        return {'ready':True}
    reason=None
    try:
        targets=repair_targets(task['contract'],stage_id)
        if not targets:
            return {'ready':True}
        plan=TestAdequacy(store).snapshot(task_id)['payload']
        if not plan:
            reason='Save the bugfix investigation before proposing a causal repair'
        else:
            execution=tasks.execution_plan(task_id)
            expected={'contract_hash':task['contract_hash'],'proposal_hash':task['proposal_hash'],
                'execution_plan_hash':execution['plan_hash']}
            if any(plan[key]!=value for key,value in expected.items()):
                reason='Refresh the stale bugfix investigation before proposing a causal repair'
            else:
                _validate_plan(task['contract'],execution['checks'],{key:plan[key] for key in ('scenarios','not_applicable','bugfixes') if key in plan})
                if any(row['cause_status']!='CONFIRMED' for row in plan['bugfixes'] if row['stage_id'] in targets):
                    reason='Investigate the unconfirmed cause before proposing a causal repair'
    except (ValueError,KeyError,TypeError):
        reason='Refine the invalid bugfix investigation or technical stage mapping before proposing a causal repair'
    if reason:
        return {'ready':False,'reason':reason,'needs_owner_input':False,
            'next_action':'Continue authorized investigation with useful safe probes or reversible diagnostic tests; save current causal evidence and rationale. Ask only if a material owner-only gap remains.'}
    return {'ready':True}


def assess(contract,plan,result,stage_id,files,scenarios,specs,profile,assessment,reasons,rejected,*,captured_stage=None):
    if not required(contract,stage_id):
        return
    active={row['stage_id']:row for row in plan['bugfixes'] if stage_id is None or row['stage_id']==stage_id}
    rows=result.get('bugfixes',[])
    ids=[row['stage_id'] for row in rows]
    captured={row['stage_id'] for row in plan['bugfixes'] if captured_stage is None or row['stage_id']==captured_stage}
    if len(ids)!=len(set(ids)) or set(ids)!=captured:
        reasons.append('Independent bugfix judgments must cover exactly the active stages')
        return
    for row in rows:
        if row['stage_id'] not in active:
            continue
        dossier=active[row['stage_id']]
        label='bugfix '+row['stage_id']
        if dossier['cause_status']!='CONFIRMED':
            reasons.append('Bugfix cause is not confirmed: '+row['stage_id'])
        for ref in dossier['trace']:
            # Before-images are supplied only by the host's authenticated source
            # capture. Never search arbitrary files or silently change versions.
            version=ref.get('version','current')
            path=('_ase_before/' if version=='before' else '')+ref['path']
            source=files.get(path)
            if source is None:
                reasons.append('Bugfix caller/contract trace unavailable: '+ref['path']+' ('+version
                    +' version) is not in the captured source packet as '+path)
            elif ref['locator'] not in source['content']:
                reasons.append('Bugfix caller/contract trace unavailable: '+_locator_message(ref['path'],version,ref['locator']))
        allowed={check for name in dossier['regression_scenario_ids'] for check in scenarios[name]['evidence_ids']}
        for field in ('cause','necessity','regression'):
            assessment(row[field],label+'/'+field,allowed,specs,profile,files,reasons,rejected)

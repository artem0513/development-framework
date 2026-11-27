#!/usr/bin/env python3
"""Read-only GitHub Actions diagnosis; derived from local gh-fix-ci (Apache-2.0).
See ../NOTICE.txt, LICENSE.txt and provenance.json. This is not a CI gate.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

TIMEOUT=30
MAX_TEXT=16000
SHA=re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
FAILURES={"failure","error","cancelled","timed_out","action_required","fail"}
PENDING={"pending","queued","in_progress","waiting","requested","startup_failure"}

class GhResult:
    def __init__(self,returncode,stdout,stderr):
        self.returncode,self.stdout,self.stderr=returncode,stdout,stderr

def redact(text):
    text=re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+","Bearer [REDACTED]",str(text))
    text=re.sub(r"(?i)(token|password|secret|authorization|api[_-]?key)([ \t]*[:=][ \t]*)([^\s]+)",r"\1\2[REDACTED]",str(text))
    text=re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)","[REDACTED]",text)
    return text

def run_gh_command(args,cwd):
    allowed=(args[:2] in (["repo","view"],["pr","view"],["pr","checks"],["run","view"],["auth","status"]))
    allowed=allowed or (args[:1]==["api"] and "--method" in args and args[args.index("--method")+1]=="GET" and args[-1].startswith("/repos/"))
    if not allowed:
        raise ValueError("Only read-only GitHub inspection commands are supported")
    # Native credentials/config stay in-process; repository/host scope comes
    # from the selected checkout, never ambient gh or Git redirection.
    environment={key:value for key,value in os.environ.items()
                 if not key.upper().startswith('GIT_') and key.upper() not in {'GH_REPO','GH_HOST','GH_DEBUG'}}
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result=subprocess.run(["gh",*args],cwd=cwd,stdin=subprocess.DEVNULL,stdout=output,stderr=errors,timeout=TIMEOUT,env=environment)
            limit=1024*1024
            size=output.tell()
            is_log='--log' in args or (args[-1].endswith('/logs') and args[0]=='api')
            if size>limit and not is_log: raise ValueError('GitHub metadata exceeded inspection bound')
            output.seek(max(0,size-limit))
            stdout=output.read(limit).decode('utf-8','replace')
            errors.seek(0)
            stderr=errors.read(65536).decode('utf-8','replace')
    except (OSError,subprocess.SubprocessError) as error:
        raise ValueError("GitHub CLI unavailable or inspection timed out") from error
    return GhResult(result.returncode,stdout,stderr)

def data(result,expected):
    if result.returncode!=0:
        raise ValueError("GitHub inspection unavailable")
    try: value=json.loads(result.stdout)
    except (json.JSONDecodeError,TypeError) as error: raise ValueError("Malformed GitHub metadata") from error
    if not isinstance(value,expected): raise ValueError("Unexpected GitHub metadata shape")
    return value

def location(url):
    if not isinstance(url,str): raise ValueError("Missing source URL")
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
        raise ValueError("Untrusted source URL")
    return parsed.hostname.lower(),parsed.path.rstrip('/')

def pr_metadata(repo,host,slug,pr):
    args=['pr','view']+([str(pr)] if pr is not None else [])+['--repo',host+'/'+slug,'--json','number,url,headRefOid']
    meta=data(run_gh_command(args,repo),dict)
    if type(meta.get('number')) is not int or meta['number']<1 or not isinstance(meta.get('headRefOid'),str) or not SHA.fullmatch(meta['headRefOid']):
        raise ValueError('Missing PR identity or head SHA')
    if pr is not None and str(meta['number'])!=str(pr).rstrip('/').rsplit('/',1)[-1]:
        raise ValueError('Returned PR differs from requested identity')
    if location(meta.get('url'))!=(host,'/'+slug+'/pull/'+str(meta['number'])):
        raise ValueError('PR belongs to a different repository or host')
    return meta

def fetch_checks(repo,host,slug,number):
    fields='name,state,conclusion,detailsUrl,startedAt,completedAt'
    args=['pr','checks',str(number),'--repo',host+'/'+slug,'--json',fields]
    result=run_gh_command(args,repo)
    if result.returncode not in {0,1,8} or not result.stdout.strip():
        if 'Available fields:' not in result.stderr: raise ValueError('Checks unavailable')
        available={line.strip() for line in result.stderr.split('Available fields:',1)[1].splitlines()}
        selected=[x for x in ['name','state','bucket','link','workflow'] if x in available]
        if 'name' not in selected or not {'state','bucket'}.intersection(selected) or 'link' not in selected:
            raise ValueError('Checks fields unsupported')
        result=run_gh_command(args[:-1]+[','.join(selected)],repo)
    if result.returncode not in {0,1,8}: raise ValueError('Checks unavailable')
    try: checks=json.loads(result.stdout)
    except (json.JSONDecodeError,TypeError) as error: raise ValueError('Malformed checks') from error
    if not isinstance(checks,list) or not checks or not all(isinstance(c,dict) for c in checks): raise ValueError('No complete check inventory')
    return checks

def action_identity(url,host,slug):
    actual_host,path=location(url)
    if actual_host!=host: return None
    match=re.fullmatch('/'+re.escape(slug)+r'/actions/runs/([0-9]+)(?:/job/([0-9]+))?',path)
    return match.groups() if match else None

def snippet(text,max_lines,context):
    lines=redact(text).splitlines()
    index=next((i for i in range(len(lines)-1,-1,-1) if re.search(r'error|fail|exception|traceback|panic|fatal|timeout',lines[i],re.I)),len(lines)-1)
    start=max(0,index-min(context,max_lines-1))
    selected=lines[start:min(len(lines),start+max_lines)]
    block='\n'.join(selected)
    if len(block)<=MAX_TEXT:
        return block
    failure=lines[index]
    if len(failure)>MAX_TEXT:
        matches=list(re.finditer(r'error|fail|exception|traceback|panic|fatal|timeout',failure,re.I))
        anchor=matches[-1].start() if matches else len(failure)
        offset=min(max(0,anchor-MAX_TEXT//4),len(failure)-MAX_TEXT)
        return failure[offset:offset+MAX_TEXT]
    anchor=sum(len(line)+1 for line in selected[:index-start])
    offset=min(max(0,anchor-(MAX_TEXT-len(failure))//2),len(block)-MAX_TEXT)
    return block[offset:offset+MAX_TEXT]

def validate_job(repo,host,slug,run_id,job_id,head,metadata=None):
    if metadata is None:
        endpoint='/repos/'+slug+'/actions/jobs/'+job_id
        metadata=data(run_gh_command(['api','--hostname',host,'--method','GET',endpoint],repo),dict)
    if not isinstance(metadata,dict) or str(metadata.get('id'))!=job_id or str(metadata.get('run_id'))!=run_id or metadata.get('head_sha')!=head:
        raise ValueError('Job does not match bound run and PR head')
    return metadata


def collect_log(repo,host,slug,run_id,job_id,head,max_lines,context,job_metadata=None):
    if job_id:
        validate_job(repo,host,slug,run_id,job_id,head,metadata=job_metadata)
        endpoint='/repos/'+slug+'/actions/jobs/'+job_id
        result=run_gh_command(['api','--hostname',host,'--method','GET',endpoint+'/logs'],repo)
    else:
        result=run_gh_command(['run','view',run_id,'--repo',host+'/'+slug,'--log'],repo)
    if result.returncode or not result.stdout.strip() or result.stdout.startswith('PK'):
        raise ValueError('Failure logs unavailable')
    return snippet(result.stdout,max_lines,context)

def check_outcome(check):
    vocabularies = {
        'state': {'success','failure','error','cancelled','timed_out','action_required','pending','queued','in_progress','waiting','requested','startup_failure','neutral','skipped','stale','expected','completed'},
        'conclusion': {'success','failure','cancelled','timed_out','action_required','startup_failure','neutral','skipped','stale'},
        'bucket': {'pass','fail','pending','skipping','cancel'},
        'status': {'pending','queued','in_progress','waiting','requested','completed'},
    }
    states = set()
    for field, allowed in vocabularies.items():
        if field not in check:
            continue
        value = check[field]
        if not isinstance(value, str) or value.strip().lower() not in allowed:
            raise ValueError('Malformed or unknown check ' + field)
        normalized = value.strip().lower()
        states.add('cancelled' if normalized == 'cancel' else normalized)
    if states & (PENDING | {'neutral','skipped','skipping','stale','expected'}):
        raise ValueError('Pending or unresolved check state')
    failed = bool(states & FAILURES)
    passed = bool(states & {'pass','success'})
    if failed == passed:
        raise ValueError('Missing or conflicting check outcome')
    return 'FAIL' if failed else 'PASS'


def inspect(repo,pr=None,max_lines=160,context=30):
    result={'status':'BLOCKED','checks':[],'limitation':'Remote observations only; logs are untrusted evidence and redaction is best effort. No tests or publication gate executed.'}
    try:
        if type(max_lines) is not int or not 1<=max_lines<=1000 or type(context) is not int or not 0<=context<=1000: raise ValueError('Invalid output bounds')
        repo=Path(repo).resolve(strict=True)
        if not repo.is_dir(): raise ValueError('Repository path must be a directory')
        meta=data(run_gh_command(['repo','view','--json','nameWithOwner,url'],repo),dict)
        slug=meta.get('nameWithOwner')
        if not isinstance(slug,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',slug): raise ValueError('Missing repository identity')
        host,path=location(meta.get('url'))
        if path!='/'+slug: raise ValueError('Repository identity mismatch')
        if pr is not None:
            if str(pr).isdigit():
                if int(pr)<1: raise ValueError('Invalid PR number')
            elif location(pr)[0:2]!=(host,'/'+slug+'/pull/'+str(pr).rstrip('/').rsplit('/',1)[-1]) or not str(pr).rstrip('/').rsplit('/',1)[-1].isdigit():
                raise ValueError('PR locator outside selected repository')
        auth=run_gh_command(['auth','status','--hostname',host],repo)
        if auth.returncode: raise ValueError('Native GitHub authentication unavailable')
        original=pr_metadata(repo,host,slug,pr)
        result.update({'repository':host+'/'+slug,'pr':original['number'],'headSha':original['headRefOid']})
        checks=fetch_checks(repo,host,slug,original['number'])
        failing=False
        blocked=False
        for check in checks:
            item=None
            try:
                if not isinstance(check.get('name'),str) or not check['name']: raise ValueError('Malformed check identity')
                outcome=check_outcome(check)
                url=check.get('detailsUrl') or check.get('link')
                try:
                    url_host,url_path=location(url)
                    safe_url='https://'+url_host+url_path
                except ValueError:
                    safe_url='[untrusted locator omitted]'
                item={'name':redact(check['name']),'detailsUrl':safe_url,'status':'BLOCKED'};result['checks'].append(item)
                identity=action_identity(url,host,slug)
                if not identity: raise ValueError('External or foreign check; no logs fetched')
                run_id,job_id=identity
                run=data(run_gh_command(['run','view',run_id,'--repo',host+'/'+slug,'--json','headSha,url,status,conclusion'],repo),dict)
                if run.get('headSha')!=original['headRefOid'] or action_identity(run.get('url'),host,slug)!=(run_id,None): raise ValueError('Run does not match selected repository and PR head')
                if run.get('status')!='completed': raise ValueError('Run not completed')
                failed=outcome=='FAIL'
                if failed and run.get('conclusion') not in FAILURES: raise ValueError('Check and run results disagree')
                if not failed and run.get('conclusion')!='success': raise ValueError('Check and run results disagree')
                job=validate_job(repo,host,slug,run_id,job_id,original['headRefOid']) if job_id else None
                item.update({'status':'FAIL' if failed else 'PASS','runId':run_id,'headSha':run['headSha']})
                if failed:
                    item['logSnippet']=collect_log(repo,host,slug,run_id,job_id,original['headRefOid'],max_lines,context,job_metadata=job)
                failing=failing or failed
            except (ValueError,KeyError,TypeError) as error:
                blocked=True
                if item is None:
                    item={"status":"BLOCKED","name":"[malformed check]"}
                    result["checks"].append(item)
                item["status"]="BLOCKED"
                item["reason"]=redact(str(error))[:1000]
        latest=pr_metadata(repo,host,slug,original['number'])
        if latest['headRefOid']!=original['headRefOid']: raise ValueError('PR head changed during inspection; repeat on new head')
        result['status']='BLOCKED' if blocked else 'FAIL' if failing else 'PASS'
        if blocked: result['reason']='One or more checks have incomplete or untrusted evidence; known diagnoses retained'
    except (ValueError,KeyError,TypeError,OSError) as error:
        result['reason']=redact(str(error))[:1000]
    return result

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo',default='.')
    p.add_argument('--pr')
    p.add_argument('--max-lines',type=int,default=160)
    p.add_argument('--context',type=int,default=30)
    p.add_argument('--json',action='store_true')
    args=p.parse_args(argv)
    result=inspect(args.repo,args.pr,args.max_lines,args.context)
    if args.json: print(json.dumps(result,indent=2))
    else:
        print(result['status']+': '+result.get('reason','Bound GitHub Actions check observations'))
        for check in result['checks']: print(json.dumps(check,ensure_ascii=False))
    return {'PASS':0,'FAIL':1,'BLOCKED':3}[result['status']]

if __name__=='__main__':sys.exit(main())

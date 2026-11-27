"""Deterministic gh subprocess fixtures, not live GitHub acceptance."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[1]/'skills/gh-fix-ci/scripts/inspect_pr_checks.py'
spec=importlib.util.spec_from_file_location('ci_inspector',PATH)
ci=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
SHA='a'*40
URL='https://github.example/acme/app/actions/runs/10/job/20'

class InspectorTests(unittest.TestCase):
    def run_inspect(self,checks,*,sha=SHA,head_after=SHA,run_url=None,log_error=False,job_sha=SHA,malformed=False,fallback=False,pr=None,job_id=20,job_run=10,job_unavailable=False,job_data=None):
        calls=[]
        def fake(args,cwd):
            calls.append(args)
            if args[:2]==['repo','view']: data={'nameWithOwner':'acme/app','url':'https://github.example/acme/app'}
            elif args[:2]==['pr','view']: data={'number':7,'url':'https://github.example/acme/app/pull/7','headRefOid':head_after if sum(x[:2]==['pr','view'] for x in calls)>1 else SHA}
            elif args[:2]==['auth','status']: return ci.GhResult(0,'','')
            elif args[:2]==['pr','checks']:
                if fallback and sum(x[:2]==['pr','checks'] for x in calls)==1: return ci.GhResult(1,'','Unknown JSON field: detailsUrl\nAvailable fields:\n  name\n  state\n  bucket\n  link\n')
                return ci.GhResult(1 if checks else 0,'broken' if malformed else json.dumps(checks),'')
            elif args[:2]==['run','view']:
                if '--log' in args:
                    return ci.GhResult(1,'','log will be available when it is complete') if log_error else ci.GhResult(0,'setup\nERROR token=secret error detail\nfinish','')
                data={'headSha':sha,'url':run_url or 'https://github.example/acme/app/actions/runs/10','status':'completed','conclusion':'failure' if any(c.get('bucket')=='fail' for c in checks) else 'success'}
            elif args[0]=='api':
                if args[-1].endswith('/logs'): return ci.GhResult(0,'ERROR token=secret job detail','')
                if job_unavailable:return ci.GhResult(1,'','not found')
                data=job_data if job_data is not None else {'id':job_id,'run_id':job_run,'head_sha':job_sha}
            else: raise AssertionError(args)
            return ci.GhResult(0,json.dumps(data),'')
        with patch.object(ci,'run_gh_command',side_effect=fake): result=ci.inspect(Path.cwd(),pr=pr,max_lines=3,context=1)
        return result,calls

    def check(self,bucket='fail',url=URL): return {'name':'unit','state':'FAILURE' if bucket=='fail' else 'SUCCESS','bucket':bucket,'link':url}

    def test_failure_json_exit_one_fetches_bound_failure_log_and_redacts_secret(self):
        result,calls=self.run_inspect([self.check()]);self.assertEqual(result['status'],'FAIL');self.assertIn('ERROR',result['checks'][0]['logSnippet']);self.assertNotIn('secret',json.dumps(result));self.assertTrue(any(c[0]=='api' and c[-1].endswith('/jobs/20/logs') for c in calls))

    def test_multiple_jobs_fetch_only_the_selected_validated_job_log(self):
        calls=[]
        def fake(args,cwd):
            calls.append(args)
            if args[:2]==['run','view']:
                return ci.GhResult(0,'ERROR unrelated last job failure','')
            job_id=args[-1].split('/jobs/')[1].split('/')[0]
            if args[-1].endswith('/logs'):
                return ci.GhResult(0,'ERROR selected job '+job_id,'')
            return ci.GhResult(0,json.dumps({'id':int(job_id),'run_id':10,'head_sha':SHA}),'')
        with patch.object(ci,'run_gh_command',side_effect=fake):
            for job in ['20','21']:
                text=ci.collect_log(Path.cwd(),'github.example','acme/app','10',job,SHA,3,1)
                self.assertEqual(text,'ERROR selected job '+job)
        self.assertFalse(any(args[:2]==['run','view'] for args in calls))
        self.assertEqual([args[-1] for args in calls],[
            '/repos/acme/app/actions/jobs/20','/repos/acme/app/actions/jobs/20/logs',
            '/repos/acme/app/actions/jobs/21','/repos/acme/app/actions/jobs/21/logs'])

    def test_success_job_requires_existing_matching_metadata_before_pass(self):
        cases=[{'job_id':999}, {'job_run':999}, {'job_sha':'b'*40}, {'job_unavailable':True}, {'job_data':[]}, {'job_data':{}}, {'job_data':{'id':20,'run_id':10}}]
        for options in cases:
            with self.subTest(options=options):
                result,calls=self.run_inspect([self.check('pass')],**options)
                self.assertEqual(result['status'],'BLOCKED')
                self.assertFalse(any('--log' in args or (args[0]=='api' and args[-1].endswith('/logs')) for args in calls))
        result,calls=self.run_inspect([self.check('pass',url='https://github.example/acme/app/actions/runs/10/job/999999')])
        self.assertEqual(result['status'],'BLOCKED')
        result,calls=self.run_inspect([self.check('pass')])
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(sum(args[0]=='api' and args[-1].endswith('/jobs/20') for args in calls),1)
        self.assertFalse(any('--log' in args or (args[0]=='api' and args[-1].endswith('/logs')) for args in calls))

    def test_failed_job_metadata_is_validated_once_before_its_logs(self):
        result,calls=self.run_inspect([self.check()])
        self.assertEqual(result['status'],'FAIL')
        endpoints=[args[-1] for args in calls if args[0]=='api']
        self.assertEqual(endpoints,['/repos/acme/app/actions/jobs/20','/repos/acme/app/actions/jobs/20/logs'])
        for options in [{'job_id':999},{'job_run':999},{'job_sha':'b'*40},{'job_unavailable':True}]:
            with self.subTest(options=options):
                result,calls=self.run_inspect([self.check()],**options)
                self.assertEqual(result['status'],'BLOCKED')
                self.assertFalse(any(args[0]=='api' and args[-1].endswith('/logs') for args in calls))

    def test_run_only_success_needs_no_job_metadata_or_logs(self):
        result,calls=self.run_inspect([self.check('pass',url='https://github.example/acme/app/actions/runs/10')])
        self.assertEqual(result['status'],'PASS')
        self.assertFalse(any(args[0]=='api' or '--log' in args for args in calls))

    def test_empty_pending_malformed_and_unknown_never_pass(self):
        for checks,kw in [([],{}),([{'name':'pending','state':'PENDING','bucket':'pending','link':URL}],{}),([self.check()],{'malformed':True}),([{'name':'unknown','link':URL}],{})]:
            with self.subTest(checks=checks,kw=kw): self.assertEqual(self.run_inspect(checks,**kw)[0]['status'],'BLOCKED')

    def test_foreign_host_repo_and_external_do_not_fetch(self):
        for url in ['https://evil.test/acme/app/actions/runs/10','https://github.example/other/app/actions/runs/10','https://build.example/jobs/10']:
            with self.subTest(url=url):
                r,c=self.run_inspect([self.check(url=url)]);self.assertEqual(r['status'],'BLOCKED');self.assertFalse(any(x[:2]==['run','view'] or x[0]=='api' for x in c))

    def test_untrusted_locator_credentials_are_not_printed(self):
        for url in ['https://user:private@github.example/acme/app/actions/runs/10','https://github.example/acme/app/actions/runs/10?access_token=private']:
            result,calls=self.run_inspect([self.check(url=url)])
            self.assertEqual(result['status'],'BLOCKED');self.assertNotIn('private',json.dumps(result))

    def test_old_head_or_foreign_metadata_prevents_log_fetch(self):
        for kw in [{'sha':'b'*40},{'run_url':'https://evil.test/acme/app/actions/runs/10'}]:
            r,c=self.run_inspect([self.check()],**kw);self.assertEqual(r['status'],'BLOCKED');self.assertFalse(any('--log' in x for x in c))

    def test_job_identity_is_validated_before_its_logs(self):
        r,c=self.run_inspect([self.check()],log_error=True);self.assertEqual(r['status'],'FAIL');self.assertIn('job detail',r['checks'][0]['logSnippet']);self.assertTrue(any(x[0]=='api' and x[-1].endswith('/jobs/20') for x in c))
        r,c=self.run_inspect([self.check()],log_error=True,job_sha='b'*40);self.assertEqual(r['status'],'BLOCKED');self.assertFalse(any(x[0]=='api' and x[-1].endswith('/logs') for x in c))
        r,c=self.run_inspect([self.check(url='https://github.example/acme/app/actions/runs/10')]);self.assertEqual(r['status'],'FAIL');self.assertTrue(any('--log' in x for x in c));self.assertFalse(any(x[0]=='api' for x in c))

    def test_field_drift_fallback_and_success_binding(self):
        r,c=self.run_inspect([self.check('pass')],fallback=True);self.assertEqual(r['status'],'PASS');self.assertEqual(sum(x[:2]==['pr','checks'] for x in c),2);self.assertFalse(any('--log' in x for x in c))
        self.assertEqual(self.run_inspect([self.check('pass')],sha='b'*40)[0]['status'],'BLOCKED')

    def test_pr_head_change_during_collection_blocks_result(self):
        self.assertEqual(self.run_inspect([self.check()],head_after='b'*40)[0]['status'],'BLOCKED')

    def test_malformed_or_conflicting_status_fields_cannot_hide_behind_pass_bucket(self):
        for field,value in [('state','NOT_A_REAL_STATE'),('state',{'unexpected':'shape'}),('state',''),('conclusion','unknown'),('status',True),('bucket',[]),('state','FAILURE'),('state','NEUTRAL')]:
            check=self.check('pass');check[field]=value
            with self.subTest(field=field,value=value):
                result,calls=self.run_inspect([check])
                self.assertEqual(result['status'],'BLOCKED')
                self.assertFalse(any('--log' in args or (args[0]=='api' and args[-1].endswith('/logs')) for args in calls))

    def test_exact_sha_lengths_and_requested_pr_identity_are_required(self):
        for size in [39,41,63,65]:
            metadata={'number':7,'url':'https://github.example/acme/app/pull/7','headRefOid':'a'*size}
            with patch.object(ci,'run_gh_command',return_value=ci.GhResult(0,json.dumps(metadata),'')):
                with self.assertRaises(ValueError):ci.pr_metadata(Path.cwd(),'github.example','acme/app','7')
        result,calls=self.run_inspect([self.check('pass')],pr='8')
        self.assertEqual(result['status'],'BLOCKED');self.assertFalse(any(x[:2]==['pr','checks'] for x in calls))

    def test_external_first_retains_later_github_failure_diagnosis(self):
        result,calls=self.run_inspect([self.check(url='https://external.example/check'),self.check()])
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['checks'][0]['status'],'BLOCKED')
        self.assertEqual(result['checks'][1]['status'],'FAIL');self.assertIn('ERROR',result['checks'][1]['logSnippet'])

    def test_tiny_snippet_still_contains_failure_when_context_is_large(self):
        self.assertEqual(ci.snippet('prefix\nERROR detail\nsuffix',1,1000),'ERROR detail')

    def test_long_context_or_failure_line_cannot_clip_out_the_matched_error(self):
        for text in ['x'*16001+'\nERROR actual failing test', 'x'*16001+'ERROR actual failing test']:
            result=ci.snippet(text,160,30)
            self.assertIn('ERROR actual failing test',result)
            self.assertLessEqual(len(result),ci.MAX_TEXT)

    def test_bearer_header_tokens_are_redacted(self):
        self.assertNotIn('privatecredential',ci.redact('Authorization: Bearer privatecredential'))

    def test_cli_json_for_blocked_and_pass_never_claims_remote_execution(self):
        import contextlib,io
        for status,code in [('PASS',0),('FAIL',1),('BLOCKED',3)]:
            output=io.StringIO()
            with patch.object(ci,'inspect',return_value={'status':status,'checks':[]}),contextlib.redirect_stdout(output):
                self.assertEqual(ci.main(['--json']),code)
            self.assertEqual(json.loads(output.getvalue())['status'],status)

    def test_subprocess_fixture_keeps_output_bounded_and_metadata_fails_closed(self):
        def fake_run(argv,**options):
            options['stdout'].write(b'x'*(1024*1024+5))
            options['stderr'].write(b'private not printed')
            return subprocess.CompletedProcess(argv,0)
        with patch.object(ci.subprocess,'run',side_effect=fake_run):
            with self.assertRaisesRegex(ValueError,'metadata exceeded'):
                ci.run_gh_command(['repo','view'],Path.cwd())
            result=ci.run_gh_command(['run','view','10','--log'],Path.cwd())
        self.assertEqual(len(result.stdout),1024*1024)

    def test_subprocess_scrubs_ambient_repo_git_overrides_and_retains_native_auth(self):
        def fake_run(argv,**options):return subprocess.CompletedProcess(argv,0)
        with patch.dict(ci.os.environ,{'GH_REPO':'evil/other','GH_HOST':'evil.example','GIT_DIR':'foreign','GIT_CONFIG_COUNT':'1','GH_TOKEN':'privatecredential'}),patch.object(ci.subprocess,'run',side_effect=fake_run) as run:
            result=ci.run_gh_command(['repo','view'],Path.cwd())
            environment=run.call_args.kwargs['env']
            self.assertNotIn('GH_REPO',environment);self.assertNotIn('GH_HOST',environment)
            self.assertFalse(any(k.upper().startswith('GIT_') for k in environment))
            self.assertEqual(environment['GH_TOKEN'],'privatecredential')
            self.assertNotIn('privatecredential',result.stdout+result.stderr)

    def test_subprocess_is_bounded_read_only_and_errors_do_not_leak(self):
        with patch.object(ci.subprocess,'run',side_effect=subprocess.TimeoutExpired(['gh'],30)) as run:
            with self.assertRaises(ValueError): ci.run_gh_command(['repo','view'],Path.cwd())
            self.assertEqual(run.call_args.kwargs['stdin'],subprocess.DEVNULL);self.assertGreater(run.call_args.kwargs['timeout'],0)
        with self.assertRaises(ValueError): ci.run_gh_command(['run','rerun','10'],Path.cwd())

if __name__=='__main__':unittest.main()

"""Public CLI routes generic projects through v2 services and fails closed."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from ase.projects import ProjectRegistry
from ase.gates import source_snapshot
from ase.state import read_record
from ase.store import Store, now
from ase.tasks import TaskService
from test_journal import contract


ROOT = Path(__file__).resolve().parents[1]


class UniversalCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ase-cli-v2-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "documents"
        self.root.mkdir()
        (self.root / "guide.txt").write_text("Operating guide", encoding="utf-8")
        self.home = self.base / "state"
        self.environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        self.sequence = 0

    def payload(self, value):
        self.sequence += 1
        path = self.base / ("input-" + str(self.sequence) + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def cli(self, *arguments, ok=0, selector=None):
        select = ["--project", selector] if selector else ["--repo", str(self.root)]
        run = subprocess.run([sys.executable, "-m", "ase", "--home", str(self.home), *select, *arguments],
                             cwd=ROOT, env=self.environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, ok, run.stdout + run.stderr)
        try:
            result = json.loads(run.stdout)
        except ValueError:
            self.fail("CLI did not return JSON: " + run.stdout + run.stderr)
        self.assertEqual(run.stderr, "")
        return result

    def create(self):
        return self.cli("task", "create", "--file", self.payload(contract("guide")))

    def test_non_git_registration_listing_and_stable_selector(self):
        project = self.cli("project", "register", "--kind", "document", "--timezone", "Europe/Bucharest")
        self.assertEqual(project["root"], str(self.root))
        projects = self.cli("project", "list")["projects"]
        self.assertEqual(projects[0]["project_id"], project["project_id"])
        second = self.base / "second-root"
        second.mkdir()
        attached = self.cli("project", "attach", project["project_id"], "--root", str(second), "--expected-revision", "1")
        self.assertEqual(attached["project_id"], project["project_id"])
        self.assertNotEqual(attached["workspace_id"], project["workspace_id"])
        status = self.cli("status", selector=project["project_id"])
        self.assertEqual(status["project_id"], project["project_id"])

    def test_v2_task_proposal_approval_and_revision(self):
        task = self.create()
        self.assertEqual(task["state"], "DRAFT")
        proposal = self.cli("task", "propose", "guide", "--expected-revision", str(task["revision"]), "--file", self.payload({"summary": "Draft the guide", "stage_ids": ["draft"]}))
        self.cli("task", "approve", "guide", "--file", self.payload({"proposal_hash": "wrong", "user_message_ref": "chat:approval", "approved_stage_ids": ["draft"]}), ok=3)
        approved = self.cli("task", "approve", "guide", "--file", self.payload({"proposal_hash": proposal["proposal_hash"], "user_message_ref": "chat:approval", "approved_stage_ids": ["draft"]}))
        self.assertEqual(approved["state"], "READY")
        changed = contract("guide")
        changed["request"] = "Revise the operating guide for new requirements"
        revised = self.cli("task", "revise", "guide", "--expected-revision", str(approved["revision"]), "--file", self.payload(changed))
        self.assertEqual(revised["approved_stage_ids"], [])
        self.assertIsNone(revised["proposal_hash"])
        status = self.cli("task", "status", "guide")
        self.assertEqual(status["readiness"]["clarity"], "UNASSESSED")
        self.cli("task", "verify", "guide", ok=3)

    def test_discovery_checks_gate_stop_and_unapproved_run(self):
        self.create()
        discovery = self.cli("task", "discover", "guide", "--expected-revision", "0")
        self.assertFalse(discovery["payload"]["authorization_granted"])
        status = self.cli("task", "status", "guide")
        self.assertTrue(status["readiness"]["discovery_present"])
        self.cli("task", "check", "guide", ok=3)
        self.cli("task", "check", "guide", "--stage", "draft", ok=3)
        self.cli("task", "verify", "guide", "--stage", "draft", ok=3)
        self.cli("task", "run", "guide", ok=3)
        proposal = self.cli("task", "propose", "guide", "--expected-revision", "1", "--file", self.payload({"summary": "Draft the guide", "stage_ids": ["draft"]}))
        self.cli("task", "approve", "guide", "--file", self.payload({"proposal_hash": proposal["proposal_hash"], "user_message_ref": "chat:approval", "approved_stage_ids": ["draft"]}))
        checks = self.cli("task", "check", "guide")
        self.assertEqual(checks["results"][0]["sha256"], hashlib.sha256((self.root / "guide.txt").read_bytes()).hexdigest())
        scoped = self.cli("task", "check", "guide", "--stage", "draft")
        self.assertEqual(scoped["results"][0]["sha256"], checks["results"][0]["sha256"])
        self.assertEqual(self.cli("task", "gate", "guide", ok=3)["status"], "BLOCKED")
        self.cli("task", "stop", "guide", "--reason", "Wait for reviewed approval")

    def test_session_closure_reports_and_knowledge(self):
        self.create()
        self.cli("session", "start", "guide", "editing")
        finish = {"finish_reason": "paused", "summary": "Saved guide", "next_action": "Review guide", "context": {"summary": "Guide awaits review"}, "blockers": []}
        closure = self.cli("session", "finish", "guide", "editing", "--closure-key", "close-1", "--expected-revision", "0", "--expected-context-revision", "0", "--file", self.payload(finish))
        self.assertEqual(closure["state"], "PAUSED")
        worklog = self.cli("worklog", "--start", "2026-01-01", "--until", "2027-01-01")
        self.assertTrue(worklog["events"])
        self.cli("meeting", "--start", "2026-01-01", "--until", "2027-01-01")
        knowledge = self.cli("knowledge", "retrieve", "--file", self.payload({"scope": {}}))
        self.assertEqual(len(knowledge["claims"]), 1)
        self.assertEqual(knowledge["claims"][0]["topic"], "task-summary:guide")
        self.assertEqual(knowledge["claims"][0]["validity"], "UNKNOWN")
        self.assertEqual(knowledge["claims"][0]["source_validity"], "UNKNOWN")
        self.cli("knowledge", "observations", "--expected-revision", "0", "--file", self.payload({"observations": [], "dependencies": []}), ok=3)
        observations = self.cli("knowledge", "observations", "--expected-revision", str(knowledge["revision"]), "--file", self.payload({"observations": [], "dependencies": []}))
        self.assertEqual(observations["revision"], knowledge["revision"] + 1)

    def test_public_legacy_execution_is_blocked_before_invocation(self):
        with closing(Store(self.root, self.home)) as store, store.db:
            store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("old", "Legacy task", "READY", "{}", now(), now()))
        for command in ("run", "resume", "verify", "check", "gate", "publish"):
            with self.subTest(command=command):
                result = self.cli(command, "old", ok=3)
                self.assertIn("v2", json.dumps(result))

    def test_cli_transfer_roundtrip_and_json_errors(self):
        self.create()
        archive = self.base / "project.zip"
        exported = self.cli("project", "export", str(archive))
        self.assertEqual(exported["format"], "autonomous-se-project")
        target = self.base / "imported"
        target.mkdir()
        imported = self.cli("project", "import", str(archive), "--root", str(target))
        self.assertNotEqual(imported["project_id"], exported["project"]["project_id"])
        status = self.cli("task", "status", "guide", selector=imported["project_id"])
        self.assertEqual(status["state"], "AWAITING_APPROVAL")
        invalid = self.cli("task", "approve", ok=2)
        self.assertEqual(invalid["status"], "USAGE_ERROR")
        self.assertIn("required", invalid["error"])
        self.assertTrue(invalid["usage"].startswith("usage:") and "approve" in invalid["usage"])
        self.cli("task", "create", "--file", self.payload({"schema_version": 2}), ok=3)

    def test_usage_errors_and_help_have_no_ansi_color_even_when_forced(self):
        self.environment.pop("NO_COLOR", None)
        self.environment.update(FORCE_COLOR="1", PYTHON_COLORS="1")
        for arguments in (("task", "approve"), ("task", "no-such-action"), ("events", "bind-local", "t", "--backend", "x")):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments, ok=2)
                self.assertEqual(result["status"], "USAGE_ERROR")
                self.assertNotIn("\x1b[", json.dumps(result, ensure_ascii=False))
        run = subprocess.run([sys.executable, "-m", "ase", "task", "--help"], cwd=ROOT, env=self.environment,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("\x1b[", run.stdout + run.stderr)

    def test_every_task_subcommand_has_one_line_help(self):
        import argparse
        from ase.__main__ import parser
        commands = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
        task = next(a for a in commands.choices["task"]._actions if isinstance(a, argparse._SubParsersAction))
        described = {action.dest: action.help for action in task._choices_actions}
        self.assertEqual(set(described), set(task.choices))
        self.assertTrue(all(value and "\n" not in value for value in described.values()), described)
        run = subprocess.run([sys.executable, "-m", "ase", "task", "--help"], cwd=ROOT, env=self.environment,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        for name in task.choices:
            self.assertRegex(run.stdout, r"(?m)^\s+" + name.replace("-", r"\-") + r"\s")

    def test_every_events_and_lead_review_subcommand_has_one_line_help(self):
        import argparse
        from ase.__main__ import parser
        commands = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
        for group in ("events", "lead-review"):
            sub = next(a for a in commands.choices[group]._actions if isinstance(a, argparse._SubParsersAction))
            described = {action.dest: action.help for action in sub._choices_actions}
            self.assertEqual(set(described), set(sub.choices), group)
            self.assertTrue(all(value and "\n" not in value for value in described.values()), described)
            run = subprocess.run([sys.executable, "-m", "ase", group, "--help"], cwd=ROOT, env=self.environment,
                                 capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            for name in sub.choices:
                self.assertRegex(run.stdout, r"(?m)^\s+" + name.replace("-", r"\-") + r"\s")
        events = next(a for a in commands.choices["events"]._actions if isinstance(a, argparse._SubParsersAction))
        revision = next(a for a in events.choices["bind-local"]._actions if a.dest == "expected_revision")
        self.assertIn("coordinator binding record", revision.help)
        ack = {a.dest: a for a in events.choices["ack"]._actions}
        for word in ("commit SHA", "gate status", "2000", "no secrets"):
            self.assertIn(word, ack["evidence"].help)
        for outcome in ("handled:", "stale:", "blocked:", "superseded_by", "marks the event processed"):
            self.assertIn(outcome, ack["outcome"].help)
        run = subprocess.run([sys.executable, "-m", "ase", "events", "ack", "--help"], cwd=ROOT, env=self.environment,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("2000", run.stdout)

    def test_quick_lane_approval_answers_with_the_remaining_steps(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "x.py").write_text("X = 1\n", encoding="utf-8")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(git + argv, cwd=self.root, check=True, capture_output=True)
        quick = self.cli("task", "quick", "--id", "q", "--request", "X must be 2", "--resource", "src",
                         "--test", "pytest", "--summary", "Set X to 2", "--user-message-ref", "chat:owner-1")
        approved = self.cli("task", "approve", "q", "--proposal-hash", quick["proposal_hash"], "--stage", "implement",
                            "--user-message-ref", "chat:owner-plan-ok")
        self.assertEqual(approved["state"], "READY")
        self.assertEqual(approved["next"], quick["next"][2:])

    def test_quick_lane_approval_outside_git_names_the_baseline_error(self):
        (self.root / "src").mkdir()
        quick = self.cli("task", "quick", "--id", "q", "--request", "X must be 2", "--resource", "src",
                         "--test", "pytest", "--summary", "Set X to 2", "--user-message-ref", "chat:owner-1")
        approved = self.cli("task", "approve", "q", "--proposal-hash", quick["proposal_hash"], "--stage", "implement",
                            "--user-message-ref", "chat:owner-plan-ok")
        self.assertEqual(approved["state"], "READY")
        self.assertIn("Safe Git", approved["next"][0])
        self.assertEqual(approved["next"][1:], quick["next"][2:])

    def test_packaging_contains_adapter_and_timezone_assets(self):
        settings = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        packaging = settings["tool"]["setuptools"]
        import fnmatch
        configured = packaging.get("packages", [])
        patterns = configured.get("find", {}).get("include", []) if isinstance(configured, dict) else configured
        packages = {".".join(path.parent.relative_to(ROOT).parts) for path in (ROOT / "ase").rglob("__init__.py")}
        self.assertIn("ase.adapters", packages)
        for package in packages:
            self.assertTrue(any(fnmatch.fnmatchcase(package, pattern) for pattern in patterns), package)
        self.assertIn("data/timezones/*", packaging.get("package-data", {}).get("ase", []))

    def test_non_git_init_and_legacy_task_revision(self):
        initial = self.cli("init")
        self.assertTrue(initial["project_id"])
        with closing(Store(self.root, self.home)) as store, store.db:
            store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("old", "Legacy task", "READY", "{}", now(), now()))
        revised = self.cli("task", "revise", "old", "--expected-revision", "0", "--file", self.payload(contract("old")))
        self.assertFalse(revised["legacy_v1"])
        self.assertEqual(revised["approved_stage_ids"], [])

    def test_current_explicit_protected_and_high_risk_authority(self):
        task = self.create()
        discovery = self.cli("task", "discover", "guide", "--expected-revision", "0")["payload"]
        (self.root / "rules.txt").write_text("Shared operating rules", encoding="utf-8")
        discovery["resources"].append({"path": "rules.txt", "status": "INSPECTED", "type": "file"})
        discovery["dependencies"].append({"path": "rules.txt", "from": "guide.txt", "reason": "Shared guide requirements"})
        self.cli("task", "discover", "guide", "--expected-revision", "1", "--file", self.payload(discovery))
        proposal = self.cli("task", "propose", "guide", "--expected-revision", str(task["revision"]), "--file", self.payload({"summary": "Draft the guide", "stage_ids": ["draft"]}))
        self.cli("task", "approve", "guide", "--file", self.payload({"proposal_hash": proposal["proposal_hash"], "user_message_ref": "chat:approval", "approved_stage_ids": ["draft"]}))
        authorization = self.cli("task", "authorize-protected", "guide", "--path", "guide.txt", "--reason", "Approved guide edit", "--user-message-ref", "chat:protected")
        self.assertEqual(authorization["paths"], ["guide.txt"])
        high = self.cli("task", "approve-high-risk", "guide", "--reason", "Reviewed resource changes", "--user-message-ref", "chat:high-risk")
        self.assertEqual(high["user_message_ref"], "chat:high-risk")
        with closing(Store(self.root, self.home)) as store:
            self.assertEqual(read_record(store.db, "protected_approval", "guide")["payload"]["fingerprint"], authorization["fingerprint"])
            self.assertEqual(read_record(store.db, "high_risk_approval", "guide")["payload"]["contract_hash"], task["contract_hash"])
            self.assertEqual(high["fingerprint"], source_snapshot(store, TaskService(store).snapshot("guide")["contract"], task_id="guide")["fingerprint"])
        with closing(Store(self.root, self.home)) as store, store.db:
            store.db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", ("old", "Legacy task", "READY", "{}", now(), now()))
        for arguments in (("publish", "old"), ("approve-high-risk", "old"), ("authorize-protected", "old", "--path", "guide.txt", "--reason", "Old permissions")):
            with self.subTest(command=arguments[0]):
                blocked = self.cli(*arguments, ok=3)
                self.assertIn("v2", json.dumps(blocked))

    def test_documented_approval_session_and_report_forms(self):
        capabilities = self.cli("capabilities")
        self.assertTrue(capabilities["tools"])
        self.assertFalse(capabilities["authorization_granted"])
        task = self.create()
        task = self.cli("task", "propose", "guide", "--file", self.payload({"summary": "Draft guide", "stage_ids": ["draft"]}), "--expected-revision", str(task["revision"]))
        approved = self.cli("task", "approve", "guide", "--proposal-hash", task["proposal_hash"], "--user-message-ref", "chat:explicit-approval", "--stage", "draft")
        self.assertEqual(approved["state"], "READY")
        closed = self.cli("session", "finish", "guide", "--session-id", "documented-session", "--file", self.payload({"finish_reason": "paused", "summary": "Save work", "next_action": "Continue later", "context": {"summary": "Guide task"}}), "--closure-key", "documented-close", "--expected-revision", "0", "--expected-context-revision", "0")
        self.assertEqual(closed["finish_reason"], "paused")
        self.cli("worklog", "--from", "2026-11-01", "--until", "2026-12-01", "--timezone", "Europe/Bucharest")
        self.cli("meeting-report", "--from", "2026-11-01", "--until", "2026-12-01", "--as-of-end")

    def test_document_project_cli_does_not_require_git_on_path(self):
        no_tools = self.base / "no-tools"
        no_tools.mkdir()
        result = subprocess.run([sys.executable, "-m", "ase", "--repo", str(self.root), "--home", str(self.home), "project", "register", "--kind", "document", "--timezone", "Europe/Bucharest"],
                                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), "PATH": str(no_tools)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["kind"], "document")


class ScopedStageCLITests(unittest.TestCase):
    """Real public execution keeps scoped blockers out of independent stages."""
    setUp = UniversalCLITests.setUp
    payload = UniversalCLITests.payload
    cli = UniversalCLITests.cli
    def _parallel_task(self, *, stages=None, gap_stages=('publish',), discovery=True):
        from ase.discovery import discover_capabilities, record_discovery
        from ase.state import transaction, write_record
        from test_runner import FIXTURE
        value = contract('parallel')
        value['stages'] += [
            {'id':'publish','acceptance_ids':[], 'depends_on':[],
             'scope':{'resources':['guide.txt'],'effects':[]}},
            {'id':'follow-up','acceptance_ids':[], 'depends_on':['publish'],
             'scope':{'resources':['guide.txt'],'effects':[]}}]
        task = self.cli('task','create','--file',self.payload(value))
        task = self.cli('task','propose','parallel','--expected-revision',str(task['revision']),
            '--file',self.payload({'summary':'Prepare independent draft while destination is investigated',
                                  'stage_ids':['draft','publish','follow-up']}))
        if stages != []:
            self.cli('task','approve','parallel','--file',self.payload({'proposal_hash':task['proposal_hash'],
                'user_message_ref':'test:original-clear-assignment','approved_stage_ids':stages or ['draft','publish','follow-up']}))
        role_payload = """payload = {'verdict':'PASS'}
request = json.loads(input_text.split('REQUEST_JSON:\\n',1)[1])
role = request['role']
if role == 'implementation':
 assert request['handoff']['stage']['id'] == 'draft', 'Blocked stage launched implementation'
 original = next(item for item in request['source_packet']['files'] if item['path'] == 'guide.txt')
 payload = {'edits':[{'path':'guide.txt','operation':'write','before_sha256':original['sha256'],
                     'content':'Prepared healthy approved draft\\n'}], 'summary':'Scoped draft proposal'}
elif role == 'ac':
 task = json.loads(next(item['content'] for item in request['source_packet']['files'] if item['path']=='_ase_context/contract.json'))
 payload = {'criteria':[{'id':ac['id'],'expected':ac['expected'],'status':'UNVERIFIED','evidence':[],
            'method':'Protocol-only fixture','limitations':['No semantic review'],
            'missing_coverage':['Independent semantic judgment unavailable']} for ac in task['acceptance']]}
elif role == 'review': payload = {'findings':[],'summary':'Protocol-only transport; no semantic approval'}
"""
        backend = self.base/'roles.py'
        backend.write_text(FIXTURE.replace("payload = {'verdict':'PASS'}",role_payload),encoding='utf-8')
        trusted = self.home/'runner'
        trusted.mkdir(exist_ok=True)
        with closing(Store(self.root,self.home)) as store:
            with transaction(store.db) as db:
                write_record(db,'backend_config','codex',{'executable':sys.executable,
                    'executable_args':[str(backend),'success','codex'],'protocol_only':True},expected_revision=0)
            if discovery:
                observed = discover_capabilities(store.project,scope={'resources':['guide.txt']},backend_config={})
                if gap_stages:
                    observed['intake'] = {'source_refs':[{'id':'task','kind':'user_message',
                        'reference':'chat:scoped-gap','observed_at':'2026-11-15T18:00:00Z'}],
                        'outcomes':[{'id':'guide','text':'Prepare independent draft','source_refs':['task']}],
                        'gaps':[{'id':'destination','text':'Publication destination unavailable','required':True,
                            'source_refs':['task'],'blocking_stage_ids':list(gap_stages)}]}
                record_discovery(store,'parallel',observed,expected_revision=0)
        return trusted/'invocations.jsonl'

    def _assert_ready_stage_runs(self, action, *, stages=None, stopped=False):
        self._parallel_task(stages=stages)
        if stopped:
            self.cli('task','stop','parallel','--reason','Temporary user pause')
        result = self.cli('task',action,'parallel',ok=3)
        self.assertEqual((self.root/'guide.txt').read_bytes(),b'Prepared healthy approved draft\n',result)
        self.assertEqual(result['status'],'BLOCKED')  # protocol roles never approve
        self.assertIn('draft',result['stage_results'])
        self.assertIn('publish',result['stage_blockers'])
        self.assertIn('follow-up',result['stage_blockers'])

    def test_run_executes_ready_approved_stage_after_scoped_gap(self):
        self._assert_ready_stage_runs('run')

    def test_resume_executes_ready_stage_and_clears_only_own_stop(self):
        self._assert_ready_stage_runs('resume',stopped=True)

    def test_partial_approval_still_runs_only_ready_approved_stage(self):
        self._assert_ready_stage_runs('run',stages=['draft'])

    def test_all_blocked_stages_never_probe_backend(self):
        log = self._parallel_task(gap_stages=('draft','publish'))
        self.cli('task','run','parallel',ok=3)
        self.cli('task','resume','parallel',ok=3)
        self.assertFalse(log.exists())
        self.assertEqual((self.root/'guide.txt').read_bytes(),b'Operating guide')

    def test_unapproved_or_undiscovered_work_never_probes_backend(self):
        for unapproved in (True,False):
            with self.subTest(unapproved=unapproved):
                # Each condition has its own durable task/project fixture.
                fixture = UniversalCLITests()
                fixture.setUp()
                try:
                    helper = ScopedStageCLITests()
                    helper.__dict__.update(fixture.__dict__)
                    log = helper._parallel_task(stages=[] if unapproved else None,discovery=False)
                    helper.cli('task','run','parallel',ok=3)
                    self.assertFalse(log.exists())
                    self.assertEqual((helper.root/'guide.txt').read_bytes(),b'Operating guide')
                finally:
                    fixture.doCleanups()

    def test_stop_and_cancel_guards_survive_partial_readiness(self):
        log = self._parallel_task()
        self.cli('task','stop','parallel','--reason','User requested pause')
        self.cli('task','run','parallel',ok=3)
        self.assertFalse(log.exists())
        with closing(Store(self.root,self.home)) as store:
            tasks = TaskService(store)
            task = tasks.snapshot('parallel')
            tasks.transition('parallel','CANCELLED',expected_revision=task['revision'],reason='User cancelled')
        self.cli('task','resume','parallel',ok=3)
        self.assertFalse(log.exists())

    def test_child_resume_cannot_clear_ancestor_stop(self):
        log = self._parallel_task()
        with closing(Store(self.root,self.home)) as store:
            from ase.discovery import discover_capabilities, record_discovery
            child = dict(contract('child'),parent_task_id='parallel',parent_stage_id='draft')
            TaskService(store).create(child)
            record_discovery(store,'child',discover_capabilities(store.project,
                scope={'resources':['guide.txt']},backend_config={}),expected_revision=0)
        self.cli('task','stop','parallel','--reason','Stop entire root')
        self.cli('task','resume','child',ok=3)
        self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()

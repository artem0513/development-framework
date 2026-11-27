from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ase.journal import Journal
from ase.knowledge import Knowledge
from ase.state import transaction, write_record
from ase.store import Store
from ase.tasks import TaskService


def contract(task_id):
    return {'id': task_id, 'schema_version': 2, 'profile': 'document', 'request': 'Describe '+task_id,
        'scope': {'resources': ['guide.txt'], 'effects': ['file_write']},
        'acceptance': [{'id': 'A', 'expected': 'A guide', 'evidence_ids': ['guide']}],
        'evidence': [{'id': 'guide', 'kind': 'artifact', 'path': 'guide.txt'}],
        'stages': [{'id': 'write', 'acceptance_ids': ['A'], 'depends_on': []}]}


class ContextModuleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ase-topic-context-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root/'project').mkdir()
        self.store = Store(root/'project', root/'state')
        self.addCleanup(self.store.close)
        for name in ('alpha','beta'):
            TaskService(self.store).create(contract(name))
        self.journal = Journal(self.store)

    def finish(self, name, key, summary, revision=0):
        return self.journal.finish_session(name,key,{'finish_reason':'paused','summary':summary,
            'next_action':'Continue '+name,'context':{'summary':summary,'source_refs':['local:guide.txt']},'blockers':[]},
            closure_key=key, expected_revision=revision, expected_context_revision=self.journal.context()['revision'])

    def test_two_tasks_survive_latest_context_replacement(self):
        self.finish('alpha','first','Alpha requirements')
        self.finish('beta','second','Beta decisions')
        value = self.journal.context()
        self.assertEqual(value['payload']['summary'],'Beta decisions')
        by_task = {item['task_id']:item for item in value['task_contexts']}
        self.assertEqual(by_task['alpha']['summary'],'Alpha requirements')
        self.assertEqual(by_task['beta']['summary'],'Beta decisions')
        self.assertEqual(value['task_context_count'],2)
        self.assertEqual(by_task['alpha']['authority'],'HISTORICAL_TASK_CONTEXT')

    def test_task_summary_is_dated_history_and_explicitly_superseded(self):
        self.finish('alpha','first','First observation')
        first = Knowledge(self.store).query('task-summary:alpha')['claims']
        self.assertEqual(len(first),1)
        self.finish('alpha','second','Revised observation',revision=1)
        claims = Knowledge(self.store).query('task-summary:alpha')['claims']
        self.assertEqual(len(claims),2)
        self.assertEqual(claims[1]['supersedes'],claims[0]['id'])
        self.assertEqual(claims[1]['statement'],'Revised observation')
        current = Knowledge(self.store).retrieve(topic='task-summary:alpha',scope={})
        self.assertEqual(current['claims'][-1]['validity'],'UNKNOWN')
        self.assertIn('journal:',current['claims'][-1]['source_refs'][0])

    def test_replay_does_not_duplicate_context_or_knowledge(self):
        result=self.finish('alpha','first','One observation')
        count=self.store.db.execute('SELECT COUNT(*) FROM knowledge_claims').fetchone()[0]
        self.assertEqual(self.finish('alpha','first','One observation'),result)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM knowledge_claims').fetchone()[0],count)
        self.assertEqual(self.journal.context()['task_context_count'],1)

    def test_failed_closure_rolls_back_context_and_knowledge_in_outer_uow(self):
        self.store.db.execute("CREATE TRIGGER reject_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        self.store.db.commit()
        with transaction(self.store.db) as db:
            with self.assertRaisesRegex(Exception,'simulated failure'):
                self.finish('alpha','first','Must roll back')
            write_record(db,'outer','unrelated',{'ok':True},expected_revision=0)
        self.assertEqual(self.journal.context()['revision'],0)
        self.assertEqual(self.journal.context()['task_context_count'],0)
        self.assertEqual(Knowledge(self.store).query()['claims'],[])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],0)

    def test_legacy_versions_are_available_without_rewriting_them(self):
        with transaction(self.store.db) as db:
            write_record(db,'project_context_version',self.store.project_id+':1',
                {'task_id':'alpha','summary':'Legacy alpha','updated_at_utc':'2026-01-01T00:00:00+00:00'},expected_revision=0)
            write_record(db,'project_context',self.store.project_id,
                {'task_id':'beta','summary':'Legacy beta','updated_at_utc':'2026-01-02T00:00:00+00:00'},expected_revision=0)
        before=list(self.store.db.execute('SELECT * FROM runtime_records'))
        values=self.journal.context()['task_contexts']
        self.assertEqual({item['task_id'] for item in values},{'alpha','beta'})
        self.assertEqual(list(self.store.db.execute('SELECT * FROM runtime_records')),before)

    def test_unrelated_business_topic_is_never_auto_superseded(self):
        old=Knowledge(self.store).record({'topic':'http-client','statement':'Reuse the shared client',
            'observed_at':'2026-01-01T00:00:00Z','source_refs':['local:guide.txt']})
        self.finish('alpha','first','The alpha task changed')
        self.assertIsNone(Knowledge(self.store).query('http-client')['claims'][0]['superseded_by'])
        self.assertEqual(Knowledge(self.store).query('http-client')['claims'][0]['id'],old['id'])


if __name__=='__main__':
    unittest.main(verbosity=2)

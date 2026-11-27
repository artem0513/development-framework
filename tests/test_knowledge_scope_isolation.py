"""Real path drift must not poison unrelated retrieval or hide active peers."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from ase.knowledge import Knowledge
from ase.store import Store


class KnowledgeScopeIsolationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='ase-claim-scope-')
        self.addCleanup(temp.cleanup)
        self.base=Path(temp.name)
        self.repo=self.base/'project'
        self.repo.mkdir()
        self.real=self.repo/'real'
        self.real.mkdir()
        (self.real/'config.txt').write_text('original')
        self.alias=self.repo/'alias'
        self.link(self.real)
        self.store=Store(self.repo,self.base/'state')
        self.addCleanup(self.store.close)
        self.api=Knowledge(self.store)

    def link(self,target):
        if self.alias.exists() or self.alias.is_symlink():
            if self.alias.is_symlink():self.alias.unlink()
            else:self.alias.rmdir()
        if os.name=='nt':
            subprocess.run(['cmd','/c','mklink','/J',str(self.alias),str(target)],check=True,capture_output=True)
        else:
            self.alias.symlink_to(target,target_is_directory=True)

    def claim(self,topic,statement,path,**extra):
        return self.api.record({'topic':topic,'statement':statement,'observed_at':'2026-01-01T00:00:00Z',
            'source_refs':['local:real/config.txt'],'scope':{'paths':[path]},**extra})

    def redirect(self):
        foreign=self.base/'foreign'
        foreign.mkdir()
        (foreign/'config.txt').write_text('unrelated private bytes')
        self.link(foreign)

    def test_superseded_unrelated_scope_drift_cannot_break_selected_retrieval(self):
        old=self.claim('old','Old rule','alias/config.txt')
        self.claim('old','New rule','real/config.txt',supersedes=old['id'])
        good=self.claim('good','Useful rule','real/config.txt')
        self.redirect()
        value=self.api.retrieve(topic='good',scope={'claim_ids':[good['id']]},refresh=False)
        self.assertEqual([c['id'] for c in value['claims']],[good['id']])
        self.assertEqual(value['claims'][0]['validity'],'UNKNOWN')

    def test_active_unrelated_topic_scope_drift_cannot_break_selected_retrieval(self):
        self.claim('unrelated','Old rule','alias/config.txt')
        good=self.claim('good','Useful rule','real/config.txt')
        self.redirect()
        value=self.api.retrieve(topic='good',scope={'claim_ids':[good['id']]},refresh=True)
        self.assertEqual(value['claims'][0]['validity'],'UNKNOWN')
        self.assertFalse(value['claims'][0]['review_required'])

    def test_invalid_active_peer_cannot_be_hidden_by_claim_selector(self):
        peer=self.claim('timeout','100ms','alias/config.txt')
        good=self.claim('timeout','1000ms','real/config.txt')
        self.redirect()
        value=self.api.retrieve(topic='timeout',scope={'claim_ids':[good['id']]},refresh=False)
        self.assertEqual(value['claims'][0]['validity'],'CONFLICT')
        self.assertEqual(value['claims'][0]['conflicts'][0]['alternative_claim_ids'],[peer['id']])

    def test_in_workspace_alias_scopes_still_overlap_after_isolation_fix(self):
        left=self.claim('timeout','100ms','alias/config.txt')
        right=self.claim('timeout','1000ms','real/config.txt')
        value=self.api.retrieve(topic='timeout',scope={'claim_ids':[right['id']]},refresh=False)
        self.assertEqual(value['claims'][0]['validity'],'CONFLICT')
        self.assertEqual(value['claims'][0]['conflicts'][0]['alternative_claim_ids'],[left['id']])


if __name__=='__main__':unittest.main(verbosity=2)

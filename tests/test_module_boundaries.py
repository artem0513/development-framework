import ast
import importlib
from pathlib import Path
import subprocess
import shutil
import tempfile
from unittest.mock import patch
import sys
import unittest


class ModuleBoundaryTests(unittest.TestCase):
    def test_affected_selection_includes_owners_and_transitive_dependents(self):
        from ase.modules import affected, MODULES
        names = {m.name for m in affected(['ase/findings.py'])}
        self.assertIn('review', names)
        self.assertTrue(all(set(m.depends_on) & names or m.name == 'review' for m in MODULES if m.name in names))
        storage = {m.name for m in affected([r'.\ase\store.py'])}
        self.assertGreater(len(storage), len(names))
        self.assertEqual(affected(['README.md']), [])

    def test_feature_services_have_independent_entry_points(self):
        for module,name,method in [('ase.testing.execution','CheckService','check'),
                ('ase.evidence.packets','PacketService','packet'),
                ('ase.review.service','VerificationService','verify'),
                ('ase.execution.actions','ActionService','execute_actions')]:
            with self.subTest(module=module):
                service=getattr(importlib.import_module(module),name)
                self.assertTrue(callable(getattr(service,method)))

    def test_every_runtime_file_and_test_has_an_explicit_functional_owner(self):
        from ase.modules import MODULES
        root=Path(__file__).resolve().parent.parent
        sources=[source for module in MODULES for source in module.sources]
        actual={p.relative_to(root).as_posix() for p in (root/'ase').rglob('*.py')}
        self.assertEqual(set(sources),actual)
        self.assertEqual(len(sources),len(set(sources)))
        declared={test for module in MODULES for test in module.tests}
        self.assertEqual(declared,{p.name for p in (root/'tests').glob('test_*.py')})

    def test_module_test_selection_and_dependency_graph_are_valid(self):
        from ase.modules import MODULES,test_files
        root=Path(__file__).resolve().parent.parent
        by_name={m.name:m for m in MODULES}
        self.assertEqual(len(by_name),len(MODULES))
        visiting,visited=set(),set()
        def visit(name):
            self.assertNotIn(name,visiting)
            if name in visited:return
            visiting.add(name)
            for dependency in by_name[name].depends_on:visit(dependency)
            visiting.remove(name);visited.add(name)
        for module in MODULES:
            visit(module.name)
            self.assertTrue(test_files(module.name,root=root))
            self.assertEqual(len(test_files(module.name,root=root)),len(module.tests))

    def test_reporting_query_modules_cannot_execute_or_approve_work(self):
        import ase.memory.reports as reports
        import ase.memory.repository as repository
        for module in (reports,repository):
            tree=ast.parse(Path(module.__file__).read_text())
            imports={n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}
            self.assertFalse(imports & {'workflow','runner','gates','credentials','publishing'})

    def test_extracted_engine_changes_invalidate_old_host_policy(self):
        from ase import runtime_policy
        root=Path(runtime_policy.__file__).parent
        with tempfile.TemporaryDirectory(prefix='ase-modular-policy-') as temp:
            copied=Path(temp)/'ase'
            shutil.copytree(root,copied,ignore=shutil.ignore_patterns('__pycache__'))
            with patch.object(runtime_policy,'__file__',str(copied/'runtime_policy.py')):
                before=runtime_policy.policy_fingerprint()
                for relative in ('testing/execution.py','evidence/packets.py','memory/context.py','journal.py','knowledge.py'):
                    file=copied/relative
                    content=file.read_bytes()
                    file.write_bytes(content+b'\n# changed source\n')
                    self.assertNotEqual(runtime_policy.policy_fingerprint(),before,relative)
                    file.write_bytes(content)
                    self.assertEqual(runtime_policy.policy_fingerprint(),before)

    def test_catalogue_cli_rejects_unknown_module_without_running_tests(self):
        result=subprocess.run([sys.executable,'-B','-m','ase.modules','does-not-exist','--test'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('Unknown module',result.stderr)


if __name__=='__main__':unittest.main(verbosity=2)

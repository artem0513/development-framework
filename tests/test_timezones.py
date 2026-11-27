"""Pinned TZif behavior independent of execution-host timezone databases."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ase.timezones import resolve_timezone, TimezoneDataUnavailable


class TimezoneTests(unittest.TestCase):
    def test_bucharest_midnight_and_dst(self):
        zone = resolve_timezone("Europe/Bucharest")
        cases = {"2026-11-11T22:30:00+00:00": "2026-11-12T00:30:00+02:00",
                 "2026-03-29T00:30:00+00:00": "2026-03-29T02:30:00+02:00",
                 "2026-03-29T01:30:00+00:00": "2026-03-29T04:30:00+03:00",
                 "2026-10-25T00:30:00+00:00": "2026-10-25T03:30:00+03:00",
                 "2026-10-25T01:30:00+00:00": "2026-10-25T03:30:00+02:00"}
        for value, expected in cases.items():
            self.assertEqual(datetime.fromisoformat(value).astimezone(zone).isoformat(), expected)

    def test_asset_matches_provenance_and_does_not_need_installed_tzdata(self):
        import ase.timezones as module
        root = Path(module.__file__).parent / "data" / "timezones"
        provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256((root / "Europe" / "Bucharest").read_bytes()).hexdigest(), provenance["zones"]["Europe/Bucharest"]["sha256"])
        with patch("ase.timezones.ZoneInfo", side_effect=AssertionError("system lookup disallowed")):
            self.assertEqual(resolve_timezone("Europe/Bucharest").key, "Europe/Bucharest")
        self.assertTrue(provenance["iana_version"])
        self.assertTrue((root / "LICENSE").is_file())

    def test_missing_invalid_and_tampered_rules_are_typed_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing"
            with self.assertRaisesRegex(TimezoneDataUnavailable, "TZ_DATA_UNAVAILABLE"):
                resolve_timezone("Europe/Bucharest", tzif_path=path)
            path.write_bytes(b"TZif-not-real")
            with self.assertRaises(TimezoneDataUnavailable):
                resolve_timezone("Europe/Bucharest", tzif_path=path)
        with patch("ase.timezones.ZoneInfo", side_effect=KeyError("missing")):
            with self.assertRaises(TimezoneDataUnavailable):
                resolve_timezone("Unknown/Zone")
        self.assertEqual(datetime(2026, 1, 1, tzinfo=resolve_timezone("UTC")).utcoffset().total_seconds(), 0)

class LocalTimeTests(unittest.TestCase):
    """Native local rules and real journal/CLI integration in isolated processes."""

    def local_process(self, source, zone=None):
        import os
        import subprocess
        import sys
        import textwrap
        # POSIX leaves omitted DST rules to host posixrules; the Windows CRT
        # supports the shorter form and applies US rules. Pin the same intended
        # transition dates explicitly where the full POSIX syntax is supported.
        if zone is None:
            zone = 'EST5EDT' if os.name == 'nt' else 'EST5EDT,M3.2.0/2,M11.1.0/2'
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(source)],
            cwd=root, env={**os.environ, 'TZ': zone, 'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_local_uses_date_specific_os_rules_across_gap_and_fold(self):
        result = self.local_process('''
            from datetime import datetime
            import json
            from ase.timezones import resolve_timezone
            values=['2026-01-15T12:00:00+00:00','2026-07-15T12:00:00+00:00',
                '2026-03-08T06:30:00+00:00','2026-03-08T07:30:00+00:00',
                '2026-11-01T05:30:00+00:00','2026-11-01T06:30:00+00:00']
            print(json.dumps([datetime.fromisoformat(v).astimezone(resolve_timezone('local')).isoformat() for v in values]))
        ''')
        self.assertEqual(result, ['2026-01-15T07:00:00-05:00','2026-07-15T08:00:00-04:00',
            '2026-03-08T01:30:00-05:00','2026-03-08T03:30:00-04:00',
            '2026-11-01T01:30:00-04:00','2026-11-01T01:30:00-05:00'])

    def test_local_selection_follows_the_current_process_without_named_tzdata(self):
        code='''
            from datetime import datetime
            import json
            from unittest.mock import patch
            from ase.timezones import resolve_timezone
            with patch('ase.timezones.ZoneInfo',side_effect=AssertionError('Named lookup forbidden')):
                print(json.dumps(datetime.fromisoformat('2026-07-15T12:00:00+00:00').astimezone(resolve_timezone('local')).isoformat()))
        '''
        self.assertEqual(self.local_process(code,'UTC0'), '2026-07-15T12:00:00+00:00')
        self.assertEqual(self.local_process(code), '2026-07-15T08:00:00-04:00')

    def test_local_journal_day_bounds_midnight_and_utc_storage(self):
        result = self.local_process('''
            from contextlib import closing
            from datetime import datetime
            import json,tempfile
            from pathlib import Path
            from ase.store import Store
            from ase.journal import Journal
            from ase.tasks import TaskService
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder)/'project';root.mkdir()
                with closing(Store(root,Path(folder)/'state')) as store:
                    journal=Journal(store)
                    TaskService(store).create({'id':'local-journal','schema_version':2,'profile':'document',
                        'request':'Write a guide','scope':{'resources':['guide.md'],'effects':['file_write']},
                        'acceptance':[{'id':'DOC','expected':'Guide exists','evidence_ids':['guide']}],
                        'evidence':[{'id':'guide','kind':'artifact','path':'guide.md'}],
                        'stages':[{'id':'write','acceptance_ids':['DOC']}]})
                    for name,stamp in [('before','2026-03-08T04:59:59+00:00'),
                        ('start','2026-03-08T05:00:00+00:00'),
                        ('gap-before','2026-03-08T06:30:00+00:00'),
                        ('gap-after','2026-03-08T07:30:00+00:00'),
                        ('until','2026-03-09T04:00:00+00:00')]:
                        journal.append({'kind':'decision','task_id':'local-journal','summary':name,'occurred_at_utc':stamp},event_key=name)
                    stored=journal._events()
                    spring=journal.worklog('2026-03-08','2026-03-09',timezone='local')
                    fall=journal.meeting_report('2026-11-01','2026-11-02',timezone='local')
                    def window(r):return [r['start_at_utc'],r['until_at_utc']]
                    print(json.dumps({'spring':window(spring),'fall':window(fall),
                        'events':[(e['summary'],e['local_date'],e['local_time']) for e in spring['events']],
                        'unchanged_utc':stored==journal._events()}))
        ''')
        self.assertEqual(result['spring'], ['2026-03-08T05:00:00+00:00','2026-03-09T04:00:00+00:00'])
        self.assertEqual(result['fall'], ['2026-11-01T04:00:00+00:00','2026-11-02T05:00:00+00:00'])
        self.assertEqual([e[0] for e in result['events']], ['start','gap-before','gap-after'])
        self.assertTrue(all(e[1]=='2026-03-08' for e in result['events']))
        self.assertEqual(result['events'][-1][2], '2026-03-08T03:30:00-04:00')
        self.assertTrue(result['unchanged_utc'])

    def test_new_settings_registry_and_task_defaults_are_local(self):
        from contextlib import closing
        import tomllib
        from ase.project_local import config_bytes
        from ase.projects import ProjectRegistry
        from ase.store import Store
        from ase.tasks import TaskService
        self.assertEqual(tomllib.loads(config_bytes(['codex']).decode())['timezone'], 'local')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'project';root.mkdir()
            with closing(ProjectRegistry(Path(folder)/'state')) as registry:
                project=registry.register(root,kind='document')
                self.assertEqual(project.timezone,'local')
            with closing(Store(project)) as store:
                task=TaskService(store).create({'id':'local-default','schema_version':2,'profile':'document',
                    'request':'Write a local guide','scope':{'resources':['guide.md'],'effects':['file_write']},
                    'acceptance':[{'id':'DOC','expected':'Guide exists','evidence_ids':['guide']}],
                    'evidence':[{'id':'guide','kind':'artifact','path':'guide.md'}],
                    'stages':[{'id':'write','acceptance_ids':['DOC']}]})
                self.assertEqual(task['contract']['reporting']['timezone'],'local')

    def test_cli_registration_and_report_need_no_timezone_option(self):
        result=self.local_process('''
            from pathlib import Path
            import json,subprocess,sys,tempfile
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder)/'project';root.mkdir();home=Path(folder)/'state'
                base=[sys.executable,'-B','-m','ase','--repo',str(root),'--home',str(home)]
                p=subprocess.run(base+['project','register','--kind','document'],capture_output=True,text=True)
                if p.returncode:raise AssertionError(p.stdout+p.stderr)
                r=subprocess.run(base+['worklog','--from','2026-03-08','--until','2026-03-09'],capture_output=True,text=True)
                if r.returncode:raise AssertionError(r.stdout+r.stderr)
                print(json.dumps({'project':json.loads(p.stdout)['timezone'],'report':json.loads(r.stdout)}))
        ''')
        self.assertEqual(result['project'],'local')
        self.assertEqual(result['report']['timezone'],'local')
        self.assertEqual(result['report']['start_at_utc'],'2026-03-08T05:00:00+00:00')
        self.assertEqual(result['report']['until_at_utc'],'2026-03-09T04:00:00+00:00')

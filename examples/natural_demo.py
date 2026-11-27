"""Exercise installed global routing from an ordinary request in a disposable repo."""
from pathlib import Path
import json
import os
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ase.core import execute, save
from create_demo import create

destination = Path(sys.argv[1]).resolve()
created = create(destination)
repo = Path(created['repo'])
(repo / 'calculator.py').write_text("def divide(numerator, denominator):\n    raise NotImplementedError('Implement the requested behavior')\n", encoding='utf-8')
subprocess.run(['git', '-C', str(repo), 'add', 'calculator.py'], check=True)
subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'Start natural language task from incomplete code'], check=True, capture_output=True)
state_home = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'AutonomousSE'
state_home.mkdir(parents=True, exist_ok=True)
prompt = ('Implement safe division in calculator.py. divide(12, 3) must return 4 and divide(-12, 3) must return -4. '
          'divide(1, 0) must raise ValueError with a message containing nonzero. Existing tests describe these confirmed criteria. '
          'Use the autonomous development workflow configured for this user: inspect and plan, implement, independently verify acceptance '
          'and review the code, run real tests, keep project memory, and prepare the final evidence report. Do not push or contact external services '
          'other than Codex model execution. This disposable repository is authorized for implementation and verification; do not ask for routine design approval.')
receipt = execute(['codex', 'exec', '--ephemeral', '--sandbox', 'workspace-write', '--add-dir', str(state_home),
                   '--cd', str(repo), '--json', prompt], repo, destination / 'session.log', 600)
save(destination / 'receipt.json', receipt)
print(json.dumps({'demo': created, 'receipt': receipt}, indent=2))

"""Fresh ordinary chat adoption of a generic task; model execution is live."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ase.core import execute, save
from create_demo import create


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    destination = args.destination.resolve()
    if not args.resume:
        created = create(destination)
        repo = Path(created["repo"])
        (repo / "calculator.py").write_text("def divide(numerator, denominator):\n    raise NotImplementedError('Implement the requested behavior')\n", encoding="utf-8")
        (repo / "requirements.txt").write_text("divide(12, 3) returns 4.\ndivide(-12, 3) returns -4.\ndivide(1, 0) raises ValueError containing nonzero.\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "Generic incomplete source-backed task"], check=True, capture_output=True)
        save(destination / "demo.json", created)
        prompt = ("Implement the three safe division requirements in requirements.txt using the configured autonomous engineering workflow. "
                  "Treat that complete local file as the agreed source inventory for this task. This is a generic future-project template exercise. "
                  "Record source completeness and an analyzed reference, execute actual checks and separate independent AC/review using the configured MCP service. "
                  "Persist a guarded checkpoint with the exact next action for a fresh chat. Report the gate and task id. "
                  "You are authorized to implement and verify this disposable repository and make local evidence files. "
                  "Do not push, send messages, enable schedules or contact external services other than Codex model execution. "
                  "Proceed with the established design without routine design approval questions.")
    else:
        created = json.loads((destination / "demo.json").read_text(encoding="utf-8"))
        repo = Path(created["repo"])
        prompt = ("Resume the existing safe division task in this repository using the configured universal project context workflow. "
                  "Recover task identity, original contract, checkpoint and source coverage from persistent state, reconcile current Git observations, "
                  "and save a new revision-checked checkpoint recording this handoff verification and the exact next action. "
                  "Do not change source code or repeat external actions. Recompute the deterministic gate and report its actual status, prior/new checkpoint revision, "
                  "source coverage and any drift. This local state maintenance is authorized. Do not create a duplicate task, push or contact external services other than Codex model execution.")
    state_home = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "AutonomousSE"
    state_home.mkdir(parents=True, exist_ok=True)
    phase = "resume" if args.resume else "start"
    receipt = execute(["codex", "exec", "--ephemeral", "--sandbox", "workspace-write", "--add-dir", str(state_home),
                       "--cd", str(repo), "--json", prompt], repo, destination / (phase + ".session.log"), 600)
    save(destination / (phase + ".receipt.json"), receipt)
    print(json.dumps({"demo": created, "receipt": receipt}, indent=2))


if __name__ == "__main__":
    main()

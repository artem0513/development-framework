"""Exercise installed project-context-manager in a fresh chat; no external CI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ase.core import execute, save


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    base = args.destination.resolve()
    if base.exists():
        raise SystemExit("Choose a fresh disposable destination")
    repo = base / "project"
    repo.mkdir(parents=True)
    (repo / "storage-decisions.md").write_text(
        "# Synthetic storage decisions\n\n"
        "2026-10-01T10:00:00+00:00: The prototype selected JSON files for local state.\n"
        "2026-11-01T10:00:00+00:00: SQLite replaced that choice for local state.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "feature/knowledge", str(repo)], check=True, capture_output=True)
    for key, value in [("user.name", "Knowledge Demo"), ("user.email", "demo@example.invalid")]:
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "Synthetic decision evidence"], check=True, capture_output=True)
    state = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "AutonomousSE"
    state.mkdir(parents=True, exist_ok=True)
    prompt = ("Use the configured project-context-manager skill to preserve the two dated storage decisions in storage-decisions.md as durable historical knowledge. "
              "Use one topic storage-decision, source-backed dates and an explicit supersession from the prototype choice to the later choice. "
              "Query the stored history and run its structural lint. Do not infer that historical claims are current code truth or authorization. "
              "This synthetic local-state maintenance is authorized. Leave repository files unchanged, do not create tasks, schedules or another orchestrator, "
              "and do not contact GitHub or other external services except Codex execution. Report actual record IDs, retained history and lint result.")
    receipt = execute(["codex", "exec", "--ephemeral", "--sandbox", "workspace-write", "--add-dir", str(state),
                       "--cd", str(repo), "--json", prompt], repo, base / "session.log", 600)
    save(base / "receipt.json", receipt)
    print(json.dumps({"repository": str(repo), "exit_code": receipt["exit_code"], "error": receipt["error"]}, indent=2))


if __name__ == "__main__":
    main()

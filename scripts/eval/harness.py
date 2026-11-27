"""Build the synthetic systems and run blind agentic Claude attempts against them.

usage:
  python scripts/eval/harness.py build [NAMES]              # create <ASE_EVAL_HOME>/systems/<name> git repos
  python scripts/eval/harness.py run BATCH ARMS REPS [IDS]  # ARMS: old (git HEAD), new (working tree)

Each eval repo is laid out as scripts/install.py lays out a shared Claude install: skills in .claude/skills,
role descriptors from templates/agents/claude in .claude/agents, and the installer's _routing('.claude/skills')
block appended to a CLAUDE.md that imports the project's AGENTS.md. The old arm takes skills, install.py and
role files from git HEAD; the new arm takes all of them from the working tree.

SUBAGENT scenarios (X3D, X7D, H8D) send a lead's worker brief as the top-level prompt, simulating a worker.
The lead can now also delegate for real: the Agent tool and the installed role descriptors are available.

Results go to <ASE_EVAL_HOME>/results/BATCH (default: the system temp folder). Graders read only
<id>/final.md and tools.txt; BATCH-map.json holds the arms and stays hidden until unblind.py.
Each attempt is a real Claude Code session (read-only tools plus subagents) and costs tokens.
"""
import functools
import concurrent.futures as cf
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAMEWORK = HERE.parents[1]
EVALS = Path(os.environ.get("ASE_EVAL_HOME") or Path(tempfile.gettempdir()) / "ase-evals")
SYSTEMS = EVALS / "systems"
_LOCAL_CLAUDE = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")  # native installer location
CLAUDE = (os.environ.get("ASE_EVAL_CLAUDE") or shutil.which("claude")
          or (str(_LOCAL_CLAUDE) if _LOCAL_CLAUDE.exists() else "claude"))
GIT = ["git", "-c", "core.autocrlf=false"]
sys.path.insert(0, str(HERE))
import systems_clinic, systems_commerce, systems_logistics, systems_workspace, systems_workspace2, systems_workspacehl, systems_workspace3, systems_replica, systems_stock, systems_scope, systems_helpers  # noqa: E402

MODULES = {"commerce": systems_commerce, "clinic": systems_clinic, "logistics": systems_logistics,
           "workspace": systems_workspace, "workspace2": systems_workspace2, "workspacehl": systems_workspacehl, "workspace3": systems_workspace3,
           **systems_replica.VARIANTS, **systems_stock.VARIANTS, **systems_scope.VARIANTS, **systems_helpers.VARIANTS}
SCENARIOS = {sid: name for name, mod in MODULES.items() for sid in mod.TICKETS}
SUBAGENTS = {**systems_replica.SUBAGENTS, **systems_stock.SUBAGENTS}
SCENARIOS.update({sid: system for sid, (system, _) in SUBAGENTS.items()})
REVIEWS = {**{sid: ("workspace", spec) for sid, spec in systems_workspace.REVIEWS.items()},
           **{sid: ("ledger", spec) for sid, spec in systems_helpers.REVIEWS.items()}}
# Files a ticket scenario finds in its repository (for example the PR an owner question refers to).
EXTRA_FILES = systems_helpers.EXTRA_FILES
SCENARIOS.update({sid: name for sid, (name, _) in REVIEWS.items()})
FOLLOWUPS = systems_workspace3.FOLLOWUPS
SCENARIOS.update({sid: "workspace3" for sid in FOLLOWUPS})


def _framework_git(*args):
    return subprocess.run(["git", "-c", "safe.directory=" + FRAMEWORK.as_posix(), "-C", str(FRAMEWORK), *args],
                          capture_output=True, check=True).stdout


@functools.lru_cache(maxsize=None)
def installer(arm):
    """scripts/install.py from git HEAD (old) or the working tree (new), loaded as a temporary module."""
    path = FRAMEWORK / "scripts" / "install.py"
    source = path.read_bytes() if arm == "new" else _framework_git("show", "HEAD:scripts/install.py")
    module = types.ModuleType("ase_eval_install_" + arm)
    module.__file__ = str(path)
    exec(compile(source, f"{arm}:scripts/install.py", "exec"), module.__dict__)
    return module


def managed_block(arm):
    """The routing text the installer writes for Claude, without its markers."""
    mod = installer(arm)
    return mod._routing(".claude/skills").removeprefix(mod.BEGIN).removesuffix(mod.END).strip()


def claude_md(arm):
    """CLAUDE.md as the installer leaves it in a project whose CLAUDE.md imports AGENTS.md."""
    mod = installer(arm)
    return mod.managed_text("@AGENTS.md\n", mod.BEGIN, mod.END, mod._routing(".claude/skills"))


PROJECT_AGENTS = """# {title}

Follow the existing architecture and conventions in README.md and docs/.
{extra}"""

PROMPT = """You are a developer on this team. Pick up the ticket below and handle it as you normally would on this team, following the repository's AGENTS.md and the instructions it points to.

Environment notes:
- In this session you can read files and run read-only git commands (log, show, blame, diff, status). You cannot edit files, build or run tests.
- The `ase` CLI and separate reviewer sessions are not available. Where the workflow would call them, say what you would submit.
- The ticket reporter and the product/platform owner are not available to chat. If you need a decision or approval from them, write the exact message you would send.

Reply with:
1. What you found (evidence you checked).
2. Your decision and why.
3. The exact code changes as a unified diff, or a clear statement that no code change should be made now and why.
4. Any message to the owner/reporter (questions or approval requests), or "none".

----- TICKET -----
{ticket}
"""

REVIEW_PROMPT = """You are a reviewer on this team. Review the pull request in {pr} (its diff is against the repository as it is now) as you normally would on this team, following the repository's AGENTS.md and the instructions it points to. The ticket it implements is below.

Environment notes:
- In this session you can read files and run read-only git commands (log, show, blame, diff, status). You cannot edit files, build or run tests.
- The `ase` CLI is not available. The PR author is not available to chat; write your review as you would post it.

Reply with your review: the verdict (approve / request changes) and the findings with severity.

----- TICKET -----
{ticket}
"""


def git(cwd, *args, env=None):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, env=env)


def build(names=None):
    SYSTEMS.mkdir(parents=True, exist_ok=True)
    for name, mod in MODULES.items():
        if names and name not in names:
            continue
        repo = SYSTEMS / name
        if repo.exists():
            raise SystemExit(f"{repo} exists; remove it manually to rebuild")
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        for message, author, date, changes in mod.history():
            for rel, content in changes.items():
                path = repo / rel
                if content is None:
                    path.unlink()
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
            git(repo, "add", "-A")
            email = author.split()[0].lower() + "@example.com"
            env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
                   "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date}
            git(repo, "commit", "-q", "-m", message, env=env)
        print("built", repo)


def framework_files(arm, prefix):
    """{relative path under prefix: bytes} from git HEAD (old) or the working tree (new)."""
    if arm == "new":
        base = FRAMEWORK / prefix
        return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts}
    names = _framework_git("ls-tree", "-r", "-z", "--name-only", "HEAD", prefix).decode("utf-8").split("\0")
    return {n[len(prefix) + 1:]: _framework_git("show", "HEAD:" + n) for n in names if n}


def skills(arm):
    # Canonical skills, as the installer copies them; the repository's mirrors could be stale.
    return framework_files(arm, "skills")


def roles(arm):
    """The Claude role descriptors the installer copies (templates/agents/claude/*.md)."""
    return {rel: data for rel, data in framework_files(arm, "templates/agents/claude").items()
            if "/" not in rel and rel.endswith(".md")}


def prepare(run_dir, system, arm, extra=None):
    repo = run_dir / "repo"
    shutil.copytree(SYSTEMS / system, repo)
    for rel, content in (extra or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8", newline="\n")
    for rel, data in skills(arm).items():
        path = repo / ".claude" / "skills" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for rel, data in roles(arm).items():
        path = repo / ".claude" / "agents" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    title = getattr(MODULES[system], "TITLE", system.capitalize())
    (repo / "AGENTS.md").write_text(PROJECT_AGENTS.format(title=title, extra=getattr(MODULES[system], "AGENTS_EXTRA", "")), encoding="utf-8", newline="\n")
    (repo / "CLAUDE.md").write_text(claude_md(arm), encoding="utf-8", newline="\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Platform", "GIT_AUTHOR_EMAIL": "platform@example.com",
           "GIT_COMMITTER_NAME": "Platform", "GIT_COMMITTER_EMAIL": "platform@example.com",
           "GIT_AUTHOR_DATE": "2026-11-20T08:00:00", "GIT_COMMITTER_DATE": "2026-11-20T08:00:00"}
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Adopt DevelopmentFramework", env=env)
    return repo


def summarize(stream_path):
    tools, final = [], ""
    for line in stream_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant":
            for item in event["message"].get("content", []):
                if item.get("type") == "tool_use":
                    inp = item.get("input", {})
                    target = (inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("skill")
                              or inp.get("subagent_type") or inp.get("description") or "")
                    tools.append(f"{item['name']}: {str(target).replace(chr(10), ' ')[:200]}")
        elif event.get("type") == "result":
            final = event.get("result") or ""
    return tools, final


def attempt(batch_dir, run_root, sid, arm):
    rid = "r" + uuid.uuid4().hex[:8]
    out = batch_dir / rid
    out.mkdir(parents=True)
    run_dir = run_root / rid
    if sid in FOLLOWUPS:
        pr_path, pr_text, ticket_id, findings = FOLLOWUPS[sid]
        repo = prepare(run_dir, SCENARIOS[sid], arm, {pr_path: pr_text})
        prompt = systems_workspace3.F_PROMPT.format(pr=pr_path, ticket=systems_workspace2.TICKETS[ticket_id], findings=findings)
    elif sid in SUBAGENTS:
        repo = prepare(run_dir, SCENARIOS[sid], arm)
        prompt = SUBAGENTS[sid][1]
    elif sid in REVIEWS:
        pr_path, pr_text, ticket_id = REVIEWS[sid][1]
        repo = prepare(run_dir, SCENARIOS[sid], arm, {pr_path: pr_text})
        prompt = REVIEW_PROMPT.format(pr=pr_path, ticket=MODULES[SCENARIOS[sid]].TICKETS[ticket_id])
    else:
        repo = prepare(run_dir, SCENARIOS[sid], arm, EXTRA_FILES.get(sid))
        prompt = PROMPT.format(ticket=MODULES[SCENARIOS[sid]].TICKETS[sid])
    cmd = [str(CLAUDE), "-p", "--setting-sources", "project", "--tools", "Read,Grep,Glob,Bash,Skill,Agent",
           "--allowedTools", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)", "Bash(git diff:*)", "Bash(git status:*)",
           "--permission-mode", "dontAsk", "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    # Keep a local text-to-speech plugin (if any) from reading eval sessions aloud.
    env = {**os.environ, "AGENT_TTS_DATA_DIR": str(EVALS / "tts-off")}
    started = time.time()
    with open(out / "stream.jsonl", "wb") as stream, open(out / "stderr.log", "wb") as err:
        try:
            proc = subprocess.run(cmd, cwd=repo, input=prompt.encode("utf-8"),
                                  stdout=stream, stderr=err, env=env, timeout=1500)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    tools, final = summarize(out / "stream.jsonl")
    (out / "final.md").write_text(final, encoding="utf-8")
    (out / "tools.txt").write_text("\n".join(tools), encoding="utf-8")
    (out / "run.json").write_text(json.dumps({"exit": code, "seconds": round(time.time() - started)}), encoding="utf-8")
    return rid, sid, arm, code


def run(batch, arms, reps, ids=None):
    batch_dir = EVALS / "results" / batch
    batch_dir.mkdir(parents=True, exist_ok=False)
    run_root = EVALS / "runs" / batch
    run_root.mkdir(parents=True)
    plan = [(sid, arm) for sid in (ids or sorted(SCENARIOS)) for arm in arms for _ in range(reps)]
    random.shuffle(plan)
    mapping = []
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for rid, sid, arm, code in pool.map(lambda p: attempt(batch_dir, run_root, *p), plan):
            mapping.append({"id": rid, "scenario": sid, "arm": arm, "exit": code})
            print(rid, sid, code, flush=True)
    # The grader opens only <id>/final.md and tools.txt; arms stay in this separate file.
    (EVALS / "results" / (batch + "-map.json")).write_text(json.dumps(mapping, indent=1), encoding="utf-8")
    (batch_dir / "scenarios.json").write_text(json.dumps({m["id"]: m["scenario"] for m in mapping}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2].split(",") if len(sys.argv) > 2 else None)
    elif sys.argv[1] == "run":
        run(sys.argv[2], sys.argv[3].split(","), int(sys.argv[4]), sys.argv[5].split(",") if len(sys.argv) > 5 else None)

"""Install owned runtime, skills and agent definitions, backing up changed files."""
from pathlib import Path
import argparse
import datetime
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tomllib
import stat
import base64
import codecs
import hashlib
import copy

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ase.project_local import (config_bytes, is_private_path, is_framework_source, validate_local_settings,
                               PRIVATE_BEGIN, PRIVATE_END, _plain_git)
from ase.framework import (GIT_TIMEOUT_SECONDS, SOURCE_RELATIVE as FRAMEWORK_SOURCE, UNKNOWN, recorded_source,
                           source_commit)

# FRAMEWORK_SOURCE is generated per install with the source commit; never copied from the checkout.
PACKAGE_DIRECTORIES = ('ase', 'skills', 'agents', 'docs', 'templates', 'scripts')

BEGIN = "<!-- autonomous-se managed begin -->"
END = "<!-- autonomous-se managed end -->"
SKILLS_BEGIN = "# autonomous-se project skill selection begin"
SKILLS_END = "# autonomous-se project skill selection end"


def managed_text(current, begin, end, block):
    """Replace one owned block, preserving all surrounding project/user text."""
    if current.count(begin) != current.count(end) or current.count(begin) > 1:
        raise ValueError('Malformed managed block; inspect existing instructions before installation')
    if begin in current:
        before, rest = current.split(begin, 1)
        _, after = rest.split(end, 1)
        return before + block + after
    return current.rstrip() + '\n\n' + block + '\n'


def _is_link(path):
    if path.is_symlink():
        return True
    try:
        # Path.is_junction is unavailable on supported Python 3.11. Windows
        # lstat has exposed the reparse tag since Python 3.8.
        info = path.lstat()
        return bool(getattr(info, 'st_file_attributes', 0) & 0x400) or (
            getattr(info, 'st_reparse_tag', None) == getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', object()))
    except (FileNotFoundError, NotADirectoryError):
        return False


def project_target(project, target):
    """Reject linked targets that would write outside the selected project."""
    target = Path(target)
    if not target.resolve().is_relative_to(project):
        raise ValueError('Project installation path escapes selected project: ' + str(target))
    if _is_link(target):
        raise ValueError('Project installation refuses a linked managed target: ' + str(target))
    for parent in target.parents:
        if parent == project:
            break
        if _is_link(parent):
            raise ValueError('Project installation refuses a linked managed directory: ' + str(parent))
        if parent.exists() and not parent.is_dir():
            raise ValueError('Project installation parent is not a directory: ' + str(parent))
    if target.is_file() and target.stat().st_nlink > 1:
        raise ValueError('Project installation refuses a hardlinked managed file: ' + str(target))
    return target


def _target_type(target, directory):
    target = Path(target)
    if _is_link(target) and (not directory or not target.exists()):
        raise ValueError('Installation refuses a linked managed file or dangling directory: ' + str(target))
    if target.exists() and not (target.is_dir() if directory else target.is_file()):
        raise ValueError(('Expected a directory destination: ' if directory else 'Expected a regular file destination: ') + str(target))
    if not directory and target.is_file() and target.stat().st_nlink > 1:
        raise ValueError('Installation refuses a hardlinked managed file: ' + str(target))
    for parent in target.parents:
        if parent.is_symlink() and not parent.exists():
            raise ValueError('Installation parent is a dangling link: ' + str(parent))
        if parent.exists() and not parent.is_dir():
            raise ValueError('Installation parent is not a directory: ' + str(parent))
    return target


def file_target(target):
    return _target_type(target, directory=False)


def directory_target(target):
    return _target_type(target, directory=True)


def _toml_tokens(text):
    """Lex enough TOML to locate a value without treating strings as syntax."""
    tokens, index = [], 0
    while index < len(text):
        start, char = index, text[index]
        if char in ' \t\r':
            index += 1
            continue
        if char == '#':
            index = text.find('\n', index)
            if index < 0:
                break
            continue
        if char in '\"\'':
            triple = text.startswith(char * 3, index)
            width = 3 if triple else 1
            index += width
            while index < len(text):
                if char == '"' and text[index] == '\\':
                    index += 2
                elif text.startswith(char * width, index):
                    index += width
                    if triple:
                        for _ in range(2):
                            if index < len(text) and text[index] == char:
                                index += 1
                            else:
                                break
                    break
                else:
                    index += 1
            tokens.append(('string', start, index))
        elif char in '\n[]=.,{}':
            tokens.append((char, start, start + 1))
            index += 1
        else:
            while index < len(text) and text[index] not in ' \t\r\n[]=.,{}#\"\'':
                index += 1
            tokens.append(('bare', start, index))
    return tokens


def _toml_key(raw):
    value, keys = tomllib.loads(raw + ' = 0'), []
    while isinstance(value, dict):
        key, value = next(iter(value.items()))
        keys.append(key)
    return tuple(keys)


def _toml_value_end(tokens, start, inline=False):
    first = tokens[start][0]
    if first == 'string':
        return start + 1
    if first in ('[', '{'):
        depth = 0
        for index in range(start, len(tokens)):
            kind = tokens[index][0]
            depth += int(kind in ('[', '{')) - int(kind in (']', '}'))
            if not depth:
                return index + 1
    index = start
    while index < len(tokens) and tokens[index][0] != '\n':
        if inline and tokens[index][0] in (',', '}'):
            break
        index += 1
    return index


def _skill_array_span(text):
    """Find an assigned skills.config array, including inline table ancestry."""
    tokens = _toml_tokens(text)
    wanted = ('skills', 'config')

    def inspect_value(start, end, path):
        if path == wanted and tokens[start][0] == '[':
            return tokens[start][1], tokens[end - 1][1]
        if path != wanted[:len(path)] or tokens[start][0] != '{':
            return None
        index = start + 1
        while index < end - 1:
            if tokens[index][0] in ('\n', ','):
                index += 1
                continue
            equal = index
            while tokens[equal][0] != '=':
                equal += 1
            key = _toml_key(text[tokens[index][1]:tokens[equal][1]].strip())
            stop = _toml_value_end(tokens, equal + 1, inline=True)
            found = inspect_value(equal + 1, stop, path + key)
            if found:
                return found
            index = stop
        return None

    index, table = 0, ()
    while index < len(tokens):
        if tokens[index][0] == '\n':
            index += 1
            continue
        if tokens[index][0] == '[':
            width = 2 if tokens[index + 1][0] == '[' else 1
            close = index + width
            while tokens[close][0] != ']':
                close += 1
            table = _toml_key(text[tokens[index + width][1]:tokens[close][1]].strip())
            index = close + width
            continue
        equal = index
        while tokens[equal][0] != '=':
            equal += 1
        key = _toml_key(text[tokens[index][1]:tokens[equal][1]].strip())
        stop = _toml_value_end(tokens, equal + 1)
        found = inspect_value(equal + 1, stop, table + key)
        if found:
            return found
        index = stop
    return None


def project_skill_config(project, skill_entries):
    """Build validated exact-path opt-ins without replacing custom TOML values."""
    config = file_target(project_target(project, project / '.codex/config.toml'))
    existing = config.read_text(encoding='utf-8') if config.exists() else ''
    try:
        tomllib.loads(existing)
        inline_array = _skill_array_span(existing)
        # Retain only selections previously owned by this installer. Discovering
        # other project folders must not enable a user's intentionally disabled
        # domain skills when adding an engineering/vendor pack.
        previous = []
        if SKILLS_BEGIN in existing and SKILLS_END in existing:
            owned = existing.split(SKILLS_BEGIN, 1)[1].split(SKILLS_END, 1)[0]
            owned_entries = (tomllib.loads('entries = [\n' + owned + '\n]')['entries'] if inline_array
                             else tomllib.loads(owned).get('skills', {}).get('config', []))
            for entry in owned_entries:
                if entry.get('enabled') is True and isinstance(entry.get('path'), str):
                    selected = Path(entry['path'])
                    # Managed absolute selectors are machine-local. A moved
                    # checkout rebuilds them without reading/writing the old
                    # location; only local existing selections carry forward.
                    if selected.is_absolute() and selected.is_relative_to(project):
                        previous.append(project_target(project, selected))
        entries = [SKILLS_BEGIN]
        for path in sorted(set(previous + list(skill_entries))):
            file_target(project_target(project, path))
            encoded = json.dumps(str(path), ensure_ascii=False)
            entries += (['{path = ' + encoded + ', enabled = true},'] if inline_array else
                        ['[[skills.config]]', 'path = ' + encoded, 'enabled = true', ''])
        entries.append(SKILLS_END)
        if inline_array:
            # Replace only our array members. Keep the user's original array,
            # inline parent tables, comments and unrelated TOML source intact.
            base = existing
            if SKILLS_BEGIN in existing:
                managed_text(existing, SKILLS_BEGIN, SKILLS_END, '')  # Validate delimiter counts.
                begin = existing.index(SKILLS_BEGIN)
                end = existing.index(SKILLS_END) + len(SKILLS_END)
                if begin and existing[begin - 1] == '\n':
                    begin -= 1
                if end < len(existing) and existing[end] == '\n':
                    end += 1
                base = existing[:begin] + existing[end:]
            elif SKILLS_END in existing:
                raise ValueError('Malformed managed project skill selection')
            opening, closing = _skill_array_span(base)
            significant = [token for token in _toml_tokens(base)
                           if token[1] > opening and token[2] <= closing and token[0] != '\n']
            if significant and significant[-1][0] != ',':
                offset = significant[-1][2]
                base = base[:offset] + ',' + base[offset:]
                closing += 1
            updated = base[:closing] + '\n' + '\n'.join(entries) + '\n' + base[closing:]
        else:
            updated = managed_text(existing, SKILLS_BEGIN, SKILLS_END, '\n'.join(entries))
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as ex:
        raise ValueError('Project config must be valid TOML before activation: ' + str(ex)) from ex
    return config, updated.encode('utf-8')


TARGETS = {
    'codex': ('.agents/skills', '.codex/agents', 'AGENTS.md'),
    'claude': ('.claude/skills', '.claude/agents', 'CLAUDE.md'),
    'copilot': ('.github/skills', '.github/agents', '.github/copilot-instructions.md'),
}


# A private installation keeps every framework file out of the project's Git history: instructions go
# to local-only files and all owned paths are listed in .git/info/exclude, which is never committed.
PRIVATE_POINTERS = {'codex': 'AGENTS.override.md', 'claude': 'CLAUDE.local.md',
                    'copilot': '.github/instructions/devframework.instructions.md'}
COPILOT_HEADER = '---\napplyTo: "**"\n---\n'


def git_top(project):
    """The nearest directory holding .git (a repository or worktree root), or None."""
    for parent in [project, *project.parents]:
        if (parent / '.git').exists():
            return parent
    return None


def _fold():
    """Path comparison as the file system sees it (Windows and macOS default to case-insensitive)."""
    return (lambda value: value.casefold()) if os.name == 'nt' or sys.platform == 'darwin' else (lambda value: value)


def _tracked(top):
    """Tracked paths, folded as the file system compares them, mapped to the index spelling."""
    from ase.inspection import _run
    fold = _fold()
    return {fold(p): p for p in _run(Path(top), ['ls-files', '--cached', '-z']).decode('utf-8', 'strict').split('\0') if p}


def _exclude_pattern(relative, folders, wholes):
    """Anchored pattern: an owned whole folder is one entry; a skill folder gets one entry per child."""
    parts = relative.split('/')
    pattern = relative
    for whole in wholes:
        head = whole.split('/')
        if parts[:len(head)] == head and len(parts) > len(head):
            pattern = whole + '/'
            break
    else:
        for folder in folders:
            head = folder.split('/')
            if parts[:len(head)] == head and len(parts) > len(head):
                pattern = '/'.join(parts[:len(head) + 1]) + ('/' if len(parts) > len(head) + 1 else '')
                break
    return '/' + re.sub(r'([*?\[\\!#])', r'\\\1', pattern)


def private_exclude(project, owned, folders=(), wholes=()):
    """Plan the exclude file for owned project paths; refuse paths Git already tracks.

    Returns None outside Git. ``folders`` (project-relative) collapse to one pattern per child and
    ``wholes`` to one pattern each. Earlier patterns are kept, so installing another target or vendor
    pack never re-exposes files.
    """
    top = git_top(project)
    if top is None:
        return None
    from ase.inspection import _run
    fold, tracked = _fold(), _tracked(top)
    prefix = project.relative_to(top).as_posix()
    prefix = '' if prefix == '.' else prefix + '/'
    relative = sorted({Path(path).resolve().relative_to(top).as_posix() for path in owned})
    clash = [path for path in relative if fold(path) in tracked]
    if clash:
        raise ValueError('Private installation refuses files already tracked by Git (nothing was changed); '
                         'remove them from the repository first: ' + ', '.join(clash[:10]))
    folders = [prefix + folder for folder in folders]
    wholes = [prefix + whole for whole in wholes]
    samples = {}
    for path in relative:
        samples.setdefault(_exclude_pattern(path, folders, wholes), path)
    exclude = (top / _run(top, ['rev-parse', '--git-path', 'info/exclude']).decode('utf-8', 'strict').strip()).resolve()
    scope = exclude.parent.parent
    _safe_destination(scope, exclude)
    current = exclude.read_text(encoding='utf-8') if exclude.exists() else ''
    previous = []
    if PRIVATE_BEGIN in current:
        managed_text(current, PRIVATE_BEGIN, PRIVATE_END, '')  # Validate delimiters.
        previous = [line for line in current.split(PRIVATE_BEGIN, 1)[1].split(PRIVATE_END, 1)[0].splitlines() if line.strip()]
    patterns = list(dict.fromkeys(previous + list(samples)))
    block = '\n'.join([PRIVATE_BEGIN, *patterns, PRIVATE_END])
    return {'path': exclude, 'scope': scope, 'data': managed_text(current, PRIVATE_BEGIN, PRIVATE_END, block).encode('utf-8'),
            'paths': relative, 'samples': sorted(samples.values()), 'patterns': patterns, 'top': top}


def check_still_untracked(top, paths):
    """Apply-time recheck: a file tracked since the preview would be modified in Git history."""
    fold, tracked = _fold(), _tracked(top)
    clash = [path for path in paths if fold(path) in tracked]
    if clash:
        raise ValueError('Private installation refuses files tracked by Git since the preview: ' + ', '.join(clash[:10]))


def check_ignored(top, samples):
    """After the exclude file is written: the project's own .gitignore negations must not re-expose a file."""
    from ase.inspection import _run
    done = _run(Path(top), ['check-ignore', '-z', '--no-index', '--stdin'], check=False,
                input=''.join(path + '\0' for path in samples).encode('utf-8'))
    ignored = {p for p in done.decode('utf-8', 'strict').split('\0') if p}
    visible = [path for path in samples if path not in ignored]
    if visible:
        raise ValueError('Project ignore rules would show framework files to Git, so nothing else was installed '
                         '(only the local exclude file changed); fix the negating .gitignore rule first: '
                         + ', '.join(visible[:10]))


def _without_block(text):
    """Remove an earlier shared install's managed block from a team instruction file."""
    if BEGIN not in text:
        return text
    managed_text(text, BEGIN, END, '')  # Validate delimiters.
    before, rest = text.split(BEGIN, 1)
    after = rest.split(END, 1)[1]
    if not after.strip():
        return before.rstrip() + '\n' if before.strip() else ''
    return before.rstrip('\n') + ('\n\n' if before.strip() else '') + after.lstrip('\n')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def _manifest_hash(manifest):
    return _sha(json.dumps({k: v for k, v in manifest.items() if k != 'manifest_sha256'},
                           sort_keys=True, separators=(',', ':')).encode('utf-8'))


def _safe_destination(scope, target):
    """Scope checks include directory aliases, even when they remain in-scope."""
    return file_target(project_target(scope, target))


def _payload(target, data, *, scope, backup_root, index, delete=False):
    target = _safe_destination(scope, target)
    before = target.read_bytes() if target.exists() else None
    changed = before is not None if delete else before != data
    backup = backup_root / str(index) / target.name if changed and before is not None else None
    return {'path': str(target), 'scope_root': str(scope), 'sha256': _sha(data),
            'before_sha256': _sha(before) if before is not None else None,
            'content_base64': base64.b64encode(data).decode('ascii'),
            'changed': changed, 'backup': str(backup) if backup else None, 'delete': delete}


PRIVATE_RULE = (' This framework is the owner\'s private local tool: its skills, agents, instructions, memory and '
                'runtime are excluded from Git here (.git/info/exclude). Push only the task\'s own changes. Never stage, '
                'commit, push or open a pull request with framework files, never add framework CI, hooks or workflow '
                'files to this repository, and do not mention the framework, ase, its gate, lanes or evidence records '
                'in commits, pull request descriptions, code, comments or messages to other people.')
CODEX_OVERRIDE_RULE = (' This local AGENTS.override.md replaces AGENTS.md for Codex; read and follow this directory\'s '
                       'AGENTS.md as the project\'s own instructions.')
# Always loaded, so it also reaches read-only investigations and subagents that never open the skill.
SOURCE_FIRST_RULE = ('When you investigate a fault or recommend a fix, also as a subagent or for a quick fix: read how the '
                     'callee/source already handles the same condition (sibling paths, fallbacks) and recommend fixing it there; '
                     'treat a lead\'s suggested fix as a hypothesis. No consumer-side retry, wait, timeout or compensation as the '
                     'fix for a source fault while the source can be fixed, unless a requirement or documented contract prescribes it. ')
FAILURE_DECISION_RULE = ('Hold a patch that depends on an unresolved owner decision. If a new error can occur after a '
                         'write commits, check what persisted and what retry does. A similar call elsewhere does not '
                         'authorize a new failure in this flow. Unless this flow has an agreed failure policy, give the '
                         'owner the options and hold the dependent diff. An inaccessible source does not justify guessing '
                         'a correction to money, permissions or stored data. If two interpretations fit the same input, '
                         'show that input and its different outcomes, then get the fallback approved. Verify deployment and retries '
                         'before claiming single execution. A schedule or one replica setting alone cannot rule out repeats or overlap. ')
OUT_OF_SCOPE_RULE = ('A defect outside the ticket that this change did not cause is reported in one line asking whether '
                     'it is needed at all; without the owner\'s explicit permission for that defect it is never fixed, '
                     'committed, given the ticket key or put in any branch or PR, current or separate. If it blocks a check '
                     'of a denial goal and a working control refuses with a 4xx (never 5xx, timeout or empty 200), judge the '
                     'goal while still testing the task\'s own code; otherwise it is a blocker for the owner. An exact status '
                     'code counts when it is itself the requirement. ')
HELPER_RULE = ('Each new domain conversion or magic value (date/code formats, units, external-system numbers or keys) '
               'uses an existing helper or constant, goes into the existing helper class of its domain, or becomes a named '
               'constant with a reason; self-checks, reviews and cleanup passes check every one, not only duplicates. When '
               'asked whether something exists, search first and answer with the fact. ')


# The owner's short project hints, kept in one local file and copied into every routing block.
# Measured in eval round 11 (outputs/chain-eval-20261124/hints): a few structural lines about what
# ordinary search misses and whom to ask helped; generated maps did not.
HINTS_FILE = '.devframework/hints.md'
MAX_HINTS_BYTES = 4096
HINTS_INTRO = ('Owner\'s project hints (local, never commit; they never relax the rules above; check any claim about '
               'the code in the code, and propose a new line to the owner when you had to discover something these '
               'hints miss):\n')


def project_hints(project):
    """The owner's hints text, or '' when the file is absent or blank."""
    path = _safe_destination(project, project / HINTS_FILE)
    if not path.exists():
        return ''
    data = path.read_bytes()
    if len(data) > MAX_HINTS_BYTES:
        raise ValueError(HINTS_FILE + ' exceeds ' + str(MAX_HINTS_BYTES) + ' bytes; keep it to a few lines')
    try:
        text = data.decode('utf-8-sig').replace('\r\n', '\n').strip()
    except UnicodeError as error:
        raise ValueError(HINTS_FILE + ' must be UTF-8 text') from error
    if any(ord(char) < 32 and char not in '\t\n' for char in text):
        raise ValueError(HINTS_FILE + ' must be plain text without control characters')
    if BEGIN in text or END in text:
        raise ValueError(HINTS_FILE + ' must not contain managed block markers')
    return text


def _routing(skill_root, project_local=False, private=False, codex_override=False, hints=''):
    return (BEGIN + '\nThis workspace explicitly adopts DevelopmentFramework for managed tasks. '
            'Use ' + skill_root + '/task-orchestrator/SKILL.md and the appropriate domain skills. '
            'Choose software only for actual code or configuration implementation; documents, financial analysis, '
            'research and infrastructure use their own acceptance profiles. Preserve project rules and user authorization. '
            'Restore local context, propose the outcome concisely, and reuse an approved implementation plan for the same scope. '
            'Before asking about a missing fact, inspect supplied context and relevant authorized sources or environment with safe read-only checks. '
            'Keep the original chat as lead, process its durable ase events inbox and return results through available native tools. '
            'For software, plan relevant risk scenarios and require current independent test adequacy alongside acceptance/review. '
            'Distinguish retrievable facts from owner-only decisions; ask only for a material remaining gap, with checked evidence and a concise hypothesis/recommendation. '
            + SOURCE_FIRST_RULE + FAILURE_DECISION_RULE + OUT_OF_SCOPE_RULE + HELPER_RULE +
            'Independent acceptance and review plus the evidence gate are required for verified completion.'
            + (' Use this project\'s .devframework/bin/ase.ps1 (PowerShell) or .devframework/bin/ase (shell) '
               'for framework commands; .devframework/config.toml selects project-local configuration and '
               '.devframework/local holds private state. Do not substitute a shared/global runtime or state home.'
               if project_local else '')
            + (PRIVATE_RULE if private else '') + (CODEX_OVERRIDE_RULE if codex_override else '')
            + ('\n\n' + HINTS_INTRO + hints if hints else '') + '\n' + END)


def _launcher_bytes(runtime):
    py = str(Path(sys.executable)).replace("'", "''")
    location = str(runtime).replace("'", "''")
    powershell = ("$aseForward = @()\n"
                  "for ($aseIndex = 0; $aseIndex -lt $args.Count; $aseIndex++) {\n"
                  "  $aseArg = [string]$args[$aseIndex]\n"
                  "  if ($aseArg -match '^--home=[A-Za-z]$' -and $aseIndex + 1 -lt $args.Count -and (([string]$args[$aseIndex + 1]).StartsWith('\\') -or ([string]$args[$aseIndex + 1]).StartsWith('/'))) {\n"
                  "    $aseIndex++; $aseArg = $aseArg + ':' + [string]$args[$aseIndex]\n"
                  "  }\n  $aseForward += $aseArg\n}\n"
                  "$aseHadPythonPath = Test-Path Env:PYTHONPATH\n"
                  "$aseOldPythonPath = $env:PYTHONPATH\n"
                  "try {\n"
                  f"  $env:PYTHONPATH = '{location}' + $(if ($aseOldPythonPath) {{ [IO.Path]::PathSeparator + $aseOldPythonPath }})\n"
                  + _powershell_call(f"& '{py}' -P -m ase @aseForward") +
                  "  $aseExitCode = $LASTEXITCODE\n"
                  "} finally {\n"
                  "  if ($aseHadPythonPath) { $env:PYTHONPATH = $aseOldPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }\n"
                  "}\nexit $aseExitCode\n")
    shell = ('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -P -c ' + shlex.quote(_SHELL_BOOTSTRAP)
             + ' ' + shlex.quote(str(runtime)) + ' "$@"\n')
    return powershell.encode('utf-8'), shell.encode('utf-8')


def _powershell_call(command):
    """Forward piped input as UTF-8: Windows PowerShell encodes it with the global $OutputEncoding (ASCII)."""
    return ("if ($MyInvocation.ExpectingInput) {\n"
            "  $aseOldOutputEncoding = $global:OutputEncoding\n"
            "  try { $global:OutputEncoding = New-Object System.Text.UTF8Encoding $false; $input | " + command + " }\n"
            "  finally { $global:OutputEncoding = $aseOldOutputEncoding }\n"
            "} else { " + command + " }\n")


# The runtime travels as an argument, not PYTHONPATH: its separator differs between a POSIX shell
# and a Windows interpreter (Git Bash), and the caller's PYTHONPATH stays as it was.
_SHELL_BOOTSTRAP = ('import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); '
                    'runpy.run_module("ase", run_name="__main__", alter_sys=True)')


def _local_launcher_bytes():
    """Use launcher-relative locations; restore PowerShell's process environment."""
    py = str(Path(sys.executable)).replace("'", "''")
    powershell = (
        "$aseRuntime = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../autonomous-se/runtime'))\n"
        "$aseProject = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))\n"
        "if (-not (Test-Path -LiteralPath (Join-Path $aseProject '.devframework/config.toml') -PathType Leaf)) {\n"
        "  throw 'Project-local installation requires its config.toml'\n}\n"
        "$asePython = $null\n"
        f"foreach ($aseCandidate in @('{py}', 'python3', 'python')) {{\n"
        "  $aseCommand = Get-Command $aseCandidate -ErrorAction SilentlyContinue\n"
        "  if ($aseCommand) {\n"
        "    & $aseCommand.Source -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null\n"
        "    if ($LASTEXITCODE -eq 0) { $asePython = $aseCommand.Source; break }\n"
        "  }\n}\n"
        "if (-not $asePython) { throw 'Python 3.11+ is required' }\n"
        "$aseForward = @()\n"
        "for ($aseIndex = 0; $aseIndex -lt $args.Count; $aseIndex++) {\n"
        "  $aseArg = [string]$args[$aseIndex]\n"
        "  if ($aseArg -match '^--home=[A-Za-z]$' -and $aseIndex + 1 -lt $args.Count -and (([string]$args[$aseIndex + 1]).StartsWith('\\') -or ([string]$args[$aseIndex + 1]).StartsWith('/'))) {\n"
        "    $aseIndex++; $aseArg = $aseArg + ':' + [string]$args[$aseIndex]\n"
        "  }\n  $aseForward += $aseArg\n}\n"
        "if (-not ($aseForward | Where-Object { $_ -match '^--repo(=|$)' })) {\n"
        "  $aseForward = @('--repo', $aseProject) + $aseForward\n}\n"
        "$aseHadPythonPath = Test-Path Env:PYTHONPATH\n"
        "$aseOldPythonPath = $env:PYTHONPATH\n"
        "try {\n"
        "  $env:PYTHONPATH = $aseRuntime + $(if ($aseOldPythonPath) { [IO.Path]::PathSeparator + $aseOldPythonPath })\n"
        + _powershell_call("& $asePython -P -m ase @aseForward") +
        "  $aseExitCode = $LASTEXITCODE\n"
        "} finally {\n"
        "  if ($aseHadPythonPath) { $env:PYTHONPATH = $aseOldPythonPath } else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }\n"
        "}\nexit $aseExitCode\n")
    shell = (
        '#!/bin/sh\n'
        'ase_bin=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P) || exit 1\n'
        'ase_runtime="$ase_bin/../autonomous-se/runtime"\n'
        'ase_project=$(CDPATH= cd -- "$ase_bin/../.." && pwd -P) || exit 1\n'
        '[ -f "$ase_project/.devframework/config.toml" ] || { echo "Project-local installation requires its config.toml" >&2; exit 1; }\n'
        'ase_python=\n'
        'for ase_candidate in ' + shlex.quote(sys.executable) + ' python3 python; do\n'
        '  if command -v "$ase_candidate" >/dev/null 2>&1 && "$ase_candidate" -c \'import sys; sys.exit(sys.version_info < (3, 11))\' 2>/dev/null; then\n'
        '    ase_python=$ase_candidate; break\n  fi\ndone\n'
        '[ -n "$ase_python" ] || { echo "Python 3.11+ is required" >&2; exit 1; }\n'
        'ase_selected=\n'
        'for ase_arg do\n  case "$ase_arg" in --repo|--repo=*) ase_selected=1;; esac\ndone\n'
        '[ -n "$ase_selected" ] || set -- --repo "$ase_project" "$@"\n'
        'exec "$ase_python" -P -c ' + shlex.quote(_SHELL_BOOTSTRAP) + ' "$ase_runtime" "$@"\n')
    return powershell.encode('utf-8'), shell.encode('utf-8')


def _local_config_bytes(path, targets, timezone):
    if not path.exists():
        return config_bytes(targets, timezone='local' if timezone is None else timezone)
    data = path.read_bytes()
    existing = data.decode('utf-8')
    try:
        parsed = validate_local_settings(tomllib.loads(existing))
        updated_targets = list(dict.fromkeys(parsed['targets'] + targets))
        desired = {'targets': updated_targets, 'timezone': parsed['timezone'] if timezone is None else timezone}
        config_bytes(updated_targets, timezone=desired['timezone'], log_retention_days=parsed['log_retention_days'])
        # Strict root fields permit changing only the chosen value spans while
        # preserving comments, whitespace, retention and the user's timezone.
        tokens = _toml_tokens(existing)
        replacements, index = [], 0
        while index < len(tokens):
            if tokens[index][0] == '\n':
                index += 1
                continue
            equal = index
            while tokens[equal][0] != '=':
                equal += 1
            key = _toml_key(existing[tokens[index][1]:tokens[equal][1]].strip())
            stop = _toml_value_end(tokens, equal + 1)
            if len(key) == 1 and key[0] in desired and parsed[key[0]] != desired[key[0]]:
                replacements.append((tokens[equal + 1][1], tokens[stop - 1][2], json.dumps(desired[key[0]], ensure_ascii=False)))
            index = stop
        for start, end, value in reversed(replacements):
            existing = existing[:start] + value + existing[end:]
        tomllib.loads(existing)
        return existing.encode('utf-8') if replacements else data
    except (tomllib.TOMLDecodeError, TypeError, KeyError, UnicodeError) as ex:
        raise ValueError('Invalid project-local config: ' + str(ex)) from ex


def _check_tracked_private(project):
    if any((parent / '.git').exists() for parent in [project, *project.parents]):
        from ase.inspection import _run
        tracked = _run(project, ['ls-files', '--cached', '-z']).decode('utf-8', 'strict').split('\0')
        if any(is_private_path(path) for path in tracked if path):
            raise ValueError('Private project-local state must not be tracked by Git')


def _package_file(source):
    relative = source.relative_to(ROOT)
    selected = (source.is_file() and source.suffix != '.pyc'
                and '__pycache__' not in relative.parts and not is_private_path(relative)
                and relative.parts[:2] != ('scripts', 'eval') and relative != FRAMEWORK_SOURCE)
    if selected:
        if source.stat().st_nlink > 1:
            raise ValueError('Canonical package contains hardlinked source files: ' + str(source))
        if _is_link(source) or any(_is_link(p) for p in source.parents if p.is_relative_to(ROOT)):
            raise ValueError('Canonical package contains linked source files: ' + str(source))
    return selected


def framework_source():
    """The commit this install copies, and whether any copied file differs from it.

    Untracked files count: the installer copies every package file, tracked or not. Installing from an
    installed runtime (no checkout of its own) carries that runtime's recorded commit forward.
    """
    if not (ROOT / '.git').exists():
        return recorded_source(ROOT / FRAMEWORK_SOURCE) or {'commit': UNKNOWN, 'dirty': True}
    status = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--', *PACKAGE_DIRECTORIES],
                            timeout=GIT_TIMEOUT_SECONDS,
                            capture_output=True, check=False)
    return {'commit': source_commit(ROOT), 'dirty': status.returncode != 0 or bool(status.stdout.strip())}


def installed_commit(manifest):
    from ase.doctor import installed_commit as recorded
    return recorded(manifest.get('runtime_home'))


def post_update_check(manifest, previous_commit):
    """Run the self-check on what was just installed; a failing check means the update is incomplete."""
    from ase import doctor
    try:
        # This checkout vouches for the install only while it is clean and still at the planned commit.
        source, note = doctor.vouching_source(ROOT, manifest.get('framework_commit'), manifest.get('framework_dirty'))
        return doctor.run(home=Path(manifest['runtime_home']), package_root=Path(manifest['runtime']),
                          source_root=source, source_note=note, project=manifest.get('project'),
                          previous_commit=previous_commit)
    except Exception as error:  # The install is already written: report it as incomplete, never crash.
        return doctor.failed_report(previous_commit, manifest.get('framework_commit'), error)


def plan_install(targets: list[str], project, *, runtime_home: Path = None, project_local=False, timezone=None) -> dict:
    """Build a complete read-only preview for explicit portable backend targets.

    ``project`` accepts a registered ProjectContext, an explicit existing path,
    or None for runtime-only preparation. Registration itself is not performed.
    """
    return _plan_install(targets, project, runtime_home=runtime_home, project_local=project_local, timezone=timezone)


def _plan_install(targets, project, *, runtime_home=None, legacy_global=False, skill_home=None,
                  project_local=False, timezone=None):
    if not targets or any(target not in TARGETS for target in targets):
        raise ValueError('Select one or more explicit targets: codex, claude, copilot')
    targets = list(dict.fromkeys(targets))
    context = project if project is not None and hasattr(project, 'project_id') and hasattr(project, 'root') else None
    if project is not None:
        project = Path(context.root if context else project).expanduser().resolve(strict=True)
        if not project.is_dir() or any(c in str(project) for c in '\r\n\0'):
            raise ValueError('Supply an existing project directory')
    if project_local:
        if project is None or legacy_global or skill_home is not None:
            raise ValueError('Project-local installation requires one selected project and project destinations')
        home = project / '.devframework'
        if runtime_home is not None and Path(runtime_home).expanduser().absolute() != home:
            raise ValueError('Project-local tooling home must be exactly project/.devframework')
        directory_target(project_target(project, home))
        directory_target(project_target(project, home / 'local/backups'))
        _check_tracked_private(project)
    else:
        if runtime_home is None:
            raise ValueError('Select an explicit shared tooling home')
        home = Path(runtime_home).expanduser().resolve()
        directory_target(home)
    if not project_local and project is not None and (home == project or home.is_relative_to(project)):
        raise ValueError('Shared tooling home must be outside the selected project')
    if legacy_global and (project is not None or targets != ['codex']):
        raise ValueError('Legacy global activation is an explicit Codex-only mode')
    runtime = home / 'autonomous-se/runtime'
    stamp = _stamp()
    backup_root = home / ('local/backups/autonomous-se' if project_local else 'backups/autonomous-se') / stamp
    planned, seen = [], set()

    def add(target, data, scope=home, delete=False):
        item = _payload(target, data, scope=scope, backup_root=backup_root, index=len(planned), delete=delete)
        if item['path'] in seen:
            raise ValueError('Duplicate managed installation destination: ' + item['path'])
        if item['backup']:
            _safe_destination(home, Path(item['backup']))
        seen.add(item['path'])
        planned.append(item)

    if project_local:
        config = _safe_destination(project, home / 'config.toml')
        add(config, _local_config_bytes(config, targets, timezone))
        ignore = _safe_destination(project, home / '.gitignore')
        current = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
        # Append after existing rules, so an earlier negation cannot undo it.
        lines = [line for line in current.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        updated = current if lines and lines[-1] == '/local/' else current.rstrip() + '\n/local/\n'
        add(ignore, updated.encode('utf-8'))

    for directory in PACKAGE_DIRECTORIES:
        for source in sorted((ROOT / directory).rglob('*')):
            if _package_file(source):
                add(runtime / source.relative_to(ROOT), source.read_bytes())
    framework = framework_source()
    add(runtime / FRAMEWORK_SOURCE, json.dumps(framework, indent=2).encode('utf-8'))
    instruction_files, destinations = {}, {}
    top = git_top(project) if project is not None and not legacy_global else None
    # Outside its own source repository the framework is the owner's private tool: in any Git
    # project nothing it installs may reach that project's history.
    private = top is not None and not is_framework_source(top)
    cleaned, owner_cleanup = [], []
    tracked = _tracked(top) if private else {}
    # Only a private install keeps them out of history: in the framework's own repository or a project
    # outside Git the routing block lands in a shared instruction file.
    hints = project_hints(project) if private else ''
    for target in targets if project is not None else []:
        skill_relative, role_relative, pointer = TARGETS[target]
        skill_directory = directory_target(project_target(project, project / skill_relative))
        directory_target(project_target(project, project / role_relative))
        destinations[target] = str(skill_directory)
        instruction = project / (PRIVATE_POINTERS[target] if private else pointer)
        if target == 'codex' and not private:
            override = _safe_destination(project, project / 'AGENTS.override.md')
            if override.is_file() and override.read_text(encoding='utf-8').strip():
                instruction = override
        instruction = _safe_destination(project, instruction)
        current = instruction.read_text(encoding='utf-8') if instruction.exists() else ''
        if private and target == 'copilot' and not current.startswith('---'):
            current = COPILOT_HEADER + current
        routing = _routing(skill_relative, project_local, private, codex_override=private and target == 'codex',
                           hints=hints)
        add(instruction, managed_text(current, BEGIN, END, routing).encode('utf-8'), project)
        instruction_files[target] = str(instruction)
        if private:
            # An earlier shared install left its block in the team's own instruction file. An uncommitted
            # block is removed; a committed one is left for the owner, since editing it would put a
            # framework-related change into a task commit.
            shared = _safe_destination(project, project / pointer)
            if shared.is_file() and BEGIN in shared.read_text(encoding='utf-8'):
                indexed = tracked.get(_fold()(shared.relative_to(top).as_posix()))
                rest = _without_block(shared.read_text(encoding='utf-8'))
                if indexed is not None and rest != (_plain_git(top, 'show', 'HEAD:' + indexed) or ''):
                    # Committed, or cleaning would not restore the committed bytes: any edit would be a
                    # framework-related change in the team's history.
                    owner_cleanup.append(str(shared))
                else:
                    add(shared, rest.encode('utf-8'), project, delete=not rest.strip() and indexed is None)
                    cleaned.append(str(shared))
        entries = []
        for source in sorted((ROOT / 'skills').rglob('*')):
            if _package_file(source):
                destination = skill_directory / source.relative_to(ROOT / 'skills')
                add(destination, source.read_bytes(), project)
                if source.name == 'SKILL.md':
                    entries.append(destination)
        roles = ROOT / ('agents' if target == 'codex' else 'templates/agents/' + target)
        for source in sorted(roles.glob('*.toml' if target == 'codex' else '*.md')):
            if _package_file(source):
                add(project / role_relative / source.name, source.read_bytes(), project)
        if target == 'codex':
            config, data = project_skill_config(project, entries)
            add(config, data, project)
    if legacy_global:
        destination = Path(skill_home).expanduser().resolve() if skill_home else (Path.home() / '.agents/skills').resolve()
        directory_target(destination)
        destinations['codex'] = str(destination)
        for source in sorted((ROOT / 'skills').rglob('*')):
            if _package_file(source):
                add(destination / source.relative_to(ROOT / 'skills'), source.read_bytes(), destination)
        for source in sorted((ROOT / 'agents').glob('*.toml')):
            if not _package_file(source):
                continue
            role = tomllib.loads(source.read_text(encoding='utf-8'))
            if 'developer_instructions' in role:
                role['developer_instructions'] = role['developer_instructions'].replace(
                    '.agents/skills/', destination.as_posix().rstrip('/') + '/')
            content = '\n'.join(key + ' = ' + json.dumps(value) for key, value in role.items()) + '\n'
            add(home / 'agents' / source.name, content.encode('utf-8'))
        instruction = _safe_destination(home, home / 'AGENTS.md')
        current = instruction.read_text(encoding='utf-8') if instruction.exists() else ''
        add(instruction, managed_text(current, BEGIN, END, _routing(str(destination))).encode('utf-8'))
        instruction_files['codex'] = str(instruction)
    ps, shell = _local_launcher_bytes() if project_local else _launcher_bytes(runtime)
    add(home / 'bin/ase.ps1', ps)
    add(home / 'bin/ase', shell)
    exclude = None
    if private:
        owned = [Path(item['path']) for item in planned if Path(item['path']).is_relative_to(project)
                 and item['path'] not in cleaned]
        if home.is_relative_to(project):
            owned.append(home / 'autonomous-se/installation.json')
        # The owner writes the hints file, possibly after this install: exclude it either way.
        owned.append(project / HINTS_FILE)
        exclude = private_exclude(project, owned, folders=[TARGETS[t][0] for t in targets],
                                  wholes=['.devframework'] if project_local else [])
        add(exclude['path'], exclude['data'], exclude['scope'])
    summary = {'runtime': str(runtime), 'targets': targets, 'project_local': bool(project_local),
               'scope': 'global' if legacy_global else 'project' if project is not None else 'runtime',
               'project': str(project) if project is not None else None,
               'project_id': getattr(context, 'project_id', None),
               'skills': destinations.get('codex'), 'skill_destinations': destinations,
               'instruction_file': instruction_files.get('codex'), 'instruction_files': instruction_files,
               'launcher': str(home / 'bin/ase.ps1'), 'remote_push_enabled': False,
               'private': private, 'git_exclude': str(exclude['path']) if exclude else None,
               'git_exclude_scope': str(exclude['scope']) if exclude else None,
               'git_top': str(exclude['top']) if exclude else None,
               'private_patterns': exclude['patterns'] if exclude else [],
               'hints': str(project / HINTS_FILE) if hints else None,
               'framework_commit': framework['commit'], 'framework_dirty': framework['dirty'],
               'framework_source_root': str(ROOT)}
    # Stable ownership inventory permits a byte-identical reapplication. Per-run
    # timestamps and backups belong to the preview/result, not this installed file.
    # A team instruction file merely cleaned of an earlier block is not an owned file.
    ownership = {**summary, 'owned_files': [{'path': item['path'], 'sha256': item['sha256']} for item in planned
                                            if item['path'] not in cleaned]}
    add(home / 'autonomous-se/installation.json', json.dumps(ownership, indent=2).encode('utf-8'))
    policy_path = runtime / 'ase/data/runtime_policy.json'
    review_policy_changed = any(Path(item['path']) == policy_path and item['changed']
                                and item['before_sha256'] is not None for item in planned)
    manifest = {**summary, 'manifest_version': 1, 'kind': 'install', 'runtime_home': str(home),
                'review_policy_changed': review_policy_changed,
                'planned_at': stamp, 'backup': str(backup_root), 'files': planned, 'removed_shared_blocks': cleaned,
                'private_paths': exclude['paths'] if exclude else [],
                # Committed blocks stay until the owner removes them in a commit of their own, outside any task.
                'owner_cleanup': owner_cleanup,
                'changed': [item['path'] for item in planned if item['changed']]}
    manifest['manifest_sha256'] = _manifest_hash(manifest)
    return manifest


def apply_install(manifest: dict) -> dict:
    """Apply exactly a preview, after preflighting every output and backup."""
    if manifest.get('kind') != 'install' or manifest.get('manifest_version') != 1:
        raise ValueError('Unsupported install manifest')
    if manifest.get('manifest_sha256') != _manifest_hash(manifest):
        raise ValueError('Manifest digest mismatch')
    home = Path(manifest['runtime_home'])
    project = Path(manifest['project']) if manifest.get('project') else None
    if manifest.get('project_local'):
        if project is None or home != project / '.devframework':
            raise ValueError('Invalid project-local manifest home')
        directory_target(project_target(project, home))
        directory_target(project_target(project, home / 'local/backups'))
        if not Path(manifest['backup']).is_relative_to(home / 'local/backups'):
            raise ValueError('Project-local backups must remain private')
        _check_tracked_private(project)
    global_root = Path(manifest['skills']) if manifest.get('scope') == 'global' else None
    allowed_roots = {home, *([project] if project else []), *([global_root] if global_root else [])}
    exclude = None
    if project is not None and manifest.get('scope') == 'project':
        top = git_top(project)
        if ((top is not None and not is_framework_source(top)) != bool(manifest.get('private'))
                or (manifest.get('private') and str(top) != manifest.get('git_top'))):
            raise ValueError('Repository state changed since the preview; plan the installation again')
    if manifest.get('private'):
        exclude = Path(manifest['git_exclude'])
        allowed_roots.add(Path(manifest['git_exclude_scope']))
        check_still_untracked(manifest['git_top'], manifest['private_paths'])
    changes, seen = [], set()
    for item in manifest['files']:
        target, scope = Path(item['path']), Path(item['scope_root'])
        if scope not in allowed_roots or str(target) in seen:
            raise ValueError('Invalid manifest destination scope')
        seen.add(str(target))
        _safe_destination(scope, target)
        data = base64.b64decode(item['content_base64'], validate=True)
        if _sha(data) != item['sha256']:
            raise ValueError('Manifest content hash mismatch')
        before = target.read_bytes() if target.exists() else None
        if (_sha(before) if before is not None else None) != item['before_sha256']:
            raise ValueError('Stale install manifest; destination changed: ' + str(target))
        if item['changed'] != ((before is not None) if item.get('delete') else (before != data)):
            raise ValueError('Manifest changed-file mismatch')
        backup = Path(item['backup']) if item.get('backup') else None
        if backup:
            _safe_destination(home, backup)
            if not backup.is_relative_to(Path(manifest['backup'])) or backup.exists():
                raise ValueError('Backup destination is already occupied or outside the planned backup')
        if item['changed']:
            changes.append((target, None if item.get('delete') else data, backup, scope))
    # Local accident barrier, not a transaction against IO errors or a hostile
    # process changing path objects between checks and writes.
    # Exclusion first: no owned file may appear as an untracked candidate for a commit.
    changes.sort(key=lambda change: (change[0] != exclude,
                                     not (manifest.get('project_local') and change[0] == home / '.gitignore')))
    checked = not manifest.get('private')
    for target, data, backup, scope in changes:
        if not checked and target != exclude:
            # The exclusion is in place (sorted first); every owned path must now be ignored before
            # any of them is written, whatever the project's own .gitignore negates.
            check_ignored(manifest['git_top'], manifest['private_paths'])
            checked = True
        _safe_destination(scope, target)
        # Repair ignore protection before creating any private backup. Keep its
        # old bytes in memory until the new rule is in place, then back them up.
        ignore_backup = bool(backup and manifest.get('project_local') and target == home / '.gitignore')
        ignore_before = target.read_bytes() if ignore_backup else None
        if backup and not ignore_backup:
            _safe_destination(home, backup)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        if data is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if ignore_backup:
            _safe_destination(home, backup)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(ignore_before)
    if sys.platform != 'win32':
        (home / 'bin/ase').chmod(0o755)
    if manifest.get('private'):
        # Backstop: Git must not offer any installed file as an untracked candidate.
        from ase.inspection import _run
        fold = _fold()
        offered = {fold(p) for p in _run(Path(manifest['git_top']), ['ls-files', '--others', '--exclude-standard', '-z'])
                   .decode('utf-8', 'strict').split('\0') if p}
        visible = [path for path in manifest['private_paths'] if fold(path) in offered]
        if visible:
            raise ValueError('Installed framework files are visible to Git; fix the ignore rules: ' + ', '.join(visible[:10]))
    return {key: value for key, value in manifest.items() if key != 'files'}


def install(home: Path, skill_home: Path = None, *, project: Path = None, global_install=False,
            targets=None):
    """Compatibility entrypoint; new callers can review plan/apply separately."""
    if skill_home is not None and not global_install:
        raise ValueError('A custom skill destination requires explicit global installation')
    manifest = _plan_install(targets or ['codex'], project, runtime_home=home,
                             legacy_global=global_install, skill_home=skill_home)
    result = apply_install(manifest)
    print(json.dumps(result, indent=2))
    return result


def register_verifier(*args, **kwargs):
    raise SystemExit('--register-verifier is deprecated: ASE uses local CLI adapters. '
                     'No configuration was changed. Review an explicit MCP cleanup preview separately.')


def _table_spans(text):
    """Find real TOML table headers; quoted/multiline string contents are ignored."""
    tokens, headers = _toml_tokens(text), []
    index = 0
    while index < len(tokens):
        kind, start, end = tokens[index]
        if kind == '\n':
            index += 1
            continue
        if kind == '[':
            width = 2 if tokens[index + 1][0] == '[' else 1
            close = index + width
            while close < len(tokens) and tokens[close][0] != ']':
                close += 1
            key = _toml_key(text[tokens[index + width][1]:tokens[close][1]].strip())
            headers.append((key, text.rfind('\n', 0, start) + 1))
            index = close + width
        else:
            equal = index
            while tokens[equal][0] != '=':
                equal += 1
            index = _toml_value_end(tokens, equal + 1)
    return [(key, start, headers[index + 1][1] if index + 1 < len(headers) else len(text))
            for index, (key, start) in enumerate(headers)]


def plan_mcp_cleanup(config_path: Path) -> dict:
    """Preview only the historical ASE registration, preserving unrelated bytes."""
    path = Path(config_path).expanduser().absolute()
    root = path.parent.resolve()
    path = _safe_destination(root, root / path.name)
    before = path.read_bytes() if path.exists() else b''
    text = before.decode('utf-8')
    parsed = tomllib.loads(text)
    entry = parsed.get('mcp_servers', {}).get('autonomous_se')
    owned = []
    if entry is not None:
        arguments = entry.get('args', []) if isinstance(entry, dict) else []
        runtime = str(entry.get('env', {}).get('PYTHONPATH', '')).replace('\\', '/').rstrip('/') if isinstance(entry, dict) else ''
        module = any(arguments[index:index + 2] == ['-m', 'ase.host'] for index in range(len(arguments)))
        if not module or not runtime.endswith('/autonomous-se/runtime'):
            raise ValueError('Cannot establish ownership of autonomous_se MCP registration')
        for key, start, end in _table_spans(text):
            if key[:2] != ('mcp_servers', 'autonomous_se'):
                continue
            # Comments between tables are user text, not registration syntax.
            lines = text[start:end].splitlines(keepends=True)
            while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith('#')):
                end -= len(lines.pop())
            owned.append((start, end))
        if not owned:
            raise ValueError('Owned registration uses an unsupported inline shape; no changes made')
    updated = text
    for start, end in reversed(owned):
        updated = updated[:start] + updated[end:]
    expected = copy.deepcopy(parsed)
    if entry is not None:
        del expected['mcp_servers']['autonomous_se']
        if not expected['mcp_servers']:
            del expected['mcp_servers']
    actual = tomllib.loads(updated)
    if actual.get('mcp_servers') == {}:
        actual.pop('mcp_servers')
    if actual != expected:
        raise ValueError('MCP cleanup would modify unrelated settings')
    stamp = _stamp()
    backup = root / 'backups/autonomous-se' / stamp / 'config.before-mcp-cleanup.toml'
    if owned:
        _safe_destination(root, backup)
    manifest = {'kind': 'mcp-cleanup', 'manifest_version': 1, 'config_path': str(path),
                'before_sha256': _sha(before) if path.exists() else None,
                'owned_entry': 'autonomous_se' if owned else None,
                'owned_blocks': [text[start:end] for start, end in owned],
                'changed': bool(owned), 'backup': str(backup) if owned else None,
                'content_base64': base64.b64encode(updated.encode('utf-8')).decode('ascii'),
                'sha256': _sha(updated.encode('utf-8'))}
    manifest['manifest_sha256'] = _manifest_hash(manifest)
    return manifest


def apply_mcp_cleanup(manifest: dict, *, authorization_ref: str) -> dict:
    if not isinstance(authorization_ref, str) or not authorization_ref.strip():
        raise ValueError('Explicit migration authorization reference is required')
    if manifest.get('kind') != 'mcp-cleanup' or manifest.get('manifest_version') != 1:
        raise ValueError('Unsupported cleanup manifest')
    if manifest.get('manifest_sha256') != _manifest_hash(manifest):
        raise ValueError('Manifest digest mismatch')
    path = Path(manifest['config_path'])
    _safe_destination(path.parent, path)
    current = plan_mcp_cleanup(path)
    for key in ('before_sha256', 'sha256', 'owned_blocks', 'owned_entry', 'changed', 'content_base64'):
        if current[key] != manifest[key]:
            raise ValueError('Stale cleanup preview; config changed')
    if manifest['changed']:
        backup = _safe_destination(path.parent, Path(manifest['backup']))
        if not backup.is_relative_to(path.parent / 'backups/autonomous-se') or backup.exists():
            raise ValueError('Invalid or occupied cleanup backup destination')
        data = base64.b64decode(manifest['content_base64'], validate=True)
        if _sha(data) != manifest['sha256']:
            raise ValueError('Cleanup content hash mismatch')
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.write_bytes(data)
    return {'changed': manifest['changed'], 'config_path': str(path), 'backup': manifest['backup'],
            'authorization_ref': authorization_ref.strip()}


def read_manifest(path):
    """Read a reviewed JSON manifest; PowerShell 5.1 `>` writes UTF-16LE with a BOM."""
    data = Path(path).read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        encoding = 'utf-8-sig'
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = 'utf-16'
    else:
        encoding = 'utf-8'
    try:
        manifest = json.loads(data.decode(encoding))
    except UnicodeDecodeError:
        raise ValueError('manifest is not UTF-8 or BOM-marked UTF-16 text; regenerate it with --preview') from None
    except json.JSONDecodeError as error:
        raise ValueError('manifest is not valid JSON (' + str(error) + '); regenerate it with --preview') from None
    if not isinstance(manifest, dict):
        raise ValueError('manifest must be a JSON object; regenerate it with --preview')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--project', type=Path, help='Explicitly activate one project/workspace')
    mode.add_argument('--runtime-only', action='store_true', help='Prepare shared tooling without routing')
    mode.add_argument('--global', dest='global_install', action='store_true', help='Explicit legacy Codex global activation')
    mode.add_argument('--cleanup-mcp', type=Path, metavar='CONFIG', help='Preview only the owned historical MCP entry')
    mode.add_argument('--apply-manifest', type=Path, help='Apply a previously reviewed install/cleanup JSON manifest')
    parser.add_argument('--target', action='append', choices=TARGETS, help='Backend target; repeat for multiple backends')
    parser.add_argument('--project-local', action='store_true', help='Keep runtime/config/private state in the selected project')
    parser.add_argument('--timezone', help='Reporting timezone: local (default for new projects) or an IANA name')
    state = Path(os.environ.get('ASE_HOME', Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'AutonomousSE'))
    parser.add_argument('--home', type=Path, help='Shared tooling destination; local mode only accepts project/.devframework')
    parser.add_argument('--skills', type=Path, help='Legacy global destination; requires --global')
    parser.add_argument('--preview', action='store_true', help='Print manifest without changing files')
    parser.add_argument('--authorization-ref', help='Explicit user authorization source for MCP cleanup')
    parser.add_argument('--register-verifier', action='store_true', help='Deprecated; always fails without writing')
    parser.add_argument('--root', action='append', type=Path, default=[], help=argparse.SUPPRESS)
    parser.add_argument('--state-home', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.project_local and not args.project:
        parser.error('--project-local requires --project')
    if args.timezone is not None and not args.project_local:
        parser.error('--timezone requires --project-local')
    if args.register_verifier:
        parser.error('--register-verifier is deprecated; no files or configuration were changed. Use local CLI adapters.')
    if args.apply_manifest:
        try:
            manifest = read_manifest(args.apply_manifest)
        except (OSError, ValueError) as error:
            parser.error('--apply-manifest ' + str(args.apply_manifest) + ': ' + str(error))
        if manifest.get('kind') == 'mcp-cleanup':
            result = apply_mcp_cleanup(manifest, authorization_ref=args.authorization_ref)
        else:
            previous = installed_commit(manifest)
            result = apply_install(manifest)
            result['doctor'] = post_update_check(manifest, previous)
            result['update_status'] = result['doctor']['status']
    elif args.cleanup_mcp:
        result = plan_mcp_cleanup(args.cleanup_mcp)
    else:
        if not (args.project or args.runtime_only or args.global_install):
            parser.error('Select --project, --runtime-only or explicit legacy --global')
        if args.skills and not args.global_install:
            parser.error('--skills requires --global; use --project for project activation')
        if not args.target and not args.global_install:
            parser.error('Select an explicit --target codex, claude or copilot')
        manifest = _plan_install(args.target or ['codex'], args.project,
                                 runtime_home=args.home if args.project_local else args.home or state / 'tooling',
                                 legacy_global=args.global_install, skill_home=args.skills,
                                 project_local=args.project_local, timezone=args.timezone)
        if args.preview:
            result = manifest
        else:
            previous = installed_commit(manifest)
            result = apply_install(manifest)
            result['doctor'] = post_update_check(manifest, previous)
            result['update_status'] = result['doctor']['status']
    print(json.dumps(result, indent=2))
    if 'doctor' in result:
        from ase.doctor import text
        print(text(result['doctor']), file=sys.stderr)
        if result['update_status'] != 'COMPLETE':
            raise SystemExit(2)


if __name__ == '__main__':
    main()

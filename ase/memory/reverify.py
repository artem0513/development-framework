"""After a framework update, list local notes whose framework references no longer hold.

Only the framework's own project store is read; nothing is changed or deleted. A note is listed
for the owner to re-verify when it was written under another framework commit (or before notes
were stamped) and names an `ase` command, flag, skill or framework file the installed framework
no longer has. A version change alone never lists a note, and owner decisions and preferences are
shown as the owner's, never rewritten.
"""
import argparse
import json
from pathlib import Path
import re

from ..framework import UNKNOWN, short, source_commit
from ..state import read_record, write_record

NOTE_KIND = 'framework_note'
# Knowledge kinds that record the owner's own choice rather than a framework-derived observation.
OWNER_KINDS = frozenset({'decision', 'preference', 'owner_decision', 'user_preference'})
COMMAND_SPAN = re.compile(r'`ase ([^`\n]+)`')
FLAG = re.compile(r'(?<![\w-])(--[a-z][a-z0-9-]*)')
WORD = re.compile(r'[a-z][a-z0-9-]*\Z')
# The framework's own skill trees (the .agents/.claude/.github copies are installed mirrors of skills/).
# Folders such as docs/ or scripts/ are common in projects too, so they are not treated as framework paths.
FRAMEWORK_PATH = re.compile(r'(?<![\w./-])((?:\.agents/|\.claude/|\.github/)?skills/[\w./-]*[\w/])')


def stamp_note(db, key, commit=None):
    """Record the framework commit a note was written under, in the caller's transaction."""
    previous = read_record(db, NOTE_KIND, key)
    write_record(db, NOTE_KIND, key, {'framework_commit': commit or source_commit()},
                 expected_revision=previous['revision'])


def _walk(parser):
    """{'flags': options of this command, 'children': {subcommand: node}} from an argparse parser."""
    node = {'flags': set(), 'children': {}}
    for action in parser._actions:
        node['flags'].update(option for option in action.option_strings if option.startswith('--'))
        if isinstance(action, argparse._SubParsersAction):
            node['children'] = {name: _walk(child) for name, child in action.choices.items()}
    return node


def inventory(root, parser):
    """What the installed framework offers: its skill tree and the commands and flags of its CLI parser."""
    return {'root': Path(root), 'commands': _walk(parser)}


def removed_references(text, framework, project_root=None):
    found = []
    for span in COMMAND_SPAN.findall(text):
        node, path, flags, known = framework['commands'], ['ase'], set(framework['commands']['flags']), True
        for word in span.split():
            if word.startswith('-') or not WORD.match(word) or not node['children']:
                break
            if word not in node['children']:
                found.append('command `' + ' '.join(path + [word]) + '`')
                known = False
                break
            path.append(word)
            node = node['children'][word]
            flags |= node['flags']
        if known:  # A flag is judged against the command it is given to and the global options.
            found.extend('flag `' + flag + '` of `' + ' '.join(path) + '`' for flag in FLAG.findall(span) if flag not in flags)
    for reference in FRAMEWORK_PATH.findall(text):
        relative = re.sub(r'^\.(?:agents|claude|github)/', '', reference).rstrip('/')
        if (framework['root'] / relative).exists():
            continue
        if project_root is not None and (project_root / reference.rstrip('/')).exists():
            continue  # The project's own file of the same name, not a framework reference.
        found.append('file `' + reference + '`')
    return list(dict.fromkeys(found))


def _notes(store):
    db = store.db
    # Superseded claims are history, already replaced by a newer note.
    for row in db.execute('SELECT id,topic,statement FROM knowledge_claims WHERE id NOT IN '
                          '(SELECT supersedes FROM knowledge_claims WHERE supersedes IS NOT NULL) ORDER BY id'):
        meta = read_record(db, 'knowledge_claim', str(row['id']))['payload'] or {}
        yield ({'note': 'knowledge claim ' + str(row['id']), 'topic': row['topic'],
                'owner': meta.get('kind') in OWNER_KINDS, 'key': 'claim:' + str(row['id'])}, row['statement'])
    columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
    extra = [name for name in ('important_decisions', 'unresolved_questions') if name in columns]
    for row in db.execute('SELECT * FROM sessions ORDER BY id'):
        text = '\n'.join([row['summary'], *(row[name] or '' for name in extra)])
        yield {'note': 'session summary ' + str(row['id']), 'topic': row['task_id'], 'owner': False,
               'key': 'session:' + str(row['id'])}, text
    # The next action `ase restore` hands back: the likeliest place for a command line.
    for row in db.execute("SELECT key,payload FROM runtime_records WHERE kind='task_context' ORDER BY key"):
        context = json.loads(row['payload']) or {}
        if context.get('next_action'):
            yield ({'note': 'next action of task ' + str(context.get('task_id') or row['key']),
                    'topic': context.get('task_id'), 'owner': False, 'key': None}, context['next_action'])
    for row in db.execute('SELECT id,context,decision,rationale FROM decisions ORDER BY id'):
        yield ({'note': 'owner decision ' + str(row['id']), 'topic': row['context'], 'owner': True, 'key': None},
               '\n'.join([row['decision'], row['rationale']]))


def reverify(store, framework, *, current_commit=None):
    """Notes to re-verify after an update, oldest first; nothing is modified. `framework` is inventory()."""
    current = current_commit or source_commit()
    listed = []
    for note, text in _notes(store):
        written = (read_record(store.db, NOTE_KIND, note['key'])['payload'] or {}).get('framework_commit') if note['key'] else None
        if written is not None and written == current:
            continue
        removed = removed_references(text, framework, store.repo)
        if removed:
            listed.append({**{k: v for k, v in note.items() if k != 'key'},
                           'written_under': short(written) if written else UNKNOWN,
                           'references': removed,
                           'action': ('owner decision: kept as recorded; the owner decides whether it still applies'
                                      if note['owner'] else 're-verify; not changed automatically')})
    return listed

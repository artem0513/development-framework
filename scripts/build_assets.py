"""Generate native role pointers from canonical skills/, without installing.

Product instructions live in skills/. Use --check to detect stale descriptors,
including this repository's own existing role copies, which are refreshed too;
target installations are a separate reviewed installer operation.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OWNED_SKILLS = (
    'task-orchestrator', 'task-intake', 'task-discovery', 'project-context-manager',
    'project-bootstrap', 'independent-ac-verifier', 'independent-code-reviewer',
    'quality-gate', 'communication-assistant', 'time-reporter', 'gh-fix-ci',
)
ROLES = {'ase-implementer': 'task-orchestrator', 'ase-ac-verifier': 'independent-ac-verifier',
         'ase-code-reviewer': 'independent-code-reviewer'}
# Role-specific reminders; the skill itself remains the authoritative text.
ROLE_NOTES = {
    'ase-code-reviewer': ('Judge whether each changed hunk is required before judging its correctness, and weigh '
                          'blast radius on shared components. A review of a change you implemented or co-authored '
                          'in the same session is a "self-check, not independent", never an approval. '),
    'ase-implementer': ('A request to review changes this session implemented or co-authored goes to an independent '
                        'review session. When none is available, the independent review stays pending; label any review '
                        'you actually perform "self-check, not independent", never an approval. '),
}
# Native subagents may not load the workspace instruction file, so its decisive rules are repeated here.
SHARED_RULES = ('Obey the workspace\'s framework instruction file (AGENTS.md, AGENTS.override.md, CLAUDE.md, '
                'CLAUDE.local.md or Copilot instructions); outside the framework\'s own repository never stage, commit '
                'or mention framework files. When investigating or recommending a fix, first read how the callee/source '
                'already handles the condition and treat a lead\'s suggested fix as a hypothesis; no consumer-side retry, '
                'wait, timeout or compensation as the fix for a source fault unless a requirement or documented contract '
                'prescribes it. A defect outside the ticket that this change did not cause is reported in one line asking '
                'whether it is needed at all; without the owner\'s explicit permission for that defect it is never fixed, '
                'committed, given the ticket key or put in any branch or PR, current or separate. If it blocks a check of '
                'a denial goal and a working control refuses with a 4xx (never 5xx, timeout or empty 200), judge the goal '
                'while still testing the task\'s own code; otherwise it is a blocker for the owner. An exact status code '
                'counts when it is itself the requirement. Each new domain conversion or magic value (date/code formats, '
                'units, external-system numbers or keys) uses an existing helper or constant, goes into the existing '
                'helper class of its domain, or becomes a named constant with a reason; self-checks, reviews and cleanup '
                'passes check every one, not only duplicates. When asked whether something exists, search first and '
                'answer with the fact. ')
READ_ONLY_ROLES = ('ase-ac-verifier', 'ase-code-reviewer')
READ_ONLY_RULE = 'Stay read-only: do not edit, create or delete files; use the shell only for inspection, tests and ase commands. '
# Codex custom agents cannot narrow sandbox_mode (the parent session's policy applies), so only the rule above.
READ_ONLY_TOOLS = {'claude': 'Read, Grep, Glob, Bash', 'copilot': "['read', 'search', 'execute']"}
# This repository's own installed role copies, refreshed and checked only when already present.
MIRRORS = {'agents': '.codex/agents', 'templates/agents/claude': '.claude/agents',
           'templates/agents/copilot': '.github/agents'}


def descriptions(root=ROOT):
    found = {path.parent.name for path in (root / 'skills').glob('*/SKILL.md')}
    if found != set(OWNED_SKILLS):
        raise ValueError('Canonical skills must contain exactly the 11 owned entrypoints')
    result = {}
    for skill in OWNED_SKILLS:
        text = (root / 'skills' / skill / 'SKILL.md').read_text(encoding='utf-8')
        match = re.search(r'^description:\s*(.+)$', text, flags=re.M)
        if not match:
            raise ValueError('Missing skill description: ' + skill)
        raw = match[1].strip()
        result[skill] = json.loads(raw) if raw.startswith('"') else raw
    return result


def generated_assets(root=ROOT):
    from ase.runtime_policy import build_bundle
    from ase.protocol import canonical
    descriptions_by_skill = descriptions(root)
    outputs = {}
    for role, skill in ROLES.items():
        description = descriptions_by_skill[skill]
        for target, relative in (('codex', '.agents/skills'), ('claude', '.claude/skills'),
                                 ('copilot', '.github/skills')):
            # Read-only roles cannot spawn subagents, so they get no delegation protocol.
            delegation = '' if role in READ_ONLY_ROLES else (
                            'For useful independent ready work with permitted native subagents, read ' + relative +
                            '/task-orchestrator/references/parallel-work.md. One lead integrates adaptive waves while preserving '
                            'root leases, ownership and budgets; serial fallback remains available. '
                            'For managed native delegation, commit ase delegation register/dispatch before native spawn, '
                            'then attach the actual handle, seal the result and separately accept it; unknown launches require reconciliation. ')
            instructions = ('Read ' + relative + '/' + skill + '/SKILL.md in the selected workspace. '
                            'Preserve the original approved scope and existing permissions. '
                            'Use the local ase CLI and original evidence; report unavailable capabilities honestly. '
                            'Inspect authorized context and relevant safe read-only evidence before proposing an owner question; '
                            'distinguish retrievable facts from owner-only decisions and give a concise supported hypothesis/recommendation. '
                            + delegation +
                            'Native advisory answers do not replace genuine independent Runner receipts. '
                            'Keep the original chat as lead and consume its durable ase events inbox; native return tools are optional. '
                            'For software, preserve explicit test_adequacy policy and a scenario plan, and require a separate '
                            'current adequacy assessment alongside original AC/review. Positive counts alone do not prove coverage. '
                            'For configured nontrivial roots, the ORIGINAL main chat performs the configured whole-solution (1 LOW/MEDIUM, 3 HIGH risk) '
                            'critical passes against original requirements and approved clarifications. Record each assessment '
                            'before fixes, recheck after them and retain a current final pass. Workers do not count as the main. '
                            'An awaiting_lead_review handoff is an internal continuation, not another owner approval request. '
                            + ROLE_NOTES.get(role, '') + SHARED_RULES
                            + (READ_ONLY_RULE if role in READ_ONLY_ROLES else '') +
                            'This descriptor grants no access, approval or independent execution by itself.')
            if target == 'codex':
                path = root / 'agents' / (role + '.toml')
                content = ('name = ' + json.dumps(role) + '\ndescription = ' + json.dumps(description) +
                           '\ndeveloper_instructions = ' + json.dumps(instructions) + '\n')
            else:
                suffix = '.agent.md' if target == 'copilot' else '.md'
                path = root / 'templates/agents' / target / (role + suffix)
                content = ('---\nname: ' + role + '\ndescription: ' + json.dumps(description) +
                           ('\ntools: ' + READ_ONLY_TOOLS[target] if role in READ_ONLY_ROLES else '') +
                           ('\ninclude-custom-instructions: true' if target == 'copilot' else '') +
                           '\n---\n\n' + instructions + '\n')
            outputs[path] = content.encode('utf-8')
            mirror = root / MIRRORS[path.parent.relative_to(root).as_posix()] / path.name
            if mirror.is_file():
                outputs[mirror] = outputs[path]
    outputs[root / 'ase/data/runtime_policy.json'] = canonical(build_bundle(root)) + b'\n'
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read-only generated asset consistency check')
    args = parser.parse_args(argv)
    changed = []
    for path, content in generated_assets().items():
        if not path.is_file() or path.read_bytes() != content:
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
    print(json.dumps({'changed': changed, 'check': args.check}, indent=2))
    return 1 if args.check and changed else 0


if __name__ == '__main__':
    raise SystemExit(main())

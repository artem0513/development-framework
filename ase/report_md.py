"""Markdown task report for a pull request body.

It keeps three things apart, as the project rules require: executed checks (facts), independent
model judgments, and enforcement that this local tool cannot provide. It is meant for publication,
so owner prompt text (kept in the local gate JSON) is cited by id only.
"""


def _cell(value):
    return str(value if value is not None else '').replace('|', '\\|').replace('\n', ' ')[:300]


def _list(lines, title, values):
    if values:
        lines.append(f'**{title}**')
        lines.extend('- ' + _cell(value) for value in values)
        lines.append('')


def render(report):
    task, gate = report['task'], report['quality_gate']
    contract, proposal = task['contract'], task.get('proposal') or {}
    unverified = [row for row in gate.get('disposed_findings') or [] if not row.get('verified')]
    note = f" ({len(unverified)} finding(s) cleared by owner, not verified)" if unverified else ''
    lines = [f"## {_cell(contract.get('id') or task['task_id'])}: gate {gate.get('status', 'UNKNOWN')}{note}", '',
             _cell(contract.get('request')), '']
    if proposal.get('summary'):
        lines += ['### Approach', '', proposal['summary'].strip(), '']
    _list(lines, 'Non-goals', proposal.get('non_goals'))
    _list(lines, 'Unchanged behaviour', proposal.get('unchanged_behavior'))
    if proposal.get('alternatives'):
        lines.append('**Alternatives considered**')
        lines.extend(f"- {_cell(row['option'])}: not chosen because {_cell(row['why_not'])}" for row in proposal['alternatives'])
        lines.append('')
    if proposal.get('rollback'):
        lines += ['**Rollback:** ' + _cell(proposal['rollback']), '']
    effects = proposal.get('effects') or []
    if effects:
        lines += ['**New behaviour/infrastructure effects**', '', '| Effect | Kind | Basis | Change |', '|---|---|---|---|']
        lines.extend(f"| {_cell(e.get('id'))} | {_cell(e.get('kind', 'code'))} | {_cell(e.get('basis'))} {_cell(e.get('ref'))} | {_cell(e.get('change'))} |"
                     for e in effects)
        lines.append('')

    lines += ['### Executed checks', '']
    by_id = (gate.get('profile_evidence') or {}).get('by_id') or {}
    if by_id:
        lines += ['| Check | Result | Detail |', '|---|---|---|']
        lines.extend(f"| {_cell(name)} | {_cell(row.get('status'))} | {_cell(row.get('reason') or row.get('method') or '')} |"
                     for name, row in sorted(by_id.items()))
    else:
        lines.append('No executed check results are recorded.')
    lines.append('')

    lines += ['### Independent judgments', '']
    lead = gate.get('lead_review') or {}
    if lead.get('required_passes') is not None and lead.get('status') != 'NOT_CONFIGURED':
        lines.append(f"- Lead critiques: {lead.get('completed_passes', 0)}/{lead['required_passes']} ({_cell(lead.get('status'))})")
    adequacy = gate.get('test_adequacy') or {}
    if adequacy.get('status'):
        lines.append(f"- Test adequacy: {_cell(adequacy['status'])}")
    lines.append('- Acceptance and code review: separate model sessions; their verdicts are judgments, not executed proof.')
    for row in gate.get('disposed_findings') or []:
        basis = 'captured prompt' if row.get('verified') else 'owner-asserted, not verified'
        lines.append(f"- {row['severity']} finding {row['disposition']} by owner ({_cell(row['user_message_ref'])}, {basis}): {_cell(row['message'])}")
    lines.append('')
    rejected, blocked = gate.get('rejected_reasons') or [], gate.get('blocked_reasons') or []
    _list(lines, 'Rejected', rejected)
    _list(lines, 'Blocked', blocked)

    usage = report.get('usage')
    if usage and usage['sessions']:
        total = usage['total']
        cost = f", ${total['cost_usd']:.2f}" if total.get('cost_usd') else ''
        lines += [f"Session usage: {total['input_tokens']} in / {total['output_tokens']} out tokens"
                  f" (+{total['cache_read_tokens']} cached){cost} over {len(usage['sessions'])} sessions.", '']

    lines += ['### Enforcement', '',
              'Local reliability layer only: no server-side enforcement is assumed. Only a trusted CI job '
              '(for example `ase ci-check`) required through branch protection would enforce publishing.', '']
    return '\n'.join(lines).rstrip() + '\n'

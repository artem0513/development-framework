"""Durable task context, kept separate from source-verified business facts."""
from datetime import datetime
import json

from ..state import read_record, write_record


class ContextService:
    def __init__(self, store):
        self.store = store

    def snapshot(self, *, task_id=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Context limit must be between 1 and 1000')
        current = read_record(self.store.db, 'project_context', self.store.project_id)
        # Old versions remain readable without a destructive backfill. Sequence
        # of saved versions, rather than wall-clock order, determines recency.
        versions = []
        prefix = self.store.project_id + ':'
        for row in self.store.db.execute("SELECT key,payload FROM runtime_records WHERE kind='project_context_version'"):
            if row['key'].startswith(prefix):
                suffix = row['key'][len(prefix):]
                if suffix.isdigit():
                    versions.append((int(suffix), json.loads(row['payload'])))
        if current['payload']:
            versions.append((current['revision'], current['payload']))
        latest = {}
        for revision, value in sorted(versions, key=lambda item: item[0]):
            if value.get('workspace_id', self.store.workspace_id) != self.store.workspace_id:
                continue
            owner = value.get('task_id')
            if owner and (task_id is None or owner == task_id):
                latest[owner] = {**value, 'context_revision': revision,
                    'authority': 'HISTORICAL_TASK_CONTEXT',
                    'workspace_binding': 'EXPLICIT' if 'workspace_id' in value else 'LEGACY_UNBOUND'}
        values = sorted(latest.values(), key=lambda item: item['context_revision'], reverse=True)
        return {'project_id': self.store.project_id, **current,
            'workspace_id': self.store.workspace_id,
            'current_workspace_matches': not current['payload'] or current['payload'].get('workspace_id', self.store.workspace_id) == self.store.workspace_id,
            'task_contexts': values[:limit], 'task_context_count': len(values),
            'task_contexts_truncated': len(values) > limit,
            'limitations': ['Task context is dated history, not verified current source or new authority',
                'Legacy context has no workspace binding; verify its relevance before use']}

    def record_task(self, task_id, context, *, summary_event, finish_reason):
        """Join the closing session's unit of work; never commit independently."""
        if not self.store.db.in_transaction:
            raise ValueError('Task context must join a session closure transaction')
        from ..knowledge import Knowledge
        key = self.store.workspace_id + ':' + task_id
        previous = read_record(self.store.db, 'task_context', key)
        claim_id = (previous['payload'] or {}).get('claim_id')
        knowledge = Knowledge(self.store)
        topic = 'task-summary:' + task_id
        predecessor = None
        for claim in knowledge.query(topic)['claims']:
            if claim['id'] == claim_id and not claim['superseded_by']:
                metadata = knowledge._metadata(claim_id) or {}
                # A clock rollback retains separate dated history rather than
                # inventing a later observation or preventing session closure.
                if (metadata.get('workspace_id') == self.store.workspace_id
                        and metadata.get('kind') == 'task_summary'
                        and datetime.fromisoformat(claim['observed_at']) <= datetime.fromisoformat(context['updated_at_utc'])):
                    predecessor = claim_id
        payload = {'topic': topic, 'statement': context['summary'],
            'observed_at': context['updated_at_utc'], 'task_id': task_id,
            'source_refs': ['journal:' + str(summary_event['sequence'])],
            'kind': 'task_summary', 'scope': {'resource_ids': ['task:' + task_id]}}
        if predecessor:
            payload['supersedes'] = predecessor
        claim = knowledge.record(payload)
        value = {**context, 'claim_id': claim['id'], 'finish_reason': finish_reason,
            'journal_ref': payload['source_refs'][0], 'authority': 'HISTORICAL_TASK_CONTEXT'}
        write_record(self.store.db, 'task_context', key, value, expected_revision=previous['revision'])
        return value

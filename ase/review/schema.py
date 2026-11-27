"""Review result format v2: the reviewer's evidence and limits become checkable fields.

v1 (`{findings[severity,path,line,message], summary}`) stays the wire format for existing
contracts. New CLI tasks opt into v2 through `review_format: {version: 2}`. Every property is
required because strict structured-output backends (Codex) reject optional fields.
"""
STR = {'type': 'string', 'minLength': 1}
TEXT = {'type': 'string'}
LIST = {'type': 'array', 'items': STR, 'maxItems': 50}


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


REVIEW_SCHEMA_V2 = _object({
    'findings': {'type': 'array', 'items': _object({
        'severity': {'type': 'string', 'enum': ['P0', 'P1', 'P2', 'P3']}, 'path': STR,
        'line': {'type': 'integer', 'minimum': 1}, 'message': STR,
        'evidence': STR, 'consequence': STR, 'fix': STR})},
    'summary': STR,
    # What was actually read, what could not be, and material changes whose necessity or
    # correctness the packet did not let the reviewer establish (these block the gate).
    'inspected': LIST, 'limits': {'type': 'array', 'items': TEXT, 'maxItems': 50},
    'unverified_material': LIST,
})


def validate_review_format(value):
    if value != {'version': 2}:
        raise ValueError('review_format must be {"version": 2}')
    return value


def review_schema_for(contract, legacy):
    return REVIEW_SCHEMA_V2 if contract.get('review_format') == {'version': 2} else legacy

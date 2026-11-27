"""Resource declarations in Azure Resource Manager JSON templates.

A literal declaration is identified by its full type, name and explicit scope. A
declaration the guard cannot evaluate (an expression name or scope) is still an
addition: its exact text is the identity the owner approves.
"""
from collections import Counter
import re

from ..protocol import decode_json


# Full resource type: a provider namespace with a dot, then one or more type segments.
TYPE = re.compile(r'[A-Za-z][A-Za-z0-9]*\.[A-Za-z0-9.]+(?:/[A-Za-z0-9_.-]+)+')
# Resource group, subscription, management group and tenant deployment template schemas.
SCHEMA_URL = (r'https?://schema\.management\.azure\.com/schemas/[^/"]+/'
              r'(?:subscription|managementGroup|tenant)?[dD]eploymentTemplate\.json#?')
SCHEMA = re.compile(SCHEMA_URL)
# The marker and the parser key on the same thing: a $schema member naming a deployment template.
SCHEMA_MEMBER = re.compile(r'"\$schema"\s*:\s*"' + SCHEMA_URL + '"', re.IGNORECASE)
SCHEMA_MARKER = 'deploymenttemplate.json'  # Casefolded substring every accepted schema contains.
JSON_SUFFIX = '.json'
IDENTITY_LIMIT = 1000  # Bounds an approval identity; generous for expression names.
NESTED_DEPLOYMENT = 'microsoft.resources/deployments'
# Deployment properties that move an inline nested template's resources to another scope.
DEPLOYMENT_TARGETS = ('subscriptionId', 'resourceGroup', 'scope')
# Scope identity format: context parts, outermost first, such as
# "deployment=logs;resourceGroup=other | [resourceId('x')]". Expressions inside an inline
# deployment are evaluated there, so the deployment is part of every inner identity.
SCOPE_SEPARATOR = ' | '
DEPLOYMENT_LABEL = 'deployment='
TARGET_SEPARATOR = ';'


def conventional(path):
    name = path.name.casefold()
    return name == 'azuredeploy.json' or name.endswith('.arm.json')


def matches(path):
    # Any JSON file may be a deployment template; a conventional name is always analyzed.
    return path.suffix.casefold() == JSON_SUFFIX


def marker(content):
    return SCHEMA_MEMBER.search(content) is not None


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= IDENTITY_LIMIT


def target(value):
    if (not {'path', 'type', 'name'} <= set(value) <= {'path', 'type', 'name', 'scope'}
            or not _text(value['type']) or not _text(value['name'])
            or ('scope' in value and not _text(value['scope']))):
        raise ValueError('An ARM resource decision requires a project path and the resource type and name '
                         'as declared, with its explicit scope when it has one')
    return value['type'].casefold(), value['name'], value.get('scope', '')


def _declared(item, parent):
    kind, name = item.get('type'), item.get('name')
    if not _text(kind) or not _text(name):
        raise ValueError('Expected an ARM resource type and name')
    if parent is None:
        return kind, name
    parent_kind, parent_name = parent
    if not TYPE.fullmatch(kind):
        # A relative nested declaration extends its parent type and name.
        return f'{parent_kind}/{kind}', f'{parent_name}/{name}'
    if '/' not in name and kind.casefold().startswith(parent_kind.casefold() + '/') and kind.count('/') == parent_kind.count('/') + 1:
        return kind, f'{parent_name}/{name}'
    return kind, name  # A full type names the whole resource.


def _within(context, part):
    scope = SCOPE_SEPARATOR.join(item for item in (context, part) if item)
    if len(scope) > IDENTITY_LIMIT:
        raise ValueError('ARM resource scope exceeds the identity bound')
    return scope


def _scope(item, context):
    """An explicit scope is added to the enclosing deployment context, never replacing it."""
    scope = item.get('scope', '')
    if not isinstance(scope, str):
        raise ValueError('Expected an ARM resource scope expression')
    return _within(context, scope)


def _deployment_context(item, name, context):
    if not all(isinstance(item[key], str) for key in DEPLOYMENT_TARGETS if key in item):
        raise ValueError('Expected ARM deployment target expressions')
    label = TARGET_SEPARATOR.join([DEPLOYMENT_LABEL + name,
                                   *(f'{key}={item[key]}' for key in DEPLOYMENT_TARGETS if key in item)])
    return _within(context, label)


def read_targets(content):
    template = decode_json(content)
    schema = template.get('$schema') if isinstance(template, dict) else None
    if not isinstance(schema, str) or not SCHEMA.fullmatch(schema):
        if isinstance(schema, str) and SCHEMA_MARKER in schema.casefold():
            raise ValueError('Unrecognized ARM deployment template schema')
        return Counter()
    targets = Counter()

    def collect(resources, parent=None, scope=''):
        if isinstance(resources, dict):
            resources = list(resources.values())  # languageVersion 2.0 symbolic names
        if not isinstance(resources, list):
            raise ValueError('Expected ARM resource declarations')
        for item in resources:
            if not isinstance(item, dict):
                raise ValueError('Expected an ARM resource object')
            kind, name = _declared(item, parent)
            where = _scope(item, scope)
            if item.get('existing') is not True:  # A reference to an existing resource adds nothing.
                targets[(kind.casefold(), name, where)] += 1
            collect(item.get('resources', []), (kind, name), where)
            if kind.casefold() == NESTED_DEPLOYMENT:
                properties = item.get('properties', {})
                inline = properties.get('template') if isinstance(properties, dict) else None
                if not isinstance(properties, dict) or not (inline is None or isinstance(inline, dict)):
                    raise ValueError('Expected an inline ARM deployment template object')
                if inline is not None:
                    # An inline nested template deploys its own resources within this deployment.
                    collect(inline.get('resources', []), None, _deployment_context(item, name, where))

    collect(template.get('resources', []))
    return targets

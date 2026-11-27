"""Tables and columns that EF Core migration Up methods create.

A string literal identifies its value; any other expression is identified by its
exact source text, so the owner still approves the declaration it adds.
"""
from collections import Counter
import json
import re

from ..state import text


TOKEN = re.compile(r'\s+|//[^\n]*|/\*.*?\*/|(?P<quote>"{3,}).*?(?P=quote)|'
                   r'@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\w+|.', re.DOTALL)
BUILDER = 'MigrationBuilder'
# The marker and the parser key on the same thing: an Up override taking a MigrationBuilder.
UP_SIGNATURE = re.compile(r'\bvoid\s+Up\s*\(\s*(?:[A-Za-z_][\w.]*\.)?MigrationBuilder\b')
RAW_QUOTE = '"""'
VERBATIM = '@"'
PAIRS = {'(': ')', '[': ']', '{': '}'}


IDENTIFIER_LIMIT = 500  # Bounds an approval identity; an expression keeps its source text.
# Leading parameters of MigrationBuilder.CreateTable and AddColumn<T>, for positional calls.
POSITIONAL = {'CreateTable': ('name', 'columns', 'schema'),
              'AddColumn': ('name', 'table', 'type', 'unicode', 'maxLength', 'rowVersion', 'schema')}
NAMED = {'name', 'table', 'schema'}


def conventional(path):
    return 'migrations' in {part.casefold() for part in path.parts[:-1]}


def matches(path):
    # A migration may live in any folder; one under Migrations is always analyzed.
    return path.suffix.casefold() == '.cs' and not path.name.casefold().endswith('.designer.cs')


def marker(content):
    return UP_SIGNATURE.search(content) is not None


def target(value):
    if not {'path', 'table'} <= value.keys() <= {'path', 'schema', 'table', 'column'}:
        raise ValueError('An EF Core schema decision requires a migration path and table, with optional schema and column')
    return (text(value['schema'], 'Schema', limit=IDENTIFIER_LIMIT) if 'schema' in value else '',
            text(value['table'], 'Table', limit=IDENTIFIER_LIMIT),
            text(value['column'], 'Column', limit=IDENTIFIER_LIMIT) if 'column' in value else '')


def _arguments(tokens, start):
    """Split a call at outer commas, such as those before columns and constraints lambdas."""
    stack, arguments, begin = [], [], start + 1
    for pos in range(begin, len(tokens)):
        token = tokens[pos]
        if token in PAIRS:
            stack.append(PAIRS[token])
        elif stack and token == stack[-1]:
            stack.pop()
        elif not stack and token in {',', ')'}:
            if pos > begin or token == ',':
                arguments.append((begin, pos))
            begin = pos + 1
            if token == ')':
                return arguments
    raise ValueError('Incomplete EF Core migration operation')


def _identifiers(source, operation, arguments):
    """Name, table and schema of one call; a value that is not a plain string literal keeps its source text."""
    tokens, spans, content = source
    values = {}
    for index, (begin, end) in enumerate(arguments):
        if end - begin >= 2 and tokens[begin + 1] == ':' and tokens[begin].isidentifier():
            key, begin = tokens[begin], begin + 2
        else:
            parameters = POSITIONAL[operation]
            key = parameters[index] if index < len(parameters) else None
        if key not in NAMED or begin >= end:
            continue
        literal = tokens[begin] if end - begin == 1 else None
        if literal == 'null' and key == 'schema':
            values[key] = ''
            continue
        value = None
        if literal is not None and literal.startswith(VERBATIM):
            value = literal[len(VERBATIM):-1].replace('""', '"')
        elif literal is not None and literal.startswith('"') and not literal.startswith(RAW_QUOTE):
            try:
                value = json.loads(literal)
            except ValueError:
                pass  # A C# escape JSON lacks: identify the value by its source text.
        if value is None:
            value = ' '.join(content[spans[begin][0]:spans[end - 1][1]].split())
        values[key] = text(value, 'Migration identifier', limit=IDENTIFIER_LIMIT)
    return values


def _up_targets(source, start, builder, terminator):
    """Scan an Up body; a builder call counts at any depth, such as inside if or foreach blocks."""
    tokens = source[0]
    added, stack = Counter(), []
    for pos in range(start, len(tokens)):
        token = tokens[pos]
        if not stack and token == terminator:
            return added
        if token in PAIRS:
            stack.append(PAIRS[token])
        elif stack and token == stack[-1]:
            stack.pop()
        elif tokens[pos] == builder and (pos == 0 or tokens[pos - 1] != '.') and (
                tokens[pos + 1:pos + 2] == ['.'] or tokens[pos + 1:pos + 3] in (['?', '.'], ['!', '.'])):
            member = pos + (2 if tokens[pos + 1] == '.' else 3)  # builder.Call, builder?.Call, builder!.Call
            operation = tokens[member] if member < len(tokens) else None
            if operation not in POSITIONAL:
                continue
            opening = member + 1
            if tokens[opening:opening + 1] == ['<']:
                # AddColumn<T> has its type arguments before the argument list.
                while opening < len(tokens) and tokens[opening] not in {'(', ';', '}'}:
                    opening += 1
            if tokens[opening:opening + 1] != ['(']:
                raise ValueError('Unrecognized EF Core ' + operation + ' call')
            values = _identifiers(source, operation, _arguments(tokens, opening))
            if 'name' not in values or (operation == 'AddColumn' and 'table' not in values):
                raise ValueError('EF Core ' + operation + ' without a recognizable name')
            schema = values.get('schema', '')
            if operation == 'CreateTable':
                added[(schema, values['name'], '')] += 1
            else:
                added[(schema, values['table'], values['name'])] += 1
    raise ValueError('Incomplete EF Core Up method')


def _up_method(tokens, pos):
    """Builder parameter, body start and terminator of `void Up(MigrationBuilder name)` at pos, or None."""
    if tokens[pos:pos + 3] != ['void', 'Up', '(']:
        return None
    end = pos + 3
    while end < len(tokens) and (tokens[end].isidentifier() or tokens[end] == '.'):
        end += 1
    kind, builder = ''.join(tokens[pos + 3:end - 1]), tokens[end - 1] if end - 1 > pos + 3 else None
    if kind != BUILDER and not kind.endswith('.' + BUILDER):
        return None
    if not builder or not builder.isidentifier() or tokens[end:end + 1] != [')']:
        raise ValueError('Unrecognized EF Core Up method')
    if tokens[end + 1:end + 2] == ['{']:
        return builder, end + 2, '}'
    if tokens[end + 1:end + 3] == ['=', '>']:
        return builder, end + 3, ';'  # Expression-bodied Up
    raise ValueError('Unrecognized EF Core Up method')


def read_targets(content):
    found = [match for match in TOKEN.finditer(content) if not (token := match[0]).isspace()
             and not token.startswith(('//', '/*'))]
    tokens, spans = [match[0] for match in found], [match.span() for match in found]
    added = Counter()
    for pos in range(len(tokens)):
        method = _up_method(tokens, pos)
        if method is not None:
            builder, body, terminator = method
            added.update(_up_targets((tokens, spans, content), body, builder, terminator))
    return added

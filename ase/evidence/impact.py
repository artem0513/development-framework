"""Bounded caller index for changed files, so reviewers see consumers outside the packet scope.

A lexical module-name match is a locator, not a call graph: it may include false positives and
miss dynamic imports. For callers outside the packet the index carries bounded, redacted
excerpts around the matching lines, so the reviewer can check the consumer contract without the
whole file; anything beyond the budget remains a coverage limit the reviewer must state.
"""
import re
from pathlib import PurePosixPath

SOURCE_SUFFIXES = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.cs', '.java', '.kt', '.go', '.rb', '.php',
                   '.rs', '.swift', '.scala', '.vue', '.svelte', '.sql', '.ps1', '.sh'}
GENERIC = {'__init__', 'index', 'main', 'mod', 'lib', 'util', 'utils', 'types', 'common', 'helpers', 'config', 'test', 'tests'}
MAX_SCAN_FILES = 3000
MAX_FILE_BYTES = 262144
MAX_CALLERS = 30
CONTEXT_LINES = 4
MAX_WINDOWS = 3
MAX_EXCERPT_CHARS = 2000
EXCERPT_BUDGET_CHARS = 48000


def _terms(path):
    """Module names a consumer would use: file stem, or the package directory for index-like files."""
    pure = PurePosixPath(path)
    stem = pure.stem
    if stem in GENERIC and len(pure.parts) > 1:
        stem = pure.parent.name
    return [stem] if len(stem) >= 3 and stem not in GENERIC else []


# One verification builds a packet per session; the scan is shared while the snapshot is unchanged.
_CACHE = {}


def change_impact(repo, snapshot_paths, changed_paths, packet_paths, *, snapshot_key=None, omitted=None,
                  index_paths=None, excerpt_paths=None):
    """Callers of changed modules. The scan covers every changed path (cached per snapshot); the index lists
    `index_paths` (default: all), with excerpts only for `excerpt_paths` (default: all listed). `packet_paths`
    are the bodies this packet really carries."""
    changed = [p for p in sorted(changed_paths) if PurePosixPath(p).suffix in SOURCE_SUFFIXES]
    if not changed:
        return None
    scan_paths = changed
    if index_paths is not None:
        changed = [p for p in changed if p in index_paths]
        if not changed:
            return None
    if snapshot_key is None:
        rows, scanned, limited, totals = _scan(repo, snapshot_paths, scan_paths)
    else:
        key = (str(repo), snapshot_key, tuple(scan_paths))
        if key not in _CACHE:
            _CACHE.clear()
            _CACHE[key] = _scan(repo, snapshot_paths, scan_paths)
        rows, scanned, limited, totals = _CACHE[key]
    # Every caller path is listed once in caller_paths; changes refer to it by index. A mechanical change
    # touching hundreds of modules otherwise repeats the same callers hundreds of times.
    table = sorted({caller for path in changed for caller, _ in rows[path]})
    index = {caller: number for number, caller in enumerate(table)}
    excerpted = [path for path in changed if excerpt_paths is None or path in excerpt_paths]
    changes = []
    for path in changed:
        budget = EXCERPT_BUDGET_CHARS // max(1, len(excerpted))  # one noisy module cannot starve the others
        callers, excerpts, by_policy, by_budget, withheld = [], {}, [], [], []
        for caller, excerpt in rows[path]:
            number = index[caller]
            callers.append(number)
            if caller in packet_paths:
                continue
            if path not in excerpted:
                withheld.append(number)
            elif omitted is not None and omitted(caller):
                by_policy.append(number)
            elif len(excerpt) <= budget:
                excerpts[str(number)], budget = excerpt, budget - len(excerpt)
            else:
                by_budget.append(number)
        row = {'path': path, 'terms': _terms(path), 'callers': callers,
               'callers_outside_packet': sum(table[number] not in packet_paths for number in callers)}
        if totals[path] > len(callers):
            row['callers_not_listed'] = totals[path] - len(callers)
        if excerpts:
            row['excerpts'] = excerpts
        omissions = {name: numbers for name, numbers in (('OMITTED_BY_SOURCE_POLICY', by_policy),
                                                         ('OMITTED_BUDGET', by_budget),
                                                         ('CHANGE_NOT_CARRIED', withheld)) if numbers}
        if omissions:
            row['excerpt_omitted'] = omissions
        changes.append(row)
    return {'method': 'lexical module-name match; locator only, not a call graph',
            'format': ('caller_paths lists each caller once; a change names its callers by index into it. '
                       'caller_in_packet: callers whose body is in this packet. excerpts: bounded excerpts around the '
                       'matching lines of callers outside the packet, by index. excerpt_omitted: callers outside the '
                       'packet without an excerpt and why (CHANGE_NOT_CARRIED: the changed file itself is reviewed in '
                       'another part, so its callers are listed without excerpts). callers_not_listed: '
                       'further matching callers beyond the per-change limit, never reviewed here.'),
            'caller_paths': table, 'caller_in_packet': [index[caller] for caller in table if caller in packet_paths],
            'changes': changes, 'scanned_files': scanned, 'scan_limit_reached': limited,
            'limitation': ('Callers outside the packet are shown only as excerpts around the matching lines; '
                           'review what the excerpts allow and state the remaining coverage limit.')}


IMPORT_LINE = re.compile(r'^\s*(from\s+\S+\s+import\b|import\s|using\s|require\(|const\s+\S+\s*=\s*require\()')


def _symbols(repo, path):
    """Top-level def/class/function names of the changed file, so windows land on real uses."""
    try:
        content = (repo / path).read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return []
    names = re.findall(r'^(?:async\s+)?(?:def|class|function|export\s+(?:async\s+)?function|export\s+class)\s+([A-Za-z_]\w{2,})',
                       content, re.M)
    return sorted(set(names))[:40]


def _excerpt(content, pattern, symbols=None):
    """Numbered lines around uses (not imports) of the changed module, redacted and bounded."""
    from ..core import redact
    lines = content.splitlines()
    uses = []
    if symbols:
        named = re.compile(r'(?<![\w-])(' + '|'.join(map(re.escape, symbols)) + r')(?![\w-])')
        uses = [i for i, line in enumerate(lines) if named.search(line) and not IMPORT_LINE.search(line)]
    hits = (uses or [i for i, line in enumerate(lines) if pattern.search(line)])[:MAX_WINDOWS]
    keep = sorted({j for i in hits for j in range(max(0, i - CONTEXT_LINES), min(len(lines), i + CONTEXT_LINES + 1))})
    out, previous = [], None
    for j in keep:
        if previous is not None and j != previous + 1:
            out.append('...')
        out.append(f'{j + 1}: {lines[j]}')
        previous = j
    return redact('\n'.join(out))[:MAX_EXCERPT_CHARS]


TERM_BOUNDARY = r'[\w-]'
# A term made only of boundary characters matches exactly when it is a whole maximal run of them,
# so one tokenization per file answers every such term; other terms keep their own search.
TOKEN = re.compile(TERM_BOUNDARY + '+')


def _scan(repo, snapshot_paths, changed):
    patterns = {path: re.compile(r'(?<!' + TERM_BOUNDARY + ')(' + '|'.join(map(re.escape, _terms(path))) + r')(?!' + TERM_BOUNDARY + ')')
                for path in changed if _terms(path)}
    symbols = {path: _symbols(repo, path) for path in patterns}
    by_token, searched = {}, []
    for path in patterns:
        terms = _terms(path)
        if all(TOKEN.fullmatch(term) for term in terms):
            for term in terms:
                by_token.setdefault(term, []).append(path)
        else:
            searched.append(path)
    rows = {path: [] for path in changed}
    totals = {path: 0 for path in changed}
    candidates = [p for p in sorted(snapshot_paths) if PurePosixPath(p).suffix in SOURCE_SUFFIXES]
    scanned = 0
    for candidate in candidates[:MAX_SCAN_FILES]:
        target = repo / candidate
        try:
            if target.is_symlink() or target.stat().st_size > MAX_FILE_BYTES:
                continue
            content = target.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        scanned += 1
        tokens = set(TOKEN.findall(content)) & by_token.keys()
        matched = {path for token in tokens for path in by_token[token]}
        matched.update(path for path in searched if patterns[path].search(content))
        for path, pattern in patterns.items():
            if path in matched and candidate != path:
                totals[path] += 1
                if len(rows[path]) < MAX_CALLERS:
                    rows[path].append((candidate, _excerpt(content, pattern, symbols[path])))
    return rows, scanned, len(candidates) > MAX_SCAN_FILES, totals

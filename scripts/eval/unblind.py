import json, re, sys, collections
batch, files = sys.argv[1], sys.argv[2:]
m = {x['id']: x for x in json.load(open(batch + '-map.json'))}
dec = collections.defaultdict(collections.Counter); own = collections.defaultdict(collections.Counter)
for f in files:
    for line in open(f, encoding='utf-8'):
        c = [x.strip() for x in line.strip().strip('|').split('|')]
        if len(c) > 3 and re.fullmatch(r'r[0-9a-f]{8}', c[0]) and c[0] in m:
            arm = m[c[0]]['arm']; d = c[2].split()[0].strip('*')
            dec[(c[1], arm)][d] += 1
            for cell in c[3:6]:
                w = cell.split()[0] if cell else ''
                if w in ('CORRECT', 'MISSING', 'NEEDLESS'): own[(c[1], arm)][w] += 1
for s in sorted({k[0] for k in dec}):
    print(f"{s:5} old {dict(dec[(s,'old')])} {dict(own[(s,'old')])}  |  new {dict(dec[(s,'new')])} {dict(own[(s,'new')])}")

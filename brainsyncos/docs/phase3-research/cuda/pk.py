import sys, re
def parse(paths):
    db = {}
    for p in paths:
        txt = open(p, encoding='utf-8', errors='replace').read()
        for st in txt.split('\n\n'):
            d = {}; key=None
            for line in st.splitlines():
                if line.startswith(' ') and key: d[key]+= '\n'+line; continue
                if ':' in line:
                    key, v = line.split(':',1); d[key]=v.strip()
            if 'Package' in d:
                d['_repo']=p.split('/')[-1]
                db.setdefault(d['Package'], []).append(d)
    return db
def vkey(v):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', v)]
def best(db,n):
    l=db.get(n)
    if not l: return None
    return sorted(l, key=lambda d: vkey(d['Version']))[-1]
def deps(d):
    out=[]
    for f in ('Depends','Pre-Depends'):
        for alt in d.get(f,'').split(','):
            alt=alt.strip()
            if not alt: continue
            first=alt.split('|')[0].strip()
            out.append(re.split(r'[ (:]', first)[0])
    return out

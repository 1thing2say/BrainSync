import sys; sys.path.insert(0, sys.argv[1]); from pk import *
db=parse(sys.argv[2].split(','))
# providers map
prov={}
for n,l in db.items():
    for d in l:
        for p in d.get('Provides','').split(','):
            p=p.strip().split(' ')[0]
            if p: prov.setdefault(p,n)
def closure(roots):
    seen=set(); ext=set(); st=list(roots)
    while st:
        n=st.pop()
        if n in seen: continue
        name = n if n in db else prov.get(n)
        if not name: ext.add(n); continue
        if name in seen: continue
        seen.add(name)
        d=best(db,name)
        st.extend(deps(d))
    return seen, ext
for spec in sys.argv[3:]:
    label, roots = spec.split('=')
    s,e=closure(roots.split('+'))
    dl=sum(int(best(db,n).get('Size',0)) for n in s); inst=sum(int(best(db,n).get('Installed-Size',0)) for n in s)
    big=sorted(s,key=lambda n:-int(best(db,n).get('Size',0)))[:6]
    print(f"### {label}: {len(s)} NVIDIA pkgs, download {dl/1e9:.2f} GB, installed {inst/1e6:.2f} GB")
    print("   largest:", ', '.join(f"{n} {int(best(db,n)['Size'])/1e6:.0f}MB" for n in big))
    print("   non-NVIDIA deps:", ' '.join(sorted(e))[:400])

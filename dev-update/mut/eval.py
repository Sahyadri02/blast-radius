import json,sys,subprocess,os,glob
S=sys.argv[1]
t=json.load(open('/home/user/br/sample/tests.json'))['tests']; idn={x['id']:x['number'] for x in t}
os.makedirs(S+'/run/commits',exist_ok=True)
done=[os.path.basename(f)[:-4] for f in glob.glob(S+'/truth/*.txt')]
for f in glob.glob(S+'/run/commits/*'): os.remove(f)
for d in done: os.symlink(f'{S}/commits/{d}.patch',f'{S}/run/commits/{d}.patch')
os.system(f'cp /home/user/br/sample/tests.json {S}/run/')
out=subprocess.run(['python3',os.environ.get('SEL','/home/user/ws/dev/entry/selector.py')],env=dict(os.environ,BR_DATA=S+'/run',BR_SRC='/home/user/sqlglot'),capture_output=True,text=True).stdout
tot=0;miss=0
ms=[round(x['seconds']*1000) for x in t]
for l in out.splitlines():
    d=json.loads(l); s=set(x for w in d['workers'] for x in w)
    fail=[x.strip() for x in open(f"{S}/truth/{d['commit']}.txt") if x.strip()]
    fn=[idn.get(x) for x in fail]
    unk=[x for x,n in zip(fail,fn) if n is None]
    m=[x for x,n in zip(fail,fn) if n is not None and n not in s]
    cost=(max(sum(ms[i] for i in w) for w in d['workers'])+2000*len(d['workers']))/1000 if d['workers'] else 0
    tot+=cost+300*bool(m); miss+=bool(m)
    print(d['commit'], 'fail',len(fail),'sel',len(s),'cost',cost, 'MISS '+str(m[:3]) if m else '', 'UNK'+str(unk[:2]) if unk else '')
print('total',round(tot,1),'misses',miss,'n',len(done))

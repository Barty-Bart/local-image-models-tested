import json,collections
# run from the test folder: python3 workflows/fit_per_card.py
# fit per card from the files each build loads (results/files_used.json, sizes on the P16) + working memory measured on the 96 GB runs
GiB=2**30
size={}
for l in open('results/file_sizes_p16.tsv'):
    if not l.strip() or l.startswith('ROOT'): continue
    a,b=l.rstrip('\n').split('\t'); size[b]=int(a)/GiB
F=json.load(open('results/files_used.json'))
def comps(m):
    P=TE=V=L=0; tes=[]
    for cls,k,f in F[m]:
        s=size.get(f)
        if s is None: print('MISSING',m,f); continue
        if cls in('UNETLoader','CheckpointLoaderSimple'): P=max(P,s)
        elif cls=='CLIPLoader': tes.append(s)
        elif cls=='VAELoader': V=s
        elif 'Lora' in cls: L+=s
    if m in ('sdxl_1.0_fp16','illustrious_xl_v2_sdxl','realvisxl_v5_sdxl_fp16'):  # one file = UNet 5.14 GB + CLIP-L/G 1.64 GB + VAE 0.17 GB (decimal)
        tes=[1.64e9/GiB]; V=0.17e9/GiB; P=P-tes[0]-V
    return P,tes,V,L
job=lambda c: 't2i' if c.startswith('T') else ('edit' if c.startswith('E') else ('fuse2' if c=='M1_cat_mountain' else 'fuse4'))
pk=collections.defaultdict(float)
for l in open('results/results.jsonl'):
    r=json.loads(l)
    if r.get('status')=='success' and r.get('seed')==1001 and r.get('tier','uncapped')=='uncapped' and r.get('smi_peak_gb') and r['model'] in F:
        k=(r['model'],job(r['case'])); pk[k]=max(pk[k],r['smi_peak_gb'])
for m in sorted(F):
    P,tes,V,L=comps(m)
    allf=P+sum(tes)+V+L
    ws={j:round(pk[(m,j)]-2.2-allf,1) for j in ('t2i','edit','fuse2','fuse4') if (m,j) in pk}
    print(f'{m:44} painter {P:5.1f} reader {"+".join(f"{t:.1f}" for t in tes):9} vae {V:4.2f} lora {L:4.2f} all {allf:5.1f} | peak-2.2-all: {ws}')

print()
TIERS=[8,12,16,24,32,48,96]; CTX=1.0   # CUDA context + desktop on a real card
FLOOR={'t2i':1.5,'edit':2.0,'fuse2':2.5,'fuse4':4.0}
rows={}
for m in sorted(F):
    P,tes,V,L=comps(m); allf=P+sum(tes)+V+L
    res={}
    for j in FLOOR:
        if (m,j) not in pk: continue
        W=max(pk[(m,j)]-2.2-allf, FLOOR[j])
        if 'sdxl' in m: W=max(max(pk[(x,j)] for x in ('sdxl_1.0_fp16','illustrious_xl_v2_sdxl','realvisxl_v5_sdxl_fp16'))-2.2-6.46, FLOOR[j])
        full=allf+W; swap=max(P+L+V+W, (max(tes) if tes else 0)+0.5)
        f=next((t for t in TIERS if full<=t-CTX),None); s=next((t for t in TIERS if swap<=t-CTX),None)
        close=[t for t in TIERS if abs((t-CTX)-full)<1 or abs((t-CTX)-swap)<1]
        res[j]=dict(full=round(full,1),swap=round(swap,1),fits=f,swaps=s,close=close,W=round(W,1))
    rows[m]=dict(files=round(allf,1),painter=round(P+L,1),reader=round(max(tes) if tes else 0,1),vae=round(V,2),jobs=res)
    js=' | '.join(f"{j}: full {r['full']}->{r['fits']} swap {r['swap']}->{r['swaps']}{' CLOSE'+str(r['close']) if r['close'] else ''}" for j,r in res.items())
    if 0: print(f'{m:42} files {allf:5.1f} | {js}')
# checked on 25 Nov with the classic loader capped to ~7.5 GB (--disable-dynamic-vram --reserve-vram 95.5, results/run-log-fit8.txt):
# image model "loaded completely", text reader swapped out -> runs by swapping parts on an 8 GB card.
# Illustrious / RealVisXL are SDXL fine-tunes with the identical file layout, so they follow SDXL.
CHECKED8 = {'sdxl_1.0_fp16':('t2i',),'illustrious_xl_v2_sdxl':('t2i',),'realvisxl_v5_sdxl_fp16':('t2i',),
            'flux3_image_klein_int8':('t2i','edit'),'moss_image_sft_int8':('t2i','edit')}
for m,js in CHECKED8.items():
    for j in js:
        r=rows[m]['jobs'][j]; r['swaps']=8; r['checked8']=True; r['close']=[t for t in r['close'] if t!=8]
json.dump(rows,open('results/fit_table.json','w'),indent=1)

NAMES={}
import re
for line in open('workflows/make_compare.py'):
    mm=re.match(r'\s*\("([^"]+)", "([^"]+)"',line)
    if mm and not mm.group(2)[:2] in ('M1','M2','E1','E2','T1','T2','T3'): NAMES.setdefault(mm.group(1),mm.group(2))
def lab(r): 
    return r
out=['| Build | Files on disk | Fits fully on | Runs by swapping parts on | Fusing 2 / 4 pictures (swapping) | Close calls |','|---|---|---|---|---|---|']
order=sorted(rows,key=lambda m:(min(r['swaps'] or 999 for r in rows[m]['jobs'].values()), rows[m]['files']))
for m in order:
    R=rows[m]; J=R['jobs']; main=[j for j in ('t2i','edit') if j in J]
    fits=max(J[j]['fits'] for j in main); sw=max(J[j]['swaps'] for j in main)
    f4=' / '.join(f"{J[j]['swaps']} GB" for j in ('fuse2','fuse4') if j in J) or '—'
    close=sorted({t for j in J for t in J[j]['close']})
    out.append(f"| {NAMES.get(m,m)} | {R['files']:.1f} GB | {fits} GB | {sw} GB | {f4} | {', '.join(str(c)+' GB' for c in close) or '—'} |")
print('\n'.join(out))
open('results/fit_table.md','w').write('\n'.join(out))

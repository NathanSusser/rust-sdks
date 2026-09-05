#!/usr/bin/env python3
"""Stall-episode metric, FIXED DEFINITION - do not change between phases.

  threshold : transport > 3.0x the run median (median taken AFTER startup exclusion)
  startup   : frames within the first 90 logged frames (3 s @30fps) are excluded,
              and any episode beginning there is reported separately, not counted
  merge     : consecutive over-threshold frames merge into one episode if <= 3 frames apart
  minimum   : an episode needs >= 3 frames
"""
import csv, sys
MULT, STARTUP_FRAMES, MERGE_GAP, MIN_LEN = 3.0, 90, 3, 3

def analyse(path, label):
    rows=list(csv.DictReader(open(path)))
    d=[(int(r['frame_id']), float(r['exposure_to_receive_ms']))
       for r in rows if r.get('exposure_to_receive_ms') not in (None,'','None')]
    if len(d) < 100: return None
    first=d[0][0]; cutoff=first+STARTUP_FRAMES
    steady=[(f,t) for f,t in d if f>=cutoff]
    vals=sorted(t for _,t in steady); p50=vals[len(vals)//2]; th=p50*MULT
    # Episodes are formed from STEADY frames only. The previous rule iterated all
    # frames and then discarded any episode whose FIRST frame preceded the cutoff,
    # which threw away a 155-frame / 1071 ms episode in A3 that began one frame
    # early and ran ~7 s past startup.
    eps=[]; cur=[]
    for f,t in steady:
        if t>th:
            if cur and f-cur[-1][0] <= MERGE_GAP: cur.append((f,t))
            else:
                if len(cur)>=MIN_LEN: eps.append(cur)
                cur=[(f,t)]
        # under-threshold frames do not break an episode unless the gap exceeds MERGE_GAP
    if len(cur)>=MIN_LEN: eps.append(cur)
    startup=[]
    steady_eps=eps
    aff=sum(len(e) for e in steady_eps)
    print(f"=== {label} ===")
    print(f"  frames {d[0][0]}..{d[-1][0]}  logged={len(d)}  steady p50={p50:.2f} ms  threshold={th:.1f} ms")
    print(f"  STEADY-STATE episodes: {len(steady_eps)}   frames affected: {aff}/{len(steady)} ({100*aff/len(steady):.1f}%)")
    for e in sorted(steady_eps,key=lambda e:-max(t for _,t in e))[:5]:
        fs=[f for f,_ in e]; ts=[t for _,t in e]
        print(f"    frames {fs[0]:5d}-{fs[-1]:5d}  n={len(e):3d}  span={(fs[-1]-fs[0]+1)/30:5.2f}s  peak={max(ts):8.2f} ms")
    if startup:
        s=startup[0]; fs=[f for f,_ in s]
        print(f"  STARTUP (excluded): {len(startup)} episode(s), largest frames {fs[0]}-{fs[-1]} "
              f"n={len(s)} span={(fs[-1]-fs[0]+1)/30:.2f}s")
    print()
    return len(steady_eps), 100*aff/len(steady)

base='/home/nsusser/code/rust-sdks/examples/local_video/scripts/'
for lbl,p in [('A1  640x360  LL=ON', 'results-a1/subscriber.csv'),
              ('A2  960x540  LL=ON', 'results-a2/subscriber.csv'),
              ('A2off 960x540 LL=OFF','results-a2off/subscriber.csv'),
              ('A3  1920x1080 LL=ON','results-a3/subscriber.csv'),
              ('RunB 720p 0.36Mbps LL=ON','results-round5b/subscriber.csv')]:
    try: analyse(base+p, lbl)
    except FileNotFoundError: pass

#!/usr/bin/env python3
"""Dependency-free player and compositor for the ASCII Dance Pocket Library."""
import argparse
import bisect
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load(ref):
    p=Path(ref)
    if not p.is_file(): p=ROOT/'clips'/(ref+'.json')
    c=json.loads(p.read_text())
    if not c.get('frames'): raise ValueError('Clip has no frames')
    if len(c['frames'])!=len(c['durations_ms']): raise ValueError('Frame/timing count mismatch')
    if any(t<=0 for t in c['durations_ms']): raise ValueError('Frame durations must be positive')
    return c

def library():
    return [json.loads(p.read_text()) for p in sorted((ROOT/'clips').glob('*.json'))]

def list_clips(args):
    clips=library()
    for c in clips:
        if args.feeling and c['feeling']!=args.feeling: continue
        if args.mode and c['mode']!=args.mode: continue
        print(f'{c["id"]:30s} {sum(c["durations_ms"])/1000:4.2f}s  {c["title"]}')

def play(args):
    clips=[load(ref) for ref in args.clips]
    if args.speed<=0: raise ValueError('--speed must be positive')
    # Windows terminals need VT processing enabled for ANSI cursor positioning.
    if os.name=='nt':
        try:
            import ctypes
            h=ctypes.windll.kernel32.GetStdHandle(-11)
            mode=ctypes.c_ulong()
            ctypes.windll.kernel32.GetConsoleMode(h,ctypes.byref(mode))
            ctypes.windll.kernel32.SetConsoleMode(h,mode.value|4)
        except Exception: pass
    if not sys.stdout.isatty() and args.loops==0:
        raise ValueError('Use --loops 1 when redirecting output')
    try:
        sys.stdout.write('\x1b[2J\x1b[?25l')
        n=0
        while args.loops==0 or n<args.loops:
            for c in clips:
                deadline=time.monotonic()
                for frame,ms in zip(c['frames'],c['durations_ms']):
                    label=f'{c["title"]} | {args.speed:g}x | Ctrl+C to stop'
                    sys.stdout.write('\x1b[H'+label+'\x1b[K\n'+''.join(line+'\x1b[K\n' for line in frame.split('\n'))+'\x1b[J')
                    sys.stdout.flush()
                    deadline+=ms/(1000*args.speed)
                    time.sleep(max(0,deadline-time.monotonic()))
            n+=1
    except KeyboardInterrupt: pass
    finally:
        sys.stdout.write('\x1b[?25h\n'); sys.stdout.flush()

def pad(frame,width,height):
    lines=frame.split('\n')
    return [lines[y].ljust(width) if y<len(lines) else ' '*width for y in range(height)]

def combine(args):
    clips=[load(ref) for ref in args.clips]
    out={'id':Path(args.output).stem,'title':args.title or 'Combined dance',
         'feeling':'mixed','mode':args.layout,'loop':True,'frames':[],'durations_ms':[]}
    height=max(c['height'] for c in clips)
    if args.layout=='sequence':
        width=max(c['width'] for c in clips)
        for c in clips:
            out['frames'] += ['\n'.join(pad(f,width,height)) for f in c['frames']]
            out['durations_ms'] += c['durations_ms']
    else:
        width=sum(c['width'] for c in clips)+args.gap*(len(clips)-1)
        # Run each dancer on its own timing. Shorter loops repeat to fill the longest.
        totals=[sum(c['durations_ms']) for c in clips]
        total=max(totals)
        boundaries={0,total}; cumulative=[]
        for c,duration in zip(clips,totals):
            times=[0]
            for t in c['durations_ms']: times.append(times[-1]+t)
            cumulative.append(times)
            for base in range(0,total,duration):
                boundaries.update(base+t for t in times if base+t<total)
        boundaries=sorted(boundaries)
        for a,b in zip(boundaries,boundaries[1:]):
            selected=[]
            for c,times,total_c in zip(clips,cumulative,totals):
                idx=bisect.bisect_right(times,a%total_c)-1
                selected.append(pad(c['frames'][idx],c['width'],height))
            out['frames'].append('\n'.join((' '*args.gap).join(s[y] for s in selected) for y in range(height)))
            out['durations_ms'].append(b-a)
        out['note']='Independent timing; shorter clips repeat and may be cut at the final boundary. Use equal-duration clips for aligned loop seams.'
    out.update(width=width,height=height,duration_ms=sum(out['durations_ms']))
    Path(args.output).write_text(json.dumps(out,indent=2)+'\n')
    print(f'Saved {args.output}: {width}x{height}, {out["duration_ms"]/1000:.2f}s')

def interactive():
    clips=library()
    print('ASCII DANCE POCKET LIBRARY / 96 loops\n')
    print('Feelings: '+', '.join(sorted({c['feeling'] for c in clips})))
    feeling=input('\nType a feeling [joyful]: ').strip().lower() or 'joyful'
    selected=[c for c in clips if c['feeling']==feeling]
    if not selected: raise ValueError('Unknown feeling. Try: python player.py list')
    for i,c in enumerate(selected,1): print(f'{i}. {c["mode"]}: {c["title"]}')
    choice=int(input('\nChoose a loop [1]: ') or '1')
    if not 1<=choice<=len(selected): raise ValueError('Choice is out of range')
    play(argparse.Namespace(clips=[selected[choice-1]['id']],speed=1,loops=0))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command')
    ls=sub.add_parser('list',help='Browse the library')
    ls.add_argument('--feeling'); ls.add_argument('--mode',choices=['solo','couple']); ls.set_defaults(func=list_clips)
    pl=sub.add_parser('play',help='Play one loop or a playlist; Ctrl+C stops')
    pl.add_argument('clips',nargs='+'); pl.add_argument('--speed',type=float,default=1)
    pl.add_argument('--loops',type=int,default=0,help='Playlist repeats; 0 = forever'); pl.set_defaults(func=play)
    co=sub.add_parser('combine',help='Create an editable JSON sequence or side-by-side clip')
    co.add_argument('clips',nargs='+'); co.add_argument('--layout',choices=['sequence','side'],default='sequence')
    co.add_argument('--gap',type=int,default=2); co.add_argument('--title'); co.add_argument('-o','--output',required=True)
    co.set_defaults(func=combine)
    args=p.parse_args()
    if getattr(args,'loops',0)<0: p.error('--loops cannot be negative')
    if getattr(args,'gap',0)<0: p.error('--gap cannot be negative')
    try:
        if args.command: args.func(args)
        else: interactive()
    except (ValueError,OSError,KeyError) as e: p.exit(1,f'Error: {e}\n')

if __name__=='__main__': main()

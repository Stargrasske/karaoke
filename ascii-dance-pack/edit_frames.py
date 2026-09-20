#!/usr/bin/env python3
"""Export/import plain ASCII drawings without hand-escaping JSON."""
import argparse
import json
from pathlib import Path

SEPARATOR='\n---FRAME---\n'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    s=p.add_subparsers(dest='mode',required=True)
    ex=s.add_parser('export'); ex.add_argument('clip'); ex.add_argument('output')
    im=s.add_parser('import'); im.add_argument('text'); im.add_argument('template'); im.add_argument('output')
    a=p.parse_args()
    try:
        if a.mode=='export':
            c=json.loads(Path(a.clip).read_text())
            Path(a.output).write_text(SEPARATOR.join(c['frames'])+'\n')
        else:
            c=json.loads(Path(a.template).read_text())
            text=Path(a.text).read_text().removesuffix('\n')
            frames=[]
            for i,chunk in enumerate(text.split(SEPARATOR),1):
                if any(ord(ch)>126 or (ord(ch)<32 and ch!='\n') for ch in chunk):
                    raise ValueError(f'Frame {i}: use printable ASCII; no tabs')
                lines=chunk.split('\n')
                if len(lines)>c['height'] or any(len(line)>c['width'] for line in lines):
                    raise ValueError(f'Frame {i}: exceeds {c["width"]}x{c["height"]}')
                lines=[line.ljust(c['width']) for line in lines]
                lines+=[' '*c['width']]*(c['height']-len(lines))
                frames.append('\n'.join(lines))
            old=c['durations_ms']; c['frames']=frames
            c['durations_ms']=[old[i] if i<len(old) else old[-1] for i in range(len(frames))]
            c['duration_ms']=sum(c['durations_ms']); c['id']=Path(a.output).stem
            c['title']='Custom: '+c['title']
            Path(a.output).write_text(json.dumps(c,indent=2)+'\n')
        print('Saved '+a.output)
    except (ValueError,OSError,KeyError) as e: p.exit(1,f'Error: {e}\n')

if __name__=='__main__': main()

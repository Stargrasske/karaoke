#!/usr/bin/env python3
"""Validate source frames and smoke-test the player/compositor/importer."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def run(*args):
    return subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,check=True,capture_output=True,text=True)

def main():
    data=json.loads((ROOT/'dance-library.json').read_text()); clips=data['clips']
    assert len(clips)==96
    assert len({c['feeling'] for c in clips})==24
    assert sum(c['mode']=='solo' for c in clips)==48
    assert sum(c['mode']=='couple' for c in clips)==48
    assert len({c['id'] for c in clips})==96
    assert len({tuple(c['frames']) for c in clips})==96, 'Duplicate animations'
    for c in clips:
        assert json.loads((ROOT/'clips'/(c['id']+'.json')).read_text())==c
        assert (ROOT/'text'/(c['id']+'.txt')).is_file()
        assert len(c['frames'])==len(c['durations_ms'])==8
        assert len(set(c['frames']))>=4, (c['id'],'too few unique poses')
        assert sum(c['durations_ms'])==c['duration_ms']
        assert all(t>0 for t in c['durations_ms'])
        for f in c['frames']:
            rows=f.split('\n')
            assert len(rows)==14 and all(len(r)==40 for r in rows)
            assert all(ch=='\n' or 32<=ord(ch)<=126 for ch in f)
            assert any(ch!=' ' for ch in f)
    with tempfile.TemporaryDirectory() as tmp:
        tmp=Path(tmp)
        for layout in ('sequence','side'):
            out=tmp/(layout+'.json')
            run('player.py','combine','joyful-solo-01','sleepy-couple-02','--layout',layout,'-o',out)
            c=json.loads(out.read_text())
            assert len(c['frames'])==len(c['durations_ms'])
            assert all(len(f.split('\n'))==c['height'] for f in c['frames'])
            assert all(len(r)==c['width'] for f in c['frames'] for r in f.split('\n'))
            run('player.py','play',out,'--loops','1','--speed','1000')
        text=tmp/'frames.txt'; imported=tmp/'custom.json'
        run('edit_frames.py','export','clips/joyful-solo-01.json',text)
        run('edit_frames.py','import',text,'clips/joyful-solo-01.json',imported)
        assert json.loads(imported.read_text())['frames']==clips[0]['frames']
        listed=run('player.py','list','--feeling','joyful').stdout
        assert len(listed.strip().split('\n'))==4
        run('player.py','play','angry-solo-01','--loops','1','--speed','1000')
    print('PASS: 96 distinct clips; 768 ASCII frames; dimensions/timings; sequence/side compositing; text round-trip; playback.')
    print(f'Loop duration range: {min(c["duration_ms"] for c in clips)/1000:.2f}-{max(c["duration_ms"] for c in clips)/1000:.2f} seconds.')

if __name__=='__main__': main()

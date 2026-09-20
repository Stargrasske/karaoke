#!/usr/bin/env python3
"""Rebuild the original editable ASCII dance library. Standard library only."""
import json, math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
W, H = 40, 14
# Arms: elbow and hand offsets from the shoulder. Feet: offsets from hip.
# Every tuple contains left elbow, left hand, right elbow, right hand,
# left knee, left foot, right knee, right foot.
POSES = {
 'a': [(-2,1),(-3,2),(2,1),(3,2),(-1,1),(-2,3),(1,1),(2,3)],
 'b': [(-2,-1),(-4,-2),(2,-1),(4,-2),(-1,1),(-3,3),(1,1),(3,3)],
 'c': [(-2,-1),(-3,-2),(2,1),(4,1),(-2,1),(-3,3),(1,1),(2,2)],
 'd': [(-2,1),(-4,1),(2,-1),(3,-2),(-1,1),(-2,2),(2,1),(3,3)],
 'e': [(-2,0),(-4,0),(2,0),(4,0),(-2,1),(-4,2),(1,1),(2,3)],
 'f': [(-2,0),(-4,0),(2,0),(4,0),(-1,1),(-2,3),(2,1),(4,2)],
 'g': [(-2,1),(-1,2),(2,1),(1,2),(-2,1),(-3,2),(2,1),(3,2)],
 'h': [(-2,-1),(-1,-2),(2,-1),(1,-2),(-1,1),(-1,3),(1,1),(1,3)],
 'i': [(-2,0),(-3,-1),(2,0),(3,1),(-1,1),(1,3),(1,1),(-1,3)],
 'j': [(-2,0),(-3,1),(2,0),(3,-1),(-1,1),(-3,3),(1,1),(3,3)],
 'k': [(-2,0),(-2,-2),(2,0),(2,2),(-2,1),(-2,3),(2,0),(3,0)],
 'l': [(-2,0),(-2,2),(2,0),(2,-2),(-2,0),(-3,0),(2,1),(2,3)],
 'm': [(-2,1),(-1,1),(2,1),(1,1),(-1,1),(-1,3),(1,1),(1,3)],
 'n': [(-2,1),(-3,3),(2,1),(3,3),(-1,1),(-2,3),(1,1),(2,3)],
 'o': [(-2,0),(-4,-1),(2,1),(1,2),(-2,1),(-4,3),(1,1),(1,3)],
 'p': [(-2,1),(-1,2),(2,0),(4,-1),(-1,1),(-1,3),(2,1),(4,3)],
 'q': [(-2,0),(-4,0),(2,-1),(3,-2),(-1,1),(-1,3),(2,0),(4,0)],
 'r': [(-2,-1),(-3,-2),(2,0),(4,0),(-2,0),(-4,0),(1,1),(1,3)],
 's': [(-2,1),(-1,0),(2,1),(1,0),(-2,1),(-2,2),(2,1),(2,2)],
 't': [(-2,0),(-4,1),(2,0),(4,1),(-1,1),(-2,3),(1,1),(2,3)],
}
# feeling, face, ms per frame, movement profile, solo A title/poses, solo B title/poses,
# couple A title, couple B title. The moves are expressive ASCII gestures, not dance lessons.
SPECS = [
 ('joyful','^_^',160,'bounce','Star hops','abcbadbd','Heel-kick party','aefbefba','Hand-in-hand hops','Mirror kick party'),
 ('excited','O_O',130,'bounce','Jump-up burst','ghbhghbh','Fast running man','akalkala','Celebration bounce','Trading jump bursts'),
 ('playful','^o^',180,'travel','Side-kick game','aerafqaf','Peekaboo bop','msbsmsbs','Linked skip game','Copycat kicks'),
 ('confident','>_>',210,'travel','Disco strut','aoaqapaf','Shoulder groove','ajijajij','Power-pair strut','Disco challenge'),
 ('romantic','u_u',360,'sway','Soft heart sway','atmtatmt','Floating ballroom step','acatadat','Close linked sway','Courting step-and-answer'),
 ('tender','-u-',380,'sway','Small cradle sway','amtsamts','Gentle open arms','amteamtf','Reassuring handhold','Gentle mirror steps'),
 ('peaceful','-_-',400,'sway','Slow side flow','atetcata','Breathing sway','amabamat','Linked calm sway','Quiet synchronized flow'),
 ('dreamy','~_~',360,'float','Cloud-step ballet','acqcadra','Drifting arm wave','aejtfaia','Floating linked steps','Dream-wave duet'),
 ('shy','._.',320,'small','Tiny toe taps','mamsmams','Peek-and-retreat','mcmamdma','Hesitant handhold','Shy step-and-answer'),
 ('flirty','^_-',240,'travel','Wink-and-point','aoampama','Hip-tease step','aijmaijm','Playful linked sway','Flirt-and-answer'),
 ('silly','o_O',170,'bounce','Rubber chicken','gkg lglgk'.replace(' ',''),'Wobbly elbows','ickdiljc','Goofy linked kicks','Silly copycat'),
 ('proud','^_>',260,'travel','Victory march','ahckahdl','Medal pose bounce','abhbabhb','Victory handhold','Champions salute'),
 ('hopeful','*_*',280,'rise','Reach for stars','amchabdh','Rising step','agcbagdb','Lift-each-other step','Shared upward reaches'),
 ('grateful','u_u',320,'bow','Thank-you bow','amgnamgn','Open-heart step','ambtambt','Thankful linked bow','Bow-and-answer'),
 ('relieved','-u-',320,'release','Exhale shoulder drop','hsntamta','Shake-it-off shuffle','mijtaijt','Shared relief sway','Shake-off duet'),
 ('nostalgic','v_v',340,'sway','Old-time shuffle','aicjaidj','Slow sock-hop','aematfmt','Linked memory sway','Gentle retro shuffle'),
 ('melancholy',';_;',380,'low','Heavy slow sway','nantnant','Dragging toe step','naen nafn'.replace(' ',''),'Comforting linked sway','Quiet call-and-response'),
 ('lonely','._.',390,'low','Reach-and-fold','noamnpam','One-person slow dance','mtnamtn a'.replace(' ',''),'Reconnection handhold','Distant echo steps'),
 ('angry','>_<',170,'stomp','Hard stomp','gkgaglg a'.replace(' ',''),'Punchy battle step','goqgprga','Stomp-together release','Face-off dance battle'),
 ('frustrated','=_=',190,'stomp','Foot-tap rant','akak alal'.replace(' ',''),'Shake-and-reset','gijgmijg','Linked tension release','Trading stomp phrases'),
 ('anxious','o_o',160,'small','Nervous toe shuffle','msimmsjm','Quick tiny steps','makmmalm','Reassuring tiny steps','Nervous echo shuffle'),
 ('surprised','O_o',230,'pop','Startle-and-freeze','msbhmsbh','Pop-back step','agcbagdb','Shared surprise bounce','Pop-and-answer'),
 ('sleepy','-.-',400,'low','Sleepwalk shuffle','nmanntan','Drowsy sway','nctnndtn','Sleepy linked sway','Slow-motion echo'),
 ('determined','>_<',220,'stomp','Steady power march','akalakal','Drive-forward groove','aoqaprao','United power step','Motivation battle'),
]

def draw_line(grid, start, end, joint=False):
    x1,y1=start; x2,y2=end
    n=max(abs(x2-x1),abs(y2-y1))
    ch='-' if y1==y2 else '|' if x1==x2 else ('\\' if (x2-x1)*(y2-y1)>0 else '/')
    for k in range(n+1):
        t=k/max(n,1); x=round(x1+(x2-x1)*t); y=round(y1+(y2-y1)*t)
        if not (0<=x<W and 0<=y<H):
            raise ValueError(f'Clipped limb: {x},{y}')
        grid[y][x]=ch
    if joint: grid[y2][x2]='o'

def motion(profile,i,variant):
    side=[0,-1,-1,0,0,1,1,0][i]
    if profile=='travel': return side*(1+variant),0
    if profile=='bounce': return side,[0,-1,-2,-1,0,-1,-2,-1][i]
    if profile=='float': return side,[0,0,-1,-1,-1,-1,0,0][i]
    if profile=='rise': return side,[1,1,0,-1,-1,0,0,1][i]
    if profile=='stomp': return side,[0,1,0,1,0,1,0,1][i]
    if profile=='bow': return side,[0,0,1,2,1,0,0,0][i]
    if profile=='release': return side,[0,0,1,1,0,0,0,0][i]
    if profile=='pop': return side,[1,1,-2,-2,0,0,-1,0][i]
    if profile=='low': return side,[1,1,2,2,1,1,2,2][i]
    if profile=='small': return (side if i%2 else 0),0
    return side,0

def dancer(g,x,y,key,face,kind,mirror=False,link=None):
    coords=POSES[key]
    def pt(v,base): return (x+(-v[0] if mirror else v[0]),base+v[1])
    shoulder=(x,y+2); hip=(x,y+4)
    for idx in (0,2):
        elbow=pt(coords[idx],y+2); hand=pt(coords[idx+1],y+2)
        physical_side=-1 if (coords[idx][0]*(-1 if mirror else 1))<0 else 1
        if link and physical_side==link[0]: hand=link[1]
        draw_line(g,shoulder,elbow); draw_line(g,elbow,hand,True)
    for idx in (4,6):
        knee=pt(coords[idx],y+4); foot=pt(coords[idx+1],y+4)
        draw_line(g,hip,knee); draw_line(g,knee,foot)
        g[foot[1]][foot[0]]='_' if kind!='robot' else '='
    draw_line(g,(x,y+1),hip)
    if kind=='robot': g[y+3][x]='#'
    elif kind=='cat': g[y+4][x+1]='~'
    elif kind=='sprite': g[y+3][x]='*'
    head={'round':'('+face+')','cat':'/'+face+'\\','robot':'['+face+']','sprite':'{'+face+'}'}[kind]
    for j,ch in enumerate(head): g[y][x-2+j]=ch

def make_clip(spec,mode,variant,index):
    feeling,face,ms,profile,t1,s1,t2,s2,c1,c2=spec
    seq=[s1,s2][variant]; assert len(seq)==8,(feeling,seq)
    title=([t1,t2] if mode=='solo' else [c1,c2])[variant]
    kinds=['round','cat','robot','sprite']; kind=kinds[(index+variant)%4]
    frames=[]
    for i,key in enumerate(seq):
        grid=[[' ']*W for _ in range(H)]
        dx,dy=motion(profile,i,variant)
        if mode=='solo':
            dancer(grid,20+dx,3+dy,key,face,kind)
        elif variant==0:
            # Persistent hand contact: both inside wrists meet at one world-space point.
            center=20+dx; y=3+dy
            dancer(grid,center-5,y,key,face,kind,link=(1,(center,y+3)))
            dancer(grid,center+5,y,key,face,kinds[(index+2)%4],True,link=(-1,(center,y+3)))
        else:
            # Mirrored call/response, two frames apart. Pair opens/closes together.
            sep=[7,7,6,6,7,7,8,8][i]
            response=(i+6)%8
            _,dy2=motion(profile,response,variant)
            dancer(grid,20-sep,3+dy,key,face,kind)
            dancer(grid,20+sep,3+dy2,seq[response],face,kinds[(index+3)%4],True)
        frames.append('\n'.join(''.join(row) for row in grid))
    clip_id=f'{feeling}-{mode}-{variant+1:02d}'
    durations=[ms]*8
    if profile in ('pop','small'):
        durations=[ms,ms,ms,ms*2,ms,ms,ms,ms*2]
    return {'id':clip_id,'title':title,'feeling':feeling,'mode':mode,
            'characters':[kind] if mode=='solo' else [kind,kinds[(index+(2 if variant==0 else 3))%4]],
            'formation':'centered' if mode=='solo' else ('joined inside hands' if variant==0 else 'mirrored two-frame call-and-response'),
            'width':W,'height':H,'loop':True,'durations_ms':durations,
            'duration_ms':sum(durations),'frames':frames}

def main():
    (ROOT/'clips').mkdir(exist_ok=True); (ROOT/'text').mkdir(exist_ok=True)
    clips=[]
    for index,spec in enumerate(SPECS):
        for mode in ('solo','couple'):
            for variant in (0,1):
                c=make_clip(spec,mode,variant,index); clips.append(c)
                (ROOT/'clips'/f'{c["id"]}.json').write_text(json.dumps(c,indent=2)+'\n')
                text=f'{c["id"]} | {c["title"]}\n{c["feeling"]} | {c["mode"]} | {c["duration_ms"]} ms per loop\n40 columns x 14 rows; spaces are significant.\n'
                for i,(frame,ms) in enumerate(zip(c['frames'],c['durations_ms'])):
                    text+=f'\n--- FRAME {i+1:02d} | {ms} ms ---\n{frame}\n'
                (ROOT/'text'/f'{c["id"]}.txt').write_text(text)
    data={'schema_version':1,'name':'ASCII Dance Pocket Library','width':W,'height':H,'clip_count':len(clips),'clips':clips}
    (ROOT/'dance-library.json').write_text(json.dumps(data,indent=2)+'\n')
    catalog=['# Dance index','', '| Feeling | Solo 1 | Solo 2 | Couple 1 | Couple 2 |','|---|---|---|---|---|']
    for spec in SPECS: catalog.append('| '+' | '.join([spec[0],spec[4],spec[6],spec[8],spec[9]])+' |')
    (ROOT/'CATALOG.md').write_text('\n'.join(catalog)+'\n')
    preview=['ASCII DANCE POCKET LIBRARY','96 loops / 24 feelings / 48 solos / 48 couples','Static sample poses. Run player.py for motion.','']
    for feeling in ('joyful','romantic','angry','shy','sleepy','silly'):
        c=next(c for c in clips if c['id']==feeling+'-couple-01')
        preview.extend([f'{feeling.upper()} / {c["title"]}',c['frames'][2],''])
    (ROOT/'SAMPLE-POSES.txt').write_text('\n'.join(preview))
    print(f'Built {len(clips)} loops, {sum(len(c["frames"]) for c in clips)} frames.')

if __name__=='__main__': main()

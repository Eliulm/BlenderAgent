import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'builder'))
import verify as v


def build(output_dir):
    scene=v.new_scene('Overlap detection')
    col=v.asset('overlap',scene=scene,scale_basis='synthetic')
    a=v.extrude(col,'a',[(-10,-10),(10,-10),(10,10),(-10,10)],-10,10,'steel')
    b=v.extrude(col,'b',[(-10,-10),(10,-10),(10,10),(-10,10)],-10,10,'polymer')
    b.location.x=.005
    return col,scene

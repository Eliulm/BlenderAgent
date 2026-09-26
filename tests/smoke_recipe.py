import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'builder'))
import verify as v


def build(output_dir):
    scene=v.new_scene('Generic verifier smoke')
    col=v.asset('smoke',scene=scene,target_bounds_mm=[85,40,40],
                scale_basis='synthetic dimensions',assumptions=[])
    v.extrude(col,'plastic_base',[(-20,-20),(20,-20),(20,20),(-20,20)],
              -5,5,'polymer',basis='synthetic exterior')
    rod=v.cylinder(col,'metal_rod',5,40,'steel',layer='internal',
                   basis='synthetic internal test')
    rod.location.x=.06
    v.add_view(col,'ROD AXIS',(0,0,1))
    v.add_cutaway(col,'Y CUT',(0,0,0),(0,1,0))
    return col,scene

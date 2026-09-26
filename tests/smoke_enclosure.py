"""A hollow polymer enclosure with a separate conductive internal cylinder."""
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'builder'))
import verify as v


def build(output_dir):
    scene=v.new_scene('Generic interior inspection')
    col=v.asset('enclosure',scene=scene,target_bounds_mm=[40,40,40],
                scale_basis='synthetic dimensions')
    n=48
    verts=[]
    for r,z in ((20,-20),(20,20),(12,-20),(12,20)):
        verts += [(r*math.cos(2*math.pi*j/n),r*math.sin(2*math.pi*j/n),z)
                  for j in range(n)]
    faces=[]
    for j in range(n):
        nxt=(j+1)%n
        faces += [(j,nxt,n+nxt,n+j),
                  (2*n+j,3*n+j,3*n+nxt,2*n+nxt),
                  (n+j,n+nxt,3*n+nxt,3*n+j),
                  (j,2*n+j,2*n+nxt,nxt)]
    v.mesh(col,'polymer_housing',verts,faces,'polymer',basis='synthetic exterior')
    v.cylinder(col,'steel_internal',7,28,'steel',layer='internal',
               basis='synthetic internal occupancy')
    v.add_cutaway(col,'HOUSING CROSS SECTION',(0,0,0),(1,0,0))
    return col,scene

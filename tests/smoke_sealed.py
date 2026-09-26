"""Closed polymer housing with a sealed void and an internal metal part."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'builder'))
import verify as v


def cube(extent):
    a=extent/2
    vertices=[(-a,-a,-a),(a,-a,-a),(a,a,-a),(-a,a,-a),
              (-a,-a,a),(a,-a,a),(a,a,a),(-a,a,a)]
    faces=[(3,2,1,0),(4,5,6,7),(0,1,5,4),(1,2,6,5),
           (2,3,7,6),(3,0,4,7)]
    return vertices,faces


def build(output_dir):
    scene=v.new_scene('Sealed enclosure test')
    col=v.asset('sealed',scene=scene,target_bounds_mm=[40,40,40],
                scale_basis='synthetic dimensions')
    outer,faces=cube(40)
    inner,inner_faces=cube(30)
    faces += [tuple(i+8 for i in reversed(face)) for face in inner_faces]
    v.mesh(col,'sealed_polymer_housing',outer+inner,faces,'polymer',
           basis='synthetic cavity shell')
    v.cylinder(col,'metal_core',5,20,'steel',layer='internal',
               basis='synthetic internal part')
    v.add_cutaway(col,'SEALED SECTION',(0,0,0),(1,0,0))
    return col,scene

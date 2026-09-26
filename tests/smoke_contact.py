"""Internal metal touches the polymer bore but occupies no common volume."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from smoke_enclosure import build as enclosure


def build(output_dir):
    col,scene=enclosure(output_dir)
    rod=next(o for o in col.objects if o.get('role')=='steel_internal')
    rod.scale.x=12/7
    rod.scale.y=12/7
    return col,scene

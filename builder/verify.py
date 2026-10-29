"""Generic Blender asset API and isolated verifier.

Recipe contract: build(output_dir) -> (asset_collection, scene). Geometry is in
metres in Blender; the convenience constructors accept millimetres. Run from
the host with: python verify.py build.py --out /path/to/attempt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time
import traceback

try:
    import bpy
    import bmesh
    from mathutils import Matrix, Vector
    from mathutils.bvhtree import BVHTree
except ImportError:
    bpy = None

MM = .001
DOMAIN_RADIUS_MM = 150  # assets must fit the 300 mm simulation sphere
DEFAULT_DIRECTIONS = (
    ('+X', (1, 0, 0)), ('-X', (-1, 0, 0)),
    ('+Y', (0, 1, 0)), ('-Y', (0, -1, 0)),
    ('+Z', (0, 0, 1)), ('-Z', (0, 0, -1)),
    ('OBLIQUE A', (1, -1, .7)), ('OBLIQUE B', (-1, 1, .7)),
)


def new_scene(name='Reference asset'):
    scene = bpy.data.scenes.new(name)
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 1
    if bpy.context.window:
        bpy.context.window.scene = scene
    return scene


def asset(name, *, scene, target_bounds_mm=None, scale_basis='', assumptions=None):
    """Create a physical asset collection, with optional XYZ scale targets."""
    if not isinstance(scene, bpy.types.Scene):
        raise TypeError('scene must be a Blender Scene')
    col = bpy.data.collections.new(name)
    scene.collection.children.link(col)
    col['ba_asset'] = True
    col['target_bounds_mm'] = json.dumps(target_bounds_mm or [None, None, None])
    col['scale_basis'] = scale_basis
    col['assumptions'] = json.dumps(assumptions or [])
    col['views'] = '[]'
    col['cutaways'] = '[]'
    return col


def _color_for(label):
    word = label.lower()
    if any(s in word for s in ('steel', 'iron', 'magnet', 'titanium')):
        return (.14, .19, .24), .8
    if any(s in word for s in ('alumin', 'metal', 'nickel', 'zinc')):
        return (.3, .34, .38), .8
    if any(s in word for s in ('copper', 'brass', 'gold')):
        return (.45, .23, .07), .8
    if any(s in word for s in ('pcb', 'fr4', 'circuit')):
        return (.04, .23, .12), .05
    if any(s in word for s in ('rubber', 'foam', 'fabric', 'textile')):
        return (.08, .09, .1), .0
    return (.16, .18, .2), .0


def _material(label, color, metallic=0.0, roughness=.5):
    mat = bpy.data.materials.get('BA ' + label)
    if mat is None:
        mat = bpy.data.materials.new('BA ' + label)
        mat.use_nodes = True
    mat.diffuse_color = (*color, 1)
    node = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    node.inputs['Base Color'].default_value = (*color, 1)
    node.inputs['Metallic'].default_value = metallic
    node.inputs['Roughness'].default_value = roughness
    return mat


def part(obj, *, role, material, layer='exterior', basis='', assembly='', color=None,
         roughness=.5, confidence='observed'):
    """Tag one exportable physical material region. Existing meshes are welcome."""
    if obj.type != 'MESH' or not role or not material:
        raise ValueError('part needs a mesh, nonempty role, and material label')
    if layer not in ('exterior', 'internal'):
        raise ValueError('layer must be exterior or internal')
    if confidence not in ('observed', 'inferred'):
        raise ValueError('confidence must be observed or inferred')
    obj['ba_part'] = True
    obj['role'] = role
    obj['material_label'] = material
    obj['em_layer'] = layer
    obj['evidence_basis'] = basis
    obj['confidence'] = confidence
    obj['assembly'] = assembly
    base, metallic = _color_for(material)
    mat = _material(role + ' ' + material, color or base, metallic, roughness)
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    for face in obj.data.polygons:
        face.material_index = 0
    return obj


def mesh(col, role, vertices_mm, faces, material, *, layer='exterior', basis='',
         assembly='', color=None, confidence='observed'):
    data = bpy.data.meshes.new(role)
    data.from_pydata([tuple(v * MM for v in xyz) for xyz in vertices_mm], [], faces)
    data.update()
    obj = bpy.data.objects.new(role, data)
    col.objects.link(obj)
    return part(obj, role=role, material=material, layer=layer, basis=basis,
                assembly=assembly, color=color, confidence=confidence)


def loft(col, role, rings_mm, material, **tags):
    if len(rings_mm) < 2 or len({len(r) for r in rings_mm}) != 1:
        raise ValueError('loft needs two or more equal-size rings')
    n = len(rings_mm[0])
    if n < 3:
        raise ValueError('loft rings need at least three vertices')
    vertices = [p for ring in rings_mm for p in ring]
    faces = [tuple(range(n - 1, -1, -1))]
    faces += [(i*n+j, i*n+(j+1)%n, (i+1)*n+(j+1)%n, (i+1)*n+j)
              for i in range(len(rings_mm)-1) for j in range(n)]
    faces.append(tuple((len(rings_mm)-1)*n+j for j in range(n)))
    return mesh(col, role, vertices, faces, material, **tags)


def extrude(col, role, outline_mm, z0_mm, z1_mm, material, **tags):
    if z1_mm <= z0_mm:
        raise ValueError('z1 must exceed z0')
    return loft(col, role,
                [[(x, y, z) for x, y in outline_mm] for z in (z0_mm, z1_mm)],
                material, **tags)


def cylinder(col, role, radius_mm, length_mm, material, *, segments=48, **tags):
    if radius_mm <= 0 or length_mm <= 0:
        raise ValueError('radius and length must be positive')
    ring = [(radius_mm * math.cos(2*math.pi*j/segments),
             radius_mm * math.sin(2*math.pi*j/segments)) for j in range(segments)]
    return extrude(col, role, ring, -length_mm/2, length_mm/2, material, **tags)


def add_view(col, label, direction, *, up=(0, 0, 1)):
    """Add an object-specific solid view after the eight default exterior views."""
    if not label or Vector(direction).length < 1e-9:
        raise ValueError('view needs a label and nonzero direction')
    views = json.loads(col['views'])
    views.append(dict(label=label, direction=list(direction), up=list(up)))
    col['views'] = json.dumps(views)


def add_cutaway(col, label, point_mm, normal, *, direction=None):
    """Define a disposable display-only half-space cut, never an exported cut."""
    if not label or Vector(normal).length < 1e-9:
        raise ValueError('cutaway needs a label and nonzero normal')
    cuts = json.loads(col['cutaways'])
    cuts.append(dict(label=label, point_mm=list(point_mm), normal=list(normal),
                     direction=list(direction or normal)))
    col['cutaways'] = json.dumps(cuts)


def _world_bmesh(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.transform(obj.matrix_world)
    bm.normal_update()
    return bm


def _inside(tree, point):
    """Majority parity along oblique rays, avoiding normal ambiguity at edges."""
    nearest=tree.find_nearest(point)
    if nearest[0] is None or nearest[3]<1e-10:
        return False
    votes=0
    for direction in ((.811,.317,.491),(-.241,.793,.557),(.429,-.653,.626)):
        ray=Vector(direction).normalized()
        origin=point.copy()
        crossings=0
        for _ in range(4096):
            hit=tree.ray_cast(origin,ray)
            if hit[0] is None:
                break
            crossings+=1
            origin=hit[0]+ray*max(1e-7,max(abs(v) for v in hit[0])*5e-7)
        else:
            raise RuntimeError('containment ray did not converge')
        votes+=crossings%2
    return votes>=2


def _metrics(obj):
    bm = _world_bmesh(obj)
    bmesh.ops.triangulate(bm,faces=list(bm.faces))
    volume = bm.calc_volume(signed=True) / MM**3 if bm.faces else 0
    remaining = set(bm.verts)
    groups=[]
    while remaining:
        stack = [remaining.pop()]
        group=set(stack)
        while stack:
            vertex = stack.pop()
            for edge in vertex.link_edges:
                for other in edge.verts:
                    if other in remaining:
                        remaining.remove(other)
                        stack.append(other)
                        group.add(other)
        groups.append(group)
    shells=[]
    for group in groups:
        faces={face for vertex in group for face in vertex.link_faces}
        shell_volume=sum(face.verts[0].co.dot(face.verts[1].co.cross(face.verts[2].co))/6
                         for face in faces)/MM**3
        vertices=list(group)
        index={vertex:i for i,vertex in enumerate(vertices)}
        tree=BVHTree.FromPolygons([vertex.co for vertex in vertices],
                                 [[index[vertex] for vertex in face.verts] for face in faces],
                                 all_triangles=True)
        shells.append((shell_volume,vertices,tree))
    positive=[shell for shell in shells if shell[0]>1e-8]
    cavities=[shell for shell in shells if shell[0]<-1e-8]
    connected_volume=len(positive)==1 and len(positive)+len(cavities)==len(shells)
    if connected_volume and cavities:
        connected_volume=all(_inside(positive[0][2],vertex.co)
                             for _,vertices,_ in cavities for vertex in vertices)
    result = dict(vertices=len(bm.verts), faces=len(bm.faces),
                  nonmanifold_edges=sum(not edge.is_manifold for edge in bm.edges),
                  degenerate_faces=sum(face.calc_area() < 1e-16 for face in bm.faces),
                  components=len(groups),positive_shells=len(positive),
                  enclosed_cavities=len(cavities),connected_volume=connected_volume,
                  volume_mm3=volume)
    bm.free()
    return result


def _prepare_mesh(obj):
    """Remove numerical slivers before validation and OBJ triangulation.

    The 0.0001 mm tolerance is far below the intended EM mesh resolution. It
    never joins separate physical objects or fills holes.
    """
    bm=bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm,verts=list(bm.verts),dist=1e-7)
    bmesh.ops.dissolve_degenerate(bm,edges=list(bm.edges),dist=1e-7)
    bmesh.ops.triangulate(bm,faces=list(bm.faces))
    bmesh.ops.dissolve_degenerate(bm,edges=list(bm.edges),dist=1e-7)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _bbox_intersects(a, b):
    aa = [a.matrix_world @ Vector(p) for p in a.bound_box]
    bb = [b.matrix_world @ Vector(p) for p in b.bound_box]
    return all(min(max(p[i] for p in aa), max(p[i] for p in bb)) -
               max(min(p[i] for p in aa), min(p[i] for p in bb)) > 1e-8
               for i in range(3))


def _union_overlap(a,b,scene):
    left=a.copy()
    left.data=a.data.copy()
    scene.collection.objects.link(left)
    try:
        mod=left.modifiers.new('BA union check','BOOLEAN')
        mod.operation='UNION'
        mod.solver='EXACT'
        mod.object=b
        with bpy.context.temp_override(scene=scene,view_layer=scene.view_layers[0],
                                       object=left,active_object=left,
                                       selected_objects=[left],selected_editable_objects=[left]):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        met=_metrics(left)
        if met['nonmanifold_edges'] or met['degenerate_faces']:
            return None
        difference=_metrics(a)['volume_mm3']+_metrics(b)['volume_mm3']-met['volume_mm3']
        return max(0,difference) if difference>=-.005 else None
    except Exception:
        return None
    finally:
        data=left.data
        bpy.data.objects.remove(left,do_unlink=True)
        if data.users==0:
            bpy.data.meshes.remove(data)


def _overlap(a, b, scene):
    """Return overlap volume or None if Blender cannot establish the result."""
    copy = a.copy()
    copy.data = a.data.copy()
    scene.collection.objects.link(copy)
    try:
        mod = copy.modifiers.new('BA intersection', 'BOOLEAN')
        mod.operation = 'INTERSECT'
        mod.solver = 'EXACT'
        mod.object = b
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0],
                                       object=copy, active_object=copy,
                                       selected_objects=[copy], selected_editable_objects=[copy]):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        met = _metrics(copy)
        if abs(met['volume_mm3'])<=.005:
            return 0
        if met['nonmanifold_edges'] or met['degenerate_faces']:
            return _union_overlap(a,b,scene)
        return abs(met['volume_mm3'])
    except Exception:
        return _union_overlap(a,b,scene)
    finally:
        data = copy.data
        bpy.data.objects.remove(copy, do_unlink=True)
        if data.users == 0:
            bpy.data.meshes.remove(data)


def validate(col, scene):
    scene.view_layers[0].update()
    objects = [o for o in col.all_objects if o.type == 'MESH']
    errors, overlaps, parts = [], [], []
    if not objects:
        errors.append('empty asset')
    roles = set()
    points = []
    for obj in objects:
        role = obj.get('role')
        if not obj.get('ba_part') or not role or not obj.get('material_label'):
            errors.append('untagged part: ' + obj.name)
        elif role in roles:
            errors.append('duplicate part role: ' + role)
        roles.add(role)
        if obj.get('em_layer') not in ('exterior', 'internal'):
            errors.append('invalid EM layer: ' + obj.name)
        if obj.modifiers:
            errors.append('unapplied modifiers: ' + obj.name)
        met = _metrics(obj)
        parts.append(dict(role=role, material=obj.get('material_label'), **met))
        if (met['nonmanifold_edges'] or met['degenerate_faces'] or
                not met['connected_volume'] or met['volume_mm3'] <= .000001):
            errors.append('invalid closed solid: ' + obj.name)
        world = [obj.matrix_world @ v.co for v in obj.data.vertices]
        if any(not math.isfinite(c) for p in world for c in p):
            errors.append('nonfinite geometry: ' + obj.name)
        points.extend(world)
    bounds = [(max(p[i] for p in points) - min(p[i] for p in points)) / MM
              for i in range(3)] if points else [0, 0, 0]
    radius = 0
    if points:
        centre = Vector([(max(p[i] for p in points) + min(p[i] for p in points)) / 2
                         for i in range(3)])
        radius = max((p - centre).length for p in points) / MM
        if radius > DOMAIN_RADIUS_MM:
            errors.append('exceeds simulation sphere: %.1f mm from bounding-box centre, '
                          'limit %d mm' % (radius, DOMAIN_RADIUS_MM))
    try:
        targets = json.loads(col['target_bounds_mm'])
        if len(targets) != 3:
            raise ValueError()
        for i, target in enumerate(targets):
            if target is not None and (target <= 0 or abs(bounds[i]-target)/target > .25):
                errors.append('scale mismatch axis '+str(i)+': observed %.1f mm, target %s mm' %
                              (bounds[i], target))
    except Exception:
        errors.append('invalid target_bounds_mm')
    if not errors:
        for i, a in enumerate(objects):
            for b in objects[i+1:]:
                if not _bbox_intersects(a, b):
                    continue
                volume = _overlap(a, b, scene)
                if volume is None:
                    errors.append('inconclusive overlap: '+a.name+' / '+b.name)
                elif volume > .005:
                    overlaps.append(dict(a=a.get('role'), b=b.get('role'), volume_mm3=volume))
        if overlaps:
            errors.append('positive-volume overlaps')
    return dict(ok=not errors, errors=errors, overlaps=overlaps,
                bounds_mm=bounds, radius_mm=radius, parts=parts,
                unchecked=['surface self-intersection', 'material constitutive values',
                           'electromagnetic response', 'functional mechanics'])


def _geometry_digest(col):
    records = []
    for obj in sorted(col.all_objects, key=lambda x: x.name):
        if obj.type != 'MESH':
            continue
        records.append((obj.name, [tuple(row) for row in obj.matrix_world],
                        [tuple(v.co) for v in obj.data.vertices],
                        [tuple(f.vertices) for f in obj.data.polygons], dict(obj.items())))
    return hashlib.sha256(json.dumps(records, default=str, sort_keys=True).encode()).hexdigest()


def _clean_display_mesh(obj):
    """Tidy tiny CSG slivers on a disposable preview mesh only."""
    bm=bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.dissolve_limit(bm,angle_limit=.0001,
                             verts=list(bm.verts),edges=list(bm.edges))
    bmesh.ops.remove_doubles(bm,verts=list(bm.verts),dist=1e-7)
    bmesh.ops.dissolve_degenerate(bm,edges=list(bm.edges),dist=1e-7)
    bmesh.ops.triangulate(bm,faces=list(bm.faces))
    bmesh.ops.dissolve_degenerate(bm,edges=list(bm.edges),dist=1e-7)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _basis(direction, up=(0, 0, 1)):
    z = Vector(direction).normalized()
    y = Vector(up) - z * z.dot(Vector(up))
    if y.length < .01:
        seed = Vector((0, 1, 0) if abs(z.y) < .9 else (1, 0, 0))
        y = seed - z * z.dot(seed)
    y.normalize()
    x = y.cross(z).normalized()
    return Matrix(((x.x, x.y, x.z, 0),
                   (y.x, y.y, y.z, 0),
                   (z.x, z.y, z.z, 0),
                   (0, 0, 0, 1)))


def _display_cut(obj, point_mm, normal, scene, display):
    """Clip a display copy, falling back to the uncut copy on CSG failure."""
    backup = obj.data.copy()
    matrix = obj.matrix_world.copy()
    try:
        obj.data.transform(obj.matrix_world)
        obj.matrix_world = Matrix.Identity(4)
        point = Vector(point_mm) * MM
        n = Vector(normal).normalized()
        pts = [v.co for v in obj.data.vertices]
        d = [n.dot(p-point) for p in pts]
        if max(d) <= 1e-9:
            return True, None
        if min(d) >= -1e-9:
            return False, None
        seed = Vector((0, 0, 1) if abs(n.z) < .9 else (0, 1, 0))
        u = n.cross(seed).normalized()
        v = n.cross(u)
        size = max((p-point).length for p in pts)*3 + .02
        verts = [point + u*x + v*y + n*z for z in (0, size)
                 for x, y in ((-size,-size),(size,-size),(size,size),(-size,size))]
        data = bpy.data.meshes.new('BA display halfspace')
        data.from_pydata(verts, [], [(3,2,1,0),(4,5,6,7),(0,1,5,4),
                                     (1,2,6,5),(2,3,7,6),(3,0,4,7)])
        tool = bpy.data.objects.new('BA display halfspace', data)
        display.objects.link(tool)
        try:
            mod = obj.modifiers.new('BA display cut', 'BOOLEAN')
            mod.operation = 'DIFFERENCE'
            mod.solver = 'EXACT'
            mod.object = tool
            with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0],
                                           object=obj, active_object=obj,
                                           selected_objects=[obj], selected_editable_objects=[obj]):
                bpy.ops.object.modifier_apply(modifier=mod.name)
        finally:
            bpy.data.objects.remove(tool, do_unlink=True)
            if data.users == 0:
                bpy.data.meshes.remove(data)
        _clean_display_mesh(obj)
        met = _metrics(obj)
        if met['nonmanifold_edges'] or met['degenerate_faces']:
            raise ValueError('invalid presentation cut')
        return bool(obj.data.polygons), None
    except Exception as exc:
        for mod in list(obj.modifiers):
            obj.modifiers.remove(mod)
        damaged = obj.data
        obj.data = backup
        obj.matrix_world = matrix
        if damaged.users == 0:
            bpy.data.meshes.remove(damaged)
        return True, str(exc)
    finally:
        if backup.users == 0:
            bpy.data.meshes.remove(backup)


def _silhouette(original, direction, display, depth):
    """Exterior context in an internal panel, as contour curves only."""
    bm = _world_bmesh(original)
    eye = Vector(direction).normalized()
    data = bpy.data.curves.new('BA context', 'CURVE')
    data.dimensions = '3D'
    data.bevel_depth = max(depth*.0004, .00003)
    for edge in bm.edges:
        if len(edge.link_faces) != 2:
            continue
        signs = [f.normal.dot(eye) >= 0 for f in edge.link_faces]
        if signs[0] == signs[1]:
            continue
        spline = data.splines.new('POLY')
        spline.points.add(1)
        for dest, vert in zip(spline.points, edge.verts):
            dest.co = (*vert.co, 1)
    bm.free()
    obj = bpy.data.objects.new('CONTEXT '+original.name, data)
    display.objects.link(obj)
    data.materials.append(_material('context', (.22,.28,.34), 0, .8))
    return obj


def preview(col, path, *, width=1440, samples=16):
    """Render multi-angle sheet; every panel is a disposable display copy."""
    physical = [o for o in col.all_objects if o.type == 'MESH' and o.data.vertices]
    if not physical:
        raise ValueError('no renderable mesh')
    points = [o.matrix_world @ v.co for o in physical for v in o.data.vertices]
    lo = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    hi = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    center = (lo+hi)/2
    span = max(hi-lo)
    pad = max(.003, span*.03)
    font = max(.002, span*.026)
    scene = new_scene('Inspection only: '+col.name)
    scene['ba_presentation_only'] = True
    world = bpy.data.worlds.new('BA studio')
    world.use_nodes = True
    scene.world = world
    background = next(n for n in world.node_tree.nodes if n.type == 'BACKGROUND')
    background.inputs['Color'].default_value = (.65,.7,.77,1)
    background.inputs['Strength'].default_value = .55
    display = bpy.data.collections.new('DISPLAY ONLY')
    scene.collection.children.link(display)
    panels = [dict(label=label, direction=direction, mode='solid')
              for label, direction in DEFAULT_DIRECTIONS]
    panels += [dict(**item, mode='solid') for item in json.loads(col['views'])]
    internals = [o for o in physical if o.get('em_layer') == 'internal']
    if internals:
        panels += [dict(label='INTERNALS A', direction=(1,-1,.7), mode='internal'),
                   dict(label='INTERNALS B', direction=(-1,1,.7), mode='internal')]
        cutaways = json.loads(col['cutaways'])
        if not cutaways:
            cutaways = [dict(label='CENTER CUT +X', point_mm=list(center/MM),
                             normal=(1,0,0), direction=(1,0,0)),
                        dict(label='CENTER CUT +Y', point_mm=list(center/MM),
                             normal=(0,1,0), direction=(0,1,0))]
        panels += [dict(**item, mode='cut') for item in cutaways]
    if len(panels) > 16:
        raise ValueError('at most 16 preview views; remove redundant custom views')
    prepared = []
    warnings = []
    for index, panel in enumerate(panels):
        rotation = _basis(panel['direction'], panel.get('up',(0,0,1)))
        current = []
        selected = physical if panel['mode'] != 'internal' else internals
        for original in selected:
            copy = original.copy()
            copy.data = original.data.copy()
            copy.name = 'DISPLAY %02d %s' % (index, original.get('role',original.name))
            display.objects.link(copy)
            copy['presentation_only'] = True
            if panel['mode'] == 'cut':
                keep, warning = _display_cut(copy, panel['point_mm'], panel['normal'], scene, display)
                if warning:
                    warnings.append(dict(panel=panel['label'], role=original.get('role'), reason=warning))
                if not keep:
                    data = copy.data
                    bpy.data.objects.remove(copy, do_unlink=True)
                    if data.users == 0:
                        bpy.data.meshes.remove(data)
                    continue
            if panel['mode'] != 'solid':
                color, _ = _color_for(original.get('material_label','unknown'))
                copy.data.materials.clear()
                copy.data.materials.append(_material('diagnostic '+original.get('role','part'),color,.15,.65))
            transformed = rotation @ Matrix.Translation(-center) @ copy.matrix_world
            points2 = [transformed @ v.co for v in copy.data.vertices]
            current.append((copy, transformed, points2))
        if panel['mode'] == 'internal':
            for original in physical:
                if original.get('em_layer') == 'internal':
                    continue
                context = _silhouette(original, panel['direction'], display, span)
                transformed = rotation @ Matrix.Translation(-center)
                points2 = [transformed @ Vector(p.co[:3])
                           for spline in context.data.splines for p in spline.points]
                if points2:
                    current.append((context, transformed, points2))
        if not current:
            warnings.append(dict(panel=panel['label'], reason='empty panel'))
            continue
        flat = [p for _,_,vertices in current for p in vertices]
        bounds = [(min(p[i] for p in flat), max(p[i] for p in flat)) for i in range(3)]
        prepared.append((panel['label'], current, bounds))
    if not prepared:
        raise ValueError('all preview panels are empty')
    columns = 3 if len(prepared) > 4 else 2
    rows = math.ceil(len(prepared)/columns)
    cell_w = max(b[0][1]-b[0][0] for _,_,b in prepared) + 2*pad
    cell_h = max(b[1][1]-b[1][0] for _,_,b in prepared) + 2*pad + 2*font
    all_points = []
    labels = []
    for index, (label, current, bounds) in enumerate(prepared):
        cx = (index%columns-(columns-1)/2)*cell_w
        cy = ((rows-1)/2-index//columns)*cell_h
        mid = Vector(tuple((a+b)/2 for a,b in bounds))
        shift = Matrix.Translation(Vector((cx,cy,0))-mid)
        for obj, matrix, vertices in current:
            obj.matrix_world = shift @ matrix
            all_points.extend(shift @ p for p in vertices)
        labels.append((label,cx-cell_w/2+pad*.4,cy+cell_h/2-font*1.5))
    zmin = min(p.z for p in all_points)
    zmax = max(p.z for p in all_points)
    ink = bpy.data.materials.new('BA label ink')
    ink.use_nodes = True
    nodes = ink.node_tree.nodes
    nodes.clear()
    output = nodes.new('ShaderNodeOutputMaterial')
    emission = nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value = (.012,.019,.028,1)
    ink.node_tree.links.new(emission.outputs[0],output.inputs['Surface'])
    for label,x,y in labels:
        data = bpy.data.curves.new('BA label','FONT')
        data.body = label
        data.size = font
        text = bpy.data.objects.new('LABEL '+label,data)
        display.objects.link(text)
        text.location = (x,y,zmax+.004)
        data.materials.append(ink)
    extent = max(cell_w*columns,cell_h*rows)
    floor_data = bpy.data.meshes.new('BA studio floor')
    e = extent*2
    floor_data.from_pydata([(-e,-e,0),(e,-e,0),(e,e,0),(-e,e,0)], [], [(0,1,2,3)])
    floor = bpy.data.objects.new('BA studio floor',floor_data)
    scene.collection.objects.link(floor)
    floor.location.z = zmin-pad*.6
    floor_data.materials.append(_material('floor',(.82,.85,.89),0,.8))
    for i, (dx,dy,dz,power,size) in enumerate(((-.6,.3,1.4,8,.9),(.6,-.4,1.1,5,.8),(.1,.9,.9,3,.7))):
        data = bpy.data.lights.new('BA studio '+str(i),'AREA')
        data.energy = power*(extent/.4)**2
        data.size = size*extent
        light = bpy.data.objects.new(data.name,data)
        scene.collection.objects.link(light)
        light.location = (dx*extent,dy*extent,zmax+dz*extent)
        light.rotation_euler = (-light.location).to_track_quat('-Z','Y').to_euler()
    data = bpy.data.cameras.new('BA preview camera')
    data.type = 'ORTHO'
    camera = bpy.data.objects.new(data.name,data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.location = (0,0,zmax+extent*3)
    data.clip_start = .001
    data.clip_end = max(100,extent*10)
    sheet_w = columns*cell_w
    sheet_h = rows*cell_h
    scene.render.resolution_x = width
    scene.render.resolution_y = round(width*sheet_h/sheet_w)
    scene.render.resolution_percentage = 100
    data.ortho_scale = 1
    frame = data.view_frame(scene=scene)
    data.ortho_scale = max(sheet_w/(max(p.x for p in frame)-min(p.x for p in frame)),
                           sheet_h/(max(p.y for p in frame)-min(p.y for p in frame)))*1.03
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = samples
    scene.cycles.use_denoising = True
    scene.render.image_settings.file_format = 'PNG'
    scene.render.film_transparent = False
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True, scene=scene.name)
    return scene, warnings, [p[0] for p in prepared]


def _remove_presentation(scene):
    """Destroy render copies before saving the physical-only .blend file."""
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj,do_unlink=True)
    bpy.data.scenes.remove(scene)
    for collection in list(bpy.data.collections):
        if collection.users==0:
            bpy.data.collections.remove(collection)
    for datablocks in (bpy.data.meshes,bpy.data.curves,bpy.data.cameras,
                       bpy.data.lights,bpy.data.worlds,bpy.data.materials):
        for block in list(datablocks):
            if block.users==0:
                datablocks.remove(block)


def export_parts(col, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for obj in sorted((o for o in col.all_objects if o.type=='MESH'), key=lambda x:x['role']):
        role = re.sub('[^a-zA-Z0-9_-]+','_',obj['role']).strip('_')
        material = re.sub('[^a-zA-Z0-9_-]+','_',obj['material_label']).strip('_')
        path = directory / f'{role}__{material}.obj'
        if path.exists():
            raise ValueError('duplicate OBJ filename: '+path.name)
        meshdata = obj.data
        meshdata.calc_loop_triangles()
        lines = ['# units: metres', '# role: '+obj['role'],
                 '# material_label: '+obj['material_label'],
                 '# em_layer: '+obj['em_layer'], 'o '+role]
        lines += ['v %.10g %.10g %.10g' % tuple(obj.matrix_world @ v.co)
                  for v in meshdata.vertices]
        lines += ['f %d %d %d' % tuple(i+1 for i in tri.vertices)
                  for tri in meshdata.loop_triangles]
        path.write_text('\n'.join(lines)+'\n')
        paths.append(str(path))
    return paths


def _worker(recipe, out, width, samples):
    started = time.monotonic()
    col = scene = None
    result = dict(ok=False, geometry_ok=False, preview_status='not_attempted')
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        namespace = runpy.run_path(str(recipe), run_name='builder_recipe')
        col, scene = namespace['build'](out)
        if not isinstance(col,bpy.types.Collection) or not isinstance(scene,bpy.types.Scene):
            raise TypeError('build must return (Collection, Scene)')
        if not col.get('ba_asset') or any(o.name not in scene.objects for o in col.all_objects):
            raise ValueError('return a tagged asset collection linked to the returned scene')
        for obj in col.all_objects:
            if obj.type=='MESH' and _metrics(obj)['degenerate_faces']:
                _prepare_mesh(obj)
        report = validate(col, scene)
        result.update(geometry_ok=report['ok'], errors=report['errors'],
                      overlaps=report['overlaps'], bounds_mm=report['bounds_mm'],
                      radius_mm=report['radius_mm'],
                      parts=len(report['parts']))
    except Exception:
        result['error'] = traceback.format_exc()
        if col is None:
            col = next((c for c in bpy.data.collections if c.get('ba_asset')),None)
            scene = next((s for s in bpy.data.scenes if col and col.name in s.collection.children),None)
    if col is not None and any(o.type=='MESH' and o.data.vertices for o in col.all_objects):
        if scene is not None:
            scene.view_layers[0].update()
        before = _geometry_digest(col)
        try:
            path = out/('preview.png' if result['geometry_ok'] else 'rejected-preview.png')
            presentation,warnings,panels = preview(col,path,width=width,samples=samples)
            result.update(preview=str(path),preview_status='degraded' if warnings else 'ready',
                          preview_warnings=warnings,preview_panels=panels)
            _remove_presentation(presentation)
        except Exception:
            result.update(preview_status='failed',preview_error=traceback.format_exc())
        if _geometry_digest(col) != before:
            result.update(geometry_ok=False,error='preview mutated physical source geometry')
    if result['geometry_ok'] and result['preview_status'] in ('ready','degraded'):
        try:
            paths = export_parts(col,out/'parts')
            for unused in list(bpy.data.scenes):
                if unused!=scene and not unused.objects:
                    bpy.data.scenes.remove(unused)
            if bpy.context.window:
                bpy.context.window.scene = scene
            bpy.ops.wm.save_as_mainfile(filepath=str(out/'asset.blend'))
            result.update(ok=True,asset=str(out/'asset.blend'),objs=paths)
        except Exception:
            result.update(ok=False,error=traceback.format_exc())
    result['seconds'] = round(time.monotonic()-started,2)
    (out/'result.json').write_text(json.dumps(result,indent=2))
    return result


def main():
    argv = sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recipe',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--width',type=int,default=1440)
    parser.add_argument('--samples',type=int,default=16)
    parser.add_argument('--timeout',type=int,default=600)
    parser.add_argument('--blender',default=os.environ.get('BLENDER_BIN') or shutil.which('blender') or
                        '/Applications/Blender.app/Contents/MacOS/Blender')
    args = parser.parse_args(argv)
    recipe = args.recipe.expanduser().resolve()
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True,exist_ok=True)
    if args.worker:
        result = _worker(recipe,out,args.width,args.samples)
        return 0 if result['ok'] else 1
    cmd = [args.blender,'--background','--factory-startup','--python-exit-code','1',
           '--python',str(Path(__file__).resolve()),'--','--worker',str(recipe),
           '--out',str(out),'--width',str(args.width),'--samples',str(args.samples)]
    try:
        with (out/'blender.log').open('w') as stream:
            proc = subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT,
                                  timeout=args.timeout,check=False)
        result_path = out/'result.json'
        result = json.loads(result_path.read_text()) if result_path.is_file() else \
            dict(ok=False,error='Blender exited without a result',exit_code=proc.returncode)
        if proc.returncode and result.get('ok'):
            result = dict(ok=False,error='Blender exited abnormally after export',exit_code=proc.returncode)
    except (OSError,subprocess.TimeoutExpired) as exc:
        result = dict(ok=False,error=str(exc))
    result['log'] = str(out/'blender.log')
    (out/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Projected decals as mesh decals.

The game draws an ApiRenderProjectedDecal in the deferred pass: for every
G-buffer pixel inside the decal's unit box (the entity transform), it
reconstructs world position, maps it into box space [0,1]^3, discards pixels
outside the box or whose surface faces away from the box +Y axis beyond the
ProjectedDecal Cutoff Angle (scortch / footprint DXBC: dot > cos(85 deg)), and
samples with uv = (box.x, box.z).

Blender has no screen-space decals, so the receiving surface is copied: every
receiver triangle overlapping the box is clipped to the box (six bisects),
pushed 2 mm along its normal, and given UV0 = native box (x, z) in [0,1].
"""
import math
import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

OFFSET = 0.002  # metres along the surface normal, against z-fighting


def _world_aabbs(objects):
    boxes = np.empty((len(objects), 2, 3))
    for i, o in enumerate(objects):
        pts = np.array([o.matrix_world @ Vector(c) for c in o.bound_box])
        boxes[i, 0] = pts.min(0); boxes[i, 1] = pts.max(0)
    return boxes


def build_decals(decals, receivers, collection, report):
    """decals: list of dicts {name, matrix (Blender world 4x4), material, cutoff}."""
    receivers = [o for o in receivers if o.type == 'MESH' and o.data and len(o.data.polygons)]
    if not decals or not receivers:
        return 0
    boxes = _world_aabbs(receivers)
    meshes_cache = {}
    made = 0
    for d in decals:
        M = d['matrix']; Minv = M.inverted()
        corners = np.array([M @ Vector((x, y, z)) for x in (-.5, .5) for y in (-.5, .5) for z in (-.5, .5)])
        lo, hi = corners.min(0), corners.max(0)
        hit = np.where(np.all(boxes[:, 0] <= hi, 1) & np.all(boxes[:, 1] >= lo, 1))[0]
        if not len(hit):
            report['decals_without_receivers'] = report.get('decals_without_receivers', 0) + 1
            continue
        cos_cut = math.cos(math.radians(d.get('cutoff', 85.0)))
        bm = bmesh.new()
        for i in hit:
            o = receivers[i]; me = o.data
            key = me.name
            if key not in meshes_cache:
                me.calc_loop_triangles()
                co = np.empty(len(me.vertices) * 3); me.vertices.foreach_get('co', co)
                tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int64); me.loop_triangles.foreach_get('vertices', tris)
                meshes_cache[key] = (co.reshape(-1, 3), tris.reshape(-1, 3))
            co, tris = meshes_cache[key]
            if not len(tris):
                continue
            T = np.array(Minv @ o.matrix_world)
            local = co @ T[:3, :3].T + T[:3, 3]
            tv = local[tris]                                   # (n,3,3)
            tmin = tv.min(1); tmax = tv.max(1)
            sel = np.all(tmin <= .5, 1) & np.all(tmax >= -.5, 1)
            if not sel.any():
                continue
            tv = tv[sel]
            n = np.cross(tv[:, 1] - tv[:, 0], tv[:, 2] - tv[:, 0])
            ln = np.linalg.norm(n, axis=1); ok = ln > 1e-12
            tv = tv[ok]; n = n[ok] / ln[ok, None]
            # Box +Z in Blender local is native +Y, the projection axis.
            face = n[:, 2] > cos_cut
            tv = tv[face]; n = n[face]
            for tri, nn in zip(tv, n):
                vs = [bm.verts.new(tuple(p + nn * OFFSET)) for p in tri]
                try:
                    bm.faces.new(vs)
                except ValueError:
                    pass
        if not bm.faces:
            bm.free(); continue
        for axis in range(3):
            for sign in (1, -1):
                no = Vector((0, 0, 0)); no[axis] = sign
                geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
                bmesh.ops.bisect_plane(bm, geom=geom, plane_co=no * .5, plane_no=no, clear_outer=True)
        if not bm.faces:
            bm.free(); continue
        mesh = bpy.data.meshes.new(d['name'])
        bm.to_mesh(mesh); bm.free()
        uv = mesh.uv_layers.new(name='UV0')
        # Native box space: x_n = -x_b, z_n = -y_b; uv = (x_n + .5, z_n + .5).
        uvs = []
        for loop in mesh.loops:
            p = mesh.vertices[loop.vertex_index].co
            uvs += [(-p.x) + .5, (-p.y) + .5]
        uv.data.foreach_set('uv', uvs)
        mesh.materials.append(d['material'])
        obj = bpy.data.objects.new(d['name'], mesh)
        collection.objects.link(obj); obj.matrix_world = M
        obj['lswtss_decal'] = True; obj.visible_shadow = False
        made += 1
    return made

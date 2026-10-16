"""Build a native Landscape (terrain) object and its decoded layer-blend material.

Geometry: an n x n grid in the Landscape Entity's local game space (origin at
the min-XZ corner, x across, z down the map, y up), converted with the
exporter's basis (game (x, y, z) -> Blender (-x, -z, y)) and parented to the
entity object, so the entity transform and any scene-reference chain apply.
Quads under the hole mask are removed.

Material (kashyyyk_landscape DXBC): layer UV = terrain UV * UV Repeat * layer
scale; weights from weights0 (layers 1-4, R/G/B/A) and weights1 (5-8);
p = w*h with h = layer Data.B; m = max(p);
b = m - min(m, max(0, (m - p) * HeightBlendFactor)); w' = b / sum(b).
Albedo, NormalR (normal xy + roughness in B) are blended with w'.
"""
from pathlib import Path

import bpy
import numpy as np


def _image(path, colorspace, cache):
    key = (path, colorspace)
    if key not in cache:
        im = bpy.data.images.load(path, check_existing=True)
        im.colorspace_settings.name = colorspace
        im.pack()
        cache[key] = im
    return cache[key]


def build_landscape(land, parent, collection, name):
    prep = land['prepared']
    n = prep['resolution']
    heights = np.fromfile(prep['heights'], dtype='<f4').reshape(n, n)
    size = land['world_size']; hscale = land['height_scale']
    cell = size / (n - 1)
    jj, ii = np.mgrid[0:n, 0:n]
    # Heightmap row 0 is the far (max-z) edge: game local
    # (i*cell, h*hscale, (n-1-j)*cell), fitted on Kachirho where 71% of the
    # landscape's own art layer sits within 0.5 m (16% unflipped).
    # Converted to Blender local with (-x, -z, y).
    verts = np.stack([-(ii * cell), -((n - 1 - jj) * cell), heights * hscale], axis=-1).reshape(-1, 3).astype(np.float32)
    a = (jj[:-1, :-1] * n + ii[:-1, :-1]).reshape(-1)
    # With rows running toward -game z, this order keeps face normals up (+Z).
    quads = np.stack([a, a + n, a + n + 1, a + 1], axis=-1)
    if prep.get('holes'):
        holes = np.frombuffer(Path(prep['holes']).read_bytes(), dtype=np.uint8)
        quads = quads[holes == 0]
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set('co', verts.reshape(-1))
    mesh.loops.add(len(quads) * 4)
    mesh.loops.foreach_set('vertex_index', quads.reshape(-1).astype(np.int32))
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set('loop_start', np.arange(0, len(quads) * 4, 4, dtype=np.int32))
    mesh.polygons.foreach_set('loop_total', np.full(len(quads), 4, dtype=np.int32))
    mesh.update(calc_edges=True)
    mesh.validate()
    mesh.polygons.foreach_set('use_smooth', np.ones(len(quads), dtype=bool))
    # Game terrain UV: u along x, v along z, 0..1 over the map.
    uvs = np.stack([ii / (n - 1), jj / (n - 1)], axis=-1).reshape(-1, 2)
    loop_uv = uvs[quads.reshape(-1)].astype(np.float32)
    uv = mesh.uv_layers.new(name='UV0')
    uv.data.foreach_set('uv', loop_uv.reshape(-1))
    mesh.materials.append(landscape_material(land, name))
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.parent = parent
    from mathutils import Matrix
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_basis = Matrix.Identity(4)
    obj['lswtss_landscape_guid'] = land['guid']
    obj['lswtss_landscape'] = (f"{prep['source_resolution']}^2 heightmap at {n}^2, {size} units, height {hscale}, "
                               f"{prep['layers']} layers, UV repeat {prep['uv_repeat']}, height blend {prep['height_blend_factor']}")
    return obj


def landscape_material(land, name):
    prep = land['prepared']
    mat = bpy.data.materials.new('Landscape: ' + name)
    mat.use_nodes = True
    nt = mat.node_tree; nodes = nt.nodes; links = nt.links
    nodes.clear()
    cache = {}
    col = [0]

    def node(t, label=None):
        nd = nodes.new(t); nd.location = (200 * (col[0] % 12) - 2400, -160 * (col[0] // 12)); col[0] += 1
        if label: nd.label = label
        return nd

    def math(op, a, b=None):
        nd = node('ShaderNodeMath'); nd.operation = op
        for i, v in enumerate((a, b)):
            if v is None: continue
            if isinstance(v, (int, float)): nd.inputs[i].default_value = v
            else: links.new(v, nd.inputs[i])
        return nd.outputs[0]

    out = node('ShaderNodeOutputMaterial'); bsdf = node('ShaderNodeBsdfPrincipled')
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    uvn = node('ShaderNodeUVMap'); uvn.uv_map = 'UV0'
    sep_uv = node('ShaderNodeSeparateXYZ'); links.new(uvn.outputs['UV'], sep_uv.inputs[0])
    u, v = sep_uv.outputs['X'], sep_uv.outputs['Y']
    # Weight maps: DirectX top-left origin -> image (u, 1 - v).
    wvec = node('ShaderNodeCombineXYZ'); links.new(u, wvec.inputs['X']); links.new(math('SUBTRACT', 1.0, v), wvec.inputs['Y'])
    weights = []
    for path in prep['images']['weights']:
        tex = node('ShaderNodeTexImage', 'weights'); tex.image = _image(path, 'Non-Color', cache)
        tex.interpolation = 'Linear'; tex.extension = 'EXTEND'
        links.new(wvec.outputs[0], tex.inputs['Vector'])
        sep = node('ShaderNodeSeparateColor'); links.new(tex.outputs['Color'], sep.inputs['Color'])
        weights += [sep.outputs['Red'], sep.outputs['Green'], sep.outputs['Blue'], tex.outputs['Alpha']]
    layers = prep['layers']
    repeat = prep['uv_repeat']; hbf = prep['height_blend_factor']
    # landscape_default DXBC sorts the 8 weights and blends only the 4
    # strongest: layer i survives when fewer than 4 layers outrank it
    # (greater weight, or equal weight at a lower index).
    raw_w = [weights[i] if i < len(weights) else 0.0 for i in range(layers)]
    if layers > 4:
        kept = []
        for i in range(layers):
            if isinstance(raw_w[i], float):
                kept.append(0.0); continue
            rank = None
            for j in range(layers):
                if j == i or isinstance(raw_w[j], float):
                    continue
                beat = math('GREATER_THAN', raw_w[j], raw_w[i]) if j > i else math('SUBTRACT', 1.0, math('LESS_THAN', raw_w[j], raw_w[i]))
                rank = beat if rank is None else math('ADD', rank, beat)
            kept.append(math('MULTIPLY', raw_w[i], math('LESS_THAN', rank, 3.5)) if rank is not None else raw_w[i])
        weights = kept
    samples = []
    for i in range(layers):
        k = repeat * prep['layer_uv_scales'][i]
        vec = node('ShaderNodeCombineXYZ', f'layer {i} UV')
        links.new(math('MULTIPLY', u, k), vec.inputs['X'])
        links.new(math('MULTIPLY', v, -k), vec.inputs['Y'])
        texs = {}
        for kind, cs in (('albedo', 'sRGB'), ('normal', 'Non-Color'), ('data', 'Non-Color')):
            tex = node('ShaderNodeTexImage', f'{kind} {i}'); tex.image = _image(prep['images'][kind][i], cs, cache)
            links.new(vec.outputs[0], tex.inputs['Vector']); texs[kind] = tex
        dsep = node('ShaderNodeSeparateColor'); links.new(texs['data'].outputs['Color'], dsep.inputs['Color'])
        w = weights[i] if i < len(weights) else 0.0
        p = math('MULTIPLY', w, dsep.outputs['Blue'])
        samples.append((p, texs['albedo'].outputs['Color'], texs['normal'].outputs['Color']))
    m = samples[0][0]
    for p, _, _ in samples[1:]:
        m = math('MAXIMUM', m, p)
    bs = []
    for p, _, _ in samples:
        t = math('MAXIMUM', math('MULTIPLY', math('SUBTRACT', m, p), hbf), 0.0)
        bs.append(math('SUBTRACT', m, math('MINIMUM', m, t)))
    total = bs[0]
    for b in bs[1:]:
        total = math('ADD', total, b)
    safe = math('MAXIMUM', total, 1e-6)
    albedo = None; normal = None
    for (p, alb, nrm), b in zip(samples, bs):
        wn = math('DIVIDE', b, safe)
        def scaled(color):
            mix = node('ShaderNodeVectorMath'); mix.operation = 'SCALE'
            links.new(color, mix.inputs[0]); links.new(wn, mix.inputs['Scale'])
            return mix.outputs[0]
        def add(acc, value):
            if acc is None: return value
            nd = node('ShaderNodeVectorMath'); nd.operation = 'ADD'
            links.new(acc, nd.inputs[0]); links.new(value, nd.inputs[1]); return nd.outputs[0]
        albedo = add(albedo, scaled(alb)); normal = add(normal, scaled(nrm))
    links.new(albedo, bsdf.inputs['Base Color'])
    # Blended NormalR: xy normal in RG (0..1), roughness in B.
    ns = node('ShaderNodeSeparateXYZ'); links.new(normal, ns.inputs[0])
    links.new(ns.outputs['Z'], bsdf.inputs['Roughness'])
    x = math('SUBTRACT', math('MULTIPLY', ns.outputs['X'], 2.0), 1.0)
    y = math('SUBTRACT', math('MULTIPLY', ns.outputs['Y'], 2.0), 1.0)
    z = math('SQRT', math('MAXIMUM', math('SUBTRACT', 1.0, math('ADD', math('MULTIPLY', x, x), math('MULTIPLY', y, y))), 0.0))
    col_n = node('ShaderNodeCombineXYZ')
    links.new(ns.outputs['X'], col_n.inputs['X']); links.new(ns.outputs['Y'], col_n.inputs['Y'])
    links.new(math('ADD', math('MULTIPLY', z, 0.5), 0.5), col_n.inputs['Z'])
    nmap = node('ShaderNodeNormalMap'); nmap.uv_map = 'UV0'
    links.new(col_n.outputs[0], nmap.inputs['Color']); links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])
    mat['lswtss_landscape_blend'] = 'height blend (DXBC verified), parallax not applied'
    return mat

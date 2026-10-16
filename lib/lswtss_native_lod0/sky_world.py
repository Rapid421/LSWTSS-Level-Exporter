"""Build a Blender World from the level's native skydome texture.

The game's skydome model (assets/envart/shared_assets/skies/skydomebase)
maps its hemispherical sky texture with an equidistant fisheye projection,
verified from the dome mesh's own vertex/UV pairs: the zenith is the image
centre, radius = 0.5 * (90 deg - elevation) / 90 deg and the image angle
equals the azimuth atan2(z, x) in model space (game y up). The World shader
evaluates the same projection for every view direction, after undoing the
sky entity's world rotation, so the visible sky and its image-based lighting
come from the original texture.
"""
import math
from pathlib import Path

import bpy


def _math(nt, op, a=None, b=None, loc=(0, 0)):
    n = nt.nodes.new('ShaderNodeMath')
    n.operation = op
    n.location = loc
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if isinstance(v, (int, float)):
            n.inputs[i].default_value = v
        else:
            nt.links.new(v, n.inputs[i])
    return n.outputs[0]


def build_sky_world(image_path, sky_object=None, strength=1.0, name='Native sky'):
    world = bpy.data.worlds.new(name)
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    coord = nt.nodes.new('ShaderNodeTexCoord')
    coord.location = (-1600, 0)
    mapping = nt.nodes.new('ShaderNodeMapping')
    mapping.vector_type = 'VECTOR'
    mapping.location = (-1400, 0)
    if sky_object is not None:
        rotation = sky_object.matrix_world.to_quaternion().inverted()
        mapping.inputs['Rotation'].default_value = rotation.to_euler('XYZ')
    nt.links.new(coord.outputs['Generated'], mapping.inputs['Vector'])
    norm = nt.nodes.new('ShaderNodeVectorMath')
    norm.operation = 'NORMALIZE'
    norm.location = (-1200, 0)
    nt.links.new(mapping.outputs['Vector'], norm.inputs[0])
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    sep.location = (-1000, 0)
    nt.links.new(norm.outputs['Vector'], sep.inputs['Vector'])
    x, y, z = sep.outputs['X'], sep.outputs['Y'], sep.outputs['Z']
    # Blender (x, y, z) = game (-x, -z, y). Elevation from z.
    elev = _math(nt, 'ARCSINE', _math(nt, 'MAXIMUM', z, 0.0, (-800, -200)), None, (-600, -200))
    radius = _math(nt, 'SUBTRACT', 0.4975, _math(nt, 'DIVIDE', elev, math.pi, (-400, -200)), (-200, -200))
    h = _math(nt, 'SQRT', _math(nt, 'ADD', _math(nt, 'MULTIPLY', x, x, (-800, 100)),
                                 _math(nt, 'MULTIPLY', y, y, (-800, 0)), (-600, 50)), None, (-400, 50))
    h = _math(nt, 'MAXIMUM', h, 1e-6, (-200, 50))
    # u = 0.5 + r * cos(az), cos(az) = game_x / h = -x / h
    u = _math(nt, 'ADD', 0.5, _math(nt, 'MULTIPLY', radius,
              _math(nt, 'DIVIDE', _math(nt, 'MULTIPLY', x, -1.0, (-200, 250)), h, (0, 250)), (200, 250)), (400, 250))
    # Image v = 1 - (0.5 + r * sin(az)), sin(az) = game_z / h = -y / h
    v = _math(nt, 'ADD', 0.5, _math(nt, 'MULTIPLY', radius, _math(nt, 'DIVIDE', y, h, (0, 0)), (200, 0)), (400, 0))
    comb = nt.nodes.new('ShaderNodeCombineXYZ')
    comb.location = (600, 100)
    nt.links.new(u, comb.inputs['X'])
    nt.links.new(v, comb.inputs['Y'])
    image = bpy.data.images.load(str(image_path), check_existing=True)
    image.name = 'Native sky: ' + Path(image_path).stem
    image.colorspace_settings.name = 'sRGB'
    image.pack()
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.image = image
    tex.extension = 'EXTEND'
    tex.interpolation = 'Cubic'
    tex.location = (800, 100)
    nt.links.new(comb.outputs['Vector'], tex.inputs['Vector'])
    bg = nt.nodes.new('ShaderNodeBackground')
    bg.location = (1100, 100)
    bg.inputs['Strength'].default_value = strength
    nt.links.new(tex.outputs['Color'], bg.inputs['Color'])
    out = nt.nodes.new('ShaderNodeOutputWorld')
    out.location = (1300, 100)
    nt.links.new(bg.outputs['Background'], out.inputs['Surface'])
    world['lswtss_sky_texture'] = str(image_path)
    world['lswtss_sky_projection'] = 'equidistant fisheye, zenith centre, verified from skydomebase UVs'
    return world

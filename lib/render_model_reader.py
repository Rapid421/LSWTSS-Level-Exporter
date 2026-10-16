"""Decode the ApiRenderModel scene component (static mesh placement).

Layout (validated on 92,149 components across 150 art scenes):
  u32 version, u8 1 + GUID16 + u32 len + model path, u32 len + node name,
  tail:
    f32 opacity, 17 render-flag bytes (flag[4] = Draw Mesh), f32, 3 x i32 -1,
    ... GUID16 at tail+57, two f32 at tail+73, 12 reserved bytes,
    u32 override count, then per override:
      string name     'g_<graph node>_<socket> @ Slot <material slot> : <material name>'
      string display  '<parameter display name>.Value @ Slot ...'
      u32 type        1 float, 2 float2, 3 float3, 4 float4, 5 texture
      3 bytes, 3 x float4 (instance value, then the material default twice),
      u32 2, u8 has-resource; if 1: GUID16 + string path (the texture)
    then the remainder, which may name a per-instance .instance_vertex_colours file.
"""
import re
import struct

OVERRIDE_NAME = re.compile(r'^g_(?P<node>.+)_(?P<socket>[^_]+) @ Slot (?P<slot>\d+) : (?P<material>.*)$')
IVC_RE = re.compile(rb'[\x20-\x7e]{4,300}?\.instance_vertex_colours\x00', re.I)


def _string(b, q):
    n = struct.unpack_from('<I', b, q)[0]
    if not 1 <= n <= 1024 or b[q + 3 + n] != 0:
        raise ValueError('bad string at %d' % q)
    return b[q + 4:q + 3 + n].decode('latin1'), q + 4 + n


def parse_tail(t):
    """Parse the bytes after the node name. Returns overrides and the vertex-colour path."""
    count = struct.unpack_from('<I', t, 93)[0]
    if count > 256:
        raise ValueError('implausible override count %d' % count)
    q = 97; overrides = []
    for _ in range(count):
        name, q = _string(t, q)
        display, q = _string(t, q)
        typ = struct.unpack_from('<I', t, q)[0]; q += 4 + 3
        value = struct.unpack_from('<4f', t, q); q += 48
        has_resource = t[q + 4]; q += 5
        texture = None
        if has_resource == 1:
            q += 16
            texture, q = _string(t, q)
        m = OVERRIDE_NAME.match(name)
        overrides.append({
            'name': name, 'display': display, 'type': typ,
            'node': m.group('node') if m else None, 'socket': m.group('socket') if m else None,
            'slot': int(m.group('slot')) if m else None, 'material': m.group('material') if m else None,
            'value': list(value[:max(1, min(4, typ))]) if typ != 5 else None,
            'texture': texture.replace('\\', '/') if texture else None})
    ivc = IVC_RE.search(t, q)
    return {'overrides': overrides,
            'instance_vertex_colours': ivc.group()[:-1].decode('latin1').replace('\\', '/') if ivc else None}

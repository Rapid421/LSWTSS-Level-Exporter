"""Read .instance_vertex_colours (CVNI chunk): per-placement vertex colour paint.

Layout (big-endian): 'CVNI', u32 version (207), u32 stream count, u32, u32,
u32, u32; then per stream: u32 1, u32 1, u32 slot type, u32 vertex count,
'DXTV', u32 buffer version, u32 attribute count, 3-byte descriptors
(type 2 = colour, format 9 = 4 x u8), 6 bytes, count x 4 colour bytes,
16 bytes.

Streams are ordered by LOD (LOD0 first); each replaces vertex attribute 2
(Color0) of the model mesh with the same vertex count, byte-for-byte in the
model's own channel order (verified on rock_mound: unpainted vertices equal
the model's colours). A stream whose count matches no mesh is stale and not
applied (aset_tree_assembly instance: 956 vs mesh 1034).
"""
import struct

# Vertex attribute format sizes (as in native_model_reader.FORMATS).
SIZES = {2: 8, 3: 12, 4: 16, 5: 4, 6: 8, 7: 4, 8: 4, 9: 4}


def read_streams(data):
    a = data.find(b'CVNI')
    if a < 0:
        raise ValueError('No CVNI chunk')
    version, count = struct.unpack_from('>2I', data, a + 4)
    out = []; q = a + 28
    for _ in range(count):
        k = data.find(b'DXTV', q)
        if k < 0:
            raise ValueError('Missing DXTV stream')
        nv = struct.unpack_from('>I', data, k - 4)[0]
        _, na = struct.unpack_from('>2I', data, k + 4)
        desc = [tuple(data[k + 12 + 3 * i:k + 15 + 3 * i]) for i in range(na)]
        # Usually colour only (2,9,0); some files carry the whole vertex layout
        # (position, normal, colour, tangent, UV ...) and only colour is used.
        try:
            stride = sum(SIZES[f] for _, f, _ in desc)
            colour = next(off for t, f, off in desc if t == 2 and f == 9)
        except (KeyError, StopIteration):
            raise ValueError('Unsupported instance colour layout %r' % desc)
        start = k + 12 + 3 * na + 6
        block = data[start:start + stride * nv]
        if len(block) != stride * nv:
            raise ValueError('Truncated instance colour stream')
        if stride == 4:
            raw = block
        else:
            raw = b''.join(block[i * stride + colour:i * stride + colour + 4] for i in range(nv))
        out.append({'vertices': nv, 'rgba': raw})
        q = start + stride * nv + 16
    return {'version': version, 'streams': out}

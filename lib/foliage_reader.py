"""Read TSS painted-foliage instance files (.foliage) and ApiRenderFoliage components.

An ApiRenderFoliage component names a ``.foliage`` instance file, the instanced
``.model`` and its node (e.g. Combined_ROOT). The .foliage file is a resource
header followed by a big-endian u32 instance count and that many 4x4
big-endian float matrices in row-vector layout (rows 0-2 are the scaled axes,
row 3 the translation), local to the foliage entity. The count is located by
requiring that it exactly accounts for the rest of the file.
"""
import re
import struct

FOLIAGE_RE = re.compile(rb'[\x20-\x7e]{4,240}?\.foliage\x00', re.I)
MODEL_RE = re.compile(rb'[\x20-\x7e]{4,240}?\.model\x00', re.I)


def read_component(raw):
    f = FOLIAGE_RE.search(raw)
    m = MODEL_RE.search(raw)
    if not f or not m:
        return None
    node = None
    tail = raw[m.end():]
    if len(tail) >= 4:
        n = struct.unpack_from('<I', tail, 0)[0]
        if 1 <= n <= 256 and len(tail) >= 4 + n and tail[3 + n] == 0:
            node = tail[4:3 + n].decode('latin1')
    return {'foliage': f.group()[:-1].decode('latin1').replace('\\', '/'),
            'model': m.group()[:-1].decode('latin1').replace('\\', '/'), 'node': node}


def read_instances(data):
    """Return a list of 16-float row-major (row-vector) matrices."""
    size = len(data)
    for off in range(0x40, min(size - 4, 0x2000)):
        count = struct.unpack_from('>I', data, off)[0]
        # The count is followed by a reserved u32 (observed 0) before the matrices.
        if count and off + 8 + count * 64 == size and data[off + 4:off + 8] == bytes(4):
            mats = []
            for k in range(count):
                v = struct.unpack_from('>16f', data, off + 8 + k * 64)
                if abs(v[15] - 1.0) > 1e-3 or any(abs(v[i]) > 1e-3 for i in (3, 7, 11)):
                    raise ValueError(f'Instance {k} is not an affine row-vector matrix')
                mats.append(list(v))
            return mats
    raise ValueError('Foliage instance count not found')

"""Read TSS Landscape (terrain) components and resources.

A level's ground is a heightfield, not a placed model. A ``Landscape`` scene
component references ``<dir>/<GUID>/landscape.landscape_data`` and carries
FillFirstBlendLayer (u8), Map World Size (f32), Map Height Scale (f32) and
Chunk Size (u32) after the path's 4-byte handle trailer. The GUID folder holds:

* ``heightmap_dx11.texture``  R16 heightfield (full mip chain, top mip used)
* ``holes_dx11.texture``      BC4 hole mask; the shader discards where > 0.5
* ``weights0/1_dx11.texture`` blend weights, layers 1-4 / 5-8 in R,G,B,A
* ``albedo/normal/data_dx11.texture`` 2D texture arrays, one slice per layer

The Landscape Entity's position is the terrain's minimum-XZ corner and world
height = y + texel/65535 * Map Height Scale (verified on Mos Eisley).
Layer blend (kashyyyk_landscape DXBC): for the strongest layers, p = w*h with
h = Data.B, m = max(p), b = m - min(m, max(0, (m - p) * HeightBlendFactor)),
weights = b / sum(b); layer UV = terrain UV * UV Repeat * layer scale.
"""
import re
import struct
from pathlib import Path

LANDDATA_RE = re.compile(rb'[\x20-\x7e]{8,240}?/([0-9A-Fa-f]{32})/[Ll]andscape\.landscape_data\x00')
MATERIAL_RE = re.compile(rb'[\x20-\x7e]{6,240}?\.material\x00')
LAYER_PARAM_RE = re.compile(rb'\x05\x00\x00\x00Data\x00(.{16})', re.S)


def read_component(raw):
    """Parse a Landscape component payload; return None when not a landscape."""
    m = LANDDATA_RE.search(raw)
    if not m:
        return None
    path = m.group()[:-1].decode('ascii').replace('\\', '/')
    fill, world_size, height_scale, chunk = struct.unpack_from('<BffI', raw, m.end() + 4)
    if not (0.5 < world_size < 100000 and 0 < height_scale < 100000 and chunk in (16, 32, 64, 128, 256, 512)):
        raise ValueError(f'Implausible landscape scalars {world_size}/{height_scale}/{chunk}')
    material = MATERIAL_RE.search(raw[:m.start()])
    # Per-layer records start at the first BlendDeltas reference; each closes
    # with its Data texture role and four floats (x = layer UV scale).
    first_layer = raw.find(b'BlendDeltas_', m.end())
    layers = [list(struct.unpack('<4f', p.group(1)))
              for p in LAYER_PARAM_RE.finditer(raw, first_layer if first_layer >= 0 else len(raw))]
    return {'landscape_data': path, 'folder': path.rsplit('/', 1)[0], 'guid': m.group(1).decode().upper(),
            'fill_first_blend_layer': bool(fill), 'world_size': world_size, 'height_scale': height_scale,
            'chunk_size': chunk, 'material': material.group()[:-1].decode('ascii').replace('\\', '/') if material else None,
            'layer_params': layers}


def material_parameters(material_bytes):
    """UV Repeat and Height Blend Factor from a landscape material graph."""
    text = material_bytes.decode('latin1')
    out = {'uv_repeat': 64.0, 'height_blend_factor': 16.0, 'source': 'default'}
    for name, key in (('UV Repeat', 'uv_repeat'), ('Height Blend Factor', 'height_blend_factor')):
        m = re.search(r'displayName="' + re.escape(name) + r'"[^>]*>\s*<userParam name="A" value="([-0-9.eE]+)"', text)
        if m:
            out[key] = float(m.group(1)); out['source'] = 'material'
    return out


_RTXT = struct.Struct('>8I')


def _rtxt(data):
    i = data.find(b'RTXT')
    if i < 0:
        raise ValueError('No RTXT chunk')
    return _RTXT.unpack_from(data, i + 4), i


def read_heightmap(path):
    """Return (n, list of float 0..1) for the top mip of an R16 heightmap."""
    data = Path(path).read_bytes()
    (ver, w, h, depth, mips, arr, fmt, _), _ = _rtxt(data)
    # 36 = R16 UNORM (0..1); 34 = R16 FLOAT, used as-is (1_4battleofnabooa:
    # values -0.005..0.048, negatives only make sense as half floats).
    if fmt not in (34, 36) or w != h:
        raise ValueError(f'Expected square R16 heightmap, got {w}x{h} format {fmt}')
    payload = sum(max(1, w >> k) * max(1, h >> k) * 2 for k in range(mips))
    off = len(data) - payload
    if fmt == 34:
        return w, list(struct.unpack_from('<%de' % (w * h), data, off))
    vals = struct.unpack_from('<%dH' % (w * h), data, off)
    return w, [v / 65535.0 for v in vals]


def read_holes(path):
    """Return (n, bytearray) of the BC4 hole mask (255 = hole) or (0, None)."""
    data = Path(path).read_bytes()
    (ver, w, h, depth, mips, arr, fmt, _), _ = _rtxt(data)
    if fmt not in (1, 37) and fmt != 1:
        return 0, None
    payload = sum(max(1, (max(1, w >> k) + 3) // 4) * max(1, (max(1, h >> k) + 3) // 4) * 8 for k in range(mips))
    off = len(data) - payload
    out = bytearray(w * h)
    bw = w // 4
    for by in range(h // 4):
        for bx in range(bw):
            o = off + (by * bw + bx) * 8
            r0, r1 = data[o], data[o + 1]
            bits = int.from_bytes(data[o + 2:o + 8], 'little')
            if r0 > r1:
                pal = [r0, r1] + [((7 - i) * r0 + i * r1) // 7 for i in range(1, 7)]
            else:
                pal = [r0, r1] + [((5 - i) * r0 + i * r1) // 5 for i in range(1, 5)] + [0, 255]
            for k in range(16):
                out[(by * 4 + k // 4) * w + bx * 4 + k % 4] = pal[(bits >> (3 * k)) & 7]
    return (w, out) if any(v > 127 for v in out) else (0, None)


def prepare_landscape(comp, folder, material_bytes, out_dir, convert, max_res=1024):
    """Write a landscape's heights/holes as raw files and its textures as PNGs.

    Returns a JSON-able dict consumed by the Blender terrain builder.
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    files = {f.name.lower(): f for f in Path(folder).iterdir()}
    n_src, heights = read_heightmap(files['heightmap_dx11.texture'])
    n = min(n_src, max_res)
    step = (n_src - 1) / (n - 1)
    picks = [int(round(i * step)) for i in range(n)]
    grid = [heights[j * n_src + i] for j in picks for i in picks]
    (out_dir / 'heights.f32').write_bytes(struct.pack('<%df' % len(grid), *grid))
    holes_path = None
    if 'holes_dx11.texture' in files:
        hn, holes = read_holes(files['holes_dx11.texture'])
        if holes is not None:
            # Sample the mask at each quad centre at the output resolution.
            hp = []
            for j in range(n - 1):
                v = min(hn - 1, int((j + 0.5) * step * hn / n_src))
                for i in range(n - 1):
                    u = min(hn - 1, int((i + 0.5) * step * hn / n_src))
                    hp.append(1 if holes[v * hn + u] > 127 else 0)
            holes_path = out_dir / 'holes.u8'
            holes_path.write_bytes(bytes(hp))
    layers = texture_array_size(files['albedo_dx11.texture'])
    images = {'albedo': [], 'normal': [], 'data': [], 'weights': []}
    for kind in ('albedo', 'normal', 'data'):
        for i in range(layers):
            dest = out_dir / f'{kind}_{i}.png'
            if not dest.exists():
                convert(files[f'{kind}_dx11.texture'], dest, i)
            images[kind].append(str(dest))
    for w in (0, 1):
        src = files.get(f'weights{w}_dx11.texture')
        if src:
            dest = out_dir / f'weights{w}.png'
            if not dest.exists():
                convert(src, dest)
            images['weights'].append(str(dest))
    params = material_parameters(material_bytes) if material_bytes else {
        'uv_repeat': 64.0, 'height_blend_factor': 16.0, 'source': 'default'}
    scales = [p[0] if p and 0 < p[0] < 1000 else 1.0 for p in comp['layer_params'][:layers]]
    scales += [1.0] * (layers - len(scales))
    return {'resolution': n, 'source_resolution': n_src, 'heights': str(out_dir / 'heights.f32'),
            'holes': str(holes_path) if holes_path else None, 'layers': layers,
            'layer_uv_scales': scales, 'images': images, **params}


def texture_array_size(path):
    data = Path(path).read_bytes()
    (ver, w, h, depth, mips, arr, fmt, _), _ = _rtxt(data)
    return depth if arr > 1 and depth > 1 else max(1, arr)

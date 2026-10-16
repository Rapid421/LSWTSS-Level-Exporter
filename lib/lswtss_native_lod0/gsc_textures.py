"""Reader for a GSC's companion ``.nxg_textures`` (TSXT) texture set.

The TSXT chunk lists texture entries in order. An entry is either embedded
(a texture GUID, 00 01 00 00, a u8-length NUL-terminated authoring name and a
9-byte trailer: u32 0, u8 mip count, 00 01 00 02) or external (a zero GUID,
a zero byte, a u8-length NUL-terminated .TEX path, the 11 slot sampler bytes
and kind 4). The DDS payloads of embedded entries follow the entry list in
entry order. Materials reference embedded textures by the same GUID.
"""
import struct

try:
    from .native_model_reader import FormatError
except ImportError:  # Standalone use by prepare_level.py.
    from native_model_reader import FormatError

SAMPLER = bytes.fromhex('00010000000000ff000100')
DX10 = int.from_bytes(b'DX10', 'little')
BLOCK_BYTES = {b'DXT1': 8, b'ATI1': 8, b'BC4U': 8, b'BC4S': 8}


def _dds_size(data, pos):
    """Return the byte length of the DDS file starting at ``pos``."""
    if data[pos:pos + 4] != b'DDS ' or pos + 128 > len(data):
        raise FormatError(f'Missing DDS header at {pos:#x}')
    (size, flags, height, width, pitch, depth, mips) = struct.unpack_from('<7I', data, pos + 4)
    pf_flags, fourcc_int = struct.unpack_from('<2I', data, pos + 80)
    fourcc = data[pos + 84:pos + 88]
    header = 128
    mips = max(1, mips)
    if pf_flags & 0x4:
        if fourcc_int == DX10:
            header += 20
            dxgi = struct.unpack_from('<I', data, pos + 128)[0]
            block = 8 if dxgi in (70, 71, 72, 79, 80, 81) else 16
        else:
            block = BLOCK_BYTES.get(fourcc, 16)
        total = sum(max(1, (max(1, width >> i) + 3) // 4) * max(1, (max(1, height >> i) + 3) // 4) * block
                    for i in range(mips))
    else:
        bits = struct.unpack_from('<I', data, pos + 88)[0]
        total = sum(max(1, width >> i) * max(1, height >> i) * bits // 8 for i in range(mips))
    if pos + header + total > len(data):
        raise FormatError(f'Truncated DDS payload at {pos:#x}')
    return header + total


def read_texture_set(data):
    """Parse a .nxg_textures file into ordered entries with DDS payloads."""
    first = data.find(b'TSXT')
    tag = data.find(b'TSXT', first + 4) if first >= 0 else -1
    if tag < 0:
        raise FormatError('No TSXT chunk')
    revision = struct.unpack_from('>I', data, tag + 4)[0]
    if revision != 14:
        raise FormatError(f'Unsupported TSXT revision {revision}')
    size = struct.unpack_from('>H', data, tag + 10)[0]
    pos = tag + 12 + size
    if data[pos - 1] != 0:
        raise FormatError('Invalid TSXT conversion string')
    reserved, count = struct.unpack_from('>2I', data, pos)
    pos += 8
    if count > 4096:
        raise FormatError(f'Implausible TSXT entry count {count}')
    entries = []
    for index in range(count):
        guid = data[pos:pos + 16]
        if guid == bytes(16):
            if data[pos + 16] != 0:
                raise FormatError(f'Unexpected external TSXT entry flag at {pos + 16:#x}')
            n = data[pos + 17]
            end = pos + 18 + n
            if n < 2 or data[end - 1] != 0 or data[end:end + 11] != SAMPLER or data[end + 11] != 4:
                raise FormatError(f'Invalid external TSXT entry at {pos:#x}')
            path = data[pos + 18:end - 1].decode('latin1').replace('\\', '/').lstrip('/').lower()
            entries.append({'index': index, 'kind': 'external', 'external_path': path})
            pos = end + 12
        else:
            if data[pos + 16:pos + 20] != b'\x00\x01\x00\x00':
                raise FormatError(f'Invalid embedded TSXT entry at {pos:#x}')
            n = data[pos + 20]
            end = pos + 21 + n
            trailer = data[end:end + 9]
            if n < 2 or data[end - 1] != 0 or trailer[:4] != bytes(4) or trailer[5:] != b'\x00\x01\x00\x02':
                raise FormatError(f'Invalid embedded TSXT entry name at {pos:#x}')
            entries.append({'index': index, 'kind': 'embedded', 'guid': guid.hex(),
                            'name': data[pos + 21:end - 1].decode('latin1').replace('\\', '/'),
                            'mip_count': trailer[4]})
            pos = end + 9
    for entry in entries:
        if entry['kind'] != 'embedded':
            continue
        if data[pos:pos + 4] != b'DDS ':
            raise FormatError(f'Embedded texture {entry["name"]} has no DDS payload at {pos:#x}')
        length = _dds_size(data, pos)
        entry['dds_offset'] = pos
        entry['dds_size'] = length
        pos += length
    return {'revision': revision, 'count': count, 'entries': entries, 'end_offset': pos,
            'trailing_bytes': len(data) - pos}


def dds_bytes(data, entry):
    return data[entry['dds_offset']:entry['dds_offset'] + entry['dds_size']]

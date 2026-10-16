"""Strict reader for the static-mesh portions of TT Games GSC resources.

GSC wrappers have several game-specific tables, while their SMNR/DXTV vertex
and index streams use the same observed payload encoding as the game's .model
resources. This reader deliberately keeps the wrapper tables separate.
"""
import math
import re
import struct
try:
    from .native_model_reader import FORMATS, FormatError
except ImportError:  # Also usable by the standalone level-preparation script.
    from native_model_reader import FORMATS, FormatError


def _read_dxtv_stream(data, start, vertex_count, end):
    """Read one length-framed DXTV vertex stream used by shared-pool GSCs."""
    if start < 0 or start + 12 > end or data[start:start + 4] != b'DXTV':
        return None
    revision, attribute_count = struct.unpack_from('>2I', data, start + 4)
    if revision != 169 or not 1 <= attribute_count <= 32:
        return None
    descriptor_end = start + 12 + attribute_count * 3
    if descriptor_end + 6 > end:
        return None
    descriptors = [tuple(data[start + 12 + i * 3:start + 15 + i * 3])
                   for i in range(attribute_count)]
    try:
        stride = sum(FORMATS[fmt][1] for _, fmt, _ in descriptors)
    except KeyError:
        return None
    if any(offset + FORMATS[fmt][1] > stride
           for _, fmt, offset in descriptors):
        return None
    raw_start = descriptor_end + 6
    raw_end = raw_start + stride * vertex_count
    stream_end = raw_end + 16
    if vertex_count <= 0 or vertex_count > 16_000_000 or stream_end > end:
        return None
    return {'offset': start, 'revision': revision,
            'vertex_count': vertex_count, 'attributes': descriptors,
            'stride': stride, 'raw_offset': raw_start,
            'end_offset': stream_end}


REF_FLAG = 0xC0000000
SLOT_INLINE_VERTEX = 0x502
SLOT_INLINE_INDEX = 0x102
FIRST_RECORD_OBJECT_ID = 2
RECORD_OBJECT_IDS = 2


def _decode_vertex_slot(data, slot, start=0, count=None):
    """Decode vertices ``start .. start+count`` of a slot (default: all)."""
    stream = slot['stream']
    attrs = {}
    raw_offset, stride = stream['raw_offset'], stream['stride']
    if count is None:
        count = slot['vertex_count'] - start
    for typ, fmt, offset in stream['attributes']:
        code, size = FORMATS[fmt]
        values = [struct.unpack_from(code, data, raw_offset + i * stride + offset)
                  for i in range(start, start + count)]
        if fmt == 8:
            values = [tuple(x / 255 for x in v) for v in values]
        attrs[typ] = values
    return attrs


def _parse_hsem_records(data, mesh_tag, section_end, declared_records):
    """Parse every HSEM SMNR record with the object/reference grammar.

    Each record serializes ``nbuf`` vertex slots and one index slot. A slot is
    either inline (marker 1, type, payload) or a back-reference
    (0xC0000000 | object id). Object ids are assigned in serialization order,
    starting at 2: each record takes two ids, each inline vertex slot and
    inline index buffer one; references take none. References are validated
    against the resolved object type and ranges; nothing is guessed.
    Every record then carries one draw range:
    index start/count, vertex base, a reserved word, u16 vertex count,
    16 reserved bytes, bounds centre/extents, -1.0, a reserved word and a
    flag that is 1 when another record follows.
    """
    objects = {}
    records = []
    next_id = FIRST_RECORD_OBJECT_ID
    pos = mesh_tag + 16
    for index in range(declared_records):
        if pos + 12 > section_end or data[pos:pos + 4] != b'SMNR':
            raise FormatError(f'GSC HSEM record {index} missing at {pos:#x}')
        revision, nbuf = struct.unpack_from('>2I', data, pos + 4)
        if revision not in (200, 201) or not 1 <= nbuf <= 16:
            raise FormatError(f'Unsupported GSC SMNR record {index} at {pos:#x}: {revision}/{nbuf}')
        record = {'index': index, 'offset': pos, 'object_id': next_id, 'slots': []}
        next_id += RECORD_OBJECT_IDS
        cursor = pos + 12
        for slot_index in range(nbuf):
            marker = struct.unpack_from('>I', data, cursor)[0]
            slot = {'offset': cursor}
            if marker == 1:
                slot['object_id'] = next_id
                next_id += 1
                kind, count = struct.unpack_from('>2I', data, cursor + 4)
                if kind != SLOT_INLINE_VERTEX:
                    raise FormatError(f'Unsupported GSC vertex slot type {kind:#x} at {cursor:#x}')
                stream = _read_dxtv_stream(data, cursor + 12, count, section_end)
                if stream is None:
                    raise FormatError(f'Invalid GSC DXTV vertex slot at {cursor + 12:#x}')
                raw_end = stream['raw_offset'] + stream['stride'] * count
                slot.update(kind='vertex', vertex_count=count, stream=stream)
                cursor = raw_end
            elif marker & REF_FLAG == REF_FLAG:
                if struct.unpack_from('>I', data, cursor + 4)[0] != 1:
                    raise FormatError(f'Unexpected GSC vertex reference word at {cursor + 4:#x}')
                target = marker & ~REF_FLAG
                if target not in objects:
                    raise FormatError(f'GSC vertex slot references unknown object {target}')
                resolved = objects[target]
                if resolved.get('kind') != 'vertex':
                    raise FormatError(f'GSC vertex slot reference {target} is not a vertex slot')
                slot.update(kind='vertex_ref', target=target,
                            vertex_count=resolved['vertex_count'], stream=resolved['stream'],
                            source_object_id=resolved['object_id'])
                cursor += 8
            else:
                raise FormatError(f'Unknown GSC vertex slot marker {marker:#x} at {cursor:#x}')
            if struct.unpack_from('>I', data, cursor)[0] != 0:
                raise FormatError(f'GSC vertex slot terminator missing at {cursor:#x}')
            cursor += 4
            if slot['kind'] == 'vertex':
                objects[slot['object_id']] = slot
            record['slots'].append(slot)
        # A u32 count of extra inline position streams follows the slots.
        # They hold position-only frames (vertex animation, or the only
        # positions when the base slot carries normals/UVs/colours). The list
        # itself takes one object id when present.
        extra_count = struct.unpack_from('>I', data, cursor)[0]
        cursor += 4
        if extra_count > 100_000:
            raise FormatError(f'Implausible GSC extra stream count {extra_count} at {cursor - 4:#x}')
        record['position_frames'] = []
        if extra_count:
            next_id += 1
        for frame in range(extra_count):
            marker, kind, count = struct.unpack_from('>3I', data, cursor)
            if marker != 1 or kind != SLOT_INLINE_VERTEX:
                raise FormatError(f'Unsupported GSC extra stream at {cursor:#x}')
            stream = _read_dxtv_stream(data, cursor + 12, count, section_end)
            if stream is None:
                raise FormatError(f'Invalid GSC extra DXTV stream at {cursor + 12:#x}')
            slot = {'offset': cursor, 'object_id': next_id, 'kind': 'vertex',
                    'vertex_count': count, 'stream': stream, 'frame': frame}
            next_id += 1
            cursor = stream['raw_offset'] + stream['stride'] * count
            if struct.unpack_from('>I', data, cursor)[0] != 0:
                raise FormatError(f'GSC extra stream terminator missing at {cursor:#x}')
            cursor += 4
            objects[slot['object_id']] = slot
            record['position_frames'].append(slot)
        marker = struct.unpack_from('>I', data, cursor)[0]
        if marker == 1:
            kind, count, width = struct.unpack_from('>3I', data, cursor + 4)
            if kind != SLOT_INLINE_INDEX or width not in (2, 4) or count > 64_000_000:
                raise FormatError(f'Unsupported GSC inline index buffer at {cursor:#x}')
            data_offset = cursor + 16
            if data_offset + count * width > section_end:
                raise FormatError(f'Truncated GSC index buffer at {cursor:#x}')
            buffer = {'kind': 'index', 'object_id': next_id, 'offset': cursor,
                      'count': count, 'width': width, 'data_offset': data_offset}
            next_id += 1
            objects[buffer['object_id']] = buffer
            cursor = data_offset + count * width
        elif marker & REF_FLAG == REF_FLAG:
            if struct.unpack_from('>I', data, cursor + 4)[0] != 1:
                raise FormatError(f'Unexpected GSC index reference word at {cursor + 4:#x}')
            target = marker & ~REF_FLAG
            buffer = objects.get(target)
            if not buffer or buffer.get('kind') != 'index':
                raise FormatError(f'GSC index reference {target} is not an index buffer')
            cursor += 8
        else:
            raise FormatError(f'Unknown GSC index slot marker {marker:#x} at {cursor:#x}')
        record['index_buffer_object_id'] = buffer['object_id']
        istart, icount, vbase, reserved = struct.unpack_from('>4I', data, cursor)
        vcount = struct.unpack_from('>H', data, cursor + 16)[0]
        bounds = struct.unpack_from('>8f', data, cursor + 34)
        tail_float, tail_word, more = struct.unpack_from('>f2I', data, cursor + 66)
        record_end = cursor + 78
        if more not in (0, 1) or (more == 1) != (index + 1 < declared_records):
            raise FormatError(f'GSC record continuation flag mismatch at {cursor + 74:#x}')
        record.update(index_start=istart, index_count=icount, vertex_base=vbase,
                      vertex_count=vcount, reserved_word=reserved,
                      reserved_bytes=data[cursor + 18:cursor + 34].hex(),
                      bounds_center=list(bounds[:3]), bounds_extent=list(bounds[4:7]),
                      bounds_center_w=bounds[3], bounds_extent_w=bounds[7],
                      tail_float=tail_float, tail_word=tail_word,
                      draw_offset=cursor, end_offset=record_end)
        records.append(record)
        pos = record_end
    return records, objects, pos


def _build_record_draw(data, record, objects):
    """Resolve one record's vertex range and index range into a mesh."""
    index_buffer = objects[record['index_buffer_object_id']]
    start, count = record['index_start'], record['index_count']
    if count == 0:
        return None, 'empty draw range'
    if count % 3 or start + count > index_buffer['count']:
        return None, f'index range {start}+{count} exceeds buffer {index_buffer["count"]}'
    fmt = '<' + ('H' if index_buffer['width'] == 2 else 'I') * count
    raw = struct.unpack_from(fmt, data, index_buffer['data_offset'] + start * index_buffer['width'])
    base, vcount = record['vertex_base'], record['vertex_count']
    slot_counts = {slot['vertex_count'] for slot in record['slots']}
    if len(slot_counts) != 1:
        return None, f'vertex slots disagree on vertex count {sorted(slot_counts)}'
    total = slot_counts.pop()
    if base + vcount > total:
        return None, f'vertex range {base}+{vcount} exceeds buffer {total}'
    # Indices are relative to the record's vertex base.
    if not all(i < vcount for i in raw):
        return None, 'index exceeds draw vertex count'
    attrs = {}
    for slot in record['slots']:
        for typ, values in _decode_vertex_slot(data, slot, base, vcount).items():
            if typ in attrs:
                return None, f'duplicate vertex attribute {typ}'
            attrs[typ] = values
    frames = record.get('position_frames', [])
    if 0 not in attrs and frames:
        # Static export uses frame 0 of the record's position frames.
        if frames[0]['vertex_count'] != total:
            return None, 'position frame vertex count differs from base slot'
        frame_attrs = _decode_vertex_slot(data, frames[0], base, vcount)
        if set(frame_attrs) != {0}:
            return None, f'unexpected position-frame attributes {sorted(frame_attrs)}'
        attrs[0] = [tuple(v[:3]) for v in frame_attrs[0]]
    if 0 not in attrs:
        return None, 'draw has no position stream'
    attrs[0] = [tuple(v[:3]) for v in attrs[0]]
    if any(not math.isfinite(x) for v in attrs[0] for x in v):
        return None, 'non-finite position'
    return {'record': record['index'], 'offset': record['offset'],
            'vertex_count': vcount, 'attributes': attrs, 'indices': list(raw),
            'index_count': count, 'index_width': index_buffer['width'],
            'vertex_base': base, 'index_start': start,
            'bounds_center': record['bounds_center'],
            'bounds_extent': record['bounds_extent']}, None


def _read_display_groups(data, psid, declared_records):
    """Read the PSID display-group array that binds HSEM records to materials.

    The array has ``first_array_count`` groups. Each group is a u16 entry
    count followed by 16-byte entries: u16 material index, u16 sort key,
    u16 reserved, u16 sort key (repeated), u16 HSEM record index, u32 render
    state and two u8 flags, each 0 or 1 (meaning not yet established).
    Records may be referenced by several groups; unreferenced records are
    reported by the caller.
    """
    count = struct.unpack_from('>I', data, psid + 12)[0]
    pos = psid + 16
    groups = []
    seen = set()
    for group_index in range(count):
        n = struct.unpack_from('>H', data, pos)[0]
        pos += 2
        if n > declared_records:
            raise FormatError(f'PSID display group {group_index} has {n} entries')
        entries = []
        for _ in range(n):
            material, sort_key, reserved, sort_key2, record, state, marker = struct.unpack_from(
                '>5HIH', data, pos)
            if marker & 0xFEFE or sort_key != sort_key2 or record >= declared_records:
                raise FormatError(f'Invalid PSID display entry at {pos:#x}')
            # A record may be drawn by more than one display group (reuse).
            seen.add(record)
            entries.append({'material': material, 'record': record, 'sort_key': sort_key,
                            'reserved': reserved, 'render_state': state,
                            'entry_flag': marker, 'offset': pos})
            pos += 16
        groups.append({'index': group_index, 'entries': entries})
    return {'offset': psid + 16, 'end_offset': pos, 'groups': groups,
            'records_referenced': len(seen)}


def _read_names(data):
    p=data.find(b'LBTN')
    if p<0 or p+12>len(data):return []
    version,size=struct.unpack_from('>2I',data,p+4)
    if size>1_000_000 or p+12+size>len(data):return []
    raw=data[p+12:p+12+size]
    return [x.decode('latin1') for x in raw.split(b'\0') if x]


def _read_psid_display_records(data, start, names):
    """Read verified PSID header fields and LBTN-named display records.

    TSS PSID revision 35 omits the legacy ROTV array tags. The parser exposes
    the observed path/count prefix and records whose length-prefixed names
    exactly match LBTN entries. In the validated corpus, the first record's
    u16 prefix equals the number of named records and later prefixes are zero.
    Each named record is followed by an affine matrix, bounds/sphere, and a
    variable tail. The tail's second u32 is retained as a type code; other
    tail words remain uninterpreted until their semantics are established.
    """
    if start < 0 or start + 12 > len(data):
        return None
    revision, path_size = struct.unpack_from('>2I', data, start + 4)
    if revision != 35:
        return {'offset': start, 'revision': revision,
                'parse_error': f'Unsupported PSID display revision {revision}'}
    section_end = data.find(b'BNAT', start + 8)
    if section_end < 0:
        section_end = len(data)
    if path_size > 1_000_000 or start + 12 + path_size + 4 > section_end:
        return {'offset': start, 'revision': revision, 'parse_error': 'Invalid PSID path length'}
    path_start = start + 12
    raw_path = data[path_start:path_start + path_size]
    path = raw_path.rstrip(b'\0').decode('latin1')
    count_offset = path_start + path_size
    first_array_count = struct.unpack_from('>I', data, count_offset)[0]
    if first_array_count > 10_000_000:
        return {'offset': start, 'revision': revision, 'file_path': path,
                'file_path_size': path_size, 'parse_error': 'Implausible first PSID array count'}
    if count_offset + 4 > section_end:
        return {'offset': start, 'revision': revision, 'file_path': path,
                'file_path_size': path_size, 'parse_error': 'Truncated PSID array header'}
    records = []
    seen = set()
    for name_index, name in enumerate(names):
        raw_name = name.encode('latin1', errors='ignore')
        if not raw_name:
            continue
        needle = raw_name + b'\0'
        scan = count_offset + 4
        while True:
            hit = data.find(needle, scan, section_end)
            if hit < 0:
                break
            scan = hit + 1
            if hit < count_offset + 6:
                continue
            string_size = struct.unpack_from('>H', data, hit - 2)[0]
            if string_size != len(needle):
                continue
            key = (hit, name_index)
            if key in seen:
                continue
            seen.add(key)
            record = {'name': name, 'name_table_index': name_index,
                      'record_ordinal': 0,
                      'record_prefix_u16': struct.unpack_from('>H', data, hit - 4)[0],
                      'offset': hit, 'name_size': string_size}
            matrix_offset = hit + string_size
            if matrix_offset + 64 <= section_end:
                matrix = struct.unpack_from('>16f', data, matrix_offset)
                valid = all(math.isfinite(x) and abs(x) < 1e8 for x in matrix)
                # The affine last row is [0, 0, 0, 1] for column-major data.
                valid = valid and all(abs(matrix[i] - expected) < 0.01
                                      for i, expected in ((3, 0.0), (7, 0.0),
                                                          (11, 0.0), (15, 1.0)))
                if valid:
                    record['transform_4x4_column_major'] = [list(matrix[i:i + 4])
                                                             for i in range(0, 16, 4)]
                    record['translation'] = list(matrix[12:15])
                    record['transform_offset'] = matrix_offset
                    bounds_offset = matrix_offset + 64
                    if bounds_offset + 48 <= section_end:
                        bounds = struct.unpack_from('>12f', data, bounds_offset)
                        minimum, maximum, sphere = bounds[:4], bounds[4:8], bounds[8:12]
                        bounds_valid = all(math.isfinite(x) and abs(x) < 1e8 for x in bounds)
                        bounds_valid = bounds_valid and all(minimum[i] <= maximum[i] for i in range(3))
                        bounds_valid = bounds_valid and minimum[3] == 1.0 and maximum[3] == 1.0
                        bounds_valid = bounds_valid and sphere[3] >= 0.0
                        if bounds_valid:
                            record['bounds_min'] = list(minimum[:3])
                            record['bounds_max'] = list(maximum[:3])
                            record['bounding_sphere'] = list(sphere)
                            record['bounds_offset'] = bounds_offset
            records.append(record)
    records.sort(key=lambda record: record['offset'])
    for record_index, record in enumerate(records):
        record['record_ordinal'] = record_index
    for record_index, record in enumerate(records):
        if 'bounds_offset' not in record:
            continue
        tail_start = record['bounds_offset'] + 48
        # The next named record provides an exact bound for all but the final
        # record. The final record is followed by other PSID arrays, so its
        # tail is intentionally not absorbed into this record.
        if record_index + 1 < len(records):
            next_record_start = records[record_index + 1]['offset'] - 4
            if next_record_start >= tail_start:
                record['record_tail_offset'] = tail_start
                record['record_tail'] = data[tail_start:next_record_start].hex()
                record['record_tail_size'] = next_record_start - tail_start
                record['record_end_offset'] = next_record_start
                raw_tail = data[tail_start:next_record_start]
                word_count = len(raw_tail) // 4
                if word_count:
                    record['record_tail_words_u32_be'] = list(
                        struct.unpack_from('>' + 'I' * word_count, raw_tail))
                if len(raw_tail) >= 8:
                    record['record_type_code'] = struct.unpack_from('>I', raw_tail, 4)[0]
                if len(raw_tail) % 4:
                    record['record_tail_remainder'] = raw_tail[word_count * 4:].hex()
    # Structured tail: u32 first display group (0xffffffff = locator with no
    # geometry), u32 type code, u32 reserved, u32 LOD count n, n f32 LOD
    # switch distances, u32 ordinal, u32 link index, u16 reserved.
    # A node with n LODs owns display groups first..first+max(n,1)-1; the
    # zero-distance LOD is the full-detail one.
    for record_index, record in enumerate(records):
        if 'bounds_offset' not in record:
            continue
        tail_start = record['bounds_offset'] + 48
        if tail_start + 16 > section_end:
            continue
        first_group, type_code, reserved, lod_count = struct.unpack_from('>4I', data, tail_start)
        if lod_count > 16:
            record['tail_parse_error'] = f'Implausible LOD count {lod_count}'
            continue
        size = 26 + 4 * lod_count
        if tail_start + size > section_end:
            record['tail_parse_error'] = 'Truncated named-group tail'
            continue
        if 'record_tail_size' in record and record['record_tail_size'] != size:
            record['tail_parse_error'] = f'Tail size {record["record_tail_size"]} != {size}'
            continue
        distances = list(struct.unpack_from(f'>{lod_count}f', data, tail_start + 16))
        ordinal, link = struct.unpack_from('>2I', data, tail_start + 16 + 4 * lod_count)
        groups = [] if first_group == 0xFFFFFFFF else list(range(first_group, first_group + max(lod_count, 1)))
        lod0 = None
        if groups:
            lod0 = groups[distances.index(min(distances))] if distances else groups[0]
        record.update(display_group_first=None if first_group == 0xFFFFFFFF else first_group,
                      display_groups=groups, lod_distances=distances, lod0_display_group=lod0,
                      record_type_code=type_code, tail_reserved=reserved, node_ordinal=ordinal,
                      node_link=None if link == 0xFFFFFFFF else link,
                      structured_tail_size=size)
    declared_group_count = records[0]['record_prefix_u16'] if records else 0
    prefix_count_matches = bool(records) and declared_group_count == len(records)
    later_prefixes_zero = all(record['record_prefix_u16'] == 0 for record in records[1:])
    return {'offset': start, 'revision': revision, 'file_path': path,
            'file_path_size': path_size, 'first_array_count': first_array_count,
            'first_array_offset': count_offset + 4, 'named_groups': records,
            'named_group_count': len(records),
            'declared_named_group_count': declared_group_count,
            'group_count_prefix_validated': prefix_count_matches and later_prefixes_zero,
            'opaque_psid_body_bytes': max(0, section_end - (count_offset + 4)),
            'arrays_fully_decoded': False}


def _read_meta_paths(data):
    """Decode the observed TSS ATEM metadata-path table.

    Revisions 88/89/90/92 use a reserved u32, a u32 entry count, and STR16
    entries whose lengths include a trailing NUL. The current
    sample corpus contains .nut authoring paths, which are not game archive
    texture resources; callers should report them without extracting them.
    """
    # META is stored byte-reversed. Search after 5LVI so a coincidental ATEM
    # byte sequence inside vertex or shader payloads cannot masquerade as the
    # metadata chunk.
    list_header = data.find(b'5LVI')
    search_from = list_header + 8 if list_header >= 0 else 0
    start = data.find(b'ATEM', search_from)
    if start < 0 or start + 16 > len(data):
        return None
    revision, reserved, count = struct.unpack_from('>3I', data, start + 4)
    if revision not in (88, 89, 90, 92) or count > 100_000:
        return {'offset': start, 'revision': revision, 'reserved': reserved,
                'parse_error': f'Unsupported ATEM metadata layout {revision}/{count}'}
    pos = start + 16
    paths = []
    for index in range(count):
        if pos + 2 > len(data):
            return {'offset': start, 'revision': revision, 'reserved': reserved,
                    'parse_error': f'Truncated ATEM string length at entry {index}'}
        size = struct.unpack_from('>H', data, pos)[0]
        pos += 2
        if size > 4096 or pos + size > len(data):
            return {'offset': start, 'revision': revision, 'reserved': reserved,
                    'parse_error': f'Invalid ATEM string length {size} at entry {index}'}
        raw = data[pos:pos + size]
        pos += size
        if raw.endswith(b'\0'):
            raw = raw[:-1]
        if not raw or any(c < 32 or c > 126 for c in raw):
            return {'offset': start, 'revision': revision, 'reserved': reserved,
                    'parse_error': f'Invalid ATEM path text at entry {index}'}
        paths.append(raw.decode('latin1'))
    return {'offset': start, 'revision': revision, 'reserved': reserved,
            'path_count': count, 'paths': paths, 'strings_fully_decoded': True,
            'end_offset': pos}


def _read_texture_refs(data, start, end):
    """Collect external TEX paths embedded in the observed TSS LTMU block."""
    if start<0 or end<=start:return []
    refs=[]
    for hit in re.finditer(rb'[A-Za-z0-9_./\\-]+\.tex\b',data[start:end],re.I):
        value=hit.group().decode('latin1').replace('\\','/')
        # TSS prefixes texture layer/slot values before the resource path.
        if value.startswith('/'):value=value[1:]
        if '/' in value and value.split('/',1)[0] in ('-','1','0',"'"):value=value.split('/',1)[1]
        if value and '/' in value:refs.append(value.lower())
    return sorted(set(refs))


SLOT_SAMPLER_BYTES = bytes.fromhex('00010000000000ff000100')


def _read_texture_ref(data, pos, end):
    """Read a 17-byte+ texture reference: GUID, flag, optional named entry.

    A zero GUID with flag 0 is an empty reference. Otherwise the GUID is
    followed by 00 01 00 00, a u8 name length, the NUL-terminated name, a
    9-byte trailer whose fifth byte is the mip count, and a
    further (secondary) 17-byte texture reference.
    """
    if pos + 17 > end:
        raise FormatError(f'Truncated texture reference at {pos:#x}')
    guid = data[pos:pos + 16]
    if guid == bytes(16):
        if data[pos + 16] != 0:
            raise FormatError(f'Unexpected empty texture reference flag at {pos + 16:#x}')
        return None, pos + 17
    if data[pos + 16:pos + 20] != b'\x00\x01\x00\x00':
        raise FormatError(f'Unsupported texture reference header at {pos + 16:#x}')
    size = data[pos + 20]
    name_end = pos + 21 + size
    if size < 2 or name_end + 9 > end or data[name_end - 1] != 0:
        raise FormatError(f'Invalid texture reference name at {pos + 20:#x}')
    name = data[pos + 21:name_end - 1].decode('latin1')
    trailer = data[name_end:name_end + 9]
    if trailer[:4] != bytes(4) or trailer[5:] != b'\x00\x01\x00\x02':
        raise FormatError(f'Unexpected texture reference trailer at {name_end:#x}')
    secondary, next_pos = _read_texture_ref(data, name_end + 9, end)
    ref = {'guid': guid.hex(), 'name': name.replace('\\', '/'), 'mip_count': trailer[4]}
    if secondary:
        ref['secondary'] = secondary
    return ref, next_pos


def _read_texture_slots(data, record_start, record_end):
    """Read a material's texture-slot array.

    The array is a u32 slot count followed by 17 zero bytes and ``count``
    slots. Each slot is a u8-length NUL-terminated external path (empty =
    one NUL), the sampler bytes 00 01 00 00 00 00 00 ff 00 01 00, a u8 kind
    (0 = none or GUID reference, 4 = external path) and a texture reference.
    The array start is located by validating the whole array, never by a
    single pattern hit.
    """
    probe = record_start
    while True:
        hit = data.find(SLOT_SAMPLER_BYTES, probe, record_end)
        if hit < 0:
            return None
        probe = hit + 1
        # Walk back to a candidate slot start: the first slot follows the
        # count word and 17 zero bytes; its name is either empty or a path.
        for name_size in range(1, 256):
            slot_start = hit - 1 - name_size
            count_pos = slot_start - 21
            if count_pos < record_start:
                break
            if data[slot_start] != name_size:
                continue
            if data[count_pos + 4:slot_start] != bytes(17):
                continue
            count = struct.unpack_from('>I', data, count_pos)[0]
            if not 1 <= count <= 64:
                continue
            try:
                slots, pos = _parse_slot_array(data, slot_start, count, record_end)
            except FormatError:
                continue
            return {'offset': count_pos, 'count': count, 'slots': slots, 'end_offset': pos}


def _parse_slot_array(data, pos, count, end):
    slots = []
    for index in range(count):
        size = data[pos]
        name_end = pos + 1 + size
        if size == 0 and slots and data[pos:pos + 17] == bytes(17):
            # Trailing slots are serialized as bare empty references.
            break
        if size < 1 or name_end + 12 > end or data[name_end - 1] != 0:
            raise FormatError(f'Invalid texture slot name at {pos:#x}')
        path = data[pos + 1:name_end - 1].decode('latin1')
        if data[name_end:name_end + 11] != SLOT_SAMPLER_BYTES:
            raise FormatError(f'Unexpected texture slot sampler bytes at {name_end:#x}')
        kind = data[name_end + 11]
        ref, pos = _read_texture_ref(data, name_end + 12, end)
        slot = {'index': index, 'kind': kind}
        if path:
            slot['external_path'] = path.replace('\\', '/').lstrip('/').lower()
        if ref:
            slot['embedded'] = ref
        if (kind == 4) != bool(path):
            raise FormatError(f'Texture slot kind {kind} inconsistent with path at {name_end + 11:#x}')
        slots.append(slot)
    return slots, pos


def _read_ltm_materials(data, start, end, declared_count):
    """Read observed LTMU material-name records and scope TEX paths.

    These revisions serialize a material name as 01 00 00 <length>, a
    null-terminated name, then a stable 01 00 01 00 01 00 marker and an
    opaque shader/material payload. The payload is not fully understood; this
    reader only uses the validated record starts to associate texture paths
    with the material that owns their byte range. Revision 280 uses the same
    prefix with a length excluding NUL and a four-byte value before the marker.
    """
    if start < 0 or end <= start or start + 12 > end:
        return [], []
    revision, count = struct.unpack_from('>2I', data, start + 4)
    if revision not in (280, 361, 364, 382, 384, 385, 387) or count != declared_count:
        return [], [{'offset': start, 'reason': f'Unsupported LTMU material layout {revision}/{count}'}]
    records = []
    failures = []
    p = start + 12
    while p + 24 <= end:
        if data[p:p + 3] != b'\x01\x00\x00':
            p += 1
            continue
        name_len = data[p + 3]
        name_start = p + 4
        name_end = name_start + name_len
        if not 2 <= name_len <= 200 or name_end + 6 > end:
            p += 1
            continue
        if revision == 280:
            raw_name = data[name_start:name_end - 1]
            if (name_end + 10 > end or data[name_end - 1] != 0 or
                    struct.unpack_from('>I', data, name_end)[0] != 4 or
                    data[name_end + 4:name_end + 10] != b'\x01\x00\x01\x00\x01\x00'):
                p += 1
                continue
        else:
            raw_name = data[name_start:name_end - 1]
            if data[name_end - 1] != 0 or data[name_end:name_end + 6] != b'\x01\x00\x01\x00\x01\x00':
                p += 1
                continue
        if not raw_name or any(c < 32 or c > 126 for c in raw_name):
            p += 1
            continue
        records.append({'offset': p, 'name': raw_name.decode('latin1')})
        p = name_end + (10 if revision == 280 else 6)
    if len(records) != declared_count:
        return [], [{'offset': start, 'reason': f'Found {len(records)} validated LTMU material records; header declares {declared_count}'}]
    for i, record in enumerate(records):
        record_end = records[i + 1]['offset'] if i + 1 < len(records) else end
        record['end_offset'] = record_end
        record['texture_paths'] = _read_texture_refs(data, record['offset'], record_end)
        try:
            record['texture_slots'] = _read_texture_slots(data, record['offset'], record_end)
        except FormatError as exc:
            record['texture_slots'] = None
            record['texture_slot_error'] = str(exc)
        # Keep the observed shader descriptor headers visible without claiming
        # that their vertex-like attributes are fully interpreted.
        dxtv = []
        scan = record['offset']
        while True:
            q = data.find(b'DXTV', scan, record_end)
            if q < 0:
                break
            if q + 12 <= record_end:
                version, attributes = struct.unpack_from('>2I', data, q + 4)
                if version == 169 and attributes <= 64 and q + 12 + attributes * 3 <= record_end:
                    desc = [tuple(data[q + 12 + j * 3:q + 15 + j * 3]) for j in range(attributes)]
                    dxtv.append({'offset': q, 'revision': version, 'attribute_count': attributes,
                                 'attributes': desc})
            scan = q + 4
        record['dxtv_headers'] = dxtv
    return records, failures


def read_gsc(data, geometry=True):
    """Decode a GSC's draws, display groups, named nodes and material table.

    ``meshes`` holds one decoded draw per HSEM record (when ``geometry``).
    ``display_groups`` binds records to LTMU material indices, and ``nodes``
    gives each named PSID node's model-space transform and its LOD0
    ``(material, record)`` draws.
    """
    mesh_tag=data.find(b'HSEM')  # FourCC bytes stored little-endian as reversed text.
    if mesh_tag<0 or mesh_tag+16>len(data):raise FormatError('GSC HSEM/MESH section not found')
    revision,header_value,flags=struct.unpack_from('>3I',data,mesh_tag+4)
    if revision not in (200,201) or header_value>100_000_000:raise FormatError(f'Unsupported GSC mesh section revision/header value {revision}/{header_value}')
    section_end=data.find(b'LTMU',mesh_tag+16)
    if section_end<0:raise FormatError('GSC material table boundary missing')
    failures=[]
    # The HSEM header value is the number of SMNR records.
    records,objects,records_end=_parse_hsem_records(data,mesh_tag,section_end,header_value)
    if records_end!=section_end:
        failures.append({'offset':records_end,'reason':f'HSEM records end {section_end-records_end} bytes before LTMU'})
    meshes=[]
    for record in records:
        if not geometry:
            continue
        draw,error=_build_record_draw(data,record,objects)
        if error:
            failures.append({'offset':record['offset'],'record':record['index'],'reason':error})
        else:
            meshes.append(draw)
    # LTMU is the start of the material table. Its revision/count are safe to
    # expose, but the variable-length revision-384+ material records must not
    # be interpreted as the legacy fixed-size UMTL structs.
    ltm=data.find(b'LTMU',mesh_tag+16)
    supc=data.find(b'SUPC',ltm+12) if ltm>=0 else -1
    psid=data.find(b'PSID',supc+8 if supc>=0 else (ltm+12 if ltm>=0 else mesh_tag+16))
    if psid < 0:
        psid = len(data)
    material_end = supc if ltm >= 0 and supc >= ltm + 12 and supc < psid else psid
    ltm_header=None
    material_records=[]
    material_failures=[]
    if ltm>=0 and ltm+12<=len(data):
        mat_revision,mat_count=struct.unpack_from('>2I',data,ltm+4)
        if mat_count<=100_000:
            ltm_header={'offset':ltm,'revision':mat_revision,'declared_count':mat_count,
                        'end_offset':material_end}
            material_records,material_failures=_read_ltm_materials(data,ltm,
                material_end,mat_count)
        else:
            failures.append({'offset':ltm,'reason':f'Implausible LTMU material count {mat_count}'})
    failures.extend(material_failures)
    names=_read_names(data)
    psid_header=_read_psid_display_records(data,psid,names)
    if psid_header and psid_header.get('parse_error'):
        failures.append({'offset':psid,'reason':psid_header['parse_error']})
    meta_table=_read_meta_paths(data)
    if meta_table and meta_table.get('parse_error'):
        failures.append({'offset':meta_table['offset'],'reason':meta_table['parse_error']})
    texture_refs=_read_texture_refs(data,ltm,material_end)
    support_section=None
    if supc>=0 and supc+8<=len(data):
        support_end=psid if psid>=supc else min(len(data),supc+16)
        support_section={'offset':supc,
                         'revision':struct.unpack_from('>I',data,supc+4)[0],
                         'size':support_end-supc,
                         'body_hex':data[supc+8:support_end].hex()}
    display_groups=None
    nodes=[]
    if psid < len(data):
        try:
            display_groups=_read_display_groups(data,psid,len(records))
        except FormatError as exc:
            failures.append({'offset':psid,'reason':str(exc)})
    if display_groups:
        material_count=ltm_header['declared_count'] if ltm_header else 0
        referenced=set()
        for group in display_groups['groups']:
            for entry in group['entries']:
                referenced.add(entry['record'])
                if entry['material']>=material_count:
                    failures.append({'offset':entry['offset'],'reason':f'Display entry material {entry["material"]} outside LTMU count {material_count}'})
        unreferenced=[r['index'] for r in records if r['index'] not in referenced]
        if unreferenced:
            failures.append({'offset':psid,'reason':f'{len(unreferenced)} HSEM records are not bound by any display group'})
        for named in (psid_header or {}).get('named_groups',[]):
            node={'name':named['name'],'ordinal':named.get('node_ordinal'),
                  'transform_4x4_column_major':named.get('transform_4x4_column_major'),
                  'display_groups':named.get('display_groups',[]),
                  'lod_distances':named.get('lod_distances',[]),
                  'lod0_display_group':named.get('lod0_display_group'),
                  'type_code':named.get('record_type_code'),'draws':[]}
            if named.get('tail_parse_error'):
                failures.append({'offset':named['offset'],'reason':f'{named["name"]}: {named["tail_parse_error"]}'})
            lod0=node['lod0_display_group']
            if lod0 is not None:
                if lod0>=len(display_groups['groups']):
                    failures.append({'offset':named['offset'],'reason':f'{named["name"]} display group {lod0} out of range'})
                else:
                    node['draws']=[{'material':e['material'],'record':e['record'],
                                    'render_state':e['render_state']}
                                   for e in display_groups['groups'][lod0]['entries']]
            nodes.append(node)
    return {'revision':revision,'flags':flags,'mesh_section_header_value':header_value,
            'names':names,'meshes':meshes,'mesh_records':len(records),
            'records':[{k:v for k,v in r.items() if k not in ('slots','position_frames')} |
                       {'position_frame_count':len(r.get('position_frames',[]))} for r in records],
            'display_groups':display_groups,'nodes':nodes,
            'material_table':ltm_header,'materials':material_records,
            'support_section':support_section,'display_table':psid_header,'metadata_table':meta_table,
            'material_texture_paths':texture_refs,'failures':failures,
            'complete':False,
            'unresolved_sections':['LTMU shader payload semantics (texture slots, colours, blend state)',
                                   'Named node hierarchy/animation link semantics (transforms are used as model space)',
                                   'Display entry render_state and flag bits',
                                   'ATEM .nut paths are parsed metadata references, not resources in the PC game archive']}

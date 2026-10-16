"""Bind decoded GSC materials to texture roles and render-pass flags.

Texture roles come from each slot's texture name (authoring suffixes such as
``_diff``, ``_nrm``/``_nml``/``_normal``, ``_nrmr``, ``_mask`` and the LEGO
``tpage``/``tpage_nrm`` pair). A slot whose name gives no role keeps its slot
index only; the lowest such slot is offered as the colour candidate and is
reported as ``role_source='slot_order'``.

The byte at offset 0x46 of the 1358-byte block after the texture slots is 0
for every ``ShadowImpostor`` material in the audited corpus, but also for some
visible character materials; it is exposed raw as ``colour_pass``. Shadow
proxies are identified by the authored ShadowImpostor material type.
"""
import re

COLOUR_PASS_OFFSET = 0x46
POST_SLOT_STATE_SIZE = 1358

_ROLE_PATTERNS = (
    ('normal_roughness', re.compile(r'(_nrmr|_normalr)$')),
    ('normal', re.compile(r'(_nrm|_nml|_normal|_norm|_n)$')),
    ('mask', re.compile(r'(_mask|_msk)$')),
    ('alpha', re.compile(r'(_alpha|_opacity|_a)$')),
    ('emissive', re.compile(r'(_em|_emissive|_glow|_illum)$')),
    ('environment', re.compile(r'(_cubemap|_cube|_env)$')),
    ('color', re.compile(r'(_diff|_diffm|_diffuse|_col|_colour|_color|_albedo|_d|_tpage)$')),
)


def texture_role(name):
    stem = name.replace('\\', '/').rsplit('/', 1)[-1].lower()
    stem = stem.rsplit('.', 1)[0]
    stem = re.sub(r'_dx11$', '', stem)
    if stem.endswith('tpage_nrm'):
        return 'normal'
    if stem.endswith('tpage'):
        return 'color'
    if 'lightmaptex' in stem:
        return 'engine_lightmap_dummy'
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(stem):
            return role
    return None


def material_bindings(data, parsed, texture_set=None):
    """Return one binding per LTMU material with resolved texture sources."""
    by_guid = {}
    if texture_set:
        by_guid = {e['guid']: e for e in texture_set['entries'] if e['kind'] == 'embedded'}
    bindings = []
    for index, material in enumerate(parsed['materials']):
        binding = {'index': index, 'name': material['name'], 'textures': [],
                   'colour_pass': None, 'issues': []}
        slots = material.get('texture_slots')
        if slots:
            post = data[slots['end_offset']:material['end_offset']]
            if len(post) >= POST_SLOT_STATE_SIZE:
                binding['colour_pass'] = bool(post[COLOUR_PASS_OFFSET])
            for slot in slots['slots']:
                if slot.get('external_path'):
                    source = {'kind': 'external', 'path': slot['external_path']}
                    name = slot['external_path']
                elif slot.get('embedded'):
                    ref = slot['embedded']
                    entry = by_guid.get(ref['guid'])
                    if entry is None:
                        binding['issues'].append(f'Embedded texture {ref["name"]} is not in the .nxg_textures set')
                        continue
                    source = {'kind': 'embedded', 'guid': ref['guid'], 'name': ref['name']}
                    name = ref['name']
                else:
                    continue
                role = texture_role(name)
                binding['textures'].append({'slot': slot['index'], 'role': role,
                                            'role_source': 'name' if role else None, **source})
        else:
            binding['issues'].append(material.get('texture_slot_error') or 'Texture slot array not found')
        if not any(t['role'] == 'color' for t in binding['textures']):
            unnamed = [t for t in binding['textures'] if t['role'] is None]
            if unnamed:
                first = min(unnamed, key=lambda t: t['slot'])
                first['role'] = 'color'
                first['role_source'] = 'slot_order'
        # The authored ShadowImpostor material type marks shadow-caster proxy
        # geometry. The 0x46 byte alone is not sufficient: some visible
        # character materials also store 0 there.
        binding['shadow_proxy'] = 'shadowimpostor' in material['name'].lower()
        bindings.append(binding)
    return bindings

"""Blender assembly of decoded GSC nodes, draws and materials.

Each placement names one GSC node. The node's LOD0 display group lists
``(material, record)`` draws; every draw becomes a mesh shared by all
placements of the same GSC, positioned by the node's model-space matrix.
Draws whose material is excluded from the colour pass (shadow impostors)
are placed in a hidden collection.
"""
import json
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

from .native_gsc_reader import read_gsc, _build_record_draw, _parse_hsem_records

BASIS = Matrix(((-1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
# Vertex colour attribute 2 stores B, G, R, A bytes (linear values).
VERTEX_COLOUR_ORDER = (2, 1, 0, 3)


def _column_major(columns):
    return Matrix([[columns[c][r] for c in range(4)] for r in range(4)])


class GscLibrary:
    def __init__(self, asset_root, gsc_audits, textures):
        self.root = Path(asset_root)
        self.audits = gsc_audits
        self.textures = textures
        self.parsed = {}
        self.meshes = {}
        self.materials = {}
        self.images = {}
        self.failures = []

    def _load(self, resource):
        key = resource.replace('\\', '/').lower()
        if key not in self.parsed:
            data = (self.root / resource).read_bytes()
            parsed = read_gsc(data, False)
            mesh_tag = data.find(b'HSEM')
            records, objects, _ = _parse_hsem_records(
                data, mesh_tag, data.find(b'LTMU', mesh_tag + 16), parsed['mesh_records'])
            self.parsed[key] = {'data': data, 'parsed': parsed, 'records': records,
                                'objects': objects,
                                'nodes': {n['name']: n for n in parsed['nodes']},
                                'bindings': self.audits.get(key, {}).get('material_bindings', [])}
        return self.parsed[key]

    def node(self, resource, name):
        return self._load(resource)['nodes'].get(name)

    def binding(self, resource, material):
        bindings = self._load(resource)['bindings']
        return bindings[material] if material < len(bindings) else None

    def mesh(self, resource, record, material):
        key = (resource.lower(), record, material)
        if key in self.meshes:
            return self.meshes[key]
        entry = self._load(resource)
        draw, error = _build_record_draw(entry['data'], entry['records'][record], entry['objects'])
        if error:
            self.failures.append({'resource': resource, 'record': record, 'reason': error})
            self.meshes[key] = None
            return None
        attrs = draw['attributes']
        verts = [(-v[0], -v[2], v[1]) for v in attrs[0]]
        idx = draw['indices']
        faces = [(idx[i + 2], idx[i + 1], idx[i]) for i in range(0, len(idx), 3)]
        mesh = bpy.data.meshes.new(f'{Path(resource).stem}/r{record}/m{material}')
        mesh.from_pydata(verts, [], faces)
        mesh.validate(clean_customdata=False)
        mesh.update()
        uv_types = sorted(t for t, v in attrs.items() if t >= 5 and v and len(v[0]) in (2, 4))
        for t in uv_types:
            values = attrs[t]
            for half, suffix in ((0, ''), (2, 'b')):
                if len(values[0]) < half + 2:
                    continue
                uv = mesh.uv_layers.new(name=f'UV{t}{suffix}')
                uv.data.foreach_set('uv', [c for loop in mesh.loops
                                           for c in (values[loop.vertex_index][half],
                                                     1.0 - values[loop.vertex_index][half + 1])])
        if 2 in attrs:
            col = mesh.color_attributes.new(name='Color0', type='FLOAT_COLOR', domain='POINT')
            col.data.foreach_set('color', [values[i] / 255.0 for values in attrs[2]
                                           for i in VERTEX_COLOUR_ORDER])
        if 1 in attrs:
            normals = []
            for v in attrs[1]:
                n = 2 * Vector(v[:3]) - Vector((1, 1, 1))
                if n.length_squared:
                    n.normalize()
                normals.append((-n.x, -n.z, n.y))
            for poly in mesh.polygons:
                poly.use_smooth = True
            mesh.normals_split_custom_set_from_vertices(normals)
        mesh.materials.append(self.material(resource, material))
        mesh['lswtss_gsc_source'] = resource
        mesh['lswtss_gsc_record'] = record
        mesh['lswtss_gsc_material_index'] = material
        mesh['lswtss_gsc_position_frames'] = entry['records'][record].get('position_frames') and \
            len(entry['records'][record]['position_frames']) or 0
        self.meshes[key] = mesh
        return mesh

    def _image(self, texture):
        if texture['kind'] == 'embedded':
            key = 'GSC:' + texture['guid']
            path = None
            for audit in self.audits.values():
                hit = audit.get('embedded_textures', {}).get(texture['guid'])
                if hit:
                    path = hit['image']
                    break
        else:
            key = 'LSWTSS:' + texture['path']
            path = (self.textures.get(texture['path']) or {}).get('image')
        if not path or not Path(path).exists():
            return None
        if key not in self.images:
            image = bpy.data.images.load(path, check_existing=True)
            image.name = key
            image.colorspace_settings.name = 'sRGB' if texture['role'] == 'color' else 'Non-Color'
            image.pack()
            self.images[key] = image
        return self.images[key]

    def material(self, resource, index):
        binding = self.binding(resource, index) or {'name': f'material {index}', 'textures': []}
        key = f'GSC:{Path(resource).stem}:{binding["name"]}'
        if key in self.materials:
            return self.materials[key]
        mat = bpy.data.materials.new(key)
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
        vcol = nt.nodes.new('ShaderNodeVertexColor')
        vcol.layer_name = 'Color0'
        vcol.location = (-700, 300)
        colour_out = vcol.outputs['Color']
        alpha_out = vcol.outputs['Alpha']
        issues = list(binding.get('issues', []))
        by_role = {}
        for t in binding['textures']:
            by_role.setdefault(t['role'], t)
        uv = nt.nodes.new('ShaderNodeUVMap')
        uv.uv_map = 'UV5'
        uv.location = (-1000, 0)
        colour_tex = by_role.get('color')
        if colour_tex:
            image = self._image(colour_tex)
            if image:
                tex = nt.nodes.new('ShaderNodeTexImage')
                tex.image = image
                tex.location = (-700, 0)
                nt.links.new(uv.outputs['UV'], tex.inputs['Vector'])
                mix = nt.nodes.new('ShaderNodeMix')
                mix.data_type = 'RGBA'
                mix.blend_type = 'MULTIPLY'
                mix.inputs['Factor'].default_value = 1.0
                mix.location = (-400, 200)
                nt.links.new(colour_out, mix.inputs['A'])
                nt.links.new(tex.outputs['Color'], mix.inputs['B'])
                colour_out = mix.outputs['Result']
                if image.alpha_mode != 'NONE' and 'tpage' not in image.name.lower():
                    amul = nt.nodes.new('ShaderNodeMath')
                    amul.operation = 'MULTIPLY'
                    amul.location = (-400, -100)
                    nt.links.new(alpha_out, amul.inputs[0])
                    nt.links.new(tex.outputs['Alpha'], amul.inputs[1])
                    alpha_out = amul.outputs[0]
            else:
                issues.append(f'Colour texture not converted: {colour_tex.get("path") or colour_tex.get("name")}')
        normal_tex = by_role.get('normal')
        if normal_tex:
            image = self._image(normal_tex)
            if image:
                tex = nt.nodes.new('ShaderNodeTexImage')
                tex.image = image
                tex.location = (-700, -350)
                nt.links.new(uv.outputs['UV'], tex.inputs['Vector'])
                nmap = nt.nodes.new('ShaderNodeNormalMap')
                nmap.uv_map = 'UV5'
                nmap.location = (-400, -350)
                nt.links.new(tex.outputs['Color'], nmap.inputs['Color'])
                nt.links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])
            else:
                issues.append(f'Normal texture not converted: {normal_tex.get("path") or normal_tex.get("name")}')
        nt.links.new(colour_out, bsdf.inputs['Base Color'])
        nt.links.new(alpha_out, bsdf.inputs['Alpha'])
        # LEGO surface type comes from the authored TPage_LEGO_<Type> material
        # name; the vertex colour supplies the brick colour for every type.
        name = binding['name'].lower()
        bsdf.inputs['Roughness'].default_value = 0.3
        if 'transparent' in name:
            bsdf.inputs['Transmission Weight'].default_value = 1.0
            bsdf.inputs['Roughness'].default_value = 0.05
            bsdf.inputs['IOR'].default_value = 1.5
        elif 'chrome' in name or 'metallic' in name:
            bsdf.inputs['Metallic'].default_value = 1.0
            bsdf.inputs['Roughness'].default_value = 0.15 if 'chrome' in name else 0.3
        elif 'pearlescent' in name:
            bsdf.inputs['Metallic'].default_value = 0.5
            bsdf.inputs['Roughness'].default_value = 0.25
        if 'glow' in name:
            nt.links.new(colour_out, bsdf.inputs['Emission Color'])
            bsdf.inputs['Emission Strength'].default_value = 2.0
        mat['lswtss_gsc_material'] = binding['name']
        mat['lswtss_gsc_shadow_proxy'] = bool(binding.get('shadow_proxy'))
        mat['lswtss_gsc_textures'] = json.dumps(binding['textures'])
        mat['lswtss_gsc_issues'] = json.dumps(issues)
        self.materials[key] = mat
        return mat


def place_gsc(library, item, parent, visible_collection, hidden_collection, proxy_collection):
    """Create objects for one GSC placement; return (objects, row status, reason)."""
    resource = item.get('gsc')
    node = library.node(resource, item.get('gsc_node') or item.get('source_group'))
    if node is None:
        return [], 'unresolved', 'Named node not found in GSC'
    if not node['draws']:
        return [], 'no_geometry', 'Node is a locator without geometry'
    columns = node.get('transform_4x4_column_major')
    local = _column_major(columns) if columns else Matrix.Identity(4)
    matrix = BASIS @ local @ BASIS.inverted()
    # Scenes export one entity per GSC node whose local transform already is
    # the node's model-space matrix; applying it again doubles offsets and
    # scales (e.g. 24x-wide terminal screens). Only apply it when the parent
    # entity does not already carry it.
    node_in_entity = parent is not None and all(
        abs(a - b) < 1e-3 * max(1.0, abs(b)) for ra, rb in zip(parent.matrix_basis, matrix) for a, b in zip(ra, rb))
    if node_in_entity:
        matrix = Matrix.Identity(4)
    objects = []
    for draw in node['draws']:
        mesh = library.mesh(resource, draw['record'], draw['material'])
        if mesh is None:
            continue
        binding = library.binding(resource, draw['material']) or {}
        obj = bpy.data.objects.new(f"{node['name']}/r{draw['record']}", mesh)
        if binding.get('shadow_proxy'):
            proxy_collection.objects.link(obj)
        elif not item.get('enabled', True) or 'ingameanimation/rigcontrols/' in resource.lower():
            # Rig-control GSCs are animation helper gizmos, not scenery.
            hidden_collection.objects.link(obj)
        else:
            visible_collection.objects.link(obj)
        obj.parent = parent
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = matrix
        obj['lswtss_gsc_resource'] = resource
        obj['lswtss_gsc_node'] = node['name']
        obj['lswtss_gsc_shadow_proxy'] = bool(binding.get('shadow_proxy'))
        obj['lswtss_gsc_node_transform'] = 'carried by entity' if node_in_entity else 'applied'
        objects.append(obj)
    if not objects:
        return [], 'unresolved', 'No draw of this node decoded'
    return objects, 'placed', None

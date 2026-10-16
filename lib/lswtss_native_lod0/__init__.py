bl_info = {
    'name': 'Skywalker Saga Native LOD0 — Research Preview',
    'author': 'LSWTSS Level Exporter contributors', 'version': (0, 3, 0), 'blender': (4, 2, 0),
    'location': 'File > Import',
    'description': 'Import native model root draw groups and inspect direct map placements',
    'category': 'Import-Export',
}

import json
from collections import Counter
from pathlib import Path
import bpy
from bpy.props import StringProperty, CollectionProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion, Vector
from .native_model_reader import read_model
from .native_gsc_reader import read_gsc
from .gsc_blender import GscLibrary, place_gsc
from .landscape_blender import build_landscape

BASIS = Matrix(((-1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))

def model_meshes(filepath):
    """Return shared Blender mesh datablocks for every native root draw."""
    parsed = read_model(Path(filepath).read_bytes(), True)
    meshes = []
    for si,scene in enumerate(parsed):
        for root in scene['lod0']:
            for di,draw in enumerate(root['draws']):
                raw=scene['meshes'][draw['mesh']]
                attrs=raw['attributes']
                verts=[(-v[0],-v[2],v[1]) for v in attrs[0]]
                idx=raw['indices'];faces=[list(reversed(idx[i:i+3])) for i in range(0,len(idx),3)]
                label=f'{Path(filepath).stem}/root/{si}/{di}'
                mesh=bpy.data.meshes.new(label)
                mesh.from_pydata(verts,[],faces);mesh.update()
                for attr,name,start in ((5,'UV0',0),(5,'UV1',2),(7,'UV2',0)):
                    if attr in attrs and len(attrs[attr][0])>=start+2:
                        uv=mesh.uv_layers.new(name=name)
                        uv.data.foreach_set('uv',[x for loop in mesh.loops for x in attrs[attr][loop.vertex_index][start:start+2]])
                for attr,name in ((2,'Color0'),(4,'Color1')):
                    if attr in attrs:
                        col=mesh.color_attributes.new(name=name,type='FLOAT_COLOR',domain='POINT')
                        fmt=next((a[1] for b in raw['layout'] for a in b['attributes'] if a[0]==attr),None)
                        divisor=1 if fmt==8 else 255
                        col.data.foreach_set('color',[x/divisor for values in attrs[attr] for x in values])
                normals=attrs.get(1)
                if normals:
                    # Packed normals are UNORM bytes in the observed layout.
                    fmt=next((a[1] for b in raw['layout'] for a in b['attributes'] if a[0]==1),None)
                    values=[]
                    for v in normals:
                        n=Vector(v[:3])
                        if fmt==8:n=2*n-Vector((1,1,1))
                        if n.length_squared:n.normalize()
                        values.append((-n.x,-n.z,n.y))
                    for p in mesh.polygons:p.use_smooth=True
                    mesh.normals_split_custom_set_from_vertices(values)
                material=scene['materials'][draw['material']]
                key='LSWTSS:'+material['path']
                mat=bpy.data.materials.get(key)
                if mat is None:
                    mat=bpy.data.materials.new(key);mat.use_nodes=True
                    mat['lswtss_resource']=material['path']
                    mat['lswtss_material_name']=material['name']
                    mat['lswtss_textures_resolved']=False
                mesh.materials.append(mat)
                mesh['lswtss_source']=str(filepath)
                mesh['lswtss_root_node']=root['name']
                mesh['lswtss_mesh_index']=draw['mesh']
                mesh['lswtss_lod']=0
                mesh['lswtss_material_slot']=draw['material']
                mesh['lswtss_material_name']=material['name']
                meshes.append(mesh)
    return meshes

def instance_coloured(mesh,ivc_path,cache):
    """Copy of mesh with Color0 replaced by the matching per-instance stream (or None)."""
    key=(ivc_path,mesh.name)
    if key in cache:return cache[key]
    result=None
    try:
        import ivc_reader
        streams=cache.setdefault(('streams',ivc_path),ivc_reader.read_streams(Path(ivc_path).read_bytes())['streams'])
        stream=next((s for s in streams if s['vertices']==len(mesh.vertices)),None)
        if stream is not None:
            result=mesh.copy();result.name=mesh.name+' (instance colour)'
            col=result.color_attributes.get('Color0') or result.color_attributes.new(name='Color0',type='FLOAT_COLOR',domain='POINT')
            col.data.foreach_set('color',[b/255 for b in stream['rgba']])
            result['lswtss_instance_vertex_colours']=str(ivc_path)
    except Exception as exc:
        result=None;cache[('error',ivc_path)]=str(exc)
    cache[key]=result;return result

def procedural_mesh(shape,cache):
    """Unit primitives in native space (Blender axes): 0 sphere (diameter 1),
    1 plane (1 x 1 in native XZ, normal native +Y = Blender +Z), 2 box (unit cube)."""
    if shape in cache:return cache[shape]
    import bmesh
    bm=bmesh.new()
    if shape==0:bmesh.ops.create_uvsphere(bm,u_segments=32,v_segments=16,radius=.5,calc_uvs=True)
    elif shape==1:bmesh.ops.create_grid(bm,x_segments=1,y_segments=1,size=.5,calc_uvs=True)
    else:bmesh.ops.create_cube(bm,size=1,calc_uvs=True)
    mesh=bpy.data.meshes.new(('Procedural sphere','Procedural plane','Procedural box')[min(shape,2)] if shape in (0,1,2) else 'Procedural shape %d'%shape)
    bm.to_mesh(mesh);bm.free()
    if mesh.uv_layers:mesh.uv_layers[0].name='UV0'
    col=mesh.color_attributes.new(name='Color0',type='FLOAT_COLOR',domain='POINT')
    col.data.foreach_set('color',[1.0]*4*len(mesh.vertices))
    mesh.materials.append(None)  # one slot, filled per object
    cache[shape]=mesh;return mesh

def override_material(base,overrides,cache):
    """Material variant carrying a placement's parameter/texture overrides."""
    if base is None or not overrides:return base
    sig=json.dumps(overrides,sort_keys=True)
    key=(base.name,sig)
    if key not in cache:
        mat=base.copy();mat.name=base.name+' (instance override)'
        mat['lswtss_overrides']=sig
        cache[key]=mat
    return cache[key]

def add_model(filepath,collection,name=None,matrix=None):
    result=[]
    for i,mesh in enumerate(model_meshes(filepath)):
        obj=bpy.data.objects.new(f'{name or Path(filepath).stem}/{i}',mesh)
        collection.objects.link(obj)
        if matrix is not None:obj.matrix_world=matrix
        result.append(obj)
    return result

def import_gsc_draws(filepath,collection):
    """Import each verified indexed GSC draw as source-local Blender geometry."""
    parsed=read_gsc(Path(filepath).read_bytes(),True);objects=[]
    for i,raw in enumerate(parsed['meshes']):
        attrs=raw['attributes'];verts=[(-v[0],-v[2],v[1]) for v in attrs[0]]
        idx=raw['indices'];faces=[list(reversed(idx[j:j+3])) for j in range(0,len(idx),3)]
        mesh=bpy.data.meshes.new(f'{Path(filepath).stem}/GSC draw {i}')
        mesh.from_pydata(verts,[],faces);mesh.update()
        for attr,name,start in ((5,'UV0',0),(5,'UV1',2),(7,'UV2',0)):
            if attr in attrs and attrs[attr] and len(attrs[attr][0])>=start+2:
                uv=mesh.uv_layers.new(name=name)
                uv.data.foreach_set('uv',[x for loop in mesh.loops for x in attrs[attr][loop.vertex_index][start:start+2]])
        for attr,name in ((2,'Color0'),(4,'Color1')):
            if attr in attrs and attrs[attr]:
                col=mesh.color_attributes.new(name=name,type='FLOAT_COLOR',domain='POINT')
                fmt=next((a[1] for layout in raw['layouts'] for a in layout['attributes'] if a[0]==attr),None)
                scale=1 if fmt==8 else 255
                col.data.foreach_set('color',[x/scale for v in attrs[attr] for x in v])
        if attrs.get(1):
            fmt=next((a[1] for layout in raw['layouts'] for a in layout['attributes'] if a[0]==1),None)
            normals=[]
            for v in attrs[1]:
                n=Vector(v[:3])
                if fmt==8:n=2*n-Vector((1,1,1))
                if n.length_squared:n.normalize()
                normals.append((-n.x,-n.z,n.y))
            for poly in mesh.polygons:poly.use_smooth=True
            mesh.normals_split_custom_set_from_vertices(normals)
        mesh['lswtss_gsc_source']=str(filepath);mesh['lswtss_gsc_draw_index']=i
        mesh['lswtss_gsc_materials_decoded']=False
        obj=bpy.data.objects.new(mesh.name,mesh);collection.objects.link(obj);objects.append(obj)
    return parsed,objects

def import_preview(manifest_path):
    manifest=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if manifest.get('format')=='lswtss-native-hierarchy-preview-2':return import_hierarchy_preview(manifest,Path(manifest_path))
    if manifest.get('format')!='lswtss-native-preview-1':raise ValueError('Unsupported preview manifest')
    collection=bpy.data.collections.new('Utapau — EXPERIMENTAL direct art')
    bpy.context.scene.collection.children.link(collection)
    diagnostics=bpy.data.collections.new('Unresolved records — inspect report')
    collection.children.link(diagnostics);diagnostics.hide_render=True
    diagnostics.hide_viewport=True
    root=Path(manifest['asset_root']);cache={};rows=[];objects=0
    for record in manifest['records']:
        row={'name':record['name'],'guid':record['guid'],'offset':record['offset'],'scene':record['scene']}
        try:
            if record['status']!='available':raise ValueError(record.get('reason',record['status']))
            path=root/record['model'];key=str(path)
            if key not in cache:cache[key]=model_meshes(path)
            xf=record['transforms'][0];w=xf['world'];scale=xf['scale']
            matrix=BASIS @ Matrix.LocRotScale(Vector(w[4:7]),Quaternion((w[3],*w[:3])),Vector(scale[:3])) @ BASIS.inverted()
            for i,mesh in enumerate(cache[key]):
                obj=bpy.data.objects.new(f"{record['name']}/{i}",mesh);collection.objects.link(obj)
                obj.matrix_world=matrix
                obj['lswtss_record_guid']=record['guid'];obj['lswtss_record_offset']=record['offset']
                obj['lswtss_scene']=record['scene'];obj['lswtss_visibility']='unverified'
                objects+=1
            row['status']='preview_placed'
        except Exception as exc:
            row['status']='unresolved';row['reason']=str(exc)
            obj=bpy.data.objects.new(record['name'],None);diagnostics.objects.link(obj)
            obj.empty_display_type='PLAIN_AXES';obj['lswtss_reason']=str(exc)
            if len(record['transforms'])==1:
                w=record['transforms'][0]['world'];obj.location=(w[4],-w[6],w[5])
        rows.append(row)
    report={'complete_map':False,'objects':objects,'source_models':len(cache),'records':rows,'limitations':manifest['limitations']}
    text=bpy.data.texts.new('Utapau import report.json');text.write(json.dumps(report,indent=2))
    collection['lswtss_complete_map']=False
    return report

def import_hierarchy_preview(manifest,manifest_path=None):
    map_name=manifest.get('map','Native map')
    collection=bpy.data.collections.new(f'{map_name} — native hierarchy preview')
    bpy.context.scene.collection.children.link(collection)
    hidden=bpy.data.collections.new('Native disabled render geometry')
    collection.children.link(hidden);hidden.hide_render=True;hidden.hide_viewport=True
    root=Path(manifest['asset_root']);cache={};records=[];object_count=0;visible_count=0;gsc_rows=[]
    instance_cache={};override_cache={};instance_colour_count=0;override_count=0
    procedural_cache={};procedural_count=0;procedural_failures=[]
    textures={}
    if manifest_path is not None:
        mtm=manifest_path.with_name('material_texture_manifest.json')
        if mtm.exists():textures=json.loads(mtm.read_text(encoding='utf-8')).get('textures',{})
    gsc_library=GscLibrary(root,manifest.get('gsc_audits',{}),textures)
    gsc_visible=bpy.data.collections.new('GSC objects');collection.children.link(gsc_visible)
    gsc_proxies=bpy.data.collections.new('GSC shadow proxies (not rendered)');collection.children.link(gsc_proxies);gsc_proxies.hide_render=True;gsc_proxies.hide_viewport=True
    gsc_markers=bpy.data.collections.new('GSC unresolved markers');collection.children.link(gsc_markers);gsc_markers.hide_render=True
    terrain_collection=bpy.data.collections.new('Terrain');collection.children.link(terrain_collection);terrain_count=0;terrain_failures=[]
    foliage_collection=bpy.data.collections.new('Foliage');collection.children.link(foliage_collection);foliage_count=0;foliage_failures=[]
    procedural_collection=bpy.data.collections.new('Procedural shapes');collection.children.link(procedural_collection)
    scene_nodes={};scene_roots={}
    for source_scene in manifest['scenes']:
        entities=source_scene['entities'];nodes={};by_id={e['id']:e for e in entities}
        scene_nodes[source_scene['path'].lower()]=nodes;scene_roots[source_scene['path'].lower()]=(source_scene,[e['id'] for e in entities if e['parent'] is None])
        for e in entities:
            obj=bpy.data.objects.new(e['name'],None);obj.empty_display_size=.04
            collection.objects.link(obj);nodes[e['id']]=obj
            obj['lswtss_record_guid']=e['guid'];obj['lswtss_record_offset']=e['offset'];obj['lswtss_scene']=source_scene['path']
        for e in entities:
            obj=nodes[e['id']]
            if e['parent'] is not None:
                if e['parent'] not in nodes:raise ValueError('Native entity parent is missing')
                obj.parent=nodes[e['parent']];obj.matrix_parent_inverse=Matrix.Identity(4)
            xf=e['local_transform']
            if xf is not None:
                q=xf['quaternion_xyzw']
                obj.matrix_basis=BASIS@Matrix.LocRotScale(Vector(xf['position'][:3]),Quaternion((q[3],*q[:3])),Vector(xf['scale'][:3]))@BASIS.inverted()
        for e in entities:
            ancestor=e;ancestor_disabled=False
            while ancestor is not None:
                ancestor_disabled|=not ancestor['enabled']
                ancestor=by_id.get(ancestor['parent'])
            for render in e['render_models']:
                row={'name':e['name'],'guid':e['guid'],'offset':e['offset'],'component_offset':render['component_offset'],'scene':source_scene['path'],'native_parent':e['parent'],'render_flags':render.get('render_flag_bytes')}
                try:
                    if render['status'] not in ('available','authored_hidden') or 'model' not in render:raise ValueError(render.get('reason',render['status']))
                    if e['local_transform'] is None:raise ValueError('Renderable entity lacks a decoded local transform')
                    path=root/render['model'];key=str(path)
                    if key not in cache:cache[key]=model_meshes(path)
                    inactive=ancestor_disabled or render['status']=='authored_hidden'
                    ivc=render.get('instance_vertex_colours')
                    for i,mesh in enumerate(cache[key]):
                        if ivc:
                            painted=instance_coloured(mesh,root/ivc,instance_cache)
                            if painted is not None:mesh=painted;instance_colour_count+=1
                        obj=bpy.data.objects.new(f"{e['name']}/{i}",mesh)
                        # Overrides name the model material slot and its material.
                        mine=[o for o in render.get('overrides',[]) if o.get('slot')==mesh.get('lswtss_material_slot') and o.get('node')]
                        if mine and mesh.materials:
                            variant=override_material(mesh.materials[0],[{k:o[k] for k in ('node','socket','type','value','texture')} for o in mine],override_cache)
                            obj.material_slots[0].link='OBJECT';obj.material_slots[0].material=variant;override_count+=1
                        (hidden if inactive else collection).objects.link(obj)
                        obj.parent=nodes[e['id']];obj.matrix_parent_inverse=Matrix.Identity(4);obj.matrix_basis=Matrix.Identity(4)
                        obj['lswtss_record_guid']=e['guid'];obj['lswtss_record_offset']=e['offset'];obj['lswtss_scene']=source_scene['path']
                        obj['lswtss_authored_hidden']=inactive
                        object_count+=1;visible_count+=not inactive
                    row['status']='native_hidden' if inactive else 'hierarchy_placed'
                except Exception as exc:row['status']='unresolved';row['reason']=str(exc)
                records.append(row)
            for fol in e.get('foliage',[]):
                if fol.get('status')!='available':continue
                try:
                    path=root/fol['model_resolved'];key=str(path)
                    if key not in cache:cache[key]=model_meshes(path)
                    target=hidden if (ancestor_disabled or not fol.get('enabled',True)) else foliage_collection
                    for k,m in enumerate(fol['instances']):
                        # Row-vector game matrix -> column-vector, then exporter basis.
                        mg=Matrix([m[0:4],m[4:8],m[8:12],m[12:16]]).transposed()
                        local=BASIS@mg@BASIS.inverted()
                        for i,mesh in enumerate(cache[key]):
                            obj=bpy.data.objects.new(f"{e['name']}/foliage{k}/{i}",mesh);target.objects.link(obj)
                            obj.parent=nodes[e['id']];obj.matrix_parent_inverse=Matrix.Identity(4);obj.matrix_basis=local
                            obj['lswtss_scene']=source_scene['path'];obj['lswtss_foliage']=fol['foliage']
                            foliage_count+=1
                except Exception as exc:foliage_failures.append({'entity':e['name'],'model':fol.get('model'),'reason':str(exc)})
            for proc in e.get('procedural',[]):
                if proc['role']=='collision_or_editor' or not proc.get('material'):continue
                try:
                    key='LSWTSS:'+proc['material']
                    mat=bpy.data.materials.get(key)
                    if mat is None:mat=bpy.data.materials.new(key);mat.use_nodes=True;mat['lswtss_resource']=proc['material']
                    mine=[{k:o[k] for k in ('node','socket','type','value','texture')} for o in proc.get('overrides',[]) if o.get('node')]
                    mat=override_material(mat,mine,override_cache) if mine else mat
                    mesh=procedural_mesh(proc['shape'],procedural_cache)
                    off=ancestor_disabled or not proc.get('enabled',True)
                    obj=bpy.data.objects.new(f"{e['name']}/procedural",mesh);(hidden if off else procedural_collection).objects.link(obj)
                    obj.material_slots[0].link='OBJECT';obj.material_slots[0].material=mat
                    obj.parent=nodes[e['id']];obj.matrix_parent_inverse=Matrix.Identity(4);obj.matrix_basis=Matrix.Identity(4)
                    obj['lswtss_procedural_role']=proc['role']
                    if proc['role']=='lightmap_emitter':
                        # Lightmap-only emitters: never seen in game, but they are
                        # the light sources the game's bake used, so keep their light.
                        obj.visible_camera=False;obj.visible_shadow=False;obj.visible_glossy=False
                        obj.display_type='WIRE'
                    procedural_count+=1
                except Exception as exc:procedural_failures.append({'entity':e['name'],'reason':str(exc)})
            for land in e.get('landscapes',[]):
                if land.get('status')!='available':continue
                try:
                    build_landscape(land,nodes[e['id']],hidden if ancestor_disabled else terrain_collection,f"{e['name']} terrain")
                    terrain_count+=1
                except Exception as exc:terrain_failures.append({'entity':e['name'],'reason':str(exc)})
            for item in e.get('gsc_objects',[]):
                row={'name':e['name'],'guid':e['guid'],'offset':e['offset'],'component_offset':item['component_offset'],'scene':source_scene['path'],'resource':item.get('gsc'),'node':item.get('source_group'),'status':item['status']}
                placed=[]
                if item['status']=='available':
                    try:
                        if e['local_transform'] is None:raise ValueError('GSC entity lacks a decoded local transform')
                        inactive=ancestor_disabled or not item.get('enabled',True)
                        placed,status,reason=place_gsc(gsc_library,item,nodes[e['id']],hidden if inactive else gsc_visible,hidden,gsc_proxies)
                        row['status']=status
                        if reason:row['reason']=reason
                        row['objects']=len(placed)
                    except Exception as exc:row['status']='unresolved';row['reason']=str(exc)
                else:row['reason']=item.get('reason',item['status'])
                if not placed:
                    obj=bpy.data.objects.new(f"{e['name']} — GSC {row['status']}",None)
                    gsc_markers.objects.link(obj);obj.empty_display_type='CUBE';obj.empty_display_size=.12
                    obj.parent=nodes[e['id']];obj.matrix_parent_inverse=Matrix.Identity(4);obj.matrix_basis=Matrix.Identity(4)
                    obj['lswtss_gsc_resource']=item.get('gsc','') or '';obj['lswtss_gsc_node']=item.get('source_group','') or ''
                    obj['lswtss_reason']=row.get('reason','')
                gsc_rows.append(row)
    # Referenced scenes are authored in world space: the game ignores the
    # referencing entity's transform (Federal District's tech layer would rise
    # 240 m; Kachirho's village layers sit 2-5 m off the terrain if it is
    # applied). Parent them under the reference entity for organisation only,
    # cancelling the host transform so their world placement is unchanged.
    bpy.context.view_layer.update()
    attached=0
    for key,(source_scene,roots) in scene_roots.items():
        link=source_scene.get('attach')
        if not link:continue
        host=scene_nodes.get(link['scene'].lower(),{}).get(link['entity_id'])
        if host is None:continue
        for rid in roots:
            child=scene_nodes[key][rid];local=child.matrix_basis.copy()
            child.parent=host;child.matrix_parent_inverse=host.matrix_world.inverted();child.matrix_basis=local
            attached+=1
    bpy.context.view_layer.update()
    # Projected decals onto every visible surface placed above.
    decal_report={};decal_count=0
    try:
        from .decals_blender import build_decals
        bpy.context.view_layer.update()
        decal_collection=bpy.data.collections.new('Decals');collection.children.link(decal_collection)
        decal_rows=[]
        for source_scene in manifest['scenes']:
            nodes=scene_nodes[source_scene['path'].lower()];by_id={e['id']:e for e in source_scene['entities']}
            for e in source_scene['entities']:
                for d in e.get('decals',[]):
                    if not d.get('material'):continue
                    # The component header byte is 0 on every decal in the game (6,452 checked), so only the entity chain decides.
                    anc=e;off=False
                    while anc is not None:off|=not anc['enabled'];anc=by_id.get(anc['parent'])
                    if off:decal_report['disabled']=decal_report.get('disabled',0)+1;continue
                    key='LSWTSS:'+d['material']
                    mat=bpy.data.materials.get(key)
                    if mat is None:
                        mat=bpy.data.materials.new(key);mat.use_nodes=True;mat['lswtss_resource']=d['material']
                    decal_rows.append({'name':e['name']+' decal','matrix':nodes[e['id']].matrix_world.copy(),'material':mat})
        receivers=[o for c in (collection,gsc_visible,terrain_collection) for o in c.objects if o.type=='MESH']
        decal_count=build_decals(decal_rows,receivers,decal_collection,decal_report)
        decal_report['requested']=len(decal_rows)
    except Exception as exc:decal_report['error']=str(exc)
    gsc_status=Counter(r['status'] for r in gsc_rows)
    report={'complete_map':False,'objects':object_count,'visible_objects':visible_count,'hidden_objects':object_count-visible_count,'source_models':len(cache),'records':records,'gsc_placements':len(gsc_rows),'gsc_placement_status':dict(gsc_status),'gsc_meshes_imported':sum(m is not None for m in gsc_library.meshes.values()),'gsc_mesh_failures':gsc_library.failures,'terrain_objects':terrain_count,'terrain_failures':terrain_failures,'foliage_objects':foliage_count,'foliage_failures':foliage_failures,'instance_coloured_objects':instance_colour_count,'instance_colour_errors':[v for k,v in instance_cache.items() if k[0]=='error'][:20],'override_objects':override_count,'override_materials':len(override_cache),'decal_objects':decal_count,'decal_report':decal_report,'procedural_objects':procedural_count,'procedural_failures':procedural_failures[:20],'gsc_rows':gsc_rows,'limitations':manifest['limitations'],'coordinate_mapping':manifest['coordinate_mapping']}
    text=bpy.data.texts.new(f'{map_name} hierarchy import report.json');text.write(json.dumps(report,indent=2))
    collection['lswtss_complete_map']=False
    return report

class LSWTSS_OT_native_model(bpy.types.Operator,ImportHelper):
    bl_idname='import_scene.lswtss_native_lod0'
    bl_label='Skywalker Saga Native LOD0 Model'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.model'
    filter_glob:StringProperty(default='*.model',options={'HIDDEN'})
    files:CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory:StringProperty(subtype='DIR_PATH')
    def execute(self,context):
        collection=bpy.data.collections.new('Skywalker Saga — native LOD0')
        context.scene.collection.children.link(collection)
        count=0;failures=[]
        paths=[Path(self.directory)/f.name for f in self.files] or [Path(self.filepath)]
        for path in paths:
            try:count+=len(add_model(path,collection))
            except Exception as exc:failures.append({'file':str(path),'reason':str(exc)})
        text=bpy.data.texts.new('Native LOD0 model import report.json')
        text.write(json.dumps({'mesh_objects':count,'failures':failures,'placement':'Source model coordinates; this is not a map assembly','textures_resolved':False},indent=2))
        self.report({'WARNING' if failures else 'INFO'},f'Imported {count} root submeshes; {len(failures)} unsupported files. Report in Text Editor.')
        return {'FINISHED'} if count else {'CANCELLED'}

class LSWTSS_OT_native_gsc(bpy.types.Operator,ImportHelper):
    bl_idname='import_scene.lswtss_native_gsc'
    bl_label='Skywalker Saga GSC Draw Meshes'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.gsc'
    filter_glob:StringProperty(default='*.gsc',options={'HIDDEN'})
    files:CollectionProperty(type=bpy.types.OperatorFileListElement)
    directory:StringProperty(subtype='DIR_PATH')
    def execute(self,context):
        paths=[Path(self.directory)/f.name for f in self.files] or [Path(self.filepath)]
        collection=bpy.data.collections.new('Skywalker Saga — decoded GSC draws')
        context.scene.collection.children.link(collection);reports=[];count=0;failures=[]
        for path in paths:
            try:
                parsed,objects=import_gsc_draws(path,collection);count+=len(objects)
                reports.append({'file':str(path),'draw_meshes':len(objects),'names':parsed['names'],'vertices':sum(x['vertex_count'] for x in parsed['meshes']),'triangles':sum(x['index_count']//3 for x in parsed['meshes']),'material_table':parsed['material_table'],'display_table':parsed['display_table'],'materials_decoded':False,'complete':parsed['complete'],'decode_failures':parsed['failures'],'unresolved_sections':parsed['unresolved_sections']})
            except Exception as exc:failures.append({'file':str(path),'reason':str(exc)})
        text=bpy.data.texts.new('GSC Draw Import Report.json');text.write(json.dumps({'imported_mesh_objects':count,'files':reports,'failures':failures,'placement':'Source-local geometry; the level placement is not inferred'},indent=2))
        self.report({'WARNING' if failures or count==0 else 'INFO'},f'Imported {count} GSC draw meshes; material/display mapping remains incomplete. See GSC Draw Import Report.')
        return {'FINISHED'} if count else {'CANCELLED'}

class LSWTSS_OT_preview(bpy.types.Operator,ImportHelper):
    bl_idname='import_scene.lswtss_map_research_preview'
    bl_label='Skywalker Saga Map Research Preview'
    bl_options={'REGISTER','UNDO'}
    filename_ext='.json'
    filter_glob:StringProperty(default='*.json',options={'HIDDEN'})
    def execute(self,context):
        try:
            r=import_preview(self.filepath)
            missing=sum(x['status']=='unresolved' for x in r['records'])
            self.report({'WARNING'},f"Experimental: {r['objects']} objects; {missing} unresolved records. See Text Editor report.")
            return {'FINISHED'}
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}

def menu(self,context):
    self.layout.operator(LSWTSS_OT_native_model.bl_idname,text='Skywalker Saga Native LOD0 (.model)')
    self.layout.operator(LSWTSS_OT_native_gsc.bl_idname,text='Skywalker Saga GSC Draw Meshes (.gsc)')
    self.layout.operator(LSWTSS_OT_preview.bl_idname,text='Skywalker Saga Map Research Preview (.json)')

CLASSES=(LSWTSS_OT_native_model,LSWTSS_OT_native_gsc,LSWTSS_OT_preview)
def register():
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu)
def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu)
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)

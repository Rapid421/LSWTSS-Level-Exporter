"""Run inside Blender: assemble geometry, source shader graphs, textures and lights."""
import bpy, json, math, sys
from collections import Counter
from pathlib import Path
from mathutils import Matrix, Quaternion, Vector

args=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
if len(args)!=3:raise SystemExit('Usage: blender -b --python build_blend.py -- hierarchy_manifest material_texture_manifest output.blend')
hierarchy_path,material_path,output_path=map(lambda x:Path(x).resolve(),args)
here=Path(__file__).resolve().parent
sys.path.insert(0,str(here/'lib'))
import lswtss_native_lod0 as addon
from lswtss_native_lod0.material_graph import apply_materials

bpy.ops.wm.read_factory_settings(use_empty=True)
report=addon.import_preview(hierarchy_path)
mat_report=apply_materials(material_path)
scene=bpy.context.scene
scene.render.engine='CYCLES';scene.cycles.samples=32;scene.cycles.preview_samples=8;scene.cycles.use_denoising=True
scene.render.resolution_x=1280;scene.render.resolution_y=720;scene.render.resolution_percentage=100
scene.view_settings.view_transform='AgX'
hierarchy=json.loads(hierarchy_path.read_text(encoding='utf-8'))
sky=hierarchy.get('sky');sky_status='no skydome texture override found in the selected scenes'
if sky:
 from lswtss_native_lod0.sky_world import build_sky_world
 sky_textures=json.loads(material_path.read_text(encoding='utf-8')).get('textures',{})
 image=(sky_textures.get(sky['texture'].replace('\\','/').strip('/').lower()) or {}).get('image')
 sky_obj=next((o for o in bpy.data.objects if o.type=='EMPTY' and o.get('lswtss_record_guid')==sky['entity_guid'] and o.get('lswtss_scene')==sky['scene']),None)
 if image and Path(image).exists():
  scene.world=build_sky_world(image,sky_obj,1.0,'Native sky — '+Path(sky['texture']).stem)
  sky_status='native sky texture'+(' with sky entity rotation' if sky_obj else ' (sky entity transform not found; unrotated)')
  # The world now draws the sky; the dome meshes would otherwise occlude it.
  for o in bpy.data.objects:
   if o.type=='MESH' and '/skies/' in str(o.data.get('lswtss_source','')).replace('\\','/').lower():
    o.hide_render=True;o['lswtss_hidden_reason']='Skydome mesh replaced by the native sky World'
 else:sky_status='sky texture was not converted: '+sky['texture']
if not sky or not sky_status.startswith('native'):
 scene.world=bpy.data.worlds.new('Native level ambient preview');scene.world.use_nodes=True
 scene.world.node_tree.nodes['Background'].inputs['Color'].default_value=(.2,.24,.32,1)
 scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value=.22
# Editor-only helpers (map calibration frames) are never drawn in game.
EDITOR_HELPERS=('/shared_assets/map/mapcalibrationframe',)
for o in bpy.data.objects:
 if o.type=='MESH' and any(h in str(o.data.get('lswtss_source','')).replace('\\','/').lower() for h in EDITOR_HELPERS):
  o.hide_render=True;o.hide_set(True);o.hide_viewport=True;o['lswtss_hidden_reason']='Editor-only map calibration helper'
scene['lswtss_sky']=sky_status
scene['lswtss_map']=hierarchy_path.stem.replace('_hierarchy_manifest','')
scene['lswtss_complete_map']=False
scene['lswtss_lod']='LOD0 only'
scene['lswtss_light_conversion']='sun: pi*intensity W/m2; point/spot: 4*pi^2*intensity W (native albedo*I/d^2 lighting, no baked lightmaps)'
scene['lswtss_materials']='Translated native material graph links; see Material Translation Report.json for unsupported nodes.'

# Recreate each light's native entity chain and authored transform.
light_path=hierarchy_path.with_name('light_manifest.json')
lm=json.loads(light_path.read_text(encoding='utf-8'))
light_collection=bpy.data.collections.new('Native authored lights');scene.collection.children.link(light_collection)
unresolved=bpy.data.collections.new('Unresolved lights — disabled');light_collection.children.link(unresolved);unresolved.hide_render=True;unresolved.hide_viewport=True
B=Matrix(((-1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))
type_map={0:'SUN',1:'POINT',2:'SPOT',3:'POINT'}
light_rows=[]
for src in lm['scenes']:
 scene_path=src['path'];ents={e['id']:e for e in src['entities']};native_lights=[x for x in lm['lights'] if x['scene']==scene_path];needed=set()
 for l in native_lights:
  p=l['parent']
  while p is not None and p not in needed:needed.add(p);p=ents[p]['parent']
 nodes={}
 for i in needed:
  e=ents[i]
  obj=next((o for o in bpy.data.objects if o.get('lswtss_record_guid')==e['guid'] and o.get('lswtss_scene')==scene_path),None)
  if obj is None:obj=bpy.data.objects.new('Light parent: '+e['name'],None);light_collection.objects.link(obj)
  nodes[i]=obj
 for i in needed:
  e=ents[i];obj=nodes[i]
  if e['parent'] in nodes:obj.parent=nodes[e['parent']];obj.matrix_parent_inverse=Matrix.Identity(4)
  xf=e.get('local_transform')
  if xf:
   q=xf['quaternion_xyzw'];obj.matrix_basis=B@Matrix.LocRotScale(Vector(xf['position'][:3]),Quaternion((q[3],*q[:3])),Vector(xf['scale'][:3]))@B.inverted()
 for l in native_lights:
  n=l['native'];p=ents.get(l['parent']);valid=bool(l['framing_complete'] and p and p.get('local_transform'));disabled=not l['enabled'];anc=p
  while anc:
   disabled|=not anc['enabled'];anc=ents.get(anc['parent'])
  typ=int(n.get('Type',1));emitters=5 if typ==3 else 1
  for sample in range(emitters):
   name=f"{p['name'] if p else 'Unresolved'}/native light {sample+1}"
   data=bpy.data.lights.new(name,type_map.get(typ,'POINT'));obj=bpy.data.objects.new(name,data)
   (light_collection if valid else unresolved).objects.link(obj)
   if p and l['parent'] in nodes:obj.parent=nodes[l['parent']];obj.matrix_parent_inverse=Matrix.Identity(4)
   obj.rotation_euler=(math.radians(-90),0,0)
   if typ==3:obj.location=(0,-float(n.get('Length',0))*((sample+.5)/emitters-.5),0)
   data.color=n.get('Base Color',(1,1,1));intensity=max(0,float(n.get('Intensity',1))*float(n.get('HDR Intensity Multiplier',1)))
   # Native diffuse lighting is albedo*intensity*NdotL*falloff, with no 1/pi in the BRDF.
   # Cycles shades albedo/pi * irradiance, so a sun needs pi*intensity W/m2 and a point
   # or spot needs 4*pi^2*intensity W to give the same inverse-square result.
   data.energy=math.pi*intensity if typ==0 else 4*math.pi*math.pi*intensity/emitters
   if typ==0:data.angle=max(0,math.radians(float(n.get('Apparent Angular Diameter',.5))))
   else:
    data.shadow_soft_size=max(.001,float(n.get('Bulb Diameter',.05))/2);data.use_custom_distance=True;data.cutoff_distance=max(.001,float(n.get('Radius',20)))
   if typ==2:data.spot_size=min(math.pi,max(.001,math.radians(float(n.get('Spot FOV',45)))));data.spot_blend=.15
   data.use_shadow=bool(n.get('Shadow Cast DX11',True));obj.hide_render=disabled or not valid;obj.hide_viewport=disabled
   obj['lswtss_light_guid']=l['guid'];obj['lswtss_scene']=scene_path;obj['lswtss_intensity_calibrated']=False
  light_rows.append({'guid':l['guid'],'scene':scene_path,'status':'placed' if valid else 'unresolved_disabled'})

issues=Counter(issue.split(':',1)[0] for row in mat_report for issue in row.get('issues',[]))
normal_graphs=[m for m in bpy.data.materials if m.get('lswtss_shader_graph') and 'Expand Normal Map' in m.get('lswtss_shader_graph')]
normal_ok=[]
for m in normal_graphs:
 bs=next((n for n in m.node_tree.nodes if n.type=='BSDF_PRINCIPLED'),None) if m.node_tree else None
 if bs and bs.inputs.get('Normal') and bs.inputs['Normal'].is_linked and any(l.from_node.type=='NORMAL_MAP' for l in m.node_tree.links if l.to_node==bs and l.to_socket==bs.inputs['Normal']):normal_ok.append(m.name)
prepare=json.loads(hierarchy_path.with_name('prepare_report.json').read_text(encoding='utf-8'))
stats={'complete_map':False,'mesh_objects':sum(o.type=='MESH' and not o.hide_render for o in scene.objects),'sky':sky_status,'terrain_objects':report.get('terrain_objects',0),'foliage_objects':report.get('foliage_objects',0),'instance_coloured_objects':report.get('instance_coloured_objects',0),'instance_colour_errors':report.get('instance_colour_errors',[]),'override_objects':report.get('override_objects',0),'override_materials':report.get('override_materials',0),'decal_objects':report.get('decal_objects',0),'decal_report':report.get('decal_report',{}),'procedural_objects':report.get('procedural_objects',0),'procedural_failures':report.get('procedural_failures',[]),'foliage_failures':report.get('foliage_failures',[])[:20],'terrain_failures':report.get('terrain_failures',[]),'gsc_placements':prepare.get('gsc_placements',0),'gsc_placement_status':report.get('gsc_placement_status',{}),'gsc_meshes_imported':report.get('gsc_meshes_imported',0),'gsc_mesh_failures':len(report.get('gsc_mesh_failures',[])),'gsc_resources_inventoried':prepare.get('gsc_resources',0),'source_models':report.get('source_models',0),'source_render_records':len(report.get('records',[])),'native_materials':len(mat_report),'translated_material_graphs':sum(bool(x.get('complete_shader')) for x in mat_report),'material_graphs_with_issues':sum(not x.get('complete_shader') for x in mat_report),'common_material_issues':issues.most_common(12),'material_issue_details':[(x['resource'].rsplit('/',1)[-1],x['issues'][:3]) for x in mat_report if not x.get('complete_shader')][:40],'packed_images':sum(bool(i.packed_file) for i in bpy.data.images),'two_channel_normal_graphs':len(normal_graphs),'two_channel_normals_verified':len(normal_ok),'native_lights':len(lm['lights']),'placed_native_lights':sum(x['status']=='placed' for x in light_rows),'unresolved_native_lights':sum(x['status']!='placed' for x in light_rows),'limitations':prepare.get('limitations',[]),'texture_conversion_failures':len(prepare.get('texture_failures',[]))}
bpy.data.texts.new('Level Export Report.json').write(json.dumps(stats,indent=2))
bpy.data.texts.new('Material Translation Report.json').write(json.dumps(mat_report,indent=2))
if any(im.filepath and not im.packed_file for im in bpy.data.images):raise RuntimeError('One or more material textures did not pack')
for screen in bpy.data.screens:
 for area in screen.areas:
  if area.type=='VIEW_3D':area.spaces.active.shading.type='MATERIAL';area.spaces.active.shading.use_scene_lights=True;area.spaces.active.overlay.show_extras=False
bpy.ops.wm.save_as_mainfile(filepath=str(output_path)) if __import__('os').environ.get('LSWTSS_TEST_NO_SAVE')!='1' else None
print('LEVEL_EXPORT',json.dumps(stats))

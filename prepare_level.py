#!/usr/bin/env python3
"""Discover a level's art scenes and extract their native render dependencies."""
from __future__ import annotations
import argparse, json, math, re, struct, subprocess, sys, xml.etree.ElementTree as ET
import types
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
from framed_scene_reader import frames
from native_model_reader import read_model
from native_texture_reader import convert, texture_info, write_dds
from landscape_reader import read_component as read_landscape_component, prepare_landscape
from foliage_reader import read_component as read_foliage_component, read_instances as read_foliage_instances
from render_model_reader import parse_tail as parse_render_model_tail
# Load the add-on's pure parser without importing its Blender-only package init.
_gsc_pkg=types.ModuleType('_lswtss_gsc_parser');_gsc_pkg.__path__=[str(HERE/'lib'/'lswtss_native_lod0')]
sys.modules.setdefault(_gsc_pkg.__name__,_gsc_pkg)
from _lswtss_gsc_parser.native_gsc_reader import read_gsc
from _lswtss_gsc_parser.gsc_textures import read_texture_set, dds_bytes
from _lswtss_gsc_parser.gsc_materials import material_bindings

EXCLUDE_HINTS = ('minimap', 'calibration', 'collision', 'blowup', 'destruct', 'cinematic', 'fake', 'optimisation', 'optimization')

def reader_env(game):
    """The archive reader needs the game's own oo2core_8_win64.dll (Oodle); it is
    loaded from the game folder (working directory + PATH), never redistributed."""
    import os
    env=dict(os.environ);env['PATH']=str(game)+os.pathsep+env.get('PATH','')
    return env

def norm(s: str) -> str: return s.replace('\\', '/').strip('/').lower()

def load_index(path: Path):
    rows=json.loads(path.read_text(encoding='utf-8'))
    catalog={norm(x['path']):x['path'].replace('\\','/') for x in rows}
    return rows,catalog

def resolve(ref, catalog):
    p=norm(ref)
    if p in catalog:return catalog[p]
    if '.' not in p:return None
    stem,ext=p.rsplit('.',1)
    for q in (stem+'_dx11.'+ext, stem+'.material', stem+'_dx11.material'):
        if q in catalog:return catalog[q]
    return None

def scene_references(path: Path):
    """Return scene_baked references: nttSceneGraphReferenceComponent layers and the
    scenes a nttStreamingResourceCollectionComponent streams in at runtime (hub
    events, puzzles and their builds stream in around the player)."""
    data=path.read_bytes();rows=frames(data)['records'];out=[]
    for r in rows:
        kind={'nttSceneGraphReferenceComponent':'layer','nttStreamingResourceCollectionComponent':'streamed'}.get(r['class'])
        if not kind:continue
        raw=data[r['payload_start']:r['end']]
        ent=r['parent']
        while ent is not None and rows[ent]['class']!='apiEntity':ent=rows[ent]['parent']
        if ent is None:continue
        for hit in re.finditer(rb'[A-Za-z0-9_./\\ #()\-]+\.scene_baked\x00',raw,re.I):
            out.append({'path':hit.group()[:-1].decode('ascii').replace('\\','/'),'entity_id':ent,'kind':kind,
                        'entity':rows[ent].get('name'),'enabled':bool(r['enabled_byte'])})
    return out

def gsc_texture_set_path(rel: str) -> str:
    """The companion texture set shares the GSC's path and stem."""
    return rel[:-4]+'.nxg_textures' if rel.lower().endswith('.gsc') else rel

def audit_gsc(path: Path, texture_set_path: Path, png_dir: Path):
    """Decode a GSC's nodes, draw bindings and material texture bindings.

    Embedded textures from the companion .nxg_textures set are written as
    PNG files; external .TEX references are returned for the shared texture
    conversion pass.
    """
    data=path.read_bytes()
    decoded=read_gsc(data,False)
    failures=list(decoded['failures']);texture_set=None
    if texture_set_path.exists():
        tdata=texture_set_path.read_bytes()
        try:texture_set=read_texture_set(tdata)
        except Exception as ex:failures.append({'reason':f'Texture set: {ex}'})
    else:failures.append({'reason':'Companion .nxg_textures set was not extracted'})
    bindings=material_bindings(data,decoded,texture_set)
    embedded={};external=set()
    used_guids={t['guid'] for b in bindings for t in b['textures'] if t['kind']=='embedded'}
    for entry in (texture_set or {}).get('entries',[]):
        if entry['kind']!='embedded' or entry['guid'] not in used_guids:continue
        dest=png_dir/(entry['guid']+'.png');dest.parent.mkdir(parents=True,exist_ok=True)
        try:
            if not dest.exists():
                from PIL import Image
                import io
                im=Image.open(io.BytesIO(dds_bytes(tdata,entry)));im.load();im.save(dest)
            embedded[entry['guid']]={'image':str(dest),'name':entry['name']}
        except Exception as ex:failures.append({'reason':f'Embedded texture {entry["name"]}: {ex}'})
    for b in bindings:
        for t in b['textures']:
            if t['kind']=='external':external.add(t['path'])
    nodes=[{k:n[k] for k in ('name','transform_4x4_column_major','display_groups','lod_distances','lod0_display_group','draws','type_code')} for n in decoded['nodes']]
    return {'mesh_revision':decoded['revision'],'record_count':decoded['mesh_records'],
            'file_size':len(data),'names':decoded['names'],'nodes':nodes,
            'material_bindings':bindings,'embedded_textures':embedded,
            'external_textures':sorted(external),'texture_set':bool(texture_set),
            'material_table':decoded['material_table'],'decode_failures':failures,
            'complete':decoded['complete'],'unresolved_sections':decoded['unresolved_sections']}

def choose_scenes(catalog, query: str):
    # The archive also contains envart asset-library scenes (characters,
    # vehicles and prefab fragments). They share planet names but are not
    # playable/exportable level folders, so only discover from level content.
    files=[Path(p) for p in catalog if p.startswith('levels/') and p.endswith('.scene_baked')]
    q=query.replace('\\','/').strip('/').lower()
    # GUI users select a full folder path from discovery; match that directory
    # exactly. Short typed fragments continue to work as before.
    hits=[p for p in files if (p.parent.as_posix().lower()==q if '/' in q else q in p.as_posix().lower())]
    if not hits:raise RuntimeError(f'No scene_baked resources match {query!r}')
    # Resolve a level folder; prefer the most specific folder with content layers.
    folders=Counter(p.parent for p in hits)
    # Score candidate directories by the number of non-lighting content layers.
    # A broad query that matches multiple plausible folders must
    # ask for a more specific path rather than silently exporting the wrong one.
    scored=[]
    for folder,c in folders.items():
        art_count=sum(1 for p in files if p.parent==folder and 'lighting' not in p.name.lower() and not any(x in p.name.lower() for x in EXCLUDE_HINTS))
        scored.append((art_count,c,folder))
    scored.sort(reverse=True,key=lambda x:(x[0],x[1]))
    if len(scored)>1 and scored[0][:2]==scored[1][:2]:
        options='\n'.join(str(x[2]) for x in scored if x[:2]==scored[0][:2])
        raise RuntimeError(f'Level name is ambiguous. Use a more specific path fragment:\n{options}')
    folder=scored[0][2]
    local=[p for p in files if p.parent==folder]
    selected=[]
    for p in local:
        n=p.name.lower()
        if any(x in n for x in EXCLUDE_HINTS):continue
        selected.append(p)
    if not any('lighting' not in p.name.lower() for p in selected):
        raise RuntimeError(f'No non-lighting content scenes were found in {folder}. This location appears to contain lighting only.')
    return folder, sorted(set(selected))

def discover_level_categories(catalog):
    """Return exportable scene folders grouped by planet or content type."""
    files=[p for p in catalog if p.startswith('levels/') and p.endswith('.scene_baked')]
    aliases={'ahchto':'ahch-to','ahch-to':'ahch-to','ajan kloss':'ajan_kloss','ajan_kloss':'ajan_kloss','kef bir':'kefbir','kefbir':'kefbir','yavin4':'yavin_4','yavin_4':'yavin_4'}
    planets={'ahch-to','ajan_kloss','bespin','cantonica','coruscant','crait','dagobah','dqar','endor','exegol','geonosis','hoth','jakku','kamino','kashyyyk','kefbir','mustafar','naboo','pasaana','takodana','tatooine','utapau','yavin_4'}
    planet_names={'ahch-to':'Ahch-To','ajan_kloss':'Ajan Kloss','dqar':"D'Qar",'kefbir':'Kef Bir','yavin_4':'Yavin 4'}
    hidden_roots={'credits','ident','loadingscreen','screens','titles','test'}
    grouped={}
    for resource in files:
        path=Path(resource);folder=path.parent.as_posix();name=path.name.lower();parts=folder.split('/')
        if any(part in ('cutscenes','cinematics') for part in path.parts):continue
        if len(parts)>1 and parts[1] in hidden_roots:continue
        # Lighting-only folders have no map meshes and make misleading choices.
        if 'lighting' in name or any(hint in name for hint in EXCLUDE_HINTS):continue
        candidate=None
        if len(parts)>3 and parts[:3]==['levels','hub','planets']:
            raw=aliases.get(parts[3].lower(),parts[3].lower())
            candidate=(raw,planet_names.get(raw,raw.replace('_',' ').title())) if raw in planets else ('__other_hub__','Other Hub Locations')
        elif len(parts)>3 and parts[:3]==['levels','hub','local_spaces']:
            raw=aliases.get(parts[3].lower(),parts[3].lower())
            candidate=(raw,planet_names.get(raw,raw.replace('_',' ').title())) if raw in planets else ('__other_hub__','Other Hub Locations')
        elif len(parts)>2 and parts[:2]==['levels','landing_pads']:
            raw=aliases.get(parts[2].lower(),parts[2].lower())
            candidate=(raw,planet_names.get(raw,raw.replace('_',' ').title())) if raw in planets else ('__other_hub__','Other Hub Locations')
        elif len(parts)>2 and parts[:2]==['levels','story'] and parts[2].startswith('episode_'):
            episode=parts[2].replace('episode_','').lstrip('0') or '0'
            candidate=(f'__story_{episode}__',f'Story - Episode {episode}')
        elif len(parts)>2 and parts[:3]==['levels','hub','events']:
            candidate=('__hub_events__','Hub Events')
        elif len(parts)>2 and parts[:3]==['levels','hub','prelevels']:
            candidate=('__hub_previews__','Hub Preview Areas')
        elif len(parts)>2 and parts[:3]==['levels','hub','capital_ship_interiors']:
            candidate=('__capital_interiors__','Capital Ship Interiors')
        elif len(parts)>2 and parts[:3]==['levels','hub','capital_ship_exteriors']:
            candidate=('__capital_exteriors__','Capital Ship Exteriors')
        elif len(parts)>2 and parts[:3]==['levels','hub','arcadegames']:
            candidate=('__arcade__','Arcade Games')
        else:
            candidate=('__other__','Other Levels')
        key,display=candidate
        grouped.setdefault(key,{'key':key,'name':display,'levels':set()})['levels'].add(folder)
    result=[]
    for row in grouped.values():
        result.append({'key':row['key'],'name':row['name'],'levels':sorted(row['levels'])})
    result.sort(key=lambda x:(x['key'].startswith('__'),x['name'].casefold()))
    return result

def parse_scene(path: Path, asset_root: Path, catalog):
    data=path.read_bytes(); parsed=frames(data);rows=parsed['records'];children={}
    for i,r in enumerate(rows):children.setdefault(r['parent'],[]).append(i)
    entities=[];lights=[];counts=Counter(); schema=json.loads((HERE/'light_schema.json').read_text(encoding='utf-8'))
    formats={'Char':'<B','Int':'<i','Float':'<f','Colour3':'<3f'}
    for i,r in enumerate(rows):
        if r['class']!='apiEntity':continue
        parent=r['parent']
        while parent is not None and rows[parent]['class']!='apiEntity':parent=rows[parent]['parent']
        e={'id':i,'name':r.get('name','Entity'),'guid':r['guid'],'offset':r['start'],'parent':parent,'enabled':bool(r['enabled_byte']),'local_transform':None,'render_models':[],'gsc_objects':[]}
        for j in children.get(i,[]):
            c=rows[j];raw=data[c['payload_start']:c['end']]
            if c['class']=='apiTransformComponent' and len(raw)==108:
                v=struct.unpack_from('<12f',raw)
                if all(math.isfinite(x) for x in v) and abs(sum(x*x for x in v[4:8])-1)<.01:
                    e['local_transform']={'scale':v[:4],'quaternion_xyzw':v[4:8],'position':v[8:12]}
            elif c['class']=='ApiRenderModel':
                m={'component_offset':c['start'],'enabled':bool(c['enabled_byte']),'status':'unresolved'}
                hits=[q for q in re.finditer(rb'[A-Za-z0-9_./\\ #()\-]+\.model\x00',raw,re.I) if q.start()>=4 and struct.unpack_from('<I',raw,q.start()-4)[0]==len(q.group())]
                if len(hits)==1:
                    h=hits[0];source=h.group()[:-1].decode('ascii').replace('\\','/');q=h.end()
                    try:
                        n=struct.unpack_from('<I',raw,q)[0];name=raw[q+4:q+3+n].decode('utf-8','replace');q+=4+n
                        opacity=struct.unpack_from('<f',raw,q)[0];flags=list(raw[q+4:q+21]);model=resolve(source,catalog)
                        m.update(source_path=source,source_node=name,opacity=opacity,draw_mesh=bool(flags[4]),render_flag_bytes=flags)
                        # Per-instance material overrides and vertex-colour paint.
                        try:
                            tail=parse_render_model_tail(raw[q:])
                            if tail['overrides']:m['overrides']=tail['overrides']
                            if tail['instance_vertex_colours']:
                                found=resolve(tail['instance_vertex_colours'],catalog)
                                if found:m['instance_vertex_colours']=found
                                else:m['instance_vertex_colours_missing']=tail['instance_vertex_colours']  # stale reference, absent from the archive
                        except Exception as ex:m['override_error']=str(ex)
                        if model:m.update(model=model,status='available')
                        else:m['reason']='Model path not present in archive index'
                        if not m['enabled'] or not m['draw_mesh'] or opacity<=0:m.update(status='authored_hidden',reason='Native component disabled, Draw Mesh false, or opacity zero')
                        elif name in ('Combined_SHAD','Combined_ZOCC'):m.update(status='render_proxy',reason='Shadow or occlusion proxy')
                    except Exception as ex:m['reason']='Malformed render component: '+str(ex)
                else:m['reason']='No unique exact model path in render component'
                # Material texture overrides (e.g. the sky texture on a skydome).
                overrides=[q.group()[:-1].decode('ascii').replace('\\','/') for q in re.finditer(rb'[A-Za-z0-9_./\\ #()\-]+\.texture\x00',raw,re.I)]
                if overrides:m['texture_overrides']=sorted(set(overrides))
                counts[m['status']]+=1;e['render_models'].append(m)
            elif c['class']=='ApiProceduralModel':
                # Unit primitive (0 sphere, 1 plane, 2 box) scaled by the entity
                # transform: u32 2, u8 0, 'ProceduralModel' name, the shared
                # render-model tail (flags, overrides), the material reference,
                # then u32 shape, 3 x u32, u8, i32 layer (-1 rendered; 2/7/8/33
                # are collision/trigger/character-only volumes).
                try:
                    tail=parse_render_model_tail(raw[25:])
                    mats=list(re.finditer(rb'[A-Za-z0-9_./\\ #()\-]+\.material\x00',raw,re.I))
                    mat_path=mats[-1].group()[:-1].decode('ascii').replace('\\','/') if mats else None
                    q=mats[-1].end() if mats else len(raw)-21
                    shape,_,_,_=struct.unpack_from('<4I',raw,q);layer=struct.unpack_from('<i',raw,q+17)[0]
                    nm=r.get('name','').lower()
                    role='render'
                    if layer!=-1 or not mat_path or 'errormaterial' in mat_path.lower():role='collision_or_editor'
                    elif 'lm_only' in nm or 'editoronly' in nm:role='lightmap_emitter'
                    e.setdefault('procedural',[]).append({'component_offset':c['start'],'enabled':bool(c['enabled_byte']),'shape':shape,'layer':layer,
                        'role':role,'material_path':mat_path,'material':resolve(mat_path,catalog) if mat_path else None,
                        'flags':list(raw[29:46]),'overrides':tail['overrides']})
                    counts['procedural_'+role]+=1
                except Exception as ex:counts['procedural_unparsed']+=1
            elif c['class']=='ApiRenderProjectedDecal':
                # A deferred decal: the entity transform is the projection box
                # (unit cube); the material ends in a ProjectedDecal node.
                hit=re.search(rb'[A-Za-z0-9_./\\ #()\-]+\.material\x00',raw,re.I)
                if hit:
                    e.setdefault('decals',[]).append({'component_offset':c['start'],'enabled':bool(c['enabled_byte']),
                        'material_path':hit.group()[:-1].decode('ascii').replace('\\','/'),'material':resolve(hit.group()[:-1].decode('ascii'),catalog)})
                    counts['decals']+=1
            elif c['class']=='ApiRenderFoliage':
                comp=read_foliage_component(raw)
                if comp:
                    comp.update(component_offset=c['start'],enabled=bool(c['enabled_byte']),
                                foliage_resolved=resolve(comp['foliage'],catalog),model_resolved=resolve(comp['model'],catalog))
                    e.setdefault('foliage',[]).append(comp);counts['foliage_components']+=1
            elif c['class']=='Landscape':
                try:
                    comp=read_landscape_component(raw)
                    if comp:e.setdefault('landscapes',[]).append({'component_offset':c['start'],'enabled':bool(c['enabled_byte']),**comp})
                except Exception as ex:e.setdefault('landscapes',[]).append({'component_offset':c['start'],'status':'unresolved','reason':str(ex)})
            elif c['class']=='nttRenderTTExportObject':
                # These components carry static GSC object placements. Keep the
                # exact resource and named sub-object; treating them as only
                # hierarchy nodes was the reason large hub maps looked empty.
                obj={'component_offset':c['start'],'enabled':bool(c['enabled_byte']),'status':'unresolved'}
                paths=[]
                for hit in re.finditer(rb'[A-Za-z0-9_./\\ #()\-]+\.gsc\x00',raw,re.I):
                    if hit.start()>=4 and struct.unpack_from('<I',raw,hit.start()-4)[0]==len(hit.group()):
                        paths.append(hit)
                if len(paths)==1:
                    hit=paths[0];source=hit.group()[:-1].decode('ascii').replace('\\','/');q=hit.end()
                    group=None
                    # The field immediately following the path is a small
                    # fixed header, then the exact GSC display-group name.
                    # The group-name length begins after a one-byte flags
                    # value and is little-endian, unlike the path's length.
                    for pos in range(q+5,min(len(raw)-4,q+24)):
                        n=struct.unpack_from('<I',raw,pos)[0]
                        if 1<=n<=256 and pos+3+n<len(raw) and raw[pos+3+n]==0:
                            value=raw[pos+4:pos+3+n]
                            if re.fullmatch(rb'[A-Za-z0-9_ #().-]+',value):
                                group=value.decode('ascii');break
                    gsc=resolve(source,catalog)
                    obj.update(source_path=source,source_group=group,gsc=gsc,status='geometry_unsupported' if gsc and group else 'unresolved')
                    if not gsc:obj['reason']='GSC path is absent from the game archive index'
                    elif not group:obj['reason']='Could not decode the named GSC display group'
                else:
                    ghg=re.search(rb'[A-Za-z0-9_./\\ #()\-]+\.ghg\x00',raw,re.I)
                    if ghg:
                        obj.update(status='character_unsupported',source_path=ghg.group()[:-1].decode('ascii').replace('\\','/'),
                                   reason='Skinned character model (.ghg) placements are not imported')
                    else:obj['reason']='No unique exact GSC resource path in component'
                counts['gsc_'+obj['status']]+=1
                e['gsc_objects'].append(obj)
        entities.append(e);counts['entities']+=1
    by_record={i:e for e in entities for i in [e['id']]}
    for i,r in enumerate(rows):
        if r['class']!='nttRenderLight':continue
        p=r['payload_start'];native={}
        for name,typ in schema:
            if typ not in formats:break
            fmt=formats[typ];n=struct.calcsize(fmt)
            if p+n>r['end']:raise ValueError(f'Truncated native light in {path.name}')
            v=struct.unpack_from(fmt,data,p);p+=n
            if any(not math.isfinite(x) for x in v):raise ValueError('Nonfinite light property')
            native[name]=v[0] if len(v)==1 else v
            if typ=='Char' and v[0] not in (0,1):raise ValueError('Invalid native light boolean')
        parent=r['parent']
        while parent is not None and rows[parent]['class']!='apiEntity':parent=rows[parent]['parent']
        lights.append({'scene':str(path.relative_to(asset_root)).replace('\\','/'),'guid':r['guid'],'offset':r['start'],'parent':parent,'enabled':bool(r['enabled_byte']),'native':native,'framing_complete':not parsed['rejected_mobj_offsets']})
    return {'path':str(path.relative_to(asset_root)).replace('\\','/'),'frame_count':len(rows),'entities':entities,'frame_diagnostics':{'rejected':len(parsed['rejected_mobj_offsets']),'weak_references':len(parsed['weak_references'])}},counts,lights

def material_graph(path: Path, resource: str):
    raw=path.read_bytes();a=raw.find(b'<materialInstance');b=raw.find(b'</materialInstance>',a)
    if a<0 or b<0:return None,[]
    root=ET.fromstring(raw[a:b+19].decode('latin1'));nodes=[];textures=[]
    for e in root.findall('shaderNodeInstance'):
        vals={x.get('name'):x.get('value','') for x in e if x.tag in ('userParam','userValue')}
        t=e.get('shaderNodeRef','');nodes.append({'name':e.get('name',''),'type':t,'values':vals})
        if t=='sampleTexture2D' and vals.get('tex'):textures.append(vals['tex'])
    links=[dict(x.attrib) for x in root.findall('link')]
    return {'resource':norm(resource),'nodes':nodes,'links':links,'render_state':material_render_state(raw,a)},textures

BLEND_MODES={0:'opaque',1:'alpha_blend',2:'additive'}
ALPHA_MODES={1:'none',5:'alpha_test',6:'alpha_test_special'}
def material_render_state(raw: bytes, xml_start: int):
    """Decode the render-state bytes of the property chunk before the XML graph.

    Verified against each material's compiled .pc_shaders permutation set on
    Federal District (171 materials): byte 0x2F is the blend mode (0 opaque,
    1 alpha blend, 2 additive), 0x33 the alpha mode (1 none, 5 alpha test on
    every cutout-shader material) and 0x1F is 0 for unlit/emissive-only
    materials and 2 for lit ones.
    """
    s=raw.rfind(b'.CC4HSER',0,xml_start)
    if s<4 or xml_start-(s-4)<0x40:return None
    c=raw[s-4:xml_start]
    return {'blend':BLEND_MODES.get(c[0x2f],f'unknown_{c[0x2f]}'),'alpha':ALPHA_MODES.get(c[0x33],f'unknown_{c[0x33]}'),
            'lit':c[0x1f]!=0,'raw':{'0x1f':c[0x1f],'0x2f':c[0x2f],'0x33':c[0x33]}}

def main():
    ap=argparse.ArgumentParser(description='Prepare an LOD0 Blender export from a native Skywalker Saga level.')
    ap.add_argument('--game',required=True,type=Path,help='Game installation folder containing GAME*.DAT')
    ap.add_argument('--archive-reader',required=True,type=Path,help='Bundled ArchiveReader.exe')
    ap.add_argument('--level',required=True,help='Unique level/location name or path fragment, e.g. mining_area or pau_city')
    ap.add_argument('--list-levels',action='store_true',help='List scene folders matching --level and exit')
    ap.add_argument('--list-planets',action='store_true',help='List exportable scene folders grouped by planet')
    ap.add_argument('--output',required=True,type=Path,help='Export working/output folder')
    ap.add_argument('--cache-dir',type=Path,help='Shared archive index/cache directory; can be reused for later levels')
    args=ap.parse_args();game=args.game.resolve();reader=args.archive_reader.resolve();out=args.output.resolve();cache=(args.cache_dir.resolve() if args.cache_dir else out/'game_cache');root=cache/'files';out.mkdir(parents=True,exist_ok=True);cache.mkdir(parents=True,exist_ok=True)
    index_path=cache/'archive_index.json'
    if not index_path.exists():
        subprocess.run([str(reader),str(game),str(cache),'__LSWTSS_INDEX_ONLY__'],check=True,cwd=str(game),env=reader_env(game),creationflags=0x08000000 if sys.platform=='win32' else 0)
    rows,catalog=load_index(index_path)
    if args.list_planets:
        print(json.dumps(discover_level_categories(catalog),separators=(',',':')),flush=True);return
    if args.list_levels:
        q=args.level.replace('\\','/').strip('/').lower()
        candidates=[p for p in catalog if p.startswith('levels/') and p.endswith('.scene_baked')]
        folders=set()
        for resource in candidates:
            path=Path(resource)
            folder=path.parent.as_posix()
            name=path.name.lower()
            if q not in folder.lower() or any(part in ('cutscenes','cinematics') for part in path.parts):
                continue
            if 'lighting' in name or any(hint in name for hint in EXCLUDE_HINTS):
                continue
            folders.add(folder)
        folders=sorted(folders)
        # Keep discovery machine-readable as one line for the Windows UI.
        print(json.dumps(folders,separators=(',',':')),flush=True);return
    folder,scenes=choose_scenes(catalog,args.level)
    def extract(paths,label):
        paths=sorted({p.replace('\\','/') for p in paths if not (root/p.replace('\\','/')).exists()})
        if not paths:return
        listpath=out/(label+'-extract-list.json');listpath.write_text(json.dumps(paths),encoding='utf-8')
        # One extractor at a time per shared cache. An OS byte-range lock on an
        # open handle is released by Windows when the holder exits or is killed
        # (a cancelled export can no longer leave a stale lock behind).
        import os,time,msvcrt
        lockfile=open(cache/'extract.lock','a+b')
        for _ in range(14400):
            try:lockfile.seek(0);msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1);break
            except OSError:time.sleep(.5)
        else:lockfile.close();raise RuntimeError('Timed out waiting for another export to finish extracting')
        try:subprocess.run([str(reader),str(game),str(cache),'@'+str(listpath),'extract'],check=True,cwd=str(game),env=reader_env(game),creationflags=0x08000000 if sys.platform=='win32' else 0)
        finally:
            try:lockfile.seek(0);msvcrt.locking(lockfile.fileno(),msvcrt.LK_UNLCK,1)
            except OSError:pass
            lockfile.close()
    extract([p.as_posix() for p in scenes],'scene')
    # Follow nttSceneGraphReferenceComponent links. A level's root scene
    # references its layers (and reused art from other folders, e.g. story
    # level terrain) from entities whose transforms place them; a referenced
    # scene's root entities are attached under that entity.
    attach={};order=[p.as_posix() for p in scenes];seen={norm(p) for p in order};queue=list(order);reference_rows=[]
    while queue:
        rel=queue.pop(0);path=root/rel
        if not path.exists():continue
        for ref in scene_references(path):
            target=resolve(ref['path'],catalog)
            row={'from':rel,'entity_id':ref['entity_id'],'entity':ref['entity'],'reference':ref['path'],'kind':ref['kind'],'resolved':target,'enabled':ref['enabled']}
            reference_rows.append(row)
            if not target:row['status']='unresolved';continue
            name=Path(target).name.lower()
            if any(x in name for x in EXCLUDE_HINTS):row['status']='excluded';continue
            # Streamed puzzle/event scenes come in an initial and a "_comp"
            # (completed) state; export the initial state only, never both.
            if ref['kind']=='streamed' and re.search(r'_comp(_perm)?\.scene_baked$',name):row['status']='excluded_completed_state';continue
            row['status']='attached'
            if norm(target) not in attach:attach[norm(target)]={'scene':rel,'entity_id':ref['entity_id'],'enabled':ref['enabled']}
            if norm(target) not in seen:
                seen.add(norm(target));order.append(target);queue.append(target)
                extract([target],'scene')
    scenes=[Path(p) for p in order]
    scene_rows=[];lights=[];counts=Counter();needed_models=set();needed_gsc=set()
    for p in scenes:
        s,c,ll=parse_scene(root/p.as_posix(),root,catalog);scene_rows.append(s);counts.update(c);lights.extend(ll)
        link=attach.get(norm(p.as_posix()))
        if link and norm(link['scene'])!=norm(p.as_posix()):s['attach']=link
        for ent in s['entities']:
            for model in ent['render_models']:
                if model.get('model') and model['status'] in ('available','authored_hidden'):needed_models.add(model['model'])
            for obj in ent.get('gsc_objects',[]):
                if obj.get('gsc'):needed_gsc.add(obj['gsc'])
            for fol in ent.get('foliage',[]):
                if fol.get('model_resolved'):needed_models.add(fol['model_resolved'])
    # Painted foliage: read each component's instance matrices.
    foliage_files=sorted({f['foliage_resolved'] for sc in scene_rows for e in sc['entities'] for f in e.get('foliage',[]) if f.get('foliage_resolved')})
    extract(foliage_files,'foliage')
    foliage_rows=[]
    for sc in scene_rows:
        for e in sc['entities']:
            for fol in e.get('foliage',[]):
                try:
                    if not fol.get('foliage_resolved') or not fol.get('model_resolved'):raise ValueError('foliage file or model not in archive')
                    fol['instances']=read_foliage_instances((root/fol['foliage_resolved']).read_bytes());fol['status']='available'
                except Exception as ex:fol['status']='unresolved';fol['reason']=str(ex);fol['instances']=[]
                foliage_rows.append({'entity':e['name'],'model':fol.get('model'),'instances':len(fol['instances']),'status':fol['status'],'reason':fol.get('reason')})
    extract(needed_models,'model')
    # Per-instance vertex colour paint files named by render components.
    extract({m['instance_vertex_colours'] for sc in scene_rows for e in sc['entities'] for m in e['render_models'] if m.get('instance_vertex_colours')},'instance-colours')
    # Terrain: extract each landscape resource folder and its material, then
    # write heights/holes and per-layer textures for the Blender builder.
    landscape_rows=[]
    for scene in scene_rows:
        for ent in scene['entities']:
            for land in ent.get('landscapes',[]):
                if land.get('status')=='unresolved':landscape_rows.append({'entity':ent['name'],**land});continue
                land_folder=land['folder'].lower()
                files=[v for k,v in catalog.items() if k.startswith(land_folder+'/')]
                mat=resolve(land['material'],catalog) if land.get('material') else None
                extract(files+([mat] if mat else []),'landscape')
                local=root/catalog.get(land_folder+'/landscape.landscape_data',land['landscape_data']).rsplit('/',1)[0]
                try:
                    land['prepared']=prepare_landscape(land,local,(root/mat).read_bytes() if mat and (root/mat).exists() else None,out/'terrain'/land['guid'],convert)
                    land['status']='available'
                except Exception as ex:land['status']='unresolved';land['reason']=str(ex)
                landscape_rows.append({'entity':ent['name'],'scene':scene['path'],'guid':land.get('guid'),'status':land['status'],'reason':land.get('reason')})
    texture_sets={}
    for rel in needed_gsc:
        t=resolve(gsc_texture_set_path(rel),catalog)
        if t:texture_sets[rel]=t
    extract(set(needed_gsc)|set(texture_sets.values()),'gsc')
    gsc_audits={};gsc_failures=[]
    for rel in sorted(needed_gsc):
        source=root/rel
        if not source.exists():
            gsc_failures.append({'resource':rel,'reason':'GSC resource was indexed but not extracted'});continue
        tset=root/texture_sets[rel] if rel in texture_sets else source.with_suffix('.nxg_textures')
        try:gsc_audits[norm(rel)]={'resource':rel,**audit_gsc(source,tset,out/'textures'/'gsc_embedded')}
        except Exception as ex:gsc_failures.append({'resource':rel,'reason':str(ex)})
    gsc_index={key:value for key,value in gsc_audits.items()}
    gsc_placement_total=0
    for scene in scene_rows:
        for ent in scene['entities']:
            for obj in ent.get('gsc_objects',[]):
                gsc_placement_total+=1
                key=norm(obj.get('gsc') or '')
                audit=gsc_index.get(key)
                if obj.get('status')!='geometry_unsupported':continue
                if not audit:
                    obj['status']='unresolved';obj['reason']='GSC resource failed to decode; see gsc_failures';continue
                matches=[n for n in audit['nodes'] if n['name']==obj['source_group']]
                if len(matches)!=1:
                    obj['status']='unresolved';obj['reason']=f'GSC node {obj["source_group"]!r} found {len(matches)} times';continue
                node=matches[0]
                obj['gsc_node']=node['name']
                if not node['draws']:
                    obj['status']='no_geometry';obj['reason']='Named GSC node is a locator without display geometry'
                else:
                    obj['status']='available'
    scene_paths=[norm(p.as_posix()) for p in scenes]
    to_extract=set()
    for p in scene_paths:
        r=resolve(p,catalog)
        if r:to_extract.add(r)
    to_extract.update(needed_models)
    # Material graph and texture dependencies are discovered from each model's native material table.
    material_refs=set();model_failures=[]
    for rel in sorted(needed_models):
        p=root/rel
        if not p.exists():continue
        try:
            for sub in read_model(p.read_bytes(),False):material_refs.update(m['path'] for m in sub['materials'])
        except Exception as ex:model_failures.append({'model':rel,'reason':str(ex)})
    # Decal and procedural-shape materials (and their override textures).
    texture_refs_extra=set()
    for scene in scene_rows:
        for ent in scene['entities']:
            for d in ent.get('decals',[])+[p for p in ent.get('procedural',[]) if p['role']!='collision_or_editor']:
                if d.get('material'):material_refs.add(d['material'])
            for p in ent.get('procedural',[]):
                for o in p.get('overrides',[]):
                    if o.get('texture'):texture_refs_extra.add(o['texture'])
    resolved_mats={}
    for ref in material_refs:
        p=resolve(ref,catalog)
        if p:resolved_mats[norm(ref)]=p;to_extract.add(p)
    extract(resolved_mats.values(),'material')
    # Extraction is exact-path filtered and does not modify the source archives.
    extract(to_extract,'model')
    graphs=[];texture_refs=set();material_missing=[]
    for ref,p in sorted(resolved_mats.items()):
        g,tex=material_graph(root/p,ref)
        if g:graphs.append(g);texture_refs.update(tex)
        else:material_missing.append(ref)
    texture_refs.update(texture_refs_extra)
    # Per-instance texture overrides on render components.
    for scene in scene_rows:
        for ent in scene['entities']:
            for model in ent['render_models']:
                for o in model.get('overrides',[]):
                    if o.get('texture'):texture_refs.add(o['texture'])
    # GSC material texture slots reference shared external .TEX resources.
    for audit in gsc_audits.values():texture_refs.update(audit.get('external_textures',[]))
    # The visible sky is the skydome render model's texture override.
    sky_candidates=[]
    for scene in scene_rows:
        for ent in scene['entities']:
            for model in ent['render_models']:
                src=(model.get('source_path') or '').lower()
                skies=[t for t in model.get('texture_overrides',[]) if '/skies/' in t.lower()]
                if 'shared_assets/skies/skydomebase' in src and skies:
                    sky_candidates.append({'scene':scene['path'],'entity_guid':ent['guid'],'entity':ent['name'],'model':model.get('model'),'texture':skies[0],'all_overrides':skies,'enabled':ent['enabled'] and model.get('enabled',True)})
    sky=None
    if sky_candidates:
        sky=sorted(sky_candidates,key=lambda c:not c['enabled'])[0]
        texture_refs.add(sky['texture'])
    resolved_tex={}
    for ref in texture_refs:
        p=resolve(ref,catalog)
        if p:resolved_tex[norm(ref)]=p;to_extract.add(p)
    extract(resolved_tex.values(),'texture')
    # Converted textures are shared by every level when a cache directory is
    # used: each archive texture is converted once and reused (sidecar JSON
    # holds the manifest entry, written last so a half-written file is never trusted).
    conversions=[];textures={};png_root=(cache/'converted_textures') if args.cache_dir else out/'textures'
    for ref,p in sorted(resolved_tex.items()):
        dest=png_root/(re.sub(r'[^A-Za-z0-9_.-]+','_',Path(p).as_posix())+'.png');dest.parent.mkdir(parents=True,exist_ok=True)
        if p.lower().endswith('.rendertarget'):
            # Live screens (TVs, terminals) the game renders at runtime: no stored image.
            textures[ref]={'render_target':True,'image':None,'image_key':'LSWTSS:'+ref,'color_space':'sRGB'}
            conversions.append({'source':ref,'status':'render_target'});continue
        side=dest.with_name(dest.name+'.json')
        try:
            if side.exists():
                cached=json.loads(side.read_text(encoding='utf-8'))
                if all(Path(x).exists() for x in [cached['entry']['image']]+cached['entry'].get('cube_faces',[])+cached['entry'].get('layers',[])):
                    textures[ref]={**cached['entry'],'image_key':'LSWTSS:'+ref};conversions.append({'source':ref,'status':'converted','cached':True,**cached['meta']});continue
            info=texture_info(root/p)
            if info['kind'] in (3,4) or info['format']==30:
                # Cube faces, array layers and HDR (BC6H) textures are written as
                # DDS, which Blender decodes natively (BC6H as float, unclipped).
                paths=[]
                for i in range(max(1,info['count']) if info['kind'] in (3,4) else 1):
                    dds=dest.with_name(dest.stem+(f'_{i}' if info['kind'] in (3,4) else '')+'.dds')
                    tmp=dds.with_name(dds.stem+f'.tmp{__import__("os").getpid()}.dds');meta=write_dds(root/p,tmp,i);__import__('os').replace(tmp,dds);paths.append(str(dds))
                usage=meta.get('native_usage',-1);hdr=info['format']==30
                entry={'image':paths[0],'color_space':'Linear Rec.709' if hdr else 'sRGB' if usage in (0,11,12) else 'Non-Color','hdr':hdr}
                if info['kind']==3:entry['cube_faces']=paths
                if info['kind']==4:entry['layers']=paths
            else:
                tmp=dest.with_name(dest.stem+f'.tmp{__import__("os").getpid()}.png');meta=convert(root/p,tmp);__import__('os').replace(tmp,dest);usage=meta.get('native_usage',-1)
                entry={'image':str(dest),'color_space':'sRGB' if usage in (0,11,12) else 'Non-Color'}
            side.write_text(json.dumps({'entry':entry,'meta':meta}),encoding='utf-8')
            textures[ref]={**entry,'image_key':'LSWTSS:'+ref};conversions.append({'source':ref,'status':'converted',**meta})
        except Exception as ex:conversions.append({'source':ref,'status':'failed','reason':str(ex)})
    # Skip graph image references that failed native texture conversion; the material compiler reports them.
    usable={k:v for k,v in textures.items()}
    material_manifest={'graphs':graphs,'textures':usable,'missing':[]}
    manifest={'format':'lswtss-native-hierarchy-preview-2','complete_map':False,'map':folder.name,'asset_root':str(root),'coordinate_mapping':'game (x,y,z) to Blender (-x,-z,y), reverse mesh winding','counts':dict(counts),'scenes':scene_rows,'gsc_audits':gsc_audits,'gsc_failures':gsc_failures,'sky':sky,'sky_candidates':sky_candidates,'limitations':['All non-excluded baked scene layers in the selected location folder are read; runtime streamed cells, builder/GSC-only geometry, gameplay states and destructibles are not reconstructed.','GSC placements are imported from each named node: LOD0 display group draws, LTMU material texture slots and .nxg_textures sets. Node transforms are applied as model space; GSC animation/state logic is not evaluated.','Unknown native scene records remain in the import diagnostics.','Native materials are compiled from material graph links; unsupported shader nodes are reported in the Blender text report.','Authored light records are imported where the selected scene includes supported native light classes; physical light power and specialized falloff may need calibration.']}
    (out/'hierarchy_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    (out/'material_texture_manifest.json').write_text(json.dumps(material_manifest,indent=2),encoding='utf-8')
    (out/'light_manifest.json').write_text(json.dumps({'complete_lighting':False,'scenes':scene_rows,'lights':lights},indent=2),encoding='utf-8')
    report={'level':folder.name,'selected_scene_layers':scene_paths,'scene_references':reference_rows,'landscapes':landscape_rows,'foliage':{'components':len(foliage_rows),'instances':sum(r['instances'] for r in foliage_rows),'unresolved':[r for r in foliage_rows if r['status']!='available']},'scene_entities':counts['entities'],'render_records':sum(counts[k] for k in ('available','authored_hidden','render_proxy','needs_extraction','unresolved')),'gsc_placements':gsc_placement_total,'gsc_resources':len(needed_gsc),'gsc_records':sum(v['record_count'] for v in gsc_audits.values()),'gsc_nodes':sum(len(v['nodes']) for v in gsc_audits.values()),'gsc_placement_status':dict(Counter(o['status'] for o in [o for sc in scene_rows for e in sc['entities'] for o in e.get('gsc_objects',[])])),'gsc_embedded_textures':sum(len(v['embedded_textures']) for v in gsc_audits.values()),'gsc_external_textures':sorted({t for v in gsc_audits.values() for t in v['external_textures']}),'gsc_failures':gsc_failures,'gsc_unresolved_reasons':Counter((o.get('reason') or '?')[:120] for sc in scene_rows for e in sc['entities'] for o in e.get('gsc_objects',[]) if o.get('status')=='unresolved').most_common(10),'sky':sky,'sky_texture_converted':bool(sky and norm(sky['texture']) in textures),'models':len(needed_models),'materials':len(material_refs),'translated_graph_inputs':len(graphs),'textures_referenced':len(resolved_tex),'textures_converted':sum(r['status']=='converted' for r in conversions),'texture_failures':[r for r in conversions if r['status']!='converted'],'authored_light_records':len(lights),'material_parse_failures':material_missing,'model_parse_failures':model_failures,'complete_map':False,'limitations':manifest['limitations']}
    (out/'prepare_report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()

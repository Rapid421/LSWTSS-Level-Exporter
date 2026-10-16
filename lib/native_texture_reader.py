"""Read observed standalone PC RTXT textures; preserve original mip payload in DDS."""
from pathlib import Path
import struct,io
from PIL import Image
FORMATS={1:('DXT1',8),2:('DXT1',8),6:('DXT5',16),30:('DX10',16),31:('DX10',16),37:('ATI1',8)}
# DXGI formats for DX10-headed payloads: 30 is BC6H (unsigned half float), 31 is BC7.
DXGI={30:95,31:98}
def convert(source,destination,slice_index=0):
 d=Path(source).read_bytes()
 # Some revision-201 GSC LTMU tables reference legacy standalone .TEX files
 # whose payload is already a complete DDS rather than an RTXT wrapper.
 if d.startswith(b'DDS '):
  im=Image.open(io.BytesIO(d));im.load();im.save(destination)
  return {'width':im.width,'height':im.height,'format':'DDS','version':None,'pixel_offset':0,'bit_depth':8,'native_usage':-1}
 o=d.find(b'.CC4HSERRTXT')-4
 if o<0:raise ValueError('No RTXT chunk')
 size=struct.unpack_from('>I',d,o)[0]
 if o+4+size!=len(d):raise ValueError('RTXT does not terminate file')
 v,w,h,depth,mips,array,fmt,reserved=struct.unpack_from('>8I',d,o+16)
 # Revision 12 stores the format as one byte at the start of the format word.
 if v==12:fmt=d[o+40]
 if depth!=1 and array==1:raise ValueError(f'Volume texture unsupported: depth={depth}, array={array}')
 is_array=array>1
 if v not in (12,14,15,16,17,18,19,20):raise ValueError(f'Unknown RTXT version {v}')
 if fmt==36:
  payload=sum(max(1,w>>i)*max(1,h>>i)*2 for i in range(mips));start=len(d)-payload
  im=Image.frombytes('I;16',(w,h),d[start:start+w*h*2]);im.save(destination)
  return {'width':w,'height':h,'format':fmt,'version':v,'pixel_offset':start,'bit_depth':16}
 if fmt not in FORMATS:raise ValueError(f'Unsupported texture format {fmt}')
 fourcc,block=FORMATS[fmt];payload=sum(max(1,(max(1,w>>i)+3)//4)*max(1,(max(1,h>>i)+3)//4)*block for i in range(mips))
 layers=depth if is_array and depth>1 else max(1,array)
 start=len(d)-payload*layers;rel=start-o
 # Fully validated standalone metadata layouts. Do not silently shift texture blocks.
 expected={12:65,14:65,15:81,16:82,17:86,18:90,19:90,20:92}[v]
 if rel!=expected:raise ValueError(f'Unexpected pixel offset {rel}, expected {expected}')
 if is_array:
  # Keep the selected native array layer and each of its complete mip levels.
  if not 0<=slice_index<layers:raise ValueError(f'Array slice {slice_index} outside 0..{layers-1}')
  sizes=[max(1,(max(1,w>>i)+3)//4)*max(1,(max(1,h>>i)+3)//4)*block for i in range(mips)]
  layer_size=sum(sizes);base=start+slice_index*layer_size
  payload_bytes=b''.join(d[base+sum(sizes[:i]):base+sum(sizes[:i+1])] for i in range(mips))
 else:payload_bytes=d[start:]
 fields=[124,0x000A1007,h,w,max(1,(w+3)//4)*max(1,(h+3)//4)*block,0,mips]+[0]*11
 fields += [32,4,int.from_bytes(fourcc.encode(),'little'),0,0,0,0,0,0x401008,0,0,0,0]
 header=b'DDS '+struct.pack('<31I',*fields)
 if fmt in DXGI:header+=struct.pack('<5I',DXGI[fmt],3,0,1,0) # DX10 header, Texture2D
 im=Image.open(io.BytesIO(header+payload_bytes));im.load();im.save(destination)
 semantic=struct.unpack_from('>I',d,o+78)[0]
 return {'width':w,'height':h,'format':fmt,'version':v,'pixel_offset':start,'bit_depth':8,'native_usage':semantic}
def texture_info(source):
 """RTXT header: kind 1 = 2D, 3 = cube (count = 6 faces), 4 = 2D array (count = layers)."""
 d=Path(source).read_bytes();o=d.find(b'.CC4HSERRTXT')-4
 v,w,h,count,mips,kind,fmt,_=struct.unpack_from('>8I',d,o+16)
 if v==12:fmt=d[o+40]
 return {'version':v,'width':w,'height':h,'count':count,'mips':mips,'kind':kind,'format':fmt}

def write_dds(source,destination,layer=0):
 """Write one layer/face (all mips) as a DX10 DDS. Blender decodes BC6H to
 float through this path, keeping HDR values that an 8-bit PNG would clip."""
 d=Path(source).read_bytes();o=d.find(b'.CC4HSERRTXT')-4
 v,w,h,count,mips,kind,fmt,_=struct.unpack_from('>8I',d,o+16)
 if v==12:fmt=d[o+40]
 if fmt not in FORMATS:raise ValueError(f'Unsupported texture format {fmt}')
 fourcc,block=FORMATS[fmt]
 sizes=[max(1,(max(1,w>>i)+3)//4)*max(1,(max(1,h>>i)+3)//4)*block for i in range(mips)]
 layers=max(1,count) if kind in (3,4) else 1
 per=sum(sizes);start=len(d)-per*layers
 if not 0<=layer<layers:raise ValueError(f'Layer {layer} outside 0..{layers-1}')
 fields=[124,0x000A1007,h,w,sizes[0],0,mips]+[0]*11
 fields+=[32,4,int.from_bytes(fourcc.encode(),'little'),0,0,0,0,0,0x401008,0,0,0,0]
 header=b'DDS '+struct.pack('<31I',*fields)
 if fmt in DXGI:header+=struct.pack('<5I',DXGI[fmt],3,0,1,0)
 Path(destination).write_bytes(header+d[start+layer*per:start+(layer+1)*per])
 return {'width':w,'height':h,'format':fmt,'version':v,'layers':layers,'kind':kind,'native_usage':struct.unpack_from('>I',d,o+78)[0]}

if __name__=='__main__':
 import json
 audit=json.loads(Path('work/material_dependency_audit.json').read_text());root=Path('work/utapau/files');out=Path('work/texture_png');rows=[]
 paths=sorted({t['resolved'] for g in audit['graphs'] for t in g['textures'] if t['resolved']})
 for resource in paths:
  dest=out/Path(resource).with_suffix('.png');dest.parent.mkdir(parents=True,exist_ok=True)
  try: row={'source':resource,'image':str(dest.resolve()),'status':'converted',**convert(root/resource,dest)}
  except Exception as e:row={'source':resource,'status':'failed','reason':str(e)}
  rows.append(row)
 Path('work/texture_conversion_report.json').write_text(json.dumps(rows,indent=2))
 print('Textures',len(rows),'converted',sum(r['status']=='converted' for r in rows));print('Failures',[r for r in rows if r['status']=='failed'])

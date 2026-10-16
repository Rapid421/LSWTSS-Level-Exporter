"""Translate explicit native material links. Unsupported shader nodes are reported."""
import json,re
from pathlib import Path
import bpy

def number(text,default=0):
 if text is None or str(text).strip()=='':return default
 if text.lower() in ('true','false'):return float(text.lower()=='true')
 if text.startswith('float'):
  vals=[float(x) for x in text[text.index('(')+1:text.rindex(')')].split(',')]
  return tuple((vals+[0,0,0])[:3])
 return float(text)

# Stand-alone blend nodes use the same formulas as the Colour Blends enum
# (Overlay: ab_rubblemound, Soft Light: twiggybushtrunk, Hard Light: bg_mountain DXBC).
BLEND_NODES={'Colour Burn Blend':0,'Darken Blend':1,'Colour Dodge Blend':3,'Hard Light Blend':5,'Lighten Blend':6,
             'Linear Light Blend':9,'Overlay Blend':10,'Screen Blend':12,'Soft Light Blend':13,'Colour Blend':14}

# Output inputs that act in the vertex/depth stage: the static mesh is the rest pose.
VERTEX_STAGE_INPUTS={'Position Offset':'Position Offset: wind/sway vertex animation not applied (static rest pose)',
                     'Position':'Position: vertex position replacement (camera-facing billboards) not applied; authored mesh pose kept',
                     'Tessellation Amount':'Tessellation Amount: GPU tessellation detail not applied (base mesh kept)',
                     'Z Bias':'Z Bias: depth bias only affects draw order',
                     'Transform':'Transform: per-vertex scale/rotation animation not applied (rest pose)',
                     'Clip Test':'Clip Test: screen clipping test not applied',
                     'Translucency':'Translucency: written to G-buffer o3.x (default 1 when unconnected, kashyyyk_leaves/clifftree_foliage DXBC); the deferred lighting response is not reproduced'}
LANDSCAPE_NODES={'Landscape','Landscape 4 Layer','sampleTextureLandscape','Landscape Blend','Landscape Texture','Landscape Channels','Landscape Params','Landscape Parallax','Index With Strongest Weight'}
OUTPUT_NODES=('StandardMaterial','ProjectedDecal','ParticleMaterial','CustomEffectMaterial')

class Compiler:
 def __init__(self,mat,graph,textures):
  self.mat=mat;self.graph=graph;self.textures=textures;self.tree=mat.node_tree;self.nodes=self.tree.nodes;self.links=self.tree.links
  self.defs={n['name']:n for n in graph['nodes']};self.incoming={(l['nodeTo'],l['socketTo']):(l['nodeFrom'],l['socketFrom']) for l in graph['links']}
  self.cache={};self.errors=set();self.reached=set();self.loaded=set();self.count=0;self.w_of={};self.notes=set()
  # Per-placement overrides (ApiRenderModel): {(graph node, socket): entry}.
  self.overrides={(o['node'],o['socket']):o for o in json.loads(mat.get('lswtss_overrides') or '[]')}
 def node(self,typ,label):
  n=self.nodes.new(typ);n.label=label;n.location=(-250*(self.count%8),200*(self.count//8));self.count+=1;return n
 def wire(self,value,socket):
  if hasattr(value,'node'):self.links.new(value,socket)
  elif isinstance(value,tuple):
   value=tuple(value[:3])+(0,)*(3-len(value[:3]))
   # HLSL implicit truncation: a vector constant into a scalar input keeps .x.
   socket.default_value=(*value,1) if socket.type=='RGBA' else value[0] if socket.type=='VALUE' else value
  else:socket.default_value=(value,value,value,1) if socket.type=='RGBA' else (value,value,value) if socket.type=='VECTOR' else value
 def vector(self,x):return isinstance(x,tuple) or (hasattr(x,'type') and x.type in ('VECTOR','RGBA'))
 def calc(self,op,a,b=None):
  vector=self.vector(a) or self.vector(b)
  if not vector and op in ('DISTANCE','DOT_PRODUCT','LENGTH'):
   # Scalar forms of the vector intrinsics: distance=|a-b|, dot=a*b, length=|a|.
   if op=='DISTANCE':return self.calc('ABSOLUTE',self.calc('SUBTRACT',a,b))
   if op=='DOT_PRODUCT':return self.calc('MULTIPLY',a,b)
   return self.calc('ABSOLUTE',a)
  if vector and op in ('MINIMUM','MAXIMUM','POWER'):
   def channel(x,c):return self.separate(x,c) if self.vector(x) else x
   return self.combine(*(self.calc(op,channel(a,c),channel(b,c)) for c in 'XYZ'))
  if vector and op in ('ADD','SUBTRACT','MULTIPLY','DIVIDE','NORMALIZE','DOT_PRODUCT','DISTANCE','LENGTH'):
   n=self.node('ShaderNodeVectorMath',op);n.operation=op
   for x,s in zip((a,b),n.inputs):
    if x is not None:self.wire(x if self.vector(x) else (x,x,x) if not hasattr(x,'node') else x,s)
   return n.outputs['Value' if op in ('DOT_PRODUCT','DISTANCE','LENGTH') else 'Vector']
  n=self.node('ShaderNodeMath',op);n.operation=op;self.wire(a,n.inputs[0])
  if b is not None:self.wire(b,n.inputs[1])
  return n.outputs[0]
 def unary(self,op,a):
  """Component-wise single-input math (HLSL intrinsics act per channel)."""
  if self.vector(a):return self.combine(*(self.calc(op,self.separate(a,c)) for c in 'XYZ'))
  return self.calc(op,a)
 def binary(self,op,a,b):
  if self.vector(a) or self.vector(b):
   ch=lambda x,c:self.separate(x,c) if self.vector(x) else x
   return self.combine(*(self.calc(op,ch(a,c),ch(b,c)) for c in 'XYZ'))
  return self.calc(op,a,b)
 def blend(self,mode,a,b,name):
  """Colour Blends 'Blend Type' (alphabetical enum), each verified in DXBC:
  0 burn (cantobight_woodendoor), 1 darken (wampa_cave_shader), 2 difference
  (cloudlightanim), 3 dodge (naboolightsmat), 5 hard light (sn_scrollingclouds),
  7 linear burn (bespin_window_01), 8 linear dodge (reliefs 01), 9 linear light
  (planetdiscoballlights), 10 overlay (toschestation_floor_lod1), 12 screen
  (rendertarget_emmisive_podracer), 13 soft light (toschestation_lod2),
  14 colour (kashyyyk_leaves). 4 exclusion, 6 lighten, 11 pin light follow the
  same alphabetical order (no surviving compiled instance)."""
  c=self.calc;one_minus=lambda x:c('SUBTRACT',1,x)
  if mode==0:  # B==0 ? 0 : saturate(1-(1-A)/B)
   r=self.binary('MINIMUM',self.binary('MAXIMUM',c('SUBTRACT',1,self.binary('DIVIDE',one_minus(a),b)),0),1)
   return c('MULTIPLY',r,self.binary('GREATER_THAN',self.unary('ABSOLUTE',b),0))
  if mode==1:return c('MINIMUM',a,b)
  if mode==2:return self.unary('ABSOLUTE',c('SUBTRACT',a,b))
  if mode==3:  # B==1 ? 1 : saturate(A/(1-B))
   r=self.binary('MINIMUM',self.binary('MAXIMUM',self.binary('DIVIDE',a,one_minus(b)),0),1)
   return self.select(self.binary('GREATER_THAN',b,.99999),1,r)
  if mode==4:return c('SUBTRACT',c('ADD',a,b),c('MULTIPLY',c('MULTIPLY',a,b),2))
  if mode in (5,10):  # hard light keys on A, overlay on B: key>0.5 ? 1-2(1-A)(1-B) : 2AB
   key=a if mode==5 else b
   return self.select(self.binary('GREATER_THAN',key,.5),one_minus(c('MULTIPLY',c('MULTIPLY',one_minus(a),one_minus(b)),2)),c('MULTIPLY',c('MULTIPLY',a,b),2))
  if mode==6:return c('MAXIMUM',a,b)
  if mode==7:return c('MAXIMUM',c('SUBTRACT',c('ADD',a,b),1),0)
  if mode==8:return c('ADD',a,b)
  if mode==9:return c('MAXIMUM',c('SUBTRACT',c('ADD',a,c('MULTIPLY',b,2)),1),0)
  if mode==11:
   b2=c('MULTIPLY',b,2)
   return self.select(self.binary('GREATER_THAN',b,.5),c('MAXIMUM',a,c('SUBTRACT',b2,1)),c('MINIMUM',a,b2))
  if mode==12:return one_minus(c('MULTIPLY',one_minus(a),one_minus(b)))
  if mode==13:  # B>0.5 ? 1-(1-A)(1-(B-0.5)) : A(B+0.5)
   return self.select(self.binary('GREATER_THAN',b,.5),one_minus(c('MULTIPLY',one_minus(a),c('SUBTRACT',1.5,b))),c('MULTIPLY',a,c('ADD',b,.5)))
  if mode==14:  # HSL colour: hue+saturation of B, HSL lightness of A
   sa=self.node('ShaderNodeSeparateColor',name+' A');sa.mode='HSL';self.wire(a,sa.inputs[0])
   sb=self.node('ShaderNodeSeparateColor',name+' B');sb.mode='HSL';self.wire(b,sb.inputs[0])
   cc=self.node('ShaderNodeCombineColor',name);cc.mode='HSL'
   self.links.new(sb.outputs[0],cc.inputs[0]);self.links.new(sb.outputs[1],cc.inputs[1]);self.links.new(sa.outputs[2],cc.inputs[2]);return cc.outputs[0]
  self.errors.add('Colour blend type %d unknown; A kept'%mode);return a
 def output_world_normal(self,label):
  nd=self.node('ShaderNodeNewGeometry',label+' normal');return self.to_native(nd.outputs['Normal'])
 def image_node(self,path,entry,label):
  key='LSWTSS:'+path
  im=bpy.data.images.get(key)
  if im is None:
   im=bpy.data.images.load(path,check_existing=False);im.name=key
   try:im.colorspace_settings.name=entry.get('color_space','sRGB')
   except TypeError:pass
   im.pack()
  tex=self.node('ShaderNodeTexImage',label);tex.image=im;self.loaded.add(path);return tex
 def select(self,cond,a,b):
  """cond ? a : b, with cond 0/1 (scalar or per channel)."""
  return self.calc('ADD',b,self.calc('MULTIPLY',cond,self.calc('SUBTRACT',a,b)))
 def to_native(self,v):
  """Blender world/object axes -> native (x,y,z) = (-bx, bz, -by)."""
  return self.combine(self.calc('MULTIPLY',self.separate(v,'X'),-1),self.separate(v,'Z'),self.calc('MULTIPLY',self.separate(v,'Y'),-1))
 def to_blender(self,v):
  """Native axes -> Blender: (bx,by,bz) = (-x, -z, y)."""
  return self.combine(self.calc('MULTIPLY',self.separate(v,'X'),-1),self.calc('MULTIPLY',self.separate(v,'Z'),-1),self.separate(v,'Y'))
 def srgb(self,a,encode):
  """Exact piecewise sRGB transfer, per channel."""
  def one(x):
   if encode:
    lo=self.calc('MULTIPLY',x,12.92);hi=self.calc('SUBTRACT',self.calc('MULTIPLY',self.calc('POWER',self.calc('MAXIMUM',x,0),1/2.4),1.055),.055);k=self.calc('GREATER_THAN',x,.0031308)
   else:
    lo=self.calc('DIVIDE',x,12.92);hi=self.calc('POWER',self.calc('DIVIDE',self.calc('ADD',self.calc('MAXIMUM',x,0),.055),1.055),2.4);k=self.calc('GREATER_THAN',x,.04045)
   return self.calc('ADD',lo,self.calc('MULTIPLY',k,self.calc('SUBTRACT',hi,lo)))
  return self.combine(*(one(self.separate(a,c)) for c in 'XYZ')) if self.vector(a) else one(a)
 def separate(self,value,channel):
  if isinstance(value,tuple):return value['XYZRGB'.index(channel)%3]
  n=self.node('ShaderNodeSeparateXYZ',channel);self.wire(value,n.inputs[0]);return n.outputs['XYZ'['RGB'.index(channel)] if channel in 'RGB' else channel]
 def combine(self,x,y,z=0):
  n=self.node('ShaderNodeCombineXYZ','Combine');
  for v,s in zip((x,y,z),n.inputs):self.wire(v,s)
  return n.outputs[0]
 def input(self,name,socket,default=0):
  key=(name,socket)
  if key in self.incoming and self.incoming[key][0] in self.defs:return self.output(*self.incoming[key])
  if key in self.incoming:self.notes.add('Source graph links a node that does not exist ('+self.incoming[key][0]+'); the socket default is used, as the game compiler does')
  return number(self.defs[name]['values'].get(socket),default)
 def uv(self,index=0):
  n=self.node('ShaderNodeUVMap','Native UV'+str(index));n.uv_map='UV'+str(index);return n.outputs['UV']
 def native_position(self):
  n=self.node('ShaderNodeNewGeometry','World position');p=n.outputs['Position']
  return self.combine(self.calc('MULTIPLY',self.separate(p,'X'),-1),self.separate(p,'Z'),self.calc('MULTIPLY',self.separate(p,'Y'),-1))
 def output(self,name,socket):
  key=(name,socket)
  if key in self.cache:return self.cache[key]
  self.reached.add(name);n=self.defs[name];t=n['type'];v=n['values'];inp=lambda s,d=0:self.input(name,s,d)
  result=0
  if t=='sampleTexture2D':
   imagekey=(name,'__image')
   if imagekey not in self.cache:
    ov=self.overrides.get((name,'tex'))
    path=((ov or {}).get('texture') or v.get('tex','')).replace('\\','/').lower();entry=self.textures.get(path)
    if entry and entry.get('render_target'):
     self.notes.add('Render target '+path+': live in-game screen, no stored image (black)');return (0.0,0.0,0.0) if socket not in ('A','Alpha','R','G','B') else 0.0
    if entry is None: self.errors.add('Missing texture: '+path);return 0
    im=bpy.data.images.get(entry['image_key'])
    if im is None:
     im=bpy.data.images.load(entry['image'],check_existing=False);im.name=entry['image_key'];im.colorspace_settings.name=entry['color_space'];im.pack()
    tex=self.node('ShaderNodeTexImage',name);tex.name=name;tex.image=im;self.loaded.add(path)
    uv=inp('UV',None) if (name,'UV') in self.incoming else self.uv()
    uv=self.combine(self.separate(uv,'X'),self.calc('SUBTRACT',1,self.separate(uv,'Y')))
    self.wire(uv,tex.inputs['Vector']);tex.extension='REPEAT';self.cache[imagekey]=tex
   tex=self.cache[imagekey]
   result=tex.outputs['Alpha'] if socket in ('A','Alpha') else self.separate(tex.outputs['Color'],socket) if socket in ('R','G','B') else tex.outputs['Color']
  elif t in ('constant1','parameter1','constant2','constant3','parameter3','parameter2','constant4','parameter4','instanced_parameter1','instanced_parameter2','instanced_parameter3','instanced_parameter4'):
   # instanced_parameterN: per-instance overrides are not stored in the material;
   # the material default is what an un-overridden instance draws.
   raw=v.get('A')
   ov=self.overrides.get((name,'A'))
   if ov and ov.get('value'):
    vals=list(ov['value'])
    raw='float%d(%s)'%(len(vals),','.join(repr(float(x)) for x in vals)) if len(vals)>1 else repr(float(vals[0]))
    v=dict(v,A=raw)
   if socket in ('X','Y','Z','W','x','y','z','w'):
    vals=[float(x) for x in raw[raw.index('(')+1:raw.rindex(')')].split(',')] if raw and raw.startswith('float') else [number(raw)]*4
    result=(vals+[0,0,0,0])['XYZW'.index(socket.upper())]
   elif raw is None or raw=='':
    # A constant with no stored value compiles as 1 (cloudlightanim clamp Max,
    # beaconmaterial Rotator Midpoint DXBC).
    result=(1.0,1.0,1.0) if t[-1] in '234' else 1.0
   else:
    result=number(raw) if ov else inp('A')
    if raw and raw.startswith('float4'):
     vals=[float(x) for x in raw[raw.index('(')+1:raw.rindex(')')].split(',')]
     if isinstance(result,tuple):result=tuple(result)  # fresh object so its id keys the w component
     self.w_of[id(result)]=vals[3]
  elif t in ('One','Zero','Half'):result={'One':1.0,'Zero':0.0,'Half':0.5}[t]
  elif t in ('saturate','abs','negate','floor','ceil','sqrt','sign','asin','acos','Length','Degrees to Radians','times half plus half','Linear to sRGB','sRGB to Linear'):
   a=inp('Val' if t=='sign' else 'Degrees' if t=='Degrees to Radians' else 'A')
   if t=='saturate':result=self.calc('MINIMUM',self.calc('MAXIMUM',a,0),1)
   elif t=='negate':result=self.calc('MULTIPLY',a,-1)
   elif t=='Length':result=self.calc('LENGTH',a) if self.vector(a) else self.unary('ABSOLUTE',a)
   elif t=='Degrees to Radians':result=self.calc('MULTIPLY',a,0.017453292519943295)
   elif t=='times half plus half':result=self.calc('ADD',self.calc('MULTIPLY',a,.5),.5)
   elif t in ('Linear to sRGB','sRGB to Linear'):result=self.srgb(a,t=='Linear to sRGB')
   else:result=self.unary({'abs':'ABSOLUTE','floor':'FLOOR','ceil':'CEIL','sqrt':'SQRT','sign':'SIGN','asin':'ARCSINE','acos':'ARCCOSINE'}[t],a)
  elif t in ('max','min'):result=self.calc('MAXIMUM' if t=='max' else 'MINIMUM',inp('A'),inp('B'))
  elif t=='mad':result=self.calc('ADD',self.calc('MULTIPLY',inp('A'),inp('B',1)),inp('C'))
  elif t=='modulo':result=self.binary('MODULO',inp('A'),inp('B',1))  # HLSL fmod: truncated, sign of A
  elif t=='Smoothstep':
   f=self.calc('MINIMUM',self.calc('MAXIMUM',self.calc('DIVIDE',self.calc('SUBTRACT',inp('X'),inp('A')),self.calc('SUBTRACT',inp('B',1),inp('A'))),0),1)
   result=self.calc('MULTIPLY',self.calc('MULTIPLY',f,f),self.calc('SUBTRACT',3,self.calc('MULTIPLY',f,2)))
  elif t=='Reflect':
   nd=self.node('ShaderNodeVectorMath',name);nd.operation='REFLECT';self.wire(inp('Incident'),nd.inputs[0]);self.wire(inp('Normal',(0,1,0)),nd.inputs[1]);result=nd.outputs['Vector']
  elif t=='float4':result=self.combine(inp('x'),inp('y'),inp('z'));self.w_of[id(result)]=inp('w')
  elif t=='splitfloat4':
   a=inp('I')
   result=self.w_of.get(id(a),1.0) if socket=='w' else self.separate(a,socket.upper()) if self.vector(a) else a
  elif t=='Mesh UV':
   uvm=self.calc('MULTIPLY',self.uv(int(float(v.get('Index') or 0))),(float(v.get('U Tiling') or 1),float(v.get('V Tiling') or 1),1))
   result=self.separate(uvm,'X') if socket=='U' else self.separate(uvm,'Y') if socket=='V' else uvm
  elif t=='Mesh Colour':
   nd=self.node('ShaderNodeVertexColor',name);nd.layer_name='Color'+str(int(float(v.get('Index',0))));result=nd.outputs['Alpha'] if socket=='A' else self.separate(nd.outputs['Color'],socket) if socket in ('R','G','B') else nd.outputs['Color']
  elif t in ('mul','add','divide','dot','distance'):result=self.calc({'mul':'MULTIPLY','add':'ADD','divide':'DIVIDE','dot':'DOT_PRODUCT','distance':'DISTANCE'}[t],inp('A'),inp('B',1 if t in ('mul','divide') else 0))
  elif t=='lerp':result=self.calc('ADD',inp('A'),self.calc('MULTIPLY',self.calc('SUBTRACT',inp('B'),inp('A')),inp('t')))
  elif t=='oneMinus':result=self.calc('SUBTRACT',1,inp('A'))
  elif t=='times 2 minus 1':result=self.calc('SUBTRACT',self.calc('MULTIPLY',inp('A'),2),1)
  elif t=='normalize':result=self.calc('NORMALIZE',inp('A'))
  elif t in ('sin','cos'):
   # vfx_holograms_legostud / bm_doorframetrim DXBC: sin(A*Frequency)*Magnitude.
   result=self.calc('MULTIPLY',self.unary('SINE' if t=='sin' else 'COSINE',self.calc('MULTIPLY',inp('A'),inp('Frequency',1))),inp('Magnitude',1))
  elif t in ('float2','float3'):result=self.combine(inp('x'),inp('y'),inp('z'))
  elif t=='splitfloat3':result=self.separate(inp('A'),socket.upper())
  elif t=='Expand NormalR':
   nr=inp('NormalR');r=self.separate(nr,'R');g=self.separate(nr,'G')
   if socket=='Roughness':result=self.separate(nr,'B')
   else:
    x=self.calc('SUBTRACT',self.calc('MULTIPLY',r,2),1);y=self.calc('SUBTRACT',self.calc('MULTIPLY',g,2),1)
    z=self.calc('SQRT',self.calc('MAXIMUM',self.calc('SUBTRACT',1,self.calc('ADD',self.calc('MULTIPLY',x,x),self.calc('MULTIPLY',y,y))),0))
    result=self.combine(r,g,self.calc('ADD',self.calc('MULTIPLY',z,.5),.5))
  elif t=='Expand Normal Map':
   # Megascans packs tangent-space X/Y into independent R/G sockets and
   # reconstructs positive Z. Preserve the B roughness channel separately.
   if socket in ('Roughness','Normal Map B'):result=inp('Normal Map B')
   else:
    r=inp('Normal Map R');g=inp('Normal Map G')
    x=self.calc('SUBTRACT',self.calc('MULTIPLY',r,2),1);y=self.calc('SUBTRACT',self.calc('MULTIPLY',g,2),1)
    z=self.calc('SQRT',self.calc('MAXIMUM',self.calc('SUBTRACT',1,self.calc('ADD',self.calc('MULTIPLY',x,x),self.calc('MULTIPLY',y,y))),0))
    result=self.combine(self.calc('ADD',self.calc('MULTIPLY',x,.5),.5),self.calc('ADD',self.calc('MULTIPLY',y,.5),.5),self.calc('ADD',self.calc('MULTIPLY',z,.5),.5))
  elif t in ('Remap','Dynamic Remap'):
   f=self.calc('DIVIDE',self.calc('SUBTRACT',inp('A'),inp('Min Input')),self.calc('SUBTRACT',inp('Max Input',1),inp('Min Input')))
   f=self.calc('MINIMUM',self.calc('MAXIMUM',f,0),1)
   mode=int(float(v.get('Interpolation') or 0))
   if mode==4:
    # ao_satellite_tile DXBC: Interpolation 4 = pow(f, Exponent).
    f=self.calc('POWER',f,inp('Exponent',1))
   else:
    # 1..3 = smoothstep applied that many times (white_glow_shader_gradient,
    # factory_fusebox_01_reduced, caverock DXBC); Exponent is unused (concretetile DXBC).
    for _ in range(mode):f=self.calc('MULTIPLY',self.calc('MULTIPLY',f,f),self.calc('SUBTRACT',3,self.calc('MULTIPLY',f,2)))
   result=self.calc('ADD',inp('Min Output'),self.calc('MULTIPLY',f,self.calc('SUBTRACT',inp('Max Output',1),inp('Min Output'))))
  elif t=='Height Blend':
   # ConcreteWalls DXBC: saturate((A + Offset - B)/(2*(1-Contrast)) + .5).
   contrast=inp('Contrast');offset=inp('Offset')
   q=self.calc('ADD',self.calc('DIVIDE',self.calc('SUBTRACT',self.calc('ADD',inp('A'),offset),inp('B')),self.calc('MULTIPLY',self.calc('SUBTRACT',1,contrast),2)),.5)
   result=self.calc('MINIMUM',self.calc('MAXIMUM',q,0),1)
  elif t=='Max Alpha Blend':
   # ConcreteWalls DXBC: pow(saturate(1-heightAlpha+vertexAlpha), Exponent).
   q=self.calc('ADD',self.calc('SUBTRACT',1,inp('Heightmap Alpha')),inp('Vertex Alpha'))
   q=self.calc('POWER',self.calc('MINIMUM',self.calc('MAXIMUM',q,0),1),inp('Exponent',1))
   result=self.calc('ADD',inp('Albedo A'),self.calc('MULTIPLY',self.calc('SUBTRACT',inp('Albedo B'),inp('Albedo A')),q))
  elif t=='Normal Map Intensity':
   nrm=self.calc('SUBTRACT',self.calc('MULTIPLY',inp('Normal'),2),(1,1,1));intensity=inp('Intensity',1)
   nrm=self.calc('NORMALIZE',self.calc('MULTIPLY',nrm,self.combine(intensity,intensity,1)))
   result=self.calc('ADD',self.calc('MULTIPLY',nrm,.5),(.5,.5,.5))
  elif t=='Detail Normal Blend':
   # AD_Rock_A DXBC uses reoriented normal blending, with base.z + 1.
   base=self.calc('ADD',self.calc('MULTIPLY',inp('Base Normal'),2),(-1,-1,0))
   detail=self.calc('ADD',self.calc('MULTIPLY',inp('Detail Normal'),(-2,-2,2)),(1,1,-1))
   nrm=self.calc('SUBTRACT',self.calc('DIVIDE',self.calc('MULTIPLY',base,self.calc('DOT_PRODUCT',base,detail)),self.separate(base,'Z')),detail)
   result=self.calc('ADD',self.calc('MULTIPLY',nrm,.5),(.5,.5,.5))
  elif t=='World Space UV Projection':
   p=self.native_position();axis=int(float(v.get('Projection Axis',1)))
   # kashyyyk_leaves / clifftree_boards DXBC: uv=(proj+Offset)*Tiling with
   # axis 0 proj=(-z,-y), axis 1 proj=(x,-z).
   if axis==0:u=self.calc('MULTIPLY',self.separate(p,'Z'),-1);vv=self.calc('MULTIPLY',self.separate(p,'Y'),-1)
   elif axis==1:u=self.separate(p,'X');vv=self.calc('MULTIPLY',self.separate(p,'Z'),-1)
   elif axis==2:u=self.separate(p,'X');vv=self.calc('MULTIPLY',self.separate(p,'Y'),-1)  # ab_coastalrockchunks_lod1 DXBC: (x,-y)
   else:
    # Custom axis: project onto the plane perpendicular to it (basis from the
    # axis and native up; X when parallel).
    ax_=number(v.get('Custom Axis'),(0,1,0));import math as _m
    ln=_m.sqrt(sum(c*c for c in ax_)) or 1;ax_=tuple(c/ln for c in ax_)
    ref=(0,1,0) if abs(ax_[1])<0.99 else (1,0,0)
    t_=(ref[1]*ax_[2]-ref[2]*ax_[1],ref[2]*ax_[0]-ref[0]*ax_[2],ref[0]*ax_[1]-ref[1]*ax_[0]);lt=_m.sqrt(sum(c*c for c in t_)) or 1;t_=tuple(c/lt for c in t_)
    b_=(ax_[1]*t_[2]-ax_[2]*t_[1],ax_[2]*t_[0]-ax_[0]*t_[2],ax_[0]*t_[1]-ax_[1]*t_[0])
    u=self.calc('DOT_PRODUCT',p,t_);vv=self.calc('MULTIPLY',self.calc('DOT_PRODUCT',p,b_),-1)
    self.notes.add('World Space UV Projection custom axis: planar basis around the authored axis (approximation)')
   result=self.calc('MULTIPLY',self.calc('ADD',self.combine(u,vv),inp('Offset',(0,0,0))),inp('Tiling',(1,1,1)))
  elif t=='Object Scale':
   def axis_len(component):
    tr=self.node('ShaderNodeVectorTransform',name);tr.vector_type='VECTOR';tr.convert_from='OBJECT';tr.convert_to='WORLD';self.wire(component,tr.inputs[0]);return self.calc('LENGTH',tr.outputs[0])
   axes={'X':(1,0,0),'Y':(0,0,1),'Z':(0,1,0)}
   result=axis_len(axes[socket]) if socket in axes else self.combine(*(axis_len(axes[c]) for c in 'XYZ'))
  elif t=='Over':
   alpha=inp('Alpha',1);result=self.calc('ADD',inp('Bottom'),self.calc('MULTIPLY',self.calc('SUBTRACT',inp('Top'),inp('Bottom')),alpha))  # bespin_exteriorfloortiles DXBC: lerp(Bottom,Top,Alpha), Alpha unclamped
  elif t=='Parallax':result=inp('UV',self.uv());self.notes.add('Parallax: grillefloor DXBC runs occlusion parallax only within 4 m of the camera; native UV is what the game shows beyond that')
  elif t=='Shader Mode':result=inp('Normal')  # light_emissive DXBC (base permutation): Normal branch
  elif t=='Panner':
   time=inp('Time');speed=self.combine(inp('Speed U'),inp('Speed V'));result=self.calc('ADD',inp('UV',self.uv()),self.calc('MULTIPLY',speed,time))
  elif t=='Colour Processing':
   # ConcreteFloor / clifftree_foliage DXBC: brightness first, then RGB->HSV,
   # h=frac(h+Hue), s=saturate(s*Saturation), HSV->RGB with V kept. Blender's
   # Hue/Saturation node is that exact operation with its hue input at Hue+0.5.
   rgb=self.calc('MULTIPLY',inp('Color'),inp('Brightness',1))
   nd=self.node('ShaderNodeHueSaturation',name);self.wire(self.calc('ADD',inp('Hue'),.5),nd.inputs['Hue']);self.wire(inp('Saturation',1),nd.inputs['Saturation'])
   nd.inputs['Value'].default_value=1;nd.inputs['Fac'].default_value=1;self.wire(rgb if self.vector(rgb) else (rgb,rgb,rgb),nd.inputs['Color']);result=nd.outputs['Color']
  elif t=='Colour Blends':result=self.blend(int(float(v.get('Blend Type',0))),inp('A'),inp('B'),name)
  elif t in BLEND_NODES:result=self.blend(BLEND_NODES[t],inp('A'),inp('B'),name)
  elif t=='Platform Specific':
   # The PC build compiles the PC DX11 branch (DX11 is the same family).
   src=next((s for s in ('PC DX11','DX11','Default') if (name,s) in self.incoming),None)
   if src is None:src=next((s for (n_,s) in self.incoming if n_==name),None)
   result=inp(src) if src else 0
  elif t=='Pass Through':result=inp('A')
  elif t in ('Random','Instance UID','Instance Index','Seeded Random'):
   # Per-instance values: Object Info's random stands in for the engine's instance hash.
   rnd=self.node('ShaderNodeObjectInfo',name).outputs['Random']
   if t=='Seeded Random':
    wn=self.node('ShaderNodeTexWhiteNoise',name+' hash');wn.noise_dimensions='1D'
    self.wire(self.calc('ADD',inp('Seed'),rnd),wn.inputs['W']);result=wn.outputs['Value']
   elif t=='Random' and socket=='2':result=self.calc('FRACT',self.calc('MULTIPLY',rnd,7.31))
   else:result=rnd
   self.notes.add(t+': per-instance hash approximated with Object Info Random')
  elif t=='Noise2D':
   nz=self.node('ShaderNodeTexNoise',name);nz.noise_dimensions='2D';nz.inputs['Scale'].default_value=1.0
   uvn=inp('UV',None) if (name,'UV') in self.incoming else self.uv()
   self.wire(self.calc('DIVIDE',uvn,64.0),nz.inputs['Vector'])
   result=self.calc('SUBTRACT',self.calc('MULTIPLY',nz.outputs['Fac'],2),1)
   self.notes.add('Noise2D: engine perlin texture approximated by Blender gradient noise')
  elif t=='Normal From Heightmap':
   entry=self.textures.get(v.get('tex','').replace('\\','/').lower())
   if not entry:self.errors.add('Missing texture: '+v.get('tex',''));return 0
   ch=int(number(v.get('Heightmap Channel'),0));k=number(v.get('Strength'),1)
   uvh=inp('UV',None) if (name,'UV') in self.incoming else self.uv()
   hs=[]
   for du,dv in ((0,0),(1/512,0),(0,1/512)):
    tex=self.image_node(entry['image'],entry,name)
    self.wire(self.combine(self.calc('ADD',self.separate(uvh,'X'),du),self.calc('SUBTRACT',1,self.calc('ADD',self.separate(uvh,'Y'),dv))),tex.inputs['Vector'])
    hs.append(tex.outputs['Alpha'] if ch==3 else self.separate(tex.outputs['Color'],'RGB'[min(ch,2)]))
   n_=self.calc('NORMALIZE',self.combine(self.calc('MULTIPLY',self.calc('SUBTRACT',hs[0],hs[1]),k*8),self.calc('MULTIPLY',self.calc('SUBTRACT',hs[0],hs[2]),k*8),1))
   result=self.calc('ADD',self.calc('MULTIPLY',n_,.5),(.5,.5,.5))
   self.notes.add('Normal From Heightmap: finite-difference slope (approximation of the engine filter)')
  elif t=='Fade Alpha':result=1.0;self.notes.add('Fade Alpha: camera-distance fade not applied (fully visible)')
  elif t=='HDR UI Intensity':result=1.0
  elif t=='any':
   a=inp('A');result=self.calc('GREATER_THAN',self.calc('LENGTH',a) if self.vector(a) else self.calc('ABSOLUTE',a),0)
  elif t=='Plane Equation':
   # Plane through Position A with normal towards Position B: (n, -n.A).
   a=inp('Position A');b=inp('Position B',(0,1,0));n_=self.calc('NORMALIZE',self.calc('SUBTRACT',b,a))
   result=n_;self.w_of[id(result)]=self.calc('MULTIPLY',self.calc('DOT_PRODUCT',n_,a),-1)
  elif t=='Cylindrical UV Projection':
   p=self.native_position() if number(v.get('Coordinate Space'),1)==1 else self.to_native(self.node('ShaderNodeTexCoord',name).outputs['Object'])
   ang=self.node('ShaderNodeMath',name);ang.operation='ARCTAN2';self.wire(self.separate(p,'Z'),ang.inputs[0]);self.wire(self.separate(p,'X'),ang.inputs[1])
   uvc=self.combine(self.calc('ADD',self.calc('DIVIDE',ang.outputs[0],6.283185307179586),.5),self.calc('MULTIPLY',self.separate(p,'Y'),-1))
   result=self.calc('MULTIPLY',self.calc('ADD',uvc,inp('Offset',(0,0,0))),inp('Tiling',(1,1,1)))
   self.notes.add('Cylindrical UV Projection: standard cylindrical mapping about native Y (approximation)')
  elif t in ('Back Buffer','Blurred Back Buffer','Composition Buffer','Refraction'):
   # Screen colour behind the surface: a path tracer has no such buffer; the
   # background is seen through the surface's own transparency instead.
   result=0.0 if socket in ('R','G','B','A') else (0.0,0.0,0.0)
   self.notes.add(t+': screen-space colour not available in Cycles; background shows through transparency (approximation)')
  elif t=='GBuffer':
   # Projection Normal = the receiving surface's normal; for a mesh decal that is its own normal.
   result=self.output_world_normal(name)
  elif t=='Depth Buffer':
   cam=self.node('ShaderNodeCameraData',name)
   result=cam.outputs['View Z Depth'] if socket=='Linear Depth' else self.native_position()
   self.notes.add('Depth Buffer: depth of the shaded surface itself (approximation)')
  elif t=='World Incident':
   # glass_low_draw DXBC: reflect(WorldIncident, N) needs the surface->camera direction.
   result=self.to_native(self.node('ShaderNodeNewGeometry',name).outputs['Incoming'])
  elif t=='View Space Position':
   # DirectX view space: x right, y up, +z forward (Blender camera space looks down -Z).
   vt=self.node('ShaderNodeVectorTransform',name);vt.vector_type='POINT';vt.convert_from='WORLD';vt.convert_to='CAMERA'
   self.links.new(self.node('ShaderNodeNewGeometry',name+' p').outputs['Position'],vt.inputs[0])
   p=self.combine(self.separate(vt.outputs[0],'X'),self.separate(vt.outputs[0],'Y'),self.calc('MULTIPLY',self.separate(vt.outputs[0],'Z'),-1))
   result=self.separate(p,'Z') if socket=='Z' else p
  elif t in ('View2World','View2Tangent'):
   # Direction transforms between DX view space and world/tangent space.
   vin=inp('Input',(0,0,1))
   vb=self.combine(self.separate(vin,'X') if self.vector(vin) else 0,self.separate(vin,'Y') if self.vector(vin) else 0,self.calc('MULTIPLY',self.separate(vin,'Z') if self.vector(vin) else 1,-1))
   vt=self.node('ShaderNodeVectorTransform',name);vt.vector_type='VECTOR';vt.convert_from='CAMERA';vt.convert_to='WORLD';self.wire(vb,vt.inputs[0])
   w=self.to_native(vt.outputs[0])
   if t=='View2World':result=w
   else:
    geo=self.node('ShaderNodeNewGeometry',name+' n');tan=self.node('ShaderNodeTangent',name+' t');tan.direction_type='UV_MAP';tan.uv_map='UV0'
    bit=self.node('ShaderNodeVectorMath',name+' b');bit.operation='CROSS_PRODUCT';self.links.new(geo.outputs['Normal'],bit.inputs[0]);self.links.new(tan.outputs['Tangent'],bit.inputs[1])
    wb=vt.outputs[0]
    result=self.combine(self.calc('DOT_PRODUCT',wb,tan.outputs['Tangent']),self.calc('DOT_PRODUCT',wb,bit.outputs['Vector']),self.calc('DOT_PRODUCT',wb,geo.outputs['Normal']))
  elif t=='sampleTextureCube':
   # D3D cube addressing on the native direction: the major axis picks the
   # face (+X,-X,+Y,-Y,+Z,-Z = faces 0..5); (sc,tc)/|ma| maps to [0,1] with t down.
   entry=self.textures.get(v.get('tex','').replace('\\','/').lower())
   if not entry or not entry.get('cube_faces'):self.errors.add('Missing cube texture: '+v.get('tex',''));return 0
   d=inp('UV',(0,0,1));x,y,z=(self.separate(d,c) for c in 'XYZ')
   ax,ay,az=(self.unary('ABSOLUTE',c) for c in (x,y,z))
   isx=self.calc('MULTIPLY',self.calc('GREATER_THAN',ax,ay),self.calc('SUBTRACT',1,self.calc('LESS_THAN',ax,az)))
   isy=self.calc('MULTIPLY',self.calc('SUBTRACT',1,isx),self.calc('SUBTRACT',1,self.calc('LESS_THAN',ay,az)))
   isz=self.calc('SUBTRACT',1,self.calc('ADD',isx,isy))
   faces=[(isx,x,(lambda:self.calc('MULTIPLY',z,-1)),(lambda:self.calc('MULTIPLY',y,-1)),ax,1),
          (isx,x,(lambda:z),(lambda:self.calc('MULTIPLY',y,-1)),ax,-1),
          (isy,y,(lambda:x),(lambda:z),ay,1),
          (isy,y,(lambda:x),(lambda:self.calc('MULTIPLY',z,-1)),ay,-1),
          (isz,z,(lambda:x),(lambda:self.calc('MULTIPLY',y,-1)),az,1),
          (isz,z,(lambda:self.calc('MULTIPLY',x,-1)),(lambda:self.calc('MULTIPLY',y,-1)),az,-1)]
   acc=None
   for i,(sel,comp,sc,tc,ma,sign) in enumerate(faces):
    w=self.calc('MULTIPLY',sel,self.calc('GREATER_THAN',self.calc('MULTIPLY',comp,sign),0))
    ma_=self.calc('MAXIMUM',ma,1e-6)
    u=self.calc('MULTIPLY',self.calc('ADD',self.calc('DIVIDE',sc(),ma_),1),.5)
    vv=self.calc('MULTIPLY',self.calc('ADD',self.calc('DIVIDE',tc(),ma_),1),.5)
    tex=self.image_node(entry['cube_faces'][i],entry,name+' face %d'%i)
    tex.extension='EXTEND';self.wire(self.combine(u,self.calc('SUBTRACT',1,vv)),tex.inputs['Vector'])
    term=self.calc('MULTIPLY',tex.outputs['Color'],w)
    acc=term if acc is None else self.calc('ADD',acc,term)
   result=self.separate(acc,socket) if socket in ('R','G','B') else acc
  elif t in ('Texture Array','ImageSequence'):
   # Layer chosen by Index / Frame Index (rounded), as in a texture2DArray fetch.
   entry=self.textures.get(v.get('tex','').replace('\\','/').lower())
   if not entry:self.errors.add('Missing texture: '+v.get('tex',''));return 0
   layers=entry.get('layers') or [entry['image']]
   idx=inp('Frame Index' if t=='ImageSequence' else 'Index',0)
   uv=inp('UV',None) if (name,'UV') in self.incoming else self.uv()
   uvf=self.combine(self.separate(uv,'X'),self.calc('SUBTRACT',1,self.separate(uv,'Y')))
   acc=None;alpha=None
   for i,path in enumerate(layers):
    tex=self.image_node(path,entry,name+' layer %d'%i);self.wire(uvf,tex.inputs['Vector'])
    w=self.calc('LESS_THAN',self.calc('ABSOLUTE',self.calc('SUBTRACT',self.calc('ROUND',idx),i)),.5) if len(layers)>1 else 1.0
    c_=self.calc('MULTIPLY',tex.outputs['Color'],w);a_=self.calc('MULTIPLY',tex.outputs['Alpha'],w)
    acc=c_ if acc is None else self.calc('ADD',acc,c_);alpha=a_ if alpha is None else self.calc('ADD',alpha,a_)
   result=alpha if socket=='A' else self.separate(acc,socket) if socket in ('R','G','B') else acc
  elif t=='Approximate Max Alpha Blend':
   # yavin_pyramidsteps DXBC: t = saturate((Vertex - Heightmap) * Exponent/1.5 + 1).
   k=self.calc('DIVIDE',inp('Exponent',50),1.5)
   f=self.calc('MINIMUM',self.calc('MAXIMUM',self.calc('ADD',self.calc('MULTIPLY',self.calc('SUBTRACT',inp('Vertex Alpha'),inp('Heightmap Alpha')),k),1),0),1)
   result=self.calc('ADD',inp('Albedo A'),self.calc('MULTIPLY',self.calc('SUBTRACT',inp('Albedo B'),inp('Albedo A')),f))
  elif t in ('Wind','Wind2D','Foliage Deformer'):
   # Vertex-stage sway: the rest pose has zero offset/deflection.
   result=(0.0,0.0,0.0) if socket in ('Pos Offset','Deflection') else 0.0
   self.notes.add(t+': wind sway evaluated at rest (zero offset)')
  elif t in ('Camera Facing','Camera Facing Y'):
   # Billboard vertex rotation toward the camera; the authored pose is kept.
   result=self.to_native(self.node('ShaderNodeTexCoord',name).outputs['Object'])
   self.notes.add(t+': billboard keeps its authored orientation')
  elif t=='Particle Colour':
   # Particle quads carry their colour as vertex colour 0.
   nd=self.node('ShaderNodeVertexColor',name);nd.layer_name='Color0'
   result=nd.outputs['Alpha'] if socket=='A' else self.separate(nd.outputs['Color'],socket) if socket in ('R','G','B') else nd.outputs['Color']
  elif t=='Particle UV':
   uvp=self.calc('MULTIPLY',self.uv(0),(number(v.get('U Tiling'),1),number(v.get('V Tiling'),1),1))
   result=self.separate(uvp,'X') if socket=='U' else self.separate(uvp,'Y') if socket=='V' else uvp
  elif t=='Particle Parameters':
   result=1.0;self.notes.add('Particle Parameters: per-particle emitter values are not available on a static mesh; 1.0 used')
  elif t=='Projected Decal UV':
   # scortch DXBC: uv = box(x,z) with Image Offset (x subtracted, y added) and
   # Image Size as divisor; the decal mesh carries box (x,z) as UV0. Alpha is
   # the in-box mask, already applied by clipping the decal mesh to the box.
   if socket=='Alpha':result=1.0
   else:
    size=number(v.get('Image Size'),(1,1,0));off=number(v.get('Image Offset'),(0,0,0))
    size=size if isinstance(size,tuple) else (size,size,1);off=off if isinstance(off,tuple) else (off,off,0)
    uv=self.uv(0)
    result=self.combine(self.calc('DIVIDE',self.calc('SUBTRACT',self.separate(uv,'X'),off[0]),size[0] or 1),self.calc('DIVIDE',self.calc('ADD',self.separate(uv,'Y'),off[1]),size[1] or 1))
  elif t=='SphereMask':
   # vfx_1_1_glow DXBC: k=2^(10*Hardness); (1 - min(|A-B|/Radius,1)^k)^k.
   d=self.calc('MINIMUM',self.calc('DIVIDE',self.calc('DISTANCE',inp('A'),inp('B')),inp('Radius',1)),1)
   k=self.calc('POWER',2,self.calc('MULTIPLY',inp('Hardness'),10))
   result=self.calc('POWER',self.calc('MAXIMUM',self.calc('SUBTRACT',1,self.calc('POWER',d,k)),0),k)
  elif t=='Radial Gradient':
   # questpickup_glow DXBC: max(0, 1 - (|UV-Center|/Radius)^Density); Center .5, Radius .5 defaults.
   uv=inp('UV',None) if (name,'UV') in self.incoming else self.uv()
   centre=inp('Center',(.5,.5,0)) if ((name,'Center') in self.incoming or v.get('Center')) else (.5,.5,0)
   radius=inp('Radius',.5) if ((name,'Radius') in self.incoming or v.get('Radius')) else .5
   d=self.calc('DIVIDE',self.calc('DISTANCE',self.combine(self.separate(uv,'X'),self.separate(uv,'Y')),self.combine(self.separate(centre,'X') if self.vector(centre) else centre,self.separate(centre,'Y') if self.vector(centre) else centre)),radius)
   result=self.calc('MAXIMUM',self.calc('SUBTRACT',1,self.calc('POWER',d,inp('Density',1))),0)
  elif t=='Screen UV':
   # SV_Position based: UV origin top-left (DirectX), Pixel Pos in pixels.
   win=self.node('ShaderNodeTexCoord',name).outputs['Window']
   uvs=self.combine(self.separate(win,'X'),self.calc('SUBTRACT',1,self.separate(win,'Y')))
   if socket=='Pixel Pos':
    r=bpy.context.scene.render;result=self.calc('MULTIPLY',uvs,(r.resolution_x,r.resolution_y,0))
   else:result=self.separate(uvs,'X') if socket=='U' else self.separate(uvs,'Y') if socket=='V' else uvs
  elif t=='Compare':result=self.calc('SUBTRACT',1,self.binary('LESS_THAN',inp('A'),inp('B')))  # footprint DXBC: ge A,B -> 1.0
  elif t=='Rotator':
   # beaconmaterial DXBC: uv' = R(Angle*2pi)(uv-Midpoint)+Midpoint, counter-clockwise.
   uv=inp('UV',None) if (name,'UV') in self.incoming else self.uv();mid=inp('Midpoint',(.5,.5,0))
   ang=self.calc('MULTIPLY',inp('Angle'),6.283185307179586);s=self.calc('SINE',ang);c=self.calc('COSINE',ang)
   du=self.calc('SUBTRACT',self.separate(uv,'X'),self.separate(mid,'X') if self.vector(mid) else mid)
   dv=self.calc('SUBTRACT',self.separate(uv,'Y'),self.separate(mid,'Y') if self.vector(mid) else mid)
   result=self.combine(self.calc('ADD',self.calc('SUBTRACT',self.calc('MULTIPLY',du,c),self.calc('MULTIPLY',dv,s)),self.separate(mid,'X') if self.vector(mid) else mid),
                       self.calc('ADD',self.calc('ADD',self.calc('MULTIPLY',du,s),self.calc('MULTIPLY',dv,c)),self.separate(mid,'Y') if self.vector(mid) else mid))
  elif t=='Camera Transform':
   # scale_test_planet / ledge_base DXBC: T is the camera position (world, native axes); Z its view direction.
   geo=self.node('ShaderNodeNewGeometry',name);cam=self.node('ShaderNodeCameraData',name)
   if socket=='Z':
    vt=self.node('ShaderNodeVectorTransform',name+' forward');vt.vector_type='VECTOR';vt.convert_from='CAMERA';vt.convert_to='WORLD';vt.inputs[0].default_value=(0,0,-1)
    result=self.to_native(vt.outputs[0])
   else:result=self.to_native(self.calc('ADD',geo.outputs['Position'],self.calc('MULTIPLY',geo.outputs['Incoming'],cam.outputs['View Distance'])))
  elif t=='Object Transform':
   # T is the object origin; Y/Z are the object's native Y/Z axes in world space.
   if socket in ('Y','Z','X'):
    vt=self.node('ShaderNodeVectorTransform',name);vt.vector_type='VECTOR';vt.convert_from='OBJECT';vt.convert_to='WORLD'
    vt.inputs[0].default_value={'X':(-1,0,0),'Y':(0,0,1),'Z':(0,-1,0)}[socket];result=self.to_native(vt.outputs[0])
   else:result=self.to_native(self.node('ShaderNodeObjectInfo',name).outputs['Location'])
  elif t=='Mesh Position':
   p=self.to_native(self.node('ShaderNodeTexCoord',name).outputs['Object'])
   result=self.separate(p,socket) if socket in ('X','Y','Z') else p
  elif t=='Pixel World Position':
   p=self.native_position();result=self.separate(p,socket) if socket in ('X','Y','Z') else p
  elif t=='Tri Planar Mapping':
   # twiggybushtrunk DXBC: UV XY=(p.x,p.y), UV XZ=(p.x,p.z), UV ZY=(p.z,p.y), raw position (no scale).
   p=inp('Position',None) if (name,'Position') in self.incoming else self.native_position()
   x,y,z=(self.separate(p,c) for c in 'XYZ')
   result={'UV XY':lambda:self.combine(x,y),'UV XZ':lambda:self.combine(x,z),'UV ZY':lambda:self.combine(z,y)}.get(socket,lambda:self.combine(x,y))()
  elif t=='Tri Planar Blend':
   # twiggybushtrunk DXBC: w=|n|^Exponent normalised; O = YZ*w.x + XZ*w.y + XY*w.z.
   n_=inp('Normal',None) if (name,'Normal') in self.incoming else self.output_world_normal(name)
   e=inp('Exponent',2);w=[self.calc('POWER',self.calc('ABSOLUTE',self.separate(n_,c)),e) for c in 'XYZ']
   total=self.calc('ADD',self.calc('ADD',w[0],w[1]),w[2])
   parts=[self.calc('MULTIPLY',inp(s),self.calc('DIVIDE',wi,total)) for s,wi in zip(('Color YZ','Color XZ','Color XY'),w)]
   result=self.calc('ADD',self.calc('ADD',parts[0],parts[1]),parts[2])
  elif t=='Tangent2View':
   # ab_rubblemound DXBC: the "view" normal is T*x+B*y+N*z in world space
   # (fresnel dots it with the world view vector). Returned in native axes.
   nm=self.node('ShaderNodeNormalMap',name);nm.uv_map='UV0';nm.space='TANGENT'
   self.wire(self.calc('ADD',self.calc('MULTIPLY',inp('Input',(0,0,1)),.5),.5),nm.inputs['Color'])
   result=self.to_native(nm.outputs['Normal'])
  elif t=='Make Tangent':
   # Only used by wind position offsets (not reproduced); tangent = normalize(cross(N, up)).
   nd=self.node('ShaderNodeVectorMath',name);nd.operation='CROSS_PRODUCT';self.wire(inp('Normal',(0,1,0)),nd.inputs[0]);nd.inputs[1].default_value=(0,1,0)
   result=self.calc('NORMALIZE',nd.outputs['Vector'])
  elif t=='Desaturate':
   # Caverock DXBC uses arithmetic RGB mean, not luminance weighting.
   rgb=inp('Input');mean=self.calc('DIVIDE',self.calc('ADD',self.calc('ADD',self.separate(rgb,'R'),self.separate(rgb,'G')),self.separate(rgb,'B')),3)
   result=self.calc('ADD',rgb,self.calc('MULTIPLY',self.calc('SUBTRACT',mean,rgb),inp('Amount',1)))
   result=self.calc('MINIMUM',self.calc('MAXIMUM',result,0),1)
   if socket in ('R','G','B'):result=self.separate(result,socket)
  elif t=='World Position':
   p=self.native_position();result=self.separate(p,socket) if socket in ('X','Y','Z') else p
  elif t=='Object Position':
   nd=self.node('ShaderNodeObjectInfo',name);p=nd.outputs['Location'];result=self.combine(self.calc('MULTIPLY',self.separate(p,'X'),-1),self.separate(p,'Z'),self.calc('MULTIPLY',self.separate(p,'Y'),-1))
  elif t=='World Normal':
   nd=self.node('ShaderNodeNewGeometry',name);p=nd.outputs['Normal'];p=self.combine(self.calc('MULTIPLY',self.separate(p,'X'),-1),self.separate(p,'Z'),self.calc('MULTIPLY',self.separate(p,'Y'),-1));result=self.separate(p,socket) if socket in ('X','Y','Z') else p
  elif t=='Time':
   # Native shader time in seconds; a simple-expression driver (no Python
   # auto-run needed) makes panners/rotators animate at 24 fps playback.
   nd=self.node('ShaderNodeValue','Native time (s)');fc=nd.outputs[0].driver_add('default_value');fc.driver.type='SCRIPTED';fc.driver.expression='frame/24'
   # Time 'Multiplier' scales it (planetdiscoballlights DXBC).
   result=self.calc('MULTIPLY',nd.outputs[0],number(v.get('Multiplier'),1)) if number(v.get('Multiplier'),1)!=1 else nd.outputs[0]
  elif t=='pow':result=self.calc('POWER',inp('A'),inp('P',1))
  elif t=='sub':result=self.calc('SUBTRACT',inp('A'),inp('B'))
  elif t=='clamp':result=self.calc('MAXIMUM',self.calc('MINIMUM',inp('Val'),inp('Max',1)),inp('Min'))
  elif t=='fractional':
   a=inp('A');result=self.combine(*(self.calc('FRACT',self.separate(a,c)) for c in 'XYZ')) if self.vector(a) else self.calc('FRACT',a)
  elif t=='splitfloat2':result=self.separate(inp('I'),{'x':'X','y':'Y'}.get(socket,'X'))
  elif t=='Scale and Bias':result=self.calc('ADD',self.calc('MULTIPLY',inp('A'),inp('Scale',1)),inp('Bias'))
  elif t=='if':
   # Branch on A versus B: A>B, A=B and A<B inputs.
   a=inp('A');b=inp('B');gt=self.calc('GREATER_THAN',a,b);lt=self.calc('LESS_THAN',a,b)
   eq=self.calc('SUBTRACT',self.calc('SUBTRACT',1,gt),lt)
   result=self.calc('ADD',self.calc('ADD',self.calc('MULTIPLY',inp('A>B'),gt),self.calc('MULTIPLY',inp('A<B'),lt)),self.calc('MULTIPLY',inp('A=B'),eq))
  elif t=='HDR Switch':result=inp('HDR')  # The PC renderer runs the HDR path.
  elif t=='Two Sided Blend':
   nd=self.node('ShaderNodeNewGeometry',name);back=nd.outputs['Backfacing']
   result=self.calc('ADD',inp('Front'),self.calc('MULTIPLY',self.calc('SUBTRACT',inp('Back'),inp('Front')),back))
  elif t=='fresnel':
   # Schlick: F0 + (1-F0) * (1 - saturate(N.V))^Exponent, using the shading normal.
   nd=self.node('ShaderNodeNewGeometry',name)
   # branch DXBC: F = F0 + (1-F0)*(1-min(|N.V|,1))^Exponent, F0 defaults to 0.1.
   # A custom Normal input (e.g. Tangent2View output, native axes) replaces the shading normal.
   normal=self.to_blender(inp('Normal')) if (name,'Normal') in self.incoming else nd.outputs['Normal']
   ndotv=self.calc('ABSOLUTE',self.calc('DOT_PRODUCT',normal,nd.outputs['Incoming']))
   f=self.calc('POWER',self.calc('SUBTRACT',1,self.calc('MINIMUM',ndotv,1)),inp('Exponent',5))
   f0=inp('F0',0.1);result=self.calc('ADD',f0,self.calc('MULTIPLY',self.calc('SUBTRACT',1,f0),f))
  elif t=='Mesh Normal':
   nd=self.node('ShaderNodeTexCoord',name);p=nd.outputs['Normal'];p=self.combine(self.calc('MULTIPLY',self.separate(p,'X'),-1),self.separate(p,'Z'),self.calc('MULTIPLY',self.separate(p,'Y'),-1));result=self.separate(p,socket) if socket in ('X','Y','Z') else p
  elif t=='Depth Fade':
   # Soft-particle fade against the depth buffer has no Cycles equivalent.
   result=inp('Opacity',1);self.notes.add('Depth Fade: soft fade where the surface meets scene depth is not reproduced; opacity kept')
  elif t=='Room Projection':
   # cor_windows_02 DXBC: tangent-space ray d=(-T.v, B.v, N.v) with v the
   # camera-to-surface direction; start p=(frac(uv)-0.5, 0); intersect the
   # box [-.5,.5]^2 x [0,.5] (t=min over axes of (sign(d)*.5-p)/d); the hit
   # point, normalised as a direction from the box centre, gives
   # uv=0.5+0.5*hit.xy. The compiled shader folds Dimensions to 1.
   geo=self.node('ShaderNodeNewGeometry',name+' geometry')
   tan=self.node('ShaderNodeTangent',name+' tangent');tan.direction_type='UV_MAP';tan.uv_map='UV0'
   view=self.calc('MULTIPLY',geo.outputs['Incoming'],-1)
   nrm=geo.outputs['Normal'];t_=tan.outputs['Tangent']
   bit=self.node('ShaderNodeVectorMath',name+' bitangent');bit.operation='CROSS_PRODUCT';self.links.new(nrm,bit.inputs[0]);self.links.new(t_,bit.inputs[1])
   d=[self.calc('MULTIPLY',self.calc('DOT_PRODUCT',t_,view),-1),self.calc('DOT_PRODUCT',bit.outputs['Vector'],view),self.calc('DOT_PRODUCT',nrm,view)]
   uv=inp('UV',self.uv())
   p=[self.calc('SUBTRACT',self.calc('FRACT',self.separate(uv,'X')),.5),self.calc('SUBTRACT',self.calc('FRACT',self.separate(uv,'Y')),.5),0]
   ts=[]
   for i,(di,pi) in enumerate(zip(d,p)):
    pos=self.calc('GREATER_THAN',di,-1e-9)
    target=self.calc('SUBTRACT',self.calc('MULTIPLY',pos,1.0),.5) if i<2 else self.calc('MULTIPLY',pos,.5)
    safe=self.calc('MULTIPLY',self.calc('MAXIMUM',self.calc('ABSOLUTE',di),1e-6),self.calc('SUBTRACT',self.calc('MULTIPLY',pos,2),1))
    ts.append(self.calc('DIVIDE',self.calc('SUBTRACT',target,pi),safe))
   tmin=self.calc('MINIMUM',self.calc('MINIMUM',ts[0],ts[1]),ts[2])
   hit=self.combine(*(self.calc('ADD',pi,self.calc('MULTIPLY',tmin,di)) for di,pi in zip(d,p)))
   hit=self.calc('NORMALIZE',hit)
   result=self.combine(self.calc('ADD',self.calc('MULTIPLY',self.separate(hit,'X'),.5),.5),self.calc('ADD',self.calc('MULTIPLY',self.separate(hit,'Y'),.5),.5))
   # bespin_window_proj DXBC uses the same unit box with Dimensions linked: they do not scale it.
  elif t=='Linear Gradient':result=self.calc('FRACT',self.separate(inp('UV',self.uv()),'X' if socket=='UGradient' else 'Y'))  # branch DXBC: VGradient = frac(uv.v)
  else:self.errors.add('Unsupported shader node: '+t+' ['+name+'] -> '+socket)
  self.cache[key]=result;return result
 def build(self):
  self.nodes.clear();out=self.node('ShaderNodeOutputMaterial','Surface');bsdf=self.node('ShaderNodeBsdfPrincipled','Native StandardMaterial');self.links.new(bsdf.outputs['BSDF'],out.inputs['Surface'])
  # Output node: StandardMaterial for meshes; decals, particles and screen
  # effects end in their own output node with the same surface sockets.
  if any(n['type'] in LANDSCAPE_NODES for n in self.graph['nodes']):
   # Terrain graphs (layer index/weight textures, texture arrays, height blend)
   # are evaluated by landscape_blender.landscape_material from the landscape
   # data itself; such a material is never assigned to an ordinary mesh.
   self.mat['lswtss_landscape_graph']=True;self.mat['lswtss_shader_graph']=json.dumps(self.graph)
   self.notes.add('Terrain material: built by the landscape builder (8-layer height blend; the game keeps the 4 strongest layers per pixel)')
   return {'resource':self.graph['resource'],'textures':0,'complete_shader':True,'issues':[],'notes':sorted(self.notes)}
  standard=next((n['name'] for t_ in OUTPUT_NODES for n in self.graph['nodes'] if n['type']==t_),None)
  if standard is None:
   self.errors.add('No material output node');return {'resource':self.graph['resource'],'textures':0,'complete_shader':False,'issues':sorted(self.errors)}
  self.output_type=self.defs[standard]['type']
  if self.output_type=='CustomEffectMaterial':
   # Screen-effect output: Colour is the displayed (unlit) colour.
   self.incoming={(k[0],'Emissive' if (k[0]==standard and k[1]=='Colour') else k[1]):v for k,v in self.incoming.items()}
  supported={'Albedo':'Base Color','Metalness':'Metallic','Roughness':'Roughness','Opacity':'Alpha','Alpha':'Alpha','Emissive':'Emission Color'}
  def emissive_linear():
   # branch/shop_holo DXBC glow passes square the Emissive input before adding
   # it (gamma-space artist value -> linear light).
   e=self.input(standard,'Emissive');return self.calc('MULTIPLY',e,e)
  for native,blender in supported.items():
   if (standard,native) in self.incoming:
    self.wire(emissive_linear() if native=='Emissive' else self.input(standard,native),bsdf.inputs[blender])
    if native=='Emissive':bsdf.inputs['Emission Strength'].default_value=1
  if (standard,'Normal') in self.incoming:
   nm=self.node('ShaderNodeNormalMap','Native tangent normal');nm.uv_map='UV0';self.wire(self.input(standard,'Normal'),nm.inputs['Color']);self.links.new(nm.outputs['Normal'],bsdf.inputs['Normal'])
  # AO is retained independently: Principled has no engine-equivalent AO input.
  if (standard,'AO Factor') in self.incoming:
   ao=self.input(standard,'AO Factor');nd=self.node('ShaderNodeValue','Native AO Factor (preserved)');self.mat['lswtss_ao_socket_preserved']=True
   # A reroute preserves the graph output without baking AO into colour.
   route=self.node('NodeReroute','Native AO');self.wire(ao,route.inputs[0])
  has_alpha=(standard,'Opacity') in self.incoming or (standard,'Alpha') in self.incoming
  state=self.graph.get('render_state') or {}
  blend=state.get('blend','opaque');lit=state.get('lit',True)
  def colour_source():
   # Unlit/additive shaders display their Emissive term; fall back to Albedo.
   if (standard,'Emissive') in self.incoming:return emissive_linear()
   if (standard,'Albedo') in self.incoming:return self.input(standard,'Albedo')
   return (1,1,1,1)
  if blend=='additive':
   # Additive: dest + src. Transparent (passes background) plus emission.
   # Source is weighted by its alpha (SrcAlpha, One), e.g. distancefog cards fade by vertex colour/alpha.
   colour=colour_source()
   if has_alpha:colour=self.calc('MULTIPLY',colour,self.input(standard,'Alpha') if (standard,'Alpha') in self.incoming else self.input(standard,'Opacity'))
   em=self.node('ShaderNodeEmission','Native additive emission');self.wire(colour,em.inputs['Color']);em.inputs['Strength'].default_value=1
   tr=self.node('ShaderNodeBsdfTransparent','Native additive pass-through')
   add=self.node('ShaderNodeAddShader','Native additive blend')
   self.links.new(tr.outputs['BSDF'],add.inputs[0]);self.links.new(em.outputs['Emission'],add.inputs[1]);self.links.new(add.outputs['Shader'],out.inputs['Surface'])
   self.mat.surface_render_method='BLENDED'
  elif blend=='alpha_blend':
   alpha=self.input(standard,'Alpha') if (standard,'Alpha') in self.incoming else self.input(standard,'Opacity') if (standard,'Opacity') in self.incoming else 1
   if not has_alpha:self.notes.add('Alpha-blend material with no Alpha input: alpha defaults to 1 (opaque), as in game')
   if lit:self.wire(alpha,bsdf.inputs['Alpha'])
   else:
    em=self.node('ShaderNodeEmission','Native unlit colour');self.wire(colour_source(),em.inputs['Color'])
    tr=self.node('ShaderNodeBsdfTransparent','Native transparency');mix=self.node('ShaderNodeMixShader','Native alpha blend')
    self.wire(alpha,mix.inputs['Fac']);self.links.new(tr.outputs['BSDF'],mix.inputs[1]);self.links.new(em.outputs['Emission'],mix.inputs[2]);self.links.new(mix.outputs['Shader'],out.inputs['Surface'])
   self.mat.surface_render_method='BLENDED'
  elif not lit:
   # Opaque unlit (LEDs, sky bases): the native shader outputs colour without lighting.
   em=self.node('ShaderNodeEmission','Native unlit colour');self.wire(colour_source(),em.inputs['Color']);self.links.new(em.outputs['Emission'],out.inputs['Surface'])
  if (standard,'Refraction Strength') in self.incoming or (standard,'Refraction Color') in self.incoming:
   # glassintact DXBC: colour = backbuffer(uv + small normal offset) *
   # (1 - max(Refraction Strength,0)) + lit surface/reflections. In Cycles the
   # see-through part is a transparent BSDF weighted 1-strength, tinted by
   # Refraction Color; the 0.07-clamped screen offset is not reproduced.
   s=self.calc('MAXIMUM',self.input(standard,'Refraction Strength'),0) if (standard,'Refraction Strength') in self.incoming else 0.0
   tr=self.node('ShaderNodeBsdfTransparent','Native refraction see-through')
   if (standard,'Refraction Color') in self.incoming:self.wire(self.input(standard,'Refraction Color'),tr.inputs['Color'])
   mix=self.node('ShaderNodeMixShader','Native refraction blend');self.wire(s,mix.inputs['Fac'])
   surface=out.inputs['Surface'].links[0].from_socket
   self.links.new(tr.outputs['BSDF'],mix.inputs[1]);self.links.new(surface,mix.inputs[2]);self.links.new(mix.outputs['Shader'],out.inputs['Surface'])
   self.mat.surface_render_method='BLENDED'
   self.notes.add('Refraction: see-through weighted 1-strength (glassintact DXBC); the small screen-space distortion is not reproduced')
  if state.get('alpha')=='alpha_test' and has_alpha:
   # Alpha test: hard cut at the engine's 0.5 reference instead of dithering.
   alpha=self.input(standard,'Alpha') if (standard,'Alpha') in self.incoming else self.input(standard,'Opacity')
   cut=self.calc('GREATER_THAN',alpha,0.5);self.wire(cut,bsdf.inputs['Alpha'])
  elif has_alpha and blend=='opaque':self.mat.surface_render_method='DITHERED'
  self.mat['lswtss_render_state']=json.dumps(state)
  for l in self.graph['links']:
   if l['nodeTo']==standard and l['socketTo'] not in set(supported)|{'Normal','AO Factor','Refraction Strength','Refraction Color'}|({'Colour'} if self.output_type=='CustomEffectMaterial' else set()):
    s_=l['socketTo']
    if s_ in VERTEX_STAGE_INPUTS:self.notes.add(VERTEX_STAGE_INPUTS[s_])
    else:self.errors.add('Untranslated StandardMaterial input: '+s_)
  # Preserve every referenced image even if an unsupported node prevents surface evaluation.
  for n in self.graph['nodes']:
   if n['type']=='sampleTexture2D':self.output(n['name'],'RGB')
  self.mat['lswtss_shader_graph']=json.dumps(self.graph)
  self.mat['lswtss_textures_resolved']=not any(e.startswith('Missing texture') for e in self.errors)
  self.mat['lswtss_shader_complete']=not self.errors
  return {'resource':self.graph['resource'],'textures':len(self.loaded),'complete_shader':not self.errors,'issues':sorted(self.errors),'notes':sorted(self.notes)}

def apply_materials(manifest_path):
 data=json.loads(Path(manifest_path).read_text());graphs={g['resource'].lower():g for g in data['graphs']};textures=data['textures'];report=[]
 for mat in bpy.data.materials:
  resource=mat.get('lswtss_resource','').replace('\\','/').lower()
  if not resource:continue
  if resource not in graphs:report.append({'resource':resource,'complete_shader':False,'issues':['No resolved native material graph']});continue
  try:report.append(Compiler(mat,graphs[resource],textures).build())
  except Exception as e:report.append({'resource':resource,'complete_shader':False,'issues':['Compile failed: '+str(e)]})
 return report

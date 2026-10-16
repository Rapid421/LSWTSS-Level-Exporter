"""Native NTT model inspection. No name, bounds, or polygon-count LOD guesses.

Independent reader informed by the public NTT Model Dumper and verified on the
295 Utapau models extracted from the user's PC installation. Unsupported metadata
is an error, never permission to emit all LODs.
"""
import math
import re
import struct

class FormatError(ValueError):
    pass

FORMATS = {2: ('<2f',8),3:('<3f',12),4:('<4f',16),5:('<2e',4),6:('<4e',8),7:('<4B',4),8:('<4B',4),9:('<4B',4)}

class Reader:
    def __init__(self,data,start=0,end=None):
        self.data=data; self.pos=start; self.end=len(data) if end is None else end
    def take(self,n):
        if n<0 or self.pos+n>self.end: raise FormatError(f'Truncated at {self.pos:#x}, requested {n}')
        p=self.pos; self.pos+=n; return self.data[p:self.pos]
    def unpack(self,fmt):return struct.unpack(fmt,self.take(struct.calcsize(fmt)))
    def u32(self):return self.unpack('>I')[0]
    def u16(self):return self.unpack('>H')[0]
    def string(self):
        n=self.u16();raw=self.take(n)
        if not n or raw[-1]!=0:raise FormatError('Invalid native string')
        return raw[:-1].decode('ascii')

def read_model(data,geometry=True):
    r=Reader(data); scenes=[]
    while r.pos<r.end:
        start=r.pos;n=r.u32();kind=r.take(12);version=r.u32();end=start+n+4
        if n<16 or end>r.end:raise FormatError('Invalid chunk length')
        if kind==b'.CC4HSER2CSG':scenes.append(read_csg(Reader(data,r.pos,end),version,geometry))
        r.pos=end
    if not scenes:raise FormatError('No native CSG chunk')
    return scenes

def read_csg(r,version,geometry):
    nm=r.u32();r.take(3)
    if nm>4096:raise FormatError('Material limit exceeded')
    materials=[]
    for _ in range(nm):
        materials.append({'path':r.string(),'name':r.string(),'flags':r.take(3).hex()})
    r.take(5);nmesh=r.u32();r.take(4)
    if nmesh>4096:raise FormatError('Mesh limit exceeded')
    meshes=[]
    for mi in range(nmesh):
        header=r.unpack('>6I');nb,nv=header[2],header[5]
        if header[0]!=0x534d4e52 or not 1<=nb<=16 or nv>16_000_000:raise FormatError('Unsupported mesh header')
        attrs={};layout=[]
        for _ in range(nb):
            if r.take(4)!=b'DXTV':raise FormatError('Missing DXTV buffer')
            bv,na=r.unpack('>2I')
            if not 1<=na<=32:raise FormatError('Invalid attribute count')
            descriptors=[tuple(r.take(3)) for _ in range(na)]
            try:stride=sum(FORMATS[fmt][1] for ty,fmt,offset in descriptors)
            except KeyError as exc:raise FormatError('Unsupported vertex format') from exc
            if any(offset+FORMATS[fmt][1]>stride for ty,fmt,offset in descriptors):raise FormatError('Attribute outside stride')
            r.take(6);raw=r.take(stride*nv);r.take(16)
            layout.append({'version':bv,'attributes':descriptors,'stride':stride})
            if geometry:
                for ty,fmt,offset in descriptors:
                    values=[struct.unpack_from(FORMATS[fmt][0],raw,i*stride+offset) for i in range(nv)]
                    if fmt==8:values=[tuple(x/255 for x in v) for v in values]
                    attrs[ty]=values
        ni,ifmt=r.unpack('>2I')
        if ifmt not in (2,4) or ni>64_000_000:raise FormatError('Unsupported index buffer')
        raw=r.take(ni*ifmt)
        indices=list(struct.unpack('<'+('H' if ifmt==2 else 'I')*ni,raw)) if geometry else []
        if geometry and (0 not in attrs or any(i>=nv for i in indices) or ni%3):raise FormatError('Invalid indexed triangle geometry')
        footer=r.take(78)
        if geometry and any(not math.isfinite(x) for v in attrs[0] for x in v):raise FormatError('Non-finite vertex')
        meshes.append({'index':mi,'vertices':nv,'indices_count':ni,'attributes':attrs,'indices':indices,'layout':layout,'footer':footer.hex()})
    nmatrices=r.u32()
    if nmatrices>1_000_000:raise FormatError('Matrix count limit')
    matrices=[r.unpack('>16f') for _ in range(nmatrices)]
    r.take(4);ngroups=r.u32()
    if ngroups>4096:raise FormatError('Draw group count limit')
    groups=[]
    for gi in range(ngroups):
        ndraw=r.u16();draws=[]
        for _ in range(ndraw):
            material=r.u16();unknown,mesh,flags=r.unpack('>3I');tail=r.take(2)
            if material>=nm or mesh>=nmesh:raise FormatError('Draw references invalid material/mesh')
            draws.append({'material':material,'mesh':mesh,'unknown':unknown,'flags':flags,'tail':tail.hex()})
        groups.append(draws)
    r.take(4);nnodes=r.u32()
    if nnodes!=ngroups:raise FormatError(f'Node/draw group correspondence unsupported: {nnodes}/{ngroups}')
    # Node records have version-dependent tails. Locate the native length-prefixed
    # node names, validate ASCII, NUL and parent index, then require EXACT table
    # cardinality. Keep offsets for independent byte-level verification.
    candidates=[]
    region=r.data[r.pos:r.end]
    for p in range(max(0,len(region)-7)):
        n=int.from_bytes(region[p:p+2],'big')
        if not 2<=n<=256 or p+2+n+4>len(region):continue
        text=region[p+2:p+2+n]
        if text[-1]!=0 or any(x<32 or x>=127 for x in text[:-1]):continue
        if not re.fullmatch(rb'Combined_(ROOT|LOD[0-9]+|SHAD|ZOCC)',text[:-1]):continue
        parent=int.from_bytes(region[p+2+n:p+2+n+4],'big',signed=True)
        if parent < -1 or parent>=nnodes:continue
        candidates.append({'name':text[:-1].decode('ascii'),'parent':parent,'offset':r.pos+p})
    if len(candidates)!=nnodes:raise FormatError(f'Unsupported node serialization: expected {nnodes}, located {len(candidates)}')
    for node,draws in zip(candidates,groups):node['draws']=draws
    if not candidates and not meshes:return {'version':version,'materials':materials,'meshes':meshes,'matrices':matrices,'nodes':[],'lod0':[]}  # empty placeholder model
    roots=[node for node in candidates if node['name'].lower() in ('combined_root','root') or re.search(r'(^|_)lod0$',node['name'],re.I)]
    if not roots:
        # A single node without alternate LOD/shadow nodes is unambiguous.
        if len(candidates)==1 and not re.search(r'lod[1-9]|shad',candidates[0]['name'],re.I):roots=candidates
        else:raise FormatError('No explicit LOD0/root node; refusing guessed LOD: '+','.join(n['name'] for n in candidates))
    return {'version':version,'materials':materials,'meshes':meshes,'matrices':matrices,'nodes':candidates,'lod0':roots}

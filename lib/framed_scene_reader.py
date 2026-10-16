"""Decode native class/length framing before attempting scene assembly."""
import re,struct,bisect

def u32(data,p):return struct.unpack_from('<I',data,p)[0]
def class_names(data):
    start=data.index(b'ClassList\0');count=u32(data,start+10)
    names=[];p=start+18
    def string(p):
        n=u32(data,p)
        if not 1<=n<=4096 or p+4+n>len(data) or data[p+3+n]!=0:raise ValueError('Invalid class string')
        return data[p+4:p+3+n].decode('ascii'),p+4+n
    for _ in range(count):
        tag,p=string(p)
        if tag!='Class':raise ValueError('Class table alignment')
        name,p=string(p);names.append(name);p+=4
        version,p=string(p)
        if version!='Version':raise ValueError('Invalid class version label')
        p+=4;length=u32(data,p);p+=4+length
        if p>len(data):raise ValueError('Class definition exceeds file')
    return names

def frames(data):
    classes=class_names(data);rows=[];rejected=[];weak_references=[];compact_records=[]
    for hit in re.finditer(rb'\x05\x00\x00\x00MOBJ\x00',data):
        start=hit.start()
        if not rows and start>=6:
            cls=struct.unpack_from('<H',data,start-6)[0]
            size=u32(data,start-4)
        elif start>=9 and data[start-9]==1:
            cls=u32(data,start-8);size=u32(data,start-4)
        else:rejected.append(start);continue
        end=start+size-4
        if cls<len(classes) and size==33 and end<=len(data) and 'WeakHandle' in classes[cls]:
            weak_references.append({'start':start,'end':end,'class':classes[cls],'target_guid':data[start+13:start+29].hex()})
            continue
        if cls>=len(classes) or size<36 or end>len(data):rejected.append(start);continue
        header=data[start+9:start+14]
        # Some class-list/container entries have the same length-delimited MOBJ
        # wrapper but do not carry the standard enabled/GUID header. Keep their
        # validated interval in the nesting stack so later apiEntity/component
        # parents remain accurate; they are metadata/list items, not render entities.
        if len(header)!=5 or header[4] not in (0,1):
            row={'start':start,'end':end,'class_index':cls,'class':classes[cls],
                 'size':size,'enabled_byte':None,'payload_start':start+9,
                 'compact_layout':True}
            rows.append(row);compact_records.append(start);continue
        row={'start':start,'end':end,'class_index':cls,'class':classes[cls],'size':size,'header_unknown':header[:4].hex(),'enabled_byte':header[4],'guid':data[start+14:start+30].hex(),'managed_byte':data[start+30],'locked_byte':data[start+31],'payload_start':start+32}
        if classes[cls]=='apiEntity':
            p=row['payload_start'];n=u32(data,p)
            if n>500 or p+4+n>end or (n and data[p+3+n]!=0):raise ValueError('Invalid entity name')
            row['name']=data[p+4:p+3+n].decode('utf-8','replace') if n else ''
            row['payload_start']=p+4+n
        rows.append(row)
    stack=[]
    for i,row in enumerate(rows):
        while stack and row['start']>=rows[stack[-1]]['end']:stack.pop()
        row['parent']=stack[-1] if stack else None
        if stack and row['end']>rows[stack[-1]]['end']:raise ValueError('Crossing native object frames')
        stack.append(i)
    return {'classes':classes,'records':rows,'weak_references':weak_references,
            'compact_mobj_offsets':compact_records,'rejected_mobj_offsets':rejected}

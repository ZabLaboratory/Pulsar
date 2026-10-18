import os,struct,uuid,hashlib,pefile,pdbparse
from minidump.minidumpfile import MinidumpFile
f=MinidumpFile.parse(r'.cache/native-reset-stability/20261017T155459Z-gen2-freeze.dmp')
root=os.path.abspath(r'.cache/native-reset-stability')
plugin=os.path.join(root,'plugin-build','RelWithDebInfo')
for name,pdb in [
 ('obs.dll',os.path.join(root,'private-runtime-exact-20261017T155459Z','runtime','bin','64bit','obs.pdb')),
 ('obs-websocket.dll',os.path.join(root,'private-runtime-exact-20261017T155459Z','runtime','obs-plugins','64bit','obs-websocket.pdb')),
 ('pulsar-capture-effects.dll',os.path.join(plugin,'pulsar-capture-effects.pdb')),
 ('w32-pthreads.dll',os.path.join(root,'private-runtime-exact-20261017T155459Z','runtime','bin','64bit','w32-pthreads.pdb')),
]:
 m=next(x for x in f.modules.modules if os.path.basename(x.name).lower()==name)
 pe=pefile.PE(m.name,fast_load=True); pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DEBUG']]); cv=next(e.entry for e in pe.DIRECTORY_ENTRY_DEBUG if getattr(e.entry,'CvSignature',None)==b'RSDS')
 dg=uuid.UUID(bytes_le=struct.pack('<IHH8s',cv.Signature_Data1,cv.Signature_Data2,cv.Signature_Data3,bytes([cv.Signature_Data4,cv.Signature_Data5])+cv.Signature_Data6))
 q=pdbparse.parse(pdb,fast_load=True); q.STREAM_PDB.load(); inf=q.STREAM_PDB; pg=uuid.UUID(bytes_le=struct.pack('<IHH8s',inf.GUID.Data1,inf.GUID.Data2,inf.GUID.Data3,inf.GUID.Data4))
 print(f'{name}: dll={os.path.basename(m.name)} ts={m.timestamp:#x} dll_sha={hashlib.sha256(open(m.name,"rb").read()).hexdigest()} pdb={pdb} pdb_sha={hashlib.sha256(open(pdb,"rb").read()).hexdigest()} cv={dg}/{cv.Age} pdb_id={pg}/{inf.Age} exact={dg==pg and cv.Age==inf.Age}')

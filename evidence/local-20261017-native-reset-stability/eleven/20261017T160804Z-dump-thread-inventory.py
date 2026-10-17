import os, struct
from minidump.minidumpfile import MinidumpFile
p=r'.cache/native-reset-stability/20261017T155459Z-gen2-freeze.dmp'
f=MinidumpFile.parse(p)
raw=open(p,'rb').read()
ns,dr=struct.unpack_from('<II',raw,8)
entries=[struct.unpack_from('<III',raw,dr+12*i) for i in range(ns)]
name_stream=next(x for x in entries if x[0]==24)
count=struct.unpack_from('<I',raw,name_stream[2])[0]
names={}
for i in range(count):
    tid,nrva=struct.unpack_from('<IQ',raw,name_stream[2]+4+12*i)
    nbytes=struct.unpack_from('<I',raw,nrva)[0]
    names[tid]=raw[nrva+4:nrva+4+nbytes].decode('utf-16le',errors='replace')
mods=f.modules.modules
infos={x.ThreadId:x for x in f.thread_info.infos}
print(f'PROCESSOR={f.sysinfo.ProcessorArchitecture} THREADS={len(f.threads.threads)} NAMED={len(names)}')
for t in f.threads.threads:
    c=t.ContextObject
    rip=int(c.Rip); rsp=int(c.Rsp); rcx=int(c.Rcx); rdx=int(c.Rdx); r8=int(c.R8)
    m=next((m for m in mods if m.baseaddress<=rip<m.endaddress),None)
    mod=f'{os.path.basename(m.name)}+0x{rip-m.baseaddress:x}' if m else '<unmapped>'
    info=infos.get(t.ThreadId)
    start=int(info.StartAddress) if info else 0
    print(f'TID={t.ThreadId:5d} name={names.get(t.ThreadId,"-")!r} pc={rip:016x} {mod} sp={rsp:016x} rcx={rcx:016x} rdx={rdx:016x} r8={r8:016x} start={start:016x}')

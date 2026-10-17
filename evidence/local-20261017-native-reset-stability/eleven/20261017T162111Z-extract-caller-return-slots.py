import struct, sys
from minidump.minidumpfile import MinidumpFile
p=sys.argv[1]; f=MinidumpFile.parse(p); r=f.get_reader()
for tid in map(int,sys.argv[2:]):
 t=next(x for x in f.threads.threads if x.ThreadId==tid); c=t.ContextObject; psp=c.Rsp+0xa8
 print(f'TID={tid} pthread_caller_sp={psp:#x}')
 for off in (0x78,0xb8):
  addr=psp+off; v=struct.unpack('<Q',r.read(addr,8))[0]; print(f'  slot+{off:#x} addr={addr:#x} value={v:#x}')

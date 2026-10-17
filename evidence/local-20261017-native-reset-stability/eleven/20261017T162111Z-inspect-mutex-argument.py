import sys, struct
from minidump.minidumpfile import MinidumpFile
p=sys.argv[1]; f=MinidumpFile.parse(p); reader=f.get_reader()
for tid in (int(x) for x in sys.argv[2:]):
 t=next(x for x in f.threads.threads if x.ThreadId==tid); c=t.ContextObject
 # Walk output establishes pthread frame at current RSP+0xa8; the saved return
 # slot is +0xa0. KERNELBASE saves its incoming RBX at entry-RSP+8, which is
 # exactly the caller (pthread) stack pointer. The observed qword is that RBX.
 return_slot=c.Rsp+0xa0
 caller_sp=return_slot+8
 saved_rbx=struct.unpack('<Q',reader.read(caller_sp,8))[0]
 event_handle=int(c.Rcx)
 print(f'TID={tid} current_rsp={c.Rsp:#x} pthread_return_slot={return_slot:#x} pthread_caller_sp={caller_sp:#x} saved_caller_rbx={saved_rbx:#x} wait_rcx_event_handle={event_handle:#x} same_saved_value_at_stackwalk_addrreturn={saved_rbx==0x2142252dd50}')
 try:
  data=reader.read(saved_rbx,0x40)
  print('MUTEX_OBJECT_MEMORY=present '+data.hex(' '))
 except Exception as e:
  print(f'MUTEX_OBJECT_MEMORY=not_in_dump ({e})')

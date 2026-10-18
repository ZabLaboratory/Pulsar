import os, struct, sys, pefile, capstone
from minidump.minidumpfile import MinidumpFile
p=sys.argv[1]; tid=int(sys.argv[2]); f=MinidumpFile.parse(p)
t=next(t for t in f.threads.threads if t.ThreadId==tid); c=t.ContextObject
mods=list(f.modules.modules)
mod=next(m for m in mods if os.path.basename(m.name).lower()=='kernelbase.dll')
print('MODULE',mod.name,'base',hex(mod.baseaddress),'size',hex(mod.size),'timestamp',hex(mod.timestamp),'checksum',hex(mod.checksum))
pe=pefile.PE(mod.name, fast_load=True); print('FILE_TIMESTAMP',hex(pe.FILE_HEADER.TimeDateStamp),'FILE_CHECKSUM',hex(pe.OPTIONAL_HEADER.CheckSum))
pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT'],pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXCEPTION']])
exports={e.name.decode(errors='replace'):e.address for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name}
rva=0x7ff9a0febe9f-mod.baseaddress
ex=exports.get('WaitForSingleObjectEx'); print('STACK_RETURN_RVA',hex(rva),'EXPORT_WFSOEX_RVA',hex(ex) if ex else None,'delta',hex(rva-ex) if ex else None)
for x in pe.DIRECTORY_ENTRY_EXCEPTION:
 s=x.struct
 if s.BeginAddress<=rva<s.EndAddress:
  print('RUNTIME_FUNCTION',hex(s.BeginAddress),hex(s.EndAddress),hex(s.UnwindData))
  u=pe.get_data(s.UnwindData,128); verflags=u[0]; count=u[2]; frame=u[3]
  print('UNWIND_HEADER',u[:4].hex(' '),'version',verflags&7,'flags',verflags>>3,'prolog',u[1],'slots',count,'frame',hex(frame))
  i=0
  while i<count:
   codeoff=u[4+i*2]; opbyte=u[5+i*2]; op=opbyte&15; info=opbyte>>4
   extra=[]
   if op==1:
    if info==0 and i+1<count: extra=[struct.unpack_from('<H',u,4+(i+1)*2)[0]]; i+=1
    elif info==1 and i+2<count: extra=list(struct.unpack_from('<I',u,4+(i+1)*2)[0:1]); i+=2
   elif op in (4,8) and i+1<count:
    extra=[struct.unpack_from('<H',u,4+(i+1)*2)[0]]; i+=1
   elif op in (5,9) and i+2<count:
    extra=[struct.unpack_from('<I',u,4+(i+1)*2)[0]]; i+=2
   print('UNWIND_CODE',codeoff,'op',op,'info',info,'extra',extra)
   i+=1
# disassemble WFSOEx entry neighborhood
md=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64)
for start,length in ((ex,0x150),(rva-0x30,0x80)):
 if start is None: continue
 print('DISASM',hex(start))
 for ins in md.disasm(pe.get_data(start,length),start): print(f'{ins.address:#x}: {ins.mnemonic} {ins.op_str}')

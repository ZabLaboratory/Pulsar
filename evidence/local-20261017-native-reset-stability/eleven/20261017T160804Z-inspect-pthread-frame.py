import os, struct, pefile
from minidump.minidumpfile import MinidumpFile
p=r'.cache/native-reset-stability/20261017T155459Z-gen2-freeze.dmp'
f=MinidumpFile.parse(p); reader=f.get_reader()
mod=next(m for m in f.modules.modules if m.name.lower().endswith('w32-pthreads.dll'))
print('MODULE',mod.name,'base',hex(mod.baseaddress),'size',hex(mod.size),'timestamp',hex(mod.timestamp),'checksum',hex(mod.checksum))
pe=pefile.PE(mod.name)
pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXCEPTION']])
rva=0x383f
hits=[x for x in pe.DIRECTORY_ENTRY_EXCEPTION if x.struct.BeginAddress<=rva<x.struct.EndAddress]
print('PC_RVA',hex(rva),'PDB_NOT_PRESENT','pdata_entry_count',len(pe.DIRECTORY_ENTRY_EXCEPTION))
for x in hits:
    s=x.struct
    print('RUNTIME_FUNCTION',hex(s.BeginAddress),hex(s.EndAddress),hex(s.UnwindData))
    ui=pe.get_data(s.UnwindData,32)
    print('UNWIND_INFO',ui[:24].hex(' '),'version',ui[0]&7,'flags',ui[0]>>3,'prolog',ui[1],'codes',ui[2],'frame',hex(ui[3]))
    code=pe.get_data(s.BeginAddress, s.EndAddress-s.BeginAddress)
    print('FUNCTION_BYTES_START',code[:96].hex(' '))
for tid in (18964,8784):
    t=next(t for t in f.threads.threads if t.ThreadId==tid)
    c=t.ContextObject
    print(f'THREAD tid={tid} pc={c.Rip:#x} sp={c.Rsp:#x} rcx={c.Rcx:#x} rdx={c.Rdx:#x} rbx={c.Rbx:#x} rbp={c.Rbp:#x} rsi={c.Rsi:#x} rdi={c.Rdi:#x} r8={c.R8:#x} r9={c.R9:#x} r10={c.R10:#x} r11={c.R11:#x} r12={c.R12:#x} r13={c.R13:#x} r14={c.R14:#x} r15={c.R15:#x}')
    raw=reader.read(c.Rsp,0x100)
    print('STACK_QWORDS',[(hex(c.Rsp+i*8),hex(struct.unpack_from('<Q',raw,i*8)[0])) for i in range(0,len(raw)//8)])
    for name,val in [('rbx',c.Rbx),('rbp',c.Rbp),('rsi',c.Rsi),('rdi',c.Rdi),('r12',c.R12),('r13',c.R13),('r14',c.R14),('r15',c.R15)]:
        if 0x10000 < val < 0x00007fffffffffff:
            try:
                data=reader.read(val,0x40)
                print('REG_MEMORY',name,hex(val),data.hex(' '))
            except Exception: pass

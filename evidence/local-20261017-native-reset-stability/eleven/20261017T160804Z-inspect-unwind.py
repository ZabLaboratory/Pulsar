import pefile, struct
p=r'.cache/native-reset-stability/private-runtime-exact-20261017T155459Z/runtime/bin/64bit/w32-pthreads.dll'
pe=pefile.PE(p)
pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXCEPTION']])
for rva in (0xf9cc,0xf9dc):
    u=pe.get_data(rva,48)
    n=u[2]
    off=4+2*n
    print(hex(rva),'unwind',u[:max(off+12,12)].hex(' '),'ver',u[0]&7,'flags',u[0]>>3,'prolog',u[1],'count',n,'chain',tuple(hex(v) for v in struct.unpack_from('<III',u,off)) if (u[0]>>3)&4 else None)

import hashlib, os, pefile, capstone
p = r'.cache/native-reset-stability/private-runtime-exact-20261017T155459Z/runtime/bin/64bit/w32-pthreads.dll'
pe = pefile.PE(p, fast_load=True)
print('DLL', p)
print('DLL_SHA256', hashlib.sha256(open(p, 'rb').read()).hexdigest())
print('FILE_TIMESTAMP', hex(pe.FILE_HEADER.TimeDateStamp), 'CHECKSUM', hex(pe.OPTIONAL_HEADER.CheckSum))
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
for ins in md.disasm(pe.get_data(0x3828, 0x28), 0x3828):
    print(f'{ins.address:#x}: {ins.mnemonic} {ins.op_str}')

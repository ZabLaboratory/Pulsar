import os,struct,uuid,hashlib,ctypes,pefile,pdbparse
from ctypes import wintypes
from minidump.minidumpfile import MinidumpFile
f=MinidumpFile.parse(r'.cache/native-reset-stability/20261017T155459Z-gen2-freeze.dmp'); mods=list(f.modules.modules)
for name,pdb in [('pulsar-capture-effects.dll',r'.cache/native-reset-stability/plugin-build/RelWithDebInfo/pulsar-capture-effects.pdb'),('obs-websocket.dll',r'.cache/native-reset-stability/private-runtime-exact-20261017T155459Z/runtime/obs-plugins/64bit/obs-websocket.pdb')]:
 m=next((x for x in mods if os.path.basename(x.name).lower()==name),None)
 if not m: print('NO_MODULE',name); continue
 print('MODULE',name,'base',hex(m.baseaddress),'timestamp',hex(m.timestamp),'checksum',hex(m.checksum),'path',m.name)
 if not os.path.isfile(pdb): print('PDB_MISSING',pdb); continue
 pe=pefile.PE(m.name,fast_load=True); pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DEBUG']]); cv=next(e.entry for e in pe.DIRECTORY_ENTRY_DEBUG if getattr(e.entry,'CvSignature',None)==b'RSDS'); dg=uuid.UUID(bytes_le=struct.pack('<IHH8s',cv.Signature_Data1,cv.Signature_Data2,cv.Signature_Data3,bytes([cv.Signature_Data4,cv.Signature_Data5])+cv.Signature_Data6)); q=pdbparse.parse(pdb,fast_load=True); q.STREAM_PDB.load(); inf=q.STREAM_PDB; pg=uuid.UUID(bytes_le=struct.pack('<IHH8s',inf.GUID.Data1,inf.GUID.Data2,inf.GUID.Data3,inf.GUID.Data4)); print('PDB_ID',dg,cv.Age,'vs',pg,inf.Age,'EXACT',dg==pg and cv.Age==inf.Age,'SHA256',hashlib.sha256(open(pdb,'rb').read()).hexdigest())
# DbgHelp lookup
k=ctypes.WinDLL('kernel32',use_last_error=True); k.GetCurrentProcess.restype=ctypes.c_void_p; proc=k.GetCurrentProcess(); d=ctypes.WinDLL(r'C:\Windows\System32\dbghelp.dll',use_last_error=True)
d.SymInitializeW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,wintypes.BOOL]; d.SymInitializeW.restype=wintypes.BOOL; d.SymSetOptions.argtypes=[wintypes.DWORD]; d.SymSetOptions.restype=wintypes.DWORD; d.SymLoadModuleExW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint64,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]; d.SymLoadModuleExW.restype=ctypes.c_uint64
class SI(ctypes.Structure): _fields_=[('SizeOfStruct',wintypes.ULONG),('TypeIndex',wintypes.ULONG),('Reserved',ctypes.c_uint64*2),('Index',wintypes.ULONG),('Size',wintypes.ULONG),('ModBase',ctypes.c_uint64),('Flags',wintypes.ULONG),('Value',ctypes.c_uint64),('Address',ctypes.c_uint64),('Register',wintypes.ULONG),('Scope',wintypes.ULONG),('Tag',wintypes.ULONG),('NameLen',wintypes.ULONG),('MaxNameLen',wintypes.ULONG),('Name',ctypes.c_wchar*1)]
class LINE(ctypes.Structure): _fields_=[('SizeOfStruct',wintypes.DWORD),('Key',ctypes.c_void_p),('LineNumber',wintypes.DWORD),('FileName',ctypes.c_wchar_p),('Address',ctypes.c_uint64)]
d.SymFromAddrW.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(SI)]; d.SymFromAddrW.restype=wintypes.BOOL; d.SymGetLineFromAddrW64.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(LINE)]; d.SymGetLineFromAddrW64.restype=wintypes.BOOL
root=os.path.abspath(r'.cache/native-reset-stability'); dirs=[os.path.join(root,'plugin-build','RelWithDebInfo'),os.path.join(root,'private-runtime-exact-20261017T155459Z','runtime','bin','64bit'),os.path.join(root,'private-runtime-exact-20261017T155459Z','runtime','obs-plugins','64bit')]; sympath=';'.join(dirs); d.SymSetOptions(0x2|0x4|0x10|0x80000|0x80); print('SYM_INIT',bool(d.SymInitializeW(proc,sympath,False)),sympath)
for m in mods:
 if os.path.basename(m.name).lower() in ('obs.dll','w32-pthreads.dll','obs-websocket.dll','pulsar-capture-effects.dll'):
  h=d.SymLoadModuleExW(proc,None,m.name,None,int(m.baseaddress),int(m.size),None,0); print('LOAD',os.path.basename(m.name),hex(h) if h else None,ctypes.get_last_error())
for addr in (0x7ff903a1c822,0x7ff90489ff26,0x7ff8fe58edb1,0x7ff970d6383f):
 b=ctypes.create_string_buffer(4096); si=ctypes.cast(b,ctypes.POINTER(SI)); si.contents.SizeOfStruct=ctypes.sizeof(SI); si.contents.MaxNameLen=1000; disp=ctypes.c_uint64(); sym='?'
 if d.SymFromAddrW(proc,addr,ctypes.byref(disp),si): sym=b.raw[SI.Name.offset:SI.Name.offset+2*si.contents.NameLen].decode('utf-16le',errors='replace')+f'+0x{disp.value:x}'
 line=LINE(); line.SizeOfStruct=ctypes.sizeof(LINE); ld=wintypes.DWORD(); loc='?'
 if d.SymGetLineFromAddrW64(proc,addr,ctypes.byref(ld),ctypes.byref(line)): loc=f'{line.FileName}:{line.LineNumber}+0x{ld.value:x}'
 print(f'PC {addr:#x}: {sym} {loc}')


import os,sys,ctypes
from ctypes import wintypes
from minidump.minidumpfile import MinidumpFile
DMP=os.path.abspath(sys.argv[1]); ROOT=os.path.dirname(DMP); rt=os.path.join(ROOT,'private-runtime-exact-20261017T155459Z','runtime','bin','64bit')
dmp=MinidumpFile.parse(DMP); mod=next(m for m in dmp.modules.modules if os.path.basename(m.name).lower()=='w32-pthreads.dll'); base=int(mod.baseaddress); size=int(mod.size); dll=os.path.join(rt,'w32-pthreads.dll')
k=ctypes.WinDLL('kernel32',use_last_error=True); k.GetCurrentProcess.restype=ctypes.c_void_p; proc=k.GetCurrentProcess(); k.LocalFree.argtypes=[ctypes.c_void_p]; k.LocalFree.restype=ctypes.c_void_p
d=ctypes.WinDLL(r'C:\Windows\System32\dbghelp.dll',use_last_error=True)
d.SymSetOptions.argtypes=[wintypes.DWORD]; d.SymSetOptions.restype=wintypes.DWORD
d.SymInitializeW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,wintypes.BOOL]; d.SymInitializeW.restype=wintypes.BOOL
d.SymLoadModuleExW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint64,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]; d.SymLoadModuleExW.restype=ctypes.c_uint64
d.SymGetTypeInfo.argtypes=[ctypes.c_void_p,ctypes.c_uint64,wintypes.ULONG,ctypes.c_int,ctypes.c_void_p]; d.SymGetTypeInfo.restype=wintypes.BOOL
class SYM(ctypes.Structure): _fields_=[('SizeOfStruct',wintypes.ULONG),('TypeIndex',wintypes.ULONG),('Reserved',ctypes.c_uint64*2),('Index',wintypes.ULONG),('Size',wintypes.ULONG),('ModBase',ctypes.c_uint64),('Flags',wintypes.ULONG),('Value',ctypes.c_uint64),('Address',ctypes.c_uint64),('Register',wintypes.ULONG),('Scope',wintypes.ULONG),('Tag',wintypes.ULONG),('NameLen',wintypes.ULONG),('MaxNameLen',wintypes.ULONG),('Name',ctypes.c_wchar*1)]
ENUM=ctypes.WINFUNCTYPE(wintypes.BOOL,ctypes.POINTER(SYM),wintypes.ULONG,ctypes.c_void_p); d.SymEnumTypesW.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ENUM,ctypes.c_void_p]; d.SymEnumTypesW.restype=wintypes.BOOL
opts=0x2|0x4|0x10|0x80|0x200|0x80000|0x400; d.SymSetOptions(opts); assert d.SymInitializeW(proc,rt,False); assert d.SymLoadModuleExW(proc,None,dll,None,base,size,None,0)==base
names={}
@ENUM
def cb(ps,sz,ctx):
 s=ps.contents; n=ctypes.wstring_at(ctypes.addressof(s)+SYM.Name.offset,int(s.NameLen)).rstrip('\0'); names[int(s.TypeIndex)]=n; return True
assert d.SymEnumTypesW(proc,base,cb,None)
def get(typeid,kind,ctype):
 v=ctype(); ok=d.SymGetTypeInfo(proc,base,typeid,kind,ctypes.byref(v)); return ok,v.value
for tid in (494,111,198,110,197,293):
 print('\nTYPE',names.get(tid),tid,'size',get(tid,2,ctypes.c_uint64))
 ok,cnt=get(tid,13,wintypes.ULONG)
 if not ok: print('child_count failed',ctypes.get_last_error()); continue
 buf=ctypes.create_string_buffer(8+4*cnt)
 class _H(ctypes.Structure):
  _fields_=[('Count',wintypes.ULONG),('Start',wintypes.ULONG)]
 h=ctypes.cast(buf,ctypes.POINTER(_H)); h.contents.Count=cnt; h.contents.Start=0
 ok=d.SymGetTypeInfo(proc,base,tid,7,buf)
 print('children',cnt,'find_ok',bool(ok),'err',ctypes.get_last_error())
 if not ok: continue
 kids=(wintypes.ULONG*cnt).from_buffer(buf,8)
 for cid in kids:
  nm=ctypes.c_void_p(); oknm=d.SymGetTypeInfo(proc,base,int(cid),1,ctypes.byref(nm)); name=ctypes.wstring_at(nm.value).rstrip('\0') if oknm and nm.value else '<name?>'
  if nm.value: k.LocalFree(nm)
  okoff,off=get(int(cid),10,wintypes.ULONG); oktype,ty=get(int(cid),4,wintypes.ULONG); oklen,leng=get(int(cid),2,ctypes.c_uint64); oktag,tag=get(int(cid),0,wintypes.ULONG)
  print(' FIELD',name,'off',off if okoff else '?','type',names.get(int(ty),f'#{ty}') if oktype else '?','typeid',ty if oktype else '?','len',leng if oklen else '?','tag',tag if oktag else '?')

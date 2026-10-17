import os,sys,ctypes
from ctypes import wintypes
from minidump.minidumpfile import MinidumpFile
DMP=os.path.abspath(sys.argv[1]); ROOT=os.path.dirname(DMP); rt=os.path.join(ROOT,'private-runtime-exact-20261017T155459Z','runtime','bin','64bit')
dmp=MinidumpFile.parse(DMP); mod=next(m for m in dmp.modules.modules if os.path.basename(m.name).lower()=='w32-pthreads.dll'); base=int(mod.baseaddress); dll=os.path.join(rt,'w32-pthreads.dll')
k=ctypes.WinDLL('kernel32',use_last_error=True); k.GetCurrentProcess.restype=ctypes.c_void_p; proc=k.GetCurrentProcess(); k.LocalFree.argtypes=[ctypes.c_void_p]; k.LocalFree.restype=ctypes.c_void_p
d=ctypes.WinDLL(r'C:\Windows\System32\dbghelp.dll',use_last_error=True); d.SymSetOptions.argtypes=[wintypes.DWORD]; d.SymSetOptions.restype=wintypes.DWORD; d.SymInitializeW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,wintypes.BOOL]; d.SymInitializeW.restype=wintypes.BOOL; d.SymLoadModuleExW.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint64,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]; d.SymLoadModuleExW.restype=ctypes.c_uint64; d.SymGetTypeInfo.argtypes=[ctypes.c_void_p,ctypes.c_uint64,wintypes.ULONG,ctypes.c_int,ctypes.c_void_p]; d.SymGetTypeInfo.restype=wintypes.BOOL
assert d.SymInitializeW(proc,rt,False); assert d.SymLoadModuleExW(proc,None,dll,None,base,int(mod.size),None,0)
class SYM(ctypes.Structure):
 _fields_=[('SizeOfStruct',wintypes.ULONG),('TypeIndex',wintypes.ULONG),('Reserved',ctypes.c_uint64*2),('Index',wintypes.ULONG),('Size',wintypes.ULONG),('ModBase',ctypes.c_uint64),('Flags',wintypes.ULONG),('Value',ctypes.c_uint64),('Address',ctypes.c_uint64),('Register',wintypes.ULONG),('Scope',wintypes.ULONG),('Tag',wintypes.ULONG),('NameLen',wintypes.ULONG),('MaxNameLen',wintypes.ULONG),('Name',ctypes.c_wchar*1)]
ENUM=ctypes.WINFUNCTYPE(wintypes.BOOL,ctypes.POINTER(SYM),wintypes.ULONG,ctypes.c_void_p)
d.SymEnumTypesW.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ENUM,ctypes.c_void_p]; d.SymEnumTypesW.restype=wintypes.BOOL
@ENUM
def enum_cb(p,n,c): return True
print('enum',bool(d.SymEnumTypesW(proc,base,enum_cb,None)),'err',ctypes.get_last_error())==base
# IMAGEHLP_SYMBOL_TYPE_INFO enum values from local Windows SDK DbgHelp.h
T={'TAG':0,'NAME':1,'LENGTH':2,'TYPE':3,'TYPEID':4,'BASETYPE':5,'CHILDRENCOUNT':13,'UDTKIND':24}
def val(tid,kind,typ):
 v=typ(); ok=d.SymGetTypeInfo(proc,base,tid,kind,ctypes.byref(v)); return bool(ok),v.value
for start in [71,28,38,89,542,571,43,34,196,151,547,293,173,197,198,494]:
 tid=start; seen=set(); print('\nCHAIN',start)
 for depth in range(6):
  if tid in seen: break
  seen.add(tid)
  ok,tag=val(tid,T['TAG'],wintypes.ULONG); okn,p=val(tid,T['NAME'],ctypes.c_void_p); name=ctypes.wstring_at(p).rstrip('\0') if okn and p else '<anon>';
  if p: k.LocalFree(p)
  okl,l=val(tid,T['LENGTH'],ctypes.c_uint64); okt,target=val(tid,T['TYPE'],wintypes.ULONG); okb,bt=val(tid,T['BASETYPE'],wintypes.ULONG); oku,udt=val(tid,T['UDTKIND'],wintypes.ULONG)
  print(' node',tid,'name=',name,'tag=',tag if ok else None,'len=',l if okl else None,'target=',target if okt else None,'base=',bt if okb else None,'udt=',udt if oku else None)
  if not okt or target==tid: break
  tid=target

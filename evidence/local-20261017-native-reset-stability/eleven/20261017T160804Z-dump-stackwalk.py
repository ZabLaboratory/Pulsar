import ctypes, os, sys, struct, json, pefile
from ctypes import wintypes
from minidump.minidumpfile import MinidumpFile

DMP = os.path.abspath(sys.argv[1])
TID = int(sys.argv[2]) if len(sys.argv)>2 else 18964
f = MinidumpFile.parse(DMP)
thread = next(t for t in f.threads.threads if t.ThreadId == TID)
ctxloc = thread.ThreadContext
with open(DMP, 'rb') as fp:
    fp.seek(ctxloc.Rva)
    ctxraw = fp.read(ctxloc.DataSize)
if len(ctxraw) != ctxloc.DataSize or len(ctxraw) < 0x100:
    raise RuntimeError(f'bad context bytes {len(ctxraw)} / {ctxloc.DataSize}')
# AMD64 CONTEXT (Windows SDK layout): Rsp @ 0x98, Rbp @ 0xa0, Rip @ 0xf8.
rsp, rbp = struct.unpack_from('<QQ', ctxraw, 0x98); rip = struct.unpack_from('<Q', ctxraw, 0xf8)[0]
print(f'DUMP={DMP}')
print(f'TID={TID} name=see MINIDUMP_THREAD_NAMES context_size={len(ctxraw)} RIP=0x{rip:016x} RSP=0x{rsp:016x} RBP=0x{rbp:016x}')

class ADDRESS64(ctypes.Structure):
    _fields_ = [('Offset', ctypes.c_uint64), ('Segment', ctypes.c_uint16), ('Mode', ctypes.c_int)]
class KDHELP64(ctypes.Structure):
    _fields_ = [('Thread', ctypes.c_uint64), ('ThCallbackStack', ctypes.c_uint32), ('ThCallbackBStore', ctypes.c_uint32), ('NextCallback', ctypes.c_uint32), ('FramePointer', ctypes.c_uint32), ('KiCallUserMode', ctypes.c_uint64), ('KeUserCallbackDispatcher', ctypes.c_uint64), ('SystemRangeStart', ctypes.c_uint64), ('KiUserExceptionDispatcher', ctypes.c_uint64), ('StackBase', ctypes.c_uint64), ('StackLimit', ctypes.c_uint64), ('Reserved', ctypes.c_uint64 * 5)]
class STACKFRAME64(ctypes.Structure):
    _fields_ = [('AddrPC', ADDRESS64), ('AddrReturn', ADDRESS64), ('AddrFrame', ADDRESS64), ('AddrStack', ADDRESS64), ('AddrBStore', ADDRESS64), ('FuncTableEntry', ctypes.c_void_p), ('Params', ctypes.c_uint64 * 4), ('Far', ctypes.c_int), ('Virtual', ctypes.c_int), ('Reserved', ctypes.c_uint64 * 3), ('KdHelp', KDHELP64)]
class SYMBOL_INFO(ctypes.Structure):
    _fields_ = [('SizeOfStruct', wintypes.ULONG), ('TypeIndex', wintypes.ULONG), ('Reserved', ctypes.c_uint64 * 2), ('Index', wintypes.ULONG), ('Size', wintypes.ULONG), ('ModBase', ctypes.c_uint64), ('Flags', wintypes.ULONG), ('Value', ctypes.c_uint64), ('Address', ctypes.c_uint64), ('Register', wintypes.ULONG), ('Scope', wintypes.ULONG), ('Tag', wintypes.ULONG), ('NameLen', wintypes.ULONG), ('MaxNameLen', wintypes.ULONG), ('Name', ctypes.c_wchar * 1)]
print(f'SIZES STACKFRAME64={ctypes.sizeof(STACKFRAME64)} SYMBOL_INFO={ctypes.sizeof(SYMBOL_INFO)} nameoff={SYMBOL_INFO.Name.offset}')

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.GetCurrentThread.restype = ctypes.c_void_p
proc = kernel32.GetCurrentProcess()
thr = kernel32.GetCurrentThread()
D = ctypes.WinDLL(r'C:\Windows\System32\dbghelp.dll', use_last_error=True)
D.SymInitializeW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, wintypes.BOOL]
D.SymInitializeW.restype = wintypes.BOOL
D.SymSetOptions.argtypes = [wintypes.DWORD]
D.SymSetOptions.restype = wintypes.DWORD
D.SymLoadModuleExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint64, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
D.SymLoadModuleExW.restype = ctypes.c_uint64
D.SymFromAddrW.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(SYMBOL_INFO)]
D.SymFromAddrW.restype = wintypes.BOOL
D.SymFunctionTableAccess64.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
D.SymFunctionTableAccess64.restype = ctypes.c_void_p
D.SymGetModuleBase64.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
D.SymGetModuleBase64.restype = ctypes.c_uint64
BOOL = wintypes.BOOL
HANDLE = ctypes.c_void_p
DWORD = wintypes.DWORD
DWORD64 = ctypes.c_uint64
PVOID = ctypes.c_void_p

reader = f.get_reader()
mods = list(f.modules.modules)
pe_cache = {}
image_read_count = [0]
def read_module_image(addr, size):
    for m in mods:
        if not (m.baseaddress <= addr < m.endaddress):
            continue
        path = m.name
        if not os.path.isfile(path):
            break
        pe = pe_cache.get(path)
        if pe is None:
            pe = pefile.PE(path, fast_load=True)
            pe_cache[path] = pe
            if int(pe.FILE_HEADER.TimeDateStamp) != int(m.timestamp):
                print(f'MODULE_TIMESTAMP_MISMATCH {os.path.basename(path)} dump={m.timestamp} file={pe.FILE_HEADER.TimeDateStamp}')
            if int(pe.OPTIONAL_HEADER.CheckSum) != int(m.checksum):
                print(f'MODULE_CHECKSUM_MISMATCH {os.path.basename(path)} dump={m.checksum} file={pe.OPTIONAL_HEADER.CheckSum}')
        rva = int(addr) - int(m.baseaddress)
        remain = int(size)
        out = bytearray()
        with open(path, 'rb') as fp:
            while remain:
                if rva < int(pe.OPTIONAL_HEADER.SizeOfHeaders):
                    take = min(remain, int(pe.OPTIONAL_HEADER.SizeOfHeaders) - rva)
                    fileoff = rva
                    rawtake = take
                else:
                    section = next((x for x in pe.sections if int(x.VirtualAddress) <= rva < int(x.VirtualAddress) + max(int(x.Misc_VirtualSize), int(x.SizeOfRawData))), None)
                    if section is None:
                        raise ValueError(f'no PE section for {os.path.basename(path)} RVA 0x{rva:x}')
                    delta = rva - int(section.VirtualAddress)
                    take = min(remain, int(section.VirtualAddress) + max(int(section.Misc_VirtualSize), int(section.SizeOfRawData)) - rva)
                    rawtake = min(take, max(0, int(section.SizeOfRawData) - delta))
                    fileoff = int(section.PointerToRawData) + delta
                if rawtake:
                    fp.seek(fileoff)
                    chunk = fp.read(rawtake)
                    if len(chunk) != rawtake:
                        raise ValueError('short PE file read')
                    out.extend(chunk)
                if rawtake < take:
                    out.extend(b'\0' * (take - rawtake))
                rva += take
                remain -= take
        image_read_count[0] += 1
        return bytes(out)
    raise ValueError(f'not dump memory or local image: 0x{addr:x}')
read_stats = {'attempts':0, 'ok':0, 'fail':0, 'last_error':None, 'last_range':None}
@ctypes.WINFUNCTYPE(BOOL, HANDLE, DWORD64, PVOID, DWORD, ctypes.POINTER(DWORD))
def readmem(hProcess, base, buf, size, nread):
    read_stats['attempts'] += 1
    try:
        try:
            data = reader.read(int(base), int(size))
        except Exception:
            data = read_module_image(int(base), int(size))
        ctypes.memmove(buf, data, len(data))
        nread[0] = len(data)
        read_stats['ok'] += 1
        return True
    except Exception as e:
        nread[0] = 0
        read_stats['fail'] += 1
        read_stats['last_error'] = repr(e)
        read_stats['last_range'] = [hex(int(base)), int(size)]
        return False
@ctypes.WINFUNCTYPE(PVOID, HANDLE, DWORD64)
def fta(hProcess, base):
    return D.SymFunctionTableAccess64(hProcess, base)
mods = list(f.modules.modules)
@ctypes.WINFUNCTYPE(DWORD64, HANDLE, DWORD64)
def getbase(hProcess, addr):
    a = int(addr)
    for m in mods:
        if m.baseaddress <= a < m.endaddress:
            return int(m.baseaddress)
    return 0

opts = 0x00000002 | 0x00000004 | 0x00000010 | 0x00080000 | 0x00000080 # UNDNAME, DEFERRED_LOADS, LOAD_LINES, FAIL_CRITICAL_ERRORS, NO_PROMPTS
D.SymSetOptions(opts)
pdb_dirs = [
    os.path.dirname(os.path.abspath(sys.argv[3])) if len(sys.argv)>3 else '',
    r'D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-native-reset-stability\.cache\native-reset-stability\plugin-build\RelWithDebInfo',
    r'D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261015-native-group-composition\.cache\native-group-composition\upstream-build-25ed780b7f392a1690dfca929cd57679959cf3ad\libobs\RelWithDebInfo',
    r'D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-native-reset-stability\.cache\native-reset-stability\private-runtime-exact-20261017T155459Z\runtime\bin\64bit',
    r'C:\Windows\System32'
]
search_path = ';'.join(x for x in pdb_dirs if x and os.path.isdir(x))
print(f'SYMBOL_PATH={search_path}')
if not D.SymInitializeW(proc, search_path, False):
    print(f'SymInitialize failed winerr={ctypes.get_last_error()}')

loaded=[]
for m in mods:
    path=m.name
    if os.path.isfile(path):
        hmod=D.SymLoadModuleExW(proc, None, path, None, int(m.baseaddress), int(m.size), None, 0)
        if hmod:
            loaded.append((os.path.basename(path), hex(int(hmod)), hex(int(m.baseaddress))))
print(f'MODULES={len(mods)} LOADED={len(loaded)}')

symbuf=ctypes.create_string_buffer(4096)
sym=ctypes.cast(symbuf, ctypes.POINTER(SYMBOL_INFO))
sym.contents.SizeOfStruct = ctypes.sizeof(SYMBOL_INFO)
sym.contents.MaxNameLen = 1500
def getsym(addr):
    symbuf.raw = b'\0'*4096
    sym.contents.SizeOfStruct = ctypes.sizeof(SYMBOL_INFO)
    sym.contents.MaxNameLen = 1500
    disp=ctypes.c_uint64(0)
    if D.SymFromAddrW(proc, int(addr), ctypes.byref(disp), sym):
        name = symbuf.raw[SYMBOL_INFO.Name.offset:SYMBOL_INFO.Name.offset + 2*sym.contents.NameLen].decode('utf-16le', errors='replace')
        return f'{name} + 0x{disp.value:x}'
    return f'<no-symbol err={ctypes.get_last_error()}>'
def modname(addr):
    a=int(addr)
    for m in mods:
        if m.baseaddress <= a < m.endaddress:
            return f'{os.path.basename(m.name)}+0x{a-m.baseaddress:x}'
    return '<no-module>'
def show(addr):
    print(f'  {hex(int(addr))} {modname(addr)} {getsym(addr)}')
print('INITIAL PC:')
show(rip)

frame=STACKFRAME64()
for a_name, a_val in [('AddrPC',rip), ('AddrStack',rsp), ('AddrFrame',rbp)]:
    a=getattr(frame,a_name)
    a.Offset=int(a_val); a.Mode=3
# verify the dump supplies memory at rsp (stack starts there)
try:
    print(f'STACK_SAMPLE={reader.read(rsp, 64).hex()}')
except Exception as e:
    print(f'STACK_SAMPLE_ERROR={e!r}')
walk=D.StackWalk64
walk.argtypes=[DWORD, HANDLE, HANDLE, ctypes.POINTER(STACKFRAME64), PVOID, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
walk.restype=BOOL
MACHINE_AMD64=0x8664
print('STACKWALK:')
ctx_buffer = ctypes.create_string_buffer(ctxraw)
show(frame.AddrPC.Offset)
for i in range(1,80):
    prev=(frame.AddrPC.Offset, frame.AddrStack.Offset, frame.AddrFrame.Offset, frame.AddrReturn.Offset)
    ok=walk(MACHINE_AMD64, proc, thr, ctypes.byref(frame), ctypes.cast(ctx_buffer, PVOID), readmem, fta, getbase, None)
    now=(frame.AddrPC.Offset, frame.AddrStack.Offset, frame.AddrFrame.Offset, frame.AddrReturn.Offset)
    print(f'walk[{i}] ok={bool(ok)} prev={[hex(x) for x in prev]} now={[hex(x) for x in now]} err={ctypes.get_last_error()}')
    if not ok or frame.AddrPC.Offset == 0 or now == prev:
        break
    show(frame.AddrPC.Offset)
print(f'READM={read_stats} image_reads={image_read_count[0]}')
print('RELEVANT_MODULES:')
for m in mods:
    base=os.path.basename(m.name).lower()
    if base in ('obs.dll','pulsar-capture-effects.dll','pulsar.exe','ntdll.dll','kernelbase.dll','kernel32.dll','obs.pdb'):
        print(os.path.basename(m.name),hex(int(m.baseaddress)),hex(int(m.endaddress)))








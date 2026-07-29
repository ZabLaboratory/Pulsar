# pulsar-nv-secure-load

Header-only. One header, `pulsar-nv-secure-load.h`, holding the whole of
Pulsar's answer to *where may an NVIDIA Maxine SDK DLL or model come from,
and is one there right now*.

Issue **#167**, Prism **ADR 023 Amendment 3** (§A3.1 override, §A3.4
invariant).

## Why it is not just "part of nv-filters"

`nv-filters` lives in `upstream/` and is patched, not owned. Three separate
builds need the *same* rules, and a rule that exists in three copies is a
rule that will drift:

| Consumer | What it uses it for |
|---|---|
| `upstream/plugins/nv-filters/` (via `patches/0003-*.patch`) | gates `obs_module_load()` on the probe; loads both SDKs and `nvcuda.dll` through it |
| `plugins/pulsar-multi-stream/` | publishes the probe in the capability manifest (`capabilities.nv_filters`) |
| `tests/nv-probe/` | the CTest gate that proves the confinement with no GPU and no SDK |

## The three rules

**Nothing but the designated directory.** Every load is
`LoadLibraryExW(<absolute path>, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
LOAD_LIBRARY_SEARCH_SYSTEM32)`. The absolute path stops the *first-level*
DLL being taken from `pulsar.exe`'s directory — which is where Windows'
standard order looks first, ahead of `SetDllDirectory`, and which is
writable in a per-user install. `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR` is what
stops that DLL's *own* imports (CUDA, TensorRT) being taken from there
afterwards. `LOAD_LIBRARY_SEARCH_DEFAULT_DIRS` would re-admit the
application directory and must never be substituted.

**No SDK, no module.** `pulsar_nv_module_should_load()` is false unless the
probe found a valid directory, the DLLs, the version minima (VFX ≥ 0.7.6,
AFX ≥ 1.6.1.2) and the three AFX `.trtpkg` models. `obs_module_load()` then
refuses, so neither SDK loader ever runs. The usual state — no SDK
installed — is inert by construction rather than by choice of directory.

An unreadable version is reported **absent**, never assumed sufficient, and
absence never satisfies the minimum.

**Only a directory this process cannot write to.** The threat is an attacker
running *as the operator*, i.e. with this process's own token, so an
operator-writable directory is one they can stock — whatever its name and
whoever designated it. `pulsar_nv_dir_is_writable_by_us()` settles it by
asking the kernel the same question the attacker would (create a file,
`CREATE_NEW` + `DELETE_ON_CLOSE`), and **fails closed** on any error that is
not an explicit denial. It applies to the SDK root *and* to `models\`
separately: a writable `models\` under a locked root would leave the
deserialised `.trtpkg` files exactly as exposed as before.

Related: the VFX default-install fallback resolves its root through
`SHGetKnownFolderPath(FOLDERID_ProgramFiles)`, **never `%ProgramFiles%`** —
Amendment 3 §A3.4 rules an environment read out of candidate-path
resolution, and a parent process chooses what that variable says.

## Rolling this back

`docs/runbooks/nv-filters-rollback.md`.

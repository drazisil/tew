# expsrv.dll static analysis — DAO-3075 follow-on crash (EIP -> 0x0003049c)

## Session note: how this analysis was done

This Ghidra MCP session only had a narrow, pre-approved tool allowlist
(`dump_bytes`, `decompile_function`, `get_references_to`, `switch_active_program`,
`list_functions`) — `list_projects`, `switch_active_project`, `create_project`, and
critically `import_and_analyze` were all denied ("haven't granted it yet") with no
way to grant them non-interactively. The `expsrv.dll` program was already present
and active in the project (`switch_active_program` succeeded immediately), but
**auto-analysis had never been run on it**: `list_functions` returns `[]` for every
filter, and `decompile_function` fails ("No function at ...") for every address,
including the entry point symbol. `get_references_to` likewise returns nothing
because it depends on the same disassembly/xref database that was never built.

So **no Ghidra decompilation was available** for this task. Everything below was
produced by manually reading raw bytes with `dump_bytes` and hand-disassembling
x86, then cross-checking against the raw PE header fields (also read via
`dump_bytes`) to compute the real image base. I'm confident in the byte-level
findings (they're directly observed, cross-checked twice for the image base, and
the call target / return address arithmetic was verified against the task's given
addresses exactly). I was not able to produce actual Ghidra-decompiled C, and I
was not able to fully reverse the enclosing caller function (`FunctionA` below) —
I only traced the specific call chain relevant to the crash.

(Note: this report was also meant to be written to `/tmp/expsrv_analysis.md` per
the task instructions, but writing to `/tmp` was denied by this session's
permission system the same way the Ghidra project/analysis tools were. It's saved
here in the project directory instead.)

## Image base

Confirmed **two independent ways**:
1. Byte-signature search: `4D 5A` ("MZ") found at `0x0F9C0000`.
2. PE header `IMAGE_OPTIONAL_HEADER32.ImageBase` field (at PE-header-relative
   offset `0x1C`) reads `00 00 9c 0f` little-endian = `0x0F9C0000`.
3. Sanity check: `AddressOfEntryPoint` RVA = `0x00026a70`; `base + RVA` =
   `0x0F9C0000 + 0x26a70 = 0x0F9E6A70`, and the bytes there
   (`53 55 56 8b 74 24 14 ...`) are exactly the classic MSVC
   `_DllMainCRTStartup` prologue (`push ebx; push ebp; push esi; mov esi,[esp+14]`).

**image_base = 0x0F9C0000**

Computed addresses (matches the task's RVAs, applied to this base):
- **static_address_1 = 0x0F9C0000 + 0x1CBD7 = `0x0F9DCBD7`**
- **static_address_2 = 0x0F9C0000 + 0x9D1B = `0x0F9C9D1B`**

## PE layout (for reference, hand-parsed from the header)

- `NumberOfSections` = 6
- `.text`   RVA `0x1000`–`0x344b2` (code)
- `ENGINE`  RVA `0x35000`–`0x41de5` (custom code section, `CODE|EXECUTE|READ`)
- `.rdata`  RVA `0x42000`–`0x4b2aa` (contains the real IAT arrays, `0x42000`–`0x42374`)
- `.data`   RVA `0x4c000`–`0x51d34`, raw-data-backed portion `0x4c000`–`0x50000`
  (the rest is BSS-style, not file-backed)
- `.rsrc`, `.reloc` follow

Import Table directory: RVA `0x453e0`, 6 `IMAGE_IMPORT_DESCRIPTOR` entries (5 real
DLLs + null terminator) — hand-parsed but not the focus of this report.

## The call chain: static_address_1 -> static_address_2

### Site of static_address_1 (0x0F9DCBD7)

Return address `0x0F9DCBD7` sits immediately after a `CALL rel32` at
`0x0F9DCBD2`:

```
0f9dcbc8: b9 e8 ff a0 0f          mov ecx, 0x0FA0FFE8      ; ecx = some global object ptr
0f9dcbcd: ff 74 24 10             push dword ptr [esp+0x10]  ; arg: a stack value from caller of FunctionA
0f9dcbd2: e8 12 d1 fe ff          call 0x0F9C9CE9            ; <-- calls FunctionB
0f9dcbd7: 56                      push esi                    ; <-- static_address_1 (return addr)
```

I was not able to fully identify what `FunctionA` (whose body contains
`0x0F9DCAC0`–at least `0x0F9DCBD7`) represents overall — it's a large routine that
does critical-section-style `call [IAT]` pairs and heap-ish global bookkeeping
before reaching this call — but the call itself is unambiguous: it's an
almost-certainly-`__thiscall` call into the function at `0x0F9C9CE9`, passing one
stack argument (from `[esp+0x10]` in the caller's frame) as the second parameter.
`ecx` (the `this` pointer for the callee) is loaded a few instructions earlier from
`mov ecx, [0x0FA0FFE8]` — another global object pointer, structurally similar to
the `0x0FA0FEEC..0x0FA0FEF8` table found below, so this whole region of `.data`
(`0x0FA0FF..`/`0x0FA0FE..`) looks like a block of cached singleton/interface state
for expsrv.dll.

### Function at 0x0F9C9CE9 (contains static_address_2)

Hand-disassembled (confirmed byte-exact against `dump_bytes` output), this is a
"get-or-lazily-load-and-cache an ITypeLib interface" helper:

```c
// __thiscall FunctionB(this=ecx, ITypeLib **pptlib /* [ebp+8] */) -> HRESULT (eax)
HRESULT FunctionB(This *this, ITypeLib **pptlib)
{
    if (this->cachedTypeLib != NULL) {        // this+0x28
        *pptlib = this->cachedTypeLib;
        goto addref_and_return;
    }

    char local[4];                             // [ebp-4], really an LPCOLESTR / path-ish value
    int hr = sub_0F9C9C73(&local);              // internal helper; fills `local`
    if (hr < 0)
        return hr;                              // propagate failure

    // *** THE CRASH SITE ***
    HRESULT hr2 = (*(HRESULT (__stdcall **)(void*, ULONG, ITypeLib**))0x0FA0FEF0)
                      (local, /*regkind=*/2, pptlib);
    if (hr2 < 0)
        return hr2;

    this->cachedTypeLib = *pptlib;

addref_and_return:
    (*(void (__stdcall **)(void*))(*(void***)this->cachedTypeLib))
        [1](this->cachedTypeLib);               // IUnknown::AddRef via vtbl slot 1
    return 0;                                    // S_OK
}
```

Raw instructions for the crash call:

```
0f9c9d0c: 8b 7d 08                mov edi, [ebp+8]        ; edi = pptlib (out param)
0f9c9d0f: 57                      push edi
0f9c9d10: 6a 02                   push 2                   ; regkind = REGKIND_NONE
0f9c9d12: ff 75 fc                push dword ptr [ebp-4]   ; szFile
0f9c9d15: ff 15 f0 fe a0 0f       call dword ptr [0x0FA0FEF0]  ; <-- indirect call
0f9c9d1b: 85 c0                   ; static_address_2, immediately after the call
```

`0x0F9C9D15` + 6 bytes = `0x0F9C9D1B` = **static_address_2, exact match**. So
static_address_2 is the return address of the very call that is the prime crash
suspect, and it lives inside the function called from static_address_1's site —
confirming both addresses are two frames of the *same* real call chain.

Argument shape (`szFile`, `REGKIND_NONE`, `ITypeLib**`) plus the fact that the
resolved-function's name (see below) is `LoadTypeLibEx` means this is precisely
the real Win32 `OLEAUT32.LoadTypeLibEx(LPCOLESTR szFile, REGKIND regkind,
ITypeLib **pptlib)` API, called through a cached function pointer instead of a
normal static import.

## Root cause: the cached pointer is populated by a fallible GetProcAddress chain, and unconditionally trusted afterward

`0x0FA0FEF0` is **not an IAT slot**. It's RVA `0x4FEF0`, which falls inside the
writable `.data` section (`0x4c000`–`0x51d34`, and specifically within the
file-backed part `0x4c000`–`0x50000`). Its on-disk value is `00 00 00 00` — i.e.
it's a genuine global variable, statically zero-initialized, meant to be filled in
by code at runtime — not populated by the PE loader's import binding.

I scanned the entirety of `.text` (RVA `0x1000`–`0x344b2`) and `ENGINE` (RVA
`0x35000`–`0x41de5`) — the only two executable sections — for any absolute-address
reference to `0x0FA0FEF0`. There is exactly **one** write site, at `0x0F9C8265`:

```
; hOleaut32 = LoadLibraryA("oleaut32.dll")   [earlier in this same function, via
;   a real static import: call dword ptr [0x0FA02194] -> mov edi, eax; test edi,edi;
;   jz <bail, skip everything below>]
; esi = GetProcAddress  [also a real static import, loaded once: mov esi,[0x0FA02190]]

0f9c822a: push "oleaut32.dll"        ; 0x0FA02B4C
0f9c822f: call [0x0FA02194]          ; LoadLibraryA
0f9c8235: mov edi, eax               ; edi = hOleaut32
0f9c8237: test edi,edi
0f9c8239: jz  <bail-out, +0x47a>     ; if LoadLibraryA failed, skip ALL of the below

0f9c8240: mov esi, [0x0FA02190]      ; esi = GetProcAddress (real static import)

0f9c8246: push "DispCallFunc"        ; 0x0FA02B3C
0f9c824b: push edi                   ; hOleaut32
0f9c824c: call esi                   ; GetProcAddress(hOleaut32, "DispCallFunc")
0f9c824e: test eax,eax
0f9c8250: mov [0x0FA0FEEC], eax      ; store result (possibly NULL!) unconditionally
0f9c8255: jz  <bail, +0x45f>          ; if it was NULL, skip the rest below

0f9c825b: push "LoadTypeLibEx"       ; 0x0FA02B2C
0f9c8260: push edi
0f9c8262: call esi                   ; GetProcAddress(hOleaut32, "LoadTypeLibEx")
0f9c8264: test eax,eax
0f9c8266: mov [0x0FA0FEF0], eax      ; <<< OUR TARGET SLOT
0f9c826b: jz  <bail, +0x44a>

0f9c8271: push "UnRegisterTypeLib"   ; 0x0FA02B18
0f9c8276: push edi
0f9c8277: call esi
0f9c8279: test eax,eax
0f9c827b: mov [0x0FA0FEF4], eax
0f9c8280: jz  <bail, +0x435>

0f9c8286: push "CreateTypeLib2"      ; 0x0FA02B08
0f9c828b: push edi
0f9c828c: call esi
0f9c828e: test eax,eax
0f9c8290: mov [0x0FA0FEF8], eax
             ... (pattern continues for more entries, not fully traced)
```

Strings (read directly from `.rdata`/`.data`, confirmed byte-exact):
- `0x0FA02B3C` = `"DispCallFunc"`
- `0x0FA02B2C` = `"LoadTypeLibEx"`   <- resolves into our crash slot `0x0FA0FEF0`
- `0x0FA02B18` = `"UnRegisterTypeLib"`
- `0x0FA02B08` = `"CreateTypeLib2"`
- `0x0FA02B4C` = `"oleaut32.dll"`

So expsrv.dll does **not** statically import `LoadTypeLibEx` / `DispCallFunc` /
`UnRegisterTypeLib` / `CreateTypeLib2` from OLEAUT32.DLL. Instead it manually
`LoadLibraryA("oleaut32.dll")` + `GetProcAddress()`s each of them one at a time
into a small table of global function-pointer slots (`0x0FA0FEEC`, `0x0FA0FEF0`,
`0x0FA0FEF4`, `0x0FA0FEF8`, ...), and if *any* `GetProcAddress` call in the chain
returns NULL, execution branches away from the remaining resolutions — leaving
every slot from that point on permanently zero.

**This is the bug's mechanism**: `FunctionB` (the "get/lazily-load ITypeLib"
helper above) calls through `[0x0FA0FEF0]` unconditionally, with **no runtime
check that the pointer is non-NULL** — it trusts that initialization already
happened successfully. On real Windows, `LoadTypeLibEx` has existed as an
OLEAUT32 export since Windows 95/NT4 and this resolution essentially can never
fail, so that trust is safe there. In the tew emulator, if `GetProcAddress` for
`"LoadTypeLibEx"` against the emulator's own faked/emulated `oleaut32.dll` module
doesn't resolve to a real handler address (e.g. because tew's `oleaut32_handlers.py`
hasn't registered an export table entry for that name, unlike `DispCallFunc`,
which the project's recent commit history shows was *just* implemented), the
`GetProcAddress` emulation must be returning 0/NULL for it. That NULL gets stored
into `0x0FA0FEF0`, execution takes the `jz` bail-out (skipping `UnRegisterTypeLib`
and `CreateTypeLib2` resolution too), and the *next* time some VBA
expression/type-info code path in expsrv.dll needs a type library — i.e. exactly
the `MSJET35 -> expsrv` call chain this crash is on — `FunctionB` calls through
the still-zero `[0x0FA0FEF0]` pointer with no guard, jumping into invalid/garbage
memory (consistent with the observed `EIP = 0x0003049c`, all-zero-byte target).

## What I confirmed vs. what remains open

**Confirmed (directly observed in bytes, cross-checked):**
- Real image base `0x0F9C0000` (two independent methods).
- static_address_1 and static_address_2 both land exactly where the task
  predicted, and are two frames of one real call chain.
- The instruction at `0x0F9C9D15` is `CALL DWORD PTR [0x0FA0FEF0]` — an indirect
  call through a *writable global* (not an IAT slot).
- `0x0FA0FEF0` is zero-initialized on disk (part of `.data`, not BSS-gap).
- The only site anywhere in `.text`/`ENGINE` that writes to `0x0FA0FEF0` is the
  `GetProcAddress(hOleaut32, "LoadTypeLibEx")` result store at `0x0F9C8266`.
- The call signature at the crash site (`szFile`, `REGKIND_NONE`, `ITypeLib**`)
  matches `OLEAUT32.LoadTypeLibEx` exactly, consistent with the resolved name.
- No NULL-check on `[0x0FA0FEF0]` exists between the resolution site and the
  crash call site (confirmed by reading `FunctionB` end-to-end).

**Not confirmed / open:**
- I could not get Ghidra to actually decompile these functions (no analysis run
  possible this session) — the C above is my own hand-transcription of the
  disassembly, not Ghidra's decompiler output. It should be re-verified with a
  real Ghidra decompile once `import_and_analyze` / project-switching permissions
  are available in an interactive session.
- I did not fully reverse `FunctionA` (`0x0F9DCAC0`+) or `sub_0F9C9C73` (the
  helper called at `0x0F9C9D03` that fills the `szFile`-like local) — only the
  parts of `FunctionA` immediately around the `static_address_1` call site.
- I did not confirm from the emulator side (tew's `oleaut32_handlers.py` /
  `import_resolver.py`) that `GetProcAddress(..., "LoadTypeLibEx")` is in fact
  what returns NULL — that's an inference from the static analysis (the only
  thing that can leave `0x0FA0FEF0` at zero), not something I traced in the
  Python emulator source. The next step is to check tew's oleaut32 export table
  for whether `"LoadTypeLibEx"` is registered, since `"DispCallFunc"` (the slot
  right before it, `0x0FA0FEEC`) was apparently resolved successfully, given
  execution reaches the second `GetProcAddress` call at all.
- I did not verify what happens on the `LoadLibraryA("oleaut32.dll")`-fails path,
  nor what the several further `GetProcAddress` resolutions after
  `CreateTypeLib2` (the pattern clearly continues past `0x0F9C8290`) resolve —
  only the four listed above were read.

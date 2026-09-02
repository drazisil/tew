"""Tests for wsock32.dll's ordinal->name aliasing table.

Real bug found 2026-09-02 chasing a live MessagePool::Get(NULL) crash on
DBThread: MCity_d.exe imports wsock32.dll functions strictly by ordinal
(confirmed via its real PE import table -- every entry shows Hint=<none>,
Member-Name=<none>), but `gethostbyname` (real ordinal 52) and
`gethostname` (real ordinal 57) were both fully implemented and registered
by *name* only, missing from `ordinal_map` -- the exact same "registered
under the wrong GetProcAddress key" bug class this project already fixed
once for oleaut32.dll's LoadTypeLibEx (2026-08-21). The game's own ordinal
IAT lookup for gethostname (ordinal 57) is what surfaced this live, as an
"[UNIMPLEMENTED] wsock32.dll!Ordinal #57" halt on DBThread, right as it
tries to actually connect to a server for the first time.

Real ordinals confirmed via `objdump -p .../wsock32.dll`'s export table,
cross-referenced against MCity_d.exe's own real import table (which only
ever references ordinals, never names, for this DLL).
"""
from __future__ import annotations

from tew.api._state import CRTState
from tew.api.win32_handlers import Win32Handlers
from tew.api.wsock32_handlers import register_wsock32_handlers
from tew.hardware.memory import Memory

MEM_SIZE = 4 * 1024 * 1024


def _env():
    mem = Memory(MEM_SIZE)
    state = CRTState()
    stubs = Win32Handlers(mem)
    register_wsock32_handlers(stubs, mem, state)
    return stubs


# Ordinal -> real export name, confirmed via `objdump -p wsock32.dll`.
# The two the game actually imports by ordinal (52, 57) are called out
# separately below; the rest of this contiguous block (51, 53-56) share
# the same real ordinal range and get the same treatment for completeness.
REAL_ORDINALS = {
    51: "gethostbyaddr",
    52: "gethostbyname",
    53: "getprotobyname",
    54: "getprotobynumber",
    55: "getservbyname",
    56: "getservbyport",
    57: "gethostname",
}


class TestOrdinalsAliasToTheCorrectNamedHandler:

    def test_every_real_ordinal_is_registered(self):
        stubs = _env()
        for ordinal, name in REAL_ORDINALS.items():
            key = f"wsock32.dll!Ordinal #{ordinal}"
            assert key in stubs._handlers, f"ordinal {ordinal} ({name}) not registered"

    def test_each_ordinal_resolves_to_its_real_named_handler(self):
        # Not just "registered" -- must be the SAME handler function as the
        # real name, not a coincidentally-present but wrong one.
        stubs = _env()
        for ordinal, name in REAL_ORDINALS.items():
            ordinal_handler = stubs._handlers[f"wsock32.dll!Ordinal #{ordinal}"].handler
            named_handler = stubs._handlers[f"wsock32.dll!{name}"].handler
            assert ordinal_handler is named_handler, (
                f"ordinal {ordinal} does not alias to {name}'s real handler"
            )

    def test_ws2_32_gets_the_same_ordinal_aliases(self):
        # register_wsock32_handlers registers under both DLL names -- the
        # game only ever imports wsock32.dll here, but ws2_32.dll shouldn't
        # silently diverge if something else starts using it by ordinal.
        stubs = _env()
        for ordinal, name in REAL_ORDINALS.items():
            ordinal_handler = stubs._handlers[f"ws2_32.dll!Ordinal #{ordinal}"].handler
            named_handler = stubs._handlers[f"ws2_32.dll!{name}"].handler
            assert ordinal_handler is named_handler

    def test_gethostname_specifically_the_ordinal_the_game_imports(self):
        # The exact ordinal MCity_d.exe's real IAT references (confirmed via
        # objdump -p MCity_d.exe) -- this is the one that halted live.
        stubs = _env()
        assert "wsock32.dll!Ordinal #57" in stubs._handlers
        assert (stubs._handlers["wsock32.dll!Ordinal #57"].handler
                is stubs._handlers["wsock32.dll!gethostname"].handler)

    def test_gethostbyname_specifically_the_ordinal_the_game_imports(self):
        stubs = _env()
        assert "wsock32.dll!Ordinal #52" in stubs._handlers
        assert (stubs._handlers["wsock32.dll!Ordinal #52"].handler
                is stubs._handlers["wsock32.dll!gethostbyname"].handler)

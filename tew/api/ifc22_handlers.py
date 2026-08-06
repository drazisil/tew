"""IFC22.dll (ImmVersion Force Feedback) handler stubs.

No FFB hardware in this emulator. Initialize returns 0 so the entire FFB
code path in cMouseFFB::cMouseFFB() is skipped. Constructors/destructors
are no-ops since nothing is allocated (Initialize=0 means no vtable
method is ever dispatched on these objects).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu_zig import ZigCPU as CPU
    from tew.hardware.memory import Memory
    from tew.api.win32_handlers import Win32Handlers

from tew.hardware.cpu_zig import EAX, ECX
from tew.api.win32_handlers import cleanup_stdcall
from tew.logger import logger


def register_ifc22_handlers(stubs: "Win32Handlers", memory: "Memory") -> None:
    """Register all IFC22.dll stubs."""

    def _ctor_noop(cpu: "CPU") -> None:
        cpu.regs[EAX] = cpu.regs[ECX]

    def _dtor_noop(cpu: "CPU") -> None:
        pass

    def _initialize(cpu: "CPU") -> None:
        logger.info("handlers", "[ifc22] CImmMouse::Initialize -> 0 (no FFB hardware)")
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    def _halt_ffb(name: str):
        def _h(cpu: "CPU") -> None:
            logger.error("handlers", f"[UNIMPLEMENTED] ifc22 FFB method {name} — halting")
            cpu.halted = True
            cpu.fatal_halt = True
        return _h

    # Default constructors — always called by cMouseFFB::cMouseFFB()
    stubs.register_handler("ifc22.dll", "??0CImmMouse@@QAE@XZ",    _ctor_noop)
    stubs.register_handler("ifc22.dll", "??0CImmProject@@QAE@XZ",  _ctor_noop)
    stubs.register_handler("ifc22.dll", "??0CImmPeriodic@@QAE@XZ", _ctor_noop)

    # Initialize — called unconditionally; returns 0 to disable all FFB
    stubs.register_handler("ifc22.dll",
        "?Initialize@CImmMouse@@QAEHPAX0KH@Z", _initialize)

    # Destructors — called when cMouseFFB is destroyed
    stubs.register_handler("ifc22.dll", "??1CImmMouse@@UAE@XZ",    _dtor_noop)
    stubs.register_handler("ifc22.dll", "??1CImmProject@@QAE@XZ",  _dtor_noop)
    stubs.register_handler("ifc22.dll", "??1CImmPeriodic@@UAE@XZ", _dtor_noop)

    # FFB device control — only reachable if Initialize succeeds (it never does)
    stubs.register_handler("ifc22.dll",
        "?UsesWin32MouseServices@CImmDevice@@QAEHH@Z",
        _halt_ffb("UsesWin32MouseServices"))
    stubs.register_handler("ifc22.dll",
        "?OpenFile@CImmProject@@QAEHPBDPAVCImmDevice@@@Z",
        _halt_ffb("OpenFile"))
    stubs.register_handler("ifc22.dll",
        "?Start@CImmProject@@QAEHPBDKKPAVCImmDevice@@@Z",
        _halt_ffb("Start"))
    stubs.register_handler("ifc22.dll",
        "?ChangeParameters@CImmPeriodic@@QAEHKKKJJJKPAUFEELIT_ENVELOPE@@@Z",
        _halt_ffb("ChangeParameters"))

"""String instructions: STOS, MOVS, LODS, SCAS, CMPS (byte and dword/word variants)."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import EAX, ECX, ESI, EDI, DF_BIT, ZF_BIT
from tew.emulator.opcodes._helpers import u32

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def _dir(cpu: "CPU") -> int:
    """Return +1 (DF=0, forward) or -1 (DF=1, backward)."""
    return -1 if cpu.get_flag(DF_BIT) else 1


def register_string_ops(cpu: "CPU") -> None:
    # STOSB — store AL to [EDI], advance EDI by 1
    def _stosb(cpu: "CPU") -> None:
        if cpu.rep_prefix == "REP":
            while u32(cpu.regs[ECX]) != 0:
                cpu.memory.write8(cpu.regs[EDI], cpu.regs[EAX] & 0xFF)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        else:
            cpu.memory.write8(cpu.regs[EDI], cpu.regs[EAX] & 0xFF)
            cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
    cpu.register(0xAA, _stosb)

    # STOSD / STOSW — store EAX (32-bit) or AX (16-bit with 0x66) to [EDI]
    def _stosd(cpu: "CPU") -> None:
        wide = not cpu.operand_size_override   # True = STOSD (32-bit)
        step = 4 if wide else 2
        d = _dir(cpu) * step
        if cpu.rep_prefix == "REP":
            while u32(cpu.regs[ECX]) != 0:
                if wide:
                    cpu.memory.write32(cpu.regs[EDI], cpu.regs[EAX])
                else:
                    cpu.memory.write16(cpu.regs[EDI], cpu.regs[EAX] & 0xFFFF)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        else:
            if wide:
                cpu.memory.write32(cpu.regs[EDI], cpu.regs[EAX])
            else:
                cpu.memory.write16(cpu.regs[EDI], cpu.regs[EAX] & 0xFFFF)
            cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
    cpu.register(0xAB, _stosd)

    # MOVSB — copy byte [ESI] → [EDI], advance both by 1
    def _movsb(cpu: "CPU") -> None:
        if cpu.rep_prefix == "REP":
            while u32(cpu.regs[ECX]) != 0:
                cpu.memory.write8(cpu.regs[EDI], cpu.memory.read8(cpu.regs[ESI]))
                cpu.regs[ESI] = u32(cpu.regs[ESI] + _dir(cpu))
                cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        else:
            cpu.memory.write8(cpu.regs[EDI], cpu.memory.read8(cpu.regs[ESI]))
            cpu.regs[ESI] = u32(cpu.regs[ESI] + _dir(cpu))
            cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
    cpu.register(0xA4, _movsb)

    # MOVSD / MOVSW — copy dword or word [ESI] → [EDI]
    def _movsd(cpu: "CPU") -> None:
        wide = not cpu.operand_size_override
        step = 4 if wide else 2
        d = _dir(cpu) * step
        if cpu.rep_prefix == "REP":
            while u32(cpu.regs[ECX]) != 0:
                if wide:
                    cpu.memory.write32(cpu.regs[EDI], cpu.memory.read32(cpu.regs[ESI]))
                else:
                    cpu.memory.write16(cpu.regs[EDI], cpu.memory.read16(cpu.regs[ESI]))
                cpu.regs[ESI] = u32(cpu.regs[ESI] + d)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        else:
            if wide:
                cpu.memory.write32(cpu.regs[EDI], cpu.memory.read32(cpu.regs[ESI]))
            else:
                cpu.memory.write16(cpu.regs[EDI], cpu.memory.read16(cpu.regs[ESI]))
            cpu.regs[ESI] = u32(cpu.regs[ESI] + d)
            cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
    cpu.register(0xA5, _movsd)

    # LODSB — load byte [ESI] into AL, advance ESI
    def _lodsb(cpu: "CPU") -> None:
        if cpu.rep_prefix == "REP":
            while u32(cpu.regs[ECX]) != 0:
                cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | cpu.memory.read8(cpu.regs[ESI])
                cpu.regs[ESI] = u32(cpu.regs[ESI] + _dir(cpu))
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        else:
            cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFFFF00) | cpu.memory.read8(cpu.regs[ESI])
            cpu.regs[ESI] = u32(cpu.regs[ESI] + _dir(cpu))
    cpu.register(0xAC, _lodsb)

    # LODSD / LODSW — load dword or word [ESI] into EAX/AX
    def _lodsd(cpu: "CPU") -> None:
        wide = not cpu.operand_size_override
        step = 4 if wide else 2
        d = _dir(cpu) * step
        if cpu.rep_prefix == "REP":
            while u32(cpu.regs[ECX]) != 0:
                if wide:
                    cpu.regs[EAX] = cpu.memory.read32(cpu.regs[ESI])
                else:
                    cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | cpu.memory.read16(cpu.regs[ESI])
                cpu.regs[ESI] = u32(cpu.regs[ESI] + d)
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
        else:
            if wide:
                cpu.regs[EAX] = cpu.memory.read32(cpu.regs[ESI])
            else:
                cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | cpu.memory.read16(cpu.regs[ESI])
            cpu.regs[ESI] = u32(cpu.regs[ESI] + d)
    cpu.register(0xAD, _lodsd)

    # SCASB — compare AL with [EDI], advance EDI
    def _scasb(cpu: "CPU") -> None:
        rep = cpu.rep_prefix
        if rep == "REP":     # REPE: stop on mismatch
            while u32(cpu.regs[ECX]) != 0:
                val = cpu.memory.read8(cpu.regs[EDI])
                al = cpu.regs[EAX] & 0xFF
                cpu.update_flags_arith(al - val, al, val, True)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if not cpu.get_flag(ZF_BIT):
                    break
        elif rep == "REPNE": # REPNE: stop on match
            while u32(cpu.regs[ECX]) != 0:
                val = cpu.memory.read8(cpu.regs[EDI])
                al = cpu.regs[EAX] & 0xFF
                cpu.update_flags_arith(al - val, al, val, True)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if cpu.get_flag(ZF_BIT):
                    break
        else:
            val = cpu.memory.read8(cpu.regs[EDI])
            al = cpu.regs[EAX] & 0xFF
            cpu.update_flags_arith(al - val, al, val, True)
            cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
    cpu.register(0xAE, _scasb)

    # SCASD / SCASW — compare EAX/AX with [EDI]
    def _scasd(cpu: "CPU") -> None:
        wide = not cpu.operand_size_override
        step = 4 if wide else 2
        d = _dir(cpu) * step
        rep = cpu.rep_prefix

        def _read() -> int:
            return cpu.memory.read32(cpu.regs[EDI]) if wide else cpu.memory.read16(cpu.regs[EDI])

        def _acc() -> int:
            return cpu.regs[EAX] if wide else (cpu.regs[EAX] & 0xFFFF)

        if rep == "REP":
            while u32(cpu.regs[ECX]) != 0:
                val = _read(); acc = _acc()
                cpu.update_flags_arith(acc - val, acc, val, True)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if not cpu.get_flag(ZF_BIT):
                    break
        elif rep == "REPNE":
            while u32(cpu.regs[ECX]) != 0:
                val = _read(); acc = _acc()
                cpu.update_flags_arith(acc - val, acc, val, True)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if cpu.get_flag(ZF_BIT):
                    break
        else:
            val = _read(); acc = _acc()
            cpu.update_flags_arith(acc - val, acc, val, True)
            cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
    cpu.register(0xAF, _scasd)

    # CMPSB — compare [ESI] with [EDI], advance both
    def _cmpsb(cpu: "CPU") -> None:
        rep = cpu.rep_prefix
        if rep == "REP":
            while u32(cpu.regs[ECX]) != 0:
                src = cpu.memory.read8(cpu.regs[ESI])
                dst = cpu.memory.read8(cpu.regs[EDI])
                cpu.update_flags_arith(src - dst, src, dst, True)
                cpu.regs[ESI] = u32(cpu.regs[ESI] + _dir(cpu))
                cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if not cpu.get_flag(ZF_BIT):
                    break
        elif rep == "REPNE":
            while u32(cpu.regs[ECX]) != 0:
                src = cpu.memory.read8(cpu.regs[ESI])
                dst = cpu.memory.read8(cpu.regs[EDI])
                cpu.update_flags_arith(src - dst, src, dst, True)
                cpu.regs[ESI] = u32(cpu.regs[ESI] + _dir(cpu))
                cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if cpu.get_flag(ZF_BIT):
                    break
        else:
            src = cpu.memory.read8(cpu.regs[ESI])
            dst = cpu.memory.read8(cpu.regs[EDI])
            cpu.update_flags_arith(src - dst, src, dst, True)
            cpu.regs[ESI] = u32(cpu.regs[ESI] + _dir(cpu))
            cpu.regs[EDI] = u32(cpu.regs[EDI] + _dir(cpu))
    cpu.register(0xA6, _cmpsb)

    # CMPSD — compare [ESI] dword with [EDI] dword
    def _cmpsd(cpu: "CPU") -> None:
        d = _dir(cpu) * 4
        rep = cpu.rep_prefix
        if rep == "REP":
            while u32(cpu.regs[ECX]) != 0:
                src = cpu.memory.read32(cpu.regs[ESI])
                dst = cpu.memory.read32(cpu.regs[EDI])
                cpu.update_flags_arith(src - dst, src, dst, True)
                cpu.regs[ESI] = u32(cpu.regs[ESI] + d)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if not cpu.get_flag(ZF_BIT):
                    break
        elif rep == "REPNE":
            while u32(cpu.regs[ECX]) != 0:
                src = cpu.memory.read32(cpu.regs[ESI])
                dst = cpu.memory.read32(cpu.regs[EDI])
                cpu.update_flags_arith(src - dst, src, dst, True)
                cpu.regs[ESI] = u32(cpu.regs[ESI] + d)
                cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
                cpu.regs[ECX] = u32(cpu.regs[ECX] - 1)
                if cpu.get_flag(ZF_BIT):
                    break
        else:
            src = cpu.memory.read32(cpu.regs[ESI])
            dst = cpu.memory.read32(cpu.regs[EDI])
            cpu.update_flags_arith(src - dst, src, dst, True)
            cpu.regs[ESI] = u32(cpu.regs[ESI] + d)
            cpu.regs[EDI] = u32(cpu.regs[EDI] + d)
    cpu.register(0xA7, _cmpsd)

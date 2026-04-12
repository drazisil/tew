"""Group 4/5 instructions: INC/DEC/CALL/JMP/PUSH via r/m operand (0xFE, 0xFF)."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import CF_BIT
from tew.emulator.opcodes._helpers import u32

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_group5(cpu: "CPU") -> None:
    # Group 4: 0xFE — INC/DEC r/m8
    def _group4_fe(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm8_resolved(d["mod"], d["rm"])
        operand, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        if d["reg"] == 0:   # INC r/m8
            result = (operand + 1) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            saved_cf = cpu.get_flag(CF_BIT)
            cpu.update_flags_arith(operand + 1, operand, 1, False)
            cpu.set_flag(CF_BIT, saved_cf)
        elif d["reg"] == 1: # DEC r/m8
            result = (operand - 1) & 0xFF
            cpu.write_rm8_resolved(is_reg, addr, result)
            saved_cf = cpu.get_flag(CF_BIT)
            cpu.update_flags_arith(operand - 1, operand, 1, True)
            cpu.set_flag(CF_BIT, saved_cf)
        else:
            raise RuntimeError(f"Unsupported 0xFE /{d['reg']}")
    cpu.register(0xFE, _group4_fe)

    # Group 5: 0xFF — INC/DEC/CALL/JMP/PUSH r/m32
    def _group5_ff(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        res = cpu.read_rm32_resolved(d["mod"], d["rm"])
        operand, is_reg, addr = res["value"], res["is_reg"], res["addr"]
        reg = d["reg"]

        if reg == 0:    # INC r/m32
            result = u32(operand + 1)
            cpu.write_rmv_resolved(is_reg, addr, result)
            saved_cf = cpu.get_flag(CF_BIT)
            cpu.update_flags_arith(operand + 1, operand, 1, False)
            cpu.set_flag(CF_BIT, saved_cf)
        elif reg == 1:  # DEC r/m32
            result = u32(operand - 1)
            cpu.write_rmv_resolved(is_reg, addr, result)
            saved_cf = cpu.get_flag(CF_BIT)
            cpu.update_flags_arith(operand - 1, operand, 1, True)
            cpu.set_flag(CF_BIT, saved_cf)
        elif reg == 2:  # CALL r/m32
            cpu.push32(cpu.eip)
            cpu.eip = operand
        elif reg == 4:  # JMP r/m32
            cpu.eip = operand
        elif reg == 6:  # PUSH r/m32
            cpu.push32(operand)
        elif reg in (3, 5):
            # CALL m16:32 / JMP m16:32 far — not needed in flat model
            pass
        elif reg == 7:
            # Undefined encoding — treat as no-op
            pass
        else:
            raise RuntimeError(f"Unsupported Group 5 extension: /{reg}")
    cpu.register(0xFF, _group5_ff)

"""Two-byte opcodes (0x0F prefix): MOVZX, MOVSX, IMUL, SETcc, Jcc near, XADD,
BSR, BSF, CMOVcc, BSWAP."""

from __future__ import annotations
from typing import TYPE_CHECKING

from tew.hardware.cpu import CF_BIT, OF_BIT, ZF_BIT
from tew.emulator.opcodes._helpers import s32, u32, clz32
from tew.emulator.opcodes.control_flow import _eval_condition

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_two_byte_opcodes(cpu: "CPU") -> None:
    def _0f_dispatch(cpu: "CPU") -> None:  # noqa: C901
        op2 = cpu.fetch8()

        if op2 == 0xB6:   # MOVZX r32, r/m8
            d = cpu.decode_mod_rm()
            res = cpu.resolve_rm(d["mod"], d["rm"])
            val = cpu.regs[res["addr"]] & 0xFF if res["is_reg"] else cpu.memory.read8(cpu._apply_segment_override(res["addr"]))
            cpu.regs[d["reg"]] = val

        elif op2 == 0xB7: # MOVZX r32, r/m16
            d = cpu.decode_mod_rm()
            res = cpu.resolve_rm(d["mod"], d["rm"])
            val = cpu.regs[res["addr"]] & 0xFFFF if res["is_reg"] else cpu.memory.read16(cpu._apply_segment_override(res["addr"]))
            cpu.regs[d["reg"]] = val

        elif op2 == 0xBE: # MOVSX r32, r/m8
            d = cpu.decode_mod_rm()
            res = cpu.resolve_rm(d["mod"], d["rm"])
            val = cpu.regs[res["addr"]] & 0xFF if res["is_reg"] else cpu.memory.read8(cpu._apply_segment_override(res["addr"]))
            # Sign-extend 8→32
            cpu.regs[d["reg"]] = u32((val ^ 0x80) - 0x80)

        elif op2 == 0xBF: # MOVSX r32, r/m16
            d = cpu.decode_mod_rm()
            res = cpu.resolve_rm(d["mod"], d["rm"])
            val = cpu.regs[res["addr"]] & 0xFFFF if res["is_reg"] else cpu.memory.read16(cpu._apply_segment_override(res["addr"]))
            # Sign-extend 16→32
            cpu.regs[d["reg"]] = u32((val ^ 0x8000) - 0x8000)

        elif op2 == 0xAF: # IMUL r32, r/m32
            d = cpu.decode_mod_rm()
            op1 = s32(cpu.regs[d["reg"]])
            op2v = s32(cpu.read_rm32(d["mod"], d["rm"]))
            result32 = u32(op1 * op2v)
            cpu.regs[d["reg"]] = result32
            full = op1 * op2v
            overflow = full != s32(result32)
            cpu.set_flag(CF_BIT, overflow)
            cpu.set_flag(OF_BIT, overflow)

        elif 0x90 <= op2 <= 0x9F: # SETcc r/m8
            d = cpu.decode_mod_rm()
            res = cpu.resolve_rm(d["mod"], d["rm"])
            val = 1 if _eval_condition(cpu, op2 & 0x0F) else 0
            if res["is_reg"]:
                cpu.regs[res["addr"]] = (cpu.regs[res["addr"]] & 0xFFFFFF00) | val
            else:
                cpu.memory.write8(cpu._apply_segment_override(res["addr"]), val)

        elif 0x80 <= op2 <= 0x8F: # Jcc rel32 (near)
            rel = cpu.fetch_signed32()
            if _eval_condition(cpu, op2 & 0x0F):
                cpu.eip = u32(cpu.eip + rel)

        elif op2 == 0xC1: # XADD r/m32, r32
            d = cpu.decode_mod_rm()
            dest = cpu.read_rm32(d["mod"], d["rm"])
            src = cpu.regs[d["reg"]]
            result = u32(dest + src)
            cpu.regs[d["reg"]] = dest   # old dest → src register
            cpu.write_rm32(d["mod"], d["rm"], result)
            cpu.update_flags_arith(dest + src, dest, src, False)

        elif op2 == 0xBD: # BSR r32, r/m32 (bit scan reverse)
            d = cpu.decode_mod_rm()
            val = cpu.read_rm32(d["mod"], d["rm"])
            if val == 0:
                cpu.set_flag(ZF_BIT, True)
            else:
                cpu.set_flag(ZF_BIT, False)
                cpu.regs[d["reg"]] = (val & 0xFFFFFFFF).bit_length() - 1

        elif op2 == 0xBC: # BSF r32, r/m32 (bit scan forward)
            d = cpu.decode_mod_rm()
            val = cpu.read_rm32(d["mod"], d["rm"]) & 0xFFFFFFFF
            if val == 0:
                cpu.set_flag(ZF_BIT, True)
            else:
                cpu.set_flag(ZF_BIT, False)
                # Isolate lowest set bit, then find its position
                lowest = val & (-val & 0xFFFFFFFF)
                cpu.regs[d["reg"]] = lowest.bit_length() - 1

        elif 0x40 <= op2 <= 0x4F: # CMOVcc r32, r/m32
            d = cpu.decode_mod_rm()
            val = cpu.read_rm32(d["mod"], d["rm"])
            if _eval_condition(cpu, op2 & 0x0F):
                cpu.regs[d["reg"]] = val

        elif 0xC8 <= op2 <= 0xCF: # BSWAP r32
            r = op2 & 0x7
            v = cpu.regs[r] & 0xFFFFFFFF
            cpu.regs[r] = (
                ((v & 0xFF) << 24) |
                (((v >> 8) & 0xFF) << 16) |
                (((v >> 16) & 0xFF) << 8) |
                ((v >> 24) & 0xFF)
            )

        elif op2 == 0xA3: # BT r/m32, r32 (bit test)
            d = cpu.decode_mod_rm()
            bit = cpu.regs[d["reg"]] & 0x1F
            val = cpu.read_rm32(d["mod"], d["rm"])
            cpu.set_flag(CF_BIT, bool((val >> bit) & 1))

        elif op2 == 0xBA: # BT/BTS/BTR/BTC r/m32, imm8 (Group 8)
            d = cpu.decode_mod_rm()
            bit = cpu.fetch8() & 0x1F
            val = cpu.read_rm32(d["mod"], d["rm"])
            cpu.set_flag(CF_BIT, bool((val >> bit) & 1))
            if d["reg"] == 5:    # BTS
                cpu.write_rm32(d["mod"], d["rm"], val | (1 << bit))
            elif d["reg"] == 6:  # BTR
                cpu.write_rm32(d["mod"], d["rm"], val & ~(1 << bit))
            elif d["reg"] == 7:  # BTC
                cpu.write_rm32(d["mod"], d["rm"], (val ^ (1 << bit)) & 0xFFFFFFFF)
            # reg==4 is BT (no write)

        else:
            raise RuntimeError(
                f"Unknown two-byte opcode: 0x0F 0x{op2:02x} at EIP=0x{cpu.eip & 0xFFFFFFFF:08x}"
            )

    cpu.register(0x0F, _0f_dispatch)

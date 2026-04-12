"""x87 FPU instructions (opcodes 0xD8–0xDF)."""

from __future__ import annotations
import math
from typing import TYPE_CHECKING

from tew.hardware.cpu import EAX, ZF_BIT, CF_BIT, OF_BIT
from tew.emulator.opcodes._helpers import u32
from tew.emulator.opcodes.control_flow import _eval_condition

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_fpu(cpu: "CPU") -> None:
    # ── 0xD8: float32 memory ops / register-register ───────────────────────
    def _d8(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            st0 = cpu.fpu_get(0); sti = cpu.fpu_get(rm)
            if reg == 0: cpu.fpu_set(0, st0 + sti)       # FADD ST(0), ST(i)
            elif reg == 1: cpu.fpu_set(0, st0 * sti)     # FMUL ST(0), ST(i)
            elif reg == 2: cpu.fpu_compare(st0, sti)      # FCOM ST(i)
            elif reg == 3: cpu.fpu_compare(st0, sti); cpu.fpu_pop()  # FCOMP ST(i)
            elif reg == 4: cpu.fpu_set(0, st0 - sti)     # FSUB ST(0), ST(i)
            elif reg == 5: cpu.fpu_set(0, sti - st0)     # FSUBR ST(0), ST(i)
            elif reg == 6: cpu.fpu_set(0, st0 / sti)     # FDIV ST(0), ST(i)
            elif reg == 7: cpu.fpu_set(0, sti / st0)     # FDIVR ST(0), ST(i)
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            val = cpu.read_float(addr)
            st0 = cpu.fpu_get(0)
            if reg == 0: cpu.fpu_set(0, st0 + val)
            elif reg == 1: cpu.fpu_set(0, st0 * val)
            elif reg == 2: cpu.fpu_compare(st0, val)
            elif reg == 3: cpu.fpu_compare(st0, val); cpu.fpu_pop()
            elif reg == 4: cpu.fpu_set(0, st0 - val)
            elif reg == 5: cpu.fpu_set(0, val - st0)
            elif reg == 6: cpu.fpu_set(0, st0 / val)
            elif reg == 7: cpu.fpu_set(0, val / st0)
    cpu.register(0xD8, _d8)

    # ── 0xD9: FLD/FST/FSTP float32, FXCH, constants, misc ─────────────────
    def _d9(cpu: "CPU") -> None:  # noqa: C901
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            if reg == 0:     # FLD ST(i)
                cpu.fpu_push(cpu.fpu_get(rm))
            elif reg == 1:   # FXCH ST(i)
                tmp = cpu.fpu_get(0)
                cpu.fpu_set(0, cpu.fpu_get(rm))
                cpu.fpu_set(rm, tmp)
            elif reg == 2:   # FNOP
                pass
            elif reg == 3:   # FSTP ST(i)
                cpu.fpu_set(rm, cpu.fpu_get(0)); cpu.fpu_pop()
            elif reg == 4:   # FCHS / FABS / FTST / FXAM
                if rm == 0: cpu.fpu_set(0, -cpu.fpu_get(0))         # FCHS
                elif rm == 1: cpu.fpu_set(0, abs(cpu.fpu_get(0)))   # FABS
                elif rm == 4: cpu.fpu_compare(cpu.fpu_get(0), 0.0)  # FTST
                elif rm == 5:  # FXAM — simplified sign classification
                    cpu.fpu_status_word &= ~0x4700
                    if cpu.fpu_get(0) < 0: cpu.fpu_status_word |= 0x0200
            elif reg == 5:   # FLD constants
                consts = [1.0, math.log2(10), math.log2(math.e), math.pi,
                          math.log10(2), math.log(2), 0.0]
                if rm < len(consts):
                    cpu.fpu_push(consts[rm])
            elif reg == 6:   # F2XM1 / FYL2X / FPREM1 / FDECSTP / FINCSTP
                if rm == 0:  # F2XM1: ST(0) = 2^ST(0) - 1
                    cpu.fpu_set(0, math.pow(2, cpu.fpu_get(0)) - 1)
                elif rm == 1:  # FYL2X: ST(1) = ST(1)*log2(ST(0)), pop
                    x = cpu.fpu_get(0); y = cpu.fpu_get(1)
                    cpu.fpu_pop()
                    cpu.fpu_set(0, y * math.log2(x))
                elif rm == 5:  # FPREM1 (IEEE remainder)
                    cpu.fpu_set(0, math.fmod(cpu.fpu_get(0), cpu.fpu_get(1)))
                    cpu.fpu_status_word &= ~0x0400
                elif rm == 6:  # FDECSTP
                    cpu.fpu_top = (cpu.fpu_top - 1) & 7
                    cpu.fpu_status_word = (cpu.fpu_status_word & ~0x3800) | (cpu.fpu_top << 11)
                elif rm == 7:  # FINCSTP
                    cpu.fpu_top = (cpu.fpu_top + 1) & 7
                    cpu.fpu_status_word = (cpu.fpu_status_word & ~0x3800) | (cpu.fpu_top << 11)
            elif reg == 7:   # FPREM / FSQRT / FSINCOS / FRNDINT / FSCALE / FSIN / FCOS
                if rm == 0:  # FPREM
                    cpu.fpu_set(0, math.fmod(cpu.fpu_get(0), cpu.fpu_get(1)))
                    cpu.fpu_status_word &= ~0x0400
                elif rm == 2:  # FSQRT
                    cpu.fpu_set(0, math.sqrt(cpu.fpu_get(0)))
                elif rm == 3:  # FSINCOS
                    v = cpu.fpu_get(0)
                    cpu.fpu_set(0, math.sin(v))
                    cpu.fpu_push(math.cos(v))
                elif rm == 4:  # FRNDINT
                    cpu.fpu_set(0, round(cpu.fpu_get(0)))
                elif rm == 5:  # FSCALE
                    scale = math.trunc(cpu.fpu_get(1))
                    cpu.fpu_set(0, cpu.fpu_get(0) * math.pow(2, scale))
                elif rm == 6:  # FSIN
                    cpu.fpu_set(0, math.sin(cpu.fpu_get(0)))
                elif rm == 7:  # FCOS
                    cpu.fpu_set(0, math.cos(cpu.fpu_get(0)))
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            if reg == 0:   # FLD m32real
                cpu.fpu_push(cpu.read_float(addr))
            elif reg == 2: # FST m32real
                cpu.write_float(addr, cpu.fpu_get(0))
            elif reg == 3: # FSTP m32real
                cpu.write_float(addr, cpu.fpu_get(0)); cpu.fpu_pop()
            elif reg == 4: # FLDENV — NOP
                pass
            elif reg == 5: # FLDCW m16
                cpu.fpu_control_word = cpu.memory.read16(addr)
            elif reg == 6: # FNSTENV — NOP
                pass
            elif reg == 7: # FNSTCW m16
                cpu.memory.write16(addr, cpu.fpu_control_word)
    cpu.register(0xD9, _d9)

    # ── 0xDA: integer32 memory ops / FCMOV ────────────────────────────────
    def _da(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            if reg == 0:
                if cpu.get_flag(CF_BIT): cpu.fpu_set(0, cpu.fpu_get(rm))    # FCMOVB
            elif reg == 1:
                if cpu.get_flag(ZF_BIT): cpu.fpu_set(0, cpu.fpu_get(rm))   # FCMOVE
            elif reg == 2:
                if cpu.get_flag(CF_BIT) or cpu.get_flag(ZF_BIT): cpu.fpu_set(0, cpu.fpu_get(rm))  # FCMOVBE
            elif reg == 3:
                cpu.fpu_set(0, cpu.fpu_get(rm))                              # FCMOVU
            elif reg == 5 and rm == 1:   # FUCOMPP
                cpu.fpu_compare(cpu.fpu_get(0), cpu.fpu_get(1))
                cpu.fpu_pop(); cpu.fpu_pop()
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            val = cpu.memory.read_signed32(addr)
            st0 = cpu.fpu_get(0)
            if reg == 0: cpu.fpu_set(0, st0 + val)
            elif reg == 1: cpu.fpu_set(0, st0 * val)
            elif reg == 2: cpu.fpu_compare(st0, val)
            elif reg == 3: cpu.fpu_compare(st0, val); cpu.fpu_pop()
            elif reg == 4: cpu.fpu_set(0, st0 - val)
            elif reg == 5: cpu.fpu_set(0, val - st0)
            elif reg == 6: cpu.fpu_set(0, st0 / val)
            elif reg == 7: cpu.fpu_set(0, val / st0)
    cpu.register(0xDA, _da)

    # ── 0xDB: FILD int32, FISTP int32, FCLEX, FINIT, FUCOMI ───────────────
    def _db(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            if reg == 4:
                if rm == 2:  # FCLEX / FNCLEX
                    cpu.fpu_status_word &= 0x7F00
                elif rm == 3:  # FINIT / FNINIT
                    cpu.fpu_control_word = 0x037F
                    cpu.fpu_status_word = 0
                    cpu.fpu_tag_word = 0xFFFF
                    cpu.fpu_top = 0
            elif reg == 5:  # FUCOMI ST, ST(i)
                _fpu_comi(cpu, cpu.fpu_get(0), cpu.fpu_get(rm), pop=False)
            elif reg == 6:  # FCOMI ST, ST(i)
                _fpu_comi(cpu, cpu.fpu_get(0), cpu.fpu_get(rm), pop=False)
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            if reg == 0:   # FILD m32int
                cpu.fpu_push(float(cpu.memory.read_signed32(addr)))
            elif reg == 1: # FISTTP m32int
                cpu.memory.write32(addr, u32(int(math.trunc(cpu.fpu_get(0)))))
                cpu.fpu_pop()
            elif reg == 2: # FIST m32int
                cpu.memory.write32(addr, u32(int(round(cpu.fpu_get(0)))))
            elif reg == 3: # FISTP m32int
                cpu.memory.write32(addr, u32(int(round(cpu.fpu_get(0)))))
                cpu.fpu_pop()
            elif reg == 5: # FLD m80real (simplified 80-bit load)
                lo = cpu.memory.read32(addr)
                hi = cpu.memory.read32(addr + 4)
                exp = cpu.memory.read16(addr + 8)
                sign = -1 if (exp & 0x8000) else 1
                e = (exp & 0x7FFF) - 16383
                mantissa = (hi * 0x100000000 + lo) / 0x8000000000000000
                if e == -16383 and lo == 0 and hi == 0:
                    cpu.fpu_push(0.0 * sign)
                else:
                    cpu.fpu_push(sign * math.pow(2, e) * mantissa)
            elif reg == 7: # FSTP m80real (simplified: store as 64-bit, zero-extend)
                cpu.write_double(addr, cpu.fpu_get(0))
                cpu.memory.write16(addr + 8, 0)
                cpu.fpu_pop()
    cpu.register(0xDB, _db)

    # ── 0xDC: float64 memory ops / register-register (reversed) ───────────
    def _dc(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            st0 = cpu.fpu_get(0); sti = cpu.fpu_get(rm)
            if reg == 0: cpu.fpu_set(rm, sti + st0)    # FADD ST(i), ST(0)
            elif reg == 1: cpu.fpu_set(rm, sti * st0)  # FMUL ST(i), ST(0)
            elif reg == 2: cpu.fpu_compare(st0, sti)   # FCOM ST(i)
            elif reg == 3: cpu.fpu_compare(st0, sti); cpu.fpu_pop()
            elif reg == 4: cpu.fpu_set(rm, sti - st0)  # FSUBR ST(i), ST(0)
            elif reg == 5: cpu.fpu_set(rm, st0 - sti)  # FSUB ST(i), ST(0)
            elif reg == 6: cpu.fpu_set(rm, sti / st0)  # FDIVR ST(i), ST(0)
            elif reg == 7: cpu.fpu_set(rm, st0 / sti)  # FDIV ST(i), ST(0)
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            val = cpu.read_double(addr)
            st0 = cpu.fpu_get(0)
            if reg == 0: cpu.fpu_set(0, st0 + val)
            elif reg == 1: cpu.fpu_set(0, st0 * val)
            elif reg == 2: cpu.fpu_compare(st0, val)
            elif reg == 3: cpu.fpu_compare(st0, val); cpu.fpu_pop()
            elif reg == 4: cpu.fpu_set(0, st0 - val)
            elif reg == 5: cpu.fpu_set(0, val - st0)
            elif reg == 6: cpu.fpu_set(0, st0 / val)
            elif reg == 7: cpu.fpu_set(0, val / st0)
    cpu.register(0xDC, _dc)

    # ── 0xDD: FLD/FST/FSTP float64, FRSTOR, FSAVE, FUCOM, FUCOMP, FFREE ──
    def _dd(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            if reg == 0:   # FFREE ST(i)
                cpu.fpu_tag_word |= (3 << (((cpu.fpu_top + rm) & 7) * 2))
            elif reg == 2: # FST ST(i)
                cpu.fpu_set(rm, cpu.fpu_get(0))
            elif reg == 3: # FSTP ST(i)
                cpu.fpu_set(rm, cpu.fpu_get(0)); cpu.fpu_pop()
            elif reg == 4: # FUCOM ST(i)
                cpu.fpu_compare(cpu.fpu_get(0), cpu.fpu_get(rm))
            elif reg == 5: # FUCOMP ST(i)
                cpu.fpu_compare(cpu.fpu_get(0), cpu.fpu_get(rm)); cpu.fpu_pop()
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            if reg == 0:   # FLD m64real
                cpu.fpu_push(cpu.read_double(addr))
            elif reg == 1: # FISTTP m64int
                cpu.write_double(addr, math.trunc(cpu.fpu_get(0)))
                cpu.fpu_pop()
            elif reg == 2: # FST m64real
                cpu.write_double(addr, cpu.fpu_get(0))
            elif reg == 3: # FSTP m64real
                cpu.write_double(addr, cpu.fpu_get(0)); cpu.fpu_pop()
            elif reg == 4: # FRSTOR — NOP
                pass
            elif reg == 6: # FNSAVE — NOP
                pass
            elif reg == 7: # FNSTSW m16
                cpu.memory.write16(addr, cpu.fpu_status_word)
    cpu.register(0xDD, _dd)

    # ── 0xDE: FADDP/FMULP/FCOMPP/FSUBP/FSUBRP/FDIVP/FDIVRP / int16 ──────
    def _de(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            st0 = cpu.fpu_get(0); sti = cpu.fpu_get(rm)
            if reg == 0: cpu.fpu_set(rm, sti + st0); cpu.fpu_pop()    # FADDP
            elif reg == 1: cpu.fpu_set(rm, sti * st0); cpu.fpu_pop()  # FMULP
            elif reg == 2: cpu.fpu_compare(st0, sti); cpu.fpu_pop()   # FCOMP5
            elif reg == 3:  # FCOMPP (DE D9 only)
                if rm == 1:
                    cpu.fpu_compare(st0, cpu.fpu_get(1))
                    cpu.fpu_pop(); cpu.fpu_pop()
            elif reg == 4: cpu.fpu_set(rm, st0 - sti); cpu.fpu_pop()  # FSUBRP
            elif reg == 5: cpu.fpu_set(rm, sti - st0); cpu.fpu_pop()  # FSUBP
            elif reg == 6: cpu.fpu_set(rm, st0 / sti); cpu.fpu_pop()  # FDIVRP
            elif reg == 7: cpu.fpu_set(rm, sti / st0); cpu.fpu_pop()  # FDIVP
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            raw = cpu.memory.read16(addr)
            val = float(raw - 0x10000 if raw & 0x8000 else raw)
            st0 = cpu.fpu_get(0)
            if reg == 0: cpu.fpu_set(0, st0 + val)
            elif reg == 1: cpu.fpu_set(0, st0 * val)
            elif reg == 2: cpu.fpu_compare(st0, val)
            elif reg == 3: cpu.fpu_compare(st0, val); cpu.fpu_pop()
            elif reg == 4: cpu.fpu_set(0, st0 - val)
            elif reg == 5: cpu.fpu_set(0, val - st0)
            elif reg == 6: cpu.fpu_set(0, st0 / val)
            elif reg == 7: cpu.fpu_set(0, val / st0)
    cpu.register(0xDE, _de)

    # ── 0xDF: FILD/FISTP int16/int64, FNSTSW AX, FUCOMIP, FCOMIP ─────────
    def _df(cpu: "CPU") -> None:
        d = cpu.decode_mod_rm()
        mod, reg, rm = d["mod"], d["reg"], d["rm"]
        if mod == 3:
            if reg == 4 and rm == 0:  # FNSTSW AX  (DF E0)
                cpu.regs[EAX] = (cpu.regs[EAX] & 0xFFFF0000) | (cpu.fpu_status_word & 0xFFFF)
            elif reg == 5:  # FUCOMIP ST, ST(i) — compare, set EFLAGS, pop
                _fpu_comi(cpu, cpu.fpu_get(0), cpu.fpu_get(rm), pop=True)
            elif reg == 6:  # FCOMIP ST, ST(i) — compare, set EFLAGS, pop
                _fpu_comi(cpu, cpu.fpu_get(0), cpu.fpu_get(rm), pop=True)
        else:
            res = cpu.resolve_rm(mod, rm)
            addr = cpu._apply_segment_override(res["addr"])
            if reg == 0:   # FILD m16int
                raw = cpu.memory.read16(addr)
                cpu.fpu_push(float(raw - 0x10000 if raw & 0x8000 else raw))
            elif reg == 1: # FISTTP m16int
                cpu.memory.write16(addr, int(math.trunc(cpu.fpu_get(0))) & 0xFFFF)
                cpu.fpu_pop()
            elif reg == 2: # FIST m16int
                cpu.memory.write16(addr, int(round(cpu.fpu_get(0))) & 0xFFFF)
            elif reg == 3: # FISTP m16int
                cpu.memory.write16(addr, int(round(cpu.fpu_get(0))) & 0xFFFF)
                cpu.fpu_pop()
            elif reg == 5: # FILD m64int
                lo = cpu.memory.read32(addr)
                hi = cpu.memory.read_signed32(addr + 4)
                cpu.fpu_push(float(hi * 0x100000000 + lo))
            elif reg == 7: # FISTP m64int
                val = cpu.fpu_get(0)
                lo = int(val) & 0xFFFFFFFF
                hi = int(math.trunc(val / 0x100000000))
                cpu.memory.write32(addr, lo & 0xFFFFFFFF)
                cpu.memory.write32(addr + 4, hi & 0xFFFFFFFF)
                cpu.fpu_pop()
    cpu.register(0xDF, _df)


def _fpu_comi(cpu: "CPU", a: float, b: float, *, pop: bool) -> None:
    """Set EFLAGS from FPU comparison (FUCOMI / FCOMI / FUCOMIP / FCOMIP)."""
    if math.isnan(a) or math.isnan(b):
        cpu.set_flag(ZF_BIT, True)
        cpu.set_flag(CF_BIT, True)
    elif a > b:
        cpu.set_flag(ZF_BIT, False)
        cpu.set_flag(CF_BIT, False)
    elif a < b:
        cpu.set_flag(ZF_BIT, False)
        cpu.set_flag(CF_BIT, True)
    else:
        cpu.set_flag(ZF_BIT, True)
        cpu.set_flag(CF_BIT, False)
    cpu.set_flag(OF_BIT, False)
    if pop:
        cpu.fpu_pop()

"""x86-32 processor state machine."""

from __future__ import annotations
import struct
from typing import TYPE_CHECKING, Callable

from tew.hardware.memory import Memory
from tew.logger import logger

if TYPE_CHECKING:
    from tew.kernel.kernel_structures import KernelStructures

# Register indices
EAX, ECX, EDX, EBX = 0, 1, 2, 3
ESP, EBP, ESI, EDI = 4, 5, 6, 7

REG = {"EAX": 0, "ECX": 1, "EDX": 2, "EBX": 3, "ESP": 4, "EBP": 5, "ESI": 6, "EDI": 7}
REG_NAMES = ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI"]

# Flag bit positions in EFLAGS
CF_BIT = 0
PF_BIT = 2
ZF_BIT = 6
SF_BIT = 7
DF_BIT = 10
OF_BIT = 11

FLAG = {"CF": CF_BIT, "PF": PF_BIT, "ZF": ZF_BIT, "SF": SF_BIT, "DF": DF_BIT, "OF": OF_BIT}

OpcodeHandler = Callable[["CPU"], None]


def _to_u32(n: int) -> int:
    return n & 0xFFFFFFFF


def _to_s32(n: int) -> int:
    n = n & 0xFFFFFFFF
    return n if n < 0x80000000 else n - 0x100000000


class CPU:
    """x86-32 processor emulation."""

    def __init__(self, memory: Memory) -> None:
        self.regs: list[int] = [0] * 8         # EAX–EDI as unsigned 32-bit
        self.eip: int = 0
        self.eflags: int = 0
        self.memory: Memory = memory
        self.halted: bool = False
        self.faulted: bool = False
        self.last_error: Exception | None = None
        self.kernel_structures: "KernelStructures | None" = None
        self.segments: dict[str, int] = {"ES": 0, "DS": 0, "CS": 0, "SS": 0, "FS": 0, "GS": 0}

        self._opcode_table: dict[int, OpcodeHandler] = {}
        self._int_handler: Callable[[int, "CPU"], None] | None = None
        self._step_handler: Callable[[int, int, "CPU"], None] | None = None
        self._step_count: int = 0
        self._segment_override: str | None = None   # "FS" | "GS" | None
        self._rep_prefix: str | None = None         # "REP" | "REPNE" | None
        self._operand_size_override: bool = False

        # x87 FPU state
        self.fpu_stack: list[float] = [0.0] * 8
        self.fpu_top: int = 0
        self.fpu_status_word: int = 0
        self.fpu_control_word: int = 0x037F  # all exceptions masked, double precision, round-nearest
        self.fpu_tag_word: int = 0xFFFF       # all registers empty

    # ── FPU helpers ───────────────────────────────────────────────────────

    def fpu_get(self, i: int) -> float:
        return self.fpu_stack[(self.fpu_top + i) & 7]

    def fpu_set(self, i: int, val: float) -> None:
        idx = (self.fpu_top + i) & 7
        self.fpu_stack[idx] = val
        self.fpu_tag_word &= ~(3 << (idx * 2))  # mark valid

    def fpu_push(self, val: float) -> None:
        self.fpu_top = (self.fpu_top - 1) & 7
        self.fpu_stack[self.fpu_top] = val
        self.fpu_tag_word &= ~(3 << (self.fpu_top * 2))
        self.fpu_status_word = (self.fpu_status_word & ~0x3800) | (self.fpu_top << 11)

    def fpu_pop(self) -> float:
        val = self.fpu_stack[self.fpu_top]
        self.fpu_tag_word |= (3 << (self.fpu_top * 2))  # mark empty
        self.fpu_top = (self.fpu_top + 1) & 7
        self.fpu_status_word = (self.fpu_status_word & ~0x3800) | (self.fpu_top << 11)
        return val

    def fpu_set_cc(self, c3: bool, c2: bool, c0: bool) -> None:
        self.fpu_status_word &= ~0x4500
        if c0: self.fpu_status_word |= 0x0100
        if c2: self.fpu_status_word |= 0x0400
        if c3: self.fpu_status_word |= 0x4000

    def fpu_compare(self, a: float, b: float) -> None:
        import math
        if math.isnan(a) or math.isnan(b):
            self.fpu_set_cc(True, True, True)
        elif a > b:
            self.fpu_set_cc(False, False, False)
        elif a < b:
            self.fpu_set_cc(False, False, True)
        else:
            self.fpu_set_cc(True, False, False)

    def read_double(self, addr: int) -> float:
        lo = self.memory.read32(addr)
        hi = self.memory.read32(addr + 4)
        return struct.unpack("<d", struct.pack("<II", lo, hi))[0]

    def write_double(self, addr: int, val: float) -> None:
        lo, hi = struct.unpack("<II", struct.pack("<d", val))
        self.memory.write32(addr, lo)
        self.memory.write32(addr + 4, hi)

    def read_float(self, addr: int) -> float:
        raw = self.memory.read32(addr)
        return struct.unpack("<f", struct.pack("<I", raw))[0]

    def write_float(self, addr: int, val: float) -> None:
        raw = struct.unpack("<I", struct.pack("<f", val))[0]
        self.memory.write32(addr, raw)

    # ── 8-bit register helpers ────────────────────────────────────────────

    def read_reg8(self, idx: int) -> int:
        """Read 8-bit register: 0=AL,1=CL,2=DL,3=BL,4=AH,5=CH,6=DH,7=BH."""
        if idx < 4:
            return self.regs[idx] & 0xFF
        return (self.regs[idx - 4] >> 8) & 0xFF

    def write_reg8(self, idx: int, val: int) -> None:
        if idx < 4:
            self.regs[idx] = (self.regs[idx] & 0xFFFFFF00) | (val & 0xFF)
        else:
            self.regs[idx - 4] = (self.regs[idx - 4] & 0xFFFF00FF) | ((val & 0xFF) << 8)

    def read_rm8(self, mod: int, rm: int) -> int:
        if mod == 3:
            return self.read_reg8(rm)
        resolved = self.resolve_rm(mod, rm)
        return self.memory.read8(self._apply_segment_override(resolved["addr"]))

    def read_rm8_resolved(self, mod: int, rm: int) -> dict:
        if mod == 0b11:
            return {"value": self.read_reg8(rm), "is_reg": True, "addr": rm}
        res = self.resolve_rm(mod, rm)
        addr = self._apply_segment_override(res["addr"])
        return {"value": self.memory.read8(addr), "is_reg": False, "addr": addr}

    def write_rm8_resolved(self, is_reg: bool, addr: int, val: int) -> None:
        if is_reg:
            self.write_reg8(addr, val)
        else:
            self.memory.write8(addr, val & 0xFF)

    def write_rm8(self, mod: int, rm: int, val: int) -> None:
        if mod == 3:
            self.write_reg8(rm, val)
        else:
            resolved = self.resolve_rm(mod, rm)
            self.memory.write8(self._apply_segment_override(resolved["addr"]), val & 0xFF)

    # ── Opcode registration ───────────────────────────────────────────────

    def register(self, opcode: int, handler: OpcodeHandler) -> None:
        self._opcode_table[opcode] = handler

    def on_interrupt(self, handler: Callable[[int, "CPU"], None]) -> None:
        self._int_handler = handler

    def on_step(self, handler: Callable[[int, int, "CPU"], None]) -> None:
        self._step_handler = handler

    def trigger_interrupt(self, int_num: int) -> None:
        if self._int_handler:
            self._int_handler(int_num, self)
        else:
            raise RuntimeError(f"Unhandled interrupt: INT 0x{int_num:02x}")

    def handle_exception(self, error: Exception) -> None:
        self.faulted = True
        self.last_error = error
        self.halted = True

    # ── Fetch helpers (read at EIP and advance) ───────────────────────────

    def fetch8(self) -> int:
        val = self.memory.read8(self.eip)
        self.eip = (self.eip + 1) & 0xFFFFFFFF
        return val

    def fetch16(self) -> int:
        val = self.memory.read16(self.eip)
        self.eip = (self.eip + 2) & 0xFFFFFFFF
        return val

    def fetch32(self) -> int:
        val = self.memory.read32(self.eip)
        self.eip = (self.eip + 4) & 0xFFFFFFFF
        return val

    def fetch_signed8(self) -> int:
        val = self.memory.read_signed8(self.eip)
        self.eip = (self.eip + 1) & 0xFFFFFFFF
        return val

    def fetch_signed32(self) -> int:
        val = self.memory.read_signed32(self.eip)
        self.eip = (self.eip + 4) & 0xFFFFFFFF
        return val

    # ── Flag helpers ──────────────────────────────────────────────────────

    def get_flag(self, bit: int) -> bool:
        return ((self.eflags >> bit) & 1) == 1

    def set_flag(self, bit: int, val: bool) -> None:
        if val:
            self.eflags |= (1 << bit)
        else:
            self.eflags &= ~(1 << bit)

    def update_flags_arith(self, result: int, op1: int, op2: int, is_sub: bool) -> None:
        r32 = result & 0xFFFFFFFF
        masked = r32

        self.set_flag(ZF_BIT, masked == 0)
        self.set_flag(SF_BIT, (masked & 0x80000000) != 0)

        p = masked & 0xFF
        p ^= p >> 4; p ^= p >> 2; p ^= p >> 1
        self.set_flag(PF_BIT, (p & 1) == 0)

        if is_sub:
            self.set_flag(CF_BIT, (op1 & 0xFFFFFFFF) < (op2 & 0xFFFFFFFF))
        else:
            self.set_flag(CF_BIT, r32 < (op1 & 0xFFFFFFFF) or r32 < (op2 & 0xFFFFFFFF))

        sign_op1 = (op1 & 0x80000000) != 0
        sign_op2 = (op2 & 0x80000000) != 0
        sign_res = (masked & 0x80000000) != 0
        if is_sub:
            self.set_flag(OF_BIT, sign_op1 != sign_op2 and sign_res != sign_op1)
        else:
            self.set_flag(OF_BIT, sign_op1 == sign_op2 and sign_res != sign_op1)

    def update_flags_logic(self, result: int) -> None:
        masked = result & 0xFFFFFFFF
        self.set_flag(ZF_BIT, masked == 0)
        self.set_flag(SF_BIT, (masked & 0x80000000) != 0)
        self.set_flag(CF_BIT, False)
        self.set_flag(OF_BIT, False)
        p = masked & 0xFF
        p ^= p >> 4; p ^= p >> 2; p ^= p >> 1
        self.set_flag(PF_BIT, (p & 1) == 0)

    # ── Stack helpers ─────────────────────────────────────────────────────

    def push32(self, val: int) -> None:
        self.regs[ESP] = (self.regs[ESP] - 4) & 0xFFFFFFFF
        self.memory.write32(self.regs[ESP], val)

    def pop32(self) -> int:
        val = self.memory.read32(self.regs[ESP])
        self.regs[ESP] = (self.regs[ESP] + 4) & 0xFFFFFFFF
        return val

    # ── ModR/M decoding ───────────────────────────────────────────────────

    def decode_mod_rm(self) -> dict[str, int]:
        byte = self.fetch8()
        return {
            "mod": (byte >> 6) & 0x3,
            "reg": (byte >> 3) & 0x7,
            "rm":  byte & 0x7,
        }

    def resolve_rm(self, mod: int, rm: int) -> dict[str, object]:
        if mod == 0b11:
            return {"is_reg": True, "addr": rm}

        if mod == 0b00:
            if rm == 5:
                addr = self.fetch32()
            elif rm == 4:
                addr = self._decode_sib(mod)
            else:
                addr = self.regs[rm]
        elif mod == 0b01:
            if rm == 4:
                sib_addr = self._decode_sib(mod)
                disp = self.fetch_signed8()
                addr = (sib_addr + disp) & 0xFFFFFFFF
            else:
                base = self.regs[rm]
                disp = self.fetch_signed8()
                addr = (base + disp) & 0xFFFFFFFF
        else:  # mod == 0b10
            if rm == 4:
                sib_addr = self._decode_sib(mod)
                disp = self.fetch_signed32()
                addr = (sib_addr + disp) & 0xFFFFFFFF
            else:
                base = self.regs[rm]
                disp = self.fetch_signed32()
                addr = (base + disp) & 0xFFFFFFFF

        return {"is_reg": False, "addr": addr}

    def _decode_sib(self, mod: int) -> int:
        """Decode SIB (Scale-Index-Base) byte."""
        sib = self.fetch8()
        scale = 1 << ((sib >> 6) & 0x3)
        index = (sib >> 3) & 0x7
        base = sib & 0x7

        if base == 5 and mod == 0b00:
            addr = self.fetch32()
        else:
            addr = self.regs[base]

        if index != 4:
            addr = (addr + self.regs[index] * scale) & 0xFFFFFFFF

        return addr

    def _apply_segment_override(self, addr: int) -> int:
        if not self._segment_override or not self.kernel_structures:
            return addr
        if self._segment_override == "FS":
            return self.kernel_structures.resolve_fs_relative_address(addr)
        if self._segment_override == "GS":
            return self.kernel_structures.resolve_gs_relative_address(addr)
        return addr

    def _clear_prefixes(self) -> None:
        self._segment_override = None
        self._rep_prefix = None
        self._operand_size_override = False

    # ── r/m 32-bit helpers ────────────────────────────────────────────────

    def read_rm32(self, mod: int, rm: int) -> int:
        resolved = self.resolve_rm(mod, rm)
        if resolved["is_reg"]:
            return self.regs[resolved["addr"]]
        return self.memory.read32(self._apply_segment_override(resolved["addr"]))

    def read_rm32_resolved(self, mod: int, rm: int) -> dict:
        res = self.resolve_rm(mod, rm)
        if res["is_reg"]:
            return {"value": self.regs[res["addr"]], "is_reg": True, "addr": res["addr"]}
        addr = self._apply_segment_override(res["addr"])
        return {"value": self.memory.read32(addr), "is_reg": False, "addr": addr}

    def write_rm32_resolved(self, is_reg: bool, addr: int, val: int) -> None:
        if is_reg:
            self.regs[addr] = val & 0xFFFFFFFF
        else:
            self.memory.write32(addr, val & 0xFFFFFFFF)

    def read_rmv_resolved(self, mod: int, rm: int) -> dict:
        res = self.resolve_rm(mod, rm)
        if res["is_reg"]:
            value = self.regs[res["addr"]] & 0xFFFF if self._operand_size_override else self.regs[res["addr"]]
            return {"value": value, "is_reg": True, "addr": res["addr"]}
        addr = self._apply_segment_override(res["addr"])
        value = self.memory.read16(addr) if self._operand_size_override else self.memory.read32(addr)
        return {"value": value, "is_reg": False, "addr": addr}

    def write_rmv_resolved(self, is_reg: bool, addr: int, val: int) -> None:
        if self._operand_size_override:
            if is_reg:
                self.regs[addr] = (self.regs[addr] & 0xFFFF0000) | (val & 0xFFFF)
            else:
                self.memory.write16(addr, val & 0xFFFF)
        else:
            if is_reg:
                self.regs[addr] = val & 0xFFFFFFFF
            else:
                self.memory.write32(addr, val & 0xFFFFFFFF)

    def write_rm32(self, mod: int, rm: int, val: int) -> None:
        resolved = self.resolve_rm(mod, rm)
        if resolved["is_reg"]:
            self.regs[resolved["addr"]] = val & 0xFFFFFFFF
        else:
            self.memory.write32(self._apply_segment_override(resolved["addr"]), val & 0xFFFFFFFF)

    def read_rmv(self, mod: int, rm: int) -> int:
        resolved = self.resolve_rm(mod, rm)
        if self._operand_size_override:
            if resolved["is_reg"]:
                return self.regs[resolved["addr"]] & 0xFFFF
            return self.memory.read16(self._apply_segment_override(resolved["addr"]))
        if resolved["is_reg"]:
            return self.regs[resolved["addr"]]
        return self.memory.read32(self._apply_segment_override(resolved["addr"]))

    def write_rmv(self, mod: int, rm: int, val: int) -> None:
        resolved = self.resolve_rm(mod, rm)
        if self._operand_size_override:
            if resolved["is_reg"]:
                self.regs[resolved["addr"]] = (self.regs[resolved["addr"]] & 0xFFFF0000) | (val & 0xFFFF)
            else:
                self.memory.write16(self._apply_segment_override(resolved["addr"]), val & 0xFFFF)
        else:
            if resolved["is_reg"]:
                self.regs[resolved["addr"]] = val & 0xFFFFFFFF
            else:
                self.memory.write32(self._apply_segment_override(resolved["addr"]), val & 0xFFFFFFFF)

    def fetch_immediate(self) -> int:
        return self.fetch16() if self._operand_size_override else self.fetch32()

    def fetch_signed_immediate(self) -> int:
        if self._operand_size_override:
            val = self.fetch16()
            return val - 0x10000 if val & 0x8000 else val
        return self.fetch_signed32()

    @property
    def rep_prefix(self) -> str | None:
        return self._rep_prefix

    @property
    def operand_size_override(self) -> bool:
        return self._operand_size_override

    # ── Execution ─────────────────────────────────────────────────────────

    _PREFIX_BYTES = frozenset([0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65, 0x66, 0x67, 0xF0, 0xF2, 0xF3])

    def _skip_prefix(self) -> None:
        while self.memory.read8(self.eip) in self._PREFIX_BYTES:
            prefix = self.fetch8()
            if prefix == 0x64:
                self._segment_override = "FS"
            elif prefix == 0x65:
                self._segment_override = "GS"
            elif prefix == 0xF3:
                self._rep_prefix = "REP"
            elif prefix == 0xF2:
                self._rep_prefix = "REPNE"
            elif prefix == 0x66:
                self._operand_size_override = True

    def step(self) -> None:
        try:
            self._skip_prefix()
            instr_addr = self.eip
            opcode = self.fetch8()
            if self._step_handler:
                self._step_handler(instr_addr, opcode, self)
            handler = self._opcode_table.get(opcode)
            if not handler:
                raise RuntimeError(
                    f"Unknown opcode: 0x{opcode:02x} at EIP=0x{(self.eip - 1) & 0xFFFFFFFF:08x}"
                )
            handler(self)
            self._clear_prefixes()
            self._step_count += 1
        except Exception as error:
            self._clear_prefixes()
            self.handle_exception(error)

    def run(self, max_steps: int = 1_000_000) -> None:
        self._step_count = 0
        while not self.halted and self._step_count < max_steps:
            self.step()
        if self._step_count >= max_steps:
            logger.warn("cpu", f"Execution limit reached ({max_steps} steps)")

    @property
    def step_count(self) -> int:
        return self._step_count

    def __str__(self) -> str:
        regs = "  ".join(f"{name}={self.regs[i] & 0xFFFFFFFF:08x}" for i, name in enumerate(REG_NAMES))
        flags = " ".join([
            "CF" if self.get_flag(CF_BIT) else "cf",
            "ZF" if self.get_flag(ZF_BIT) else "zf",
            "SF" if self.get_flag(SF_BIT) else "sf",
            "OF" if self.get_flag(OF_BIT) else "of",
        ])
        return f"EIP={self.eip & 0xFFFFFFFF:08x}  {regs}  [{flags}]"

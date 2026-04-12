"""x86-32 opcode registration — imports and wires all instruction groups."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU


def register_all_opcodes(cpu: "CPU") -> None:
    from tew.emulator.opcodes.data_movement import register_data_movement
    from tew.emulator.opcodes.arithmetic import register_arithmetic
    from tew.emulator.opcodes.logic import register_logic
    from tew.emulator.opcodes.stack import register_stack
    from tew.emulator.opcodes.control_flow import register_control_flow
    from tew.emulator.opcodes.group5 import register_group5
    from tew.emulator.opcodes.two_byte import register_two_byte_opcodes
    from tew.emulator.opcodes.string_ops import register_string_ops
    from tew.emulator.opcodes.misc import register_misc
    from tew.emulator.opcodes.fpu import register_fpu

    register_data_movement(cpu)
    register_arithmetic(cpu)
    register_logic(cpu)
    register_stack(cpu)
    register_control_flow(cpu)
    register_group5(cpu)
    register_two_byte_opcodes(cpu)
    register_string_ops(cpu)
    register_misc(cpu)
    register_fpu(cpu)

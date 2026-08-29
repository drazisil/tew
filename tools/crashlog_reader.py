#!/usr/bin/env python3
"""Reads the structured crash dump tew writes on every fault/halt
(tew.kernel.exception_diagnostics._write_crash_log, default
/tmp/emu_crash.json) and prints a summary, or resolves extra ad-hoc
addresses against that crash's recorded DLL table + static memory map.

Usage:
  python tools/crashlog_reader.py                    # summarize the crash
  python tools/crashlog_reader.py 0x1004d37e 0x82be068  # resolve addresses
  python tools/crashlog_reader.py --crash-log PATH   # non-default crash file
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def resolve_address(addr: int, dll_table: list[dict], static_memory_map: list[dict]) -> str:
    for dll in dll_table:
        if dll["base"] <= addr < dll["end"]:
            return f"{dll['name']}+0x{addr - dll['base']:x}"
    for region in static_memory_map:
        start, end, label = region["start"], region["end"], region["label"]
        if addr >= start and (end is None or addr < end):
            return label
    return "unknown region"


def _format_reg(name: str, entry: dict) -> str:
    marker = ""
    if "valid" in entry:
        marker = " [ok]" if entry["valid"] else " [!!]"
    return f"  {name}: 0x{entry['value']:08x}{marker}"


def print_summary(data: dict) -> None:
    print(f"--- {data['kind'].upper()} @ 0x{data['eip']:08x} ---")
    if data.get("eip_annotation"):
        print(f"Location: {data['eip_annotation']}")

    if "memory_access" in data:
        ma = data["memory_access"]
        print(f"Attempted address: 0x{ma['attempted_address']:08x}")
        if ma.get("in_dll"):
            d = ma["in_dll"]
            print(f"  in {d['name']} (0x{d['base']:08x}-0x{d['end']:08x}), offset 0x{d['offset']:x}")
        elif "looks_like_unresolved_import" in ma:
            note = " (looks like an unresolved import)" if ma["looks_like_unresolved_import"] else ""
            print(f"  NOT in any loaded DLL{note}")

    print("\nRegisters:")
    for name, entry in data["registers"].items():
        print(_format_reg(name, entry))

    print(f"\nStack (ESP=0x{data['esp']:08x}):")
    for slot in data["stack_dump"]:
        if "error" in slot:
            print(f"  [ESP+{slot['offset']:03x}] (read error)")
            break
        ann = f"  ← {slot['annotation']}" if slot.get("annotation") else ""
        print(f"  [ESP+{slot['offset']:03x}] 0x{slot['value']:08x}{ann}")

    print("\nEBP chain:")
    for frame in data["ebp_chain"]:
        if frame.get("cycle"):
            print(f"  frame[{frame['depth']}] EBP=0x{frame['ebp']:08x} (cycle -- stopping)")
            break
        if "error" in frame:
            print(f"  frame[{frame['depth']}] EBP=0x{frame['ebp']:08x} (read error)")
            break
        ann = f"  ← {frame['annotation']}" if frame.get("annotation") else ""
        print(f"  frame[{frame['depth']}] EBP=0x{frame['ebp']:08x}  ret=0x{frame['ret']:08x}{ann}")

    print(f"\nDLL table ({len(data['dll_table'])} loaded):")
    for dll in data["dll_table"]:
        print(f"  0x{dll['base']:08x}-0x{dll['end']:08x}  {dll['name']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("addresses", nargs="*", help="extra hex addresses to resolve against the crash's DLL table / memory map")
    parser.add_argument("--crash-log", default="/tmp/emu_crash.json", help="path to crash JSON (default: /tmp/emu_crash.json)")
    args = parser.parse_args()

    path = Path(args.crash_log)
    if not path.exists():
        print(f"error: crash log not found: {path}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(path.read_text())

    print_summary(data)

    if args.addresses:
        print("\nResolved addresses:")
        for raw in args.addresses:
            addr = int(raw, 16)
            print(f"  0x{addr:08x}  ← {resolve_address(addr, data['dll_table'], data['static_memory_map'])}")


if __name__ == "__main__":
    main()

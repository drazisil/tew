"""
Tests for tew.api.lc_map — LCMapStringW wide-string transformation.

Structure:
    TestLCMapFlags         — verify the IntFlag values match winnls.h
    TestToLowercase        — _to_lowercase ASCII character helper
    TestToUppercase        — _to_uppercase ASCII character helper
    TestLCMapWideString    — lc_map_wide_string end-to-end with a real Memory
"""

import pytest
from tew.hardware.memory import Memory
from tew.api.lc_map import (
    LCMapFlags,
    LCMapStringArgs,
    _to_lowercase,
    _to_uppercase,
    lc_map_wide_string,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_wide(mem: Memory, addr: int, text: str) -> None:
    """Write a null-terminated UTF-16LE string at *addr*."""
    for i, ch in enumerate(text):
        mem.write16(addr + i * 2, ord(ch))
    mem.write16(addr + len(text) * 2, 0)


def read_wide(mem: Memory, addr: int, count: int) -> str:
    """Read *count* UTF-16LE characters from *addr* and return as str."""
    return "".join(chr(mem.read16(addr + i * 2)) for i in range(count))


SRC = 0x1000
DST = 0x2000


@pytest.fixture
def mem():
    return Memory(0x10000)


def lower_args(mem, src_text, cch_src, cch_dest, *, null_terminated=False):
    """Write *src_text* and return an LCMAP_LOWERCASE LCMapStringArgs."""
    write_wide(mem, SRC, src_text)
    return LCMapStringArgs(
        locale    = 0x0409,  # en-US, ignored
        map_flags = int(LCMapFlags.LCMAP_LOWERCASE),
        src_ptr   = SRC,
        cch_src   = 0xFFFFFFFF if null_terminated else cch_src,
        dest_ptr  = DST,
        cch_dest  = cch_dest,
    )


def upper_args(mem, src_text, cch_src, cch_dest):
    """Write *src_text* and return an LCMAP_UPPERCASE LCMapStringArgs."""
    write_wide(mem, SRC, src_text)
    return LCMapStringArgs(
        locale    = 0x0409,
        map_flags = int(LCMapFlags.LCMAP_UPPERCASE),
        src_ptr   = SRC,
        cch_src   = cch_src,
        dest_ptr  = DST,
        cch_dest  = cch_dest,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Flag values match winnls.h
# ─────────────────────────────────────────────────────────────────────────────

class TestLCMapFlags:
    def test_lowercase(self):   assert int(LCMapFlags.LCMAP_LOWERCASE)         == 0x00000100
    def test_uppercase(self):   assert int(LCMapFlags.LCMAP_UPPERCASE)         == 0x00000200
    def test_sortkey(self):     assert int(LCMapFlags.LCMAP_SORTKEY)           == 0x00000400
    def test_byterev(self):     assert int(LCMapFlags.LCMAP_BYTEREV)           == 0x00000800
    def test_ling_casing(self): assert int(LCMapFlags.LCMAP_LINGUISTIC_CASING) == 0x01000000


# ─────────────────────────────────────────────────────────────────────────────
# Character helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestToLowercase:
    def test_a_to_z_converted(self):
        for cp in range(ord('A'), ord('Z') + 1):
            assert _to_lowercase(cp) == cp + 0x20, \
                f"{chr(cp)!r} should lowercase to {chr(cp + 0x20)!r}"

    def test_lowercase_unchanged(self):
        for cp in range(ord('a'), ord('z') + 1):
            assert _to_lowercase(cp) == cp

    def test_digits_unchanged(self):
        for cp in range(ord('0'), ord('9') + 1):
            assert _to_lowercase(cp) == cp

    def test_null_unchanged(self):
        assert _to_lowercase(0) == 0

    def test_non_ascii_unchanged(self):
        assert _to_lowercase(0x80) == 0x80
        assert _to_lowercase(0xFF) == 0xFF


class TestToUppercase:
    def test_a_to_z_converted(self):
        for cp in range(ord('a'), ord('z') + 1):
            assert _to_uppercase(cp) == cp - 0x20, \
                f"{chr(cp)!r} should uppercase to {chr(cp - 0x20)!r}"

    def test_uppercase_unchanged(self):
        for cp in range(ord('A'), ord('Z') + 1):
            assert _to_uppercase(cp) == cp

    def test_digits_unchanged(self):
        for cp in range(ord('0'), ord('9') + 1):
            assert _to_uppercase(cp) == cp

    def test_null_unchanged(self):
        assert _to_uppercase(0) == 0

    def test_non_ascii_unchanged(self):
        assert _to_uppercase(0x80) == 0x80
        assert _to_uppercase(0xFF) == 0xFF


# ─────────────────────────────────────────────────────────────────────────────
# lc_map_wide_string
# ─────────────────────────────────────────────────────────────────────────────

class TestLCMapWideString:

    # ── Unsupported flags return None ────────────────────────────────────────

    def test_sortkey_returns_none(self, mem):
        args = LCMapStringArgs(
            locale=0, map_flags=int(LCMapFlags.LCMAP_SORTKEY),
            src_ptr=SRC, cch_src=1, dest_ptr=DST, cch_dest=1,
        )
        assert lc_map_wide_string(mem, args) is None

    def test_byterev_returns_none(self, mem):
        args = LCMapStringArgs(
            locale=0, map_flags=int(LCMapFlags.LCMAP_BYTEREV),
            src_ptr=SRC, cch_src=1, dest_ptr=DST, cch_dest=1,
        )
        assert lc_map_wide_string(mem, args) is None

    def test_zero_flags_returns_none(self, mem):
        args = LCMapStringArgs(
            locale=0, map_flags=0,
            src_ptr=SRC, cch_src=1, dest_ptr=DST, cch_dest=1,
        )
        assert lc_map_wide_string(mem, args) is None

    # ── LCMAP_LOWERCASE ──────────────────────────────────────────────────────

    def test_lowercase_hello(self, mem):
        args = lower_args(mem, "HELLO", cch_src=5, cch_dest=5)
        result = lc_map_wide_string(mem, args)
        assert result == 5
        assert read_wide(mem, DST, 5) == "hello"

    def test_lowercase_already_lower(self, mem):
        args = lower_args(mem, "hello", cch_src=5, cch_dest=5)
        assert lc_map_wide_string(mem, args) == 5
        assert read_wide(mem, DST, 5) == "hello"

    def test_lowercase_mixed(self, mem):
        args = lower_args(mem, "HeLLo", cch_src=5, cch_dest=5)
        lc_map_wide_string(mem, args)
        assert read_wide(mem, DST, 5) == "hello"

    def test_lowercase_digits_and_punct_unchanged(self, mem):
        args = lower_args(mem, "A1!Z", cch_src=4, cch_dest=4)
        lc_map_wide_string(mem, args)
        assert read_wide(mem, DST, 4) == "a1!z"

    # ── LCMAP_UPPERCASE ──────────────────────────────────────────────────────

    def test_uppercase_hello(self, mem):
        args = upper_args(mem, "hello", cch_src=5, cch_dest=5)
        result = lc_map_wide_string(mem, args)
        assert result == 5
        assert read_wide(mem, DST, 5) == "HELLO"

    def test_uppercase_already_upper(self, mem):
        args = upper_args(mem, "HELLO", cch_src=5, cch_dest=5)
        assert lc_map_wide_string(mem, args) == 5
        assert read_wide(mem, DST, 5) == "HELLO"

    def test_uppercase_digits_and_punct_unchanged(self, mem):
        args = upper_args(mem, "a1!z", cch_src=4, cch_dest=4)
        lc_map_wide_string(mem, args)
        assert read_wide(mem, DST, 4) == "A1!Z"

    # ── LCMAP_LINGUISTIC_CASING ignored ──────────────────────────────────────

    def test_linguistic_casing_with_lowercase(self, mem):
        """LCMAP_LINGUISTIC_CASING combined with LCMAP_LOWERCASE must still work."""
        write_wide(mem, SRC, "HELLO")
        args = LCMapStringArgs(
            locale    = 0x0409,
            map_flags = int(LCMapFlags.LCMAP_LOWERCASE | LCMapFlags.LCMAP_LINGUISTIC_CASING),
            src_ptr   = SRC,
            cch_src   = 5,
            dest_ptr  = DST,
            cch_dest  = 5,
        )
        result = lc_map_wide_string(mem, args)
        assert result == 5
        assert read_wide(mem, DST, 5) == "hello"

    def test_linguistic_casing_with_uppercase(self, mem):
        write_wide(mem, SRC, "hello")
        args = LCMapStringArgs(
            locale    = 0x0409,
            map_flags = int(LCMapFlags.LCMAP_UPPERCASE | LCMapFlags.LCMAP_LINGUISTIC_CASING),
            src_ptr   = SRC,
            cch_src   = 5,
            dest_ptr  = DST,
            cch_dest  = 5,
        )
        result = lc_map_wide_string(mem, args)
        assert result == 5
        assert read_wide(mem, DST, 5) == "HELLO"

    # ── Null-terminated (cch_src == 0xFFFFFFFF) ───────────────────────────────

    def test_null_terminated_lowercase(self, mem):
        args = lower_args(mem, "ABC", cch_src=3, cch_dest=4, null_terminated=True)
        result = lc_map_wide_string(mem, args)
        # count == 4 (3 chars + null terminator)
        assert result == 4
        assert read_wide(mem, DST, 3) == "abc"
        assert mem.read16(DST + 6) == 0  # null written to output

    def test_null_terminated_empty(self, mem):
        mem.write16(SRC, 0)  # just a null terminator
        args = LCMapStringArgs(
            locale=0, map_flags=int(LCMapFlags.LCMAP_LOWERCASE),
            src_ptr=SRC, cch_src=0xFFFFFFFF, dest_ptr=DST, cch_dest=1,
        )
        result = lc_map_wide_string(mem, args)
        assert result == 1   # just the null terminator
        assert mem.read16(DST) == 0

    # ── Size query (cch_dest == 0) ────────────────────────────────────────────

    def test_size_query_explicit_count(self, mem):
        args = lower_args(mem, "HELLO", cch_src=5, cch_dest=0)
        result = lc_map_wide_string(mem, args)
        assert result == 5
        # Nothing written to dst.
        assert mem.read16(DST) == 0

    def test_size_query_null_terminated(self, mem):
        args = lower_args(mem, "HI", cch_src=2, cch_dest=0, null_terminated=True)
        result = lc_map_wide_string(mem, args)
        assert result == 3   # 2 chars + null

    # ── Buffer too small ──────────────────────────────────────────────────────

    def test_buffer_too_small_returns_zero(self, mem):
        args = lower_args(mem, "HELLO", cch_src=5, cch_dest=3)
        result = lc_map_wide_string(mem, args)
        assert result == 0

    def test_buffer_exact_size_succeeds(self, mem):
        args = lower_args(mem, "HELLO", cch_src=5, cch_dest=5)
        result = lc_map_wide_string(mem, args)
        assert result == 5
        assert read_wide(mem, DST, 5) == "hello"

    # ── Roundtrip ─────────────────────────────────────────────────────────────

    def test_roundtrip_lower_then_upper(self, mem):
        """Lowercasing then uppercasing should recover the original string."""
        original = "HeLLo123"
        write_wide(mem, SRC, original)
        mid = 0x3000

        # lowercase pass: SRC → mid
        args1 = LCMapStringArgs(
            locale=0, map_flags=int(LCMapFlags.LCMAP_LOWERCASE),
            src_ptr=SRC, cch_src=len(original), dest_ptr=mid, cch_dest=len(original),
        )
        lc_map_wide_string(mem, args1)

        # uppercase pass: mid → DST
        args2 = LCMapStringArgs(
            locale=0, map_flags=int(LCMapFlags.LCMAP_UPPERCASE),
            src_ptr=mid, cch_src=len(original), dest_ptr=DST, cch_dest=len(original),
        )
        lc_map_wide_string(mem, args2)

        assert read_wide(mem, DST, len(original)) == original.upper()

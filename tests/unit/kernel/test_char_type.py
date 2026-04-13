"""
Tests for tew.api.char_type — CT_CTYPE1 classification and wide-string helper.

Structure:
    TestCtype1Flags      — verify the IntFlag values match winnls.h
    TestClassifyCtype1   — per-character classification correctness
    TestClassifyWideString — classify_wide_string end-to-end with a real Memory
"""

import pytest
from tew.hardware.memory import Memory
from tew.api.char_type import (
    CT_CTYPE1,
    CT_CTYPE2,
    CT_CTYPE3,
    Ctype1,
    GetStringTypeArgs,
    classify_ctype1,
    classify_wide_string,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_wide(mem: Memory, addr: int, text: str) -> None:
    """Write a null-terminated UTF-16LE string at *addr*."""
    for i, ch in enumerate(text):
        mem.write16(addr + i * 2, ord(ch))
    mem.write16(addr + len(text) * 2, 0)


def read_flags(mem: Memory, base: int, count: int) -> list[int]:
    """Read *count* WORDs from *base* and return them as a list of ints."""
    return [mem.read16(base + i * 2) for i in range(count)]


SRC = 0x1000
OUT = 0x2000


@pytest.fixture
def mem():
    return Memory(0x10000)


# ─────────────────────────────────────────────────────────────────────────────
# Flag values match winnls.h
# ─────────────────────────────────────────────────────────────────────────────

class TestCtype1Flags:
    def test_c1_upper(self):   assert int(Ctype1.UPPER)  == 0x0001
    def test_c1_lower(self):   assert int(Ctype1.LOWER)  == 0x0002
    def test_c1_digit(self):   assert int(Ctype1.DIGIT)  == 0x0004
    def test_c1_space(self):   assert int(Ctype1.SPACE)  == 0x0008
    def test_c1_punct(self):   assert int(Ctype1.PUNCT)  == 0x0010
    def test_c1_cntrl(self):   assert int(Ctype1.CNTRL)  == 0x0020
    def test_c1_blank(self):   assert int(Ctype1.BLANK)  == 0x0040
    def test_c1_xdigit(self):  assert int(Ctype1.XDIGIT) == 0x0080
    def test_c1_alpha(self):   assert int(Ctype1.ALPHA)  == 0x0100


# ─────────────────────────────────────────────────────────────────────────────
# Per-character classification
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyCtype1:

    # ── Control characters ───────────────────────────────────────────────────

    def test_nul_is_control(self):
        assert classify_ctype1(0x00) == int(Ctype1.CNTRL)

    def test_bs_is_control(self):
        assert classify_ctype1(0x08) == int(Ctype1.CNTRL)

    def test_tab_is_control_space_blank(self):
        flags = classify_ctype1(0x09)
        assert flags & int(Ctype1.CNTRL)
        assert flags & int(Ctype1.SPACE)
        assert flags & int(Ctype1.BLANK)

    def test_lf_is_control_space_not_blank(self):
        flags = classify_ctype1(0x0A)
        assert flags & int(Ctype1.CNTRL)
        assert flags & int(Ctype1.SPACE)
        assert not (flags & int(Ctype1.BLANK))

    def test_cr_is_control_space_not_blank(self):
        flags = classify_ctype1(0x0D)
        assert flags & int(Ctype1.CNTRL)
        assert flags & int(Ctype1.SPACE)
        assert not (flags & int(Ctype1.BLANK))

    def test_del_is_control(self):
        assert classify_ctype1(0x7F) == int(Ctype1.CNTRL)

    # ── Space ────────────────────────────────────────────────────────────────

    def test_space_is_space_blank(self):
        flags = classify_ctype1(0x20)
        assert flags & int(Ctype1.SPACE)
        assert flags & int(Ctype1.BLANK)

    def test_space_not_alpha_not_cntrl_not_digit(self):
        flags = classify_ctype1(0x20)
        assert not (flags & int(Ctype1.ALPHA))
        assert not (flags & int(Ctype1.CNTRL))
        assert not (flags & int(Ctype1.DIGIT))

    # ── Digits ───────────────────────────────────────────────────────────────

    def test_zero_is_digit_xdigit(self):
        flags = classify_ctype1(ord('0'))
        assert flags & int(Ctype1.DIGIT)
        assert flags & int(Ctype1.XDIGIT)
        assert not (flags & int(Ctype1.ALPHA))

    def test_nine_is_digit_xdigit(self):
        flags = classify_ctype1(ord('9'))
        assert flags & int(Ctype1.DIGIT)
        assert flags & int(Ctype1.XDIGIT)

    def test_all_digits_are_digit_xdigit(self):
        for ch in '0123456789':
            flags = classify_ctype1(ord(ch))
            assert flags & int(Ctype1.DIGIT),  f"{ch!r} should have DIGIT"
            assert flags & int(Ctype1.XDIGIT), f"{ch!r} should have XDIGIT"

    # ── Uppercase hex letters ─────────────────────────────────────────────────

    def test_upper_a_is_upper_alpha_xdigit(self):
        flags = classify_ctype1(ord('A'))
        assert flags & int(Ctype1.UPPER)
        assert flags & int(Ctype1.ALPHA)
        assert flags & int(Ctype1.XDIGIT)

    def test_upper_f_is_upper_alpha_xdigit(self):
        flags = classify_ctype1(ord('F'))
        assert flags & int(Ctype1.UPPER)
        assert flags & int(Ctype1.ALPHA)
        assert flags & int(Ctype1.XDIGIT)

    def test_upper_g_not_xdigit(self):
        flags = classify_ctype1(ord('G'))
        assert flags & int(Ctype1.UPPER)
        assert flags & int(Ctype1.ALPHA)
        assert not (flags & int(Ctype1.XDIGIT))

    def test_upper_z_not_xdigit(self):
        flags = classify_ctype1(ord('Z'))
        assert flags & int(Ctype1.UPPER)
        assert flags & int(Ctype1.ALPHA)
        assert not (flags & int(Ctype1.XDIGIT))

    # ── Lowercase hex letters ─────────────────────────────────────────────────

    def test_lower_a_is_lower_alpha_xdigit(self):
        flags = classify_ctype1(ord('a'))
        assert flags & int(Ctype1.LOWER)
        assert flags & int(Ctype1.ALPHA)
        assert flags & int(Ctype1.XDIGIT)

    def test_lower_f_is_lower_alpha_xdigit(self):
        flags = classify_ctype1(ord('f'))
        assert flags & int(Ctype1.LOWER)
        assert flags & int(Ctype1.ALPHA)
        assert flags & int(Ctype1.XDIGIT)

    def test_lower_g_not_xdigit(self):
        flags = classify_ctype1(ord('g'))
        assert flags & int(Ctype1.LOWER)
        assert flags & int(Ctype1.ALPHA)
        assert not (flags & int(Ctype1.XDIGIT))

    def test_lower_z_not_xdigit(self):
        flags = classify_ctype1(ord('z'))
        assert flags & int(Ctype1.LOWER)
        assert flags & int(Ctype1.ALPHA)
        assert not (flags & int(Ctype1.XDIGIT))

    # ── Mutual exclusivity ───────────────────────────────────────────────────

    def test_letters_never_digit(self):
        for cp in [*range(0x41, 0x5B), *range(0x61, 0x7B)]:
            assert not (classify_ctype1(cp) & int(Ctype1.DIGIT)), \
                f"cp 0x{cp:02x} ({chr(cp)!r}) should not have DIGIT"

    def test_digits_never_alpha(self):
        for cp in range(0x30, 0x3A):
            assert not (classify_ctype1(cp) & int(Ctype1.ALPHA)), \
                f"cp 0x{cp:02x} ({chr(cp)!r}) should not have ALPHA"

    def test_upper_never_lower(self):
        for cp in range(0x41, 0x5B):
            flags = classify_ctype1(cp)
            assert flags & int(Ctype1.UPPER)
            assert not (flags & int(Ctype1.LOWER)), \
                f"cp 0x{cp:02x} ({chr(cp)!r}) should not have LOWER"

    def test_lower_never_upper(self):
        for cp in range(0x61, 0x7B):
            flags = classify_ctype1(cp)
            assert flags & int(Ctype1.LOWER)
            assert not (flags & int(Ctype1.UPPER)), \
                f"cp 0x{cp:02x} ({chr(cp)!r}) should not have UPPER"

    # ── Punctuation ──────────────────────────────────────────────────────────

    def test_exclamation_is_punct(self):
        flags = classify_ctype1(ord('!'))
        assert flags & int(Ctype1.PUNCT)
        assert not (flags & int(Ctype1.ALPHA))
        assert not (flags & int(Ctype1.DIGIT))

    def test_at_is_punct(self):
        assert classify_ctype1(ord('@')) & int(Ctype1.PUNCT)

    def test_underscore_is_punct(self):
        assert classify_ctype1(ord('_')) & int(Ctype1.PUNCT)

    def test_tilde_is_punct(self):
        assert classify_ctype1(ord('~')) & int(Ctype1.PUNCT)

    # ── Coverage: every ASCII codepoint is classified ────────────────────────

    def test_no_unclassified_ascii(self):
        """Every codepoint 0x00–0x7F must produce a non-zero bitmask."""
        for cp in range(0x80):
            assert classify_ctype1(cp) != 0, \
                f"U+{cp:04X} should have at least one Ctype1 flag"

    # ── Out-of-range ─────────────────────────────────────────────────────────

    def test_non_ascii_returns_zero(self):
        assert classify_ctype1(0x80)   == 0
        assert classify_ctype1(0xFF)   == 0
        assert classify_ctype1(0x263A) == 0   # ☺ WHITE SMILING FACE
        assert classify_ctype1(0x10000) == 0  # beyond BMP


# ─────────────────────────────────────────────────────────────────────────────
# classify_wide_string
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyWideString:

    def test_rejects_ct_ctype2(self, mem):
        args = GetStringTypeArgs(info_type=CT_CTYPE2, src_ptr=SRC, cch_src=1, out_ptr=OUT)
        assert classify_wide_string(mem, args) is False

    def test_rejects_ct_ctype3(self, mem):
        args = GetStringTypeArgs(info_type=CT_CTYPE3, src_ptr=SRC, cch_src=1, out_ptr=OUT)
        assert classify_wide_string(mem, args) is False

    def test_accepts_ct_ctype1(self, mem):
        write_wide(mem, SRC, "A")
        args = GetStringTypeArgs(info_type=CT_CTYPE1, src_ptr=SRC, cch_src=1, out_ptr=OUT)
        assert classify_wide_string(mem, args) is True

    def test_explicit_count(self, mem):
        write_wide(mem, SRC, "Hello")
        args = GetStringTypeArgs(info_type=CT_CTYPE1, src_ptr=SRC, cch_src=5, out_ptr=OUT)
        classify_wide_string(mem, args)

        flags = read_flags(mem, OUT, 5)
        assert flags[0] == classify_ctype1(ord('H'))  # H: upper + alpha + xdigit
        assert flags[1] == classify_ctype1(ord('e'))  # e: lower + alpha + xdigit
        assert flags[2] == classify_ctype1(ord('l'))  # l: lower + alpha
        assert flags[3] == classify_ctype1(ord('l'))
        assert flags[4] == classify_ctype1(ord('o'))

    def test_null_terminated(self, mem):
        write_wide(mem, SRC, "Az")
        args = GetStringTypeArgs(
            info_type=CT_CTYPE1,
            src_ptr=SRC,
            cch_src=0xFFFFFFFF,  # −1: null-terminated
            out_ptr=OUT,
        )
        classify_wide_string(mem, args)

        assert mem.read16(OUT + 0) == classify_ctype1(ord('A'))
        assert mem.read16(OUT + 2) == classify_ctype1(ord('z'))
        # Byte beyond the two-character result must be untouched.
        assert mem.read16(OUT + 4) == 0

    def test_mixed_categories(self, mem):
        # "0 !" covers digit, space, punct
        write_wide(mem, SRC, "0 !")
        args = GetStringTypeArgs(info_type=CT_CTYPE1, src_ptr=SRC, cch_src=3, out_ptr=OUT)
        classify_wide_string(mem, args)

        assert mem.read16(OUT + 0) & int(Ctype1.DIGIT)
        assert mem.read16(OUT + 2) & int(Ctype1.SPACE)
        assert mem.read16(OUT + 4) & int(Ctype1.PUNCT)

    def test_empty_count_writes_nothing(self, mem):
        args = GetStringTypeArgs(info_type=CT_CTYPE1, src_ptr=SRC, cch_src=0, out_ptr=OUT)
        assert classify_wide_string(mem, args) is True
        # Output buffer must be untouched.
        assert mem.read16(OUT) == 0

    def test_null_terminated_empty_string(self, mem):
        # Write only a null terminator at SRC — length is zero.
        mem.write16(SRC, 0)
        args = GetStringTypeArgs(
            info_type=CT_CTYPE1, src_ptr=SRC, cch_src=0xFFFFFFFF, out_ptr=OUT
        )
        assert classify_wide_string(mem, args) is True
        assert mem.read16(OUT) == 0

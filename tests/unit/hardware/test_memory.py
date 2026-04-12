"""Tests for tew.hardware.memory.Memory."""

import pytest
from tew.hardware.memory import Memory


@pytest.fixture
def mem():
    return Memory(1024)


class TestConstructor:
    def test_size(self, mem):
        assert mem.size == 1024

    def test_zero_initialized(self, mem):
        for i in range(100):
            assert mem.read8(i) == 0


class TestEightBit:
    def test_write_read(self, mem):
        mem.write8(0, 0xFF)
        assert mem.read8(0) == 0xFF

    def test_signed_minus_one(self, mem):
        mem.write8(0, 0xFF)
        assert mem.read_signed8(0) == -1

    def test_signed_positive(self, mem):
        mem.write8(1, 0x7F)
        assert mem.read_signed8(1) == 127

    def test_signed_min(self, mem):
        mem.write8(2, 0x80)
        assert mem.read_signed8(2) == -128


class TestSixteenBit:
    def test_little_endian(self, mem):
        mem.write16(0, 0x1234)
        assert mem.read16(0) == 0x1234
        assert mem.read8(0) == 0x34   # low byte first
        assert mem.read8(1) == 0x12   # high byte second


class TestThirtyTwoBit:
    def test_little_endian(self, mem):
        mem.write32(0, 0x12345678)
        assert mem.read32(0) == 0x12345678
        assert mem.read8(0) == 0x78
        assert mem.read8(1) == 0x56
        assert mem.read8(2) == 0x34
        assert mem.read8(3) == 0x12

    def test_signed_minus_one(self, mem):
        mem.write32(0, 0xFFFFFFFF)
        assert mem.read_signed32(0) == -1

    def test_signed_max(self, mem):
        mem.write32(4, 0x7FFFFFFF)
        assert mem.read_signed32(4) == 2147483647

    def test_signed_min(self, mem):
        mem.write32(8, 0x80000000)
        assert mem.read_signed32(8) == -2147483648


class TestLoad:
    def test_load_bytes(self, mem):
        data = bytes([0xDE, 0xAD, 0xBE, 0xEF])
        mem.load(100, data)
        assert mem.read8(100) == 0xDE
        assert mem.read8(101) == 0xAD
        assert mem.read8(102) == 0xBE
        assert mem.read8(103) == 0xEF


class TestBoundsChecking:
    def test_read8_out_of_bounds(self, mem):
        with pytest.raises(Exception):
            mem.read8(1024)

    def test_read8_negative(self, mem):
        with pytest.raises(Exception):
            mem.read8(-1)

    def test_read32_near_end(self, mem):
        with pytest.raises(Exception):
            mem.read32(1021)   # needs 4 bytes, only 3 available

    def test_load_exceeds_bounds(self, mem):
        with pytest.raises(Exception):
            mem.load(1000, bytes(100))  # 1000 + 100 > 1024


class TestIsValidAddress:
    def test_valid(self, mem):
        assert mem.is_valid_address(0) is True
        assert mem.is_valid_address(512) is True
        assert mem.is_valid_address(1023) is True

    def test_invalid(self, mem):
        assert mem.is_valid_address(-1) is False
        assert mem.is_valid_address(1024) is False
        assert mem.is_valid_address(2000) is False


class TestIsValidRange:
    def test_valid(self, mem):
        assert mem.is_valid_range(0, 100) is True
        assert mem.is_valid_range(900, 124) is True

    def test_invalid(self, mem):
        assert mem.is_valid_range(1000, 100) is False
        assert mem.is_valid_range(0, 2000) is False

"""
Tests for ini_file.py — parse_ini, read_profile_string, read_profile_int,
write_profile_string, and write_profile_section.
"""

import pytest
from pathlib import Path

from tew.api.ini_file import (
    parse_ini,
    read_profile_string,
    read_profile_int,
    write_profile_string,
    write_profile_section,
)


# ── parse_ini ─────────────────────────────────────────────────────────────────

class TestParseIni:

    def test_empty_string_returns_empty_dict(self):
        assert parse_ini("") == {}

    def test_blank_lines_ignored(self):
        assert parse_ini("\n\n\n") == {}

    def test_semicolon_comments_skipped(self):
        ini = parse_ini("; this is a comment\n[s]\nk=v")
        assert ini == {"s": {"k": "v"}}

    def test_hash_comments_skipped(self):
        ini = parse_ini("# hash comment\n[s]\nk=v")
        assert ini == {"s": {"k": "v"}}

    def test_section_header_parsed(self):
        ini = parse_ini("[MySection]\nfoo=bar")
        assert "mysection" in ini

    def test_section_name_lowercased(self):
        ini = parse_ini("[UPPER]\nk=v")
        assert "upper" in ini
        assert "UPPER" not in ini

    def test_key_lowercased(self):
        ini = parse_ini("[s]\nMyKey=value")
        assert "mykey" in ini["s"]

    def test_value_case_preserved(self):
        ini = parse_ini("[s]\nk=HeLLo WoRLd")
        assert ini["s"]["k"] == "HeLLo WoRLd"

    def test_multiple_sections(self):
        text = "[alpha]\na=1\n[beta]\nb=2"
        ini = parse_ini(text)
        assert ini["alpha"]["a"] == "1"
        assert ini["beta"]["b"] == "2"

    def test_duplicate_key_last_wins(self):
        ini = parse_ini("[s]\nk=first\nk=second")
        assert ini["s"]["k"] == "second"

    def test_key_outside_section_ignored(self):
        ini = parse_ini("orphan=value\n[s]\nk=v")
        assert "orphan" not in ini.get("", {})
        # The key belongs to no section — it should not appear anywhere.
        for section_keys in ini.values():
            assert "orphan" not in section_keys

    def test_inline_value_not_stripped_of_comment(self):
        # Win32 treats ; inside a value as part of the value string.
        ini = parse_ini("[s]\nk=hello ; world")
        assert ini["s"]["k"] == "hello ; world"

    def test_value_surrounding_whitespace_stripped(self):
        ini = parse_ini("[s]\nk=  value  ")
        assert ini["s"]["k"] == "value"

    def test_key_surrounding_whitespace_stripped(self):
        ini = parse_ini("[s]\n  key  =value")
        assert "key" in ini["s"]

    def test_equals_in_value_allowed(self):
        ini = parse_ini("[s]\nk=a=b=c")
        assert ini["s"]["k"] == "a=b=c"


# ── read_profile_string ───────────────────────────────────────────────────────

class TestReadProfileString:

    INI_TEXT = """
[Game]
Title=Motor City Online
Version=1.0
[Network]
Port=9000
"""

    @pytest.fixture
    def ini(self):
        return parse_ini(self.INI_TEXT)

    def test_normal_lookup(self, ini):
        assert read_profile_string(ini, "Game", "Title", "") == "Motor City Online"

    def test_case_insensitive_section(self, ini):
        assert read_profile_string(ini, "GAME", "title", "") == "Motor City Online"

    def test_case_insensitive_key(self, ini):
        assert read_profile_string(ini, "game", "TITLE", "") == "Motor City Online"

    def test_missing_key_returns_default(self, ini):
        assert read_profile_string(ini, "Game", "missing", "DEF") == "DEF"

    def test_missing_section_returns_default(self, ini):
        assert read_profile_string(ini, "NoSection", "k", "fallback") == "fallback"

    def test_enumerate_sections(self, ini):
        result = read_profile_string(ini, None, None, "")
        sections = result.split("\0")
        assert "game" in sections
        assert "network" in sections

    def test_enumerate_sections_null_separated(self, ini):
        result = read_profile_string(ini, None, None, "")
        assert "\0" in result  # at least one separator

    def test_enumerate_keys_in_section(self, ini):
        result = read_profile_string(ini, "Game", None, "")
        keys = result.split("\0")
        assert "title" in keys
        assert "version" in keys

    def test_enumerate_empty_section(self, ini):
        ini_empty = parse_ini("[empty]\n")
        assert read_profile_string(ini_empty, "empty", None, "") == ""

    def test_enumerate_sections_on_empty_ini(self):
        assert read_profile_string({}, None, None, "") == ""


# ── read_profile_int ──────────────────────────────────────────────────────────

class TestReadProfileInt:

    def _ini(self, value: str):
        return parse_ini(f"[s]\nk={value}")

    def test_decimal_value(self):
        assert read_profile_int(self._ini("42"), "s", "k", 0) == 42

    def test_hex_value(self):
        assert read_profile_int(self._ini("0xFF"), "s", "k", 0) == 255

    def test_leading_zero_parsed_as_decimal(self):
        # Win32 GetPrivateProfileIntA uses strtol base-10.
        # "010" is decimal 10, not C-style octal 8.
        assert read_profile_int(self._ini("010"), "s", "k", 0) == 10

    def test_missing_key_returns_default(self):
        ini = parse_ini("[s]\nother=1")
        assert read_profile_int(ini, "s", "missing", 99) == 99

    def test_missing_section_returns_default(self):
        assert read_profile_int({}, "s", "k", 7) == 7

    def test_non_numeric_value_returns_default(self):
        assert read_profile_int(self._ini("not_a_number"), "s", "k", 5) == 5

    def test_negative_decimal(self):
        assert read_profile_int(self._ini("-1"), "s", "k", 0) == -1

    def test_case_insensitive_lookup(self):
        ini = parse_ini("[Section]\nKey=100")
        assert read_profile_int(ini, "SECTION", "KEY", 0) == 100


# ── write_profile_string ──────────────────────────────────────────────────────

class TestWriteProfileString:

    def test_create_new_file_and_key(self, tmp_path):
        p = str(tmp_path / "test.ini")
        assert write_profile_string(p, "Section", "key", "hello")
        ini = parse_ini(open(p).read())
        assert ini["section"]["key"] == "hello"

    def test_add_key_to_existing_section(self, tmp_path):
        p = str(tmp_path / "test.ini")
        write_profile_string(p, "s", "a", "1")
        write_profile_string(p, "s", "b", "2")
        ini = parse_ini(open(p).read())
        assert ini["s"]["a"] == "1"
        assert ini["s"]["b"] == "2"

    def test_update_existing_key(self, tmp_path):
        p = str(tmp_path / "test.ini")
        write_profile_string(p, "s", "k", "old")
        write_profile_string(p, "s", "k", "new")
        ini = parse_ini(open(p).read())
        assert ini["s"]["k"] == "new"

    def test_delete_key(self, tmp_path):
        p = str(tmp_path / "test.ini")
        write_profile_string(p, "s", "keep", "yes")
        write_profile_string(p, "s", "drop", "gone")
        write_profile_string(p, "s", "drop", None)   # delete
        ini = parse_ini(open(p).read())
        assert "drop" not in ini.get("s", {})
        assert ini["s"]["keep"] == "yes"

    def test_delete_section(self, tmp_path):
        p = str(tmp_path / "test.ini")
        write_profile_string(p, "keep", "k", "v")
        write_profile_string(p, "drop", "k", "v")
        write_profile_string(p, "drop", None, None)   # delete section
        ini = parse_ini(open(p).read())
        assert "drop" not in ini
        assert "keep" in ini

    def test_app_name_none_returns_false(self, tmp_path):
        p = str(tmp_path / "test.ini")
        assert write_profile_string(p, None, "k", "v") is False

    def test_creates_intermediate_dirs(self, tmp_path):
        p = str(tmp_path / "sub" / "dir" / "test.ini")
        assert write_profile_string(p, "s", "k", "v")
        assert Path(p).exists()

    def test_delete_key_on_nonexistent_file_returns_true(self, tmp_path):
        # Deleting a key from a file that doesn't exist is a no-op success.
        p = str(tmp_path / "ghost.ini")
        assert write_profile_string(p, "s", "k", None) is True

    def test_delete_section_on_nonexistent_file_returns_true(self, tmp_path):
        p = str(tmp_path / "ghost.ini")
        assert write_profile_string(p, "s", None, None) is True


# ── write_profile_section ─────────────────────────────────────────────────────

class TestWriteProfileSection:

    def test_create_section(self, tmp_path):
        p = str(tmp_path / "test.ini")
        assert write_profile_section(p, "Game", {"title": "MCO", "version": "1"})
        ini = parse_ini(open(p).read())
        assert ini["game"]["title"] == "MCO"
        assert ini["game"]["version"] == "1"

    def test_replace_existing_section(self, tmp_path):
        p = str(tmp_path / "test.ini")
        write_profile_section(p, "s", {"a": "1", "b": "2"})
        write_profile_section(p, "s", {"c": "3"})
        ini = parse_ini(open(p).read())
        assert "a" not in ini["s"]
        assert ini["s"]["c"] == "3"

    def test_other_sections_preserved(self, tmp_path):
        p = str(tmp_path / "test.ini")
        write_profile_section(p, "alpha", {"x": "1"})
        write_profile_section(p, "beta",  {"y": "2"})
        ini = parse_ini(open(p).read())
        assert ini["alpha"]["x"] == "1"
        assert ini["beta"]["y"] == "2"

    def test_app_name_none_returns_false(self, tmp_path):
        p = str(tmp_path / "test.ini")
        assert write_profile_section(p, None, {"k": "v"}) is False

    def test_empty_pairs_creates_empty_section(self, tmp_path):
        p = str(tmp_path / "test.ini")
        assert write_profile_section(p, "empty", {})
        ini = parse_ini(open(p).read())
        assert ini.get("empty") == {}

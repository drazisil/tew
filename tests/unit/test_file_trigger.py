"""FileTextTrigger: fire-once "text appeared in a growing file" trigger."""

import pytest

from tew.file_trigger import FileTextTrigger


def append(path, text):
    with open(path, "a", encoding="latin-1") as f:
        f.write(text)


def test_does_not_fire_on_text_already_in_the_file(tmp_path):
    # The game's logs persist across runs -- a stale copy must not fire.
    log = tmp_path / "MCity_Log.txt"
    log.write_text("Done Getting Personas\n")
    trig = FileTextTrigger(str(log), "Done Getting Personas")
    assert trig.poll() is False
    assert trig.fired is False


def test_fires_once_when_text_is_appended(tmp_path):
    log = tmp_path / "MCity_Log.txt"
    log.write_text("old line\n")
    trig = FileTextTrigger(str(log), "Done Getting Personas")
    assert trig.poll() is False
    append(log, "Persona_DownloadList: ok\n")
    assert trig.poll() is False
    append(log, "Done Getting Personas\n")
    assert trig.poll() is True
    assert trig.fired is True
    assert trig.poll() is False  # only ever once
    append(log, "Done Getting Personas\n")
    assert trig.poll() is False


def test_matches_text_split_across_two_appends(tmp_path):
    log = tmp_path / "MCity_Log.txt"
    log.write_text("")
    trig = FileTextTrigger(str(log), "Done Getting Personas")
    append(log, "xx Done Getting ")
    assert trig.poll() is False
    append(log, "Personas yy")
    assert trig.poll() is True


def test_file_created_after_the_trigger_counts_from_the_start(tmp_path):
    log = tmp_path / "later.txt"
    trig = FileTextTrigger(str(log), "READY")
    assert trig.poll() is False  # missing file is fine, not an error
    log.write_text("boot...\nREADY\n")
    assert trig.poll() is True


def test_truncated_or_replaced_file_is_read_from_the_start(tmp_path):
    log = tmp_path / "MCity_Log.txt"
    log.write_text("a" * 500)
    trig = FileTextTrigger(str(log), "READY")
    assert trig.poll() is False
    log.write_text("READY")  # replaced by a shorter file (new run)
    assert trig.poll() is True


def test_no_match_never_fires(tmp_path):
    log = tmp_path / "MCity_Log.txt"
    log.write_text("")
    trig = FileTextTrigger(str(log), "Done Getting Personas")
    for line in ("one\n", "two\n", "Done Getting Persona\n"):
        append(log, line)
        assert trig.poll() is False


def test_empty_text_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        FileTextTrigger(str(tmp_path / "x"), "")

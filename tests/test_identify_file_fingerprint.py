import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from karaoke.identify import identify_file_fingerprint

def test_identify_file_fingerprint_success(tmp_path):
    dummy_audio = tmp_path / "test.mp3"
    dummy_audio.write_bytes(b"dummy")

    mock_json = '{"track": {"subtitle": "Portishead", "title": "Glory Box", "sections": [{"metadata": [{"title": "Album", "text": "Dummy"}]}]}, "matches": [{"offset": 12.0}]}'
    with patch("shutil.which", return_value="/usr/bin/songrec"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = mock_json
        mock_run.return_value.returncode = 0

        res = identify_file_fingerprint(dummy_audio)
        assert res is not None
        assert res.artist == "Portishead"
        assert res.title == "Glory Box"
        assert res.album == "Dummy"
        assert res.source == "fingerprint"
        assert res.offset == 12.0

def test_identify_file_fingerprint_missing_file(tmp_path):
    missing = tmp_path / "nonexistent.mp3"
    assert identify_file_fingerprint(missing) is None

def test_identify_file_fingerprint_no_songrec():
    with patch("shutil.which", return_value=None):
        assert identify_file_fingerprint("/tmp/test.mp3") is None


def test_file_fingerprint_passes_the_file_positionally(tmp_path):
    """`-d` is --audio-device, so a path given there is read as a mic name.

    songrec then printed its device list to stdout, json.loads failed, the
    bare except swallowed it, and every fingerprint returned None while
    exiting 0 -- indistinguishable from "no match". Whole-library scans
    reported `fingerprinted: 0` for months. The other tests here mock
    subprocess.run without inspecting the command, so nothing caught it.
    """
    dummy = tmp_path / "test.mp3"
    dummy.write_bytes(b"dummy")
    calls = []

    def record(cmd, *a, **kw):
        calls.append(cmd)
        out = MagicMock()
        out.stdout = '{"track": {"subtitle": "A", "title": "B"}, "matches": []}'
        out.returncode = 0
        return out

    with patch("shutil.which", return_value="/usr/bin/songrec"), \
         patch("subprocess.run", side_effect=record):
        identify_file_fingerprint(dummy)

    recognize = next(c for c in calls if c[:2] == ["songrec", "recognize"])
    assert "-d" not in recognize, "a file must not be passed as --audio-device"
    assert any(str(c).endswith(".wav") for c in recognize), \
        "the sliced wav should be a positional argument"


def test_live_identification_still_uses_the_device_flag():
    """The mic path is the one place `-d` is right, and must keep it.

    identify_live passes a pactl source name like
    alsa_input.pci-0000_c1_00.6.HiFi__Mic2__source; fixing the file path
    must not disturb it.
    """
    from karaoke.identify import identify_live

    calls = []

    def record(cmd, *a, **kw):
        calls.append(cmd)
        out = MagicMock()
        out.stdout = '{"track": {"subtitle": "A", "title": "B"}, "matches": []}'
        out.returncode = 0
        return out

    with patch("shutil.which", return_value="/usr/bin/songrec"), \
         patch("karaoke.identify._default_source", return_value="alsa_input.test"), \
         patch("subprocess.run", side_effect=record):
        identify_live(mic=True)

    assert calls, "songrec should have been invoked"
    cmd = calls[0]
    assert "-d" in cmd and cmd[cmd.index("-d") + 1] == "alsa_input.test"

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from karaoke.folder_scan import scan_and_ingest_folder


def test_scan_and_ingest_folder_dry_run(tmp_path):
    test_file = tmp_path / "song.mp3"
    test_file.write_bytes(b"dummy")

    fake_tags = MagicMock(artist="Portishead", title="Glory Box", album="Dummy",
                          duration=300.0, path=str(test_file))

    with patch("karaoke.tags.is_audio", return_value=True), \
         patch("karaoke.tags.extract_tags", return_value=fake_tags), \
         patch("karaoke.identify.identify_file_fingerprint", return_value=None), \
         patch("karaoke.analyze.analyze_audio", return_value=MagicMock(key=None, bpm=None)), \
         patch("karaoke.clap_vector.available", return_value=False):
        stats = scan_and_ingest_folder(tmp_path, dry_run=True, resolve_streaming=False)
        assert stats["seen"] == 1
        assert stats["processed"] == 1
        assert stats["items"][0]["artist"] == "Portishead"
        assert stats["items"][0]["title"] == "Glory Box"


def test_scan_missing_dir():
    with pytest.raises(FileNotFoundError):
        scan_and_ingest_folder("/nonexistent/path/xyz")


def test_scan_only_paths_restricts_to_named_files(tmp_path):
    """only_paths (the retry pass) processes just the listed files."""
    keep = tmp_path / "keep.mp3"
    skip = tmp_path / "skip.mp3"
    keep.write_bytes(b"dummy")
    skip.write_bytes(b"dummy")

    fake_tags = MagicMock(artist="Portishead", title="Glory Box", album="Dummy",
                          duration=300.0)

    with patch("karaoke.tags.is_audio", return_value=True), \
         patch("karaoke.tags.extract_tags", return_value=fake_tags), \
         patch("karaoke.identify.identify_file_fingerprint", return_value=None), \
         patch("karaoke.analyze.analyze_audio", return_value=MagicMock(key=None, bpm=None)), \
         patch("karaoke.clap_vector.available", return_value=False):
        stats = scan_and_ingest_folder(
            tmp_path, dry_run=True, resolve_streaming=False,
            only_paths={str(keep)},
        )
    assert stats["seen"] == 1
    assert stats["items"][0]["path"] == str(keep)


def test_scan_uses_fingerprint_when_tags_missing(tmp_path):
    test_file = tmp_path / "track01.mp3"
    test_file.write_bytes(b"dummy")

    fake_tags = MagicMock(artist="", title="track01", album="", duration=None, path=str(test_file))
    fake_fp = MagicMock(artist="Radiohead", title="Creep", album="Pablo Honey")

    with patch("karaoke.tags.is_audio", return_value=True), \
         patch("karaoke.tags.extract_tags", return_value=fake_tags), \
         patch("karaoke.folder_scan.identify_file_fingerprint", return_value=fake_fp), \
         patch("karaoke.analyze.analyze_audio", return_value=MagicMock(key=None, bpm=None)), \
         patch("karaoke.clap_vector.available", return_value=False):
        stats = scan_and_ingest_folder(tmp_path, dry_run=True, resolve_streaming=False)
        assert stats["fingerprinted"] == 1
        assert stats["items"][0]["artist"] == "Radiohead"
        assert stats["items"][0]["title"] == "Creep"


def test_scan_uses_filename_fallback_when_tags_and_fingerprint_missing(tmp_path):
    test_file = tmp_path / "07 Gadjo - Besame Mucho.mp3"
    test_file.write_bytes(b"dummy")

    fake_tags = MagicMock(artist="", title="07 Gadjo - Besame Mucho", album="", duration=None, path=str(test_file))

    with patch("karaoke.tags.is_audio", return_value=True), \
         patch("karaoke.tags.extract_tags", return_value=fake_tags), \
         patch("karaoke.folder_scan.identify_file_fingerprint", return_value=None), \
         patch("karaoke.analyze.analyze_audio", return_value=MagicMock(key=None, bpm=None)), \
         patch("karaoke.clap_vector.available", return_value=False):
        stats = scan_and_ingest_folder(tmp_path, dry_run=True, resolve_streaming=False)
        assert stats["processed"] == 1
        assert stats["items"][0]["artist"] == "Gadjo"
        assert stats["items"][0]["title"] == "Besame Mucho"


def test_scan_uses_filename_fallback_no_spaces_hyphen(tmp_path):
    test_file = tmp_path / "Andalousie-Ferreri & Tio Ferret.mp3"
    test_file.write_bytes(b"dummy")

    fake_tags = MagicMock(artist="", title="Andalousie-Ferreri & Tio Ferret", album="", duration=None, path=str(test_file))

    with patch("karaoke.tags.is_audio", return_value=True), \
         patch("karaoke.tags.extract_tags", return_value=fake_tags), \
         patch("karaoke.folder_scan.identify_file_fingerprint", return_value=None), \
         patch("karaoke.analyze.analyze_audio", return_value=MagicMock(key=None, bpm=None)), \
         patch("karaoke.clap_vector.available", return_value=False):
        stats = scan_and_ingest_folder(tmp_path, dry_run=True, resolve_streaming=False)
        assert stats["processed"] == 1
        assert stats["items"][0]["artist"] == "Andalousie"
        assert stats["items"][0]["title"] == "Ferreri & Tio Ferret"


def test_scan_reports_progress_events(tmp_path):
    test_file = tmp_path / "song.mp3"
    test_file.write_bytes(b"dummy")

    fake_tags = MagicMock(artist="Portishead", title="Glory Box", album="Dummy",
                          duration=300.0, path=str(test_file))
    events = []

    with patch("karaoke.tags.is_audio", return_value=True), \
         patch("karaoke.tags.extract_tags", return_value=fake_tags), \
         patch("karaoke.identify.identify_file_fingerprint", return_value=None), \
         patch("karaoke.analyze.analyze_audio", return_value=MagicMock(key=None, bpm=None)), \
         patch("karaoke.clap_vector.available", return_value=False):
        stats = scan_and_ingest_folder(
            tmp_path,
            dry_run=True,
            resolve_streaming=False,
            progress=lambda event, payload: events.append((event, payload.copy())),
        )

    names = [event for event, _ in events]
    assert names == ["found", "item_start", "tagged", "analysis_start", "analysis_done", "item_done", "done"]
    assert events[0][1]["total"] == 1
    assert events[-1][1]["processed"] == stats["processed"] == 1

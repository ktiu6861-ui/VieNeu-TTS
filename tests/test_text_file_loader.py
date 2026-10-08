from pathlib import Path

import pytest

from main import load_text_from_file


def test_load_text_from_file_reads_utf8(tmp_path: Path) -> None:
    path = tmp_path / "script.txt"
    path.write_text("Xin chào\nĐây là nội dung từ file.", encoding="utf-8")

    assert load_text_from_file(path) == "Xin chào\nĐây là nội dung từ file."


def test_load_text_from_file_rejects_non_text_extension(tmp_path: Path) -> None:
    path = tmp_path / "script.wav"
    path.write_bytes(b"not-a-text-file")

    with pytest.raises(ValueError, match=".txt"):
        load_text_from_file(path)

from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

REQUIRED_CLONE_VOICES = {"ban mai", "bích ngọc"}


def copy_clone_voices(source_data: Path, target_data: Path) -> int:
    index_path = source_data / "voices.json"
    if not index_path.is_file():
        return 0

    items = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError(f"Invalid cloned voice index: {index_path}")

    copied_items = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        filename = item.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            continue
        source_audio = source_data / "voices" / filename
        if not source_audio.is_file():
            continue
        target_voices = target_data / "voices"
        target_voices.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_audio, target_voices / filename)
        copied_items.append(item)

    copied_names = {
        item["name"].strip().casefold()
        for item in copied_items
        if isinstance(item.get("name"), str)
    }
    missing = REQUIRED_CLONE_VOICES - copied_names
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(
            f"Required cloned voices are missing or incomplete: {names}. "
            f"Check {index_path} and its voices directory before building."
        )

    if copied_items:
        target_data.mkdir(parents=True, exist_ok=True)
        (target_data / "voices.json").write_text(
            json.dumps(copied_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return len(copied_items)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    bundle = project_root / "dist" / "offline-build" / "GiongViet"
    archive = project_root / "dist" / "GiongViet-offline-windows-x64.zip"

    if not (bundle / "GiongViet.exe").is_file():
        print(f"Portable executable is missing: {bundle / 'GiongViet.exe'}", file=sys.stderr)
        return 1
    if not (bundle / "_internal" / "hf-cache" / "hub").is_dir():
        print("Bundled offline model cache is missing.", file=sys.stderr)
        return 1

    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    source_data = local_app_data / "GiongViet" / "GiongViet"
    try:
        voice_count = copy_clone_voices(source_data, bundle / "user-data")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Could not package cloned voices: {exc}", file=sys.stderr)
        return 1

    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive = archive.with_suffix(".zip.tmp")
    temporary_archive.unlink(missing_ok=True)
    files = [path for path in bundle.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    print(f"Creating ZIP64 archive: {len(files)} files, {total_bytes / (1024 ** 3):.2f} GiB...")

    with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as package:
        for path in files:
            package.write(path, Path("GiongViet") / path.relative_to(bundle))

    with zipfile.ZipFile(temporary_archive) as package:
        bad_file = package.testzip()
        if bad_file:
            raise RuntimeError(f"ZIP CRC validation failed for {bad_file}")
    temporary_archive.replace(archive)

    print(f"PORTABLE_ZIP_OK: {archive} ({archive.stat().st_size / (1024 ** 3):.2f} GiB)")
    print(f"Packaged cloned voices: {voice_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

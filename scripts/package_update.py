from __future__ import annotations

import hashlib
import json
import tomllib
import zipfile
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    bundle = project_root / "dist" / "offline-build" / "GiongViet"
    archive = project_root / "dist" / "GiongViet-update-windows-x64.zip"
    manifest_path = project_root / "dist" / "GiongViet-update-manifest.json"
    if not (bundle / "GiongViet.exe").is_file():
        raise FileNotFoundError(f"Portable executable is missing: {bundle / 'GiongViet.exe'}")

    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(".zip.tmp")
    files = []
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle)
        if relative.parts[0] == "user-data":
            continue
        if relative.parts[:2] == ("_internal", "hf-cache"):
            continue
        files.append((path, Path("GiongViet") / relative))

    with zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
    ) as package:
        for source, member in files:
            package.write(source, member)
    temporary.replace(archive)
    digest = hashlib.sha256()
    with archive.open("rb") as data:
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            digest.update(chunk)
    manifest = {
        "version": version,
        "archive": archive.name,
        "sha256": digest.hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"UPDATE_PACKAGE_OK: {archive} ({archive.stat().st_size / (1024 ** 3):.2f} GiB)")
    print(f"UPDATE_MANIFEST_OK: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

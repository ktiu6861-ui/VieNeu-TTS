from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
import zipfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--base-bundle", type=Path)
    parser.add_argument("--base-version")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    bundle = (args.bundle or (project_root / "dist" / "offline-build" / "GiongViet")).resolve()
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
    if args.base_bundle or args.base_version:
        if not args.base_bundle or not args.base_version:
            raise ValueError("--base-bundle and --base-version must be provided together")
        base_bundle = args.base_bundle.resolve()
        if not (base_bundle / "GiongViet.exe").is_file():
            raise FileNotFoundError(f"Base portable executable is missing: {base_bundle / 'GiongViet.exe'}")

        def eligible_files(folder: Path) -> dict[Path, Path]:
            result = {}
            for candidate in folder.rglob("*"):
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(folder)
                if relative.parts[0] == "user-data" or relative.parts[:2] == ("_internal", "hf-cache"):
                    continue
                result[relative] = candidate
            return result

        current_files = eligible_files(bundle)
        base_files = eligible_files(base_bundle)
        removed = set(base_files) - set(current_files)
        if removed:
            print(f"DELTA_SKIPPED: {len(removed)} removed files require a full update")
        else:
            changed = []
            for relative, current in current_files.items():
                previous = base_files.get(relative)
                if previous is None or current.stat().st_size != previous.stat().st_size:
                    changed.append((relative, current))
                elif _sha256(current) != _sha256(previous):
                    changed.append((relative, current))
            delta_name = f"GiongViet-update-delta-{args.base_version}.zip"
            delta_path = archive.parent / delta_name
            delta_tmp = delta_path.with_suffix(".zip.tmp")
            with zipfile.ZipFile(
                delta_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
            ) as package:
                for relative, source in changed:
                    package.write(source, Path("GiongViet") / relative)
            delta_tmp.replace(delta_path)
            manifest["deltas"] = {
                args.base_version: {
                    "archive": delta_name,
                    "sha256": _sha256(delta_path),
                }
            }
            print(
                f"DELTA_PACKAGE_OK: {delta_path} ({delta_path.stat().st_size / (1024 ** 2):.1f} MiB, "
                f"{len(changed)} changed files from {args.base_version})"
            )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"UPDATE_PACKAGE_OK: {archive} ({archive.stat().st_size / (1024 ** 3):.2f} GiB)")
    print(f"UPDATE_MANIFEST_OK: {manifest_path}")
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

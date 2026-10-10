from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_VERSION = "0.1.11"
GITHUB_REPOSITORY = "ktiu6861-ui/VieNeu-TTS"
UPDATE_ARCHIVE = "GiongViet-update-windows-x64.zip"
UPDATE_MANIFEST = "GiongViet-update-manifest.json"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"


def _version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", value.strip(), flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid release version: {value}")
    return tuple(int(part) for part in match.group(1).split("."))


def _request(url: str, *, github_api: bool = False):
    headers = {"User-Agent": "GiongViet-Updater"}
    if github_api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)


def check_latest_version() -> tuple[str, str]:
    """Check the latest release without downloading its update package."""
    with _request(RELEASES_API, github_api=True) as response:
        release = json.loads(response.read().decode("utf-8"))
    tag = release.get("tag_name", "")
    if _version_key(tag) <= _version_key(APP_VERSION):
        return "current", APP_VERSION
    return "available", tag.lstrip("vV")


def download_latest_update(progress=None) -> tuple[str, str]:
    """Download and verify the update asset from the repository's latest release."""
    with _request(RELEASES_API, github_api=True) as response:
        release = json.loads(response.read().decode("utf-8"))

    tag = release.get("tag_name", "")
    latest_key = _version_key(tag)
    if latest_key <= _version_key(APP_VERSION):
        return "current", "Bạn đang dùng phiên bản mới nhất."

    assets = {item.get("name"): item for item in release.get("assets", [])}
    if UPDATE_MANIFEST not in assets or UPDATE_ARCHIVE not in assets:
        raise ValueError(
            "Bản phát hành mới chưa có đủ gói cập nhật. Hãy thử lại sau khi người phát hành tải đủ tệp."
        )

    with _request(assets[UPDATE_MANIFEST]["browser_download_url"]) as response:
        manifest = json.loads(response.read().decode("utf-8"))
    version = manifest.get("version", "")
    expected_hash = manifest.get("sha256", "").lower()
    if version != tag.lstrip("vV") or manifest.get("archive") != UPDATE_ARCHIVE:
        raise ValueError("Thông tin phiên bản trong gói cập nhật không khớp bản phát hành.")
    archive_name = UPDATE_ARCHIVE
    deltas = manifest.get("deltas", {})
    delta = deltas.get(APP_VERSION) if isinstance(deltas, dict) else None
    if isinstance(delta, dict):
        delta_name = delta.get("archive")
        delta_hash = str(delta.get("sha256", "")).lower()
        if (
            isinstance(delta_name, str)
            and Path(delta_name).name == delta_name
            and delta_name.startswith("GiongViet-update-delta-")
            and delta_name in assets
            and re.fullmatch(r"[0-9a-f]{64}", delta_hash)
        ):
            archive_name = delta_name
            expected_hash = delta_hash
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("Bản phát hành thiếu mã kiểm tra SHA-256 hợp lệ.")

    task_dir = Path(tempfile.mkdtemp(prefix="giong-viet-update-"))
    archive_path = task_dir / archive_name
    total = int(assets[archive_name].get("size", 0))
    try:
        asset_url = assets[archive_name]["browser_download_url"]
        last_error = None
        for attempt in range(5):
            downloaded = archive_path.stat().st_size if archive_path.exists() else 0
            if total and downloaded > total:
                archive_path.unlink(missing_ok=True)
                downloaded = 0
            elif total and downloaded == total:
                break
            request = urllib.request.Request(asset_url, headers={"User-Agent": "GiongViet-Updater"})
            if downloaded:
                request.add_header("Range", f"bytes={downloaded}-")
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    status_code = getattr(response, "status", response.getcode())
                    content_range = response.headers.get("Content-Range", "")
                    range_match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                    if downloaded and (
                        status_code != 206
                        or not range_match
                        or int(range_match.group(1)) != downloaded
                    ):
                        archive_path.unlink(missing_ok=True)
                        downloaded = 0
                        if status_code == 206:
                            raise OSError("Update server returned an invalid resume range.")

                    if not total:
                        if range_match and range_match.group(3) != "*":
                            total = int(range_match.group(3))
                        else:
                            total = int(response.headers.get("Content-Length", 0)) + downloaded

                    digest = hashlib.sha256()
                    mode = "ab" if downloaded and status_code == 206 else "wb"
                    if mode == "ab":
                        with archive_path.open("rb") as existing:
                            while existing_chunk := existing.read(1024 * 1024):
                                digest.update(existing_chunk)
                    else:
                        downloaded = 0

                    read_error = None
                    with archive_path.open(mode) as target:
                        while True:
                            try:
                                chunk = response.read(1024 * 1024)
                            except (OSError, http.client.HTTPException) as exc:
                                read_error = exc
                                break
                            if not chunk:
                                break
                            target.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            if progress and total:
                                progress(min(99, downloaded * 100 // total))

                    if read_error:
                        last_error = read_error
                    elif total and downloaded < total:
                        last_error = ConnectionError(
                            f"Update download ended early ({downloaded} of {total} bytes)."
                        )
                    elif total and downloaded > total:
                        archive_path.unlink(missing_ok=True)
                        last_error = ConnectionError("Update download exceeded the expected file size.")
                    else:
                        break
            except InterruptedError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
                last_error = exc

            if attempt < 4:
                if progress and total and archive_path.exists():
                    progress(min(99, archive_path.stat().st_size * 100 // total))
                time.sleep(min(2 ** attempt, 8))
        else:
            raise RuntimeError(f"Không thể tải tiếp gói cập nhật sau nhiều lần thử: {last_error}") from last_error
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    digest = hashlib.sha256()
    with archive_path.open("rb") as downloaded_file:
        while chunk := downloaded_file.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        archive_path.unlink(missing_ok=True)
        raise ValueError("Mã SHA-256 của gói cập nhật không khớp; gói tải về đã bị xóa.")
    if progress:
        progress(100)
    return str(archive_path), version


def launch_update_installer(archive_path: str, process_id: int) -> None:
    """Start a detached Windows helper that swaps app files and preserves user data."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Tính năng cập nhật chỉ dùng được trong bản portable.")

    install_dir = Path(sys.executable).resolve().parent
    executable = install_dir / "GiongViet.exe"
    archive = Path(archive_path).resolve()
    if not executable.is_file() or not archive.is_file():
        raise FileNotFoundError("Không tìm thấy ứng dụng hoặc gói cập nhật đã tải.")

    script = r"""
param([string]$Archive, [string]$InstallDir, [string]$Executable, [int]$ProcessId, [switch]$Delta)
$ErrorActionPreference = 'Stop'
$parent = Split-Path -Parent $InstallDir
$task = Join-Path $parent ('.GiongViet-update-' + [guid]::NewGuid().ToString('N'))
$backup = $InstallDir + '.previous-' + [guid]::NewGuid().ToString('N')
$readyMarker = Join-Path $InstallDir '.GiongViet-update-ready'
$swapped = $false
$changes = [System.Collections.Generic.List[object]]::new()
function Start-UpdatedApp {
    New-Item -ItemType File -Path $readyMarker -Force | Out-Null
    $env:GIONGVIET_UPDATE_READY_MARKER = $readyMarker
    $started = Start-Process -FilePath $Executable -WorkingDirectory $InstallDir -PassThru
    Remove-Item Env:\GIONGVIET_UPDATE_READY_MARKER -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddMinutes(5)
    while ((Test-Path -LiteralPath $readyMarker) -and (Get-Date) -lt $deadline) {
        $started.Refresh()
        if ($started.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    Remove-Item -LiteralPath $readyMarker -Force -ErrorAction SilentlyContinue
}
try {
    while (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 1 }
    New-Item -ItemType Directory -Path $task | Out-Null
    $stage = Join-Path $task 'stage'
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Archive, $stage)
    $payload = Join-Path $stage 'GiongViet'
    if (-not (Test-Path (Join-Path $payload 'GiongViet.exe'))) { throw 'Gói cập nhật thiếu GiongViet.exe.' }
    if (-not $Delta -and -not (Test-Path (Join-Path $payload '_internal'))) { throw 'Gói cập nhật thiếu runtime.' }
    if ($Delta) {
        $backupFiles = Join-Path $task 'backup'
        $rootPrefix = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\') + '\'
        foreach ($file in Get-ChildItem -LiteralPath $payload -File -Recurse) {
            $relative = $file.FullName.Substring($payload.Length).TrimStart([char]'\', [char]'/')
            if ($relative.StartsWith('user-data\', [System.StringComparison]::OrdinalIgnoreCase) -or
                $relative.StartsWith('_internal\hf-cache\', [System.StringComparison]::OrdinalIgnoreCase) -or
                ([regex]::Split($relative, '[\\/]') -contains '..')) { throw 'Delta package contains protected or invalid paths.' }
            $target = [System.IO.Path]::GetFullPath((Join-Path $InstallDir $relative))
            if (-not $target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Delta path escaped the install directory.' }
            $backupFile = Join-Path $backupFiles $relative
            $hadOld = Test-Path -LiteralPath $target
            $changes.Add([pscustomobject]@{ Relative = $relative; HadOld = $hadOld })
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            if ($hadOld) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $backupFile) -Force | Out-Null
                Move-Item -LiteralPath $target -Destination $backupFile
            }
            Move-Item -LiteralPath $file.FullName -Destination $target
        }
        Start-UpdatedApp
        Remove-Item -LiteralPath $task -Recurse -Force
        Remove-Item -LiteralPath $Archive -Force
        exit 0
    }
    Move-Item -LiteralPath $InstallDir -Destination $backup
    $oldData = Join-Path $backup 'user-data'
    if (Test-Path $oldData) { Move-Item -LiteralPath $oldData -Destination (Join-Path $payload 'user-data') }
    $oldCache = Join-Path $backup '_internal\hf-cache'
    if (Test-Path $oldCache) {
        $newInternal = Join-Path $payload '_internal'
        New-Item -ItemType Directory -Path $newInternal -Force | Out-Null
        Move-Item -LiteralPath $oldCache -Destination (Join-Path $newInternal 'hf-cache')
    }
    Move-Item -LiteralPath $payload -Destination $InstallDir
    $swapped = $true
    Start-UpdatedApp
    Remove-Item -LiteralPath $backup -Recurse -Force
    Remove-Item -LiteralPath $task -Recurse -Force
    Remove-Item -LiteralPath $Archive -Force
} catch {
    Remove-Item -LiteralPath $readyMarker -Force -ErrorAction SilentlyContinue
    if ($Delta) {
        for ($i = $changes.Count - 1; $i -ge 0; $i--) {
            $change = $changes[$i]
            $target = Join-Path $InstallDir $change.Relative
            $backupFile = Join-Path (Join-Path $task 'backup') $change.Relative
            if (Test-Path -LiteralPath $backupFile) {
                if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                Move-Item -LiteralPath $backupFile -Destination $target
            } elseif (-not $change.HadOld -and (Test-Path -LiteralPath $target)) {
                Remove-Item -LiteralPath $target -Force
            }
        }
        if (Test-Path -LiteralPath $Executable) { Start-Process -FilePath $Executable }
    } elseif (-not $swapped -and (Test-Path $backup) -and -not (Test-Path $InstallDir)) {
        $payload = Join-Path $task 'stage\GiongViet'
        $stagedData = Join-Path $payload 'user-data'
        if ((Test-Path $stagedData) -and -not (Test-Path (Join-Path $backup 'user-data'))) {
            Move-Item -LiteralPath $stagedData -Destination (Join-Path $backup 'user-data')
        }
        $stagedCache = Join-Path $payload '_internal\hf-cache'
        if ((Test-Path $stagedCache) -and -not (Test-Path (Join-Path $backup '_internal\hf-cache'))) {
            New-Item -ItemType Directory -Path (Join-Path $backup '_internal') -Force | Out-Null
            Move-Item -LiteralPath $stagedCache -Destination (Join-Path $backup '_internal\hf-cache')
        }
        Move-Item -LiteralPath $backup -Destination $InstallDir
        Start-Process -FilePath $Executable
    }
    $log = Join-Path $env:TEMP 'GiongViet-update-error.txt'
    $_ | Out-String | Set-Content -LiteralPath $log -Encoding UTF8
}
"""
    task_dir = archive.parent
    script_path = task_dir / "apply-update.ps1"
    script_path.write_text(script, encoding="utf-8")
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    )
    arguments = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script_path),
        "-Archive",
        str(archive),
        "-InstallDir",
        str(install_dir),
        "-Executable",
        str(executable),
        "-ProcessId",
        str(process_id),
    ]
    if archive.name.startswith("GiongViet-update-delta-"):
        arguments.append("-Delta")
    subprocess.Popen(
        arguments,
        close_fds=True,
        creationflags=flags,
    )

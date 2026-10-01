$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
	if (-not (Test-Path ".venv-build/Scripts/python.exe")) {
		python -m venv .venv-build
		if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
	}

	$python = ".venv-build/Scripts/python.exe"
	& $python -m pip install ".[build]"
	if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

	& $python scripts/stage_offline_assets.py
	if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

	$distPath = "dist/offline-build"
	$workPath = "build/offline-pyinstaller"
	if (Test-Path $distPath) { Remove-Item $distPath -Recurse -Force }
	$archive = "dist/GiongViet-offline-windows-x64.zip"
	if (Test-Path $archive) { Remove-Item $archive -Force }

	& $python -m PyInstaller packaging/GiongViet.spec --noconfirm --clean --distpath $distPath --workpath $workPath
	if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

	& $python scripts/package_portable.py
	if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
	Write-Host "Created $archive and $distPath/GiongViet/GiongViet.exe"
}
finally {
	Pop-Location
}

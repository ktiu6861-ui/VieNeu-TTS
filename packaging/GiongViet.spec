from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs
from pathlib import Path

project_root = Path(SPECPATH).resolve().parent
offline_cache = project_root / "build" / "offline-assets" / "hf-cache"
if not offline_cache.is_dir():
    raise SystemExit("Offline models are missing. Run build.ps1 to stage the local Hugging Face cache first.")

vieneu_datas, vieneu_binaries, vieneu_hidden = collect_all("vieneu")
g2p_datas, g2p_binaries, g2p_hidden = collect_all("sea_g2p")
onnx_binaries = collect_dynamic_libs("onnxruntime")

analysis = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=vieneu_binaries + g2p_binaries + onnx_binaries,
    datas=vieneu_datas + g2p_datas + [(str(offline_cache), "hf-cache")],
    hiddenimports=vieneu_hidden + g2p_hidden + [
        "onnxruntime",
        "onnxruntime.capi",
        "onnxruntime.capi.onnxruntime_pybind11_state",
        "lameenc",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "torchaudio", "transformers"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

app = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="GiongViet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

bundle = COLLECT(
    app,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GiongViet",
)

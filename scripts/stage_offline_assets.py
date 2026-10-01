from __future__ import annotations

import shutil
import sys
from pathlib import Path

REQUIRED_REPOS = (
    "k2-fsa/OmniVoice",
    "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX",
    "pnnbao-ump/VieNeu-TTS-v3-Turbo",
)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except ImportError as exc:
        print(f"Cannot locate Hugging Face cache: {exc}", file=sys.stderr)
        return 1

    source_hub = Path(HF_HUB_CACHE)
    target_hub = project_root / "build" / "offline-assets" / "hf-cache" / "hub"
    target_hub.mkdir(parents=True, exist_ok=True)

    for repo_id in REQUIRED_REPOS:
        repo_name = "models--" + repo_id.replace("/", "--")
        source = source_hub / repo_name
        snapshots = source / "snapshots"
        if not snapshots.is_dir() or not any(snapshots.iterdir()):
            print(f"Required model is missing from the local cache: {repo_id}", file=sys.stderr)
            print("Run the app once with internet access to download it, then rebuild.", file=sys.stderr)
            return 1

        files = [path for path in snapshots.rglob("*") if path.is_file()]
        if not files or any(not path.resolve().is_file() for path in files):
            print(f"Model cache is incomplete: {repo_id}", file=sys.stderr)
            return 1

        target = target_hub / repo_name
        print(f"Bundling {repo_id} ({len(files)} cached files)...")
        shutil.copytree(source, target, symlinks=False, dirs_exist_ok=True)

    bundled_files = [path for path in target_hub.rglob("*") if path.is_file()]
    if not bundled_files:
        print("No model files were staged.", file=sys.stderr)
        return 1

    total_bytes = sum(path.stat().st_size for path in bundled_files)
    print(f"Offline model cache ready: {len(bundled_files)} files, {total_bytes / (1024 ** 3):.2f} GiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

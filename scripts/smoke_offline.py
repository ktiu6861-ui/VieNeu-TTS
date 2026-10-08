from __future__ import annotations

import argparse
import logging
import traceback
from pathlib import Path

import soundfile as sf


def main() -> int:
    import sys

    # Windows consoles may use cp1252 and fail when a preset voice name contains
    # Vietnamese characters. Keep smoke-test logging portable across terminals.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("offline-smoke.wav"))
    args = parser.parse_args()

    import os

    hub_cache = args.cache.resolve() / "hub"
    os.environ["HF_HOME"] = str(args.cache.resolve())
    os.environ["HF_HUB_CACHE"] = str(hub_cache)
    os.environ["HF_HUB_OFFLINE"] = "1"
    logging.basicConfig(level=logging.INFO)

    try:
        from PySide6 import QtCore

        dll_directory = os.add_dll_directory(str(Path(QtCore.__file__).resolve().parent))
        from vieneu import Vieneu

        print("Loading VieNeu from offline cache...", flush=True)
        threads = min(max(os.cpu_count() or 1, 1), 12)
        try:
            tts = Vieneu(mode="v3turbo", backend="onnx", precision="int8", threads=threads)
            precision = "INT8"
        except Exception:
            tts = Vieneu(mode="v3turbo", backend="onnx", precision="fp32", threads=threads)
            precision = "FP32"
        voices = tts.list_preset_voices()
        if not voices:
            raise RuntimeError("No preset voices were loaded.")

        print(f"Generating with {voices[0][1]}...", flush=True)
        audio = tts.infer("Xin chao, day la ban kiem tra offline.", voice=voices[0][1])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tts.save(audio, str(args.output))

        info = sf.info(str(args.output))
        if info.frames <= 0 or info.samplerate != 48_000:
            raise RuntimeError(f"Unexpected WAV output: {info}")
        print(f"OFFLINE_SYNTHESIS_OK: {args.output} ({info.frames} frames, {info.samplerate} Hz; {precision})")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

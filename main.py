from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Qt, QThread, Signal, Slot, QUrl, QTimer
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from updater import APP_VERSION, check_latest_version, download_latest_update, launch_update_installer

APP_NAME = "GiongViet"
DEFAULT_RENDER_SECONDS_PER_CHAR = 0.03
MAX_VOICE_REFERENCE_SECONDS = 8.0
SAMPLE_TEXT = "Xin chào, đây là bản thử giọng đọc tiếng Việt của tôi."
SAMPLE_ENGLISH_TEXT = "Hello, and welcome. Take a moment to slow down, breathe, and enjoy this story. Every word deserves to be heard clearly, warmly, and with feeling."
_DLL_DIRECTORY_HANDLES = []


class UpdateWorker(QThread):
    completed = Signal(str, str, str)
    progress = Signal(int)

    def __init__(self, parent=None, *, check_only: bool = False) -> None:
        super().__init__(parent)
        self.check_only = check_only

    def _report_progress(self, percent: int) -> None:
        if self.isInterruptionRequested():
            raise InterruptedError("Đã hủy tải bản cập nhật.")
        self.progress.emit(percent)

    def run(self) -> None:
        try:
            if self.check_only:
                status, detail = check_latest_version()
                self.completed.emit(status, detail, "")
                return
            result, detail = download_latest_update(self._report_progress)
            if result == "current":
                self.completed.emit("current", detail, "")
            else:
                self.completed.emit("ready", detail, result)
        except Exception as exc:
            self.completed.emit("error", str(exc), "")


def load_text_from_file(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Không tìm thấy file văn bản: {source}")
    if source.suffix.lower() != ".txt":
        raise ValueError("Chỉ hỗ trợ file .txt.")
    for encoding in ("utf-8-sig", "utf-16", "cp1258", "latin-1"):
        try:
            return source.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return source.read_text(encoding="utf-8", errors="replace")


def prepare_tts_text(text: str) -> str:
    """Clean punctuation spacing while preserving words and punctuation marks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+([,.;!?])", r"\1", text)
    text = re.sub(r"[,;:]{2,}", lambda match: match.group(0)[0], text)
    text = re.sub(r",(?=[^\s\d])", ", ", text)
    text = re.sub(r"([!?])\1+", r"\1", text)
    text = re.sub(r"\.{4,}", "...", text)
    return text.strip()


def _onnx_threads() -> int:
    # Use the available logical processors for CPU-bound ONNX inference.
    return min(max(os.cpu_count() or 1, 1), 12)


def _optimize_runtime() -> None:
    """Tối ưu cho infer ONNX trên CPU: tránh over-commit thread và giảm overhead nền."""
    try:
        cpu_threads = _onnx_threads()
        for env_name in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            os.environ.setdefault(env_name, str(cpu_threads))
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    except Exception:
        pass


def _save_mp3(audio, output: Path) -> None:
    import lameenc
    import numpy as np

    pcm = np.asarray(audio, dtype=np.float32)
    pcm = np.nan_to_num(pcm, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = np.clip(pcm, -1.0, 1.0)
    pcm16 = np.rint(pcm * 32767).astype("<i2", copy=False)
    encoder = lameenc.Encoder()
    encoder.set_in_sample_rate(48_000)
    encoder.set_channels(1)
    encoder.set_bit_rate(192)
    encoder.set_quality(2)
    encoded = encoder.encode(pcm16.tobytes()) + encoder.flush()
    output.write_bytes(encoded)


def app_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        path = Path(sys.executable).resolve().parent / "user-data"
        legacy_location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        legacy_path = Path(legacy_location) if legacy_location else None
        legacy_index = legacy_path / "voices.json" if legacy_path else None
        voice_index = path / "voices.json"
        if legacy_index and legacy_index.is_file() and not voice_index.exists():
            path.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_index, voice_index)
            legacy_voices = legacy_path / "voices"
            if legacy_voices.is_dir():
                shutil.copytree(legacy_voices, path / "voices", dirs_exist_ok=True)
    else:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        path = Path(location) if location else Path.home() / ".giong-viet"
    path.mkdir(parents=True, exist_ok=True)
    return path


class EngineWorker(QObject):
    ready = Signal(list)
    failed = Signal(str)
    initializing = Signal(str)
    generated = Signal(str)
    voice_added = Signal(str, list)
    voice_removed = Signal(str, list)

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.tts = None
        self.precision = None
        self.voice_dir = data_dir / "voices"
        self.output_dir = data_dir / "outputs"
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.voice_index = data_dir / "voices.json"

    def _voice_rows(self) -> list[tuple[str, str]]:
        rows = list(self.tts.list_preset_voices())
        known_ids = {str(voice_id) for _, voice_id in rows}

        for item in self._read_index():
            name = item["name"]
            if name not in known_ids:
                rows.append((name, name))
                known_ids.add(name)

        return rows

    @Slot()
    def initialize(self) -> None:
        try:
            self.initializing.emit("Đang khởi động engine ONNX…")
            _optimize_runtime()
            runtime_dirs = [Path(sys.modules["PySide6.QtCore"].__file__).resolve().parent]
            if getattr(sys, "frozen", False):
                bundle_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
                runtime_dirs.append(bundle_dir)
                hf_home = bundle_dir / "hf-cache"
                os.environ["HF_HOME"] = str(hf_home)
                os.environ["HF_HUB_CACHE"] = str(hf_home / "hub")
                os.environ["HF_HUB_OFFLINE"] = "1"

            if os.name == "nt":
                for runtime_dir in runtime_dirs:
                    if runtime_dir.is_dir():
                        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(runtime_dir)))

            from vieneu import Vieneu

            engine_options = {
                "mode": "v3turbo",
                "backend": "onnx",
                "threads": _onnx_threads(),
            }
            try:
                self.initializing.emit("Đang nạp model giọng đọc…")
                self.tts = Vieneu(**engine_options, precision="int8")
                self.precision = "INT8"
            except Exception as int8_error:
                logging.warning("VieNeu INT8 graph unavailable; retrying with FP32: %s", int8_error)
                self.initializing.emit("Đang thử nạp model giọng đọc ở chế độ tương thích…")
                self.tts = Vieneu(**engine_options, precision="fp32")
                self.precision = "FP32"
            custom_voices = self._read_index()
            for index, item in enumerate(custom_voices, start=1):
                audio = self.voice_dir / item["file"]
                if audio.is_file():
                    self.initializing.emit(
                        f"Đang nạp giọng clone {index}/{len(custom_voices)}: {item['name']}…"
                    )
                    self.tts.add_voice(
                        item["name"],
                        str(audio),
                        denoise=item.get("denoise", True),
                    )
            self.ready.emit(self._voice_rows())
        except Exception as exc:
            self.failed.emit(f"Không tải được engine VieNeu.\n\n{exc}")

    def _read_index(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self.voice_index.read_text(encoding="utf-8"))
            return [item for item in data if isinstance(item, dict) and "name" in item and "file" in item]
        except (OSError, json.JSONDecodeError):
            return []

    def _write_index(self, data: list[dict[str, str]]) -> None:
        temporary = self.voice_index.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.voice_index)

    @Slot(str, str, str, str)
    def generate(self, text: str, voice_id: str, output_dir: str, file_name: str) -> None:
        output_files: list[Path] = []
        try:
            normalized_text = prepare_tts_text(text)
            if not normalized_text:
                raise ValueError("Hãy nhập nội dung cần đọc.")

            # Smaller chunks expose sentence and comma boundaries more often,
            # while preserving enough text for the model to keep its prosody.
            audio = self.tts.infer(normalized_text, voice=voice_id, max_chars=120)
            target_dir = Path(output_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            stem = Path(file_name).name.strip() if file_name.strip() else ""
            if stem.lower().endswith(".mp3"):
                stem = stem[:-4]
            if not stem:
                stem = f"giong-viet-{datetime.now():%Y%m%d-%H%M%S-%f}"
            if any(char in stem for char in '<>:"/\\|?*') or stem in {".", ".."}:
                raise ValueError("Tên file không hợp lệ. Hãy bỏ các ký tự \\ / : * ? \" < > |.")
            mp3_output = target_dir / f"{stem}.mp3"
            if mp3_output.exists():
                stem = f"{stem}-{datetime.now():%Y%m%d-%H%M%S-%f}"
                mp3_output = target_dir / f"{stem}.mp3"
            output_files.append(mp3_output)
            _save_mp3(audio, mp3_output)
            self.generated.emit(str(mp3_output))
        except Exception as exc:
            for path in output_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logging.warning("Could not remove incomplete audio file %s", path, exc_info=True)
            detail = traceback.format_exc() if "--smoke" in sys.argv else str(exc)
            self.failed.emit(f"Tạo giọng thất bại.\n\n{detail}")

    @Slot(str, str, bool)
    def add_voice(self, name: str, source: str, denoise: bool) -> None:
        saved_audio = self.voice_dir / f"{uuid.uuid4().hex}.wav"
        registered = False
        try:
            name = name.strip()
            if not name:
                raise ValueError("Hãy nhập tên cho giọng clone.")

            existing_names = {
                str(value).casefold()
                for label, voice_id in self.tts.list_preset_voices()
                for value in (label, voice_id)
            }
            existing_names.update(item["name"].casefold() for item in self._read_index())
            if name.casefold() in existing_names:
                raise ValueError("Tên này đã có trong thư viện. Hãy chọn tên khác.")

            source_path = Path(source)
            if not source_path.is_file():
                raise FileNotFoundError(f"Không tìm thấy file âm thanh: {source_path}")

            import soundfile as sf

            audio_info = sf.info(str(source_path))
            duration = audio_info.frames / audio_info.samplerate if audio_info.samplerate else 0
            if duration < 3:
                raise ValueError("Clip quá ngắn hoặc không có âm thanh. WAV 3–8 giây được khuyến nghị.")
            if duration > MAX_VOICE_REFERENCE_SECONDS:
                raise ValueError(
                    "Clip mẫu dài hơn 8 giây. VieNeu v3 Turbo chỉ dùng tối đa 8 giây đầu, "
                    "nên hãy cắt một đoạn tiếng nói rõ dài 3–8 giây rồi clone lại."
                )

            shutil.copy2(source_path, saved_audio)
            self.tts.add_voice(name, str(saved_audio), denoise=denoise)
            registered = True
            index = self._read_index()
            index.append({"name": name, "file": saved_audio.name, "denoise": denoise})
            self._write_index(index)
            self.voice_added.emit(name, self._voice_rows())
        except Exception as exc:
            if registered:
                try:
                    self.tts.remove_voice(name)
                except Exception:
                    pass
            saved_audio.unlink(missing_ok=True)
            self.failed.emit(f"Không thể clone giọng từ file:\n{source}\n\n{exc}")

    @Slot(str)
    def remove_voice(self, name: str) -> None:
        try:
            item_to_remove = next((item for item in self._read_index() if item["name"] == name), None)
            if item_to_remove is None:
                raise ValueError("Chỉ có thể xóa giọng do bạn đã clone.")
            self.tts.remove_voice(name)
            self._write_index([item for item in self._read_index() if item["name"] != name])
            (self.voice_dir / item_to_remove["file"]).unlink(missing_ok=True)
            self.voice_removed.emit(name, self._voice_rows())
        except Exception as exc:
            self.failed.emit(f"Không thể xóa giọng.\n\n{exc}")


class MainWindow(QMainWindow):
    generate_requested = Signal(str, str, str, str)
    add_voice_requested = Signal(str, str, bool)
    remove_voice_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.data_dir = app_data_dir()
        self.default_output_dir = self.data_dir / "outputs"
        self.output_dir = self._load_output_dir()
        self.queue_output_dir = self._load_queue_output_dir()
        self.render_seconds_per_char = self._load_render_speed_estimate()
        self.playback_rate = self._load_playback_rate()
        self._render_started_at: float | None = None
        self._render_char_count = 0
        self._render_expected_seconds = 0.0
        self._queue_running = False
        self._queue_items: list[tuple[str, str]] = []
        self.update_worker: UpdateWorker | None = None
        self._update_install_approved = False
        self.voice_names: set[str] = set()
        self.engine_ready = False
        self.setWindowTitle("Giọng Việt")
        self.setMinimumSize(900, 680)
        self.resize(1120, 760)
        self._build_ui()
        self._connect_engine()
        self._set_busy(True, "Đang khởi động engine ONNX…")
        self.thread.start()
        QTimer.singleShot(2500, lambda: self._check_for_updates(automatic=True))

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(22, 24, 16, 20)
        side.setSpacing(10)
        brand = QLabel("GIỌNG VIỆT")
        brand.setObjectName("brand")
        side.addWidget(brand)
        side.addWidget(QLabel("STUDIO CÁ NHÂN  ·  LOCAL"))
        side.addSpacing(26)
        self.nav_studio = QPushButton("◉   Tạo giọng")
        self.nav_voices = QPushButton("♬   Thư viện giọng")
        self.nav_studio.setObjectName("navActive")
        self.nav_voices.setObjectName("nav")
        side.addWidget(self.nav_studio)
        side.addWidget(self.nav_voices)
        self._build_queue_panel(side)
        self.update_button = QPushButton("Cập nhật phiên bản mới")
        self.update_button.setObjectName("updateButton")
        self.update_button.setToolTip(f"Phiên bản hiện tại: {APP_VERSION}")
        self.update_button.clicked.connect(self._check_for_updates)
        side.addWidget(self.update_button)
        side.addStretch(1)
        privacy = QLabel("CHẠY TRÊN MÁY NÀY\nKhông gửi nội dung đọc · Có kiểm tra cập nhật online")
        privacy.setObjectName("privacy")
        side.addWidget(privacy)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(34, 25, 34, 28)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._studio_page())
        self.pages.addWidget(self._voices_page())
        content_layout.addWidget(self.pages, 1)
        ownership = QLabel("♡  Ứng dụng này thuộc quyền sở hữu của Phương Phương  ✿")
        ownership.setObjectName("ownership")
        ownership.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        content_layout.addWidget(ownership)
        outer.addWidget(sidebar)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)

        self.nav_studio.clicked.connect(lambda: self._navigate(0))
        self.nav_voices.clicked.connect(lambda: self._navigate(1))
        self.setStyleSheet(STYLESHEET)

    def _studio_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("Tạo bản đọc")
        title.setObjectName("pageTitle")
        title_block.addWidget(title)
        subtitle = QLabel("Chọn chất giọng, nhập nội dung và tạo file âm thanh riêng tư.")
        subtitle.setObjectName("muted")
        title_block.addWidget(subtitle)
        heading.addLayout(title_block)
        heading.addStretch(1)
        self.slower_button = QPushButton("−")
        self.slower_button.setObjectName("speedControl")
        self.slower_button.setToolTip("Giảm tốc độ nghe")
        self.slower_button.clicked.connect(lambda: self._adjust_playback_rate(-0.1))
        heading.addWidget(self.slower_button, alignment=Qt.AlignmentFlag.AlignTop)
        self.playback_rate_label = QLabel()
        self.playback_rate_label.setMinimumWidth(48)
        self.playback_rate_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.playback_rate_label, alignment=Qt.AlignmentFlag.AlignTop)
        self.faster_button = QPushButton("＋")
        self.faster_button.setObjectName("speedControl")
        self.faster_button.setToolTip("Tăng tốc độ nghe")
        self.faster_button.clicked.connect(lambda: self._adjust_playback_rate(0.1))
        heading.addWidget(self.faster_button, alignment=Qt.AlignmentFlag.AlignTop)
        self._update_playback_rate_label()
        self.engine_badge = QLabel("●  ĐANG TẢI ENGINE")
        self.engine_badge.setObjectName("badge")
        heading.addWidget(self.engine_badge, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)

        text_heading = QHBoxLayout()
        text_label = QLabel("NỘI DUNG")
        text_label.setObjectName("sectionLabel")
        text_heading.addWidget(text_label)
        text_heading.addStretch(1)
        self.character_count_label = QLabel("0 ký tự")
        self.character_count_label.setObjectName("muted")
        text_heading.addWidget(self.character_count_label)
        layout.addLayout(text_heading)

        text_actions = QHBoxLayout()
        text_actions.addStretch(1)
        self.english_sample_button = QPushButton("Thử văn bản tiếng Anh")
        self.english_sample_button.clicked.connect(
            lambda: self.text_input.setPlainText(SAMPLE_ENGLISH_TEXT)
        )
        text_actions.addWidget(self.english_sample_button)
        self.load_txt_button = QPushButton("Tải từ file .txt")
        self.load_txt_button.clicked.connect(self._load_text_file)
        text_actions.addWidget(self.load_txt_button)
        layout.addLayout(text_actions)

        self.text_input = QPlainTextEdit()
        self.text_input.setPlaceholderText("Nhập nội dung cần đọc…")
        self.text_input.setPlainText(SAMPLE_TEXT)
        self.text_input.setMinimumHeight(125)
        self.text_input.textChanged.connect(self._update_character_count)
        layout.addWidget(self.text_input)
        self._update_character_count()

        voice_row = QHBoxLayout()
        voice_col = QVBoxLayout()
        voice_col.addWidget(QLabel("GIỌNG ĐỌC"))
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumHeight(44)
        self.voice_combo.addItem("Đang tải danh sách giọng…", "")
        voice_col.addWidget(self.voice_combo)
        voice_row.addLayout(voice_col, 1)
        voice_row.addSpacing(14)
        clone_col = QVBoxLayout()
        clone_col.addWidget(QLabel("GIỌNG CỦA BẠN"))
        self.clone_button = QPushButton("＋  Clone từ file WAV")
        self.clone_button.setMinimumHeight(44)
        self.clone_button.clicked.connect(self._choose_clone)
        clone_col.addWidget(self.clone_button)
        voice_row.addLayout(clone_col)
        layout.addLayout(voice_row)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("THƯ MỤC LƯU"))
        self.output_path_edit = QLineEdit(str(self.output_dir))
        self.output_path_edit.setPlaceholderText("Nhập đường dẫn thư mục lưu audio")
        self.output_path_edit.editingFinished.connect(self._apply_output_dir_edit)
        output_row.addWidget(self.output_path_edit, 1)
        self.choose_output_button = QPushButton("Chọn thư mục lưu")
        self.choose_output_button.clicked.connect(self._choose_output_dir)
        output_row.addWidget(self.choose_output_button)
        layout.addLayout(output_row)

        filename_row = QHBoxLayout()
        filename_row.addWidget(QLabel("TÊN FILE MP3"))
        self.output_name_edit = QLineEdit()
        self.output_name_edit.setPlaceholderText("Nhập tên file trước khi tạo audio (không cần .mp3)")
        self.output_name_edit.setToolTip("Bỏ trống để dùng tên file tự động.")
        filename_row.addWidget(self.output_name_edit, 1)
        layout.addLayout(filename_row)

        actions = QHBoxLayout()
        self.status = QLabel("Đang tải model lần đầu có thể mất vài phút.")
        self.status.setObjectName("muted")
        actions.addWidget(self.status, 1)
        self.generate_button = QPushButton("Tạo âm thanh   →")
        self.generate_button.setObjectName("primary")
        self.generate_button.setMinimumHeight(48)
        self.generate_button.setMinimumWidth(190)
        self.generate_button.clicked.connect(self._generate)
        actions.addWidget(self.generate_button)
        layout.addLayout(actions)

        self.render_progress_label = QLabel("Chưa tạo audio")
        self.render_progress_label.setObjectName("muted")
        layout.addWidget(self.render_progress_label)
        self.render_progress_bar = QProgressBar()
        self.render_progress_bar.setRange(0, 100)
        self.render_progress_bar.setValue(0)
        self.render_progress_bar.setFormat("%p% · ước tính")
        self.render_progress_bar.setMinimumHeight(18)
        layout.addWidget(self.render_progress_bar)
        self.render_timer = QTimer(self)
        self.render_timer.setInterval(500)
        self.render_timer.timeout.connect(self._update_render_progress)

        self.result_frame = QFrame()
        self.result_frame.setObjectName("result")
        result_layout = QHBoxLayout(self.result_frame)
        self.result_label = QLabel("Bản đọc sẽ xuất hiện ở đây.")
        self.result_label.setObjectName("muted")
        result_layout.addWidget(self.result_label, 1)
        self.play_button = QPushButton("▶  Phát")
        self.play_button.clicked.connect(self._play_result)
        self.play_button.setEnabled(False)
        result_layout.addWidget(self.play_button)
        self.stop_button = QPushButton("■  Dừng")
        self.stop_button.clicked.connect(self._stop_result)
        self.stop_button.setEnabled(False)
        result_layout.addWidget(self.stop_button)
        self.open_button = QPushButton("Mở thư mục")
        self.open_button.clicked.connect(self._open_output_dir)
        result_layout.addWidget(self.open_button)
        layout.addWidget(self.result_frame)
        layout.addStretch(1)
        return page

    def _build_queue_panel(self, side: QVBoxLayout) -> None:
        side.addSpacing(24)
        heading = QHBoxLayout()
        title = QLabel("HÀNG CHỜ TIẾP THEO")
        title.setObjectName("queueTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        self.queue_count_label = QLabel("0 mục")
        self.queue_count_label.setObjectName("queueCount")
        heading.addWidget(self.queue_count_label)
        side.addLayout(heading)

        self.queue_list = QListWidget()
        self.queue_list.setObjectName("queueList")
        self.queue_list.setMaximumHeight(150)
        side.addWidget(self.queue_list)

        queue_folder_label = QLabel("THƯ MỤC LƯU HÀNG CHỜ")
        queue_folder_label.setObjectName("queueTitle")
        side.addWidget(queue_folder_label)
        queue_folder_row = QHBoxLayout()
        queue_folder_row.setSpacing(4)
        self.queue_output_path_edit = QLineEdit(str(self.queue_output_dir))
        self.queue_output_path_edit.setFixedHeight(30)
        self.queue_output_path_edit.setPlaceholderText("Đường dẫn lưu audio hàng chờ")
        self.queue_output_path_edit.setToolTip(str(self.queue_output_dir))
        self.queue_output_path_edit.editingFinished.connect(self._apply_queue_output_dir_edit)
        queue_folder_row.addWidget(self.queue_output_path_edit, 1)
        self.choose_queue_output_button = QPushButton("Chọn")
        self.choose_queue_output_button.setFixedHeight(30)
        self.choose_queue_output_button.setToolTip("Chọn thư mục lưu riêng cho hàng chờ")
        self.choose_queue_output_button.clicked.connect(self._choose_queue_output_dir)
        queue_folder_row.addWidget(self.choose_queue_output_button)
        side.addLayout(queue_folder_row)

        self.add_queue_button = QPushButton("Thêm nội dung hiện tại")
        self.add_queue_button.setObjectName("queueAction")
        self.add_queue_button.clicked.connect(self._add_to_queue)
        side.addWidget(self.add_queue_button)
        self.remove_queue_button = QPushButton("Bỏ mục chọn")
        self.remove_queue_button.setObjectName("queueAction")
        self.remove_queue_button.clicked.connect(self._remove_from_queue)
        self.remove_queue_button.setEnabled(False)
        self.queue_list.currentRowChanged.connect(
            lambda row: self.remove_queue_button.setEnabled(row >= 0 and not self._queue_running)
        )
        side.addWidget(self.remove_queue_button)
        self.run_queue_button = QPushButton("Đọc hàng chờ")
        self.run_queue_button.setObjectName("queuePrimary")
        self.run_queue_button.clicked.connect(self._start_queue)
        self.run_queue_button.setEnabled(False)
        side.addWidget(self.run_queue_button)

    def _voices_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Thư viện giọng")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        intro = QLabel("Các giọng dựng sẵn của VieNeu và những giọng bạn đã clone.")
        intro.setObjectName("muted")
        layout.addWidget(intro)
        self.voice_list = QPlainTextEdit()
        self.voice_list.setReadOnly(True)
        self.voice_list.setObjectName("voiceList")
        layout.addWidget(self.voice_list, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Chọn giọng cá nhân để xóa:"))
        self.delete_combo = QComboBox()
        self.delete_combo.setMinimumHeight(42)
        row.addWidget(self.delete_combo, 1)
        self.delete_button = QPushButton("Xóa giọng")
        self.delete_button.clicked.connect(self._remove_voice)
        row.addWidget(self.delete_button)
        layout.addLayout(row)
        return page

    def _connect_engine(self) -> None:
        self.thread = QThread(self)
        self.worker = EngineWorker(self.data_dir)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.initialize)
        self.worker.initializing.connect(self._engine_initializing)
        self.worker.ready.connect(self._engine_ready)
        self.worker.failed.connect(self._engine_failed)
        self.worker.generated.connect(self._generated)
        self.worker.voice_added.connect(self._voice_added)
        self.worker.voice_removed.connect(self._voice_removed)
        self.generate_requested.connect(self.worker.generate)
        self.add_voice_requested.connect(self.worker.add_voice)
        self.remove_voice_requested.connect(self.worker.remove_voice)

    def _check_for_updates(self, automatic: bool = False) -> None:
        if automatic and not getattr(sys, "frozen", False):
            return
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self,
                "Cập nhật phiên bản",
                "Tự cập nhật chỉ khả dụng trong bản portable đã đóng gói.\n\n"
                f"Phiên bản mã nguồn hiện tại: {APP_VERSION}",
            )
            return
        if self.update_worker is not None and self.update_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("Đang kiểm tra cập nhật…")
        self.update_worker = UpdateWorker(self, check_only=automatic)
        self.update_worker.completed.connect(self._update_check_finished)
        self.update_worker.progress.connect(self._update_download_progress)
        self.update_worker.start()

    def _update_download_progress(self, percent: int) -> None:
        self.update_button.setText(f"Đang tải cập nhật… {percent}%")

    def _update_check_finished(self, status: str, detail: str, archive_path: str) -> None:
        worker = self.update_worker
        self.update_worker = None
        if worker is not None:
            if worker.isRunning():
                worker.wait()
            worker.deleteLater()
        self.update_button.setEnabled(True)
        self.update_button.setText("Cập nhật phiên bản mới")

        if status == "current":
            self._update_install_approved = False
            if worker is None or not worker.check_only:
                QMessageBox.information(self, "Cập nhật phiên bản", "Bạn đang dùng phiên bản mới nhất.")
            return
        if status == "available":
            answer = QMessageBox.question(
                self,
                "Đã có phiên bản mới",
                f"Đã có phiên bản {detail}. Bạn có muốn tải và cài đặt ngay không? "
                "Giọng clone, model và dữ liệu của bạn sẽ được giữ nguyên.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._update_install_approved = True
                self._check_for_updates()
            return
        if status == "error":
            self._update_install_approved = False
            if worker is None or not worker.check_only:
                QMessageBox.warning(self, "Không thể kiểm tra cập nhật", detail)
            return

        if not self._update_install_approved:
            answer = QMessageBox.question(
                self,
                "Đã có phiên bản mới",
                f"Phiên bản {detail} đã tải về và xác minh thành công.\n\n"
                "Cập nhật ngay? Ứng dụng sẽ đóng rồi tự mở lại. Giọng clone, model và dữ liệu của bạn được giữ nguyên.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                Path(archive_path).unlink(missing_ok=True)
                return
        self._update_install_approved = False
        try:
            launch_update_installer(archive_path, os.getpid())
        except Exception as exc:
            QMessageBox.critical(self, "Không thể cài bản cập nhật", str(exc))
            return
        self.close()

    def _navigate(self, page: int) -> None:
        self.pages.setCurrentIndex(page)
        self.nav_studio.setObjectName("navActive" if page == 0 else "nav")
        self.nav_voices.setObjectName("navActive" if page == 1 else "nav")
        self.nav_studio.style().unpolish(self.nav_studio)
        self.nav_studio.style().polish(self.nav_studio)
        self.nav_voices.style().unpolish(self.nav_voices)
        self.nav_voices.style().polish(self.nav_voices)

    def _engine_ready(self, voices: list) -> None:
        self._clear_update_ready_marker()
        self.engine_ready = True
        self.voice_combo.clear()
        self.voice_names = {voice_id for _, voice_id in voices}
        for label, voice_id in voices:
            self.voice_combo.addItem(label, voice_id)
        self._refresh_voice_page(voices)
        self.engine_badge.setText(f"●  LOCAL · ONNX / CPU · {self.worker.precision}")
        self.engine_badge.setObjectName("badgeReady")
        self.engine_badge.style().unpolish(self.engine_badge)
        self.engine_badge.style().polish(self.engine_badge)
        self._set_busy(False, f"Sẵn sàng · {len(voices)} giọng")

    def _refresh_voice_page(self, voices: list) -> None:
        lines = [f"  {voice_id}    {label}" for label, voice_id in voices]
        self.voice_list.setPlainText("\n".join(lines))
        self.delete_combo.clear()
        custom_names = {item["name"] for item in self.worker._read_index()}
        for _, voice_id in voices:
            if voice_id in custom_names:
                self.delete_combo.addItem(voice_id, voice_id)
        self.delete_button.setEnabled(self.delete_combo.count() > 0)

    def _voice_added(self, name: str, voices: list) -> None:
        self._engine_ready(voices)
        self.voice_combo.setCurrentIndex(self.voice_combo.findData(name))
        self._set_busy(False, f"Đã lưu giọng “{name}” trên thiết bị này.")

    def _voice_removed(self, name: str, voices: list) -> None:
        self._engine_ready(voices)
        self._set_busy(False, f"Đã xóa giọng “{name}”.")

    def _engine_failed(self, message: str) -> None:
        self._clear_update_ready_marker()
        self._finish_render_progress(success=False)
        self._queue_running = False
        if self.engine_ready:
            self._set_busy(False, "Tác vụ thất bại; bạn có thể thử lại.")
            QMessageBox.critical(self, "Tác vụ thất bại", message)
            return

        self._set_busy(False, "Engine chưa sẵn sàng.")
        self.engine_badge.setText("●  CẦN KIỂM TRA")
        QMessageBox.critical(self, "VieNeu chưa khởi động", message)

    def _engine_initializing(self, message: str) -> None:
        self.status.setText(message)

    @staticmethod
    def _clear_update_ready_marker() -> None:
        marker = os.environ.pop("GIONGVIET_UPDATE_READY_MARKER", "")
        if marker:
            try:
                Path(marker).unlink(missing_ok=True)
            except OSError:
                logging.warning("Could not clear updater startup marker: %s", marker, exc_info=True)

    def _set_busy(self, busy: bool, message: str) -> None:
        ready = self.engine_ready and self.voice_combo.count() > 0
        self.generate_button.setEnabled(not busy and ready)
        self.clone_button.setEnabled(not busy and ready)
        self.add_queue_button.setEnabled(not busy and ready)
        self.run_queue_button.setEnabled(
            not busy and ready and bool(self._queue_items) and not self._queue_running
        )
        self.remove_queue_button.setEnabled(
            not busy and not self._queue_running and self.queue_list.currentRow() >= 0
        )
        self.status.setText(message)
        if busy:
            self.generate_button.setText("Đang xử lý…")
        else:
            self.generate_button.setText("Tạo âm thanh   →")

    def _load_text_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file văn bản", "", "Text file (*.txt)")
        if not path:
            return
        try:
            text = load_text_from_file(path)
        except Exception as exc:
            QMessageBox.warning(self, "Không thể tải file .txt", str(exc))
            return
        self.text_input.setPlainText(text)
        self._set_busy(False, f"Đã tải nội dung từ {Path(path).name}")

    def _generate(self) -> None:
        text = self.text_input.toPlainText().strip()
        voice_id = self.voice_combo.currentData()
        if not text:
            QMessageBox.information(self, "Chưa có nội dung", "Hãy nhập nội dung cần đọc trước.")
            return
        if not voice_id:
            return
        self._start_generation(text, str(voice_id))

    def _start_generation(self, text: str, voice_id: str) -> None:
        self._render_char_count = len(text)
        self._render_started_at = time.monotonic()
        self._render_expected_seconds = max(
            1.0, self.render_seconds_per_char * self._render_char_count
        )
        self.render_progress_bar.setValue(0)
        self.render_progress_label.setText("Đang chờ tiến trình render · ETA ước tính…")
        self.render_timer.start()
        self._set_busy(True, "Đang tạo giọng trên máy này…")
        file_name = "" if self._queue_running else self.output_name_edit.text().strip()
        target_dir = self.queue_output_dir if self._queue_running else self.output_dir
        self.generate_requested.emit(text, voice_id, str(target_dir), file_name)
        if file_name:
            self.output_name_edit.clear()

    def _add_to_queue(self) -> None:
        text = self.text_input.toPlainText().strip()
        voice_id = self.voice_combo.currentData()
        if not text:
            QMessageBox.information(self, "Chưa có nội dung", "Hãy nhập nội dung cần đọc trước.")
            return
        if not voice_id:
            return
        self._queue_items.append((text, str(voice_id)))
        preview = " ".join(text.split())
        if len(preview) > 96:
            preview = preview[:93] + "..."
        self.queue_list.addItem(f"{len(self._queue_items)}. [{voice_id}] {preview}")
        self.queue_count_label.setText(f"{len(self._queue_items)} mục")
        self.run_queue_button.setEnabled(not self._queue_running and self.engine_ready)

    def _remove_from_queue(self) -> None:
        row = self.queue_list.currentRow()
        if row < 0 or self._queue_running:
            return
        self.queue_list.takeItem(row)
        self._queue_items.pop(row)
        for index in range(self.queue_list.count()):
            item = self.queue_list.item(index)
            item.setText(f"{index + 1}. " + item.text().split(". ", 1)[1])
        self.queue_count_label.setText(f"{len(self._queue_items)} mục")
        self.run_queue_button.setEnabled(bool(self._queue_items) and self.engine_ready)
        self.remove_queue_button.setEnabled(self.queue_list.currentRow() >= 0)

    def _start_queue(self) -> None:
        if not self._queue_items or self._queue_running:
            return
        self._queue_running = True
        self._start_queue_item()

    def _start_queue_item(self) -> None:
        if not self._queue_items:
            self._queue_running = False
            self._set_busy(False, "HÃ ng chá» Ä‘Ã£ Ä‘á»c xong.")
            return
        text, voice_id = self._queue_items[0]
        self.queue_list.setCurrentRow(0)
        self._start_generation(text, voice_id)

    def _update_character_count(self) -> None:
        count = len(self.text_input.toPlainText())
        self.character_count_label.setText(f"{count:,}".replace(",", ".") + " ký tự")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _update_render_progress(self) -> None:
        if self._render_started_at is None:
            return
        elapsed = max(0.0, time.monotonic() - self._render_started_at)
        # infer() reports only when the complete waveform is ready, so this bar
        # is time-based and capped until file writing has also completed.
        progress = min(94, int(elapsed / self._render_expected_seconds * 94))
        remaining = max(1, math.ceil(self._render_expected_seconds - elapsed))
        self.render_progress_bar.setValue(progress)
        if elapsed < self._render_expected_seconds:
            estimate = f"còn khoảng {self._format_duration(remaining)}"
        else:
            estimate = f"đã vượt ETA ước tính {self._format_duration(elapsed - self._render_expected_seconds)}"
        if progress >= 94:
            status = "Đang hoàn tất giọng đọc; model chưa trả xong audio"
        else:
            status = f"Render đang chạy · {progress}%"
        self.render_progress_label.setText(
            f"{status} · đã {self._format_duration(elapsed)} · {estimate}"
        )

    def _finish_render_progress(self, success: bool) -> None:
        if self._render_started_at is None:
            return
        elapsed = max(0.01, time.monotonic() - self._render_started_at)
        self.render_timer.stop()
        self._render_started_at = None
        if success:
            measured = min(30.0, max(0.01, elapsed / max(1, self._render_char_count)))
            self.render_seconds_per_char = (
                measured if self.render_seconds_per_char <= 0
                else self.render_seconds_per_char * 0.7 + measured * 0.3
            )
            try:
                self._write_settings({"render_seconds_per_char": self.render_seconds_per_char})
            except OSError:
                logging.warning("Could not save the latest render-time estimate.", exc_info=True)
            self.render_progress_bar.setValue(100)
            self.render_progress_label.setText(
                f"Audio hoàn thành · 100% · {self._format_duration(elapsed)}"
            )
        else:
            self.render_progress_bar.setValue(0)
            self.render_progress_label.setText("Render chưa hoàn thành")

    def _load_render_speed_estimate(self) -> float:
        value = self._read_settings().get("render_seconds_per_char")
        try:
            value = float(value)
        except (TypeError, ValueError):
            return DEFAULT_RENDER_SECONDS_PER_CHAR
        if not math.isfinite(value) or not 0.01 <= value <= 30:
            return DEFAULT_RENDER_SECONDS_PER_CHAR
        return value

    def _load_playback_rate(self) -> float:
        try:
            value = float(self._read_settings().get("playback_rate", 1.0))
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(value):
            return 1.0
        return min(2.0, max(0.5, round(value, 1)))

    def _update_playback_rate_label(self) -> None:
        self.playback_rate_label.setText(f"{self.playback_rate:.1f}×")
        self.slower_button.setEnabled(self.playback_rate > 0.5)
        self.faster_button.setEnabled(self.playback_rate < 2.0)

    def _adjust_playback_rate(self, delta: float) -> None:
        self.playback_rate = min(2.0, max(0.5, round(self.playback_rate + delta, 1)))
        self._update_playback_rate_label()
        if hasattr(self, "player"):
            self.player.setPlaybackRate(self.playback_rate)
        try:
            self._write_settings({"playback_rate": self.playback_rate})
        except OSError:
            logging.warning("Could not save playback speed setting.", exc_info=True)

    def _read_settings(self) -> dict:
        try:
            settings = json.loads((self.data_dir / "settings.json").read_text(encoding="utf-8"))
            return settings if isinstance(settings, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _write_settings(self, updates: dict) -> None:
        settings = self._read_settings()
        settings.update(updates)
        path = self.data_dir / "settings.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _load_output_dir(self) -> Path:
        saved = self._read_settings().get("output_dir")
        if isinstance(saved, str) and saved.strip():
            try:
                return Path(saved).expanduser()
            except (OSError, ValueError):
                pass
        self.default_output_dir.mkdir(parents=True, exist_ok=True)
        return self.default_output_dir

    def _load_queue_output_dir(self) -> Path:
        saved = self._read_settings().get("queue_output_dir")
        if isinstance(saved, str) and saved.strip():
            try:
                output_dir = Path(saved).expanduser()
                output_dir.mkdir(parents=True, exist_ok=True)
                return output_dir
            except (OSError, ValueError):
                pass
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    def _choose_queue_output_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục lưu audio hàng chờ", str(self.queue_output_dir)
        )
        if not chosen:
            return
        self.queue_output_path_edit.setText(chosen)
        self._apply_queue_output_dir_edit()

    def _apply_queue_output_dir_edit(self) -> None:
        path_text = self.queue_output_path_edit.text().strip()
        if not path_text:
            self.queue_output_path_edit.setText(str(self.queue_output_dir))
            return
        try:
            output_dir = Path(path_text).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            self.queue_output_path_edit.setText(str(self.queue_output_dir))
            QMessageBox.warning(
                self,
                "Không thể dùng thư mục lưu hàng chờ",
                f"Không thể tạo hoặc lưu đường dẫn này. Hãy kiểm tra lại đường dẫn và quyền truy cập.\n\n{exc}",
            )
            return
        self.queue_output_dir = output_dir
        self.queue_output_path_edit.setText(str(output_dir))
        self.queue_output_path_edit.setToolTip(str(output_dir))
        try:
            self._write_settings({"queue_output_dir": str(output_dir)})
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Không thể ghi cài đặt",
                f"Thư mục đã dùng được trong phiên này nhưng không thể ghi nhớ cho lần sau.\n\n{exc}",
            )

    def _choose_output_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục lưu audio", str(self.output_dir)
        )
        if not chosen:
            return
        self.output_path_edit.setText(chosen)
        self._apply_output_dir_edit()

    def _apply_output_dir_edit(self) -> None:
        path_text = self.output_path_edit.text().strip()
        if not path_text:
            self.output_path_edit.setText(str(self.output_dir))
            return
        try:
            output_dir = Path(path_text).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            self.output_path_edit.setText(str(self.output_dir))
            QMessageBox.warning(
                self,
                "Không thể dùng thư mục lưu",
                f"Không thể tạo hoặc lưu đường dẫn này. Hãy kiểm tra lại đường dẫn và quyền truy cập.\n\n{exc}",
            )
            return
        self.output_dir = output_dir
        self.output_path_edit.setText(str(self.output_dir))
        try:
            self._write_settings({"output_dir": str(self.output_dir)})
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Không thể ghi cài đặt",
                f"Thư mục đã nhập dùng được trong phiên này nhưng không thể ghi nhớ cho lần sau.\n\n{exc}",
            )

    def _open_output_dir(self) -> None:
        output_dir = Path(self.current_audio).parent if self.current_audio else self.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_dir)))

    def _choose_clone(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file giọng mẫu", "", "Audio WAV (*.wav)")
        if not path:
            return
        name, accepted = QInputDialog.getText(self, "Lưu giọng clone", "Tên giọng:")
        name = name.strip()
        if not accepted or not name:
            return
        if name in self.voice_names:
            QMessageBox.warning(self, "Tên đã tồn tại", "Hãy chọn một tên giọng khác.")
            return

        answer = QMessageBox.question(
            self,
            "Khử nhiễu clip mẫu?",
            "Clip có tiếng nền hoặc tạp âm không? Chọn Không nếu bản thu đã sạch để giữ màu giọng tự nhiên hơn.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return

        denoise = answer == QMessageBox.StandardButton.Yes
        self._set_busy(True, "Đang phân tích clip mẫu và lưu giọng…")
        self.add_voice_requested.emit(name, path, denoise)

    def _remove_voice(self) -> None:
        name = self.delete_combo.currentData()
        if not name:
            return
        answer = QMessageBox.question(self, "Xóa giọng", f"Xóa giọng clone “{name}” khỏi thư viện?")
        if answer == QMessageBox.StandardButton.Yes:
            self._set_busy(True, "Đang xóa giọng…")
            self.remove_voice_requested.emit(name)

    def _generated(self, path: str) -> None:
        self._finish_render_progress(success=True)
        if hasattr(self, "player"):
            self.player.stop()
        self.current_audio = path
        self.result_label.setText(Path(path).name)
        self.result_label.setObjectName("resultName")
        self.result_label.style().unpolish(self.result_label)
        self.result_label.style().polish(self.result_label)
        self.play_button.setEnabled(True)
        if self._queue_running:
            if self._queue_items:
                self._queue_items.pop(0)
                self.queue_list.takeItem(0)
            for index in range(self.queue_list.count()):
                item = self.queue_list.item(index)
                item.setText(f"{index + 1}. " + item.text().split(". ", 1)[1])
            self.queue_count_label.setText(f"{len(self._queue_items)} mục")
            self._set_busy(False, f"Đã lưu MP3 · {Path(path).name}")
            self._start_queue_item()
            return
        self._set_busy(False, f"Đã lưu MP3 · {path}")

    def _play_result(self) -> None:
        if not hasattr(self, "player"):
            self.player = QMediaPlayer(self)
            self.audio_output = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_output)
            self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.setSource(QUrl.fromLocalFile(self.current_audio))
        self.player.setPlaybackRate(self.playback_rate)
        self.player.play()

    def _playback_state_changed(self, state) -> None:
        self.stop_button.setEnabled(state != QMediaPlayer.PlaybackState.StoppedState)

    def _stop_result(self) -> None:
        if hasattr(self, "player"):
            self.player.stop()

    def closeEvent(self, event) -> None:
        if self.update_worker is not None and self.update_worker.isRunning():
            self.update_worker.requestInterruption()
            self.update_worker.wait()
        if hasattr(self, "player"):
            self.player.stop()
        if self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(3000)
        super().closeEvent(event)


STYLESHEET = """
QWidget#root { background: #f3f5f2; color: #202923; font-family: 'Segoe UI'; font-size: 14px; }
QFrame#sidebar { background: #17251e; color: #edf4ee; }
QLabel#brand { color: #f5f7f4; font-size: 23px; font-weight: 800; letter-spacing: 1px; }
QFrame#sidebar QLabel { color: #92a49a; font-size: 10px; font-weight: 700; }
QLabel#privacy { color: #9eb5a6; font-size: 11px; line-height: 1.5; }
QPushButton { background: #ffffff; border: 1px solid #dce3dd; border-radius: 7px; color: #25352c; padding: 10px 14px; font-weight: 600; }
QPushButton:hover { background: #eaf1eb; border-color: #9bb7a3; }
QPushButton:disabled { color: #9aa49d; background: #e8ece8; }
QPushButton#nav, QPushButton#navActive { text-align: left; border: none; padding: 12px 14px; color: #a9bbb0; background: transparent; }
QPushButton#navActive { color: #ffffff; background: #2b4135; }
QPushButton#primary { background: #dd633f; color: #ffffff; border: none; font-size: 14px; }
QPushButton#primary:hover { background: #c64e30; }
QPushButton#speedControl { min-width: 28px; max-width: 28px; padding: 6px 4px; }
QPushButton#updateButton { font-size: 11px; padding: 7px 9px; }
QLabel#pageTitle { color: #1a2921; font-size: 28px; font-weight: 750; }
QLabel#muted { color: #718078; }
QLabel#ownership { color: #89968e; font-size: 10px; padding-top: 3px; }
QLabel#sectionLabel { color: #66766c; font-size: 11px; font-weight: 800; }
QLabel#queueTitle { color: #d8e5dc; font-size: 10px; font-weight: 800; }
QLabel#queueCount { color: #9eb5a6; font-size: 9px; }
QListWidget#queueList { background: #203329; color: #edf4ee; border: 1px solid #354b3e; border-radius: 6px; padding: 3px; font-size: 10px; }
QPushButton#queueAction, QPushButton#queuePrimary { text-align: left; font-size: 10px; padding: 7px 8px; }
QPushButton#queueAction { background: #24382d; color: #dce8df; border-color: #3a5143; }
QPushButton#queuePrimary { background: #dd633f; color: #ffffff; border: none; }
QComboBox, QLineEdit { background: #ffffff; border: 1px solid #d8e0d9; border-radius: 7px; padding: 9px 11px; color: #213028; }
QComboBox::drop-down { border: none; width: 28px; }
QPlainTextEdit { background: #ffffff; border: 1px solid #d8e0d9; border-radius: 8px; padding: 14px; color: #26352d; selection-background-color: #b6d5bf; font-size: 15px; }
QLabel#badge, QLabel#badgeReady { background: #e7eae6; color: #637068; border-radius: 12px; padding: 8px 11px; font-size: 10px; font-weight: 800; }
QLabel#badgeReady { background: #d9eee0; color: #286341; }
QFrame#result { background: #e8eee9; border: 1px solid #dce5dd; border-radius: 8px; min-height: 60px; }
QLabel#resultName { color: #26382d; font-weight: 700; }
QPlainTextEdit#voiceList { font-family: 'Segoe UI'; line-height: 1.5; }
"""


def main() -> int:
    if "--smoke" in sys.argv:
        smoke_log = Path(tempfile.gettempdir()) / "giong-viet-smoke.log"
        sys.stdout = smoke_log.open("w", encoding="utf-8")
        sys.stderr = sys.stdout
        with tempfile.TemporaryDirectory(prefix="giong-viet-smoke-") as temporary:
            worker = EngineWorker(Path(temporary))
            generated: list[str] = []
            errors: list[str] = []
            worker.generated.connect(generated.append)
            worker.failed.connect(errors.append)
            worker.initialize()
            if errors or worker.tts is None:
                print("OFFLINE_SMOKE_FAILED:", errors or ["Engine did not initialize."])
                return 1

            voices = worker._voice_rows()
            if not voices:
                print("OFFLINE_SMOKE_FAILED: no preset voices loaded")
                return 1

            try:
                from sea_g2p import G2P

                phonemes = G2P().convert("Xin chao, day la ban kiem tra offline.")
                print(f"G2P_OK: {phonemes}")
            except Exception:
                print("OFFLINE_SMOKE_G2P_FAILED:")
                traceback.print_exc()
                return 1

            smoke_output_dir = Path(temporary) / "outputs"
            worker.generate(
                "Xin chao, day la ban kiem tra offline.",
                voices[0][1],
                str(smoke_output_dir),
                "",
            )
            if errors or not generated:
                print("OFFLINE_SMOKE_FAILED:", errors or ["No audio was generated."])
                return 1

            mp3_output = Path(generated[0])
            if not mp3_output.is_file() or mp3_output.stat().st_size < 4:
                print("OFFLINE_SMOKE_FAILED: MP3 output is missing or empty")
                return 1
            with mp3_output.open("rb") as mp3_file:
                header = mp3_file.read(4)
            if header[:2] != b"\xff\xfb":
                print("OFFLINE_SMOKE_FAILED: invalid MP3 frame header")
                return 1

            print(
                f"OFFLINE_SMOKE_OK: {voices[0][1]}, MP3 only "
                f"({mp3_output.stat().st_size} bytes)"
            )
            return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("GiongViet")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

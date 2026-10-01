from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, Qt, QThread, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

APP_NAME = "GiongViet"
SAMPLE_TEXT = "Xin chào, đây là bản thử giọng đọc tiếng Việt của tôi."
_DLL_DIRECTORY_HANDLES = []


def _onnx_threads() -> int:
    cpu = os.cpu_count() or 1
    if cpu <= 4:
        return 2
    if cpu <= 8:
        return 4
    return 6


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
    generated = Signal(str)
    voice_added = Signal(str, list)
    voice_removed = Signal(str, list)

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.tts = None
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

            self.tts = Vieneu(mode="v3turbo", backend="onnx", threads=_onnx_threads())
            for item in self._read_index():
                audio = self.voice_dir / item["file"]
                if audio.is_file():
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

    @Slot(str, str)
    def generate(self, text: str, voice_id: str) -> None:
        try:
            audio = self.tts.infer(text.strip(), voice=voice_id)
            output = self.output_dir / f"giong-viet-{datetime.now():%Y%m%d-%H%M%S}.wav"
            self.tts.save(audio, str(output))
            self.generated.emit(str(output))
        except Exception as exc:
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
            if duration < 1:
                raise ValueError("Clip quá ngắn hoặc không có âm thanh. WAV 3–8 giây được khuyến nghị.")
            if duration > 30:
                raise ValueError("Clip mẫu dài hơn 30 giây. Hãy cắt còn khoảng 3–8 giây rồi thử lại.")

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
    generate_requested = Signal(str, str)
    add_voice_requested = Signal(str, str, bool)
    remove_voice_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.data_dir = app_data_dir()
        self.voice_names: set[str] = set()
        self.engine_ready = False
        self.setWindowTitle("Giọng Việt")
        self.setMinimumSize(900, 680)
        self.resize(1120, 760)
        self._build_ui()
        self._connect_engine()
        self._set_busy(True, "Đang khởi động VieNeu…")
        self.thread.start()

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
        side.addStretch(1)
        privacy = QLabel("CHẠY TRÊN MÁY NÀY\nKhông API · Không gửi dữ liệu")
        privacy.setObjectName("privacy")
        side.addWidget(privacy)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(34, 25, 34, 28)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._studio_page())
        self.pages.addWidget(self._voices_page())
        content_layout.addWidget(self.pages)
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
        layout.setSpacing(16)

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
        self.engine_badge = QLabel("●  ĐANG TẢI ENGINE")
        self.engine_badge.setObjectName("badge")
        heading.addWidget(self.engine_badge, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)

        text_label = QLabel("NỘI DUNG")
        text_label.setObjectName("sectionLabel")
        layout.addWidget(text_label)
        self.text_input = QPlainTextEdit()
        self.text_input.setPlaceholderText("Nhập nội dung cần đọc…")
        self.text_input.setPlainText(SAMPLE_TEXT)
        self.text_input.setMinimumHeight(205)
        layout.addWidget(self.text_input)

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
        self.open_button = QPushButton("Mở thư mục")
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir / "outputs"))))
        result_layout.addWidget(self.open_button)
        layout.addWidget(self.result_frame)
        layout.addStretch(1)
        return page

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
        self.worker.ready.connect(self._engine_ready)
        self.worker.failed.connect(self._engine_failed)
        self.worker.generated.connect(self._generated)
        self.worker.voice_added.connect(self._voice_added)
        self.worker.voice_removed.connect(self._voice_removed)
        self.generate_requested.connect(self.worker.generate)
        self.add_voice_requested.connect(self.worker.add_voice)
        self.remove_voice_requested.connect(self.worker.remove_voice)

    def _navigate(self, page: int) -> None:
        self.pages.setCurrentIndex(page)
        self.nav_studio.setObjectName("navActive" if page == 0 else "nav")
        self.nav_voices.setObjectName("navActive" if page == 1 else "nav")
        self.nav_studio.style().unpolish(self.nav_studio)
        self.nav_studio.style().polish(self.nav_studio)
        self.nav_voices.style().unpolish(self.nav_voices)
        self.nav_voices.style().polish(self.nav_voices)

    def _engine_ready(self, voices: list) -> None:
        self.engine_ready = True
        self.voice_combo.clear()
        self.voice_names = {voice_id for _, voice_id in voices}
        for label, voice_id in voices:
            self.voice_combo.addItem(label, voice_id)
        self._refresh_voice_page(voices)
        self.engine_badge.setText("●  LOCAL · ONNX / CPU")
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
        if self.engine_ready:
            self._set_busy(False, "Tác vụ thất bại; bạn có thể thử lại.")
            QMessageBox.critical(self, "Tác vụ thất bại", message)
            return

        self._set_busy(False, "Engine chưa sẵn sàng.")
        self.engine_badge.setText("●  CẦN KIỂM TRA")
        QMessageBox.critical(self, "VieNeu chưa khởi động", message)

    def _set_busy(self, busy: bool, message: str) -> None:
        ready = self.engine_ready and self.voice_combo.count() > 0
        self.generate_button.setEnabled(not busy and ready)
        self.clone_button.setEnabled(not busy and ready)
        self.status.setText(message)
        if busy:
            self.generate_button.setText("Đang xử lý…")
        else:
            self.generate_button.setText("Tạo âm thanh   →")

    def _generate(self) -> None:
        text = self.text_input.toPlainText().strip()
        voice_id = self.voice_combo.currentData()
        if not text:
            QMessageBox.information(self, "Chưa có nội dung", "Hãy nhập nội dung cần đọc trước.")
            return
        if not voice_id:
            return
        self._set_busy(True, "Đang tạo giọng trên máy này…")
        self.generate_requested.emit(text, voice_id)

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
            QMessageBox.StandardButton.Yes,
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
        self.current_audio = path
        self.result_label.setText(Path(path).name)
        self.result_label.setObjectName("resultName")
        self.result_label.style().unpolish(self.result_label)
        self.result_label.style().polish(self.result_label)
        self.play_button.setEnabled(True)
        self._set_busy(False, f"Đã lưu WAV · {path}")

    def _play_result(self) -> None:
        if not hasattr(self, "player"):
            self.player = QMediaPlayer(self)
            self.audio_output = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_output)
        self.player.setSource(QUrl.fromLocalFile(self.current_audio))
        self.player.play()

    def closeEvent(self, event) -> None:
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
QLabel#pageTitle { color: #1a2921; font-size: 28px; font-weight: 750; }
QLabel#muted { color: #718078; }
QLabel#sectionLabel { color: #66766c; font-size: 11px; font-weight: 800; }
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

            worker.generate("Xin chao, day la ban kiem tra offline.", voices[0][1])
            if errors or not generated:
                print("OFFLINE_SMOKE_FAILED:", errors or ["No WAV was generated."])
                return 1

            import soundfile as sf

            audio_info = sf.info(generated[0])
            if audio_info.frames <= 0 or audio_info.samplerate != 48_000:
                print("OFFLINE_SMOKE_FAILED: invalid output", audio_info)
                return 1

            print(f"OFFLINE_SMOKE_OK: {voices[0][1]}, {audio_info.frames} frames at {audio_info.samplerate} Hz")
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

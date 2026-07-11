import sys
import os

# Prepend conda lib to LD_LIBRARY_PATH so the dynamic linker resolves
# GLib/libxcb from the conda environment before system paths.
# Fixes ImportError("undefined symbol: g_pointer_bit_unlock_and_set")
# and "Could not load Qt platform plugin xcb" when PyQt5 + opencv coexist.
_conda_lib = os.path.join(os.path.dirname(sys.executable), "..", "lib")
_conda_lib = os.path.realpath(_conda_lib)
_curr = os.environ.get("LD_LIBRARY_PATH", "")
if _conda_lib not in _curr.split(":"):
    os.environ["LD_LIBRARY_PATH"] = f"{_conda_lib}:{_curr}" if _curr else _conda_lib
    # Restart so the linker uses the updated path
    os.execv(sys.executable, [sys.executable] + sys.argv)

import shlex
import cv2
import yaml
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QMutex, QMutexLocker, QPoint, QProcess
from PyQt5.QtGui import QImage, QPixmap, QPainter, QPen, QColor
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QLineEdit, QTabWidget, QMessageBox, QGroupBox,
    QGridLayout, QComboBox, QTextEdit, QDoubleSpinBox
)

# ========= Constants =========
PROCESSING_WIDTH = 1920
PROCESSING_HEIGHT = 1080

os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = "/usr/lib/aarch64-linux-gnu/qt5/plugins/platforms"
os.environ["QT_PLUGIN_PATH"] = "/usr/lib/aarch64-linux-gnu/qt5/plugins"

# ========= Data classes =========
@dataclass
class Calibration:
    width_meters: float = 3.5
    length_meters: float = 20.0
    points: List[Tuple[int, int]] = field(default_factory=list)
    expanded_roi: List[Tuple[int, int]] = field(default_factory=list)

    def calculate_target_points(self) -> List[List[int]]:
        w = int(self.width_meters)
        h = int(self.length_meters)
        return [[0, 0], [w, 0], [w, h], [0, h]]

    def calculate_expanded_roi(self, expansion_factor: float = 1.2):
        if len(self.points) != 4:
            return
        cx = sum(x for x, y in self.points) / 4
        cy = sum(y for x, y in self.points) / 4
        expanded = []
        for x, y in self.points:
            dx = x - cx
            dy = y - cy
            new_x = int(cx + dx * expansion_factor)
            new_y = int(cy + dy * expansion_factor)
            new_x = max(0, min(new_x, PROCESSING_WIDTH))
            new_y = max(0, min(new_y, PROCESSING_HEIGHT))
            expanded.append((new_x, new_y))
        self.expanded_roi = expanded

    def to_yaml_dict(self):
        return {
            "SOURCE": [[int(x), int(y)] for (x, y) in self.points],
            "TARGET_WIDTH": int(self.width_meters),
            "TARGET_HEIGHT": int(self.length_meters),
            "TARGET": self.calculate_target_points(),
        }

@dataclass
class SourceItem:
    uri: str
    calib: Calibration = field(default_factory=Calibration)
    captured_frame: Optional[any] = None
    last_preview_frame: Optional[any] = None

# ========= Helpers =========

# ========= Video thread =========
class VideoThread(QThread):
    frame_ready = pyqtSignal(object)
    opened = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self._source = None
        self._running = False
        self._mutex = QMutex()

    def set_source(self, source: str):
        with QMutexLocker(self._mutex):
            self._source = source

    def stop(self):
        with QMutexLocker(self._mutex):
            self._running = False

    def run(self):
        with QMutexLocker(self._mutex):
            src = self._source
        self._running = True
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            self.opened.emit(False, f"Cannot open source: {src}")
            return
        self.opened.emit(True, src)
        try:
            while self._running:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frame = cv2.resize(frame, (PROCESSING_WIDTH, PROCESSING_HEIGHT))
                self.frame_ready.emit(frame)
                self.msleep(15)
        finally:
            cap.release()
            self._running = False

# ========= VideoWidget with overlay =========
class VideoWidget(QLabel):
    clicked = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(720, 405)
        self.setAlignment(Qt.AlignCenter)
        self._frame = None
        self._pix = None
        self._scale = 1.0
        self._offset = QPoint(0, 0)
        self._points: List[Tuple[int, int]] = []
        self._expanded_roi: List[Tuple[int, int]] = []

    def set_frame(self, frame_bgr):
        self._frame = frame_bgr
        self._update_pixmap()

    def get_frame(self):
        return self._frame

    def set_points(self, pts: List[Tuple[int, int]]):
        self._points = pts[:]
        self.update()

    def clear_points(self):
        self._points = []
        self.update()

    def set_expanded_roi(self, pts: List[Tuple[int, int]]):
        self._expanded_roi = pts[:]
        self.update()

    def clear_expanded_roi(self):
        self._expanded_roi = []
        self.update()

    def _update_pixmap(self):
        if self._frame is None:
            self._pix = None
            self.clear()
            return
        h, w = self._frame.shape[:2]
        rgb = cv2.cvtColor(self._frame, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg)
        scaled = pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._scale = scaled.width() / w
        x_off = (self.width() - scaled.width()) // 2
        y_off = (self.height() - scaled.height()) // 2
        self._offset = QPoint(x_off, y_off)
        self._pix = scaled
        self.setPixmap(self._pix)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_pixmap()

    def mousePressEvent(self, event):
        if self._frame is None:
            return
        if event.button() == Qt.LeftButton:
            x = (event.x() - self._offset.x()) / self._scale
            y = (event.y() - self._offset.y()) / self._scale
            h, w = self._frame.shape[:2]
            if 0 <= x < w and 0 <= y < h:
                self.clicked.emit(int(x), int(y))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._pix is None:
            return
        p = QPainter(self)
        p.translate(self._offset)
        p.scale(self._scale, self._scale)

        # Draw expanded ROI (yellow, dashed)
        if self._expanded_roi and len(self._expanded_roi) == 4:
            pen = QPen(QColor(255, 255, 0), 3, Qt.DashLine)
            p.setPen(pen)
            for i in range(4):
                x1, y1 = self._expanded_roi[i]
                x2, y2 = self._expanded_roi[(i+1)%4]
                p.drawLine(x1, y1, x2, y2)

        # Draw source polygon (green, solid)
        if self._points:
            p.setPen(QPen(QColor(0, 255, 0), 2))
            for i in range(len(self._points)):
                x1, y1 = self._points[i]
                x2, y2 = self._points[(i+1) % len(self._points)]
                p.drawLine(x1, y1, x2, y2)
            for idx, (x, y) in enumerate(self._points):
                p.setPen(QPen(QColor(255, 0, 0), 6))
                p.drawPoint(x, y)
                p.setPen(QPen(QColor(255, 255, 0), 1))
                p.drawText(x + 5, y - 5, str(idx))

# ========= Main Window (simplified, 2 tabs) =========
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DeepStream Speed - Calibration & Run")
        self.setMinimumSize(1200, 800)

        self.sources: Dict[str, SourceItem] = {}
        self.vthread = VideoThread()
        self.vthread.frame_ready.connect(self.on_frame_ready)
        self.vthread.opened.connect(self.on_opened)

        self.tabs = QTabWidget()
        self.tab_config = QWidget()
        self.tab_run = QWidget()
        self.tabs.addTab(self.tab_config, "Configuration & Calibration")
        self.tabs.addTab(self.tab_run, "Run")

        root = QVBoxLayout(self)
        root.addWidget(self.tabs)

        self._build_tab_config()
        self._build_tab_run()

    # ---------- Tab: Configuration & Calibration ----------
    def _build_tab_config(self):
        layout = QVBoxLayout(self.tab_config)

        # Source management
        src_group = QGroupBox("Video Source")
        src_layout = QHBoxLayout(src_group)
        self.combo_source = QComboBox()
        self.combo_source.setMinimumWidth(300)
        self.btn_add_source = QPushButton("Add")
        self.btn_remove_source = QPushButton("Remove")
        self.btn_browse_file = QPushButton("Browse File")
        self.le_new_uri = QLineEdit()
        self.le_new_uri.setPlaceholderText("File path or rtsp://...")
        src_layout.addWidget(self.combo_source)
        src_layout.addWidget(self.btn_add_source)
        src_layout.addWidget(self.btn_remove_source)
        src_layout.addWidget(self.btn_browse_file)
        src_layout.addWidget(self.le_new_uri)
        layout.addWidget(src_group)

        # Video preview
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        self.video_widget = VideoWidget()
        btn_row = QHBoxLayout()
        self.btn_preview = QPushButton("Start Preview")
        self.btn_stop_preview = QPushButton("Stop Preview")
        self.btn_capture = QPushButton("Capture Frame")
        self.btn_use_last = QPushButton("Use Current Frame")
        btn_row.addWidget(self.btn_preview)
        btn_row.addWidget(self.btn_stop_preview)
        btn_row.addWidget(self.btn_capture)
        btn_row.addWidget(self.btn_use_last)
        preview_layout.addWidget(self.video_widget)
        preview_layout.addLayout(btn_row)
        layout.addWidget(preview_group)

        # Calibration controls
        calib_group = QGroupBox("Calibration (4 points on road)")
        calib_layout = QGridLayout(calib_group)
        calib_layout.addWidget(QLabel("Real Width (m):"), 0, 0)
        self.dsb_width = QDoubleSpinBox()
        self.dsb_width.setRange(0.5, 50.0)
        self.dsb_width.setValue(3.5)
        self.dsb_width.setSingleStep(0.5)
        self.dsb_width.setSuffix(" m")
        calib_layout.addWidget(self.dsb_width, 0, 1)
        calib_layout.addWidget(QLabel("Measurement Region Length (m):"), 0, 2)
        self.dsb_length = QDoubleSpinBox()
        self.dsb_length.setRange(1.0, 200.0)
        self.dsb_length.setValue(20.0)
        self.dsb_length.setSingleStep(1.0)
        self.dsb_length.setSuffix(" m")
        calib_layout.addWidget(self.dsb_length, 0, 3)

        self.btn_clear_points = QPushButton("Clear Points")
        self.btn_save_config = QPushButton("Save Configuration")
        calib_layout.addWidget(self.btn_clear_points, 1, 0, 1, 2)
        calib_layout.addWidget(self.btn_save_config, 1, 2, 1, 2)
        layout.addWidget(calib_group)

        # Signals
        self.combo_source.currentTextChanged.connect(self.on_source_changed)
        self.btn_add_source.clicked.connect(self.on_add_source)
        self.btn_remove_source.clicked.connect(self.on_remove_source)
        self.btn_browse_file.clicked.connect(self.on_browse_file)
        self.btn_preview.clicked.connect(self.on_start_preview)
        self.btn_stop_preview.clicked.connect(self.on_stop_preview)
        self.btn_capture.clicked.connect(self.on_capture_frame)
        self.btn_use_last.clicked.connect(self.on_use_last_frame)
        self.video_widget.clicked.connect(self.on_video_click)
        self.btn_clear_points.clicked.connect(self.on_clear_points)
        self.btn_save_config.clicked.connect(self.on_save_config)

        # Update width/length when source changes
        self.dsb_width.valueChanged.connect(self.on_measurement_changed)
        self.dsb_length.valueChanged.connect(self.on_measurement_changed)

    def on_source_changed(self, uri: str):
        if not uri or uri not in self.sources:
            self.video_widget.set_frame(None)
            self.video_widget.clear_points()
            self.video_widget.clear_expanded_roi()
            return
        si = self.sources[uri]
        self.dsb_width.blockSignals(True)
        self.dsb_length.blockSignals(True)
        self.dsb_width.setValue(si.calib.width_meters)
        self.dsb_length.setValue(si.calib.length_meters)
        self.dsb_width.blockSignals(False)
        self.dsb_length.blockSignals(False)
        self.video_widget.set_points(si.calib.points)
        self.video_widget.set_expanded_roi(si.calib.expanded_roi)
        frame = si.captured_frame or si.last_preview_frame
        self.video_widget.set_frame(frame)

    def on_add_source(self):
        uri = self.le_new_uri.text().strip()
        if not uri:
            QMessageBox.warning(self, "Missing URL", "Enter file path or RTSP URL.")
            return
        if not (uri.startswith("rtsp://") or os.path.exists(uri)):
            QMessageBox.warning(self, "Invalid Source",
                                "Enter existing file or RTSP starting with rtsp://")
            return
        if uri in self.sources:
            QMessageBox.information(self, "Already Exists", "This source is already in the list.")
            return
        self.sources[uri] = SourceItem(uri=uri)
        self.combo_source.addItem(uri)
        self.combo_source.setCurrentText(uri)
        self.le_new_uri.clear()
        # Also update run tab combo
        self._refresh_run_combo()

    def on_remove_source(self):
        uri = self.combo_source.currentText()
        if not uri:
            return
        self.sources.pop(uri, None)
        idx = self.combo_source.findText(uri)
        if idx >= 0:
            self.combo_source.removeItem(idx)
        self.video_widget.set_frame(None)
        self._refresh_run_combo()

    def on_browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "",
                                               "Videos (*.mp4 *.avi *.mkv *.MOV);;All files (*)")
        if path:
            self.le_new_uri.setText(path)

    def on_start_preview(self):
        uri = self.combo_source.currentText()
        if not uri:
            QMessageBox.information(self, "Not Selected", "Please select a source from the list.")
            return
        self.vthread.set_source(uri)
        self.vthread.start()

    def on_stop_preview(self):
        if self.vthread.isRunning():
            self.vthread.stop()
            self.vthread.wait(500)

    def on_capture_frame(self):
        uri = self.combo_source.currentText()
        if uri not in self.sources:
            return
        frame = self.video_widget.get_frame()
        if frame is None:
            QMessageBox.information(self, "No Frame",
                                    "Please start preview or use 'Use Current Frame'.")
            return
        self.sources[uri].captured_frame = frame.copy()
        self.video_widget.set_frame(self.sources[uri].captured_frame)

    def on_use_last_frame(self):
        uri = self.combo_source.currentText()
        if uri not in self.sources:
            return
        si = self.sources[uri]
        if si.last_preview_frame is None:
            QMessageBox.information(self, "No Frame",
                                    "Please start preview to get a frame.")
            return
        si.captured_frame = si.last_preview_frame.copy()
        self.video_widget.set_frame(si.captured_frame)

    def on_video_click(self, x, y):
        uri = self.combo_source.currentText()
        if uri not in self.sources:
            return
        si = self.sources[uri]
        if si.captured_frame is None and si.last_preview_frame is None:
            QMessageBox.information(self, "No Frame",
                                    "Please capture a frame or preview first.")
            return
        if len(si.calib.points) >= 4:
            QMessageBox.information(self, "4 Points Enough",
                                    "Click 'Clear Points' to select again.")
            return
        si.calib.points.append((x, y))
        self.video_widget.set_points(si.calib.points)
        if len(si.calib.points) == 4:
            si.calib.calculate_expanded_roi(1.2)
            self.video_widget.set_expanded_roi(si.calib.expanded_roi)
            QMessageBox.information(self, "Complete",
                                    "4 points selected. Expanded ROI (yellow) calculated.")

    def on_clear_points(self):
        uri = self.combo_source.currentText()
        if uri in self.sources:
            self.sources[uri].calib.points = []
            self.sources[uri].calib.expanded_roi = []
            self.video_widget.clear_points()
            self.video_widget.clear_expanded_roi()

    def on_measurement_changed(self):
        uri = self.combo_source.currentText()
        if uri in self.sources:
            self.sources[uri].calib.width_meters = self.dsb_width.value()
            self.sources[uri].calib.length_meters = self.dsb_length.value()
            # Recalculate expanded ROI if points exist
            if len(self.sources[uri].calib.points) == 4:
                self.sources[uri].calib.calculate_expanded_roi(1.2)
                self.video_widget.set_expanded_roi(self.sources[uri].calib.expanded_roi)

    def on_save_config(self):
        uri = self.combo_source.currentText()
        if uri not in self.sources:
            QMessageBox.warning(self, "No Source Selected", "Please select a source first.")
            return
        si = self.sources[uri]
        if len(si.calib.points) != 4:
            QMessageBox.warning(self, "Missing Points", "Please select 4 points on the road.")
            return
        if si.captured_frame is None and si.last_preview_frame is None:
            QMessageBox.warning(self, "Missing Image", "Please capture a frame or preview first.")
            return

        # Update measurements
        si.calib.width_meters = self.dsb_width.value()
        si.calib.length_meters = self.dsb_length.value()
        if len(si.calib.points) == 4:
            si.calib.calculate_expanded_roi(1.2)

        # YAML file
        yaml_dir = os.path.join(os.getcwd(), "configs")
        os.makedirs(yaml_dir, exist_ok=True)
        default_yaml = os.path.join(yaml_dir, f"points_{uri.replace(':', '_').replace('/', '_')}.yml")
        yaml_path, _ = QFileDialog.getSaveFileName(
            self, "Save YAML File", default_yaml, "YAML (*.yml *.yaml)"
        )
        if not yaml_path:
            return
        with open(yaml_path, 'w') as f:
            yaml.safe_dump(si.calib.to_yaml_dict(), f, sort_keys=False)

        # Analytics config
        analytics_path = os.path.join(yaml_dir, "config_nvdsanalytics.txt")
        self._write_analytics_config(analytics_path, si.calib.expanded_roi)

        QMessageBox.information(
            self, "Saved",
            f"✓ YAML: {yaml_path}\n✓ Analytics: {analytics_path}"
        )

    def _write_analytics_config(self, path: str, roi_points: List[Tuple[int, int]]):
        if len(roi_points) != 4:
            return
        roi_str = ";".join(f"{int(x)};{int(y)}" for x, y in roi_points)
        # Write minimal config
        config = [
            "[property]\n",
            "enable=1\n",
            f"config-width={PROCESSING_WIDTH}\n",
            f"config-height={PROCESSING_HEIGHT}\n",
            "osd-mode=2\n",
            "display-font-size=12\n",
            "\n",
            "[roi-filtering-stream-0]\n",
            "enable=1\n",
            f"roi-RF={roi_str}\n",
            "inverse-roi=0\n",
            "class-id=-1\n",
        ]
        with open(path, 'w') as f:
            f.writelines(config)

    # ---------- Tab: Run ----------
    def _build_tab_run(self):
        layout = QVBoxLayout(self.tab_run)

        src_layout = QHBoxLayout()
        src_layout.addWidget(QLabel("Source:"))
        self.combo_run_source = QComboBox()
        src_layout.addWidget(self.combo_run_source)
        layout.addLayout(src_layout)

        backend_layout = QHBoxLayout()
        backend_layout.addStretch()
        layout.addLayout(backend_layout)

        btn_layout = QHBoxLayout()
        self.btn_run_display = QPushButton("Display (file/RTSP)")
        self.btn_run_mp4 = QPushButton("Record MP4")
        self.btn_run_rtsp = QPushButton("Display RTSP")
        self.btn_stop = QPushButton("Stop")
        btn_layout.addWidget(self.btn_run_display)
        btn_layout.addWidget(self.btn_run_mp4)
        btn_layout.addWidget(self.btn_run_rtsp)
        btn_layout.addWidget(self.btn_stop)
        layout.addLayout(btn_layout)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        layout.addWidget(self.txt_log)

        self.proc: Optional[QProcess] = None

        self.btn_run_display.clicked.connect(lambda: self.run_pipeline("display"))
        self.btn_run_mp4.clicked.connect(lambda: self.run_pipeline("file"))
        self.btn_run_rtsp.clicked.connect(lambda: self.run_pipeline("rtsp"))
        self.btn_stop.clicked.connect(self.on_stop_proc)

    def _refresh_run_combo(self):
        self.combo_run_source.clear()
        for uri in self.sources.keys():
            self.combo_run_source.addItem(uri)

    def run_pipeline(self, mode: str):
        uri = self.combo_run_source.currentText()
        if not uri:
            QMessageBox.warning(self, "No Source Selected", "Please select a source from the list.")
            return

        if self.proc and self.proc.state() == QProcess.Running:
            QMessageBox.warning(self, "Running", "Pipeline is running. Please Stop first.")
            return

        # Fix #14: build argv list and pass directly to QProcess so URIs and
        # file paths containing spaces are not mangled by shell word-splitting.
        argv = [
            "python3", "main.py",
            "--source", uri,
            "--width", str(PROCESSING_WIDTH),
            "--height", str(PROCESSING_HEIGHT),
        ]

        if mode == "display":
            argv += ["--mode", "display"]
        elif mode == "file":
            out_dir = os.path.join(os.getcwd(), "output")
            os.makedirs(out_dir, exist_ok=True)
            out_file = os.path.join(out_dir, "output.mp4")
            argv += ["--mode", "file", "--output", out_file]
        elif mode == "rtsp":
            if not uri.startswith("rtsp://"):
                QMessageBox.warning(self, "Not RTSP", "This mode requires RTSP source.")
                return
            argv += ["--mode", "display"]

        # Log the equivalent shell command for visibility
        cmd_display = " ".join(shlex.quote(a) for a in argv)
        self.txt_log.append(f"$ {cmd_display}")
        if self.proc is None:
            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self._read_proc_out)
            self.proc.finished.connect(lambda: self.txt_log.append("=== Finished ==="))
        program = argv[0]
        self.proc.start(program, argv[1:])

    def _read_proc_out(self):
        if not self.proc:
            return
        out = bytes(self.proc.readAllStandardOutput()).decode(errors="ignore")
        if out:
            self.txt_log.append(out)

    def on_stop_proc(self):
        if self.proc:
            self.proc.kill()
            self.proc = None
            self.txt_log.append("=== Stopped ===")

    # ---------- Common slots ----------
    def on_opened(self, ok: bool, msg: str):
        if not ok:
            QMessageBox.critical(self, "Error Opening Source", msg)

    def on_frame_ready(self, frame):
        self.video_widget.set_frame(frame)
        uri = self.combo_source.currentText()
        if uri in self.sources:
            self.sources[uri].last_preview_frame = frame

# ========= Main =========
def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
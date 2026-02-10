#!/usr/bin/env python3
"""
CBA-IV Battery Test GUI (Qt6)

- Works with PySide6 or PyQt6 (auto-detects).
- Live graph (V/A/W) using QtCharts.
- Live stats + log.
- Worker thread performs I/O with the CBA device so UI doesn't block.

Requires:
  - wmr_cba
  - PySide6 OR PyQt6
  - QtCharts bindings (PySide6.QtCharts or PyQt6.QtCharts)

Run:
  python3 cba_gui.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Optional

# ---- Qt shim: PySide6 first, then PyQt6 ----
QT_API = None
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
    QT_API = "PySide6"
except Exception:
    from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore
    from PyQt6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis  # type: ignore
    QT_API = "PyQt6"

# ---- CBA library ----
from wmr_cba import wmr_cba


@dataclass
class Sample:
    t_s: float
    v: float
    a: float
    w: float
    ah: float
    wh: float


class CbaWorker(QtCore.QThread):
    """
    Background thread that owns the CBA object and performs the test loop.
    Emits signals with fresh samples and status/log messages.
    """
    sample_signal = QtCore.pyqtSignal(object) if QT_API == "PyQt6" else QtCore.Signal(object)
    status_signal = QtCore.pyqtSignal(str) if QT_API == "PyQt6" else QtCore.Signal(str)
    finished_signal = QtCore.pyqtSignal(str) if QT_API == "PyQt6" else QtCore.Signal(str)

    def __init__(self, amps: float, cutoff: float, interval_s: float, parent=None):
        super().__init__(parent)
        self.amps = float(amps)
        self.cutoff = float(cutoff)
        self.interval_s = float(interval_s)

        self._stop_requested = False
        self._cba = None

    def request_stop(self) -> None:
        self._stop_requested = True

    def _sleep_interruptible(self, total_s: float, step_s: float = 0.05) -> None:
        end = time.monotonic() + total_s
        while not self._stop_requested:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(step_s, remaining))

    def _wait_for_running(self, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while not self._stop_requested and time.monotonic() < deadline:
            try:
                if bool(self._cba.is_running()):
                    return True
            except Exception:
                pass
            self._sleep_interruptible(0.05)
        return False

    def _safe_stop_close(self) -> None:
        if self._cba is None:
            return
        try:
            self._cba.do_stop()
        except Exception:
            pass
        try:
            self._cba.close()
        except Exception:
            pass

    def run(self) -> None:
        try:
            self.status_signal.emit(f"Using {QT_API}. Connecting to CBA-IV...")
            self._cba = wmr_cba.CBA4()
            self.status_signal.emit("Connected. Starting test...")

            # Start test with device cutoff enforcement
            self._cba.do_start(self.amps, self.cutoff)

            # Wait for device to actually begin running
            if not self._wait_for_running(timeout_s=5.0):
                if self._stop_requested:
                    self.status_signal.emit("Stopped before test started.")
                    self.finished_signal.emit("Stopped.")
                    return
                self.status_signal.emit("ERROR: Device did not report running within 5 seconds.")
                self.finished_signal.emit("Start failed.")
                return

            self.status_signal.emit("Test running.")

            start_t = time.monotonic()
            last_t = start_t
            ah = 0.0
            wh = 0.0

            while not self._stop_requested:
                # Sample voltage over the interval to average out noise.
                # The worker thread updates cached status every ~750ms, so
                # longer intervals yield more distinct readings.
                voltage_samples = []
                sample_interval = 0.5  # seconds between samples
                interval_end = time.monotonic() + self.interval_s
                status = None

                while not self._stop_requested:
                    try:
                        s = self._cba.get_status_response()
                    except Exception:
                        s = None

                    if s is not None:
                        status = s
                        if (s[1] & 2) != 2:
                            break
                        sample_v = (s[20] + (s[21] << 8) + (s[22] << 16) + (s[23] << 24)) / 1_000_000
                        voltage_samples.append(sample_v)

                    remaining = interval_end - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(sample_interval, remaining))

                if self._stop_requested:
                    break

                if status is None:
                    continue

                running = (status[1] & 2) == 2
                if not running:
                    self.status_signal.emit("CBA-IV stopped the test (is_running() == False).")
                    break

                now = time.monotonic()
                dt = now - last_t
                last_t = now

                v = sum(voltage_samples) / len(voltage_samples)
                # Use set current (status[3:7]) rather than measured current (status[16:20]).
                # Measured current is only 10-bit resolution across 40A (~0.04A steps),
                # which causes visible jumps in the display and integration error in Ah/Wh.
                a = (status[3] + (status[4] << 8) + (status[5] << 16) + (status[6] << 24)) / 1_000_000
                w = v * a

                if dt > 0:
                    hours = dt / 3600.0
                    ah += a * hours
                    wh += w * hours

                t_s = now - start_t
                sample = Sample(t_s=t_s, v=v, a=a, w=w, ah=ah, wh=wh)
                self.sample_signal.emit(sample)

                # Guard cutoff
                if v <= self.cutoff:
                    self.status_signal.emit(f"Cutoff reached (guard): {v:.4f} V <= {self.cutoff:.4f} V")
                    break

            duration = time.monotonic() - start_t
            self.finished_signal.emit(
                f"Done. Duration {duration:.1f}s, {ah:.6f}Ah, {wh:.6f}Wh"
            )

        except Exception as e:
            self.status_signal.emit(f"ERROR: {e}")
            self.finished_signal.emit("Error.")
        finally:
            self._safe_stop_close()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CBA-IV Battery Test (Qt)")
        self.resize(1050, 700)

        self.worker: Optional[CbaWorker] = None
        self.t0_wall = time.time()

        # ---------- Controls ----------
        self.amps_edit = QtWidgets.QDoubleSpinBox()
        self.amps_edit.setRange(0.01, 40.0)
        self.amps_edit.setDecimals(3)
        self.amps_edit.setValue(5.0)
        self.amps_edit.setSuffix(" A")

        self.cutoff_edit = QtWidgets.QDoubleSpinBox()
        self.cutoff_edit.setRange(0.1, 1000.0)
        self.cutoff_edit.setDecimals(3)
        self.cutoff_edit.setValue(10.5)
        self.cutoff_edit.setSuffix(" V")

        self.interval_edit = QtWidgets.QDoubleSpinBox()
        self.interval_edit.setRange(1.0, 10.0)
        self.interval_edit.setDecimals(2)
        self.interval_edit.setValue(1.0)
        self.interval_edit.setSuffix(" s")

        self.xaxis_combo = QtWidgets.QComboBox()
        self.xaxis_combo.addItems(["Time (s)", "Amp-hours (Ah)"])
        self.xaxis_combo.currentIndexChanged.connect(self._on_xaxis_changed)

        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self.start_btn.clicked.connect(self.on_start)
        self.stop_btn.clicked.connect(self.on_stop)

        controls = QtWidgets.QFormLayout()
        controls.addRow("Discharge current:", self.amps_edit)
        controls.addRow("Cutoff voltage:", self.cutoff_edit)
        controls.addRow("Update interval:", self.interval_edit)
        controls.addRow("X axis:", self.xaxis_combo)

        btns = QtWidgets.QHBoxLayout()
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)

        controls_box = QtWidgets.QVBoxLayout()
        controls_box.addLayout(controls)
        controls_box.addLayout(btns)
        controls_box.addStretch(1)

        controls_widget = QtWidgets.QWidget()
        controls_widget.setLayout(controls_box)

        # ---------- Stats ----------
        self.stat_label = QtWidgets.QLabel("Idle.")
        self.stat_label.setFont(QtGui.QFont("Monospace", 11))
        self.stat_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)

        # ---------- Log ----------
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)

        # ---------- Chart ----------
        self.series_v = QLineSeries()
        self.series_a = QLineSeries()
        self.series_w = QLineSeries()
        self.series_v.setName("Voltage (V)")
        self.series_a.setName("Current (A)")
        self.series_w.setName("Power (W)")

        self.chart = QChart()
        self.chart.addSeries(self.series_v)
        self.chart.addSeries(self.series_a)
        self.chart.addSeries(self.series_w)
        self.chart.legend().setVisible(True)
        self.chart.setTitle("Live Discharge Telemetry")

        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("Time (s)")
        self.axis_x.setRange(0, 60)

        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("Value")
        self.axis_y.setRange(0, 20)

        self.chart.addAxis(self.axis_x, QtCore.Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.axis_y, QtCore.Qt.AlignmentFlag.AlignLeft)

        for s in (self.series_v, self.series_a, self.series_w):
            s.attachAxis(self.axis_x)
            s.attachAxis(self.axis_y)

        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # ---------- Layout ----------
        left = QtWidgets.QVBoxLayout()
        left.addWidget(controls_widget)
        left.addWidget(QtWidgets.QLabel("Status / Stats:"))
        left.addWidget(self.stat_label)
        left.addWidget(QtWidgets.QLabel("Log:"))
        left.addWidget(self.log)

        left_widget = QtWidgets.QWidget()
        left_widget.setLayout(left)

        splitter = QtWidgets.QSplitter()
        splitter.setOrientation(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(self.chart_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)

        self._y_max = 1.0
        self._samples: list[Sample] = []
        self._reset_series()

    def _reset_series(self):
        self.series_v.clear()
        self.series_a.clear()
        self.series_w.clear()
        self._y_max = 1.0
        self._samples = []
        self.axis_x.setRange(0, 60)
        self.axis_y.setRange(0, 20)

    def _append_log(self, msg: str):
        self.log.appendPlainText(msg)
        # auto-scroll
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _fmt_duration(self, seconds: float) -> str:
        sec_i = int(round(seconds))
        h = sec_i // 3600
        m = (sec_i % 3600) // 60
        s = sec_i % 60
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def closeEvent(self, event):
        if self.worker is not None:
            self.worker.request_stop()
            self.worker.wait(3000)
        event.accept()

    def on_start(self):
        if self.worker is not None:
            return

        amps = float(self.amps_edit.value())
        cutoff = float(self.cutoff_edit.value())
        interval_s = float(self.interval_edit.value())

        self._reset_series()
        self._append_log(f"Starting: {amps:.3f}A cutoff {cutoff:.3f}V interval {interval_s:.2f}s")
        self.stat_label.setText("Starting...")

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self.worker = CbaWorker(amps=amps, cutoff=cutoff, interval_s=interval_s)
        self.worker.sample_signal.connect(self.on_sample)
        self.worker.status_signal.connect(self.on_status)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def on_stop(self):
        if self.worker is None:
            return
        self._append_log("Stop requested.")
        self.worker.request_stop()
        # keep UI responsive; worker thread will emit finished when done

    def on_status(self, msg: str):
        self._append_log(msg)

    def on_finished(self, msg: str):
        self._append_log(msg)
        self.stat_label.setText(msg)

        # cleanup worker
        if self.worker is not None:
            try:
                self.worker.wait(2000)
            except Exception:
                pass
        self.worker = None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _x_for_sample(self, sample: Sample) -> float:
        if self.xaxis_combo.currentIndex() == 1:
            return sample.ah
        return sample.t_s

    def _replot(self):
        """Re-plot all stored samples using the current X-axis mode."""
        self.series_v.clear()
        self.series_a.clear()
        self.series_w.clear()
        for s in self._samples:
            x = self._x_for_sample(s)
            self.series_v.append(x, s.v)
            self.series_a.append(x, s.a)
            self.series_w.append(x, s.w)
        self._update_axes()

    def _update_axes(self):
        if self.xaxis_combo.currentIndex() == 1:
            self.axis_x.setTitleText("Amp-hours (Ah)")
            x_last = self._samples[-1].ah if self._samples else 0
            x_max = max(0.001, x_last * 1.05)
        else:
            self.axis_x.setTitleText("Time (s)")
            x_last = self._samples[-1].t_s if self._samples else 0
            x_max = max(60.0, x_last * 1.05)
        self.axis_x.setRange(0, x_max)

        y_hi = self._y_max * 1.25 + 0.1
        self.axis_y.setRange(0, y_hi)

    def _on_xaxis_changed(self, _index: int):
        self._replot()

    def on_sample(self, sample: Sample):
        # Friendly fixed-width stat line (similar to your CLI)
        dur = self._fmt_duration(sample.t_s)
        stat = (
            f"{dur:>4} ({sample.t_s:6.1f} s) ... "
            f"{sample.v:9.4f}V ... "
            f"{sample.a:9.4f}A ... "
            f"{sample.w:9.3f}W ... "
            f"{sample.ah:12.6f}Ah ... "
            f"{sample.wh:12.6f}Wh"
        )
        self.stat_label.setText(stat)

        self._samples.append(sample)

        # Add new point to chart
        x = self._x_for_sample(sample)
        self.series_v.append(x, sample.v)
        self.series_a.append(x, sample.a)
        self.series_w.append(x, sample.w)

        # Auto-scale Y axis: track the max seen so the axis only grows,
        # preventing older data from going off-screen when values decrease.
        self._y_max = max(self._y_max, sample.v, sample.a, sample.w)
        self._update_axes()


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()


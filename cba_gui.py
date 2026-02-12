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

import csv
import math
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

    def __init__(self, amps: float, cutoff: float, interval_s: float,
                 interface_number: Optional[int] = None, parent=None):
        super().__init__(parent)
        self.amps = float(amps)
        self.cutoff = float(cutoff)
        self.interval_s = float(interval_s)
        self.interface_number = interface_number

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
            if self.interface_number is not None:
                iface = wmr_cba.MpOrLibUsb(self.interface_number)
                self._cba = wmr_cba.CBA4(interface=iface)
            else:
                self._cba = wmr_cba.CBA4()
            sn = self._cba.get_serial_number()
            self.status_signal.emit(f"Connected to CBA-IV (SN: {sn}). Starting test...")

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
        self.setWindowTitle("CBA-IV Battery Analyzer")
        self.resize(1050, 700)

        self.worker: Optional[CbaWorker] = None
        self.t0_wall = time.time()

        # ---------- Device / Mode ----------
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.addItem("Auto (first available)", None)
        self.scan_btn = QtWidgets.QPushButton("Scan")
        self.scan_btn.clicked.connect(self._on_scan_devices)

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("Constant Current Discharge")

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
        self.cutoff_edit.valueChanged.connect(self._on_cutoff_changed)

        # xaxis_combo created later as a floating overlay (needs self as parent)

        self.toggle_btn = QtWidgets.QPushButton("Start")
        self.toggle_btn.clicked.connect(self._on_toggle)
        self._running = False

        self.save_btn = QtWidgets.QPushButton("Save CSV")
        self.save_btn.clicked.connect(self._save_csv)
        self.save_btn.setEnabled(False)

        self.load_btn = QtWidgets.QPushButton("Load CSV")
        self.load_btn.clicked.connect(self._load_csv)

        device_row = QtWidgets.QHBoxLayout()
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(self.scan_btn)

        controls = QtWidgets.QFormLayout()
        controls.addRow("Device:", device_row)
        controls.addRow("Mode:", self.mode_combo)
        controls.addRow("Discharge current:", self.amps_edit)
        controls.addRow("Cutoff voltage:", self.cutoff_edit)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addWidget(self.toggle_btn)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.load_btn)

        controls_box = QtWidgets.QVBoxLayout()
        controls_box.addLayout(controls)
        controls_box.addLayout(btn_row)
        controls_box.addStretch(1)

        controls_widget = QtWidgets.QWidget()
        controls_widget.setLayout(controls_box)

        # ---------- Stats ----------
        stat_font = QtGui.QFont("Monospace", 11)
        self._stat_fields = {}
        stats_group = QtWidgets.QGroupBox("Stats")
        stats_layout = QtWidgets.QFormLayout()
        stats_layout.setHorizontalSpacing(20)
        for key, label in [
            ("duration", "Duration:"),
            ("voltage", "Voltage:"),
            ("current", "Current:"),
            ("power", "Power:"),
            ("ah", "Amp-hours:"),
            ("wh", "Watt-hours:"),
        ]:
            val = QtWidgets.QLabel("--")
            val.setFont(stat_font)
            val.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            stats_layout.addRow(label, val)
            self._stat_fields[key] = val
        stats_group.setLayout(stats_layout)

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

        # Cutoff threshold line (red, dashed)
        self.series_cutoff = QLineSeries()
        self.series_cutoff.setName("Cutoff (V)")
        cutoff_pen = QtGui.QPen(QtGui.QColor("#DC3545"))
        cutoff_pen.setWidth(2)
        cutoff_pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        self.series_cutoff.setPen(cutoff_pen)

        self.chart = QChart()
        self.chart.addSeries(self.series_v)
        self.chart.addSeries(self.series_a)
        self.chart.addSeries(self.series_w)
        self.chart.addSeries(self.series_cutoff)
        self.chart.legend().setVisible(True)
        self.chart.legend().setLabelColor(QtGui.QColor("#E0E0E0"))
        self.chart.setTitle("")
        self.chart.setTitleBrush(QtGui.QBrush(QtGui.QColor("#E0E0E0")))

        # Chart styling: dark plot area
        self.chart.setBackgroundBrush(QtGui.QBrush(QtGui.QColor("#464646")))
        self.chart.setBackgroundPen(QtGui.QPen(QtGui.QColor("#6A6A6A"), 1))
        self.chart.setPlotAreaBackgroundBrush(QtGui.QBrush(QtGui.QColor("#414141")))
        self.chart.setPlotAreaBackgroundVisible(True)

        # Grid line pens
        grid_pen = QtGui.QPen(QtGui.QColor("#585858"))
        grid_pen.setWidth(1)
        minor_pen = QtGui.QPen(QtGui.QColor("#4A4A4A"))
        minor_pen.setWidth(1)
        axis_pen = QtGui.QPen(QtGui.QColor("#999999"), 1)

        label_color = QtGui.QColor("#E0E0E0")
        label_brush = QtGui.QBrush(label_color)

        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("Time (s)")
        self.axis_x.setTitleBrush(label_brush)
        self.axis_x.setLabelsColor(label_color)
        self.axis_x.setRange(0, 60)
        self.axis_x.setGridLineVisible(True)
        self.axis_x.setMinorGridLineVisible(True)
        self.axis_x.setMinorTickCount(1)
        self.axis_x.setGridLinePen(grid_pen)
        self.axis_x.setMinorGridLinePen(minor_pen)
        self.axis_x.setLinePen(axis_pen)

        # Left Y axis: Voltage
        self.axis_y_v = QValueAxis()
        self.axis_y_v.setTitleText("Voltage (V)")
        self.axis_y_v.setTitleBrush(label_brush)
        self.axis_y_v.setLabelsColor(label_color)
        self.axis_y_v.setRange(0, 20)
        self.axis_y_v.setGridLineVisible(True)
        self.axis_y_v.setGridLinePen(grid_pen)
        self.axis_y_v.setMinorGridLineVisible(False)
        self.axis_y_v.setLinePen(axis_pen)

        # Right Y axis: Current and Power
        self.axis_y_aw = QValueAxis()
        self.axis_y_aw.setTitleText("Current (A) / Power (W)")
        self.axis_y_aw.setTitleBrush(label_brush)
        self.axis_y_aw.setLabelsColor(label_color)
        self.axis_y_aw.setRange(0, 10)
        self.axis_y_aw.setGridLineVisible(False)
        self.axis_y_aw.setMinorGridLineVisible(False)
        self.axis_y_aw.setLinePen(axis_pen)

        self.chart.addAxis(self.axis_x, QtCore.Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.axis_y_v, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.chart.addAxis(self.axis_y_aw, QtCore.Qt.AlignmentFlag.AlignRight)

        # Attach voltage + cutoff to left axis
        for s in (self.series_v, self.series_cutoff):
            s.attachAxis(self.axis_x)
            s.attachAxis(self.axis_y_v)

        # Attach current + power to right axis
        for s in (self.series_a, self.series_w):
            s.attachAxis(self.axis_x)
            s.attachAxis(self.axis_y_aw)

        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # ---------- Layout ----------
        left = QtWidgets.QVBoxLayout()
        left.addWidget(controls_widget)
        left.addWidget(stats_group)
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

        # X axis selector floating over the bottom-right corner of the window
        self.xaxis_combo = QtWidgets.QComboBox(self)
        self.xaxis_combo.addItems(["Time (s)", "Amp-hours (Ah)"])
        self.xaxis_combo.currentIndexChanged.connect(self._on_xaxis_changed)
        self._position_xaxis_overlay()

        self._v_min = float('inf')
        self._v_max = 0.0
        self._aw_max = 0.0
        self._time_unit = 's'
        self._time_divisor = 1
        self._samples: list[Sample] = []
        self._reset_series()

    def _reset_series(self):
        self.series_v.clear()
        self.series_a.clear()
        self.series_w.clear()
        self._v_min = float('inf')
        self._v_max = 0.0
        self._aw_max = 0.0
        self._time_unit = 's'
        self._time_divisor = 1
        self._samples = []
        self.save_btn.setEnabled(False)
        self.axis_x.setRange(0, 60)
        self.axis_x.setTickCount(12)
        self.axis_x.applyNiceNumbers()
        self.axis_y_v.setRange(0, 20)
        self.axis_y_v.applyNiceNumbers()
        self.axis_y_aw.setRange(0, 10)
        self.axis_y_aw.applyNiceNumbers()
        self._update_cutoff_line()

    def _update_cutoff_line(self):
        """Redraw the horizontal cutoff threshold line across the full X range."""
        cutoff_v = float(self.cutoff_edit.value())
        x_max = self.axis_x.max()
        self.series_cutoff.clear()
        self.series_cutoff.append(0, cutoff_v)
        self.series_cutoff.append(x_max, cutoff_v)

    def _on_cutoff_changed(self, _value: float):
        self._update_cutoff_line()

    def _on_scan_devices(self):
        self.device_combo.clear()
        self.device_combo.addItem("Auto (first available)", None)
        try:
            serials = wmr_cba.CBA4.scan()
            for idx, sn in enumerate(serials):
                self.device_combo.addItem(f"CBA-IV (SN: {sn})", idx)
            if serials:
                self._append_log(f"Scan: found {len(serials)} device(s).")
                self.device_combo.setCurrentIndex(1)
            else:
                self._append_log("Scan: no CBA devices found.")
        except Exception as e:
            self._append_log(f"Scan error: {e}")

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

    def _set_stats(self, text: str = "--"):
        for val in self._stat_fields.values():
            val.setText(text)

    def _position_xaxis_overlay(self):
        """Position the X-axis selector at the bottom-right edge of the chart area."""
        combo_w = 150
        combo_h = self.xaxis_combo.sizeHint().height()
        self.xaxis_combo.setFixedWidth(combo_w)
        cw = self.centralWidget()
        x_combo = cw.x() + cw.width() - combo_w - 6
        y = cw.y() + cw.height() - combo_h - 6
        self.xaxis_combo.move(x_combo, y)
        self.xaxis_combo.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_xaxis_overlay()

    def closeEvent(self, event):
        if self.worker is not None:
            self.worker.request_stop()
            self.worker.wait(3000)
        event.accept()

    def _on_toggle(self):
        if self._running:
            self.on_stop()
        else:
            self.on_start()

    def on_start(self):
        if self.worker is not None:
            return

        amps = float(self.amps_edit.value())
        cutoff = float(self.cutoff_edit.value())

        self._reset_series()
        self._append_log(f"Starting: {amps:.3f}A cutoff {cutoff:.3f}V")
        self._set_stats("Starting...")

        self._running = True
        self.toggle_btn.setText("Stop")
        self.device_combo.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.mode_combo.setEnabled(False)
        self.amps_edit.setEnabled(False)
        self.cutoff_edit.setEnabled(False)

        interface_number = self.device_combo.currentData()
        self.worker = CbaWorker(amps=amps, cutoff=cutoff, interval_s=1.0,
                                interface_number=interface_number)
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

        # cleanup worker
        if self.worker is not None:
            try:
                self.worker.wait(2000)
            except Exception:
                pass
        self.worker = None

        self._running = False
        self.toggle_btn.setText("Start")
        self.device_combo.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.amps_edit.setEnabled(True)
        self.cutoff_edit.setEnabled(True)

    def _x_for_sample(self, sample: Sample) -> float:
        if self.xaxis_combo.currentIndex() == 1:
            return sample.ah
        return sample.t_s / self._time_divisor

    def _replot(self):
        """Re-plot all stored samples using the current X-axis mode."""
        pts_v = []
        pts_a = []
        pts_w = []
        for s in self._samples:
            x = self._x_for_sample(s)
            pts_v.append(QtCore.QPointF(x, s.v))
            pts_a.append(QtCore.QPointF(x, s.a))
            pts_w.append(QtCore.QPointF(x, s.w))
        self.series_v.replace(pts_v)
        self.series_a.replace(pts_a)
        self.series_w.replace(pts_w)
        self._update_axes()

    def _update_axes(self):
        if self.xaxis_combo.currentIndex() == 1:
            self.axis_x.setTitleText("Amp-hours (Ah)")
            x_last = self._samples[-1].ah if self._samples else 0
        else:
            # Auto-scale time unit based on test duration
            t_max = self._samples[-1].t_s if self._samples else 0
            if t_max >= 10800:   # >= 180 min -> hours
                unit, divisor, label = 'hr', 3600, 'Time (hr)'
            elif t_max >= 180:   # >= 3 min -> minutes
                unit, divisor, label = 'min', 60, 'Time (min)'
            else:
                unit, divisor, label = 's', 1, 'Time (s)'

            if unit != self._time_unit:
                self._time_unit = unit
                self._time_divisor = divisor
                # Reset axis range so the replot uses the new scale,
                # not the stale range from the old unit.
                self.axis_x.setRange(0, 1)
                self._replot()
                return

            self.axis_x.setTitleText(label)
            x_last = t_max / divisor

        # Rescale X axis when data approaches the current edge (>90%) or
        # when the axis is much too wide for the data (<30%, e.g. after
        # switching to Ah mode with a small-capacity battery).
        current_x_max = self.axis_x.max()
        needs_grow = x_last > current_x_max * 0.90
        needs_shrink = x_last > 0 and x_last < current_x_max * 0.30
        if needs_grow or needs_shrink:
            new_x_max = x_last * 1.25
            if self.xaxis_combo.currentIndex() == 1:
                new_x_max = max(0.001, new_x_max)
            elif self._time_unit == 's':
                new_x_max = max(60.0, new_x_max)
            else:
                new_x_max = max(1.0, new_x_max)
            self.axis_x.setRange(0, new_x_max)
            self.axis_x.setTickCount(12)
            self.axis_x.applyNiceNumbers()

        # Voltage axis (left): pick finest nice interval that keeps ticks <= 16
        if self._samples and self._v_min <= self._v_max:
            cutoff = float(self.cutoff_edit.value())
            raw_min = min(self._v_min, cutoff)
            raw_max = self._v_max
            chosen = None
            for iv in (0.1, 0.2, 0.5, 1, 2, 5):
                # Subtract one interval so the cutoff line isn't pinned to the bottom edge
                v_lo = math.floor(raw_min / iv) * iv - iv
                v_hi = math.ceil(raw_max / iv) * iv
                if v_hi <= v_lo:
                    v_hi += iv
                n_ticks = round((v_hi - v_lo) / iv) + 1
                if n_ticks <= 16:
                    chosen = (v_lo, v_hi, n_ticks)
                    break
            if chosen:
                self.axis_y_v.setRange(chosen[0], chosen[1])
                self.axis_y_v.setTickCount(chosen[2])
            else:
                self.axis_y_v.setRange(math.floor(raw_min), math.ceil(raw_max))
                self.axis_y_v.applyNiceNumbers()

        # Current/Power axis (right) — match tick count to left axis
        # so labels align with the voltage grid lines.
        v_ticks = self.axis_y_v.tickCount()
        aw_hi = max(1.0, self._aw_max) * 1.25 + 0.1
        self.axis_y_aw.setRange(0, aw_hi)
        self.axis_y_aw.setTickCount(v_ticks)
        self.axis_y_aw.applyNiceNumbers()
        # applyNiceNumbers may change the tick count; force it back
        self.axis_y_aw.setTickCount(v_ticks)

        self._update_cutoff_line()

    def _on_xaxis_changed(self, _index: int):
        self.axis_x.setRange(0, 1)
        self._replot()

    def on_sample(self, sample: Sample):
        # Update individual stat fields
        dur = self._fmt_duration(sample.t_s)
        self._stat_fields["duration"].setText(f"{dur} ({sample.t_s:.1f} s)")
        self._stat_fields["voltage"].setText(f"{sample.v:.4f} V")
        self._stat_fields["current"].setText(f"{sample.a:.4f} A")
        self._stat_fields["power"].setText(f"{sample.w:.3f} W")
        self._stat_fields["ah"].setText(f"{sample.ah:.6f} Ah")
        self._stat_fields["wh"].setText(f"{sample.wh:.6f} Wh")

        self._samples.append(sample)
        self.save_btn.setEnabled(True)

        # Add new point to chart
        x = self._x_for_sample(sample)
        self.series_v.append(x, sample.v)
        self.series_a.append(x, sample.a)
        self.series_w.append(x, sample.w)

        # Auto-scale Y axes independently
        self._v_min = min(self._v_min, sample.v)
        self._v_max = max(self._v_max, sample.v)
        self._aw_max = max(self._aw_max, sample.a, sample.w)
        self._update_axes()


    def _save_csv(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save CSV", "", "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("t(s),voltage(V),current(A),power(W),amp_hours(Ah),watt_hours(Wh)\n")
                for s in self._samples:
                    f.write(f"{s.t_s:.0f},{s.v:.4f},{s.a:.4f},{s.w:.3f},{s.ah:.6f},{s.wh:.6f}\n")
            self._append_log(f"Saved {len(self._samples)} samples to {path}")
        except Exception as e:
            self._append_log(f"Save failed: {e}")

    def _load_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load CSV", "", "CSV files (*.csv)")
        if not path:
            return
        try:
            samples = []
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    samples.append(Sample(
                        t_s=float(row["t(s)"]),
                        v=float(row["voltage(V)"]),
                        a=float(row["current(A)"]),
                        w=float(row["power(W)"]),
                        ah=float(row["amp_hours(Ah)"]),
                        wh=float(row["watt_hours(Wh)"]),
                    ))
            if not samples:
                self._append_log(f"No data rows in {path}")
                return

            self._reset_series()
            self._samples = samples
            self.save_btn.setEnabled(True)

            for s in self._samples:
                self._v_min = min(self._v_min, s.v)
                self._v_max = max(self._v_max, s.v)
                self._aw_max = max(self._aw_max, s.a, s.w)

            self._replot()

            last = self._samples[-1]
            dur = self._fmt_duration(last.t_s)
            self._stat_fields["duration"].setText(f"{dur} ({last.t_s:.1f} s)")
            self._stat_fields["voltage"].setText(f"{last.v:.4f} V")
            self._stat_fields["current"].setText(f"{last.a:.4f} A")
            self._stat_fields["power"].setText(f"{last.w:.3f} W")
            self._stat_fields["ah"].setText(f"{last.ah:.6f} Ah")
            self._stat_fields["wh"].setText(f"{last.wh:.6f} Wh")

            self._append_log(f"Loaded {len(samples)} samples from {path}")
        except Exception as e:
            self._append_log(f"Load failed: {e}")


def main():
    app = QtWidgets.QApplication(sys.argv)

    # Dark palette for the entire application
    palette = QtGui.QPalette()
    dark = QtGui.QColor("#464646")
    mid = QtGui.QColor("#595959")
    light_text = QtGui.QColor("#E0E0E0")
    highlight = QtGui.QColor("#2A82DA")
    palette.setColor(QtGui.QPalette.ColorRole.Window, dark)
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, light_text)
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("#414141"))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, mid)
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, mid)
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, light_text)
    palette.setColor(QtGui.QPalette.ColorRole.Text, light_text)
    palette.setColor(QtGui.QPalette.ColorRole.Button, mid)
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, light_text)
    palette.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor("#FF4444"))
    palette.setColor(QtGui.QPalette.ColorRole.Link, highlight)
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#FFFFFF"))
    palette.setColor(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("#808080"))
    palette.setColor(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.Text, QtGui.QColor("#808080"))
    palette.setColor(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor("#808080"))
    app.setPalette(palette)

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

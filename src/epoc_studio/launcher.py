"""EPOC-X Studio — Unified Desktop Launcher & Stream Hub.

Coordinates a single BLE headset connection (or CSV replay), shares the live
CSV stream both in-process and over a local TCP broker so Headmap, Live
Waveforms, and the 3D Brain Map all run inside native desktop windows owned by
this application, while keeping every CLI tool usable standalone.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from epoc_direct import devices as dev_store
from epoc_direct.broker import StreamController, _extract_csv_duration_ms
from epoc_direct.paths import get_keys_path, get_recordings_dir

from .brain_map_view import BrainDSP, create_brain_map_window, parse_csv_lines_for_dsp
from .csv_compare_view import create_csv_compare_window
from .headmap_view import HeadmapWindow
from .live_viewer_view import create_viewer_window, parse_duration
from .manual_view import create_manual_window
from .qt_common import (
    apply_dark_titlebar,
    ensure_qt_app,
    ensure_stdio,
    get_app_icon,
    install_sigint_handler,
    install_windows_shortcuts,
)

BRAIN_PORT = 8080
HAS_PYLSL = importlib.util.find_spec("pylsl") is not None

APP_QSS = """
QMainWindow, QDialog, QWidget {
    background-color: #080d18;
    color: #e2e8f0;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 13px;
}
QFrame#HeaderBar {
    background-color: #0f172a;
    border-bottom: 1px solid #1e293b;
}
QFrame.Card {
    background-color: #0f172a;
    border: 1px solid #1e293b;
    border-radius: 10px;
}
QFrame.ModuleRow {
    background-color: #131d31;
    border: 1px solid #1e293b;
    border-radius: 8px;
}
QLabel.CardTitle {
    color: #38bdf8;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
}
QLabel.Muted {
    color: #94a3b8;
    font-size: 12px;
}
QLabel.MetricCaption {
    color: #64748b;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.6px;
}
QLabel.MetricValue {
    color: #e2e8f0;
    font-family: Consolas, "Courier New", monospace;
    font-size: 12px;
    font-weight: 700;
}
QComboBox, QLineEdit, QPlainTextEdit {
    background-color: #162033;
    color: #e2e8f0;
    border: 1px solid #2b3d5c;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 20px;
    selection-background-color: #0284c7;
}
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus {
    border-color: #38bdf8;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #0f172a;
    color: #e2e8f0;
    border: 1px solid #2b3d5c;
    selection-background-color: #1e2d47;
}
QPushButton {
    background-color: #162033;
    color: #e2e8f0;
    border: 1px solid #2b3d5c;
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 20px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #1e2d47;
    border-color: #38bdf8;
}
QPushButton:disabled {
    background-color: #0f172a;
    color: #475569;
    border-color: #1e293b;
}
QPushButton#PrimaryBtn {
    background-color: #0284c7;
    color: #f8fafc;
    border: 1px solid #38bdf8;
}
QPushButton#PrimaryBtn:hover {
    background-color: #0369a1;
}
QPushButton#DangerBtn {
    background-color: rgba(239, 68, 68, 0.16);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.55);
}
QPushButton#DangerBtn:hover {
    background-color: rgba(239, 68, 68, 0.28);
}
QPushButton#RecordActiveBtn {
    background-color: rgba(239, 68, 68, 0.22);
    color: #fecaca;
    border: 1px solid #ef4444;
}
QPushButton#RecordActiveBtn:hover {
    background-color: rgba(239, 68, 68, 0.34);
}
QPushButton#PauseActiveBtn {
    background-color: rgba(234, 179, 8, 0.18);
    color: #facc15;
    border: 1px solid rgba(234, 179, 8, 0.6);
}
QPushButton.ModeTab {
    background-color: #131d31;
    color: #94a3b8;
    border: 1px solid #1e293b;
    padding: 6px 14px;
}
QPushButton.ModeTab[active="true"] {
    background-color: rgba(56, 189, 248, 0.16);
    color: #38bdf8;
    border: 1px solid #38bdf8;
}
QCheckBox {
    color: #cbd5e1;
    spacing: 7px;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid #334155;
    background-color: #162033;
}
QCheckBox::indicator:checked {
    background-color: #0284c7;
    border-color: #38bdf8;
}
QScrollArea {
    background-color: #080d18;
    border: none;
}
QScrollBar:vertical {
    background: #080d18;
    width: 9px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #1e293b;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #334155;
}
QScrollBar:horizontal {
    background: #080d18;
    height: 9px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: #1e293b;
    border-radius: 4px;
    min-width: 28px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    height: 0px;
    width: 0px;
}
"""


class BridgeSignals(QtCore.QObject):
    status_changed = QtCore.pyqtSignal(str, str)
    log_added = QtCore.pyqtSignal(str)
    ui_call_requested = QtCore.pyqtSignal(object)


DEVICE_MANAGER_QSS = """
QDialog#DeviceManagerDialog QLabel {
    background-color: transparent;
}
QDialog#DeviceManagerDialog QFrame.Card {
    background-color: #080d18;
    border: 1px solid #1e293b;
    border-radius: 10px;
}
QDialog#DeviceManagerDialog QLineEdit {
    background-color: #080d18;
    color: #e2e8f0;
    border: 1px solid #2b3d5c;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 20px;
}
QDialog#DeviceManagerDialog QLineEdit:focus {
    border-color: #38bdf8;
}
QDialog#DeviceManagerDialog QPushButton {
    background-color: #080d18;
    color: #e2e8f0;
    border: 1px solid #2b3d5c;
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 20px;
    font-weight: 600;
}
QDialog#DeviceManagerDialog QPushButton:hover {
    background-color: #1e2d47;
    border-color: #38bdf8;
}
QDialog#DeviceManagerDialog QPushButton:disabled {
    background-color: #0f172a;
    color: #475569;
    border-color: #1e293b;
}
QDialog#DeviceManagerDialog QPushButton#PrimaryBtn {
    background-color: #080d18;
    color: #f8fafc;
    border: 1px solid #38bdf8;
}
QDialog#DeviceManagerDialog QPushButton#PrimaryBtn:hover {
    background-color: #132a42;
}
QDialog#DeviceManagerDialog QPushButton#DangerBtn {
    background-color: #080d18;
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.55);
}
QDialog#DeviceManagerDialog QPushButton#DangerBtn:hover {
    background-color: rgba(239, 68, 68, 0.16);
}
QDialog#DeviceManagerDialog QPushButton#DangerBtn:disabled {
    background-color: #0f172a;
    color: #475569;
    border-color: #1e293b;
}
QDialog#DeviceManagerDialog QListWidget {
    background-color: transparent;
    border: none;
    outline: 0;
}
"""


class _DeviceListDelegate(QtWidgets.QStyledItemDelegate):
    """Paints device profiles as compact Studio ModuleRow cards."""

    _ADDR_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 54)

    def paint(self, painter: QtGui.QPainter, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex):
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        rect = QtCore.QRectF(option.rect).adjusted(1.0, 2.0, -1.0, -2.0)
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)

        if selected:
            bg = QtGui.QColor(56, 189, 248, 28)
            border = QtGui.QColor("#38bdf8")
            title_color = QtGui.QColor("#38bdf8")
        elif hovered:
            bg = QtGui.QColor("#0f172a")
            border = QtGui.QColor("#2b3d5c")
            title_color = QtGui.QColor("#f8fafc")
        else:
            bg = QtGui.QColor("#080d18")
            border = QtGui.QColor("#1e293b")
            title_color = QtGui.QColor("#f8fafc")

        painter.setBrush(bg)
        painter.setPen(QtGui.QPen(border, 1.0))
        painter.drawRoundedRect(rect, 8.0, 8.0)

        title = index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
        subtitle = index.data(self._ADDR_ROLE) or ""

        title_font = QtGui.QFont(option.font)
        title_font.setPixelSize(13)
        title_font.setBold(True)

        sub_font = QtGui.QFont(option.font)
        sub_font.setPixelSize(11)

        inner = rect.adjusted(10.0, 7.0, -10.0, -7.0)
        title_rect = QtCore.QRectF(inner.left(), inner.top(), inner.width(), 19.0)
        sub_rect = QtCore.QRectF(inner.left(), inner.top() + 20.0, inner.width(), 16.0)

        painter.setFont(title_font)
        painter.setPen(title_color)
        fm_title = QtGui.QFontMetrics(title_font)
        painter.drawText(
            title_rect,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            fm_title.elidedText(title, QtCore.Qt.TextElideMode.ElideRight, int(title_rect.width())),
        )

        painter.setFont(sub_font)
        painter.setPen(QtGui.QColor("#94a3b8"))
        fm_sub = QtGui.QFontMetrics(sub_font)
        painter.drawText(
            sub_rect,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            fm_sub.elidedText(subtitle, QtCore.Qt.TextElideMode.ElideRight, int(sub_rect.width())),
        )

        painter.restore()


class DeviceManagerDialog(QtWidgets.QDialog):
    """Manage saved EPOC-X device keys and whitening bytes in KEYS.txt."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DeviceManagerDialog")
        self.setStyleSheet(APP_QSS + DEVICE_MANAGER_QSS)
        self.setWindowTitle("Manage EPOC-X Devices (KEYS.txt)")
        icon = get_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(840, 470)
        self.setMinimumSize(640, 400)
        self.keys_path = get_keys_path()
        self.devices = dev_store.load_devices(str(self.keys_path))

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Studio-style top header bar
        header = QtWidgets.QFrame()
        header.setObjectName("HeaderBar")
        header.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        h_lay = QtWidgets.QHBoxLayout(header)
        h_lay.setContentsMargins(16, 8, 16, 8)
        h_lay.setSpacing(24)

        self.count_lbl = self._add_metric(h_lay, "PROFILES:", str(len(self.devices)), "#38bdf8", min_width=24)
        self.store_lbl = self._add_metric(h_lay, "STORE:", self.keys_path.name, "#e2e8f0")
        self.store_lbl.setToolTip(str(self.keys_path))

        h_lay.addStretch(1)

        self.status_pill = QtWidgets.QFrame()
        self.status_pill.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.status_pill.setStyleSheet(
            "QFrame { background: #162033; border: 1px solid #2b3d5c; border-radius: 14px; }"
        )
        pill_lay = QtWidgets.QHBoxLayout(self.status_pill)
        pill_lay.setContentsMargins(11, 5, 11, 5)
        pill_lay.setSpacing(7)
        self.status_dot = QtWidgets.QLabel()
        self.status_dot.setFixedSize(9, 9)
        self.status_text = QtWidgets.QLabel("Saved Profile")
        self.status_text.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #e2e8f0; border: none; background: transparent;"
        )
        pill_lay.addWidget(self.status_dot)
        pill_lay.addWidget(self.status_text)
        h_lay.addWidget(self.status_pill)
        root.addWidget(header)

        # Main body matching Studio margins and card grid
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(10)

        # Left card: Saved Devices
        list_card = QtWidgets.QFrame()
        list_card.setProperty("class", "Card")
        list_lay = QtWidgets.QVBoxLayout(list_card)
        list_lay.setContentsMargins(14, 11, 14, 11)
        list_lay.setSpacing(8)

        list_hdr = QtWidgets.QHBoxLayout()
        list_title = QtWidgets.QLabel("Saved Devices")
        list_title.setProperty("class", "CardTitle")
        list_hdr.addWidget(list_title)
        list_hdr.addStretch(1)
        list_lay.addLayout(list_hdr)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.list_widget.setMouseTracking(True)
        self.list_widget.setItemDelegate(_DeviceListDelegate(self.list_widget))
        self.list_widget.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.currentRowChanged.connect(self._on_select)
        list_lay.addWidget(self.list_widget, 1)
        list_card.setMinimumWidth(220)
        body.addWidget(list_card, 4)

        # Right card: Device Profile editor
        form_card = QtWidgets.QFrame()
        form_card.setProperty("class", "Card")
        form_outer = QtWidgets.QVBoxLayout(form_card)
        form_outer.setContentsMargins(14, 11, 14, 11)
        form_outer.setSpacing(10)

        form_hdr = QtWidgets.QHBoxLayout()
        form_title = QtWidgets.QLabel("Device Profile")
        form_title.setProperty("class", "CardTitle")
        self.profile_meta_lbl = QtWidgets.QLabel("")
        self.profile_meta_lbl.setStyleSheet(
            "color: #94a3b8; font-family: Consolas, monospace; font-size: 12px; background: transparent;"
        )
        form_hdr.addWidget(form_title)
        form_hdr.addStretch(1)
        form_hdr.addWidget(self.profile_meta_lbl)
        form_outer.addLayout(form_hdr)

        form_lay = QtWidgets.QFormLayout()
        form_lay.setContentsMargins(0, 2, 0, 0)
        form_lay.setHorizontalSpacing(12)
        form_lay.setVerticalSpacing(9)
        form_lay.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("EPOC-X")
        self.addr_edit = QtWidgets.QLineEdit()
        self.addr_edit.setPlaceholderText("(auto)")
        self.serial_edit = QtWidgets.QLineEdit()
        self.serial_edit.setPlaceholderText("?")
        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setFont(QtGui.QFont("Consolas", 9))
        self.key_edit.setPlaceholderText("32 or 64 hex characters...")
        self.whiten_edit = QtWidgets.QLineEdit()
        self.whiten_edit.setFont(QtGui.QFont("Consolas", 9))
        self.whiten_edit.setPlaceholderText("0x14")

        form_lay.addRow("BT Name:", self.name_edit)
        form_lay.addRow("BT Address:", self.addr_edit)
        form_lay.addRow("Serial:", self.serial_edit)
        form_lay.addRow("AES-256 Key:", self.key_edit)
        form_lay.addRow("Whitening Byte:", self.whiten_edit)
        form_outer.addLayout(form_lay)

        form_outer.addStretch(1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        self.new_btn = QtWidgets.QPushButton("New")
        self.save_btn = QtWidgets.QPushButton("Save / Update")
        self.save_btn.setObjectName("PrimaryBtn")
        self.del_btn = QtWidgets.QPushButton("Delete")
        self.del_btn.setObjectName("DangerBtn")
        btn_row.addWidget(self.new_btn)
        btn_row.addWidget(self.del_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.save_btn)
        form_outer.addLayout(btn_row)

        body.addWidget(form_card, 9)
        layout.addLayout(body, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(12)
        hint = QtWidgets.QLabel(
            f"Stored in {self.keys_path}. Leave BT address as (auto) to discover the first EPOC-X."
        )
        hint.setWordWrap(True)
        hint.setProperty("class", "Muted")
        footer.addWidget(hint, 1)

        close_btn = QtWidgets.QPushButton("Done")
        close_btn.setMinimumWidth(84)
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(footer)
        root.addLayout(layout, 1)

        self.new_btn.clicked.connect(self._new_blank)
        self.save_btn.clicked.connect(self._save_current)
        self.del_btn.clicked.connect(self._delete_current)

        self._refresh_list(select_idx=0 if self.devices else -1)

    @staticmethod
    def _add_metric(
        parent_layout: QtWidgets.QHBoxLayout,
        caption: str,
        initial: str,
        color: str,
        min_width: int = 0,
    ) -> QtWidgets.QLabel:
        box = QtWidgets.QWidget()
        box.setStyleSheet("border: none; background: transparent;")
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        cap = QtWidgets.QLabel(caption)
        cap.setStyleSheet(
            "color: #64748b; font-size: 10px; font-weight: 700; "
            "letter-spacing: 0.6px; border: none; background: transparent;"
        )
        val = QtWidgets.QLabel(initial)
        if min_width > 0:
            val.setMinimumWidth(min_width)
        val.setStyleSheet(
            f"color: {color}; font-family: Consolas, 'Courier New', monospace; "
            "font-size: 12px; font-weight: 700; border: none; background: transparent;"
        )
        lay.addWidget(cap)
        lay.addWidget(val)
        parent_layout.addWidget(box)
        return val

    def _update_header_state(self, row: int):
        total = len(self.devices)
        self.count_lbl.setText(str(total))
        if 0 <= row < total:
            self.status_dot.setStyleSheet("background: #22c55e; border-radius: 4px; border: none;")
            self.status_text.setText("Saved Profile")
            self.profile_meta_lbl.setText(f"Profile {row + 1} of {total}")
            self.del_btn.setEnabled(True)
        elif total == 0:
            self.status_dot.setStyleSheet("background: #64748b; border-radius: 4px; border: none;")
            self.status_text.setText("No Saved Devices")
            self.profile_meta_lbl.setText("New profile")
            self.del_btn.setEnabled(False)
        else:
            self.status_dot.setStyleSheet("background: #38bdf8; border-radius: 4px; border: none;")
            self.status_text.setText("New Profile")
            self.profile_meta_lbl.setText("Unsaved new profile")
            self.del_btn.setEnabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        apply_dark_titlebar(self)

    def _refresh_list(self, select_idx: int = -1):
        self.list_widget.clear()
        for d in self.devices:
            addr = d.address if d.address and d.address not in ("?", "(auto)") else "auto-discover"
            item = QtWidgets.QListWidgetItem(d.name or "EPOC-X")
            item.setData(_DeviceListDelegate._ADDR_ROLE, f"{addr}  ·  W=0x{d.whiten:02x}")
            self.list_widget.addItem(item)
        if 0 <= select_idx < len(self.devices):
            self.list_widget.setCurrentRow(select_idx)
            self._update_header_state(select_idx)
        elif not self.devices:
            self._new_blank()
        else:
            self._update_header_state(self.list_widget.currentRow())

    def _on_select(self, row: int):
        if 0 <= row < len(self.devices):
            d = self.devices[row]
            self.name_edit.setText(d.name)
            self.addr_edit.setText(d.address)
            self.serial_edit.setText(d.serial)
            self.key_edit.setText(d.aes_key)
            self.key_edit.setCursorPosition(0)
            self.whiten_edit.setText(f"0x{d.whiten:02x}")
        self._update_header_state(row)

    def _new_blank(self):
        self.list_widget.clearSelection()
        self.list_widget.setCurrentRow(-1)
        self.name_edit.setText("EPOC-X")
        self.addr_edit.setText("(auto)")
        self.serial_edit.setText("?")
        self.key_edit.clear()
        self.whiten_edit.setText("0x14")
        self._update_header_state(-1)

    def _save_current(self):
        key_hex = self.key_edit.text().strip().replace(" ", "").replace(":", "").lower()
        try:
            raw = bytes.fromhex(key_hex)
            if len(raw) not in (16, 32):
                raise ValueError(f"Expected 16 or 32 bytes (32 or 64 hex chars), got {len(raw)} bytes.")
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Invalid AES Key", str(e))
            return

        try:
            whiten = int(self.whiten_edit.text().strip() or "0x55", 0)
            if not (0 <= whiten <= 255):
                raise ValueError("Whitening byte must be 0x00..0xFF.")
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Invalid Whitening Byte", str(e))
            return

        prof = dev_store.DeviceProfile(
            name=self.name_edit.text().strip() or "EPOC-X",
            address=self.addr_edit.text().strip() or "(auto)",
            serial=self.serial_edit.text().strip() or "?",
            aes_key=key_hex,
            whiten=whiten,
        )
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.devices):
            self.devices[row] = prof
            dev_store.save_devices(self.devices, str(self.keys_path))
            self._refresh_list(select_idx=row)
        else:
            self.devices.append(prof)
            dev_store.save_devices(self.devices, str(self.keys_path))
            self._refresh_list(select_idx=len(self.devices) - 1)

    def _delete_current(self):
        row = self.list_widget.currentRow()
        if not (0 <= row < len(self.devices)):
            return
        if len(self.devices) == 1:
            ans = QtWidgets.QMessageBox.question(
                self,
                "Delete Last Device",
                f"Delete the only saved device profile in {self.keys_path.name}?",
            )
            if ans != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.devices.pop(row)
        dev_store.save_devices(self.devices, str(self.keys_path))
        self._refresh_list(select_idx=min(row, len(self.devices) - 1))


class ReplayProgressBar(QtWidgets.QWidget):
    """Simple flat click-to-seek replay progress bar matching the Brain Map style without glow."""

    seekPressed = QtCore.pyqtSignal(float)
    seekMoved = QtCore.pyqtSignal(float)
    seekReleased = QtCore.pyqtSignal(float)

    _PAD_X = 8
    _TRACK_H = 6

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._progress = 0.0
        self._dragging = False
        self._hovered = False
        self.setFixedHeight(28)
        self.setMinimumWidth(90)
        self.setMouseTracking(True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def progress(self) -> float:
        return self._progress

    def setProgress(self, ratio: float) -> None:
        clamped = max(0.0, min(1.0, float(ratio)))
        if abs(clamped - self._progress) > 1e-6:
            self._progress = clamped
            self.update()

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.setCursor(
            QtCore.Qt.CursorShape.PointingHandCursor
            if enabled
            else QtCore.Qt.CursorShape.ArrowCursor
        )
        if not enabled:
            self._dragging = False
            self._hovered = False
        self.update()

    def _ratio_from_x(self, x: float) -> float:
        span = max(1.0, float(self.width() - 2 * self._PAD_X))
        return max(0.0, min(1.0, (float(x) - self._PAD_X) / span))

    def enterEvent(self, event: QtGui.QEnterEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.isEnabled():
            self._dragging = True
            ratio = self._ratio_from_x(event.position().x())
            self._progress = ratio
            self.update()
            self.seekPressed.emit(ratio)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging and self.isEnabled():
            ratio = self._ratio_from_x(event.position().x())
            self._progress = ratio
            self.update()
            self.seekMoved.emit(ratio)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            ratio = self._ratio_from_x(event.position().x())
            self._progress = ratio
            self.update()
            self.seekReleased.emit(ratio)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        track_w = max(1.0, w - 2.0 * self._PAD_X)
        track_x = float(self._PAD_X)
        cy = h * 0.5
        track_y = cy - self._TRACK_H * 0.5
        radius = self._TRACK_H * 0.5

        enabled = self.isEnabled()
        track_color = QtGui.QColor("#1e293b") if enabled else QtGui.QColor("#162033")
        fill_color = QtGui.QColor("#38bdf8") if enabled else QtGui.QColor("#334155")
        thumb_fill = QtGui.QColor("#f8fafc") if enabled else QtGui.QColor("#64748b")
        thumb_border = QtGui.QColor("#38bdf8") if enabled else QtGui.QColor("#334155")

        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(track_color)
        p.drawRoundedRect(QtCore.QRectF(track_x, track_y, track_w, self._TRACK_H), radius, radius)

        fill_w = track_w * self._progress
        if fill_w > 0.5:
            p.setBrush(fill_color)
            p.drawRoundedRect(QtCore.QRectF(track_x, track_y, fill_w, self._TRACK_H), radius, radius)

        thumb_r = 7.0 if (enabled and (self._hovered or self._dragging)) else 6.0
        thumb_x = track_x + fill_w
        p.setPen(QtGui.QPen(thumb_border, 2.0))
        p.setBrush(thumb_fill)
        p.drawEllipse(QtCore.QPointF(thumb_x, cy), thumb_r, thumb_r)


class LauncherWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(APP_QSS)
        self.setWindowTitle("EPOC-X Studio")
        icon = get_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(880, 780)
        self.setMinimumSize(460, 360)

        self._closing = False
        self.signals = BridgeSignals()
        self.signals.status_changed.connect(self._on_status_changed)
        self.signals.log_added.connect(self._append_log)
        self.signals.ui_call_requested.connect(self._execute_ui_call)

        self.controller = StreamController(
            on_status=lambda s, m: self.signals.status_changed.emit(s, m),
            on_log=lambda m: self.signals.log_added.emit(m),
        )

        self.windows: dict[str, Optional[QtWidgets.QWidget]] = {
            "headmap": None,
            "live_viewer": None,
            "brain_map": None,
            "csv_compare": None,
            "manual": None,
        }
        self._window_streams: dict[str, object] = {}
        self._window_pause_listeners: dict[str, object] = {}
        self._window_state_listeners: dict[str, object] = {}
        self._window_replay_listeners: dict[str, object] = {}
        self._studio_scrub_dragging = False
        self._pending_replay_sec = 0.0
        self._last_seek_sent_at = 0.0
        self._last_seek_sent_ratio: Optional[float] = None
        self._csv_duration_cache: Optional[tuple[str, int, int, Optional[float]]] = None
        self._source_mode = "live"  # live | replay
        self._note_start_ns: Optional[int] = None

        self._build_ui()
        self._load_devices()
        self._refresh_recording_placeholder()

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self._refresh_runtime_state)
        self._poll_timer.start(250)

        self._append_log(f"Stream broker listening on {self.controller.connect_addr}")

    def showEvent(self, event):
        super().showEvent(event)
        apply_dark_titlebar(self)

    # --- UI construction ---

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header Bar (pinned at top)
        header = QtWidgets.QFrame()
        header.setObjectName("HeaderBar")
        header.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        h_lay = QtWidgets.QHBoxLayout(header)
        h_lay.setContentsMargins(16, 8, 16, 8)
        h_lay.setSpacing(24)

        self.rate_lbl = self._add_metric(h_lay, "RATE:", "0.0 Hz", "#38bdf8", min_width=58)
        self.samples_lbl = self._add_metric(h_lay, "SAMPLES:", "0", "#e2e8f0", min_width=42)
        self.clients_lbl = self._add_metric(h_lay, "ATTACHED:", "0", "#94a3b8", min_width=18)

        h_lay.addStretch(1)

        right_box = QtWidgets.QWidget()
        right_box.setStyleSheet("background: transparent;")
        right_lay = QtWidgets.QHBoxLayout(right_box)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(8)

        self.status_pill = QtWidgets.QFrame()
        self.status_pill.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.status_pill.setStyleSheet(
            "QFrame { background: #162033; border: 1px solid #2b3d5c; border-radius: 14px; }"
        )
        pill_lay = QtWidgets.QHBoxLayout(self.status_pill)
        pill_lay.setContentsMargins(11, 5, 11, 5)
        pill_lay.setSpacing(7)
        self.status_dot = QtWidgets.QLabel()
        self.status_dot.setFixedSize(9, 9)
        self.status_text = QtWidgets.QLabel("Disconnected")
        self.status_text.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #e2e8f0; border: none; background: transparent;"
        )
        pill_lay.addWidget(self.status_dot)
        pill_lay.addWidget(self.status_text)
        right_lay.addWidget(self.status_pill)
        self._set_status_visual("disconnected", "Disconnected")

        self.help_btn = QtWidgets.QPushButton("?")
        self.help_btn.setFixedSize(28, 28)
        self.help_btn.setToolTip("Open User Manual")
        self.help_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.help_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.help_btn.setStyleSheet(
            "QPushButton { background: #162033; border: 1px solid #2b3d5c; border-radius: 14px; "
            "color: #94a3b8; font-size: 13px; font-weight: 700; padding: 0px; }"
            "QPushButton:hover { background: #1d2a42; border-color: #38bdf8; color: #e2e8f0; }"
            "QPushButton:pressed { background: #0f172a; }"
        )
        self.help_btn.clicked.connect(lambda: self._launch_module("manual"))
        right_lay.addWidget(self.help_btn)

        h_lay.addWidget(right_box)

        root.addWidget(header)

        # Scrollable main body
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        scroll_canvas = QtWidgets.QWidget()
        scroll_canvas.setStyleSheet("background-color: #080d18;")
        canvas_lay = QtWidgets.QHBoxLayout(scroll_canvas)
        canvas_lay.setContentsMargins(0, 0, 0, 0)
        canvas_lay.setSpacing(0)

        body_wrap = QtWidgets.QWidget()
        body_wrap.setStyleSheet("background-color: #080d18;")
        body_wrap.setMaximumWidth(1140)
        body_wrap.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        body = QtWidgets.QVBoxLayout(body_wrap)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(10)

        body.addWidget(self._build_source_card())
        body.addWidget(self._build_modules_card())
        body.addWidget(self._build_recording_card())
        body.addWidget(self._build_log_card(), 1)

        canvas_lay.addStretch(1)
        canvas_lay.addWidget(body_wrap, 100)
        canvas_lay.addStretch(1)

        scroll.setWidget(scroll_canvas)
        root.addWidget(scroll, 1)

    def _add_metric(
        self,
        parent_layout,
        caption: str,
        initial: str,
        color: str,
        min_width: int = 0,
    ) -> QtWidgets.QLabel:
        box = QtWidgets.QWidget()
        box.setStyleSheet("border: none; background: transparent;")
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        cap = QtWidgets.QLabel(caption)
        cap.setStyleSheet(
            "color: #64748b; font-size: 10px; font-weight: 700; "
            "letter-spacing: 0.6px; border: none; background: transparent;"
        )
        val = QtWidgets.QLabel(initial)
        if min_width > 0:
            val.setMinimumWidth(min_width)
        val.setStyleSheet(
            f"color: {color}; font-family: Consolas, 'Courier New', monospace; "
            "font-size: 12px; font-weight: 700; border: none; background: transparent;"
        )
        lay.addWidget(cap)
        lay.addWidget(val)
        parent_layout.addWidget(box)
        return val

    def _build_source_card(self) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setProperty("class", "Card")
        card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(9)

        top = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("Stream Source")
        lbl.setProperty("class", "CardTitle")
        top.addWidget(lbl)
        top.addStretch(1)

        self.live_tab_btn = QtWidgets.QPushButton("Live Headset")
        self.live_tab_btn.setProperty("class", "ModeTab")
        self.live_tab_btn.setProperty("active", True)
        self.replay_tab_btn = QtWidgets.QPushButton("Replay CSV")
        self.replay_tab_btn.setProperty("class", "ModeTab")
        self.replay_tab_btn.setProperty("active", False)
        self.live_tab_btn.clicked.connect(lambda: self._switch_source_tab("live"))
        self.replay_tab_btn.clicked.connect(lambda: self._switch_source_tab("replay"))
        top.addWidget(self.live_tab_btn)
        top.addWidget(self.replay_tab_btn)
        lay.addLayout(top)

        self.source_stack = QtWidgets.QStackedWidget()
        self.source_stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

        # Page 0: Live BLE
        live_page = QtWidgets.QWidget()
        live_page.setStyleSheet("background: transparent;")
        ll = QtWidgets.QHBoxLayout(live_page)
        ll.setContentsMargins(0, 2, 0, 2)
        ll.setSpacing(8)

        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setMinimumWidth(140)
        self.device_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.manage_dev_btn = QtWidgets.QPushButton("Devices...")
        self.manage_dev_btn.clicked.connect(self._open_device_manager)

        self.lsl_chk = QtWidgets.QCheckBox("LSL")
        if not HAS_PYLSL:
            self.lsl_chk.setEnabled(False)
            self.lsl_chk.setToolTip("Optional: install pylsl (`pip install pylsl`) to enable LSL output")

        ll.addWidget(QtWidgets.QLabel("Device:"))
        ll.addWidget(self.device_combo, 1)
        ll.addWidget(self.manage_dev_btn)
        ll.addWidget(self.lsl_chk)
        self.source_stack.addWidget(live_page)

        # Page 1: Replay CSV
        replay_page = QtWidgets.QWidget()
        replay_page.setStyleSheet("background: transparent;")
        rl = QtWidgets.QHBoxLayout(replay_page)
        rl.setContentsMargins(0, 2, 0, 2)
        rl.setSpacing(8)

        default_rec = get_recordings_dir() / "eeg_recording.csv"
        self.replay_edit = QtWidgets.QLineEdit(str(default_rec) if default_rec.exists() else "")
        self.replay_edit.setMinimumWidth(120)
        self.replay_edit.setPlaceholderText("Select recorded CSV file...")
        self.replay_edit.textChanged.connect(self._on_replay_path_changed)
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_replay_file)

        self.speed_combo = QtWidgets.QComboBox()
        for sp in ("0.5x", "1x", "2x", "4x"):
            self.speed_combo.addItem(sp, float(sp[:-1]))
        self.speed_combo.setCurrentIndex(1)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)

        self.loop_chk = QtWidgets.QCheckBox("Loop")
        self.loop_chk.setChecked(True)

        rl.addWidget(QtWidgets.QLabel("CSV File:"))
        rl.addWidget(self.replay_edit, 1)
        rl.addWidget(browse_btn)
        rl.addWidget(QtWidgets.QLabel("Speed:"))
        rl.addWidget(self.speed_combo)
        rl.addWidget(self.loop_chk)
        self.source_stack.addWidget(replay_page)

        self.source_stack.setFixedHeight(max(live_page.sizeHint().height(), replay_page.sizeHint().height(), 40))
        lay.addWidget(self.source_stack)

        # Action buttons + inline replay progress bar
        act_row = QtWidgets.QHBoxLayout()
        act_row.setSpacing(10)
        self.connect_btn = QtWidgets.QPushButton("Connect Stream")
        self.connect_btn.setObjectName("PrimaryBtn")
        self.connect_btn.setMinimumHeight(34)
        self.connect_btn.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.connect_btn.clicked.connect(self._toggle_stream)

        self.pause_btn = QtWidgets.QPushButton("Pause Stream")
        self.pause_btn.setMinimumHeight(34)
        self.pause_btn.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)

        self.studio_replay_row = QtWidgets.QWidget()
        self.studio_replay_row.setStyleSheet("background: transparent;")
        s_lay = QtWidgets.QHBoxLayout(self.studio_replay_row)
        s_lay.setContentsMargins(4, 0, 0, 0)
        s_lay.setSpacing(10)

        self.studio_replay_slider = ReplayProgressBar()
        self.studio_replay_slider.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.studio_replay_slider.setEnabled(False)
        self.studio_replay_slider.setToolTip("Click or drag to seek across Studio, Waveforms, and 3D Brain Map")
        self.studio_replay_slider.seekPressed.connect(self._on_studio_scrub_pressed)
        self.studio_replay_slider.seekMoved.connect(self._on_studio_scrub_moved)
        self.studio_replay_slider.seekReleased.connect(self._on_studio_scrub_released)

        self.studio_replay_pos_lbl = QtWidgets.QLabel("00:00.0 / 00:00.0")
        self.studio_replay_pos_lbl.setStyleSheet(
            "color: #e2e8f0; font-family: Consolas, 'Courier New', monospace; "
            "font-size: 12px; font-weight: 700; border: none; background: transparent;"
        )

        s_lay.addWidget(self.studio_replay_slider, 1)
        s_lay.addWidget(self.studio_replay_pos_lbl)
        self.studio_replay_row.setVisible(False)

        self._act_spacer = QtWidgets.QWidget()
        self._act_spacer.setStyleSheet("background: transparent;")
        self._act_spacer.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored
        )

        act_row.addWidget(self.connect_btn)
        act_row.addWidget(self.pause_btn)
        act_row.addWidget(self.studio_replay_row, 1)
        act_row.addWidget(self._act_spacer, 1)
        lay.addLayout(act_row)

        return card

    def _build_modules_card(self) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setProperty("class", "Card")
        card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(7)

        top = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("Visualization & Analysis Modules")
        lbl.setProperty("class", "CardTitle")
        top.addWidget(lbl)
        top.addStretch(1)
        self.launch_all_btn = QtWidgets.QPushButton("Launch All Live Views")
        self.launch_all_btn.setObjectName("PrimaryBtn")
        self.launch_all_btn.clicked.connect(self._launch_all_live)
        top.addWidget(self.launch_all_btn)
        lay.addLayout(top)

        # 1. Headmap row
        self.headmap_scale_combo = QtWidgets.QComboBox()
        for label, val in (("1.0x", 1.0), ("1.25x", 1.25), ("1.5x", 1.5)):
            self.headmap_scale_combo.addItem(label, val)
        self.headmap_scale_combo.setCurrentIndex(1)
        self.headmap_status_lbl, self.headmap_btn, row_hm = self._make_module_row(
            "Headmap",
            "Contact & signal quality + head orientation",
            [self.headmap_scale_combo],
            lambda: self._toggle_module("headmap"),
        )
        lay.addWidget(row_hm)

        # 2. Live Waveforms row
        self.viewer_width_combo = QtWidgets.QComboBox()
        for w in ("3s", "5s", "10s", "30s"):
            self.viewer_width_combo.addItem(w, w)
        self.viewer_width_combo.setCurrentIndex(1)
        self.viewer_status_lbl, self.viewer_btn, row_lv = self._make_module_row(
            "Live Waveforms",
            "14 EEG + 7 motion channels (PyQtGraph)",
            [QtWidgets.QLabel("Window:"), self.viewer_width_combo],
            lambda: self._toggle_module("live_viewer"),
        )
        lay.addWidget(row_lv)

        # 3. 3D Brain Map row (in-app Qt WebEngine)
        self.focus_brain_btn = QtWidgets.QPushButton("Focus")
        self.focus_brain_btn.clicked.connect(lambda: self._focus_window("brain_map"))
        self.focus_brain_btn.setEnabled(False)
        self.brain_status_lbl, self.brain_btn, row_br = self._make_module_row(
            "3D Brain Map",
            "FFT cortical band power 3D WebGL window",
            [self.focus_brain_btn],
            lambda: self._toggle_module("brain_map"),
        )
        lay.addWidget(row_br)

        # 4. CSV Compare row (in-app Qt WebEngine)
        self.focus_compare_btn = QtWidgets.QPushButton("Focus")
        self.focus_compare_btn.clicked.connect(lambda: self._focus_window("csv_compare"))
        self.focus_compare_btn.setEnabled(False)
        self.compare_status_lbl, self.compare_btn, row_cmp = self._make_module_row(
            "CSV Compare",
            "Side-by-side offline recording diff & correlation window",
            [self.focus_compare_btn],
            lambda: self._toggle_module("csv_compare"),
            idle_text="Open",
        )
        lay.addWidget(row_cmp)

        return card

    def _make_module_row(
        self,
        title: str,
        desc: str,
        extras: list[QtWidgets.QWidget],
        on_click,
        idle_text: str = "Launch",
    ):
        row = QtWidgets.QFrame()
        row.setProperty("class", "ModuleRow")
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(10)

        info = QtWidgets.QVBoxLayout()
        info.setSpacing(1)
        name_lbl = QtWidgets.QLabel(title)
        name_lbl.setStyleSheet("font-weight: 700; color: #f8fafc; background: transparent;")
        desc_lbl = QtWidgets.QLabel(desc)
        desc_lbl.setWordWrap(True)
        desc_lbl.setMinimumWidth(110)
        desc_lbl.setStyleSheet("color: #94a3b8; font-size: 12px; background: transparent;")
        info.addWidget(name_lbl)
        info.addWidget(desc_lbl)
        lay.addLayout(info, 1)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)
        for w in extras:
            if isinstance(w, QtWidgets.QLabel):
                w.setStyleSheet("color: #94a3b8; font-size: 12px; background: transparent;")
            controls.addWidget(w)

        status_lbl = QtWidgets.QLabel("Idle")
        status_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        status_lbl.setFixedSize(86, 32)
        status_lbl.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self._style_badge(status_lbl, running=False)
        controls.addWidget(status_lbl)
        controls.setAlignment(status_lbl, QtCore.Qt.AlignmentFlag.AlignVCenter)

        btn = QtWidgets.QPushButton(idle_text)
        btn.setMinimumWidth(84)
        btn.clicked.connect(on_click)
        controls.addWidget(btn)
        lay.addLayout(controls, 0)

        return status_lbl, btn, row

    def _build_recording_card(self) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setProperty("class", "Card")
        card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("CSV Recording & Timed Notes")
        lbl.setProperty("class", "CardTitle")
        self.rec_stats_lbl = QtWidgets.QLabel("Not recording")
        self.rec_stats_lbl.setStyleSheet("color: #94a3b8; font-family: Consolas, monospace; font-size: 12px;")
        top.addWidget(lbl)
        top.addStretch(1)
        top.addWidget(self.rec_stats_lbl)
        lay.addLayout(top)

        rec_row = QtWidgets.QHBoxLayout()
        rec_row.setSpacing(8)
        self.rec_path_edit = QtWidgets.QLineEdit()
        self.rec_path_edit.setMinimumWidth(120)
        rec_browse = QtWidgets.QPushButton("Browse...")
        rec_browse.clicked.connect(self._browse_record_file)
        self.rec_btn = QtWidgets.QPushButton("Start Recording")
        self.rec_btn.setMinimumWidth(132)
        self.rec_btn.clicked.connect(self._toggle_recording)
        rec_row.addWidget(QtWidgets.QLabel("Output:"))
        rec_row.addWidget(self.rec_path_edit, 1)
        rec_row.addWidget(rec_browse)
        rec_row.addWidget(self.rec_btn)
        lay.addLayout(rec_row)

        note_row = QtWidgets.QHBoxLayout()
        note_row.setSpacing(8)
        self.note_edit = QtWidgets.QLineEdit()
        self.note_edit.setMinimumWidth(120)
        self.note_edit.setPlaceholderText("Type an event note and press Enter (timestamp captured on first key)...")
        self.note_edit.textEdited.connect(self._on_note_edited)
        self.note_edit.returnPressed.connect(self._save_note)
        note_btn = QtWidgets.QPushButton("Save Note")
        note_btn.setMinimumWidth(132)
        note_btn.clicked.connect(self._save_note)
        note_row.addWidget(QtWidgets.QLabel("Note:   "))
        note_row.addWidget(self.note_edit, 1)
        note_row.addWidget(note_btn)
        lay.addLayout(note_row)

        return card

    def _build_log_card(self) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setProperty("class", "Card")
        card.setMinimumHeight(96)
        card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(5)

        lbl = QtWidgets.QLabel("Activity Log")
        lbl.setProperty("class", "CardTitle")
        lay.addWidget(lbl)

        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(300)
        self.log_box.setStyleSheet(
            "QPlainTextEdit { background: #080d18; border: 1px solid #1e293b; "
            "font-family: Consolas, 'Courier New', monospace; font-size: 11px; color: #94a3b8; }"
        )
        self.log_box.setMinimumHeight(54)
        lay.addWidget(self.log_box, 1)
        return card

    # --- Helpers & State Updates ---

    @staticmethod
    def _style_badge(lbl: QtWidgets.QLabel, running: bool, running_text: str = "Running"):
        if running:
            lbl.setText(running_text)
            lbl.setStyleSheet(
                "background: rgba(34, 197, 94, 0.16); color: #4ade80; "
                "border: 1px solid rgba(34, 197, 94, 0.45); border-radius: 10px; "
                "padding: 5px 12px; font-size: 13px; font-weight: 700;"
            )
        else:
            lbl.setText("Idle")
            lbl.setStyleSheet(
                "background: #0f172a; color: #64748b; border: 1px solid #1e293b; "
                "border-radius: 10px; padding: 5px 12px; font-size: 13px; font-weight: 600;"
            )

    def _set_status_visual(self, state: str, text: str):
        colors = {
            "streaming": "#22c55e",
            "replaying": "#38bdf8",
            "connecting": "#eab308",
            "scanning": "#eab308",
            "reconnecting": "#eab308",
            "paused": "#eab308",
            "error": "#ef4444",
            "disconnected": "#64748b",
        }
        dot_color = colors.get(state, "#64748b")
        self.status_dot.setStyleSheet(f"background: {dot_color}; border-radius: 4px; border: none;")
        self.status_text.setText(text)

    def _on_status_changed(self, state: str, msg: str):
        self._set_status_visual(state, msg)
        active = self.controller.mode != "idle"
        if active:
            self.connect_btn.setText("Disconnect" if self.controller.mode == "live" else "Stop Replay")
            self.connect_btn.setObjectName("DangerBtn")
        else:
            self._pending_replay_sec = 0.0
            self.connect_btn.setText("Connect Stream" if self._source_mode == "live" else "Start Replay")
            self.connect_btn.setObjectName("PrimaryBtn")
        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)

        self.pause_btn.setEnabled(active)
        if self.controller.paused:
            self.pause_btn.setText("Resume Stream")
            self.pause_btn.setObjectName("PauseActiveBtn")
        else:
            self.pause_btn.setText("Pause Stream")
            self.pause_btn.setObjectName("")
        self.pause_btn.style().unpolish(self.pause_btn)
        self.pause_btn.style().polish(self.pause_btn)
        self._refresh_runtime_state()

    def _append_log(self, msg: str):
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText(f"[{ts}] {msg}")

    def _load_devices(self):
        self.device_combo.clear()
        devs = dev_store.load_devices()
        for d in devs:
            self.device_combo.addItem(d.display_label, d)
        if not devs:
            self.device_combo.addItem("No device in KEYS.txt — click Devices... to add", None)

    def _refresh_recording_placeholder(self):
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.rec_path_edit.setText(str(get_recordings_dir() / f"eeg_{stamp}.csv"))

    def _switch_source_tab(self, mode: str):
        self._source_mode = mode
        self.source_stack.setCurrentIndex(0 if mode == "live" else 1)
        self.live_tab_btn.setProperty("active", mode == "live")
        self.replay_tab_btn.setProperty("active", mode == "replay")
        for b in (self.live_tab_btn, self.replay_tab_btn):
            b.style().unpolish(b)
            b.style().polish(b)
        if self.controller.mode == "idle":
            self.connect_btn.setText("Connect Stream" if mode == "live" else "Start Replay")
        self._refresh_runtime_state()

    # --- Actions ---

    def _open_device_manager(self):
        dlg = DeviceManagerDialog(self)
        dlg.exec()
        self._load_devices()

    def _browse_replay_file(self):
        rec_dir = get_recordings_dir()
        start_dir = str(rec_dir if rec_dir.exists() else rec_dir.parent)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select EEG Recording CSV", start_dir, "CSV Files (*.csv);;All Files (*)"
        )
        if path:
            self.replay_edit.setText(path)

    def _browse_record_file(self):
        rec_dir = get_recordings_dir()
        start_dir = self.rec_path_edit.text() or str(rec_dir if rec_dir.exists() else rec_dir.parent)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save EEG Recording CSV", start_dir, "CSV Files (*.csv)"
        )
        if path:
            self.rec_path_edit.setText(path)

    def _on_speed_changed(self):
        sp = self.speed_combo.currentData()
        if sp:
            self.controller.set_replay_speed(float(sp))

    @QtCore.pyqtSlot(object)
    def _execute_ui_call(self, fn):
        fn()

    def _run_on_ui_thread(self, fn, timeout: float = 2.0) -> bool:
        """Execute a callable on the Qt UI thread and return its boolean outcome."""
        if self._closing:
            return False
        app = QtWidgets.QApplication.instance()
        if app is not None and QtCore.QThread.currentThread() == app.thread():
            return bool(fn())
        done = threading.Event()
        result: dict[str, bool] = {"ok": False}

        def _wrapped():
            try:
                if not self._closing:
                    result["ok"] = bool(fn())
            finally:
                done.set()

        self.signals.ui_call_requested.emit(_wrapped)
        if not done.wait(timeout):
            return False
        return result["ok"]

    def _start_selected_source(self, *, defer_dialogs: bool = False) -> bool:
        """Start the currently selected Studio stream source if idle."""
        if self.controller.mode != "idle":
            return True

        def _notify_invalid(title: str, message: str, *, warn: bool) -> None:
            def _show() -> None:
                if self._closing:
                    return
                if warn:
                    QtWidgets.QMessageBox.warning(self, title, message)
                else:
                    QtWidgets.QMessageBox.information(self, title, message)

            if defer_dialogs:
                QtCore.QTimer.singleShot(0, _show)
            else:
                _show()

        if self._source_mode == "live":
            prof = self.device_combo.currentData()
            if prof is None:
                self._append_log("Cannot start live stream: no device profile configured")
                _notify_invalid(
                    "No Device Configured",
                    "Please add your EPOC-X AES key and whitening byte in Devices... first.",
                    warn=False,
                )
                return False
            self.controller.start_live(prof, use_lsl=self.lsl_chk.isChecked())
            return self.controller.mode != "idle"

        csv_path = self.replay_edit.text().strip()
        if not csv_path or not os.path.exists(csv_path):
            self._append_log(f"Cannot start replay: CSV file not found ({csv_path})")
            _notify_invalid("File Not Found", f"Cannot find CSV file:\n{csv_path}", warn=True)
            return False
        sp = float(self.speed_combo.currentData() or 1.0)
        start_sec = self._pending_replay_sec
        self._pending_replay_sec = 0.0
        self.controller.start_replay(
            csv_path,
            speed=sp,
            loop=self.loop_chk.isChecked(),
            start_elapsed_sec=start_sec,
        )
        self._refresh_runtime_state()
        return self.controller.mode != "idle"

    def _start_stream_from_view(self) -> bool:
        """Safely start Studio's selected source when triggered from an attached view."""
        return self._run_on_ui_thread(
            lambda: self._start_selected_source(defer_dialogs=True),
            timeout=2.0,
        )

    def _toggle_stream(self):
        if self.controller.mode != "idle":
            self._pending_replay_sec = 0.0
            self.controller.stop_source()
            return
        self._start_selected_source(defer_dialogs=False)

    def _toggle_pause(self):
        self.controller.set_paused()
        self._refresh_runtime_state()

    @staticmethod
    def _fmt_mmss_tenth(sec: float) -> str:
        s = max(0.0, float(sec))
        mm = int(s // 60.0)
        rem = s - mm * 60.0
        return f"{mm:02d}:{rem:04.1f}"

    def _get_selected_csv_duration(self) -> Optional[float]:
        csv_path = self.replay_edit.text().strip()
        if not csv_path or not os.path.isfile(csv_path):
            self._csv_duration_cache = None
            return None
        try:
            st = os.stat(csv_path)
        except OSError:
            self._csv_duration_cache = None
            return None
        cached = self._csv_duration_cache
        if cached is not None and cached[0] == csv_path and cached[1] == st.st_mtime_ns and cached[2] == st.st_size:
            return cached[3]
        dur_ms = _extract_csv_duration_ms(csv_path)
        dur_s = max(0.0, dur_ms / 1000.0) if dur_ms is not None else None
        self._csv_duration_cache = (csv_path, st.st_mtime_ns, st.st_size, dur_s)
        return dur_s

    def _on_replay_path_changed(self, _text: str) -> None:
        self._pending_replay_sec = 0.0
        self._csv_duration_cache = None
        self._refresh_runtime_state()

    def _get_active_or_selected_duration(self) -> Optional[float]:
        if self.controller.mode == "replay":
            prog = self.controller.get_replay_progress()
            if prog is not None:
                return prog[1]
            return self._get_selected_csv_duration()
        if self.controller.mode == "idle" and self._source_mode == "replay":
            return self._get_selected_csv_duration()
        return None

    def _apply_studio_seek(self, ratio: float, *, force: bool) -> None:
        dur = self._get_active_or_selected_duration()
        if dur is None or dur <= 0.0:
            return
        clamped_ratio = max(0.0, min(1.0, float(ratio)))
        target_sec = clamped_ratio * dur
        self.studio_replay_pos_lbl.setText(
            f"{self._fmt_mmss_tenth(target_sec)} / {self._fmt_mmss_tenth(dur)}"
        )
        if self.controller.mode == "replay":
            now = time.monotonic()
            if force:
                if (
                    self._last_seek_sent_ratio is not None
                    and abs(clamped_ratio - self._last_seek_sent_ratio) < 1e-6
                    and (now - self._last_seek_sent_at) < 0.25
                ):
                    return
            elif (now - self._last_seek_sent_at) < 0.05:
                return
            self._last_seek_sent_at = now
            self._last_seek_sent_ratio = clamped_ratio
            self.controller.seek_replay(target_sec, window_samples=128)
        elif self.controller.mode == "idle" and self._source_mode == "replay":
            self._pending_replay_sec = target_sec

    def _on_studio_scrub_pressed(self, ratio: float) -> None:
        self._studio_scrub_dragging = True
        self._last_seek_sent_ratio = None
        self._apply_studio_seek(ratio, force=True)

    def _on_studio_scrub_moved(self, ratio: float) -> None:
        self._apply_studio_seek(ratio, force=False)

    def _on_studio_scrub_released(self, ratio: float) -> None:
        self._studio_scrub_dragging = False
        self._apply_studio_seek(ratio, force=True)
        self._refresh_runtime_state()

    def _toggle_recording(self):
        if self.controller.recording_path:
            self.controller.stop_recording()
            self.rec_btn.setText("Start Recording")
            self.rec_btn.setObjectName("")
            self.rec_btn.style().unpolish(self.rec_btn)
            self.rec_btn.style().polish(self.rec_btn)
            self.rec_path_edit.setEnabled(True)
            self._refresh_recording_placeholder()
        else:
            path = self.rec_path_edit.text().strip()
            if not path:
                self._refresh_recording_placeholder()
                path = self.rec_path_edit.text().strip()
            self.controller.start_recording(path)
            self.rec_btn.setText("Stop Recording")
            self.rec_btn.setObjectName("RecordActiveBtn")
            self.rec_btn.style().unpolish(self.rec_btn)
            self.rec_btn.style().polish(self.rec_btn)
            self.rec_path_edit.setEnabled(False)

    def _on_note_edited(self, text: str):
        if text and self._note_start_ns is None:
            self._note_start_ns = time.time_ns()
        elif not text:
            self._note_start_ns = None

    def _save_note(self):
        text = self.note_edit.text().strip()
        if not text:
            return
        end_ns = time.time_ns()
        start_ns = self._note_start_ns or end_ns
        self.controller.add_note(text, start_ns=start_ns, end_ns=end_ns)
        self.note_edit.clear()
        self._note_start_ns = None

    # --- In-App Module Window Management ---

    def _is_window_open(self, key: str) -> bool:
        return self.windows.get(key) is not None

    def _focus_window(self, key: str):
        win = self.windows.get(key)
        if win is None:
            return
        if win.isMinimized():
            win.showNormal()
        else:
            win.show()
        win.raise_()
        win.activateWindow()

    def _cleanup_pause_listener(self, key: str) -> None:
        listener = self._window_pause_listeners.pop(key, None)
        if listener is not None:
            self.controller.remove_pause_listener(listener)  # type: ignore[arg-type]
        state_listener = self._window_state_listeners.pop(key, None)
        if state_listener is not None:
            self.controller.remove_state_listener(state_listener)  # type: ignore[arg-type]
        replay_listener = self._window_replay_listeners.pop(key, None)
        if replay_listener is not None:
            self.controller.remove_replay_listener(replay_listener)  # type: ignore[arg-type]

    def _cleanup_stream(self, key: str, stream) -> None:
        self._cleanup_pause_listener(key)
        if self._window_streams.get(key) is stream:
            self._window_streams.pop(key, None)
        self.controller.unsubscribe(stream)

    def _on_window_closed(self, key: str, win: QtWidgets.QWidget) -> None:
        if self.windows.get(key) is win:
            self.windows[key] = None
        self._cleanup_pause_listener(key)
        stream = self._window_streams.pop(key, None)
        if stream is not None:
            self.controller.unsubscribe(stream)

    def _stop_module(self, key: str) -> None:
        win = self.windows.get(key)
        self.windows[key] = None
        self._cleanup_pause_listener(key)
        stream = self._window_streams.pop(key, None)
        if win is not None:
            try:
                win.close()
                win.shutdown()
            except Exception:
                pass
        if stream is not None:
            self.controller.unsubscribe(stream)

    def _toggle_module(self, key: str):
        if self._is_window_open(key):
            self._stop_module(key)
            self._append_log(f"Closed {key.replace('_', ' ')}")
            return
        self._launch_module(key)

    def _launch_module(self, key: str):
        if self._is_window_open(key):
            self._focus_window(key)
            return

        win: Optional[QtWidgets.QWidget] = None
        stream = None
        try:
            if key == "headmap":
                stream = self.controller.subscribe()
                self._window_streams[key] = stream
                scale = float(self.headmap_scale_combo.currentData() or 1.0)
                args = SimpleNamespace(
                    demo=False,
                    lsl=False,
                    lsl_name="EPOCX",
                    connect=None,
                    scale=scale,
                    no_labels=False,
                    float_above=False,
                    geometry=None,
                    name=None,
                    no_orientation=False,
                    send_to=None,
                    device="epocx1",
                    key=None,
                )
                win = HeadmapWindow(
                    args=args,
                    stream=stream,
                    on_close=lambda k=key, st=stream: self._cleanup_stream(k, st),
                )
                self._append_log("Launched Headmap window")

            elif key == "live_viewer":
                stream = self.controller.subscribe()
                self._window_streams[key] = stream
                width = self.viewer_width_combo.currentData() or "5s"
                replay_data = self.controller.get_replay_all()

                def _on_viewer_seek(elapsed_sec: float, st=stream) -> Optional[float]:
                    res = self.controller.seek_replay(
                        elapsed_sec,
                        window_samples=128,
                        exclude_stream=st,
                    )
                    if res is None:
                        return None
                    QtCore.QTimer.singleShot(0, self._refresh_runtime_state)
                    return res[0]

                win = create_viewer_window(
                    stream,
                    group_specs=("5-18", "19-25"),
                    style="linespoints",
                    width_ms=parse_duration(width),
                    quick_on_header=True,
                    window_title="EPOC-X Live Waveforms",
                    on_close=lambda k=key, st=stream: self._cleanup_stream(k, st),
                    initial_paused=self.controller.paused,
                    initial_active=(self.controller.mode != "idle"),
                    initial_mode=self.controller.mode,
                    on_pause_request=self.controller.set_paused,
                    on_start_request=self._start_stream_from_view,
                    on_seek_request=_on_viewer_seek,
                    replay_data=replay_data,
                )
                if win is not None:
                    self._window_state_listeners[key] = win.set_stream_state
                    self.controller.add_state_listener(win.set_stream_state)
                    win.set_stream_state(self.controller.mode, self.controller.paused)

                    def _on_replay_ready(w=win) -> None:
                        latest = self.controller.get_replay_all()
                        if latest is not None:
                            w.load_replay_data(*latest)

                    self._window_replay_listeners[key] = _on_replay_ready
                    self.controller.add_replay_listener(_on_replay_ready)
                    if replay_data is None:
                        latest = self.controller.get_replay_all()
                        if latest is not None:
                            win.load_replay_data(*latest)
                self._append_log(f"Launched Live Waveforms window (window={width})")

            elif key == "brain_map":
                stream = self.controller.subscribe()
                self._window_streams[key] = stream
                dsp = BrainDSP()
                dsp.set_stream_state(self.controller.mode, self.controller.paused)
                snapshot = self.controller.get_replay_snapshot(window_samples=dsp.window_len)
                if snapshot is not None:
                    first_ts, last_ts, header_line, window_lines = snapshot
                    dsp.set_replay_bounds(first_ts, last_ts)
                    snap_samples, snap_ts = parse_csv_lines_for_dsp(
                        header_line, window_lines, default_fs=dsp.fs
                    )
                    if snap_samples:
                        dsp.replace_window(snap_samples, snap_ts)

                def _on_brain_seek(
                    elapsed_sec: float,
                    st=stream,
                    target_dsp=dsp,
                ) -> Optional[float]:
                    res = self.controller.seek_replay(
                        elapsed_sec,
                        window_samples=target_dsp.window_len,
                        exclude_stream=st,
                    )
                    if res is None:
                        return None
                    actual_elapsed, hdr_line, win_lines = res
                    samples, timestamps = parse_csv_lines_for_dsp(
                        hdr_line, win_lines, default_fs=target_dsp.fs
                    )
                    if samples:
                        target_dsp.replace_window(samples, timestamps)
                    QtCore.QTimer.singleShot(0, self._refresh_runtime_state)
                    return actual_elapsed

                win = create_brain_map_window(
                    dsp=dsp,
                    stream=stream,
                    host="127.0.0.1",
                    port=BRAIN_PORT,
                    allow_ephemeral_fallback=True,
                    on_close=lambda k=key, st=stream: self._cleanup_stream(k, st),
                    on_pause_request=self.controller.set_paused,
                    on_start_request=self._start_stream_from_view,
                    on_seek_request=_on_brain_seek,
                )
                self._window_state_listeners[key] = win.server_controller.set_stream_state
                self.controller.add_state_listener(win.server_controller.set_stream_state)
                win.server_controller.set_stream_state(self.controller.mode, self.controller.paused)
                self._append_log(f"Launched 3D Brain Map window (local server {win.server_controller.url})")

            elif key == "csv_compare":
                win = create_csv_compare_window()
                self._append_log("Opened CSV Compare window")
            elif key == "manual":
                win = create_manual_window()
                self._append_log("Opened User Manual")

        except Exception as exc:
            if stream is not None:
                self._cleanup_stream(key, stream)
            self._append_log(f"Failed to open {key.replace('_', ' ')}: {exc}")
            QtWidgets.QMessageBox.critical(
                self,
                "Module Launch Error",
                f"Could not launch {key.replace('_', ' ')}:\n{exc}",
            )
            return

        if win is None:
            if stream is not None:
                self._cleanup_stream(key, stream)
            self._append_log(f"Failed to initialize {key.replace('_', ' ')}")
            return

        win.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        win.closed.connect(lambda k=key, w=win: self._on_window_closed(k, w))
        self.windows[key] = win
        win.show()
        apply_dark_titlebar(win)
        win.raise_()
        win.activateWindow()

    def _launch_all_live(self):
        for key in ("headmap", "live_viewer", "brain_map"):
            if not self._is_window_open(key):
                self._launch_module(key)

    def _refresh_runtime_state(self):
        self.rate_lbl.setText(f"{self.controller.sample_hz:.1f} Hz")
        self.samples_lbl.setText(f"{self.controller.sample_count:,}")
        self.clients_lbl.setText(str(self.controller.client_count))

        if self.controller.recording_path and self.controller.recording_start_time:
            elapsed = max(0, int(time.time() - self.controller.recording_start_time))
            mm, ss = divmod(elapsed, 60)
            self.rec_stats_lbl.setText(
                f"REC  {mm:02d}:{ss:02d}  |  {self.controller.recorded_rows:,} rows"
            )
            self.rec_stats_lbl.setStyleSheet(
                "color: #fca5a5; font-family: Consolas, monospace; font-size: 12px; font-weight: 700;"
            )
        else:
            self.rec_stats_lbl.setText("Not recording")
            self.rec_stats_lbl.setStyleSheet("color: #94a3b8; font-family: Consolas, monospace; font-size: 12px;")

        replay_elapsed: Optional[float] = None
        replay_dur: Optional[float] = None
        if self.controller.mode == "replay":
            prog = self.controller.get_replay_progress()
            if prog is not None:
                replay_elapsed, replay_dur = prog
            else:
                replay_dur = self._get_selected_csv_duration()
                if replay_dur is not None:
                    replay_elapsed = min(self._pending_replay_sec, replay_dur)
        elif self.controller.mode == "idle" and self._source_mode == "replay":
            replay_dur = self._get_selected_csv_duration()
            if replay_dur is not None:
                self._pending_replay_sec = max(0.0, min(self._pending_replay_sec, replay_dur))
                replay_elapsed = self._pending_replay_sec

        if replay_dur is not None and replay_elapsed is not None:
            self.studio_replay_row.setVisible(True)
            self._act_spacer.setVisible(False)
            self.studio_replay_slider.setEnabled(replay_dur > 0.0)
            if not self._studio_scrub_dragging:
                self.studio_replay_pos_lbl.setText(
                    f"{self._fmt_mmss_tenth(replay_elapsed)} / {self._fmt_mmss_tenth(replay_dur)}"
                )
                ratio = (replay_elapsed / replay_dur) if replay_dur > 0.0 else 0.0
                self.studio_replay_slider.setProgress(ratio)
        else:
            self.studio_replay_row.setVisible(False)
            self._act_spacer.setVisible(True)
            self.studio_replay_slider.setEnabled(False)

        hm_on = self._is_window_open("headmap")
        lv_on = self._is_window_open("live_viewer")
        br_on = self._is_window_open("brain_map")
        cmp_on = self._is_window_open("csv_compare")

        self._style_badge(self.headmap_status_lbl, hm_on)
        self.headmap_btn.setText("Stop" if hm_on else "Launch")
        self._style_badge(self.viewer_status_lbl, lv_on)
        self.viewer_btn.setText("Stop" if lv_on else "Launch")
        self._style_badge(self.brain_status_lbl, br_on)
        self.brain_btn.setText("Stop" if br_on else "Launch")
        self.focus_brain_btn.setEnabled(br_on)
        self._style_badge(self.compare_status_lbl, cmp_on, running_text="Open")
        self.compare_btn.setText("Close" if cmp_on else "Open")
        self.focus_compare_btn.setEnabled(cmp_on)

    def closeEvent(self, event: QtGui.QCloseEvent):
        self._closing = True
        self._poll_timer.stop()
        for key in list(self.windows):
            self._stop_module(key)
        self.controller.shutdown()
        event.accept()


def _parse_args(argv: Optional[list[str]] = None):
    ap = argparse.ArgumentParser(
        prog="epoc-studio",
        description="EPOC-X Studio — Unified EEG Stream Hub and Visualization Windows.",
    )
    ap.add_argument(
        "--install-shortcut",
        action="store_true",
        help="Install Windows Start Menu and Desktop shortcuts for EPOC-X Studio and exit",
    )
    ap.add_argument(
        "--no-desktop",
        action="store_true",
        help="When used with --install-shortcut, skip creating a Desktop shortcut",
    )
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    ensure_stdio()
    args = _parse_args(argv)
    if args.install_shortcut:
        created = install_windows_shortcuts(desktop=not args.no_desktop)
        for kind, path in created.items():
            print(f"Created {kind} shortcut: {path}")
        return 0

    app = ensure_qt_app(sys.argv)
    win = LauncherWindow()
    install_sigint_handler(win, win.close)
    win.show()
    apply_dark_titlebar(win)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

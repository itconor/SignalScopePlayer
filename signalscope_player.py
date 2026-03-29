#!/usr/bin/env python3
"""SignalScope Logger Player — standalone desktop playback client.

Two connection modes:
  • Hub mode   — connects to a SignalScope hub via mobile API (Bearer token)
  • Direct mode — opens a recordings directory with catalog.json (local/SMB)

Requirements:  pip install PySide6
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, QUrl, Signal, Slot, QThread, QSize, QRect, QPoint,
)
from PySide6.QtGui import (
    QColor, QPainter, QFont, QFontMetrics, QPen, QBrush, QLinearGradient,
    QIcon, QPalette, QAction,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QLineEdit, QListWidget, QListWidgetItem,
    QSplitter, QTabWidget, QFileDialog, QComboBox, QSlider, QFrame,
    QScrollArea, QSizePolicy, QToolTip, QMessageBox, QStyle, QStyleFactory,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

# ─── Version ──────────────────────────────────────────────────────────────────
__version__ = "1.2.2"

# ─── Brand assets ─────────────────────────────────────────────────────────────
def _asset(name: str) -> str:
    """Return absolute path to a bundled asset, or '' if not found."""
    here = Path(__file__).parent
    p = here / name
    return str(p) if p.exists() else ""

# ─── Color scheme (matches SignalScope logger web UI) ─────────────────────────
C = {
    "bg":       "#07142b",
    "bg_grad1": "#12376f",
    "bg_grad2": "#05101f",
    "sur":      "#0d2346",
    "bor":      "#17345f",
    "acc":      "#17a8ff",
    "ok":       "#22c55e",
    "wn":       "#f59e0b",
    "al":       "#ef4444",
    "tx":       "#eef5ff",
    "mu":       "#8aa4c8",
    "seg_ok":     "#166534",
    "seg_warn":   "#78350f",
    "seg_silent": "#7f1d1d",
    "seg_none":   "#0e2040",
    "seg_future": "#0a1828",
    "hdr_bg":     "#0a1f41",
    "input_bg":   "#173a69",
}

SEG_SECS = 300  # 5-minute segments

SETTINGS_PATH = Path.home() / ".signalscope_player.json"

AUDIO_EXTS = (".mp3", ".aac", ".opus")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_time(secs: float) -> str:
    s = int(max(0, secs))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _seg_color(seg: dict) -> str:
    sil = seg.get("silence_pct", 0.0)
    if seg.get("_none"):
        return C["seg_none"]
    if seg.get("_future"):
        return C["seg_future"]
    if sil > 80:
        return C["seg_silent"]
    if sil > 10:
        return C["seg_warn"]
    return C["seg_ok"]


def _load_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_settings(data: dict):
    try:
        SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ─── Data Source Abstraction ──────────────────────────────────────────────────

class DataSource(ABC):
    @abstractmethod
    def catalog(self) -> list:
        """Return [{"slug", "name", "site", "owner", "rec_format"}, ...]"""

    @abstractmethod
    def days(self, slug: str, site: str = "") -> list:
        """Return ["2026-03-29", ...] reverse sorted."""

    @abstractmethod
    def segments(self, slug: str, date: str, site: str = "") -> list:
        """Return [{"filename", "start_s", "silence_pct", "has_silence", ...}, ...]"""

    @abstractmethod
    def metadata(self, slug: str, date: str, site: str = "") -> list:
        """Return [{"ts_s", "type", "title", "artist", "show_name", "presenter"}, ...]"""

    @abstractmethod
    def audio_url(self, slug: str, date: str, filename: str, seek_s: float = 0) -> str:
        """Return a URL or local path for audio playback."""

    @abstractmethod
    def mode(self) -> str:
        """'hub' or 'direct'"""


class HubDataSource(DataSource):
    def __init__(self, hub_url: str, token: str):
        self._url = hub_url.rstrip("/")
        self._token = token

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self._url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def catalog(self) -> list:
        data = self._get("/api/mobile/logger/catalog")
        return data.get("catalog", [])

    def days(self, slug: str, site: str = "") -> list:
        params = {"slug": slug}
        if site:
            params["site"] = site
        for _ in range(10):
            data = self._get("/api/mobile/logger/days", params)
            if not data.get("pending"):
                return data.get("days", [])
            time.sleep(3)
        return data.get("days", [])

    def segments(self, slug: str, date: str, site: str = "") -> list:
        params = {"slug": slug, "date": date}
        if site:
            params["site"] = site
        for _ in range(10):
            data = self._get("/api/mobile/logger/segments", params)
            if not data.get("pending"):
                return data.get("segments", [])
            time.sleep(3)
        return data.get("segments", [])

    def metadata(self, slug: str, date: str, site: str = "") -> list:
        params = {"slug": slug, "date": date}
        if site:
            params["site"] = site
        for _ in range(10):
            data = self._get("/api/mobile/logger/metadata", params)
            if not data.get("pending"):
                return data.get("events", [])
            time.sleep(3)
        return data.get("events", [])

    def audio_url(self, slug: str, date: str, filename: str,
                  seek_s: float = 0) -> str:
        qs = urllib.parse.urlencode({
            "slug": slug, "date": date, "filename": filename,
            "seek_s": seek_s, "token": self._token,
        })
        return f"{self._url}/api/mobile/logger/stream_pcm?{qs}"

    def mode(self) -> str:
        return "hub"


class DirectDataSource(DataSource):
    def __init__(self, root: str):
        self._root = Path(root)

    def catalog(self) -> list:
        cat_path = self._root / "catalog.json"
        if not cat_path.exists():
            # Fallback: scan directories as streams
            result = []
            for d in sorted(self._root.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    result.append({
                        "slug": d.name, "name": d.name,
                        "site": "local", "owner": "local",
                        "rec_format": "mp3",
                    })
            return result
        try:
            data = json.loads(cat_path.read_text())
            return [{"slug": slug, "name": info.get("name", slug),
                     "site": info.get("owner", "local"),
                     "owner": info.get("owner", "local"),
                     "rec_format": info.get("rec_format", "mp3")}
                    for slug, info in data.items()]
        except Exception:
            return []

    def days(self, slug: str, site: str = "") -> list:
        sdir = self._root / slug
        if not sdir.exists():
            return []
        return sorted(
            [d.name for d in sdir.iterdir()
             if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name)],
            reverse=True)

    def segments(self, slug: str, date: str, site: str = "") -> list:
        day_dir = self._root / slug / date
        result = {}
        # Try SQLite metadata first
        for db_path in [self._root / "logger_index.db",
                        self._root.parent / "logger_index.db"]:
            if db_path.exists():
                try:
                    conn = sqlite3.connect(str(db_path), timeout=5)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT * FROM segments WHERE stream=? AND date=? ORDER BY start_s",
                        (slug, date)).fetchall()
                    conn.close()
                    for r in rows:
                        d = dict(r)
                        try:
                            d["silence_ranges"] = json.loads(
                                d.get("silence_ranges") or "[]")
                        except Exception:
                            d["silence_ranges"] = []
                        result[d["filename"]] = d
                except Exception:
                    pass
                break
        # Supplement with filesystem scan
        if day_dir.exists():
            for f in sorted(day_dir.iterdir()):
                if f.suffix in AUDIO_EXTS and f.name not in result:
                    m = re.match(r"^(\d{2})-(\d{2})\.", f.name)
                    ss = (int(m.group(1)) * 3600 + int(m.group(2)) * 60) if m else 0.0
                    result[f.name] = {
                        "stream": slug, "date": date, "filename": f.name,
                        "start_s": ss, "has_silence": 0, "silence_pct": 0.0,
                        "silence_ranges": [], "quality": "high",
                    }
        return sorted(result.values(), key=lambda x: x.get("start_s", 0))

    def metadata(self, slug: str, date: str, site: str = "") -> list:
        import datetime as _dt
        try:
            midnight = _dt.datetime.strptime(date, "%Y-%m-%d").replace(
                tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            return []

        # ── Sidecar JSON (logger >= 1.5.5) ─────────────────────────────────
        # Each logger writes meta_{owner}.json per day dir.  Read all of them
        # and merge — gives full metadata across all instances on shared dirs.
        try:
            day_dir = self._root / slug / date
            if not day_dir.is_dir():
                # Also check one level up (root is a slug directory)
                day_dir = self._root.parent / slug / date
            if day_dir.is_dir():
                seen: dict = {}
                for meta_file in sorted(day_dir.glob("meta_*.json")):
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            entries = json.load(f)
                        for e in entries:
                            ts    = e.get("ts", 0)
                            etype = e.get("type", "")
                            key   = (round(ts, 1), etype)
                            if key not in seen:
                                seen[key] = {
                                    "ts_s":      ts - midnight,
                                    "type":      etype,
                                    "title":     e.get("title",     ""),
                                    "artist":    e.get("artist",    ""),
                                    "show_name": e.get("show_name", ""),
                                    "presenter": e.get("presenter", ""),
                                }
                    except Exception:
                        pass
                if seen:
                    return sorted(seen.values(), key=lambda e: e["ts_s"])
        except Exception:
            pass

        # ── Legacy SQLite fallback (logger 1.5.2–1.5.4 metadata.db) ────────
        _SQL = ("SELECT ts, type, title, artist, show_name, presenter "
                "FROM metadata_log WHERE stream=? AND ts>=? AND ts<? ORDER BY ts")

        def _query(db_path):
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(_SQL, (slug, midnight, midnight + 86400)).fetchall()
            conn.close()
            return [{"ts_s": r["ts"] - midnight, "type": r["type"],
                     "title": r["title"] or "", "artist": r["artist"] or "",
                     "show_name": r["show_name"] or "", "presenter": r["presenter"] or ""}
                    for r in rows]

        for db_path in [self._root / "metadata.db",
                        self._root.parent / "metadata.db"]:
            if db_path.exists():
                try:
                    rows = _query(db_path)
                    if rows:
                        return rows
                except Exception:
                    pass
                break

        # ── Legacy fallback: logger_index.db (pre-1.5.2) ───────────────────
        for db_path in [self._root / "logger_index.db",
                        self._root.parent / "logger_index.db"]:
            if db_path.exists():
                try:
                    return _query(db_path)
                except Exception:
                    pass
                break
        return []

    def audio_url(self, slug: str, date: str, filename: str,
                  seek_s: float = 0) -> str:
        return str(self._root / slug / date / filename)

    def mode(self) -> str:
        return "direct"


# ─── Background worker for data fetching ─────────────────────────────────────

class FetchWorker(QThread):
    finished = Signal(str, object)  # (task_name, result)
    error = Signal(str, str)        # (task_name, error_msg)

    def __init__(self, task_name: str, func, *args):
        super().__init__()
        self._task = task_name
        self._func = func
        self._args = args

    def run(self):
        try:
            result = self._func(*self._args)
            self.finished.emit(self._task, result)
        except Exception as e:
            self.error.emit(self._task, str(e))


# ─── Custom Widgets ───────────────────────────────────────────────────────────

class DayBar(QWidget):
    """288-block overview bar representing 24 hours of 5-minute segments."""
    clicked = Signal(int)  # start_s of clicked block

    def __init__(self):
        super().__init__()
        self._blocks = [None] * 288  # segment dicts or None
        self._head_s = -1            # playback head position (seconds)
        self._mark_in = -1
        self._mark_out = -1
        self._hover_x = -1
        self.setFixedHeight(32)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_segments(self, segs: list):
        self._blocks = [None] * 288
        for seg in segs:
            idx = int(seg.get("start_s", 0)) // SEG_SECS
            if 0 <= idx < 288:
                self._blocks[idx] = seg
        self.update()

    def set_head(self, secs: float):
        self._head_s = secs
        self.update()

    def set_marks(self, in_s: float, out_s: float):
        self._mark_in = in_s
        self._mark_out = out_s
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Background
        p.fillRect(0, 0, w, h, QColor(C["seg_none"]))
        p.setPen(QPen(QColor(C["bor"]), 1))
        p.drawRoundedRect(0, 0, w - 1, h - 1, 4, 4)

        # Blocks
        bw = w / 288.0
        for i, seg in enumerate(self._blocks):
            x = int(i * bw)
            bx = int((i + 1) * bw) - x
            if seg:
                p.fillRect(x, 1, bx, h - 2, QColor(_seg_color(seg)))

        # Mark range
        if self._mark_in >= 0 and self._mark_out >= 0:
            x1 = int(self._mark_in / 86400 * w)
            x2 = int(self._mark_out / 86400 * w)
            p.fillRect(x1, 0, x2 - x1, h, QColor(23, 168, 255, 50))

        # Marks
        for val, col in [(self._mark_in, C["acc"]), (self._mark_out, C["wn"])]:
            if val >= 0:
                x = int(val / 86400 * w)
                p.setPen(QPen(QColor(col), 2))
                p.drawLine(x, 0, x, h)

        # Playback head
        if self._head_s >= 0:
            x = int(self._head_s / 86400 * w)
            p.setPen(QPen(QColor(C["ok"]), 2))
            p.drawLine(x, 0, x, h)

        # Hour ticks
        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        for hr in range(1, 24):
            x = int(hr / 24 * w)
            p.drawLine(x, 0, x, h)

        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            frac = event.position().x() / self.width()
            secs = int(frac * 86400)
            self.clicked.emit(secs)

    def mouseMoveEvent(self, event):
        self._hover_x = int(event.position().x())
        frac = event.position().x() / self.width()
        secs = int(frac * 86400)
        QToolTip.showText(event.globalPosition().toPoint(),
                          _fmt_time(secs), self)


class SegmentGrid(QWidget):
    """24-row × 12-col grid of 5-minute segment blocks."""
    segment_clicked = Signal(dict)  # the segment dict

    BLOCK_H = 24
    BLOCK_GAP = 2
    LABEL_W = 42
    ROW_H = BLOCK_H + BLOCK_GAP

    def __init__(self):
        super().__init__()
        self._segments = {}    # start_s → seg dict
        self._selected_s = -1  # start_s of selected segment
        self._playing_s = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._update_size()

    def _update_size(self):
        self.setMinimumHeight(24 * self.ROW_H + 4)

    def set_segments(self, segs: list):
        self._segments = {}
        for seg in segs:
            self._segments[int(seg.get("start_s", 0))] = seg
        self.update()

    def set_selected(self, start_s: int):
        self._selected_s = start_s
        self.update()

    def set_playing(self, start_s: int):
        self._playing_s = start_s
        self.update()

    def _block_rect(self, hour: int, slot: int) -> QRect:
        grid_w = self.width() - self.LABEL_W - 4
        bw = max(1, (grid_w - 11 * self.BLOCK_GAP) // 12)
        x = self.LABEL_W + slot * (bw + self.BLOCK_GAP)
        y = hour * self.ROW_H + 2
        return QRect(x, y, bw, self.BLOCK_H)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        font = QFont("Segoe UI", 9)
        p.setFont(font)

        import datetime as _dt
        now_s = (_dt.datetime.utcnow().hour * 3600 +
                 _dt.datetime.utcnow().minute * 60)

        for hour in range(24):
            # Hour label
            y = hour * self.ROW_H + 2
            p.setPen(QColor(C["mu"]))
            p.drawText(QRect(0, y, self.LABEL_W - 6, self.BLOCK_H),
                       Qt.AlignRight | Qt.AlignVCenter, f"{hour:02d}:00")

            for slot in range(12):
                start_s = hour * 3600 + slot * SEG_SECS
                rect = self._block_rect(hour, slot)
                seg = self._segments.get(start_s)

                if seg:
                    color = _seg_color(seg)
                elif start_s > now_s:
                    color = C["seg_future"]
                else:
                    color = C["seg_none"]

                p.setPen(Qt.NoPen)
                p.setBrush(QColor(color))
                p.drawRoundedRect(rect, 3, 3)

                # Selection / playing outlines
                if start_s == self._playing_s:
                    p.setPen(QPen(QColor(C["ok"]), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 3, 3)
                elif start_s == self._selected_s:
                    p.setPen(QPen(QColor(255, 255, 255), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 3, 3)

        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            for hour in range(24):
                for slot in range(12):
                    rect = self._block_rect(hour, slot)
                    if rect.contains(pos):
                        start_s = hour * 3600 + slot * SEG_SECS
                        seg = self._segments.get(start_s)
                        if seg:
                            self._selected_s = start_s
                            self.update()
                            self.segment_clicked.emit(seg)
                        return

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        for hour in range(24):
            for slot in range(12):
                rect = self._block_rect(hour, slot)
                if rect.contains(pos):
                    start_s = hour * 3600 + slot * SEG_SECS
                    seg = self._segments.get(start_s)
                    if seg:
                        tip = (f"{_fmt_time(start_s)} — "
                               f"silence: {seg.get('silence_pct', 0):.0f}%  "
                               f"quality: {seg.get('quality', 'high')}")
                    else:
                        tip = f"{_fmt_time(start_s)} — no recording"
                    QToolTip.showText(event.globalPosition().toPoint(), tip, self)
                    return


class MetaBand(QWidget):
    """Horizontal band showing metadata spans (tracks, shows, or mics)."""

    def __init__(self, band_type: str = "track"):
        super().__init__()
        self._type = band_type
        self._events = []
        h = 16 if band_type == "mic" else 22
        self.setFixedHeight(h)

    def set_events(self, events: list):
        self._events = [e for e in events if e.get("type") == self._type]
        self.update()

    def paintEvent(self, event):
        if not self._events:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        colors = {
            "track": (QColor(180, 83, 9, 50), QColor(251, 191, 36, 200),
                      QColor("#fcd34d")),
            "show":  (QColor(109, 40, 217, 40), QColor(167, 139, 250, 180),
                      QColor("#c4b5fd")),
            "mic":   (QColor(16, 185, 129, 65), QColor(52, 211, 153, 230),
                      QColor("#6ee7b7")),
        }
        bg_col, border_col, text_col = colors.get(self._type, colors["track"])

        font = QFont("Segoe UI", 8)
        p.setFont(font)

        for i, ev in enumerate(self._events):
            ts = ev.get("ts_s", 0)
            # Duration: until next event of same type, or +300s
            next_ts = (self._events[i + 1]["ts_s"]
                       if i + 1 < len(self._events) else ts + 300)
            x1 = int(ts / 86400 * w)
            x2 = int(next_ts / 86400 * w)
            sw = max(2, x2 - x1)

            p.fillRect(x1, 1, sw, h - 2, bg_col)
            p.setPen(QPen(border_col, 2))
            p.drawLine(x1, 1, x1, h - 1)

            # Label
            label = ev.get("title") or ev.get("show_name") or ""
            if ev.get("artist"):
                label = f"{ev['artist']} — {label}" if label else ev["artist"]
            if label and sw > 30:
                p.setPen(text_col)
                p.drawText(QRect(x1 + 4, 0, sw - 6, h),
                           Qt.AlignLeft | Qt.AlignVCenter, label)

        p.end()


# ─── Styled push button helper ────────────────────────────────────────────────

def _make_btn(text: str, color: str = None, small: bool = False) -> QPushButton:
    btn = QPushButton(text)
    bg = color or C["sur"]
    pad = "3px 9px" if small else "5px 12px"
    fs = "12px" if small else "13px"
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {bg}; color: {C['tx']}; border: 1px solid {C['bor']};
            border-radius: 5px; padding: {pad}; font-size: {fs};
        }}
        QPushButton:hover {{ background: {C['input_bg']}; }}
        QPushButton:disabled {{ opacity: 0.4; }}
    """)
    return btn


# ─── Connection Dialog ────────────────────────────────────────────────────────

class ConnectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SignalScope Player — Connect")
        self.setFixedSize(500, 430)
        self.setStyleSheet(f"""
            QDialog {{ background: {C['bg']}; color: {C['tx']}; }}
            QLabel {{ color: {C['tx']}; font-size: 13px; }}
            QLineEdit {{
                background: {C['input_bg']}; color: {C['tx']};
                border: 1px solid {C['bor']}; border-radius: 5px;
                padding: 7px 9px; font-size: 13px;
            }}
            QTabWidget::pane {{
                border: 1px solid {C['bor']}; background: {C['sur']};
                border-radius: 4px;
            }}
            QTabBar::tab {{
                background: {C['sur']}; color: {C['mu']}; padding: 8px 18px;
                border: 1px solid {C['bor']}; border-bottom: none;
                border-top-left-radius: 5px; border-top-right-radius: 5px;
            }}
            QTabBar::tab:selected {{
                background: {C['bg']}; color: {C['acc']}; font-weight: bold;
            }}
        """)

        self.data_source = None
        settings = _load_settings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # Logo / title
        logo_path = _asset("signalscope_logo.jpg")
        if logo_path:
            from PySide6.QtGui import QPixmap
            logo_lbl = QLabel()
            px = QPixmap(logo_path).scaledToHeight(130, Qt.SmoothTransformation)
            logo_lbl.setPixmap(px)
            logo_lbl.setAlignment(Qt.AlignCenter)
            layout.addWidget(logo_lbl)
            sub = QLabel("Player")
            sub.setStyleSheet(f"font-size: 13px; color: {C['mu']};")
            sub.setAlignment(Qt.AlignCenter)
            layout.addWidget(sub)
        else:
            title = QLabel("SignalScope Player")
            title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {C['acc']};")
            title.setAlignment(Qt.AlignCenter)
            layout.addWidget(title)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Hub tab ──
        hub_w = QWidget()
        hub_l = QVBoxLayout(hub_w)
        hub_l.setContentsMargins(12, 12, 12, 12)
        hub_l.setSpacing(8)

        hub_l.addWidget(QLabel("Hub URL"))
        self._hub_url = QLineEdit(settings.get("hub_url", ""))
        self._hub_url.setPlaceholderText("https://hub.example.com")
        hub_l.addWidget(self._hub_url)

        hub_l.addWidget(QLabel("API Token"))
        self._hub_token = QLineEdit(settings.get("hub_token", ""))
        self._hub_token.setPlaceholderText("Bearer token from Settings → Mobile API")
        self._hub_token.setEchoMode(QLineEdit.Password)
        hub_l.addWidget(self._hub_token)

        hub_btn = _make_btn("Connect", C["ok"])
        hub_btn.clicked.connect(self._connect_hub)
        hub_l.addWidget(hub_btn)
        hub_l.addStretch()
        tabs.addTab(hub_w, "Hub")

        # ── Direct tab ──
        dir_w = QWidget()
        dir_l = QVBoxLayout(dir_w)
        dir_l.setContentsMargins(12, 12, 12, 12)
        dir_l.setSpacing(8)

        dir_l.addWidget(QLabel("Recordings Directory"))
        path_row = QHBoxLayout()
        self._dir_path = QLineEdit(settings.get("dir_path", ""))
        self._dir_path.setPlaceholderText("/media/storage/logger_recordings")
        path_row.addWidget(self._dir_path)
        browse_btn = _make_btn("Browse…", small=True)
        browse_btn.clicked.connect(self._browse_dir)
        path_row.addWidget(browse_btn)
        dir_l.addLayout(path_row)

        dir_btn = _make_btn("Open", C["ok"])
        dir_btn.clicked.connect(self._open_direct)
        dir_l.addWidget(dir_btn)
        dir_l.addStretch()
        tabs.addTab(dir_w, "Direct")

        # Status
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {C['al']}; font-size: 11px;")
        self._status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._status)

        # Restore last tab
        if settings.get("last_mode") == "direct":
            tabs.setCurrentIndex(1)

    def _connect_hub(self):
        url = self._hub_url.text().strip()
        token = self._hub_token.text().strip()
        if not url or not token:
            self._status.setText("Enter both URL and token")
            return
        self._status.setText("Connecting…")
        self._status.setStyleSheet(f"color: {C['mu']}; font-size: 11px;")
        QApplication.processEvents()
        try:
            ds = HubDataSource(url, token)
            cat = ds.catalog()
            self.data_source = ds
            _save_settings({
                "hub_url": url, "hub_token": token,
                "dir_path": self._dir_path.text().strip(),
                "last_mode": "hub",
            })
            self.accept()
        except Exception as e:
            self._status.setText(f"Connection failed: {e}")
            self._status.setStyleSheet(f"color: {C['al']}; font-size: 11px;")

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Recordings Directory",
                                             self._dir_path.text())
        if d:
            self._dir_path.setText(d)

    def _open_direct(self):
        path = self._dir_path.text().strip()
        if not path or not Path(path).is_dir():
            self._status.setText("Select a valid directory")
            return
        self.data_source = DirectDataSource(path)
        _save_settings({
            "hub_url": self._hub_url.text().strip(),
            "hub_token": self._hub_token.text().strip(),
            "dir_path": path,
            "last_mode": "direct",
        })
        self.accept()


# ─── About Dialog ─────────────────────────────────────────────────────────────

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About SignalScope Player")
        self.setFixedSize(460, 360)
        self.setStyleSheet(f"""
            QDialog   {{ background: {C['bg']}; }}
            QWidget   {{ background: {C['bg']}; color: {C['tx']}; }}
            QLabel    {{ background: transparent; color: {C['tx']}; }}
            QPushButton {{
                background: {C['sur']}; color: {C['tx']};
                border: 1px solid {C['bor']}; border-radius: 5px;
                padding: 6px 18px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {C['acc']}; color: #fff; }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.setSpacing(10)

        # Logo banner
        logo_path = _asset("signalscope_logo.jpg")
        if logo_path:
            from PySide6.QtGui import QPixmap
            logo_lbl = QLabel()
            px = QPixmap(logo_path).scaledToHeight(120, Qt.SmoothTransformation)
            logo_lbl.setPixmap(px)
            logo_lbl.setAlignment(Qt.AlignCenter)
            lay.addWidget(logo_lbl)
        else:
            name_lbl = QLabel("SignalScope Player")
            name_lbl.setStyleSheet(
                f"font-size: 18px; font-weight: bold; color: {C['acc']};")
            name_lbl.setAlignment(Qt.AlignCenter)
            lay.addWidget(name_lbl)

        ver_lbl = QLabel(f"Player  ·  Version {__version__}")
        ver_lbl.setStyleSheet(f"font-size: 12px; color: {C['mu']};")
        ver_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(ver_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {C['bor']};")
        lay.addWidget(sep)

        desc = QLabel(
            "Desktop playback client for SignalScope compliance logger "
            "recordings.\nBrowse streams, navigate timelines, and export "
            "clips from any hub or\nlocal/network recording directory."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"font-size: 12px; color: {C['mu']}; line-height: 1.5;")
        lay.addWidget(desc)

        # GitHub link
        gh_lbl = QLabel(
            '<a href="https://github.com/itconor/SignalScopePlayer" '
            f'style="color:{C["acc"]}; text-decoration:none;">'
            "github.com/itconor/SignalScopePlayer</a>"
        )
        gh_lbl.setOpenExternalLinks(True)
        gh_lbl.setStyleSheet("font-size: 12px;")
        lay.addWidget(gh_lbl)

        credits = QLabel("Built with Python · PySide6 / Qt · ffmpeg")
        credits.setStyleSheet(f"font-size: 11px; color: {C['mu']};")
        lay.addWidget(credits)

        lay.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setDefault(True)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)


# ─── Main Window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, ds: DataSource):
        super().__init__()
        self._ds = ds
        self._current_slug = ""
        self._current_site = ""
        self._current_date = ""
        self._segments = []
        self._meta = []
        self._playing_seg = None
        self._workers = []
        self._mark_in = -1
        self._mark_out = -1

        self.setWindowTitle(f"SignalScope Player — {ds.mode()} mode")
        self.setMinimumSize(900, 600)
        self.resize(1100, 720)

        self._setup_style()
        self._build_ui()
        self._build_menu()
        self._setup_audio()
        self._load_catalog()

    def _setup_style(self):
        self.setStyleSheet(f"""
            QMainWindow {{ background: {C['bg']}; }}
            QWidget {{ background: {C['bg']}; color: {C['tx']}; }}
            QLabel {{ color: {C['tx']}; }}
            QListWidget {{
                background: {C['sur']}; color: {C['tx']};
                border: 1px solid {C['bor']}; border-radius: 5px;
                font-size: 13px; outline: none;
            }}
            QListWidget::item {{
                padding: 6px 10px; border-radius: 3px;
            }}
            QListWidget::item:selected {{
                background: {C['acc']}; color: #fff;
            }}
            QListWidget::item:hover {{
                background: rgba(23,168,255,0.15);
            }}
            QScrollArea {{ border: none; background: {C['bg']}; }}
            QScrollBar:vertical {{
                background: {C['sur']}; width: 10px; border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: {C['bor']}; border-radius: 5px; min-height: 30px;
            }}
            QSlider::groove:horizontal {{
                background: {C['seg_none']}; height: 4px; border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {C['ok']}; width: 14px; height: 14px;
                margin: -5px 0; border-radius: 7px;
            }}
            QSlider::sub-page:horizontal {{
                background: {C['ok']}; border-radius: 2px;
            }}
            QComboBox {{
                background: {C['input_bg']}; color: {C['tx']};
                border: 1px solid {C['bor']}; border-radius: 5px;
                padding: 5px 8px; font-size: 12px;
            }}
            QComboBox::drop-down {{
                border: none; width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background: {C['sur']}; color: {C['tx']};
                border: 1px solid {C['bor']}; selection-background-color: {C['acc']};
            }}
        """)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Header ──
        header = QWidget()
        header.setFixedHeight(46)
        header.setStyleSheet(f"""
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 rgba(10,31,65,240), stop:1 rgba(9,24,48,240));
            border-bottom: 1px solid {C['bor']};
        """)
        hdr_l = QHBoxLayout(header)
        hdr_l.setContentsMargins(14, 0, 14, 0)

        icon_path = _asset("signalscope_icon.png")
        if icon_path:
            from PySide6.QtGui import QPixmap
            icon_lbl = QLabel()
            icon_lbl.setPixmap(
                QPixmap(icon_path).scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            icon_lbl.setStyleSheet("background: transparent;")
            hdr_l.addWidget(icon_lbl)
        else:
            logo = QLabel("🎙")
            logo.setStyleSheet("font-size: 20px; background: transparent;")
            hdr_l.addWidget(logo)

        title = QLabel("SignalScope Player")
        title.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {C['tx']}; background: transparent;")
        hdr_l.addWidget(title)

        self._conn_label = QLabel(
            f"● {self._ds.mode().title()} mode")
        self._conn_label.setStyleSheet(
            f"font-size: 11px; color: {C['ok']}; background: transparent;")
        hdr_l.addStretch()
        hdr_l.addWidget(self._conn_label)

        main_layout.addWidget(header)

        # ── Body splitter ──
        body = QSplitter(Qt.Horizontal)
        body.setHandleWidth(1)
        body.setStyleSheet(f"QSplitter::handle {{ background: {C['bor']}; }}")
        main_layout.addWidget(body, 1)

        # ── Sidebar ──
        sidebar = QWidget()
        sidebar.setFixedWidth(210)
        sidebar.setStyleSheet(f"""
            QWidget {{ background: {C['sur']}; }}
        """)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(10, 10, 10, 10)
        sb_layout.setSpacing(8)

        sb_layout.addWidget(QLabel("Streams"))
        self._stream_list = QListWidget()
        self._stream_list.currentItemChanged.connect(self._on_stream_selected)
        sb_layout.addWidget(self._stream_list, 1)

        sb_layout.addWidget(QLabel("Dates"))
        self._date_list = QListWidget()
        self._date_list.currentItemChanged.connect(self._on_date_selected)
        sb_layout.addWidget(self._date_list, 1)

        # Legend
        legend = QWidget()
        legend.setStyleSheet(f"background: {C['sur']};")
        leg_l = QHBoxLayout(legend)
        leg_l.setContentsMargins(0, 4, 0, 4)
        leg_l.setSpacing(6)
        for label, col in [("OK", C["seg_ok"]), ("Warn", C["seg_warn"]),
                           ("Silent", C["seg_silent"]), ("None", C["seg_none"])]:
            dot = QLabel("■")
            dot.setStyleSheet(f"color: {col}; font-size: 10px; background: transparent;")
            leg_l.addWidget(dot)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"font-size: 10px; color: {C['mu']}; background: transparent;")
            leg_l.addWidget(lbl)
        leg_l.addStretch()
        sb_layout.addWidget(legend)

        body.addWidget(sidebar)

        # ── Content panel ──
        content = QWidget()
        content_l = QVBoxLayout(content)
        content_l.setContentsMargins(0, 0, 0, 0)
        content_l.setSpacing(0)

        # Day bar
        daybar_wrap = QWidget()
        daybar_wrap.setStyleSheet(f"background: {C['bg']};")
        db_l = QVBoxLayout(daybar_wrap)
        db_l.setContentsMargins(12, 8, 12, 4)
        self._daybar = DayBar()
        self._daybar.clicked.connect(self._on_daybar_click)
        db_l.addWidget(self._daybar)

        # Meta bands
        self._track_band = MetaBand("track")
        self._show_band = MetaBand("show")
        self._mic_band = MetaBand("mic")
        db_l.addWidget(self._show_band)
        db_l.addWidget(self._track_band)
        db_l.addWidget(self._mic_band)
        content_l.addWidget(daybar_wrap)

        # Segment grid (scrollable)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._seg_grid = SegmentGrid()
        self._seg_grid.segment_clicked.connect(self._on_segment_clicked)
        scroll.setWidget(self._seg_grid)
        content_l.addWidget(scroll, 1)

        # ── Player bar ──
        player_wrap = QWidget()
        player_wrap.setStyleSheet(f"""
            QWidget {{
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #143766, stop:1 #102b54);
                border-top: 1px solid {C['bor']};
            }}
        """)
        player_l = QVBoxLayout(player_wrap)
        player_l.setContentsMargins(14, 10, 14, 10)
        player_l.setSpacing(6)

        # Top row: play + info + time
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedSize(36, 36)
        self._play_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C['ok']}; color: white; border: none;
                border-radius: 18px; font-size: 16px; font-weight: bold;
            }}
            QPushButton:hover {{ background: #2dd96d; }}
            QPushButton:disabled {{ background: #555; }}
        """)
        self._play_btn.clicked.connect(self._toggle_play)
        self._play_btn.setEnabled(False)
        top_row.addWidget(self._play_btn)

        info_col = QVBoxLayout()
        info_col.setSpacing(1)
        self._p_title = QLabel("—")
        self._p_title.setStyleSheet(
            f"font-weight: bold; font-size: 13px; color: {C['tx']}; background: transparent;")
        info_col.addWidget(self._p_title)
        self._p_sub = QLabel("")
        self._p_sub.setStyleSheet(
            f"font-size: 11px; color: {C['mu']}; background: transparent;")
        info_col.addWidget(self._p_sub)
        top_row.addLayout(info_col)

        top_row.addStretch()

        self._time_label = QLabel("00:00:00")
        self._time_label.setStyleSheet(f"""
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 12px; color: {C['mu']}; background: transparent;
        """)
        top_row.addWidget(self._time_label)
        player_l.addLayout(top_row)

        # Scrub bar
        self._scrub = QSlider(Qt.Horizontal)
        self._scrub.setRange(0, SEG_SECS)
        self._scrub.setValue(0)
        self._scrub.sliderReleased.connect(self._on_scrub_seek)
        player_l.addWidget(self._scrub)

        # Export row
        export_row = QHBoxLayout()
        export_row.setSpacing(6)

        self._mark_in_btn = _make_btn("⬥ Mark In", small=True)
        self._mark_in_btn.clicked.connect(self._do_mark_in)
        export_row.addWidget(self._mark_in_btn)

        self._mark_out_btn = _make_btn("⬥ Mark Out", small=True)
        self._mark_out_btn.clicked.connect(self._do_mark_out)
        export_row.addWidget(self._mark_out_btn)

        self._inout_label = QLabel("")
        self._inout_label.setStyleSheet(
            f"color: {C['acc']}; font-size: 12px; font-weight: 600; background: transparent;")
        export_row.addWidget(self._inout_label)

        export_row.addStretch()

        self._export_fmt = QComboBox()
        self._export_fmt.addItems(["MP3", "AAC", "Opus"])
        export_row.addWidget(self._export_fmt)

        self._export_btn = _make_btn("⬇ Export Clip", C["acc"], small=True)
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._do_export)
        export_row.addWidget(self._export_btn)

        player_l.addLayout(export_row)
        content_l.addWidget(player_wrap)

        body.addWidget(content)
        body.setSizes([210, 890])

        # ── Playback timer ──
        self._play_timer = QTimer()
        self._play_timer.setInterval(200)
        self._play_timer.timeout.connect(self._update_playback_position)

    def _build_menu(self):
        from PySide6.QtGui import QKeySequence
        mb = self.menuBar()
        mb.setStyleSheet(f"""
            QMenuBar {{
                background: {C['sur']}; color: {C['tx']};
                border-bottom: 1px solid {C['bor']}; font-size: 13px;
            }}
            QMenuBar::item {{ background: transparent; padding: 4px 10px; }}
            QMenuBar::item:selected {{ background: {C['acc']}; color: #fff; border-radius: 3px; }}
            QMenu {{
                background: {C['sur']}; color: {C['tx']};
                border: 1px solid {C['bor']}; border-radius: 4px; font-size: 13px;
            }}
            QMenu::item {{ padding: 6px 24px; }}
            QMenu::item:selected {{ background: {C['acc']}; color: #fff; }}
            QMenu::separator {{ height: 1px; background: {C['bor']}; margin: 3px 8px; }}
        """)

        # File menu
        file_menu = mb.addMenu("File")
        reconnect_act = QAction("Reconnect / Change Source…", self)
        reconnect_act.setShortcut(QKeySequence("Ctrl+R"))
        reconnect_act.triggered.connect(self._reconnect)
        file_menu.addAction(reconnect_act)
        file_menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.setShortcut(QKeySequence.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # Playback menu
        play_menu = mb.addMenu("Playback")
        play_act = QAction("Play / Pause", self)
        play_act.setShortcut(QKeySequence(Qt.Key_Space))
        play_act.triggered.connect(self._toggle_play)
        play_menu.addAction(play_act)

        # Help menu
        help_menu = mb.addMenu("Help")
        about_act = QAction("About SignalScope Player…", self)
        about_act.triggered.connect(lambda: AboutDialog(self).exec())
        help_menu.addAction(about_act)
        gh_act = QAction("View on GitHub…", self)
        gh_act.triggered.connect(lambda: QUrl("https://github.com/itconor/SignalScopePlayer"))
        from PySide6.QtGui import QDesktopServices
        gh_act.triggered.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/itconor/SignalScopePlayer")))
        help_menu.addAction(gh_act)

    def _reconnect(self):
        self._stop_playback()
        dlg = ConnectionDialog()
        if dlg.exec() == QDialog.Accepted and dlg.data_source is not None:
            self._ds = dlg.data_source
            self._current_slug = ""
            self._current_site = ""
            self._current_date = ""
            self._segments = []
            self._meta = []
            self._playing_seg = None
            self._mark_in = -1
            self._mark_out = -1
            self.setWindowTitle(f"SignalScope Player — {self._ds.mode()} mode")
            self._conn_label.setText(f"● {self._ds.mode().title()} mode")
            self._conn_label.setStyleSheet(
                f"font-size: 11px; color: {C['ok']}; background: transparent;")
            self._stream_list.clear()
            self._date_list.clear()
            self._seg_grid.set_segments([])
            self._daybar.set_segments([], 0)
            self._track_band.set_events([])
            self._show_band.set_events([])
            self._mic_band.set_events([])
            self._load_catalog()

    def _setup_audio(self):
        self._player = QMediaPlayer()
        self._audio_out = QAudioOutput()
        self._audio_out.setVolume(1.0)
        self._player.setAudioOutput(self._audio_out)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.errorOccurred.connect(self._on_player_error)

    # ── Data loading ──────────────────────────────────────────────────────────

    def _fetch(self, name: str, func, *args):
        w = FetchWorker(name, func, *args)
        w.finished.connect(self._on_fetch_done)
        w.error.connect(self._on_fetch_error)
        self._workers.append(w)
        w.start()

    @Slot(str, object)
    def _on_fetch_done(self, task: str, result):
        if task == "catalog":
            self._populate_streams(result)
        elif task == "days":
            self._populate_dates(result)
        elif task == "segments":
            self._populate_segments(result)
        elif task == "metadata":
            self._apply_metadata(result)
        # Cleanup finished workers
        self._workers = [w for w in self._workers if w.isRunning()]

    @Slot(str, str)
    def _on_fetch_error(self, task: str, error: str):
        self._conn_label.setText(f"● Error: {error}")
        self._conn_label.setStyleSheet(
            f"font-size: 11px; color: {C['al']}; background: transparent;")
        self._workers = [w for w in self._workers if w.isRunning()]

    def _load_catalog(self):
        self._fetch("catalog", self._ds.catalog)

    def _populate_streams(self, catalog: list):
        self._stream_list.clear()
        for entry in catalog:
            name = entry.get("name", entry.get("slug", "?"))
            site = entry.get("site", "")
            label = f"{name}  ({site})" if site else name
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, entry)
            self._stream_list.addItem(item)

    def _on_stream_selected(self, current, previous):
        if not current:
            return
        entry = current.data(Qt.UserRole)
        self._current_slug = entry.get("slug", "")
        self._current_site = entry.get("site", "")
        self._current_date = ""
        self._segments = []
        self._seg_grid.set_segments([])
        self._daybar.set_segments([])
        self._date_list.clear()
        self._stop_playback()
        self._fetch("days", self._ds.days, self._current_slug, self._current_site)

    def _populate_dates(self, days: list):
        self._date_list.clear()
        for d in days:
            self._date_list.addItem(d)

    def _on_date_selected(self, current, previous):
        if not current:
            return
        self._current_date = current.text()
        self._stop_playback()
        self._fetch("segments", self._ds.segments,
                    self._current_slug, self._current_date, self._current_site)
        self._fetch("metadata", self._ds.metadata,
                    self._current_slug, self._current_date, self._current_site)

    def _populate_segments(self, segs: list):
        self._segments = segs
        self._seg_grid.set_segments(segs)
        self._daybar.set_segments(segs)

    def _apply_metadata(self, events: list):
        self._meta = events
        self._track_band.set_events(events)
        self._show_band.set_events(events)
        self._mic_band.set_events(events)

    # ── Playback ──────────────────────────────────────────────────────────────

    def _on_segment_clicked(self, seg: dict):
        self._play_segment(seg)

    def _on_daybar_click(self, secs: int):
        # Find the segment at this time
        target_start = (secs // SEG_SECS) * SEG_SECS
        for seg in self._segments:
            if int(seg.get("start_s", -1)) == target_start:
                self._play_segment(seg)
                return

    def _play_segment(self, seg: dict, seek_s: float = 0):
        self._stop_playback()
        self._playing_seg = seg
        start_s = seg.get("start_s", 0)
        filename = seg.get("filename", "")

        self._seg_grid.set_playing(int(start_s))
        self._p_title.setText(f"{_fmt_time(start_s)}  —  {filename}")
        self._p_sub.setText(f"{self._current_slug} · {self._current_date}")
        self._play_btn.setEnabled(True)

        url = self._ds.audio_url(
            self._current_slug, self._current_date, filename, seek_s)

        if self._ds.mode() == "direct":
            self._player.setSource(QUrl.fromLocalFile(url))
        else:
            self._player.setSource(QUrl(url))

        self._player.play()
        self._play_btn.setText("⏸")
        self._play_timer.start()

    def _toggle_play(self):
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
            self._play_btn.setText("▶")
            self._play_timer.stop()
        elif self._playing_seg:
            self._player.play()
            self._play_btn.setText("⏸")
            self._play_timer.start()

    def _stop_playback(self):
        self._player.stop()
        self._play_btn.setText("▶")
        self._play_btn.setEnabled(False)
        self._play_timer.stop()
        self._playing_seg = None
        self._seg_grid.set_playing(-1)
        self._scrub.setValue(0)
        self._time_label.setText("00:00:00")

    def _on_media_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            # Auto-advance to next segment
            if self._playing_seg and self._segments:
                cur_s = int(self._playing_seg.get("start_s", -1))
                for seg in self._segments:
                    if int(seg.get("start_s", 0)) == cur_s + SEG_SECS:
                        self._play_segment(seg)
                        return
            self._stop_playback()

    def _on_position_changed(self, pos_ms):
        if not self._playing_seg:
            return
        pos_s = pos_ms / 1000.0
        self._scrub.blockSignals(True)
        self._scrub.setValue(int(pos_s))
        self._scrub.blockSignals(False)
        abs_s = self._playing_seg.get("start_s", 0) + pos_s
        self._time_label.setText(_fmt_time(abs_s))
        self._daybar.set_head(abs_s)

    def _on_player_error(self, error, msg=""):
        self._p_sub.setText(f"Playback error: {error}")

    def _on_scrub_seek(self):
        if self._playing_seg and self._ds.mode() == "direct":
            pos_ms = self._scrub.value() * 1000
            self._player.setPosition(pos_ms)
        elif self._playing_seg and self._ds.mode() == "hub":
            # Hub mode: restart with seek offset
            self._play_segment(self._playing_seg, seek_s=self._scrub.value())

    def _update_playback_position(self):
        # Handled by _on_position_changed for direct mode
        pass

    # ── Mark In/Out & Export ──────────────────────────────────────────────────

    def _current_abs_time(self) -> float:
        if not self._playing_seg:
            return -1
        return self._playing_seg.get("start_s", 0) + self._scrub.value()

    def _do_mark_in(self):
        t = self._current_abs_time()
        if t >= 0:
            self._mark_in = t
            self._update_marks()

    def _do_mark_out(self):
        t = self._current_abs_time()
        if t >= 0:
            self._mark_out = t
            self._update_marks()

    def _update_marks(self):
        parts = []
        if self._mark_in >= 0:
            parts.append(f"In: {_fmt_time(self._mark_in)}")
        if self._mark_out >= 0:
            parts.append(f"Out: {_fmt_time(self._mark_out)}")
        self._inout_label.setText("  —  ".join(parts))
        self._daybar.set_marks(self._mark_in, self._mark_out)
        can_export = (self._mark_in >= 0 and self._mark_out > self._mark_in
                      and self._ds.mode() == "direct")
        self._export_btn.setEnabled(can_export)

    def _do_export(self):
        if self._ds.mode() != "direct":
            return
        if self._mark_in < 0 or self._mark_out <= self._mark_in:
            return

        fmt = self._export_fmt.currentText().lower()
        ext = {"mp3": "mp3", "aac": "m4a", "opus": "ogg"}.get(fmt, "mp3")
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Export Clip",
            f"{self._current_slug}_{self._current_date}_{_fmt_time(self._mark_in).replace(':','-')}.{ext}",
            f"Audio (*.{ext})")
        if not save_path:
            return

        # Collect segments in range
        ffmpeg = "ffmpeg"
        files = []
        for seg in self._segments:
            ss = seg.get("start_s", 0)
            se = ss + SEG_SECS
            if se > self._mark_in and ss < self._mark_out:
                path = self._ds.audio_url(
                    self._current_slug, self._current_date, seg["filename"])
                if Path(path).exists():
                    files.append(path)

        if not files:
            QMessageBox.warning(self, "Export", "No audio files in selected range.")
            return

        # Build ffmpeg concat + trim
        try:
            list_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False)
            for f in files:
                list_file.write(f"file '{f}'\n")
            list_file.close()

            first_seg_start = min(s.get("start_s", 0) for s in self._segments
                                  if s["filename"] == Path(files[0]).name)
            offset_in = self._mark_in - first_seg_start
            duration = self._mark_out - self._mark_in

            cmd = [ffmpeg, "-hide_banner", "-y",
                   "-f", "concat", "-safe", "0", "-i", list_file.name,
                   "-ss", str(max(0, offset_in)), "-t", str(duration)]
            if fmt == "mp3":
                cmd += ["-c:a", "libmp3lame", "-b:a", "192k"]
            elif fmt == "aac":
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            elif fmt == "opus":
                cmd += ["-c:a", "libopus", "-b:a", "128k"]
            cmd.append(save_path)

            subprocess.run(cmd, check=True, capture_output=True)
            self._p_sub.setText(f"Exported: {Path(save_path).name}")
        except Exception as e:
            QMessageBox.warning(self, "Export Failed", str(e))
        finally:
            try:
                os.unlink(list_file.name)
            except Exception:
                pass

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._stop_playback()
        for w in self._workers:
            w.quit()
            w.wait(1000)
        event.accept()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SignalScope Player")
    app.setStyle("Fusion")

    icon_path = _asset("signalscope_icon.ico") or _asset("signalscope_icon.png")
    if icon_path:
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(icon_path))

    # Dark palette
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(C["bg"]))
    palette.setColor(QPalette.WindowText, QColor(C["tx"]))
    palette.setColor(QPalette.Base, QColor(C["sur"]))
    palette.setColor(QPalette.AlternateBase, QColor(C["bg"]))
    palette.setColor(QPalette.ToolTipBase, QColor(C["sur"]))
    palette.setColor(QPalette.ToolTipText, QColor(C["tx"]))
    palette.setColor(QPalette.Text, QColor(C["tx"]))
    palette.setColor(QPalette.Button, QColor(C["sur"]))
    palette.setColor(QPalette.ButtonText, QColor(C["tx"]))
    palette.setColor(QPalette.Link, QColor(C["acc"]))
    palette.setColor(QPalette.Highlight, QColor(C["acc"]))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    dlg = ConnectionDialog()
    if dlg.exec() != QDialog.Accepted or dlg.data_source is None:
        sys.exit(0)

    win = MainWindow(dlg.data_source)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

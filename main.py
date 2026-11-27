# -*- coding: utf-8 -*-
"""
NAS – Bild-Zuspieler für grandMA3
---------------------------
Zeigt Bilder im Vollbild auf einem Beamer. Jedes Bild ist ein Dimmer-Kanal,
der per Art-Net oder sACN von der grandMA3 gesteuert wird.
Kanalwert = Sichtbarkeit, Überblendungen kommen aus den Cue-Fade-Zeiten.
"""

APP_NAME = "NAS"
APP_VERSION = "0.2.1"

import json
import math
import os
import re
import shutil
import socket
import struct
import sys
import threading
import time
import uuid

# =============================================================================
# PROTOKOLL (Art-Net / sACN) – ohne Qt, damit es separat testbar ist
# --- PROTOCOL-START ---
# =============================================================================

ARTNET_PORT = 6454
SACN_PORT = 5568
ARTNET_ID = b"Art-Net\x00"
SACN_ID = b"ASC-E1.17\x00\x00\x00"


def parse_artnet(pkt):
    """ArtDmx-Paket -> (port_address 0..32767, dmx_bytes) oder None."""
    if len(pkt) < 18 or pkt[:8] != ARTNET_ID:
        return None
    opcode = struct.unpack_from("<H", pkt, 8)[0]
    if opcode != 0x5000:  # OpDmx
        return None
    port = pkt[14] | ((pkt[15] & 0x7F) << 8)  # SubUni + Net
    length = struct.unpack_from(">H", pkt, 16)[0]
    length = max(0, min(length, 512, len(pkt) - 18))
    return port, bytes(pkt[18:18 + length])


def parse_sacn(pkt):
    """E1.31-Datenpaket -> (universe, dmx_bytes, priority, options, cid) oder None."""
    if len(pkt) < 126 or pkt[4:16] != SACN_ID:
        return None
    if struct.unpack_from(">I", pkt, 18)[0] != 0x00000004:  # Root: E1.31 Data
        return None
    if struct.unpack_from(">I", pkt, 40)[0] != 0x00000002:  # Framing: Data
        return None
    if pkt[117] != 0x02:  # DMP: Set Property
        return None
    if pkt[125] != 0x00:  # nur Startcode 0 (normale DMX-Werte)
        return None
    cid = bytes(pkt[22:38])
    priority = pkt[108]
    options = pkt[112]
    universe = struct.unpack_from(">H", pkt, 113)[0]
    count = struct.unpack_from(">H", pkt, 123)[0] - 1
    count = max(0, min(count, 512, len(pkt) - 126))
    return universe, bytes(pkt[126:126 + count]), priority, options, cid


def build_artnet(port, data, seq=0):
    """Testpaket ArtDmx bauen (für Selbsttest)."""
    data = bytes(data[:512])
    if len(data) % 2:
        data += b"\x00"
    return (ARTNET_ID + struct.pack("<H", 0x5000) + struct.pack(">H", 14)
            + bytes([seq & 0xFF, 0, port & 0xFF, (port >> 8) & 0x7F])
            + struct.pack(">H", len(data)) + data)


def build_sacn(universe, data, priority=100, seq=0, cid=b"\x01" * 16, name="Test"):
    """Testpaket E1.31 bauen (für Selbsttest)."""
    data = bytes(data[:512])
    n = len(data) + 1
    dmp_len = 10 + n
    framing_len = 77 + dmp_len
    root_len = 22 + framing_len
    pkt = bytearray()
    pkt += struct.pack(">HH", 0x0010, 0x0000) + SACN_ID
    pkt += struct.pack(">H", 0x7000 | root_len) + struct.pack(">I", 0x00000004) + cid[:16]
    pkt += struct.pack(">H", 0x7000 | framing_len) + struct.pack(">I", 0x00000002)
    pkt += name.encode("utf-8")[:63].ljust(64, b"\x00")
    pkt += bytes([priority]) + struct.pack(">H", 0) + bytes([seq & 0xFF, 0]) + struct.pack(">H", universe)
    pkt += struct.pack(">H", 0x7000 | dmp_len) + bytes([0x02, 0xA1]) + struct.pack(">HHH", 0, 1, n)
    pkt += b"\x00" + data
    return bytes(pkt)


def detect_new_channel(baseline, snap, threshold=128):
    """Sucht den ersten Kanal, der seit baseline über die Schwelle gezogen wurde.
    baseline wird für neu auftauchende Universen ergänzt (deren Startwerte zählen nicht)."""
    for u in sorted(snap):
        if u not in baseline:
            baseline[u] = snap[u]
            continue
        cur, base = snap[u], baseline[u]
        for i in range(min(len(cur), 512)):
            if cur[i] >= threshold and base[i] < threshold:
                return u, i + 1, cur[i]
    return None


def sacn_group(universe):
    return "239.255.%d.%d" % ((universe >> 8) & 0xFF, universe & 0xFF)


def local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # Standard-Route ermitteln (es wird nichts gesendet)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips.discard("0.0.0.0")
    return sorted(ips, key=lambda ip: (ip.startswith("127."), ip))


class DmxReceiver:
    """Empfängt Art-Net oder sACN in einem eigenen Thread.
    Universen werden in grandMA3-Zählung gespeichert (1, 2, 3 …)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}      # universe -> bytearray(512)
        self.stats = {}     # universe -> {"count", "last", "src", "raw"}
        self.sources = {}   # sACN: universe -> {cid: (priority, last)}
        self.sock = None
        self.thread = None
        self.running = False
        self.error = ""
        self.protocol = "sacn"
        self.iface = "0.0.0.0"
        self.artnet_offset = 1
        self.joined = set()
        self.port_override = None

    def start(self, protocol, iface, universes, artnet_offset=1):
        self.stop()
        self.protocol = protocol
        self.iface = iface or "0.0.0.0"
        self.artnet_offset = int(artnet_offset)
        self.error = ""
        self.joined = set()
        with self.lock:
            self.data.clear()
            self.stats.clear()
            self.sources.clear()
        port = self.port_override or (ARTNET_PORT if protocol == "artnet" else SACN_PORT)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if protocol == "artnet":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except OSError:
                pass
            s.bind(("", port))
            s.settimeout(0.3)
        except OSError as e:
            self.error = "Port %d nicht verfügbar: %s" % (port, e)
            return False
        self.sock = s
        self.running = True
        if protocol == "sacn":
            self.update_universes(universes)
        self.thread = threading.Thread(target=self._loop, name="dmx-rx", daemon=True)
        self.thread.start()
        return True

    def update_universes(self, universes):
        if self.protocol != "sacn" or not self.sock:
            return
        if self.iface and self.iface != "0.0.0.0":
            ifaces = [self.iface]
        else:
            ifaces = [ip for ip in local_ipv4s() if not ip.startswith("127.")] or ["0.0.0.0"]
        errors = []
        for u in sorted(universes):
            if not 1 <= u <= 63999:
                continue
            for ip in ifaces:
                key = (u, ip)
                if key in self.joined:
                    continue
                try:
                    mreq = socket.inet_aton(sacn_group(u)) + socket.inet_aton(ip)
                    self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    self.joined.add(key)
                except OSError as e:
                    errors.append("Multicast U%d über %s: %s" % (u, ip, e))
        if errors and not self.joined:
            self.error = errors[0]

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(1.0)
        self.sock = None
        self.thread = None

    def _loop(self):
        sock = self.sock
        while self.running:
            try:
                pkt, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if not self.running:
                    break
                time.sleep(0.01)  # z. B. WSAECONNRESET unter Windows
                continue
            try:
                self._handle(pkt, addr[0])
            except Exception:
                pass

    def _handle(self, pkt, src):
        now = time.monotonic()
        if self.protocol == "artnet":
            r = parse_artnet(pkt)
            if not r:
                return
            port, data = r
            self._apply(port + self.artnet_offset, data, src, now, raw=port)
        else:
            r = parse_sacn(pkt)
            if not r:
                return
            u, data, prio, opts, cid = r
            if opts & 0x80:  # Preview-Daten ignorieren
                return
            with self.lock:
                srcs = self.sources.setdefault(u, {})
                if opts & 0x40:  # Stream beendet
                    srcs.pop(cid, None)
                    return
                srcs[cid] = (prio, now)
                active = [p for (p, t) in srcs.values() if now - t < 2.5]
                if active and prio < max(active):
                    return
            self._apply(u, data, src, now, raw=u)

    def _apply(self, u, data, src, now, raw=None):
        n = min(len(data), 512)
        with self.lock:
            buf = self.data.get(u)
            if buf is None:
                buf = bytearray(512)
                self.data[u] = buf
            buf[0:n] = data[0:n]
            st = self.stats.setdefault(u, {"count": 0})
            st["count"] += 1
            st["last"] = now
            st["src"] = src
            st["raw"] = raw

    def snapshot(self):
        with self.lock:
            return {u: bytes(b) for u, b in self.data.items()}

    def stats_copy(self):
        with self.lock:
            return {u: dict(s) for u, s in self.stats.items()}

# --- PROTOCOL-END ---

# =============================================================================
# Qt
# =============================================================================
from PySide6.QtCore import Qt, QTimer, QRect, QRectF, QPoint, QSize, Signal, QObject, QUrl
from PySide6.QtGui import (QPainter, QPixmap, QImage, QColor, QFont, QPen, QBrush, QIcon, QPolygon,
                           QKeySequence, QShortcut, QImageReader, QGuiApplication, QFontMetrics,
                           QDesktopServices)
from PySide6.QtWidgets import (QApplication, QWidget, QMainWindow, QLabel, QPushButton, QHBoxLayout,
                               QVBoxLayout, QStackedWidget, QButtonGroup, QScrollArea, QFileDialog,
                               QMessageBox, QMenu, QTableWidget, QTableWidgetItem, QHeaderView,
                               QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QFormLayout, QGroupBox,
                               QInputDialog, QToolTip, QAbstractItemView, QSizePolicy, QFrame,
                               QDialog)

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")
MAX_SOURCE = (3840, 2160)

C_BG = "#121212"
C_PANEL = "#1e1e1e"
C_TILE = "#262626"
C_YELLOW = "#f5c400"
C_BLUE = "#3d8eff"
C_ORANGE = "#ff8a00"
C_RED = "#e5484d"
C_GREEN = "#3ecf6e"
C_TEXT = "#d8d8d8"
C_DIM = "#8a8a8a"

STYLE = f"""
QWidget {{ background:{C_BG}; color:{C_TEXT}; font-family:"Segoe UI", Arial; font-size:10pt; }}
QLabel {{ background:transparent; }}
QPushButton {{ background:#2a2a2a; border:1px solid #3a3a3a; border-radius:3px; padding:6px 12px; }}
QPushButton:hover {{ background:#353535; }}
QPushButton:pressed {{ background:#444; }}
QPushButton:checked {{ background:#3d3300; border-color:{C_YELLOW}; color:#ffe36b; }}
QPushButton#sheet {{ padding:8px 16px; font-weight:600; }}
QPushButton#output:checked {{ background:#0f3d1f; border-color:{C_GREEN}; color:#8ff0b0; }}
QPushButton#blackout:checked {{ background:#4a1416; border-color:{C_RED}; color:#ffb3b5; }}
QTableWidget {{ background:#1a1a1a; alternate-background-color:#1f1f1f; gridline-color:#2c2c2c;
               selection-background-color:#2e4f7a; border:1px solid #2c2c2c; }}
QHeaderView::section {{ background:#252525; color:#aaa; border:0; border-right:1px solid #333;
                        padding:5px; font-weight:600; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background:#202020; border:1px solid #3a3a3a;
                        padding:4px 6px; border-radius:3px; min-height:20px; }}
QComboBox QAbstractItemView {{ background:#202020; selection-background-color:#2e4f7a; }}
QGroupBox {{ border:1px solid #2e2e2e; border-radius:4px; margin-top:16px; padding:10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 4px; color:{C_YELLOW}; font-weight:600; }}
QScrollArea {{ border:0; }}
QMenu {{ background:#222; border:1px solid #3a3a3a; }}
QMenu::item:selected {{ background:#2e4f7a; }}
QToolTip {{ background:#222; color:#eee; border:1px solid #555; }}
QCheckBox::indicator {{ width:16px; height:16px; }}
"""

DEFAULTS = {
    "protocol": "sacn",
    "interface": "0.0.0.0",
    "artnet_offset": 1,
    "output_screen": "",
    "output_screen_index": -1,
    "output_on_start": False,
    "fit": "contain",
    "mix": "add",
    "fade_in": 0.0,
    "fade_out": 0.0,
    "start_fid": 9001,
    "start_universe": 7,
    "start_address": 1,
    "master_universe": 0,
    "master_address": 0,
    "background": "#000000",
}


def log(msg):
    if sys.stdout:
        try:
            print(msg)
        except Exception:
            pass


def app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_data_dir():
    d = os.path.join(app_base_dir(), "NAS_Daten")
    try:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, ".schreibtest")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return d
    except OSError:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "NAS")
        os.makedirs(d, exist_ok=True)
        return d


def fmt_addr(u, a):
    return "%d.%03d" % (u, a)


def fmt_num(v):
    return ("%g" % v) if v is not None else ""


def make_app_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1b1b1b"))
    p.setPen(QPen(QColor(C_YELLOW), 4))
    p.drawRoundedRect(4, 8, 56, 48, 6, 6)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C_YELLOW))
    p.drawEllipse(40, 16, 10, 10)
    p.drawPolygon(QPolygon([QPoint(10, 50), QPoint(26, 28), QPoint(36, 42), QPoint(44, 34), QPoint(56, 50)]))
    p.end()
    return QIcon(pm)


# =============================================================================
# Show-Daten
# =============================================================================
class ShowStore(QObject):
    changed = Signal()

    def __init__(self, data_dir):
        super().__init__()
        self.dir = data_dir
        self.img_dir = os.path.join(data_dir, "bilder")
        os.makedirs(self.img_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "show.json")
        self.cfg = dict(DEFAULTS)
        self.images = []
        self.load()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.save)

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            log("show.json konnte nicht gelesen werden: %s" % e)
            try:
                shutil.copy2(self.path, self.path + ".defekt")
            except OSError:
                pass
            return
        for k, v in (d.get("settings") or {}).items():
            if k in DEFAULTS:
                self.cfg[k] = v
        for it in d.get("images") or []:
            try:
                self.images.append({
                    "id": str(it.get("id") or uuid.uuid4().hex),
                    "file": str(it["file"]),
                    "name": str(it.get("name") or ""),
                    "fid": int(it.get("fid", 0)),
                    "universe": int(it.get("universe", 1)),
                    "address": int(it.get("address", 1)),
                    "fade_in": None if it.get("fade_in") is None else float(it["fade_in"]),
                    "fade_out": None if it.get("fade_out") is None else float(it["fade_out"]),
                })
            except (KeyError, ValueError, TypeError):
                continue

    def save(self):
        d = {"app": APP_NAME, "version": APP_VERSION, "settings": self.cfg, "images": self.images}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            log("Speichern fehlgeschlagen: %s" % e)

    def touch(self):
        self._timer.start()
        self.changed.emit()

    def abs_path(self, img):
        p = img["file"]
        return p if os.path.isabs(p) else os.path.join(self.dir, p)

    def by_id(self, img_id):
        for it in self.images:
            if it["id"] == img_id:
                return it
        return None

    def next_patch(self):
        if not self.images:
            return int(self.cfg["start_fid"]), int(self.cfg["start_universe"]), int(self.cfg["start_address"])
        last = self.images[-1]
        fid = max(i["fid"] for i in self.images) + 1
        u, a = last["universe"], last["address"] + 1
        used = {(i["universe"], i["address"]) for i in self.images}
        while True:
            if a > 512:
                u, a = u + 1, 1
            if (u, a) not in used:
                break
            a += 1
        return fid, u, a

    def add_files(self, paths):
        added, errors = [], []
        for src in paths:
            if not os.path.isfile(src) or not src.lower().endswith(IMAGE_EXT):
                errors.append(os.path.basename(src))
                continue
            base, ext = os.path.splitext(os.path.basename(src))
            safe = re.sub(r"[^\w\-. ]", "_", base).strip() or "bild"
            dst = os.path.join(self.img_dir, safe + ext.lower())
            n = 2
            while os.path.exists(dst):
                dst = os.path.join(self.img_dir, "%s_%d%s" % (safe, n, ext.lower()))
                n += 1
            try:
                shutil.copy2(src, dst)
            except OSError:
                errors.append(os.path.basename(src))
                continue
            fid, u, a = self.next_patch()
            it = {"id": uuid.uuid4().hex, "file": os.path.relpath(dst, self.dir).replace("\\", "/"),
                  "name": base, "fid": fid, "universe": u, "address": a,
                  "fade_in": None, "fade_out": None}
            self.images.append(it)
            added.append(it)
        if added:
            self.touch()
        return added, errors

    def remove(self, img_id):
        it = self.by_id(img_id)
        if not it:
            return
        self.images.remove(it)
        path = self.abs_path(it)
        if os.path.abspath(path).startswith(os.path.abspath(self.img_dir)):
            try:
                os.remove(path)
            except OSError:
                pass
        self.touch()

    def auto_patch(self):
        fid = int(self.cfg["start_fid"])
        u = int(self.cfg["start_universe"])
        a = int(self.cfg["start_address"])
        for it in self.images:
            if a > 512:
                u, a = u + 1, 1
            it["fid"], it["universe"], it["address"] = fid, u, a
            fid += 1
            a += 1
        self.touch()

    def base_universe(self):
        if self.images:
            return min(it["universe"] for it in self.images)
        return int(self.cfg["start_universe"])

    def set_universe(self, new):
        """Alle Bilder in ein anderes Universum verschieben, Adressen bleiben gleich."""
        new = max(1, min(63999, int(new)))
        base = self.base_universe()
        for it in self.images:
            it["universe"] = max(1, min(63999, new + it["universe"] - base))
        self.cfg["start_universe"] = new
        self.touch()

    def repatch_addresses(self, u, a):
        """Adressen ab u.a fortlaufend neu vergeben, FIDs bleiben."""
        self.cfg["start_universe"], self.cfg["start_address"] = int(u), int(a)
        for it in self.images:
            if a > 512:
                u, a = u + 1, 1
            it["universe"], it["address"] = u, a
            a += 1
        self.touch()

    def conflicts(self):
        seen, bad = {}, set()
        mu, ma = int(self.cfg["master_universe"]), int(self.cfg["master_address"])
        for it in self.images:
            key = (it["universe"], it["address"])
            if key in seen:
                bad.add(it["id"])
                bad.add(seen[key])
            seen[key] = it["id"]
            if mu and ma and key == (mu, ma):
                bad.add(it["id"])
        return bad

    def universes(self):
        us = {it["universe"] for it in self.images}
        if int(self.cfg["master_universe"]) and int(self.cfg["master_address"]):
            us.add(int(self.cfg["master_universe"]))
        return us


# =============================================================================
# Bilder laden, skalieren, mischen
# =============================================================================
class ImageLibrary:
    def __init__(self, store):
        self.store = store
        self.sources = {}
        self.thumbs = {}
        self.frames = {}       # (w, h, fit) -> {id: QImage}
        self.frame_order = []
        self.errors = set()

    def invalidate(self, img_id=None):
        if img_id is None:
            self.sources.clear()
            self.thumbs.clear()
            self.frames.clear()
            self.frame_order.clear()
            self.errors.clear()
            return
        self.sources.pop(img_id, None)
        self.errors.discard(img_id)
        for k in list(self.thumbs):
            if k[0] == img_id:
                del self.thumbs[k]
        for d in self.frames.values():
            d.pop(img_id, None)

    def source(self, img):
        q = self.sources.get(img["id"])
        if q is not None:
            return q
        if img["id"] in self.errors:
            return None
        reader = QImageReader(self.store.abs_path(img))
        reader.setAutoTransform(True)
        q = reader.read()
        if q.isNull():
            self.errors.add(img["id"])
            return None
        if q.width() > MAX_SOURCE[0] or q.height() > MAX_SOURCE[1]:
            q = q.scaled(MAX_SOURCE[0], MAX_SOURCE[1], Qt.KeepAspectRatio, Qt.SmoothTransformation)
        q = q.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.sources[img["id"]] = q
        return q

    def thumb(self, img, w, h):
        key = (img["id"], w, h)
        pm = self.thumbs.get(key)
        if pm is None:
            src = self.source(img)
            if src is None:
                return None
            pm = QPixmap.fromImage(src.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.thumbs[key] = pm
        return pm

    def frame(self, img, w, h, fit):
        key = (w, h, fit)
        d = self.frames.get(key)
        if d is None:
            d = {}
            self.frames[key] = d
            self.frame_order.append(key)
            while len(self.frame_order) > 3:
                self.frames.pop(self.frame_order.pop(0), None)
        f = d.get(img["id"])
        if f is not None:
            return f
        src = self.source(img)
        if src is None:
            return None
        f = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        f.fill(Qt.transparent)
        mode = {"contain": Qt.KeepAspectRatio, "cover": Qt.KeepAspectRatioByExpanding,
                "stretch": Qt.IgnoreAspectRatio}.get(fit, Qt.KeepAspectRatio)
        sc = src.scaled(w, h, mode, Qt.SmoothTransformation)
        p = QPainter(f)
        p.drawImage((w - sc.width()) // 2, (h - sc.height()) // 2, sc)
        p.end()
        d[img["id"]] = f
        return f

    def has_frame(self, img, w, h, fit):
        d = self.frames.get((w, h, fit))
        return bool(d) and img["id"] in d

    def compose(self, w, h, layers, fit, mix, bg):
        out = QImage(max(1, w), max(1, h), QImage.Format_ARGB32_Premultiplied)
        out.fill(QColor(bg))
        if layers:
            p = QPainter(out)
            if mix == "add":
                p.setCompositionMode(QPainter.CompositionMode_Plus)
            else:
                layers = sorted(layers, key=lambda l: l[0]["fid"])
            for img, lvl in layers:
                f = self.frame(img, w, h, fit)
                if f is None:
                    continue
                p.setOpacity(max(0.0, min(1.0, lvl)))
                p.drawImage(0, 0, f)
            p.end()
        return out


# =============================================================================
# Ausgabe-Fenster (Beamer) und Vorschau
# =============================================================================
class OutputWindow(QWidget):
    def __init__(self, main):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.main = main
        self.setWindowTitle(APP_NAME + " – Ausgabe")
        self.setWindowIcon(main.windowIcon())
        self.setCursor(Qt.BlankCursor)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setStyleSheet("background:black;")

    def pixel_size(self):
        dpr = self.devicePixelRatioF()
        return max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))

    def show_on(self, screen):
        g = screen.geometry()
        self.hide()
        self.setGeometry(g)
        self.show()
        if self.windowHandle():
            self.windowHandle().setScreen(screen)
        self.setGeometry(g)
        self.showFullScreen()
        self.raise_()

    def paintEvent(self, e):
        w, h = self.pixel_size()
        img = self.main.render(w, h)
        p = QPainter(self)
        p.drawImage(self.rect(), img)
        p.end()

    def keyPressEvent(self, e):
        self.main.handle_key(e)

    def closeEvent(self, e):
        e.ignore()
        self.main.set_output(False)


class PreviewWidget(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.aspect = 16 / 9

    def set_aspect(self, a):
        self.aspect = a if a > 0 else 16 / 9
        self.updateGeometry()
        self.setFixedHeight(int(self.width() / self.aspect))

    def resizeEvent(self, e):
        self.setFixedHeight(int(self.width() / self.aspect))

    def paintEvent(self, e):
        p = QPainter(self)
        r = self.rect().adjusted(1, 1, -1, -1)
        dpr = self.devicePixelRatioF()
        img = self.main.render(int(r.width() * dpr), int(r.height() * dpr))
        p.drawImage(r, img)
        p.setPen(QPen(QColor("#3a3a3a"), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.setPen(QColor(C_DIM))
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(r.adjusted(6, 4, -6, -4), Qt.AlignTop | Qt.AlignLeft,
                   "AUSGABE AKTIV" if self.main.output_active() else "VORSCHAU (Ausgabe aus)")
        p.end()


# =============================================================================
# Image Pool
# =============================================================================
class PoolView(QWidget):
    TW, TH, GAP = 170, 136, 6

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

    def cols(self):
        return max(1, (self.width() - self.GAP) // (self.TW + self.GAP))

    def tile_rect(self, i):
        c = self.cols()
        r, k = divmod(i, c)
        return QRect(self.GAP + k * (self.TW + self.GAP), self.GAP + r * (self.TH + self.GAP), self.TW, self.TH)

    def update_height(self):
        n = len(self.main.store.images) + 1
        rows = math.ceil(n / self.cols())
        self.setMinimumHeight(self.GAP + rows * (self.TH + self.GAP))

    def resizeEvent(self, e):
        self.update_height()

    def index_at(self, pos):
        for i in range(len(self.main.store.images) + 1):
            if self.tile_rect(i).contains(pos):
                return i
        return -1

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        imgs = self.main.store.images
        bad = self.main.store.conflicts()
        small = QFont(self.font())
        small.setPointSize(8)
        bold = QFont(self.font())
        bold.setBold(True)
        for i, img in enumerate(imgs):
            r = self.tile_rect(i)
            if not r.intersects(e.rect()):
                continue
            lvl = self.main.levels.get(img["id"], 0.0)
            p.fillRect(r, QColor(C_TILE))
            # Vorschau
            ta = r.adjusted(4, 22, -4, -34)
            pm = self.main.lib.thumb(img, ta.width(), ta.height())
            p.fillRect(ta, QColor("#0c0c0c"))
            if pm:
                x = ta.x() + (ta.width() - pm.width()) // 2
                y = ta.y() + (ta.height() - pm.height()) // 2
                p.drawPixmap(x, y, pm)
                if lvl < 0.999:
                    p.fillRect(QRect(x, y, pm.width(), pm.height()), QColor(0, 0, 0, int(150 * (1 - lvl))))
            else:
                p.setPen(QColor(C_RED))
                p.drawText(ta, Qt.AlignCenter, "Datei fehlt /\nnicht lesbar")
            # Kopf
            p.setFont(bold)
            p.setPen(QColor(C_YELLOW if lvl > 0.001 else C_TEXT))
            p.drawText(r.adjusted(6, 2, -6, 0), Qt.AlignLeft | Qt.AlignTop, str(i + 1))
            p.setFont(small)
            p.setPen(QColor(C_DIM))
            p.drawText(r.adjusted(6, 4, -6, 0), Qt.AlignRight | Qt.AlignTop, "FID %d" % img["fid"])
            # Fuß
            p.setFont(self.font())
            p.setPen(QColor(C_TEXT))
            fm = QFontMetrics(self.font())
            p.drawText(QRect(r.x() + 6, r.bottom() - 32, r.width() - 12, 16), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(img["name"] or "(ohne Name)", Qt.ElideRight, r.width() - 12))
            p.setFont(small)
            p.setPen(QColor(C_RED) if img["id"] in bad else QColor(C_DIM))
            p.drawText(QRect(r.x() + 6, r.bottom() - 16, r.width() - 12, 12), Qt.AlignLeft | Qt.AlignVCenter,
                       fmt_addr(img["universe"], img["address"]) + ("  DOPPELT" if img["id"] in bad else ""))
            p.setPen(QColor(C_YELLOW if lvl > 0.001 else C_DIM))
            p.drawText(QRect(r.x() + 6, r.bottom() - 16, r.width() - 12, 12), Qt.AlignRight | Qt.AlignVCenter,
                       "%d%%" % round(lvl * 100))
            # Pegelbalken
            p.fillRect(QRect(r.x(), r.bottom() - 3, r.width(), 4), QColor("#151515"))
            if lvl > 0:
                p.fillRect(QRect(r.x(), r.bottom() - 3, int(r.width() * lvl), 4), QColor(C_YELLOW))
            # Test-Markierung
            if img["id"] in self.main.test_ids:
                badge = QRect(r.right() - 46, ta.y() + 4, 40, 16)
                p.setBrush(QColor(C_ORANGE))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(badge, 3, 3)
                p.setPen(QColor("black"))
                p.setFont(small)
                p.drawText(badge, Qt.AlignCenter, "TEST")
            # Rahmen
            p.setBrush(Qt.NoBrush)
            if img["id"] == self.main.selected_id:
                p.setPen(QPen(QColor(C_BLUE), 2))
                p.drawRect(r.adjusted(1, 1, -1, -1))
            elif lvl > 0.001:
                p.setPen(QPen(QColor(C_YELLOW), 1))
                p.drawRect(r.adjusted(0, 0, -1, -1))
        # Plus-Kachel
        r = self.tile_rect(len(imgs))
        p.setPen(QPen(QColor("#4a4a4a"), 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r.adjusted(0, 0, -1, -1))
        f = QFont(self.font())
        f.setPointSize(26)
        p.setFont(f)
        p.setPen(QColor(C_DIM))
        p.drawText(r.adjusted(0, -16, 0, -16), Qt.AlignCenter, "+")
        p.setFont(small)
        p.drawText(r.adjusted(0, 40, 0, 0), Qt.AlignCenter, "Bilder hinzufügen\n(oder hierher ziehen)")
        p.end()

    def mousePressEvent(self, e):
        i = self.index_at(e.position().toPoint())
        imgs = self.main.store.images
        if i == len(imgs):
            if e.button() == Qt.LeftButton:
                self.main.add_images_dialog()
            return
        if 0 <= i < len(imgs):
            self.main.selected_id = imgs[i]["id"]
            self.update()

    def mouseDoubleClickEvent(self, e):
        i = self.index_at(e.position().toPoint())
        imgs = self.main.store.images
        if 0 <= i < len(imgs):
            self.main.toggle_test(imgs[i]["id"])

    def contextMenuEvent(self, e):
        i = self.index_at(e.pos())
        imgs = self.main.store.images
        if not 0 <= i < len(imgs):
            return
        img = imgs[i]
        self.main.selected_id = img["id"]
        self.update()
        m = QMenu(self)
        a_test = m.addAction("Test ein/aus (lokal 100 %)")
        a_name = m.addAction("Umbenennen …")
        a_repl = m.addAction("Datei ersetzen …")
        m.addSeparator()
        a_del = m.addAction("Entfernen")
        act = m.exec(e.globalPos())
        if act == a_test:
            self.main.toggle_test(img["id"])
        elif act == a_name:
            self.main.rename_image(img["id"])
        elif act == a_repl:
            self.main.replace_image(img["id"])
        elif act == a_del:
            self.main.remove_image(img["id"])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        files = []
        for pth in paths:
            if os.path.isdir(pth):
                files += sorted(os.path.join(pth, f) for f in os.listdir(pth) if f.lower().endswith(IMAGE_EXT))
            else:
                files.append(pth)
        self.main.add_images(files)


# =============================================================================
# Patch-Sheet
# =============================================================================
class PatchView(QWidget):
    COLS = ["Nr", "FID", "Name", "Universum", "Adresse", "Fade In (s)", "Fade Out (s)", "Datei"]

    def __init__(self, main):
        super().__init__()
        self.main = main
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        for text, fn in [("Bilder hinzufügen", main.add_images_dialog), ("Entfernen", self.remove),
                         ("▲ Hoch", lambda: self.move(-1)), ("▼ Runter", lambda: self.move(1)),
                         ("Auto-Patch …", self.auto_patch),
                         ("Vorschaubilder für grandMA3 exportieren …", main.export_thumbs)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        ubar = QHBoxLayout()
        ul = QLabel("Universum für die Bilder:")
        ul.setStyleSheet("color:%s; font-weight:600;" % C_YELLOW)
        ubar.addWidget(ul)
        self.uni = QSpinBox()
        self.uni.setRange(1, 63999)
        self.uni.setMinimumWidth(90)
        self.uni.setKeyboardTracking(False)
        self.uni.valueChanged.connect(self.on_universe)
        ubar.addWidget(self.uni)
        self.uni_info = QLabel("")
        self.uni_info.setStyleSheet("color:%s;" % C_DIM)
        ubar.addWidget(self.uni_info)
        b = QPushButton("Universum lernen …")
        b.setToolTip("Dimmer an der Konsole auf Full ziehen – die App erkennt Universum und Kanal")
        b.clicked.connect(self.learn)
        ubar.addWidget(b)
        ubar.addStretch(1)
        lay.addLayout(ubar)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        for c in range(len(self.COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.itemChanged.connect(self.on_changed)
        self.table.itemSelectionChanged.connect(self.on_select)
        lay.addWidget(self.table)
        hint = QLabel("Doppelklick zum Bearbeiten. Adresse auch als „7.001“ eingebbar. "
                      "Fade leer = Standard aus den Einstellungen. 0 = Konsole direkt folgen.")
        hint.setStyleSheet("color:%s;" % C_DIM)
        lay.addWidget(hint)

    def refresh(self):
        self._busy = True
        imgs = self.main.store.images
        self.uni.blockSignals(True)
        self.uni.setValue(self.main.store.base_universe())
        self.uni.blockSignals(False)
        us = sorted({it["universe"] for it in imgs})
        if imgs:
            self.uni_info.setText("Bilder liegen auf %s … %s%s" % (
                fmt_addr(min(us), min(i["address"] for i in imgs if i["universe"] == min(us))),
                fmt_addr(max(us), max(i["address"] for i in imgs if i["universe"] == max(us))),
                "  (mehrere Universen)" if len(us) > 1 else ""))
        else:
            self.uni_info.setText("neue Bilder starten bei %s" % fmt_addr(
                int(self.main.store.cfg["start_universe"]), int(self.main.store.cfg["start_address"])))
        bad = self.main.store.conflicts()
        self.table.setRowCount(len(imgs))
        for row, it in enumerate(imgs):
            vals = [str(row + 1), str(it["fid"]), it["name"], str(it["universe"]), str(it["address"]),
                    fmt_num(it["fade_in"]), fmt_num(it["fade_out"]), it["file"]]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c in (0, 7):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setForeground(QColor(C_DIM))
                if c in (5, 6) and v == "":
                    item.setToolTip("Standard: %g s" % float(self.main.store.cfg["fade_in" if c == 5 else "fade_out"]))
                if c in (3, 4) and it["id"] in bad:
                    item.setBackground(QColor("#5a1c1e"))
                    item.setToolTip("Adresse doppelt belegt (oder Master-Kanal)")
                self.table.setItem(row, c, item)
            if it["id"] == self.main.selected_id:
                self.table.selectRow(row)
        self._busy = False

    def on_select(self):
        if self._busy:
            return
        row = self.table.currentRow()
        imgs = self.main.store.images
        if 0 <= row < len(imgs):
            self.main.selected_id = imgs[row]["id"]

    def on_changed(self, item):
        if self._busy:
            return
        row, c = item.row(), item.column()
        imgs = self.main.store.images
        if not 0 <= row < len(imgs):
            return
        it = imgs[row]
        txt = item.text().strip().replace(",", ".")
        try:
            if c == 1:
                it["fid"] = max(1, int(float(txt)))
            elif c == 2:
                it["name"] = item.text().strip()
            elif c == 3:
                it["universe"] = max(1, min(63999, int(float(txt))))
            elif c == 4:
                if "." in txt:
                    u, a = txt.split(".", 1)
                    it["universe"] = max(1, min(63999, int(u)))
                    it["address"] = max(1, min(512, int(a)))
                else:
                    it["address"] = max(1, min(512, int(float(txt))))
            elif c in (5, 6):
                key = "fade_in" if c == 5 else "fade_out"
                it[key] = None if txt == "" else max(0.0, min(600.0, float(txt)))
        except ValueError:
            pass
        self.main.store.touch()
        self.main.patch_changed()

    def on_universe(self, v):
        if self._busy:
            return
        self.main.store.set_universe(v)
        self.main.settings.reload_patch_fields()
        self.main.patch_changed()

    def learn(self):
        LearnDialog(self.main).exec()

    def remove(self):
        if self.main.selected_id:
            self.main.remove_image(self.main.selected_id)

    def move(self, d):
        imgs = self.main.store.images
        it = self.main.store.by_id(self.main.selected_id)
        if not it:
            return
        i = imgs.index(it)
        j = i + d
        if 0 <= j < len(imgs):
            imgs[i], imgs[j] = imgs[j], imgs[i]
            self.main.store.touch()
            self.main.patch_changed()

    def auto_patch(self):
        cfg = self.main.store.cfg
        if not self.main.store.images:
            return
        r = QMessageBox.question(self, "Auto-Patch",
                                 "Alle Bilder in Pool-Reihenfolge neu patchen?\n\nAb FID %d, Adresse %s\n"
                                 "(Startwerte in den Einstellungen)" % (
                                     int(cfg["start_fid"]), fmt_addr(int(cfg["start_universe"]), int(cfg["start_address"]))))
        if r == QMessageBox.Yes:
            self.main.store.auto_patch()
            self.main.patch_changed()


class LearnDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Universum lernen")
        self.setMinimumWidth(520)
        self.found = None
        lay = QVBoxLayout(self)
        steps = QLabel(
            "<b>So geht's:</b><ol style='margin-left:-20px'>"
            "<li>An der Konsole einen Dimmer in ein <b>freies Universum</b> patchen (z. B. FID 9001).</li>"
            "<li>Dieses Universum unter <i>DMX Protocols</i> per %s ausgeben.</li>"
            "<li>Den Dimmer <b>jetzt</b> auf Full ziehen.</li></ol>"
            "Die App erkennt den Kanal, der hochgeht." % (
                "sACN" if main.store.cfg["protocol"] == "sacn" else "Art-Net"))
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.RichText)
        lay.addWidget(steps)
        self.recv = QLabel("")
        self.recv.setStyleSheet("color:%s;" % C_DIM)
        self.recv.setWordWrap(True)
        lay.addWidget(self.recv)
        self.state = QLabel("Warte auf einen Kanal, der hochgezogen wird …")
        self.state.setStyleSheet("font-size:12pt; padding:10px; background:#1e1e1e; border:1px solid #333;")
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        row = QHBoxLayout()
        self.btn_apply = QPushButton("Übernehmen")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self.apply)
        again = QPushButton("Nochmal")
        again.clicked.connect(self.reset)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(again)
        row.addWidget(cancel)
        row.addWidget(self.btn_apply)
        lay.addLayout(row)
        # sACN: zum Lernen viele Universen abonnieren (Art-Net empfängt ohnehin alles)
        main.receiver.update_universes(set(range(1, 33)) | main.store.universes())
        self.reset()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    def reset(self):
        self.found = None
        self.baseline = dict(self.main.receiver.snapshot())
        self.btn_apply.setEnabled(False)
        self.btn_apply.setText("Übernehmen")
        self.state.setText("Warte auf einen Kanal, der hochgezogen wird …")

    def poll(self):
        snap = self.main.receiver.snapshot()
        stats = self.main.receiver.stats_copy()
        now = time.monotonic()
        live = sorted(u for u, s in stats.items() if now - s["last"] < 1.5)
        self.recv.setText("Empfangene Universen: " + (", ".join(map(str, live)) if live else
                          "keine – Netzwerk/Interface und DMX Protocols an der Konsole prüfen"))
        if self.found:
            return
        r = detect_new_channel(self.baseline, snap)
        if r:
            u, a, v = r
            self.found = (u, a)
            used = self.main.patched_map(u)
            warn = ""
            n = max(1, len(self.main.store.images))
            if any(a <= ch < a + n for ch in range(1, 513) if snap[u][ch - 1] and ch != a):
                warn = "<br><span style='color:%s'>Achtung: In diesem Bereich sind schon andere Kanäle aktiv – " \
                       "ist das Universum wirklich frei?</span>" % C_ORANGE
            self.state.setText("<b style='color:%s'>Erkannt: Universum %d, Kanal %d</b> (Wert %d = %d %%)%s" % (
                C_GREEN, u, a, v, round(v / 2.55), warn))
            self.state.setTextFormat(Qt.RichText)
            self.btn_apply.setText("Bilder ab %s patchen" % fmt_addr(u, a))
            self.btn_apply.setEnabled(True)

    def apply(self):
        if not self.found:
            return
        u, a = self.found
        self.main.store.repatch_addresses(u, a)
        self.main.settings.reload_patch_fields()
        self.main.patch_changed()
        self.accept()


# =============================================================================
# DMX-Monitor
# =============================================================================
class DmxGrid(QWidget):
    COLS = 32

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.universe = 0
        self.values = bytes(512)
        self.setMouseTracking(True)
        self.setMinimumHeight(16 * 30)

    def cell(self):
        cw = max(20, (self.width() - 2) / self.COLS)
        ch = max(26, min(40, (self.height() - 2) / 16))
        return cw, ch

    def paintEvent(self, e):
        p = QPainter(self)
        cw, ch = self.cell()
        patched = self.main.patched_map(self.universe)
        f = QFont(self.font())
        f.setPointSize(8)
        p.setFont(f)
        for ch_i in range(512):
            row, col = divmod(ch_i, self.COLS)
            r = QRectF(1 + col * cw, 1 + row * ch, cw - 2, ch - 2)
            v = self.values[ch_i]
            base = QColor(30, 30, 30)
            if v:
                t = v / 255.0
                base = QColor(int(30 + 200 * t), int(30 + 165 * t), int(30 - 30 * t))
            p.fillRect(r, base)
            info = patched.get(ch_i + 1)
            if info:
                p.fillRect(QRectF(r.x(), r.y(), r.width(), 3), QColor(C_BLUE if info[0] != "M" else C_ORANGE))
            p.setPen(QColor("#000" if v > 150 else ("#ddd" if v else "#555")))
            p.drawText(r, Qt.AlignCenter, str(v))
            p.setPen(QColor(C_DIM if not v else "#222"))
            p.drawText(r.adjusted(2, 1, 0, 0), Qt.AlignLeft | Qt.AlignTop, "" if ch_i % 8 else str(ch_i + 1))
        p.end()

    def mouseMoveEvent(self, e):
        cw, ch = self.cell()
        x, y = e.position().x(), e.position().y()
        col, row = int((x - 1) // cw), int((y - 1) // ch)
        if 0 <= col < self.COLS and 0 <= row < 16:
            n = row * self.COLS + col + 1
            v = self.values[n - 1]
            txt = "%s = %d (%d%%)" % (fmt_addr(self.universe, n), v, round(v / 2.55))
            info = self.main.patched_map(self.universe).get(n)
            if info:
                txt += "\n" + info[1]
            QToolTip.showText(e.globalPosition().toPoint(), txt, self)


class DmxView(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        top.addWidget(QLabel("Universum:"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(240)
        self.combo.currentIndexChanged.connect(self.on_combo)
        top.addWidget(self.combo)
        top.addStretch(1)
        lay.addLayout(top)
        self.info = QLabel("")
        self.info.setStyleSheet("color:%s;" % C_DIM)
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.grid = DmxGrid(main)
        lay.addWidget(self.grid, 1)
        legend = QLabel("Blauer Streifen = Bild gepatcht · Oranger Streifen = Master-Kanal · Maus drüber für Details")
        legend.setStyleSheet("color:%s;" % C_DIM)
        lay.addWidget(legend)
        self._last_counts = {}
        self._last_t = time.monotonic()
        self._rates = {}

    def on_combo(self):
        u = self.combo.currentData()
        if u:
            self.grid.universe = int(u)
            self.main.receiver.update_universes({int(u)})

    def refresh_list(self):
        stats = self.main.receiver.stats_copy()
        now = time.monotonic()
        dt = max(0.001, now - self._last_t)
        for u, s in stats.items():
            self._rates[u] = (s["count"] - self._last_counts.get(u, 0)) / dt
            self._last_counts[u] = s["count"]
        self._last_t = now
        us = sorted(set(stats) | self.main.store.universes() | ({self.grid.universe} if self.grid.universe else set()))
        cur = self.combo.currentData()
        proto = self.main.store.cfg["protocol"]
        off = int(self.main.store.cfg["artnet_offset"])
        same = [self.combo.itemData(i) for i in range(self.combo.count())] == us
        self.combo.blockSignals(True)
        if not same:
            self.combo.clear()
        for idx, u in enumerate(us):
            st = stats.get(u)
            live = st and now - st["last"] < 1.5
            label = "Universum %d" % u
            if proto == "artnet":
                label += "  (Art-Net %d)" % (u - off)
            label += "  ●" if live else "  ○"
            if same:
                self.combo.setItemText(idx, label)
            else:
                self.combo.addItem(label, u)
        if same:
            pass
        elif cur in us:
            self.combo.setCurrentIndex(us.index(cur))
        elif us:
            self.combo.setCurrentIndex(0)
        self.combo.blockSignals(False)
        if self.combo.currentData():
            self.grid.universe = int(self.combo.currentData())
        lines = []
        for u in sorted(stats):
            s = stats[u]
            age = now - s["last"]
            state = "%d Pakete/s" % round(self._rates.get(u, 0)) if age < 1.5 else "seit %ds still" % age
            raw = (" = Art-Net %d" % s["raw"]) if proto == "artnet" else ""
            lines.append("U%d%s: %s von %s" % (u, raw, state, s["src"]))
        self.info.setText("Empfangen: " + (" · ".join(lines) if lines else "noch nichts"))

    def refresh_values(self):
        snap = self.main.snapshot
        self.grid.values = snap.get(self.grid.universe, bytes(512))
        self.grid.update()


# =============================================================================
# Einstellungen
# =============================================================================
class SettingsView(QScrollArea):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.setWidgetResizable(True)
        inner = QWidget()
        self.setWidget(inner)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(10, 4, 10, 10)
        cfg = main.store.cfg
        self._busy = True

        g = QGroupBox("Empfang")
        f = QFormLayout(g)
        self.proto = QComboBox()
        self.proto.addItem("sACN (E1.31)", "sacn")
        self.proto.addItem("Art-Net", "artnet")
        f.addRow("Protokoll", self.proto)
        self.iface = QComboBox()
        f.addRow("Netzwerk-Interface", self.iface)
        self.offset = QSpinBox()
        self.offset.setRange(-1, 2)
        f.addRow("Art-Net-Zählung", self.offset)
        l = QLabel("grandMA3-Universum = Art-Net-Universum + diesen Wert. Standard 1 "
                   "(Art-Net zählt ab 0). Im DMX-Monitor siehst du, was ankommt.")
        l.setWordWrap(True)
        l.setStyleSheet("color:%s;" % C_DIM)
        f.addRow("", l)
        lay.addWidget(g)

        g = QGroupBox("Ausgabe (Beamer)")
        f = QFormLayout(g)
        self.screen = QComboBox()
        f.addRow("Bildschirm", self.screen)
        self.screen_warn = QLabel("")
        self.screen_warn.setStyleSheet("color:%s;" % C_ORANGE)
        self.screen_warn.setWordWrap(True)
        f.addRow("", self.screen_warn)
        self.on_start = QCheckBox("Ausgabe beim Programmstart öffnen")
        f.addRow("", self.on_start)
        self.fit = QComboBox()
        self.fit.addItem("Einpassen (ganzes Bild, ggf. schwarze Ränder)", "contain")
        self.fit.addItem("Füllen (Bildschirm voll, Ränder abgeschnitten)", "cover")
        self.fit.addItem("Strecken (verzerrt)", "stretch")
        f.addRow("Bild einpassen", self.fit)
        self.mix = QComboBox()
        self.mix.addItem("Mischen – Crossfade ohne dunkles Loch", "add")
        self.mix.addItem("Überdecken – höhere FID liegt oben", "over")
        f.addRow("Mischmodus", self.mix)
        lay.addWidget(g)

        g = QGroupBox("Fade-Zeiten in der App")
        f = QFormLayout(g)
        self.fin = QDoubleSpinBox()
        self.fout = QDoubleSpinBox()
        for s in (self.fin, self.fout):
            s.setRange(0, 600)
            s.setDecimals(1)
            s.setSingleStep(0.5)
            s.setSuffix(" s")
        f.addRow("Standard Fade In", self.fin)
        f.addRow("Standard Fade Out", self.fout)
        l = QLabel("0 s = die App folgt der Konsole direkt (Fades kommen aus den Cue-Zeiten). "
                   "Größer 0 = die App blendet zusätzlich selbst weich. Pro Bild im Patch änderbar.")
        l.setWordWrap(True)
        l.setStyleSheet("color:%s;" % C_DIM)
        f.addRow("", l)
        lay.addWidget(g)

        g = QGroupBox("Patch")
        f = QFormLayout(g)
        self.sfid = QSpinBox()
        self.sfid.setRange(1, 999999)
        self.suni = QSpinBox()
        self.suni.setRange(1, 63999)
        self.sadr = QSpinBox()
        self.sadr.setRange(1, 512)
        f.addRow("Start-FID", self.sfid)
        f.addRow("Start-Universum", self.suni)
        f.addRow("Startadresse", self.sadr)
        self.muni = QSpinBox()
        self.muni.setRange(0, 63999)
        self.madr = QSpinBox()
        self.madr.setRange(0, 512)
        f.addRow("Master-Kanal Universum", self.muni)
        f.addRow("Master-Kanal Adresse", self.madr)
        l = QLabel("Optional: ein eigener Dimmer als Master für alle Bilder (0 = aus).")
        l.setStyleSheet("color:%s;" % C_DIM)
        f.addRow("", l)
        lay.addWidget(g)

        g = QGroupBox("Daten")
        f = QFormLayout(g)
        pl = QLabel(main.store.dir)
        pl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pl.setWordWrap(True)
        f.addRow("Ordner", pl)
        b = QPushButton("Ordner öffnen")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(main.store.dir)))
        f.addRow("", b)
        f.addRow("Version", QLabel(APP_VERSION))
        lay.addWidget(g)
        lay.addStretch(1)

        self.fill_ifaces()
        self.fill_screens()
        self.proto.setCurrentIndex(max(0, self.proto.findData(cfg["protocol"])))
        self.offset.setValue(int(cfg["artnet_offset"]))
        self.on_start.setChecked(bool(cfg["output_on_start"]))
        self.fit.setCurrentIndex(max(0, self.fit.findData(cfg["fit"])))
        self.mix.setCurrentIndex(max(0, self.mix.findData(cfg["mix"])))
        self.fin.setValue(float(cfg["fade_in"]))
        self.fout.setValue(float(cfg["fade_out"]))
        self.sfid.setValue(int(cfg["start_fid"]))
        self.suni.setValue(int(cfg["start_universe"]))
        self.sadr.setValue(int(cfg["start_address"]))
        self.muni.setValue(int(cfg["master_universe"]))
        self.madr.setValue(int(cfg["master_address"]))
        self._busy = False

        for w in (self.proto, self.iface, self.screen, self.fit, self.mix):
            w.currentIndexChanged.connect(self.changed)
        for w in (self.offset, self.sfid, self.suni, self.sadr, self.muni, self.madr, self.fin, self.fout):
            w.valueChanged.connect(self.changed)
        self.on_start.toggled.connect(self.changed)
        self.update_warn()

    def reload_patch_fields(self):
        cfg = self.main.store.cfg
        self._busy, was = True, self._busy
        self.suni.setValue(int(cfg["start_universe"]))
        self.sadr.setValue(int(cfg["start_address"]))
        self._busy = was

    def fill_ifaces(self):
        cur = self.main.store.cfg["interface"]
        self._busy, was = True, self._busy
        self.iface.clear()
        self.iface.addItem("Alle Netzwerkkarten", "0.0.0.0")
        for ip in local_ipv4s():
            self.iface.addItem(ip, ip)
        i = self.iface.findData(cur)
        if i < 0:
            self.iface.addItem(cur + " (nicht gefunden)", cur)
            i = self.iface.count() - 1
        self.iface.setCurrentIndex(i)
        self._busy = was

    def fill_screens(self):
        self._busy, was = True, self._busy
        self.screen.clear()
        prim = QGuiApplication.primaryScreen()
        for i, s in enumerate(QGuiApplication.screens()):
            g = s.geometry()
            sz = s.size() * s.devicePixelRatio()
            txt = "%d: %s  %d×%d%s" % (i + 1, s.name(), sz.width(), sz.height(),
                                       "  (Hauptbildschirm)" if s is prim else "")
            self.screen.addItem(txt, i)
        sc = self.main.output_screen()
        if sc in QGuiApplication.screens():
            self.screen.setCurrentIndex(QGuiApplication.screens().index(sc))
        self._busy = was
        self.update_warn()

    def update_warn(self):
        sc = self.main.output_screen()
        if sc is self.main.screen():
            self.screen_warn.setText("Ausgabe liegt auf demselben Bildschirm wie diese Bedienung. "
                                     "Mit F2 schaltest du die Ausgabe wieder aus.")
        else:
            self.screen_warn.setText("")

    def changed(self, *_):
        if self._busy:
            return
        cfg = self.main.store.cfg
        old = (cfg["protocol"], cfg["interface"], int(cfg["artnet_offset"]))
        old_fit = cfg["fit"]
        cfg["protocol"] = self.proto.currentData()
        cfg["interface"] = self.iface.currentData() or "0.0.0.0"
        cfg["artnet_offset"] = self.offset.value()
        cfg["output_on_start"] = self.on_start.isChecked()
        cfg["fit"] = self.fit.currentData()
        cfg["mix"] = self.mix.currentData()
        cfg["fade_in"] = self.fin.value()
        cfg["fade_out"] = self.fout.value()
        cfg["start_fid"] = self.sfid.value()
        cfg["start_universe"] = self.suni.value()
        cfg["start_address"] = self.sadr.value()
        cfg["master_universe"] = self.muni.value()
        cfg["master_address"] = self.madr.value()
        idx = self.screen.currentData()
        screens = QGuiApplication.screens()
        if idx is not None and 0 <= idx < len(screens):
            if cfg.get("output_screen") != screens[idx].name() or cfg.get("output_screen_index") != idx:
                cfg["output_screen"] = screens[idx].name()
                cfg["output_screen_index"] = idx
                self.main.screen_changed()
        self.main.store.touch()
        if old != (cfg["protocol"], cfg["interface"], int(cfg["artnet_offset"])):
            self.main.restart_receiver()
        if old_fit != cfg["fit"]:
            self.main.lib.frames.clear()
            self.main.lib.frame_order.clear()
            self.main.prewarm()
        self.main.patch_changed()
        self.update_warn()


# =============================================================================
# Hauptfenster
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self, data_dir):
        super().__init__()
        self.setWindowTitle("%s %s – grandMA3" % (APP_NAME, APP_VERSION))
        self.setWindowIcon(make_app_icon())
        self.store = ShowStore(data_dir)
        self.lib = ImageLibrary(self.store)
        self.receiver = DmxReceiver()
        self.levels = {}
        self.test_ids = set()
        self.blackout = False
        self.selected_id = None
        self.snapshot = {}
        self.master = 1.0
        self._render_cache = {}
        self._gen = 0
        self._prewarm_queue = []
        self._tick_n = 0
        self._last_t = time.perf_counter()

        self.output = OutputWindow(self)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Kopfzeile
        top = QFrame()
        top.setStyleSheet("QFrame { background:%s; border-bottom:1px solid #2c2c2c; }" % C_PANEL)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(10, 6, 10, 6)
        title = QLabel("NAS")
        title.setStyleSheet("color:%s; font-weight:700; font-size:12pt; letter-spacing:1px;" % C_YELLOW)
        tl.addWidget(title)
        tl.addSpacing(16)
        self.sheet_group = QButtonGroup(self)
        self.stack = QStackedWidget()
        self.pool = PoolView(self)
        pool_scroll = QScrollArea()
        pool_scroll.setWidgetResizable(True)
        pool_scroll.setWidget(self.pool)
        self.patch = PatchView(self)
        self.dmx = DmxView(self)
        self.settings = SettingsView(self)
        for i, (name, w) in enumerate([("Image Pool", pool_scroll), ("Patch", self.patch),
                                       ("DMX-Monitor", self.dmx), ("Einstellungen", self.settings)]):
            b = QPushButton(name)
            b.setObjectName("sheet")
            b.setCheckable(True)
            self.sheet_group.addButton(b, i)
            tl.addWidget(b)
            self.stack.addWidget(w)
        self.sheet_group.button(0).setChecked(True)
        self.sheet_group.idClicked.connect(self.stack.setCurrentIndex)
        tl.addStretch(1)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.RichText)
        tl.addWidget(self.status)
        tl.addSpacing(10)
        self.btn_black = QPushButton("BLACKOUT  F3")
        self.btn_black.setObjectName("blackout")
        self.btn_black.setCheckable(True)
        self.btn_black.toggled.connect(self.set_blackout)
        tl.addWidget(self.btn_black)
        self.btn_out = QPushButton("AUSGABE  F2")
        self.btn_out.setObjectName("output")
        self.btn_out.setCheckable(True)
        self.btn_out.toggled.connect(self.set_output)
        tl.addWidget(self.btn_out)
        root.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.stack, 1)
        side = QFrame()
        side.setFixedWidth(380)
        side.setStyleSheet("QFrame { background:%s; border-left:1px solid #2c2c2c; }" % C_PANEL)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 10, 10, 10)
        self.preview = PreviewWidget(self)
        sl.addWidget(self.preview)
        self.active_label = QLabel("")
        self.active_label.setWordWrap(True)
        self.active_label.setTextFormat(Qt.RichText)
        self.active_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        sl.addWidget(self.active_label, 1)
        help_l = QLabel("Doppelklick auf ein Bild = Test (lokal 100 %)<br>Rechtsklick = Menü<br>"
                        "F2 Ausgabe · F3 Blackout · Strg+Q Beenden")
        help_l.setStyleSheet("color:%s;" % C_DIM)
        sl.addWidget(help_l)
        bodyw = QWidget()
        bodyw.setLayout(body)
        body.addWidget(side)
        root.addWidget(bodyw, 1)

        QShortcut(QKeySequence("F2"), self, activated=lambda: self.btn_out.toggle())
        QShortcut(QKeySequence("F3"), self, activated=lambda: self.btn_black.toggle())
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda *_: self.settings.fill_screens())
        app.screenRemoved.connect(self.on_screen_removed)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)
        self.slow = QTimer(self)
        self.slow.timeout.connect(self.tick_slow)
        self.slow.start(1000)

        self.resize(1400, 820)
        self.restart_receiver()
        self.patch.refresh()
        self.screen_changed()
        self.tick_slow()
        if self.store.cfg.get("output_on_start"):
            QTimer.singleShot(600, lambda: self.btn_out.setChecked(True))

    # ---------- Empfang
    def restart_receiver(self):
        cfg = self.store.cfg
        self.receiver.start(cfg["protocol"], cfg["interface"], self.store.universes(), cfg["artnet_offset"])
        self.tick_slow()

    def patched_map(self, universe):
        m = {}
        for i, it in enumerate(self.store.images):
            if it["universe"] == universe:
                m[it["address"]] = ("B", "Bild %d · FID %d · %s" % (i + 1, it["fid"], it["name"]))
        cfg = self.store.cfg
        if int(cfg["master_universe"]) == universe and int(cfg["master_address"]):
            m[int(cfg["master_address"])] = ("M", "Master-Kanal")
        return m

    # ---------- Bildschirme
    def output_screen(self):
        screens = QGuiApplication.screens()
        cfg = self.store.cfg
        for s in screens:
            if cfg.get("output_screen") and s.name() == cfg["output_screen"]:
                return s
        idx = int(cfg.get("output_screen_index", -1))
        if 0 <= idx < len(screens):
            return screens[idx]
        prim = QGuiApplication.primaryScreen()
        for s in screens:
            if s is not prim:
                return s
        return prim

    def screen_changed(self):
        sc = self.output_screen()
        if sc:
            sz = sc.size()
            if sz.height():
                self.preview.set_aspect(sz.width() / sz.height())
        if self.output_active():
            self.output.show_on(sc)
        self.prewarm()

    def on_screen_removed(self, *_):
        self.settings.fill_screens()
        if self.output_active():
            QTimer.singleShot(300, self.screen_changed)

    # ---------- Ausgabe
    def output_active(self):
        return self.output.isVisible()

    def set_output(self, on):
        if on:
            self.output.show_on(self.output_screen())
            if self.output_screen() is not self.screen():
                self.activateWindow()
        else:
            self.output.hide()
        if self.btn_out.isChecked() != on:
            self.btn_out.blockSignals(True)
            self.btn_out.setChecked(on)
            self.btn_out.blockSignals(False)
        self._gen += 1
        self.prewarm()

    def set_blackout(self, on):
        self.blackout = on

    def handle_key(self, e):
        if e.key() == Qt.Key_F2:
            self.btn_out.toggle()
        elif e.key() == Qt.Key_F3:
            self.btn_black.toggle()

    def render(self, w, h):
        cfg = self.store.cfg
        key = (w, h)
        hit = self._render_cache.get(key)
        if hit and hit[0] == self._gen:
            return hit[1]
        layers = []
        for it in self.store.images:
            lvl = self.levels.get(it["id"], 0.0) * self.master
            if lvl > 0.002:
                layers.append((it, lvl))
        img = self.lib.compose(w, h, layers, cfg["fit"], cfg["mix"], cfg.get("background", "#000000"))
        if len(self._render_cache) > 4:
            self._render_cache.clear()
        self._render_cache[key] = (self._gen, img)
        return img

    def prewarm(self):
        """Bilder vorab auf Ausgabegröße skalieren, damit der erste Fade nicht ruckelt."""
        if self.output_active():
            w, h = self.output.pixel_size()
        else:
            sc = self.output_screen()
            if not sc:
                return
            s = sc.size() * sc.devicePixelRatio()
            w, h = s.width(), s.height()
        fit = self.store.cfg["fit"]
        self._prewarm_queue = [(it, w, h, fit) for it in self.store.images]

    # ---------- Takt
    def tick(self):
        now = time.perf_counter()
        dt = min(0.1, now - self._last_t)
        self._last_t = now
        self._tick_n += 1
        self.snapshot = self.receiver.snapshot()
        cfg = self.store.cfg
        changed = False

        mu, ma = int(cfg["master_universe"]), int(cfg["master_address"])
        master = 1.0
        if mu and ma:
            d = self.snapshot.get(mu)
            master = (d[ma - 1] / 255.0) if d else 0.0
        if abs(master - self.master) > 1e-4:
            self.master = master
            changed = True

        gin, gout = float(cfg["fade_in"]), float(cfg["fade_out"])
        for it in self.store.images:
            iid = it["id"]
            if self.blackout:
                target = 0.0
            else:
                d = self.snapshot.get(it["universe"])
                target = (d[it["address"] - 1] / 255.0) if d else 0.0
                if iid in self.test_ids:
                    target = 1.0
            cur = self.levels.get(iid, 0.0)
            if target != cur:
                fi = gin if it["fade_in"] is None else it["fade_in"]
                fo = gout if it["fade_out"] is None else it["fade_out"]
                if target > cur:
                    cur = min(target, cur + dt / fi) if fi > 0 else target
                else:
                    cur = max(target, cur - dt / fo) if fo > 0 else target
                self.levels[iid] = cur
                changed = True

        if changed:
            self._gen += 1
            if self.output_active():
                self.output.update()
            self.preview.update()
        elif self._prewarm_queue:
            it, w, h, fit = self._prewarm_queue.pop(0)
            if self.store.by_id(it["id"]) and not self.lib.has_frame(it, w, h, fit):
                self.lib.frame(it, w, h, fit)
        if (changed and self._tick_n % 3 == 0) or self._tick_n % 20 == 0:
            self.pool.update()
            self.update_active_label()
        if self.stack.currentWidget() is self.dmx and self._tick_n % 3 == 0:
            self.dmx.refresh_values()

    def tick_slow(self):
        cfg = self.store.cfg
        stats = self.receiver.stats_copy()
        now = time.monotonic()
        need = self.store.universes()
        live_need = [u for u in need if u in stats and now - stats[u]["last"] < 1.5]
        live_any = [u for u in stats if now - stats[u]["last"] < 1.5]
        proto = "sACN" if cfg["protocol"] == "sacn" else "Art-Net"
        iface = "alle Netzwerke" if cfg["interface"] == "0.0.0.0" else cfg["interface"]
        if self.receiver.error and not live_any:
            sig = "<span style='color:%s'>● %s</span>" % (C_RED, self.receiver.error)
        elif live_need:
            sig = "<span style='color:%s'>● SIGNAL</span>" % C_GREEN
        elif live_any:
            sig = "<span style='color:%s'>● Signal nur auf U%s</span>" % (
                C_ORANGE, ",".join(str(u) for u in sorted(live_any)))
        else:
            sig = "<span style='color:%s'>○ KEIN SIGNAL</span>" % C_DIM
        self.status.setText("<span style='color:%s'>%s · %s</span> &nbsp; %s" % (C_DIM, proto, iface, sig))
        if self.stack.currentWidget() is self.dmx:
            self.dmx.refresh_list()
        self.update_active_label()

    def update_active_label(self):
        rows = []
        for i, it in enumerate(self.store.images):
            lvl = self.levels.get(it["id"], 0.0) * self.master
            if lvl > 0.001:
                rows.append("<span style='color:%s'>%d</span> &nbsp;%s &nbsp;<span style='color:%s'>%d%%</span>" % (
                    C_YELLOW, i + 1, it["name"] or "(ohne Name)", C_DIM, round(lvl * 100)))
        extra = ""
        if self.blackout:
            extra = "<br><span style='color:%s'>BLACKOUT aktiv</span>" % C_RED
        if self.test_ids:
            extra += "<br><span style='color:%s'>Test aktiv: %d Bild(er)</span>" % (C_ORANGE, len(self.test_ids))
        if self.master < 0.999 and int(self.store.cfg["master_address"]):
            extra += "<br><span style='color:%s'>Master: %d%%</span>" % (C_DIM, round(self.master * 100))
        text = "<b>Sichtbar</b><br>" + ("<br>".join(rows) if rows else "<span style='color:%s'>nichts</span>" % C_DIM)
        text += extra
        if text != self.active_label.text():
            self.active_label.setText(text)

    # ---------- Bilder verwalten
    def add_images_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Bilder hinzufügen", "",
                                                "Bilder (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        if files:
            self.add_images(files)

    def add_images(self, files):
        added, errors = self.store.add_files(files)
        if added:
            self.selected_id = added[-1]["id"]
        self.patch_changed()
        if errors:
            QMessageBox.warning(self, "Nicht übernommen",
                                "Diese Dateien konnten nicht übernommen werden:\n" + "\n".join(errors[:20]))

    def remove_image(self, img_id):
        it = self.store.by_id(img_id)
        if not it:
            return
        if QMessageBox.question(self, "Entfernen", "Bild „%s“ (FID %d) entfernen?" % (it["name"], it["fid"])) \
                != QMessageBox.Yes:
            return
        self.store.remove(img_id)
        self.lib.invalidate(img_id)
        self.levels.pop(img_id, None)
        self.test_ids.discard(img_id)
        if self.selected_id == img_id:
            self.selected_id = None
        self.patch_changed()

    def rename_image(self, img_id):
        it = self.store.by_id(img_id)
        if not it:
            return
        name, ok = QInputDialog.getText(self, "Umbenennen", "Name:", text=it["name"])
        if ok:
            it["name"] = name.strip()
            self.store.touch()
            self.patch_changed()

    def replace_image(self, img_id):
        it = self.store.by_id(img_id)
        if not it:
            return
        f, _ = QFileDialog.getOpenFileName(self, "Datei ersetzen", "",
                                           "Bilder (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        if not f:
            return
        added, errors = self.store.add_files([f])
        if not added:
            return
        new = added[0]
        self.store.images.remove(new)
        old_path = self.store.abs_path(it)
        it["file"] = new["file"]
        try:
            if os.path.abspath(old_path).startswith(os.path.abspath(self.store.img_dir)):
                os.remove(old_path)
        except OSError:
            pass
        self.store.touch()
        self.lib.invalidate(img_id)
        self.patch_changed()

    def toggle_test(self, img_id):
        if img_id in self.test_ids:
            self.test_ids.discard(img_id)
        else:
            self.test_ids.add(img_id)
        self.pool.update()

    def patch_changed(self):
        self.receiver.update_universes(self.store.universes())
        self.patch.refresh()
        self.pool.update_height()
        self.pool.update()
        self._gen += 1
        self.preview.update()
        if self.output_active():
            self.output.update()
        self.prewarm()

    def export_thumbs(self):
        if not self.store.images:
            QMessageBox.information(self, "Export", "Noch keine Bilder im Pool.")
            return
        root = QFileDialog.getExistingDirectory(self, "Ziel wählen (z. B. USB-Stick)")
        if not root:
            return
        target = os.path.join(root, "grandMA3", "gma3_library", "media", "images")
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Export", "Ordner konnte nicht angelegt werden:\n%s" % e)
            return
        n = 0
        for it in self.store.images:
            src = self.lib.source(it)
            if src is None:
                continue
            th = src.scaled(512, 288, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            name = re.sub(r"[^\w\-]", "_", it["name"] or "bild")[:40]
            if th.save(os.path.join(target, "%d_%s.png" % (it["fid"], name)), "PNG"):
                n += 1
        QMessageBox.information(self, "Export", "%d Vorschaubilder gespeichert in:\n%s" % (n, target))

    # ---------- Beenden
    def closeEvent(self, e):
        if self.output_active():
            r = QMessageBox.question(self, "Beenden", "Die Ausgabe ist aktiv. Wirklich beenden?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self.store.save()
        self.receiver.stop()
        self.output.hide()
        self.output.deleteLater()
        e.accept()
        QApplication.instance().quit()


# =============================================================================
# Selbsttest (läuft beim GitHub-Build)
# =============================================================================
def run_selftest():
    import tempfile
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines = []
    ok = True

    def check(name, cond):
        nonlocal ok
        lines.append(("OK    " if cond else "FEHLER") + "  " + name)
        ok = ok and bool(cond)

    data = bytearray(512)
    data[0], data[1], data[511] = 255, 128, 7
    r = parse_artnet(build_artnet(6, data))
    check("Art-Net parsen", r and r[0] == 6 and r[1][0] == 255 and r[1][1] == 128 and r[1][511] == 7)
    r = parse_artnet(build_artnet(0x123, data[:3]))
    check("Art-Net Net/SubUni", r and r[0] == 0x123 and len(r[1]) == 4)
    r = parse_sacn(build_sacn(7, data, priority=120))
    check("sACN parsen", r and r[0] == 7 and r[1][1] == 128 and r[2] == 120 and len(r[1]) == 512)
    check("sACN Gruppe", sacn_group(7) == "239.255.0.7" and sacn_group(300) == "239.255.1.44")

    app = QApplication.instance() or QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="bz_test_")

    for proto, port in (("artnet", 16454), ("sacn", 15568)):
        rec = DmxReceiver()
        rec.port_override = port
        started = rec.start(proto, "0.0.0.0", {7}, 1)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pkt = build_artnet(6, data) if proto == "artnet" else build_sacn(7, data)
        got = None
        for _ in range(40):
            tx.sendto(pkt, ("127.0.0.1", port))
            time.sleep(0.05)
            snap = rec.snapshot()
            if 7 in snap:
                got = snap[7]
                break
        tx.close()
        rec.stop()
        check("%s Empfang über UDP (U7 = Art-Net 6)" % proto if proto == "artnet" else "sACN Empfang über UDP",
              started and got is not None and got[1] == 128)

    red = QImage(64, 36, QImage.Format_RGB32)
    red.fill(QColor(255, 0, 0))
    blue = QImage(100, 100, QImage.Format_RGB32)
    blue.fill(QColor(0, 0, 255))
    red.save(os.path.join(tmp, "rot.png"))
    blue.save(os.path.join(tmp, "blau.png"))

    win = MainWindow(os.path.join(tmp, "daten"))
    win.show()
    added, err = win.store.add_files([os.path.join(tmp, "rot.png"), os.path.join(tmp, "blau.png")])
    win.patch_changed()
    check("Bilder hinzufügen", len(added) == 2 and not err)
    check("Auto-Patch Startwerte", added[0]["fid"] == 9001 and fmt_addr(added[0]["universe"], added[0]["address"]) == "7.001"
          and added[1]["fid"] == 9002 and added[1]["address"] == 2)
    a, b = added
    mixed = win.lib.compose(160, 90, [(a, 0.5), (b, 0.5)], "stretch", "add", "#000000")
    c = mixed.pixelColor(80, 45)
    check("Crossfade additiv (Mitte ~50/50) r=%d b=%d" % (c.red(), c.blue()),
          abs(c.red() - 128) < 6 and abs(c.blue() - 128) < 6)
    over = win.lib.compose(160, 90, [(a, 1.0), (b, 1.0)], "stretch", "over", "#000000")
    c = over.pixelColor(80, 45)
    check("Überdecken (höhere FID oben)", c.blue() > 250 and c.red() < 5)
    cont = win.lib.compose(160, 160, [(a, 1.0)], "contain", "add", "#000000")
    check("Einpassen mit Rändern", cont.pixelColor(80, 5).red() < 5 and cont.pixelColor(80, 80).red() > 250)

    win.receiver.stop()
    win.receiver.protocol = "artnet"
    win.receiver.artnet_offset = 1
    d2 = bytearray(512)
    d2[0] = 255
    win.receiver._handle(build_artnet(6, d2), "127.0.0.1")
    for _ in range(5):
        win.tick()
    check("DMX -> Pegel Bild 1 = 100 %%, Bild 2 = 0 %%",
          abs(win.levels.get(a["id"], 0) - 1) < 1e-6 and win.levels.get(b["id"], 0) == 0)
    win.store.save()
    check("Show gespeichert", os.path.exists(os.path.join(tmp, "daten", "show.json")))
    win.store.set_universe(12)
    check("Universum wechseln (7 -> 12, Adressen bleiben)",
          a["universe"] == 12 and a["address"] == 1 and b["universe"] == 12 and b["address"] == 2)
    win.patch_changed()
    check("Universum-Feld im Patch", win.patch.uni.value() == 12)
    base = {3: bytes(512)}
    snap = {3: bytes(512), 5: bytes([255] * 512)}
    first = detect_new_channel(base, snap)
    lvl = bytearray(512)
    lvl[40] = 255
    snap2 = {3: bytes(lvl), 5: bytes([255] * 512)}
    check("Lernen: neues Universum zählt nicht, Kanal 3.041 erkannt",
          first is None and detect_new_channel(base, snap2) == (3, 41, 255))
    win.store.repatch_addresses(3, 41)
    check("Lernen übernehmen -> 3.041 / 3.042, FIDs bleiben",
          (a["universe"], a["address"], b["address"], a["fid"]) == (3, 41, 42, 9001))
    dlg = LearnDialog(win)
    dlg.poll()
    dlg.close()
    check("Lern-Dialog öffnen", True)
    for i in range(4):
        win.stack.setCurrentIndex(i)
        app.processEvents()
    win.dmx.refresh_list()
    win.dmx.refresh_values()
    app.processEvents()
    win.output.hide()
    win.close()
    app.processEvents()

    text = "%s %s Selbsttest\n" % (APP_NAME, APP_VERSION) + "\n".join(lines) + \
           "\n\nERGEBNIS: " + ("ALLES OK" if ok else "FEHLER") + "\n"
    log(text)
    try:
        with open(os.path.join(app_base_dir(), "selbsttest.txt"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    data_dir = None
    if "--data" in sys.argv:
        i = sys.argv.index("--data")
        if i + 1 < len(sys.argv):
            data_dir = sys.argv[i + 1]
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    win = MainWindow(data_dir or default_data_dir())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

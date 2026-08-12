import base64
import ctypes
import io
import json
import math
import os
import re
import secrets
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

import pymupdf
from PIL import Image, ImageDraw, ImageOps, ImageWin

import win32print


# ============================================================
# PhonePrint v3
# Windows + Android/browser remote print UI
# ============================================================

HOST = "0.0.0.0"
PORT = 5000
MAX_UPLOAD_MB = 200
SESSION_TTL_SECONDS = 60 * 60 * 8   # 8 hours

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

PRINT_PIN = os.environ.get("PRINT_PIN") or f"{secrets.randbelow(1_000_000):06d}"

job_lock = threading.Lock()
local_jobs = {}
job_counter = 0

session_lock = threading.Lock()
pdf_sessions = {}


# ============================================================
# Win32 constants / native API
# ============================================================

# DeviceCapabilities
DC_FIELDS = 1
DC_PAPERS = 2
DC_PAPERSIZE = 3
DC_BINS = 6
DC_DUPLEX = 7
DC_BINNAMES = 12
DC_ENUMRESOLUTIONS = 13
DC_PAPERNAMES = 16
DC_ORIENTATION = 17
DC_COPIES = 18
DC_COLLATE = 22
DC_COLORDEVICE = 32
DC_NUP = 33

# DEVMODE dmFields
DM_ORIENTATION = 0x00000001
DM_PAPERSIZE = 0x00000002
DM_COPIES = 0x00000100
DM_DEFAULTSOURCE = 0x00000200
DM_PRINTQUALITY = 0x00000400
DM_COLOR = 0x00000800
DM_DUPLEX = 0x00001000
DM_YRESOLUTION = 0x00002000
DM_COLLATE = 0x00008000

DMORIENT_PORTRAIT = 1
DMORIENT_LANDSCAPE = 2
DMCOLOR_MONOCHROME = 1
DMCOLOR_COLOR = 2
DMDUP_SIMPLEX = 1
DMDUP_VERTICAL = 2
DMDUP_HORIZONTAL = 3
DMCOLLATE_FALSE = 0
DMCOLLATE_TRUE = 1

# GetDeviceCaps
HORZRES = 8
VERTRES = 10
LOGPIXELSX = 88
LOGPIXELSY = 90
PHYSICALWIDTH = 110
PHYSICALHEIGHT = 111
PHYSICALOFFSETX = 112
PHYSICALOFFSETY = 113

JOB_CONTROL_CANCEL = 3

DM_OUT_BUFFER = 0x00000002
DM_IN_BUFFER = 0x00000008
IDOK = 1


winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

DeviceCapabilitiesW = winspool.DeviceCapabilitiesW
DeviceCapabilitiesW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.WORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
DeviceCapabilitiesW.restype = ctypes.c_int

DocumentPropertiesW = winspool.DocumentPropertiesW
DocumentPropertiesW.argtypes = [
    wintypes.HWND,
    wintypes.HANDLE,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
]
DocumentPropertiesW.restype = ctypes.c_long

CreateDCW = gdi32.CreateDCW
CreateDCW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
]
CreateDCW.restype = wintypes.HDC

GetDeviceCaps = gdi32.GetDeviceCaps
GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
GetDeviceCaps.restype = ctypes.c_int

StartPageW = gdi32.StartPage
StartPageW.argtypes = [wintypes.HDC]
StartPageW.restype = ctypes.c_int

EndPageW = gdi32.EndPage
EndPageW.argtypes = [wintypes.HDC]
EndPageW.restype = ctypes.c_int

EndDocW = gdi32.EndDoc
EndDocW.argtypes = [wintypes.HDC]
EndDocW.restype = ctypes.c_int

AbortDocW = gdi32.AbortDoc
AbortDocW.argtypes = [wintypes.HDC]
AbortDocW.restype = ctypes.c_int

DeleteDCW = gdi32.DeleteDC
DeleteDCW.argtypes = [wintypes.HDC]
DeleteDCW.restype = wintypes.BOOL


class DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wintypes.LPCWSTR),
        ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR),
        ("fwType", wintypes.DWORD),
    ]


StartDocW = gdi32.StartDocW
StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(DOCINFOW)]
StartDocW.restype = ctypes.c_int


class DEVMODEW_PUBLIC(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),

        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),

        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class NativePrinterDC:
    def __init__(self, hdc, devmode_buffer):
        if not hdc:
            err = ctypes.get_last_error()
            raise OSError(err, "CreateDCW failed.")
        self.hdc = hdc
        self._devmode_buffer = devmode_buffer

    def get_caps(self, index):
        return GetDeviceCaps(self.hdc, index)

    def start_doc(self, title):
        di = DOCINFOW()
        di.cbSize = ctypes.sizeof(DOCINFOW)
        di.lpszDocName = str(title)
        rc = StartDocW(self.hdc, ctypes.byref(di))
        if rc <= 0:
            raise OSError(ctypes.get_last_error(), "StartDocW failed.")
        return rc

    def start_page(self):
        rc = StartPageW(self.hdc)
        if rc <= 0:
            raise OSError(ctypes.get_last_error(), "StartPage failed.")

    def end_page(self):
        rc = EndPageW(self.hdc)
        if rc <= 0:
            raise OSError(ctypes.get_last_error(), "EndPage failed.")

    def end_doc(self):
        rc = EndDocW(self.hdc)
        if rc <= 0:
            raise OSError(ctypes.get_last_error(), "EndDoc failed.")

    def abort_doc(self):
        if self.hdc:
            AbortDocW(self.hdc)

    def close(self):
        if self.hdc:
            DeleteDCW(self.hdc)
            self.hdc = None


# ============================================================
# Printer enumeration / capabilities / status
# ============================================================

PRINTER_STATUS_FLAGS = [
    (0x00000001, "Paused"),
    (0x00000002, "Error"),
    (0x00000004, "Pending deletion"),
    (0x00000008, "Paper jam"),
    (0x00000010, "Paper out"),
    (0x00000020, "Manual feed"),
    (0x00000040, "Paper problem"),
    (0x00000080, "Offline"),
    (0x00000100, "I/O active"),
    (0x00000200, "Busy"),
    (0x00000400, "Printing"),
    (0x00000800, "Output bin full"),
    (0x00001000, "Not available"),
    (0x00002000, "Waiting"),
    (0x00004000, "Processing"),
    (0x00008000, "Initializing"),
    (0x00010000, "Warming up"),
    (0x00020000, "Toner low"),
    (0x00040000, "No toner"),
    (0x00080000, "Page punt"),
    (0x00100000, "User intervention"),
    (0x00200000, "Out of memory"),
    (0x00400000, "Door open"),
    (0x00800000, "Server unknown"),
    (0x01000000, "Power save"),
]


def enum_printer_infos():
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    entries = win32print.EnumPrinters(flags, None, 2)
    out = []
    for e in entries:
        name = e.get("pPrinterName")
        if not name:
            continue
        status = int(e.get("Status") or 0)
        out.append({
            "name": name,
            "driver": e.get("pDriverName") or "",
            "port": e.get("pPortName") or "",
            "location": e.get("pLocation") or "",
            "comment": e.get("pComment") or "",
            "attributes": int(e.get("Attributes") or 0),
            "status": status,
            "status_text": printer_status_text(status),
            "jobs": int(e.get("cJobs") or 0),
        })
    out.sort(key=lambda x: x["name"].lower())
    return out


def printer_status_text(status):
    if not status:
        return "Ready / no spooler error"
    labels = [label for flag, label in PRINTER_STATUS_FLAGS if status & flag]
    return ", ".join(labels) if labels else f"Status 0x{status:08X}"


def installed_printer_names():
    return [p["name"] for p in enum_printer_infos()]


def default_printer_name():
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        names = installed_printer_names()
        return names[0] if names else None


def printer_info(printer_name):
    h = win32print.OpenPrinter(printer_name)
    try:
        info = win32print.GetPrinter(h, 2)
        status = int(info.get("Status") or 0)
        return {
            "name": printer_name,
            "driver": info.get("pDriverName") or "",
            "port": info.get("pPortName") or "",
            "location": info.get("pLocation") or "",
            "comment": info.get("pComment") or "",
            "attributes": int(info.get("Attributes") or 0),
            "status": status,
            "status_text": printer_status_text(status),
            "jobs": int(info.get("cJobs") or 0),
            "devmode": info.get("pDevMode"),
        }
    finally:
        win32print.ClosePrinter(h)


def dc_scalar(device, port, cap):
    return int(DeviceCapabilitiesW(device, port, cap, None, None))


def dc_array(device, port, cap, ctype, count=None):
    if count is None:
        count = dc_scalar(device, port, cap)
    if count <= 0:
        return []
    arr = (ctype * count)()
    rc = DeviceCapabilitiesW(device, port, cap, ctypes.byref(arr), None)
    if rc < 0:
        return []
    return list(arr)


def dc_fixed_strings(device, port, cap, width):
    count = dc_scalar(device, port, cap)
    if count <= 0:
        return []
    buf = ctypes.create_unicode_buffer(count * width)
    rc = DeviceCapabilitiesW(device, port, cap, ctypes.byref(buf), None)
    if rc < 0:
        return []
    raw = buf[:]
    result = []
    for i in range(count):
        s = raw[i * width:(i + 1) * width].split("\x00", 1)[0].strip()
        result.append(s)
    return result


def current_devmode_values(dm):
    if dm is None:
        return {}

    def gv(attr, fallback=None):
        try:
            return getattr(dm, attr)
        except Exception:
            return fallback

    return {
        "orientation": gv("Orientation"),
        "paper_id": gv("PaperSize"),
        "copies": gv("Copies"),
        "source_id": gv("DefaultSource"),
        "quality": gv("PrintQuality"),
        "color": gv("Color"),
        "duplex": gv("Duplex"),
        "y_resolution": gv("YResolution"),
        "collate": gv("Collate"),
        "fields": int(gv("Fields", 0) or 0),
        "driver_extra": int(gv("DriverExtra", 0) or 0),
    }


def looks_virtual(info):
    hay = " ".join([
        info.get("name", ""),
        info.get("driver", ""),
        info.get("port", ""),
    ]).lower()
    hints = ["pdf", "xps", "onenote", "fax", "document writer", "portprompt:", "file:"]
    return any(h in hay for h in hints)


def get_capabilities(printer_name):
    info = printer_info(printer_name)
    device = info["name"]
    port = info["port"]
    dm = info["devmode"]

    caps = {
        "printer": {
            "name": device,
            "driver": info["driver"],
            "port": port,
            "location": info["location"],
            "comment": info["comment"],
            "virtual": looks_virtual(info),
            "status": info["status"],
            "status_text": info["status_text"],
            "jobs": info["jobs"],
        },
        "defaults": current_devmode_values(dm),
        "papers": [],
        "bins": [],
        "resolutions": [],
        "duplex": False,
        "color": False,
        "collate": False,
        "max_copies": 1,
        "driver_nup": [],
        "software_nup": [1, 2, 4, 6, 9, 16],
    }

    paper_ids = dc_array(device, port, DC_PAPERS, ctypes.c_ushort)
    paper_names = dc_fixed_strings(device, port, DC_PAPERNAMES, 64)

    point_count = dc_scalar(device, port, DC_PAPERSIZE)
    paper_points = []
    if point_count > 0:
        arr = (wintypes.POINT * point_count)()
        rc = DeviceCapabilitiesW(device, port, DC_PAPERSIZE, ctypes.byref(arr), None)
        if rc >= 0:
            paper_points = list(arr)

    for i, pid in enumerate(paper_ids):
        pname = paper_names[i] if i < len(paper_names) and paper_names[i] else f"Paper {pid}"
        size = None
        if i < len(paper_points):
            p = paper_points[i]
            size = {"width_mm": round(p.x / 10.0, 1), "height_mm": round(p.y / 10.0, 1)}
        caps["papers"].append({"id": int(pid), "name": pname, "size": size})

    bin_ids = dc_array(device, port, DC_BINS, ctypes.c_ushort)
    bin_names = dc_fixed_strings(device, port, DC_BINNAMES, 24)
    for i, bid in enumerate(bin_ids):
        bname = bin_names[i] if i < len(bin_names) and bin_names[i] else f"Source {bid}"
        caps["bins"].append({"id": int(bid), "name": bname})

    res_count = dc_scalar(device, port, DC_ENUMRESOLUTIONS)
    if res_count > 0:
        arr = (wintypes.LONG * (res_count * 2))()
        rc = DeviceCapabilitiesW(device, port, DC_ENUMRESOLUTIONS, ctypes.byref(arr), None)
        if rc >= 0:
            seen = set()
            for i in range(res_count):
                x = int(arr[i * 2])
                y = int(arr[i * 2 + 1])
                if x > 0 and y > 0 and (x, y) not in seen:
                    caps["resolutions"].append({"x": x, "y": y})
                    seen.add((x, y))

    caps["duplex"] = dc_scalar(device, port, DC_DUPLEX) == 1
    caps["color"] = dc_scalar(device, port, DC_COLORDEVICE) == 1
    caps["collate"] = dc_scalar(device, port, DC_COLLATE) == 1

    mc = dc_scalar(device, port, DC_COPIES)
    caps["max_copies"] = max(1, mc if mc > 0 else 1)

    nup_count = dc_scalar(device, port, DC_NUP)
    if nup_count > 0:
        vals = dc_array(device, port, DC_NUP, wintypes.DWORD, nup_count)
        caps["driver_nup"] = sorted({int(v) for v in vals if int(v) > 0})

    return caps


# ============================================================
# PDF session / validation / preview helpers
# ============================================================

def cleanup_expired_sessions():
    now = time.time()
    expired = []
    with session_lock:
        for sid, data in pdf_sessions.items():
            if now - data["created"] > SESSION_TTL_SECONDS:
                expired.append((sid, data))
        for sid, _ in expired:
            pdf_sessions.pop(sid, None)

    for _, data in expired:
        try:
            Path(data["path"]).unlink(missing_ok=True)
        except Exception:
            pass


def get_pdf_session(session_id):
    cleanup_expired_sessions()
    with session_lock:
        data = pdf_sessions.get(session_id)
    if not data:
        raise ValueError("PDF session expired or does not exist. Upload the PDF again.")
    return data


def inspect_pdf(path):
    doc = pymupdf.open(path)
    try:
        page_count = len(doc)
        pages = []
        sizes = []
        for i in range(page_count):
            page = doc[i]
            r = page.rect
            width_mm = round(r.width * 25.4 / 72.0, 1)
            height_mm = round(r.height * 25.4 / 72.0, 1)
            sizes.append((width_mm, height_mm))
            pages.append({
                "page": i + 1,
                "width_pt": round(r.width, 2),
                "height_pt": round(r.height, 2),
                "width_mm": width_mm,
                "height_mm": height_mm,
                "orientation": "landscape" if r.width > r.height else "portrait",
            })

        unique_sizes = sorted({(w, h) for w, h in sizes})
        mixed_sizes = len(unique_sizes) > 1
        mixed_orientation = len({p["orientation"] for p in pages}) > 1 if pages else False

        return {
            "page_count": page_count,
            "pages": pages,
            "mixed_sizes": mixed_sizes,
            "mixed_orientation": mixed_orientation,
            "unique_sizes": [{"width_mm": w, "height_mm": h} for w, h in unique_sizes[:20]],
            "metadata": doc.metadata or {},
        }
    finally:
        doc.close()


def parse_page_range(text, page_count):
    text = (text or "all").strip().lower()
    if page_count <= 0:
        raise ValueError("This PDF has no pages.")

    if text in ("", "all", "*"):
        return list(range(page_count))

    result = []
    seen = set()

    for token in text.split(","):
        token = token.strip()
        if not token:
            continue

        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", token)
        if not m:
            raise ValueError(f"Invalid page item: {token!r}.")

        start = int(m.group(1))
        end = int(m.group(2) or start)

        if start < 1 or end < 1:
            raise ValueError("Page numbers start at 1.")

        if start > page_count or end > page_count:
            bad = max(start, end)
            raise ValueError(f"Page {bad} does not exist. This PDF has {page_count} pages.")

        step = 1 if end >= start else -1
        for p in range(start, end + step, step):
            p0 = p - 1
            if p0 not in seen:
                result.append(p0)
                seen.add(p0)

    if not result:
        raise ValueError("No pages are selected.")

    return result


def odd_even_filter(pages, mode):
    if mode == "odd":
        return [p for p in pages if (p + 1) % 2 == 1]
    if mode == "even":
        return [p for p in pages if (p + 1) % 2 == 0]
    return pages


def validate_selection(page_range, odd_even, page_count):
    pages = parse_page_range(page_range, page_count)
    pages = odd_even_filter(pages, odd_even)
    if not pages:
        raise ValueError("The current page range and odd/even filter select no pages.")
    return pages


def nup_grid(n):
    return {
        1: (1, 1),
        2: (2, 1),
        4: (2, 2),
        6: (2, 3),
        9: (3, 3),
        16: (4, 4),
    }[n]


def render_thumbnail(path, page_index, max_width=260):
    doc = pymupdf.open(path)
    try:
        if page_index < 0 or page_index >= len(doc):
            raise ValueError("Invalid page.")
        page = doc[page_index]
        zoom = max_width / max(page.rect.width, 1)
        matrix = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
        data = pix.tobytes("png")
        return data
    finally:
        doc.close()


def get_selected_paper_mm(caps, paper_id, orientation):
    selected = None
    if paper_id is not None:
        for p in caps["papers"]:
            if p["id"] == paper_id:
                selected = p
                break

    if selected and selected.get("size"):
        w = float(selected["size"]["width_mm"])
        h = float(selected["size"]["height_mm"])
    else:
        # Fallback A4-style preview when driver default has no published size.
        w, h = 210.0, 297.0

    if orientation == "landscape" and h > w:
        w, h = h, w
    elif orientation == "portrait" and w > h:
        w, h = h, w

    return w, h


def preview_image(path, pages, paper_mm, nup, margins, scale_mode, scale_percent, sheet_index=0):
    paper_w_mm, paper_h_mm = paper_mm

    # Keep browser preview reasonably sized.
    target_w = 900
    target_h = max(200, int(round(target_w * paper_h_mm / paper_w_mm)))
    target_h = min(target_h, 1200)

    canvas = Image.new("RGB", (target_w, target_h), "white")
    draw = ImageDraw.Draw(canvas)

    px_per_mm_x = target_w / paper_w_mm
    px_per_mm_y = target_h / paper_h_mm

    left = int(margins["left"] * px_per_mm_x)
    right = target_w - int(margins["right"] * px_per_mm_x)
    top = int(margins["top"] * px_per_mm_y)
    bottom = target_h - int(margins["bottom"] * px_per_mm_y)

    draw.rectangle([0, 0, target_w - 1, target_h - 1], outline=(150, 150, 150), width=2)

    cols, rows = nup_grid(nup)
    gap = 8
    usable_w = max(1, right - left)
    usable_h = max(1, bottom - top)
    cell_w = max(1, (usable_w - gap * (cols - 1)) // cols)
    cell_h = max(1, (usable_h - gap * (rows - 1)) // rows)

    start = sheet_index * nup
    sheet_pages = pages[start:start + nup]

    doc = pymupdf.open(path)
    try:
        for slot, pno in enumerate(sheet_pages):
            page = doc[pno]
            pix = page.get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), colorspace=pymupdf.csRGB, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            col = slot % cols
            row = slot // cols
            x0 = left + col * (cell_w + gap)
            y0 = top + row * (cell_h + gap)
            x1 = x0 + cell_w
            y1 = y0 + cell_h

            fit = min(cell_w / img.width, cell_h / img.height)
            if scale_mode == "percent":
                fit *= scale_percent / 100.0
            elif scale_mode == "actual":
                fit = min(fit, 1.0)
            fit = min(fit, min(cell_w / img.width, cell_h / img.height))

            ow = max(1, int(img.width * fit))
            oh = max(1, int(img.height * fit))
            resized = img.resize((ow, oh), Image.Resampling.LANCZOS)

            dx = x0 + (cell_w - ow) // 2
            dy = y0 + (cell_h - oh) // 2

            draw.rectangle([x0, y0, x1, y1], outline=(215, 215, 215), width=1)
            canvas.paste(resized, (dx, dy))

    finally:
        doc.close()

    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    out.seek(0)
    return out


# ============================================================
# Settings parsing / native DEVMODE / printing
# ============================================================

def optional_int(form, name):
    raw = (form.get(name) or "").strip().lower()
    if raw in ("", "default", "none"):
        return None
    return int(raw)


def bounded_number(form, name, default, lo, hi):
    raw = form.get(name, default)
    try:
        value = float(raw)
    except Exception:
        raise ValueError(f"{name} must be numeric.")
    if value < lo or value > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}.")
    return value


def parse_settings(form, caps):
    copies = int(form.get("copies", 1))
    if not 1 <= copies <= max(1, caps["max_copies"]):
        raise ValueError(f"Copies must be between 1 and {caps['max_copies']}.")

    nup = int(form.get("nup", 1))
    if nup not in {1, 2, 4, 6, 9, 16}:
        raise ValueError("Invalid pages-per-sheet value.")

    orientation = form.get("orientation", "portrait")
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("Invalid orientation.")

    duplex = form.get("duplex", "simplex")
    if duplex not in {"simplex", "long", "short"}:
        raise ValueError("Invalid duplex option.")
    if not caps["duplex"] and duplex != "simplex":
        raise ValueError("This printer does not report duplex support.")

    color = form.get("color", "auto")
    if color not in {"auto", "mono", "color"}:
        raise ValueError("Invalid color option.")
    if color == "color" and not caps["color"]:
        raise ValueError("This printer does not report color support.")

    collate = form.get("collate", "off")
    if collate not in {"off", "on"}:
        raise ValueError("Invalid collate option.")

    scaling = form.get("scaling", "fit")
    if scaling not in {"fit", "actual", "percent"}:
        raise ValueError("Invalid scaling mode.")

    paper_id = optional_int(form, "paper_id")
    if paper_id is not None and paper_id not in {p["id"] for p in caps["papers"]}:
        raise ValueError("Selected paper is not supported by this printer.")

    source_id = optional_int(form, "source_id")
    if source_id is not None and source_id not in {b["id"] for b in caps["bins"]}:
        raise ValueError("Selected paper source is not supported.")

    rx = optional_int(form, "resolution_x")
    ry = optional_int(form, "resolution_y")
    if (rx is None) != (ry is None):
        raise ValueError("Resolution X and Y must be selected together.")
    if rx is not None:
        if (rx, ry) not in {(r["x"], r["y"]) for r in caps["resolutions"]}:
            raise ValueError("Selected resolution is not reported by this printer.")

    return {
        "page_range": form.get("page_range", "all"),
        "odd_even": form.get("odd_even", "all"),
        "copies": copies,
        "orientation": orientation,
        "paper_id": paper_id,
        "source_id": source_id,
        "duplex": duplex,
        "color": color,
        "collate": collate,
        "resolution_x": rx,
        "resolution_y": ry,
        "nup": nup,
        "scaling": scaling,
        "scale_percent": bounded_number(form, "scale_percent", 100, 10, 100),
        "margins": {
            "top": bounded_number(form, "margin_top", 5, 0, 50),
            "right": bounded_number(form, "margin_right", 5, 0, 50),
            "bottom": bounded_number(form, "margin_bottom", 5, 0, 50),
            "left": bounded_number(form, "margin_left", 5, 0, 50),
        },
    }


def build_native_devmode(printer_name, settings):
    h = win32print.OpenPrinter(printer_name)
    try:
        hraw = int(h)

        size = DocumentPropertiesW(None, hraw, printer_name, None, None, 0)
        if size <= 0:
            raise RuntimeError(f"DocumentPropertiesW could not determine DEVMODE size (rc={size}).")

        base = ctypes.create_string_buffer(size)
        rc = DocumentPropertiesW(None, hraw, printer_name, ctypes.byref(base), None, DM_OUT_BUFFER)
        if rc != IDOK:
            raise RuntimeError(f"Could not obtain printer defaults (DocumentPropertiesW rc={rc}).")

        dm = DEVMODEW_PUBLIC.from_buffer(base)
        fields = int(dm.dmFields)

        if settings["orientation"] == "portrait":
            dm.dmOrientation = DMORIENT_PORTRAIT
            fields |= DM_ORIENTATION
        else:
            dm.dmOrientation = DMORIENT_LANDSCAPE
            fields |= DM_ORIENTATION

        if settings["paper_id"] is not None:
            dm.dmPaperSize = int(settings["paper_id"])
            fields |= DM_PAPERSIZE

        if settings["source_id"] is not None:
            dm.dmDefaultSource = int(settings["source_id"])
            fields |= DM_DEFAULTSOURCE

        dm.dmCopies = int(settings["copies"])
        fields |= DM_COPIES

        if settings["duplex"] == "simplex":
            dm.dmDuplex = DMDUP_SIMPLEX
            fields |= DM_DUPLEX
        elif settings["duplex"] == "long":
            dm.dmDuplex = DMDUP_VERTICAL
            fields |= DM_DUPLEX
        elif settings["duplex"] == "short":
            dm.dmDuplex = DMDUP_HORIZONTAL
            fields |= DM_DUPLEX

        if settings["color"] == "mono":
            dm.dmColor = DMCOLOR_MONOCHROME
            fields |= DM_COLOR
        elif settings["color"] == "color":
            dm.dmColor = DMCOLOR_COLOR
            fields |= DM_COLOR

        if settings["collate"] == "on":
            dm.dmCollate = DMCOLLATE_TRUE
            fields |= DM_COLLATE
        elif settings["collate"] == "off":
            dm.dmCollate = DMCOLLATE_FALSE
            fields |= DM_COLLATE

        if settings["resolution_x"] is not None:
            dm.dmPrintQuality = int(settings["resolution_x"])
            dm.dmYResolution = int(settings["resolution_y"])
            fields |= DM_PRINTQUALITY | DM_YRESOLUTION

        dm.dmFields = fields

        validated = ctypes.create_string_buffer(size)
        rc = DocumentPropertiesW(
            None,
            hraw,
            printer_name,
            ctypes.byref(validated),
            ctypes.byref(base),
            DM_IN_BUFFER | DM_OUT_BUFFER,
        )
        if rc != IDOK:
            raise RuntimeError(f"Printer driver rejected requested settings (rc={rc}).")

        return validated
    finally:
        win32print.ClosePrinter(h)


def create_printer_dc(printer_name, settings):
    dm_buffer = build_native_devmode(printer_name, settings)
    hdc = CreateDCW("WINSPOOL", printer_name, None, ctypes.byref(dm_buffer))
    return NativePrinterDC(hdc, dm_buffer)


def mm_to_px(mm, dpi):
    return int(round(float(mm) / 25.4 * dpi))


def render_pdf_page(page, dpi=300, mono=False):
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if mono:
        image = image.convert("L").convert("RGB")
    return image


def fit_rect(cell, image_size, mode, percent):
    left, top, right, bottom = cell
    cw = max(1, right - left)
    ch = max(1, bottom - top)
    iw, ih = image_size
    fit = min(cw / iw, ch / ih)

    if mode == "actual":
        factor = min(1.0, fit)
    elif mode == "percent":
        factor = fit * (percent / 100.0)
    else:
        factor = fit

    factor = min(factor, fit)
    ow = max(1, int(round(iw * factor)))
    oh = max(1, int(round(ih * factor)))
    x = left + (cw - ow) // 2
    y = top + (ch - oh) // 2
    return (x, y, x + ow, y + oh)


def do_print(pdf_path, original_name, printer_name, settings, pages, local_id):
    doc = None
    dc = None
    try:
        with job_lock:
            local_jobs[local_id]["status"] = "Preparing"

        doc = pymupdf.open(pdf_path)
        dc = create_printer_dc(printer_name, settings)

        dpi_x = dc.get_caps(LOGPIXELSX)
        dpi_y = dc.get_caps(LOGPIXELSY)
        printable_w = dc.get_caps(HORZRES)
        printable_h = dc.get_caps(VERTRES)

        m = settings["margins"]
        left = min(max(0, mm_to_px(m["left"], dpi_x)), printable_w - 1)
        right = max(left + 1, min(printable_w, printable_w - mm_to_px(m["right"], dpi_x)))
        top = min(max(0, mm_to_px(m["top"], dpi_y)), printable_h - 1)
        bottom = max(top + 1, min(printable_h, printable_h - mm_to_px(m["bottom"], dpi_y)))

        cols, rows = nup_grid(settings["nup"])
        gap_x = mm_to_px(2, dpi_x) if cols > 1 else 0
        gap_y = mm_to_px(2, dpi_y) if rows > 1 else 0
        cell_w = max(1, ((right - left) - gap_x * (cols - 1)) // cols)
        cell_h = max(1, ((bottom - top) - gap_y * (rows - 1)) // rows)

        dc.start_doc(f"PhonePrint - {original_name}")

        cursor = 0
        total = len(pages)
        while cursor < total:
            dc.start_page()

            for slot in range(settings["nup"]):
                if cursor >= total:
                    break

                pno = pages[cursor]
                page = doc[pno]
                image = render_pdf_page(page, dpi=300, mono=settings["color"] == "mono")

                col = slot % cols
                row = slot // cols
                x0 = left + col * (cell_w + gap_x)
                y0 = top + row * (cell_h + gap_y)
                cell = (x0, y0, x0 + cell_w, y0 + cell_h)

                dest = fit_rect(cell, image.size, settings["scaling"], settings["scale_percent"])
                ImageWin.Dib(image).draw(int(dc.hdc), dest)

                cursor += 1
                with job_lock:
                    local_jobs[local_id]["status"] = f"Rendering {cursor}/{total}"

            dc.end_page()

        dc.end_doc()

        with job_lock:
            local_jobs[local_id]["status"] = "Sent"
            local_jobs[local_id]["finished"] = time.strftime("%H:%M:%S")

    except Exception as exc:
        if dc:
            try:
                dc.abort_doc()
            except Exception:
                pass
        with job_lock:
            local_jobs[local_id]["status"] = "Error"
            local_jobs[local_id]["error"] = str(exc)
            local_jobs[local_id]["traceback"] = traceback.format_exc()
    finally:
        if dc:
            try:
                dc.close()
            except Exception:
                pass
        if doc:
            try:
                doc.close()
            except Exception:
                pass


# ============================================================
# Queue helpers
# ============================================================

def windows_queue(printer_name):
    h = win32print.OpenPrinter(printer_name)
    try:
        jobs = win32print.EnumJobs(h, 0, 999, 1)
        return [{
            "id": int(j.get("JobId") or 0),
            "document": j.get("pDocument") or "",
            "user": j.get("pUserName") or "",
            "status": j.get("pStatus") or "",
            "pages_printed": int(j.get("PagesPrinted") or 0),
            "total_pages": int(j.get("TotalPages") or 0),
        } for j in jobs]
    finally:
        win32print.ClosePrinter(h)


def cancel_windows_job(printer_name, job_id):
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.SetJob(h, int(job_id), 0, None, JOB_CONTROL_CANCEL)
    finally:
        win32print.ClosePrinter(h)


# ============================================================
# UI
# ============================================================

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>PhonePrint v3</title>
<style>
:root{
  --bg:#f4f6f8;--surface:#fff;--surface2:#f8fafc;--text:#17191d;--muted:#69707c;
  --border:#dfe3e8;--field:#fff;--accent:#1769e0;--accent2:#0f56c4;--soft:#eaf2ff;
  --danger:#b42318;--ok:#067647;--warn:#b54708;--shadow:0 10px 35px rgba(20,30,50,.07)
}
html[data-theme="dark"]{
  --bg:#0d1014;--surface:#15191e;--surface2:#1b2026;--text:#f4f6f8;--muted:#a8afb9;
  --border:#2d333b;--field:#11151a;--accent:#79aaff;--accent2:#94bbff;--soft:#17243a;
  --danger:#ff8b83;--ok:#75e0a7;--warn:#fdb022;--shadow:0 10px 35px rgba(0,0,0,.28)
}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
button,input,select{font:inherit}.shell{width:min(1180px,100%);margin:auto;padding:14px 14px 110px}
.appbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.brand{display:flex;align-items:center;gap:10px}.logo{width:42px;height:42px;border-radius:13px;background:var(--accent);display:grid;place-items:center}
h1{font-size:1.28rem;margin:0}.sub{margin:2px 0 0;color:var(--muted);font-size:.86rem}
.toolbar{display:flex;gap:8px}.toolbar select{width:auto;min-width:100px}
.layout{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(310px,.8fr);gap:14px;align-items:start}
.card{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:15px;box-shadow:var(--shadow)}
.card+.card{margin-top:14px}.head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:12px}
.title{font-weight:760}.muted{color:var(--muted);font-size:.84rem}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.field{display:flex;flex-direction:column;gap:6px}.field label{font-size:.88rem;font-weight:650}
input,select{width:100%;min-height:44px;padding:9px 11px;border:1px solid var(--border);border-radius:11px;background:var(--field);color:var(--text);outline:none}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--soft)}
.drop{border:1px dashed var(--border);border-radius:14px;padding:13px;background:var(--surface2)}
.docmeta{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.badge{border:1px solid var(--border);background:var(--surface2);border-radius:999px;padding:5px 8px;font-size:.77rem;color:var(--muted)}
.pagesegs{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.seg input{position:absolute;opacity:0}.seg label{display:grid;place-items:center;min-height:42px;border:1px solid var(--border);border-radius:10px;background:var(--surface2);font-size:.86rem;font-weight:700}
.seg input:checked+label{border-color:var(--accent);background:var(--soft);color:var(--accent)}
.valid{color:var(--ok)}.invalid{color:var(--danger)}.warning{color:var(--warn)}
.thumbstrip{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:9px;margin-top:12px}
.thumb{position:relative;border:2px solid transparent;border-radius:12px;background:var(--surface2);padding:5px;cursor:pointer}
.thumb.selected{border-color:var(--accent);background:var(--soft)}.thumb img{display:block;width:100%;aspect-ratio:3/4;object-fit:contain;background:white;border-radius:7px}
.thumb .pn{text-align:center;font-size:.78rem;padding:4px 0 1px}.thumb .tick{position:absolute;top:8px;right:8px;background:var(--accent);color:white;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:.75rem}
.previewbox{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:10px;display:grid;place-items:center;min-height:220px;overflow:auto}
.previewbox img{max-width:100%;max-height:70vh;box-shadow:0 4px 18px rgba(0,0,0,.15);background:white}
.previewnav{display:flex;gap:8px;align-items:center;margin-top:9px}.previewnav button{flex:1}.margins{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}
.more summary{cursor:pointer;font-weight:700;padding:5px 0}.tip{padding:10px;border-radius:11px;background:var(--surface2);color:var(--muted);font-size:.82rem}
.primary,.secondary,.danger{width:100%;min-height:44px;border-radius:11px;font-weight:750;cursor:pointer}
.primary{border:0;background:var(--accent);color:white}.secondary{border:1px solid var(--border);background:var(--field);color:var(--text)}.danger{border:1px solid color-mix(in srgb,var(--danger) 50%,var(--border));background:transparent;color:var(--danger)}
.job{border-top:1px solid var(--border);padding:10px 0}.job:first-child{border-top:0}.statusdot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;background:var(--ok)}
.summary{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;font-size:.88rem}.summary dt{color:var(--muted)}.summary dd{margin:0;font-weight:650}
.sticky{display:none}.hidden{display:none!important}
@media(max-width:880px){.layout{grid-template-columns:1fr}}
@media(max-width:620px){
  .shell{padding:10px 10px 100px}.card{padding:13px;border-radius:15px}.grid2{grid-template-columns:1fr}.margins{grid-template-columns:1fr 1fr}
  .pagesegs{grid-template-columns:1fr 1fr 1fr}.desktopPrint{display:none}.sticky{position:fixed;display:block;left:0;right:0;bottom:0;z-index:50;padding:9px 10px calc(9px + env(safe-area-inset-bottom));background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(14px);border-top:1px solid var(--border)}
}
@media(max-width:390px){.pagesegs{grid-template-columns:1fr}.toolbar select{min-width:84px}}
</style>
</head>
<body>
<div class="shell">
<header class="appbar">
  <div class="brand"><div class="logo">🖨️</div><div><h1>PhonePrint v3</h1><p class="sub">Preview, validate, then print</p></div></div>
  <div class="toolbar">
    <select id="presetSelect"><option value="">Presets</option></select>
    <select id="themeMode"><option value="auto">Auto</option><option value="light">Light</option><option value="dark">Dark</option></select>
  </div>
</header>

<form id="printForm">
<input type="hidden" name="session_id" id="sessionId">
<input type="hidden" name="odd_even" id="oddEven" value="all">
<input type="hidden" name="resolution_x" id="resolutionX">
<input type="hidden" name="resolution_y" id="resolutionY">

<div class="layout">
<main>
  <section class="card">
    <div class="head"><div class="title">1. Document</div><div id="docState" class="muted">No PDF loaded</div></div>
    <div class="drop">
      <div class="field"><label>PDF file</label><input id="pdfFile" type="file" accept="application/pdf,.pdf"></div>
    </div>
    <div id="docMeta" class="docmeta"></div>

    <div id="pageControls" class="hidden">
      <div style="height:14px"></div>
      <div class="title">Pages to print</div>
      <div style="height:8px"></div>
      <div class="pagesegs">
        <div class="seg"><input id="modeAll" type="radio" name="page_mode" value="all" checked><label for="modeAll">All</label></div>
        <div class="seg"><input id="modeOdd" type="radio" name="page_mode" value="odd"><label for="modeOdd">Odd</label></div>
        <div class="seg"><input id="modeEven" type="radio" name="page_mode" value="even"><label for="modeEven">Even</label></div>
      </div>
      <div style="height:10px"></div>
      <div class="field"><label>Custom range</label><input id="pageRange" name="page_range" value="all" placeholder="1-3,5,8-10"></div>
      <div id="rangeStatus" class="muted" style="margin-top:6px"></div>
      <div class="thumbstrip" id="thumbStrip"></div>
    </div>
  </section>

  <section id="previewCard" class="card hidden">
    <div class="head"><div class="title">2. Layout preview</div><div id="previewLabel" class="muted"></div></div>
    <div class="previewbox"><img id="previewImage" alt="Print preview"></div>
    <div class="previewnav">
      <button class="secondary" type="button" id="prevSheet">← Previous</button>
      <button class="secondary" type="button" id="nextSheet">Next →</button>
    </div>
    <div class="tip" style="margin-top:9px">This previews PhonePrint's page selection, margins, orientation, scaling and pages-per-sheet. Vendor-specific driver effects may not be visible.</div>
  </section>

  <section class="card">
    <div class="head"><div class="title">3. Print settings</div><div class="muted">Updates by printer</div></div>
    <div class="grid2">
      <div class="field"><label>Paper</label><select name="paper_id" id="paper"></select></div>
      <div class="field"><label>Copies</label><input type="number" name="copies" id="copies" value="1" min="1" max="1"></div>
      <div class="field"><label>Orientation</label><select name="orientation" id="orientation"><option value="portrait">Portrait</option><option value="landscape">Landscape</option></select></div>
      <div class="field"><label>Two-sided</label><select name="duplex" id="duplex"><option value="simplex">One-sided</option><option value="long">Long edge</option><option value="short">Short edge</option></select></div>
      <div class="field"><label>Color</label><select name="color" id="color"></select></div>
      <div class="field"><label>Pages per sheet</label><select name="nup" id="nup"></select></div>
    </div>

    <details class="more" style="margin-top:12px">
      <summary>More settings</summary>
      <div style="height:10px"></div>
      <div class="grid2">
        <div class="field"><label>Paper source</label><select name="source_id" id="source"></select></div>
        <div class="field"><label>Resolution / quality</label><select id="resolution"></select></div>
        <div class="field"><label>Collate</label><select name="collate" id="collate"><option value="off">Off</option><option value="on">On</option></select></div>
        <div class="field"><label>Scaling</label><select name="scaling" id="scaling"><option value="fit">Fit to printable area</option><option value="actual">Actual / no enlargement</option><option value="percent">Custom percentage</option></select></div>
        <div class="field"><label>Scale %</label><input type="number" name="scale_percent" id="scalePercent" value="100" min="10" max="100"></div>
      </div>
      <div style="height:10px"></div>
      <div class="field"><label>Margins (mm): top · right · bottom · left</label>
        <div class="margins">
          <input type="number" step=".5" name="margin_top" value="5" min="0" max="50">
          <input type="number" step=".5" name="margin_right" value="5" min="0" max="50">
          <input type="number" step=".5" name="margin_bottom" value="5" min="0" max="50">
          <input type="number" step=".5" name="margin_left" value="5" min="0" max="50">
        </div>
      </div>
    </details>
  </section>
</main>

<aside>
  <section class="card">
    <div class="head"><div class="title">Printer</div><div id="printerStatus" class="muted"></div></div>
    <div class="field"><label>Select printer</label><select name="printer" id="printerSelect">
    {% for p in printers %}
      <option value="{{p.name}}" {% if p.name == default_printer %}selected{% endif %}>{{p.name}}</option>
    {% endfor %}
    </select></div>
    <div class="field" style="margin-top:10px"><label>PIN</label><input name="pin" type="password" inputmode="numeric" autocomplete="off" required></div>
    <div id="capStatus" class="muted" style="margin-top:8px"></div>
    <div id="printerMeta" class="docmeta"></div>
  </section>

  <section class="card">
    <div class="head"><div class="title">Print summary</div><div id="summaryState" class="muted">Waiting for PDF</div></div>
    <dl class="summary" id="summary"></dl>
    <div style="height:10px"></div>
    <button class="secondary" type="button" id="savePresetBtn">Save current as preset</button>
    <div style="height:7px"></div>
    <button class="danger" type="button" id="deletePresetBtn">Delete selected preset</button>
  </section>

  <section class="card">
    <div class="head"><div class="title">Queue</div></div>
    <button class="secondary" type="button" id="refreshQueueBtn">Refresh queue</button>
    <div id="jobs"></div>
  </section>

  <section class="card"><div class="title">Status</div><div id="message" style="margin-top:8px;font-weight:650">Ready.</div></section>

  <div class="desktopPrint" style="margin-top:14px"><button id="desktopPrintBtn" class="primary" type="submit" disabled>Print document</button></div>
</aside>
</div>
</form>
</div>

<div class="sticky"><button id="mobilePrintBtn" class="primary" type="submit" form="printForm" disabled>Print document</button></div>

<script>
const form = document.getElementById("printForm");
const pdfFile = document.getElementById("pdfFile");
const sessionId = document.getElementById("sessionId");
const pageRange = document.getElementById("pageRange");
const oddEven = document.getElementById("oddEven");
const thumbStrip = document.getElementById("thumbStrip");
const pageControls = document.getElementById("pageControls");
const previewCard = document.getElementById("previewCard");
const previewImage = document.getElementById("previewImage");
const previewLabel = document.getElementById("previewLabel");
const rangeStatus = document.getElementById("rangeStatus");
const message = document.getElementById("message");
const printerSelect = document.getElementById("printerSelect");
const presetSelect = document.getElementById("presetSelect");
let docInfo = null;
let caps = null;
let validPages = [];
let selectedThumbPages = new Set();
let previewSheet = 0;
let previewTimer = null;

function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]))}
function addOption(sel,val,text,selected=false){const o=document.createElement("option");o.value=val;o.textContent=text;o.selected=selected;sel.appendChild(o)}
function clear(sel){sel.innerHTML=""}
function enablePrint(on){document.getElementById("desktopPrintBtn").disabled=!on;document.getElementById("mobilePrintBtn").disabled=!on}

// ---------- Theme ----------
const themeMode=document.getElementById("themeMode");
const mediaDark=matchMedia("(prefers-color-scheme: dark)");
function applyTheme(mode){
  localStorage.setItem("phoneprint-theme",mode);themeMode.value=mode;
  document.documentElement.dataset.theme=mode==="auto"?(mediaDark.matches?"dark":"light"):mode;
}
themeMode.addEventListener("change",()=>applyTheme(themeMode.value));
mediaDark.addEventListener?.("change",()=>{if(themeMode.value==="auto")applyTheme("auto")});
applyTheme(localStorage.getItem("phoneprint-theme")||"auto");

// ---------- PDF upload ----------
pdfFile.addEventListener("change", async ()=>{
  if(!pdfFile.files.length)return;
  const fd=new FormData();fd.append("file",pdfFile.files[0]);
  message.textContent="Inspecting PDF…";enablePrint(false);
  const r=await fetch("/api/upload",{method:"POST",body:fd});
  const data=await r.json();
  if(!r.ok){message.className="invalid";message.textContent=data.error||"Upload failed";return}
  docInfo=data;
  sessionId.value=data.session_id;
  document.getElementById("docState").textContent=`${data.page_count} pages`;
  document.getElementById("docMeta").innerHTML=[
    `<span class="badge">${esc(data.filename)}</span>`,
    `<span class="badge">${data.page_count} pages</span>`,
    `<span class="badge">${(data.size_bytes/1024/1024).toFixed(2)} MB</span>`,
    data.mixed_orientation?`<span class="badge">Mixed orientation</span>`:"",
    data.mixed_sizes?`<span class="badge">Mixed page sizes</span>`:""
  ].join("");
  pageControls.classList.remove("hidden");
  previewCard.classList.remove("hidden");
  pageRange.value="all";
  document.getElementById("modeAll").checked=true;
  oddEven.value="all";
  selectedThumbPages.clear();
  await validateRange();
  renderThumbs();
  message.className="";
  message.textContent="PDF ready.";
});

// ---------- page selection ----------
document.querySelectorAll('input[name="page_mode"]').forEach(r=>r.addEventListener("change",()=>{
  oddEven.value=r.value==="odd"?"odd":r.value==="even"?"even":"all";
  validateRange();
}));

pageRange.addEventListener("input",()=>validateRange());

async function validateRange(){
  if(!docInfo){enablePrint(false);return}
  const u=new URL("/api/validate",location.origin);
  u.searchParams.set("session_id",sessionId.value);
  u.searchParams.set("page_range",pageRange.value);
  u.searchParams.set("odd_even",oddEven.value);
  const r=await fetch(u);
  const data=await r.json();
  if(!r.ok||!data.valid){
    validPages=[];
    rangeStatus.className="invalid";
    rangeStatus.textContent=data.error||"Invalid page selection.";
    enablePrint(false);
    updateThumbSelection([]);
    updateSummary();
    return;
  }
  validPages=data.pages;
  rangeStatus.className="valid";
  rangeStatus.textContent=`${data.count} page${data.count===1?"":"s"} selected: ${data.display}`;
  enablePrint(data.count>0);
  updateThumbSelection(validPages);
  previewSheet=0;
  updateSummary();
  schedulePreview();
}

function pagesToRange(nums){
  if(!nums.length)return "";
  const sorted=[...nums].sort((a,b)=>a-b);
  const out=[];let start=sorted[0],prev=sorted[0];
  for(let i=1;i<=sorted.length;i++){
    const cur=sorted[i];
    if(cur===prev+1){prev=cur;continue}
    out.push(start===prev?String(start):`${start}-${prev}`);
    start=cur;prev=cur;
  }
  return out.join(",");
}

function updateThumbSelection(pages){
  selectedThumbPages=new Set(pages);
  document.querySelectorAll(".thumb").forEach(el=>{
    const p=Number(el.dataset.page);
    el.classList.toggle("selected",selectedThumbPages.has(p));
    const t=el.querySelector(".tick");
    if(t)t.style.display=selectedThumbPages.has(p)?"grid":"none";
  });
}

function renderThumbs(){
  thumbStrip.innerHTML="";
  const max=Math.min(docInfo.page_count,40);
  for(let p=1;p<=max;p++){
    const el=document.createElement("div");
    el.className="thumb";el.dataset.page=p;
    el.innerHTML=`<div class="tick">✓</div><img loading="lazy" src="/api/thumbnail/${sessionId.value}/${p}"><div class="pn">Page ${p}</div>`;
    el.addEventListener("click",()=>{
      document.getElementById("modeAll").checked=true;oddEven.value="all";
      if(selectedThumbPages.has(p))selectedThumbPages.delete(p);else selectedThumbPages.add(p);
      pageRange.value=selectedThumbPages.size===docInfo.page_count?"all":pagesToRange([...selectedThumbPages]);
      validateRange();
    });
    thumbStrip.appendChild(el);
  }
  if(docInfo.page_count>40){
    const note=document.createElement("div");note.className="muted";note.textContent="Showing first 40 thumbnails. Page-range input still supports the full document.";thumbStrip.appendChild(note);
  }
}

// ---------- capabilities ----------
async function loadCaps(){
  const u=new URL("/api/capabilities",location.origin);u.searchParams.set("printer",printerSelect.value);
  document.getElementById("capStatus").textContent="Reading capabilities…";
  const r=await fetch(u);const data=await r.json();
  if(!r.ok){document.getElementById("capStatus").className="invalid";document.getElementById("capStatus").textContent=data.error;return}
  caps=data;
  document.getElementById("capStatus").className="muted";
  document.getElementById("capStatus").textContent=`${data.papers.length} papers · ${data.bins.length} sources · ${data.resolutions.length} resolutions`;
  document.getElementById("printerStatus").textContent=data.printer.status_text;
  document.getElementById("printerMeta").innerHTML=`<span class="badge">${esc(data.printer.driver)}</span><span class="badge">${esc(data.printer.port)}</span>${data.printer.virtual?'<span class="badge">Virtual</span>':""}`;

  const paper=document.getElementById("paper");clear(paper);addOption(paper,"","Driver default");
  data.papers.forEach(x=>addOption(paper,x.id,x.size?`${x.name} · ${x.size.width_mm}×${x.size.height_mm} mm`:x.name,Number(data.defaults.paper_id)===x.id));

  const source=document.getElementById("source");clear(source);addOption(source,"","Driver default");
  data.bins.forEach(x=>addOption(source,x.id,x.name,Number(data.defaults.source_id)===x.id));

  const copies=document.getElementById("copies");copies.max=Math.max(1,data.max_copies);copies.value=Math.min(Math.max(1,Number(data.defaults.copies||1)),Number(copies.max));

  const duplex=document.getElementById("duplex");duplex.disabled=!data.duplex;
  if(!data.duplex)duplex.value="simplex";else if(Number(data.defaults.duplex)===2)duplex.value="long";else if(Number(data.defaults.duplex)===3)duplex.value="short";else duplex.value="simplex";

  const color=document.getElementById("color");clear(color);addOption(color,"auto","Driver default");
  if(data.color)addOption(color,"color","Color",Number(data.defaults.color)===2);
  addOption(color,"mono","Black & white",Number(data.defaults.color)===1);

  const collate=document.getElementById("collate");collate.disabled=!data.collate;collate.value=data.collate&&Number(data.defaults.collate)===1?"on":"off";

  const resolution=document.getElementById("resolution");clear(resolution);addOption(resolution,"","Driver default");
  data.resolutions.forEach(rr=>addOption(resolution,`${rr.x},${rr.y}`,rr.x===rr.y?`${rr.x} dpi`:`${rr.x}×${rr.y} dpi`,Number(data.defaults.quality)===rr.x&&Number(data.defaults.y_resolution)===rr.y));
  syncResolution();

  const nup=document.getElementById("nup");clear(nup);const nupOptions=data.software_nup||[1,2,4,6,9,16];nupOptions.forEach(n=>addOption(nup,String(n),n===1?"1 page":`${n} pages`,n===1));if(nup.selectedIndex<0)nup.value="1";
  restorePrinterSettings();
  updateSummary();
  schedulePreview();
}
printerSelect.addEventListener("change",()=>{loadCaps();refreshQueue()});

function syncResolution(){
  const v=document.getElementById("resolution").value;
  if(!v){document.getElementById("resolutionX").value="";document.getElementById("resolutionY").value="";}
  else{const [x,y]=v.split(",");document.getElementById("resolutionX").value=x;document.getElementById("resolutionY").value=y;}
}
document.getElementById("resolution").addEventListener("change",syncResolution);

// ---------- live preview ----------
function schedulePreview(){
  clearTimeout(previewTimer);
  previewTimer=setTimeout(loadPreview,220);
}
async function loadPreview(){
  if(!docInfo||!validPages.length||!caps)return;
  const params=new URLSearchParams(new FormData(form));
  params.set("sheet_index",previewSheet);
  const r=await fetch("/api/preview?"+params.toString());
  if(!r.ok)return;
  const totalSheets=Number(r.headers.get("X-Total-Sheets")||1);
  previewSheet=Math.min(previewSheet,totalSheets-1);
  previewImage.src=URL.createObjectURL(await r.blob());
  previewLabel.textContent=`Sheet ${previewSheet+1} of ${totalSheets}`;
  document.getElementById("prevSheet").disabled=previewSheet<=0;
  document.getElementById("nextSheet").disabled=previewSheet>=totalSheets-1;
}
document.getElementById("prevSheet").addEventListener("click",()=>{if(previewSheet>0){previewSheet--;loadPreview()}});
document.getElementById("nextSheet").addEventListener("click",()=>{previewSheet++;loadPreview()});

form.addEventListener("change",e=>{
  if(e.target.id==="printerSelect"||e.target.id==="pdfFile")return;
  savePrinterSettings();updateSummary();schedulePreview();
});
form.addEventListener("input",e=>{
  if(["pageRange","pdfFile"].includes(e.target.id))return;
  savePrinterSettings();updateSummary();schedulePreview();
});

// ---------- per-printer settings ----------
const rememberFields=["paper","copies","orientation","duplex","color","nup","source","resolution","collate","scaling","scalePercent"];
function printerKey(){return "phoneprint-printer-"+printerSelect.value}
function savePrinterSettings(){
  if(!caps)return;
  const obj={};
  rememberFields.forEach(id=>{const el=document.getElementById(id);if(el)obj[id]=el.value});
  ["margin_top","margin_right","margin_bottom","margin_left"].forEach(n=>obj[n]=form.elements[n].value);
  localStorage.setItem(printerKey(),JSON.stringify(obj));
}
function restorePrinterSettings(){
  try{
    const obj=JSON.parse(localStorage.getItem(printerKey())||"null");if(!obj)return;
    Object.entries(obj).forEach(([k,v])=>{
      const el=document.getElementById(k)||form.elements[k];if(!el)return;
      const options=el.options?[...el.options].map(o=>o.value):null;
      if(!options||options.includes(String(v)))el.value=v;
    });
    syncResolution();
  }catch{}
}

// ---------- presets ----------
function presets(){try{return JSON.parse(localStorage.getItem("phoneprint-presets")||"{}")}catch{return {}}}
function refreshPresets(){
  const p=presets();clear(presetSelect);addOption(presetSelect,"","Presets");
  Object.keys(p).sort().forEach(name=>addOption(presetSelect,name,name));
}
document.getElementById("savePresetBtn").addEventListener("click",()=>{
  const name=prompt("Preset name:");if(!name)return;
  const data={};
  rememberFields.forEach(id=>{const el=document.getElementById(id);if(el)data[id]=el.value});
  ["margin_top","margin_right","margin_bottom","margin_left"].forEach(n=>data[n]=form.elements[n].value);
  const p=presets();p[name]=data;localStorage.setItem("phoneprint-presets",JSON.stringify(p));refreshPresets();presetSelect.value=name;
});
presetSelect.addEventListener("change",()=>{
  const p=presets();const data=p[presetSelect.value];if(!data)return;
  Object.entries(data).forEach(([k,v])=>{const el=document.getElementById(k)||form.elements[k];if(!el)return;const opts=el.options?[...el.options].map(o=>o.value):null;if(!opts||opts.includes(String(v)))el.value=v});
  syncResolution();savePrinterSettings();updateSummary();schedulePreview();
});
document.getElementById("deletePresetBtn").addEventListener("click",()=>{
  const name=presetSelect.value;if(!name)return;
  const p=presets();delete p[name];localStorage.setItem("phoneprint-presets",JSON.stringify(p));refreshPresets();
});
refreshPresets();

// ---------- summary ----------
function updateSummary(){
  const box=document.getElementById("summary");
  if(!docInfo||!caps||!validPages.length){box.innerHTML="";document.getElementById("summaryState").textContent="Waiting for valid selection";return}
  const nup=Number(document.getElementById("nup").value||1),copies=Number(document.getElementById("copies").value||1);
  const sheets=Math.ceil(validPages.length/nup)*copies;
  const paper=document.getElementById("paper");const paperText=paper.options[paper.selectedIndex]?.text||"Driver default";
  const duplex=document.getElementById("duplex").value;
  const vals=[
    ["Printer",printerSelect.value],["Pages",`${validPages.length} selected`],["Sheets",String(sheets)],
    ["Copies",String(copies)],["Paper",paperText],["Sides",duplex==="simplex"?"One-sided":duplex==="long"?"Two-sided, long edge":"Two-sided, short edge"],
    ["Layout",`${nup} page${nup===1?"":"s"} per sheet`],["Color",document.getElementById("color").options[document.getElementById("color").selectedIndex]?.text||"Default"]
  ];
  box.innerHTML=vals.map(([a,b])=>`<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join("");
  document.getElementById("summaryState").textContent=`${sheets} output sheet${sheets===1?"":"s"}`;
}

// ---------- queue ----------
async function refreshQueue(){
  const pin=form.elements.pin.value;if(!pin){document.getElementById("jobs").innerHTML='<div class="muted" style="margin-top:8px">Enter PIN to view queue.</div>';return}
  const u=new URL("/api/jobs",location.origin);u.searchParams.set("printer",printerSelect.value);u.searchParams.set("pin",pin);
  const r=await fetch(u);const data=await r.json();if(!r.ok){document.getElementById("jobs").innerHTML=`<div class="invalid">${esc(data.error)}</div>`;return}
  let html="";
  data.local_jobs.forEach(j=>{html+=`<div class="job"><b>#${j.id}</b> ${esc(j.file)}<div class="muted">${esc(j.status)}</div>${j.error?`<div class="invalid">${esc(j.error)}</div>`:""}</div>`});
  data.windows_jobs.forEach(j=>{html+=`<div class="job"><b>Windows #${j.id}</b> ${esc(j.document)}<div class="muted">${esc(j.status||"Queued / printing")}</div><button class="danger" type="button" onclick="cancelJob(${j.id})">Cancel</button></div>`});
  document.getElementById("jobs").innerHTML=html||'<div class="muted" style="margin-top:8px">Queue is empty.</div>';
}
document.getElementById("refreshQueueBtn").addEventListener("click",refreshQueue);
async function cancelJob(id){
  if(!confirm(`Cancel Windows print job #${id}?`))return;
  const body=new URLSearchParams();body.set("pin",form.elements.pin.value);body.set("printer",printerSelect.value);
  const r=await fetch(`/api/cancel/${id}`,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body});
  const data=await r.json();if(!r.ok)alert(data.error||"Cancel failed");refreshQueue();
}

// ---------- print ----------
form.addEventListener("submit",async e=>{
  e.preventDefault();
  if(!docInfo||!validPages.length){message.className="invalid";message.textContent="Choose a PDF and a valid page selection first.";return}
  syncResolution();
  message.className="";message.textContent="Sending print job…";enablePrint(false);
  const r=await fetch("/print",{method:"POST",body:new FormData(form)});
  const data=await r.json();
  if(!r.ok){message.className="invalid";message.textContent=data.error||"Print failed";enablePrint(true);return}
  message.className="valid";message.textContent=`Accepted as PhonePrint job #${data.local_job_id}.`;
  enablePrint(true);refreshQueue();
});

loadCaps();
</script>
</body>
</html>
"""


# ============================================================
# Routes
# ============================================================

def require_pin(pin):
    if pin != PRINT_PIN:
        raise PermissionError("Wrong PIN.")


@app.get("/")
def index():
    return render_template_string(
        PAGE,
        printers=enum_printer_infos(),
        default_printer=default_printer_name(),
    )


@app.post("/api/upload")
def api_upload():
    cleanup_expired_sessions()

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Choose a PDF file."), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify(error="PhonePrint currently accepts PDF files only."), 400

    safe = secure_filename(f.filename) or "document.pdf"
    sid = secrets.token_urlsafe(18)
    path = UPLOAD_DIR / f"{sid}_{safe}"
    f.save(path)

    try:
        info = inspect_pdf(path)
        if info["page_count"] <= 0:
            path.unlink(missing_ok=True)
            return jsonify(error="This PDF contains no pages."), 400

        stat = path.stat()

        with session_lock:
            pdf_sessions[sid] = {
                "path": str(path),
                "filename": safe,
                "created": time.time(),
                "info": info,
            }

        return jsonify(
            session_id=sid,
            filename=safe,
            size_bytes=stat.st_size,
            **info,
        )
    except Exception as exc:
        path.unlink(missing_ok=True)
        return jsonify(error=f"Could not open PDF: {exc}"), 400


@app.get("/api/thumbnail/<session_id>/<int:page_no>")
def api_thumbnail(session_id, page_no):
    try:
        data = get_pdf_session(session_id)
        if page_no < 1 or page_no > data["info"]["page_count"]:
            return jsonify(error="Page does not exist."), 404
        png = render_thumbnail(data["path"], page_no - 1)
        return send_file(io.BytesIO(png), mimetype="image/png", max_age=3600)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/validate")
def api_validate():
    try:
        data = get_pdf_session(request.args.get("session_id", ""))
        page_count = data["info"]["page_count"]
        pages = validate_selection(
            request.args.get("page_range", "all"),
            request.args.get("odd_even", "all"),
            page_count,
        )
        display = pages_to_display(pages)
        return jsonify(valid=True, count=len(pages), pages=[p + 1 for p in pages], display=display)
    except Exception as exc:
        return jsonify(valid=False, error=str(exc)), 400


def pages_to_display(pages):
    nums = [p + 1 for p in pages]
    if not nums:
        return ""
    parts = []
    start = prev = nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ",".join(parts)


@app.get("/api/capabilities")
def api_capabilities():
    printer = request.args.get("printer", "")
    if printer not in installed_printer_names():
        return jsonify(error="Unknown or unavailable Windows printer."), 404
    try:
        return jsonify(get_capabilities(printer))
    except Exception as exc:
        return jsonify(error=f"Capability query failed: {exc}"), 500


@app.get("/api/preview")
def api_preview():
    try:
        data = get_pdf_session(request.args.get("session_id", ""))
        printer = request.args.get("printer", "")
        if printer not in installed_printer_names():
            return jsonify(error="Unknown printer."), 400

        caps = get_capabilities(printer)
        settings = parse_settings(request.args, caps)
        pages = validate_selection(settings["page_range"], settings["odd_even"], data["info"]["page_count"])

        sheet_index = int(request.args.get("sheet_index", 0))
        total_sheets = max(1, math.ceil(len(pages) / settings["nup"]))
        if sheet_index < 0:
            sheet_index = 0
        if sheet_index >= total_sheets:
            sheet_index = total_sheets - 1

        paper_mm = get_selected_paper_mm(caps, settings["paper_id"], settings["orientation"])
        img = preview_image(
            data["path"],
            pages,
            paper_mm,
            settings["nup"],
            settings["margins"],
            settings["scaling"],
            settings["scale_percent"],
            sheet_index,
        )

        response = send_file(img, mimetype="image/png", max_age=0)
        response.headers["X-Total-Sheets"] = str(total_sheets)
        response.headers["X-Sheet-Index"] = str(sheet_index)
        return response
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/print")
def submit_print():
    global job_counter

    try:
        require_pin(request.form.get("pin", ""))

        data = get_pdf_session(request.form.get("session_id", ""))
        printer = request.form.get("printer", "")
        if printer not in installed_printer_names():
            return jsonify(error="Selected printer is not installed."), 400

        caps = get_capabilities(printer)
        settings = parse_settings(request.form, caps)
        pages = validate_selection(settings["page_range"], settings["odd_even"], data["info"]["page_count"])

        with job_lock:
            job_counter += 1
            local_id = job_counter
            local_jobs[local_id] = {
                "id": local_id,
                "file": data["filename"],
                "printer": printer,
                "status": "Queued",
                "created": time.strftime("%H:%M:%S"),
            }

        t = threading.Thread(
            target=do_print,
            args=(data["path"], data["filename"], printer, settings, pages, local_id),
            daemon=True,
        )
        t.start()

        return jsonify(ok=True, local_job_id=local_id)

    except PermissionError as exc:
        return jsonify(error=str(exc)), 403
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/jobs")
def api_jobs():
    try:
        require_pin(request.args.get("pin", ""))
        printer = request.args.get("printer", "")
        if printer not in installed_printer_names():
            return jsonify(error="Unknown printer."), 400

        with job_lock:
            relevant = [j for j in local_jobs.values() if j.get("printer") == printer][-25:][::-1]

        return jsonify(local_jobs=relevant, windows_jobs=windows_queue(printer))
    except PermissionError as exc:
        return jsonify(error=str(exc)), 403
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.post("/api/cancel/<int:job_id>")
def api_cancel(job_id):
    try:
        require_pin(request.form.get("pin", ""))
        printer = request.form.get("printer", "")
        if printer not in installed_printer_names():
            return jsonify(error="Unknown printer."), 400
        cancel_windows_job(printer, job_id)
        return jsonify(ok=True)
    except PermissionError as exc:
        return jsonify(error=str(exc)), 403
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"File is larger than {MAX_UPLOAD_MB} MB."), 413


if __name__ == "__main__":
    print("=" * 72)
    print("PhonePrint v3")
    print(f"PIN: {PRINT_PIN}")
    print(f"Local:   http://127.0.0.1:{PORT}")
    print(f"Android: http://[IP]:{PORT}")
    print()
    print("Installed printers:")
    for p in enum_printer_infos():
        mark = "*" if p["name"] == default_printer_name() else " "
        print(f" {mark} {p['name']} | {p['status_text']} | {p['driver']} | {p['port']}")
    print("=" * 72)

    app.run(host=HOST, port=PORT, debug=False, threaded=True)

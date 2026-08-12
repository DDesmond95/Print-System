import ctypes
import os
import re
import secrets
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from werkzeug.utils import secure_filename

import pymupdf
from PIL import Image, ImageWin

import win32print
import win32ui


# ============================================================
# PhonePrint v2 - Windows dynamic printer capabilities
# ============================================================

HOST = "0.0.0.0"
PORT = 5000
MAX_UPLOAD_MB = 150

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

PRINT_PIN = os.environ.get("PRINT_PIN") or f"{secrets.randbelow(1_000_000):06d}"

job_lock = threading.Lock()
local_jobs = {}
job_counter = 0


# ============================================================
# Win32 constants
# ============================================================

# DeviceCapabilities()
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

# Values used by DEVMODE
DMORIENT_PORTRAIT = 1
DMORIENT_LANDSCAPE = 2

DMCOLOR_MONOCHROME = 1
DMCOLOR_COLOR = 2

DMDUP_SIMPLEX = 1
DMDUP_VERTICAL = 2       # long edge
DMDUP_HORIZONTAL = 3    # short edge

DMCOLLATE_FALSE = 0
DMCOLLATE_TRUE = 1

# GetDeviceCaps()
HORZRES = 8
VERTRES = 10
LOGPIXELSX = 88
LOGPIXELSY = 90
PHYSICALWIDTH = 110
PHYSICALHEIGHT = 111
PHYSICALOFFSETX = 112
PHYSICALOFFSETY = 113

# SetJob
JOB_CONTROL_CANCEL = 3


winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
DeviceCapabilitiesW = winspool.DeviceCapabilitiesW
DeviceCapabilitiesW.argtypes = [
    wintypes.LPCWSTR,  # pDevice
    wintypes.LPCWSTR,  # pPort
    wintypes.WORD,     # fwCapability
    ctypes.c_void_p,   # pOutput
    ctypes.c_void_p,   # pDevMode
]
DeviceCapabilitiesW.restype = ctypes.c_int


# ---- Native DEVMODE / printer DC functions ---------------------------------
# pywin32's PyCDC API differs across builds. For per-job printer settings we
# use the underlying Windows APIs directly: DocumentPropertiesW -> CreateDCW.

DM_OUT_BUFFER = 0x00000002
DM_IN_BUFFER = 0x00000008
IDOK = 1

class DEVMODEW_PUBLIC(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),

        # Printer branch of the DEVMODE anonymous union.
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

gdi32_native = ctypes.WinDLL("gdi32", use_last_error=True)

CreateDCW_native = gdi32_native.CreateDCW
CreateDCW_native.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
]
CreateDCW_native.restype = wintypes.HDC

GetDeviceCaps_native = gdi32_native.GetDeviceCaps
GetDeviceCaps_native.argtypes = [wintypes.HDC, ctypes.c_int]
GetDeviceCaps_native.restype = ctypes.c_int

StartPage_native = gdi32_native.StartPage
StartPage_native.argtypes = [wintypes.HDC]
StartPage_native.restype = ctypes.c_int

EndPage_native = gdi32_native.EndPage
EndPage_native.argtypes = [wintypes.HDC]
EndPage_native.restype = ctypes.c_int

EndDoc_native = gdi32_native.EndDoc
EndDoc_native.argtypes = [wintypes.HDC]
EndDoc_native.restype = ctypes.c_int

AbortDoc_native = gdi32_native.AbortDoc
AbortDoc_native.argtypes = [wintypes.HDC]
AbortDoc_native.restype = ctypes.c_int

DeleteDC_native = gdi32_native.DeleteDC
DeleteDC_native.argtypes = [wintypes.HDC]
DeleteDC_native.restype = wintypes.BOOL


class DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wintypes.LPCWSTR),
        ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR),
        ("fwType", wintypes.DWORD),
    ]


StartDocW_native = gdi32_native.StartDocW
StartDocW_native.argtypes = [wintypes.HDC, ctypes.POINTER(DOCINFOW)]
StartDocW_native.restype = ctypes.c_int


def _winerr(message):
    err = ctypes.get_last_error()
    if err:
        raise OSError(err, message)
    raise RuntimeError(message)


class NativePrinterDC:
    """Small compatibility wrapper around a native HDC."""

    def __init__(self, hdc, devmode_buffer):
        if not hdc:
            _winerr("CreateDCW failed.")
        self.hdc = hdc
        # Keep the DEVMODE memory alive for the DC lifetime.
        self._devmode_buffer = devmode_buffer

    def GetDeviceCaps(self, index):
        return GetDeviceCaps_native(self.hdc, index)

    def GetHandleOutput(self):
        return int(self.hdc)

    def StartDoc(self, title):
        di = DOCINFOW()
        di.cbSize = ctypes.sizeof(DOCINFOW)
        di.lpszDocName = str(title)
        di.lpszOutput = None
        di.lpszDatatype = None
        di.fwType = 0
        rc = StartDocW_native(self.hdc, ctypes.byref(di))
        if rc <= 0:
            _winerr("StartDocW failed.")
        return rc

    def StartPage(self):
        rc = StartPage_native(self.hdc)
        if rc <= 0:
            _winerr("StartPage failed.")
        return rc

    def EndPage(self):
        rc = EndPage_native(self.hdc)
        if rc <= 0:
            _winerr("EndPage failed.")
        return rc

    def EndDoc(self):
        rc = EndDoc_native(self.hdc)
        if rc <= 0:
            _winerr("EndDoc failed.")
        return rc

    def AbortDoc(self):
        if self.hdc:
            AbortDoc_native(self.hdc)

    def DeleteDC(self):
        if self.hdc:
            DeleteDC_native(self.hdc)
            self.hdc = None


# ============================================================
# Printer enumeration / capability discovery
# ============================================================

def enum_printer_infos():
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    entries = win32print.EnumPrinters(flags, None, 2)

    result = []
    for e in entries:
        name = e.get("pPrinterName")
        if not name:
            continue
        result.append({
            "name": name,
            "driver": e.get("pDriverName") or "",
            "port": e.get("pPortName") or "",
            "location": e.get("pLocation") or "",
            "comment": e.get("pComment") or "",
            "attributes": int(e.get("Attributes") or 0),
        })

    result.sort(key=lambda x: x["name"].lower())
    return result


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
        return {
            "name": printer_name,
            "driver": info.get("pDriverName") or "",
            "port": info.get("pPortName") or "",
            "location": info.get("pLocation") or "",
            "comment": info.get("pComment") or "",
            "attributes": int(info.get("Attributes") or 0),
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
    out = []
    for i in range(count):
        s = raw[i * width:(i + 1) * width].split("\x00", 1)[0].strip()
        out.append(s)
    return out


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
    name = info["name"].lower()
    port = info["port"].lower()
    driver = info["driver"].lower()

    hints = [
        "pdf", "xps", "onenote", "fax",
        "document writer", "adobe",
    ]
    return (
        any(h in name for h in hints)
        or any(h in driver for h in hints)
        or "portprompt:" in port
        or port == "file:"
    )


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
        },
        "defaults": current_devmode_values(dm),
        "papers": [],
        "bins": [],
        "resolutions": [],
        "duplex": False,
        "color": False,
        "collate": False,
        "max_copies": 1,
        "nup": [],
        "orientation_degrees": 0,
    }

    # Paper IDs / names / sizes.
    paper_ids = dc_array(device, port, DC_PAPERS, ctypes.c_ushort)
    paper_names = dc_fixed_strings(device, port, DC_PAPERNAMES, 64)

    point_count = dc_scalar(device, port, DC_PAPERSIZE)
    paper_points = []
    if point_count > 0:
        # POINT = two LONGs; values are tenths of a millimeter.
        arr = (wintypes.POINT * point_count)()
        rc = DeviceCapabilitiesW(device, port, DC_PAPERSIZE, ctypes.byref(arr), None)
        if rc >= 0:
            paper_points = list(arr)

    for i, pid in enumerate(paper_ids):
        name = paper_names[i] if i < len(paper_names) and paper_names[i] else f"Paper {pid}"
        size = None
        if i < len(paper_points):
            p = paper_points[i]
            size = {
                "width_mm": round(p.x / 10.0, 1),
                "height_mm": round(p.y / 10.0, 1),
            }
        caps["papers"].append({
            "id": int(pid),
            "name": name,
            "size": size,
        })

    # Trays / bins.
    bin_ids = dc_array(device, port, DC_BINS, ctypes.c_ushort)
    bin_names = dc_fixed_strings(device, port, DC_BINNAMES, 24)
    for i, bid in enumerate(bin_ids):
        name = bin_names[i] if i < len(bin_names) and bin_names[i] else f"Source {bid}"
        caps["bins"].append({"id": int(bid), "name": name})

    # Resolutions. Output is pairs of LONG (x,y).
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

    max_copies = dc_scalar(device, port, DC_COPIES)
    caps["max_copies"] = max(1, max_copies if max_copies > 0 else 1)

    orient = dc_scalar(device, port, DC_ORIENTATION)
    caps["orientation_degrees"] = max(0, orient)

    nup_count = dc_scalar(device, port, DC_NUP)
    if nup_count > 0:
        vals = dc_array(device, port, DC_NUP, wintypes.DWORD, nup_count)
        caps["nup"] = sorted({int(v) for v in vals if int(v) > 0})

    # Guarantee our software N-up choices are always available independently
    # from the driver's own N-up support.
    caps["software_nup"] = [1, 2, 4, 6, 9, 16]

    return caps


# ============================================================
# Per-job DEVMODE
# ============================================================

def build_native_devmode(printer_name, settings):
    """
    Ask the selected driver for its complete DEVMODE, modify standard public
    fields in-place, then give it back to DocumentPropertiesW for validation.

    The allocated buffer contains dmDriverExtra bytes as well, so vendor-private
    driver state is preserved.
    """
    h = win32print.OpenPrinter(printer_name)
    try:
        hraw = int(h)

        size = DocumentPropertiesW(
            None, hraw, printer_name, None, None, 0
        )
        if size <= 0:
            raise RuntimeError(
                f"DocumentPropertiesW could not determine DEVMODE size (rc={size})."
            )

        base = ctypes.create_string_buffer(size)

        rc = DocumentPropertiesW(
            None, hraw, printer_name,
            ctypes.byref(base), None,
            DM_OUT_BUFFER,
        )
        if rc != IDOK:
            raise RuntimeError(
                f"DocumentPropertiesW could not obtain printer defaults (rc={rc})."
            )

        dm = DEVMODEW_PUBLIC.from_buffer(base)

        # Safety check: the public DEVMODE supplied by the driver must be large
        # enough for the fields this program accesses.
        min_needed = DEVMODEW_PUBLIC.dmPanningHeight.offset + ctypes.sizeof(wintypes.DWORD)
        if int(dm.dmSize) < min_needed:
            # Older drivers can legally expose a shorter public DEVMODE.
            # All fields PhonePrint currently modifies are before dmFormName,
            # so require only through dmCollate.
            min_core = DEVMODEW_PUBLIC.dmCollate.offset + ctypes.sizeof(ctypes.c_short)
            if int(dm.dmSize) < min_core:
                raise RuntimeError(
                    f"Printer returned an unexpectedly short DEVMODE (dmSize={dm.dmSize})."
                )

        fields = int(dm.dmFields)

        orientation = settings["orientation"]
        if orientation == "portrait":
            dm.dmOrientation = DMORIENT_PORTRAIT
            fields |= DM_ORIENTATION
        elif orientation == "landscape":
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

        rx = settings["resolution_x"]
        ry = settings["resolution_y"]
        if rx is not None and ry is not None:
            dm.dmPrintQuality = int(rx)
            dm.dmYResolution = int(ry)
            fields |= DM_PRINTQUALITY | DM_YRESOLUTION

        dm.dmFields = fields

        # Let the driver validate / merge the edited public fields while
        # preserving the driver's private DEVMODE bytes.
        validated = ctypes.create_string_buffer(size)
        rc = DocumentPropertiesW(
            None, hraw, printer_name,
            ctypes.byref(validated),
            ctypes.byref(base),
            DM_IN_BUFFER | DM_OUT_BUFFER,
        )
        if rc != IDOK:
            raise RuntimeError(
                f"Printer driver rejected the requested settings (DocumentPropertiesW rc={rc})."
            )

        return validated
    finally:
        win32print.ClosePrinter(h)


def create_printer_dc(printer_name, settings):
    devmode_buffer = build_native_devmode(printer_name, settings)

    hdc = CreateDCW_native(
        "WINSPOOL",
        printer_name,
        None,
        ctypes.byref(devmode_buffer),
    )
    return NativePrinterDC(hdc, devmode_buffer)


# ============================================================
# PDF and layout
# ============================================================

def parse_page_range(text, page_count):
    text = (text or "all").strip().lower()

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
            raise ValueError(f"Invalid page item: {token}")

        start = int(m.group(1))
        end = int(m.group(2) or start)

        if not (1 <= start <= page_count and 1 <= end <= page_count):
            raise ValueError(f"Pages must be between 1 and {page_count}.")

        step = 1 if end >= start else -1
        for p in range(start, end + step, step):
            p0 = p - 1
            if p0 not in seen:
                result.append(p0)
                seen.add(p0)

    if not result:
        raise ValueError("No pages selected.")

    return result


def odd_even_filter(pages, mode):
    if mode == "odd":
        return [p for p in pages if (p + 1) % 2 == 1]
    if mode == "even":
        return [p for p in pages if (p + 1) % 2 == 0]
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


def mm_to_px(mm, dpi):
    return int(round(float(mm) / 25.4 * dpi))


def render_pdf_page(page, dpi=300, mono=False):
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if mono:
        # Keep 8-bit grayscale rather than 1-bit dithering. Printer driver can
        # perform its own halftoning.
        image = image.convert("L").convert("RGB")
    return image


def fitted_rect(cell, image_size, mode, percent):
    left, top, right, bottom = cell
    cw = max(1, right - left)
    ch = max(1, bottom - top)
    iw, ih = image_size

    fit = min(cw / iw, ch / ih)

    if mode == "actual":
        # "Actual size" is approximated from rendered PDF pixels at 300 dpi.
        # The caller prints onto a device measured in printer pixels, so use
        # percentage mode for exact user control.
        factor = min(1.0, fit)
    elif mode == "percent":
        factor = fit * (percent / 100.0)
    else:
        factor = fit

    # Prevent one N-up page from covering another cell.
    factor = min(factor, fit)

    ow = max(1, int(round(iw * factor)))
    oh = max(1, int(round(ih * factor)))
    x = left + (cw - ow) // 2
    y = top + (ch - oh) // 2
    return (x, y, x + ow, y + oh)


def number(form, name, default, lo, hi):
    raw = form.get(name, default)
    try:
        value = float(raw)
    except Exception:
        raise ValueError(f"{name} must be numeric.")
    if value < lo or value > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}.")
    return value


def optional_int(form, name):
    raw = (form.get(name) or "").strip().lower()
    if raw in ("", "default", "none"):
        return None
    return int(raw)


def parse_settings(form, caps):
    printer_name = form.get("printer", "")
    if printer_name != caps["printer"]["name"]:
        raise ValueError("Printer changed while loading settings. Select it again.")

    copies = int(form.get("copies", 1))
    if not 1 <= copies <= max(1, caps["max_copies"]):
        raise ValueError(f"Copies must be 1-{caps['max_copies']}.")

    nup = int(form.get("nup", 1))
    if nup not in {1, 2, 4, 6, 9, 16}:
        raise ValueError("Unsupported pages-per-sheet value.")

    orientation = form.get("orientation", "portrait")
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("Invalid orientation.")

    odd_even = form.get("odd_even", "all")
    if odd_even not in {"all", "odd", "even"}:
        raise ValueError("Invalid odd/even value.")

    duplex = form.get("duplex", "simplex")
    if duplex not in {"simplex", "long", "short"}:
        raise ValueError("Invalid duplex value.")
    if not caps["duplex"] and duplex != "simplex":
        raise ValueError("Selected printer does not report duplex support.")

    color = form.get("color", "auto")
    if color not in {"auto", "mono", "color"}:
        raise ValueError("Invalid color value.")
    if color == "color" and not caps["color"]:
        raise ValueError("Selected printer does not report color capability.")

    collate = form.get("collate", "off")
    if collate not in {"off", "on"}:
        raise ValueError("Invalid collate value.")
    if collate == "on" and not caps["collate"]:
        raise ValueError("Selected printer does not report collate support.")

    scaling = form.get("scaling", "fit")
    if scaling not in {"fit", "percent", "actual"}:
        raise ValueError("Invalid scaling option.")

    paper_id = optional_int(form, "paper_id")
    valid_papers = {p["id"] for p in caps["papers"]}
    if paper_id is not None and paper_id not in valid_papers:
        raise ValueError("Selected paper is no longer supported by this printer.")

    source_id = optional_int(form, "source_id")
    valid_bins = {b["id"] for b in caps["bins"]}
    if source_id is not None and source_id not in valid_bins:
        raise ValueError("Selected tray/source is no longer supported.")

    rx = optional_int(form, "resolution_x")
    ry = optional_int(form, "resolution_y")
    if (rx is None) != (ry is None):
        raise ValueError("Resolution X and Y must be selected together.")
    if rx is not None:
        valid_res = {(r["x"], r["y"]) for r in caps["resolutions"]}
        if (rx, ry) not in valid_res:
            raise ValueError("Selected resolution is not reported by this printer.")

    return {
        "page_range": form.get("page_range", "all"),
        "odd_even": odd_even,
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
        "scale_percent": number(form, "scale_percent", 100, 10, 100),
        "margins": {
            "top": number(form, "margin_top", 5, 0, 50),
            "right": number(form, "margin_right", 5, 0, 50),
            "bottom": number(form, "margin_bottom", 5, 0, 50),
            "left": number(form, "margin_left", 5, 0, 50),
        },
    }


# ============================================================
# Printing
# ============================================================

def do_print(pdf_path, original_name, printer_name, settings, local_id):
    doc = None
    dc = None

    try:
        with job_lock:
            local_jobs[local_id]["status"] = "Opening PDF"

        doc = pymupdf.open(pdf_path)
        pages = parse_page_range(settings["page_range"], len(doc))
        pages = odd_even_filter(pages, settings["odd_even"])

        if not pages:
            raise ValueError("No pages remain after odd/even filtering.")

        dc = create_printer_dc(printer_name, settings)

        dpi_x = dc.GetDeviceCaps(LOGPIXELSX)
        dpi_y = dc.GetDeviceCaps(LOGPIXELSY)
        printable_w = dc.GetDeviceCaps(HORZRES)
        printable_h = dc.GetDeviceCaps(VERTRES)

        margins = settings["margins"]
        left = mm_to_px(margins["left"], dpi_x)
        top = mm_to_px(margins["top"], dpi_y)
        right = printable_w - mm_to_px(margins["right"], dpi_x)
        bottom = printable_h - mm_to_px(margins["bottom"], dpi_y)

        left = min(max(0, left), printable_w - 1)
        top = min(max(0, top), printable_h - 1)
        right = max(left + 1, min(printable_w, right))
        bottom = max(top + 1, min(printable_h, bottom))

        cols, rows = nup_grid(settings["nup"])
        gap_x = mm_to_px(2, dpi_x) if cols > 1 else 0
        gap_y = mm_to_px(2, dpi_y) if rows > 1 else 0

        uw = right - left
        uh = bottom - top
        cell_w = max(1, (uw - gap_x * (cols - 1)) // cols)
        cell_h = max(1, (uh - gap_y * (rows - 1)) // rows)

        with job_lock:
            local_jobs[local_id]["status"] = "Sending to Windows spooler"
            local_jobs[local_id]["pages"] = [p + 1 for p in pages]

        dc.StartDoc(f"PhonePrint - {original_name}")

        cursor = 0
        while cursor < len(pages):
            dc.StartPage()

            for slot in range(settings["nup"]):
                if cursor >= len(pages):
                    break

                pno = pages[cursor]
                page = doc[pno]

                mono = settings["color"] == "mono"
                image = render_pdf_page(page, dpi=300, mono=mono)

                col = slot % cols
                row = slot // cols
                cx = left + col * (cell_w + gap_x)
                cy = top + row * (cell_h + gap_y)
                cell = (cx, cy, cx + cell_w, cy + cell_h)

                dest = fitted_rect(
                    cell,
                    image.size,
                    settings["scaling"],
                    settings["scale_percent"],
                )

                ImageWin.Dib(image).draw(dc.GetHandleOutput(), dest)

                cursor += 1
                with job_lock:
                    local_jobs[local_id]["status"] = f"Rendering {cursor}/{len(pages)}"

            dc.EndPage()

        dc.EndDoc()

        with job_lock:
            local_jobs[local_id]["status"] = "Sent"
            local_jobs[local_id]["finished"] = time.strftime("%H:%M:%S")

    except Exception as exc:
        if dc is not None:
            try:
                dc.AbortDoc()
            except Exception:
                pass

        with job_lock:
            local_jobs[local_id]["status"] = "Error"
            local_jobs[local_id]["error"] = str(exc)
            local_jobs[local_id]["traceback"] = traceback.format_exc()
    finally:
        try:
            if dc is not None:
                dc.DeleteDC()
        except Exception:
            pass
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass
        try:
            Path(pdf_path).unlink(missing_ok=True)
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
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>PhonePrint</title>

<style>
:root {
  --bg: #f4f5f7;
  --card: #ffffff;
  --text: #17181a;
  --muted: #686d76;
  --border: #d9dce1;
  --field: #ffffff;
  --accent: #1769e0;
  --accentText: #ffffff;
  --danger: #b42318;
  --ok: #067647;
  --shadow: 0 8px 30px rgba(0,0,0,.07);
}

html[data-resolved-theme="dark"] {
  --bg: #101214;
  --card: #181b1f;
  --text: #f2f4f7;
  --muted: #a5abb4;
  --border: #30343a;
  --field: #111316;
  --accent: #7eb0ff;
  --accentText: #07101f;
  --danger: #ff8b83;
  --ok: #75e0a7;
  --shadow: 0 8px 30px rgba(0,0,0,.25);
}

* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
}
.wrap { max-width:820px; margin:auto; padding:18px; }
.topbar {
  display:flex; gap:12px; align-items:center; justify-content:space-between;
  margin-bottom:14px;
}
h1 { margin:0; font-size:1.7rem; }
.subtitle { color:var(--muted); margin:.2rem 0 0; }
.card {
  background:var(--card); border:1px solid var(--border);
  border-radius:18px; padding:16px; margin:14px 0; box-shadow:var(--shadow);
}
.grid { display:grid; grid-template-columns:1fr 1fr; gap:13px; }
.grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
.grid4 { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
@media(max-width:620px) {
  .grid,.grid3 { grid-template-columns:1fr; }
  .grid4 { grid-template-columns:1fr 1fr; }
}
label { font-weight:650; display:block; margin:0 0 6px; }
input,select,button {
  width:100%; border:1px solid var(--border); border-radius:11px;
  background:var(--field); color:var(--text); padding:11px;
  font:inherit;
}
button { cursor:pointer; font-weight:700; }
button.primary {
  background:var(--accent); color:var(--accentText); border-color:transparent;
  padding:14px; margin-top:12px;
}
button.smallbtn { padding:8px 10px; margin-top:8px; }
.muted { color:var(--muted); font-size:.92rem; }
.ok { color:var(--ok); }
.bad { color:var(--danger); }
.section-title { font-size:1.05rem; margin:3px 0 12px; }
hr { border:0; border-top:1px solid var(--border); margin:16px 0; }
.badge {
  display:inline-block; border:1px solid var(--border); border-radius:999px;
  padding:4px 8px; margin:2px 5px 2px 0; font-size:.82rem; color:var(--muted);
}
.job { border-top:1px solid var(--border); padding:11px 0; }
.job:first-child { border-top:0; }
#capStatus { min-height:1.4em; }
.themebox { width:auto; min-width:115px; }
.hidden { display:none !important; }
</style>
</head>

<body>
<div class="wrap">

  <div class="topbar">
    <div>
      <h1>PhonePrint</h1>
      <p class="subtitle">Android → Windows print spooler</p>
    </div>
    <select id="themeMode" class="themebox" aria-label="Theme">
      <option value="auto">Auto</option>
      <option value="light">Light</option>
      <option value="dark">Dark</option>
    </select>
  </div>

  <form id="printForm" class="card" enctype="multipart/form-data">
    <div class="section-title"><b>Document</b></div>

    <label>PDF file</label>
    <input type="file" name="file" accept="application/pdf,.pdf" required>

    <div style="height:13px"></div>

    <div class="grid">
      <div>
        <label>Printer</label>
        <select name="printer" id="printerSelect">
        {% for p in printers %}
          <option value="{{ p.name }}" {% if p.name == default_printer %}selected{% endif %}>
            {{ p.name }}
          </option>
        {% endfor %}
        </select>
      </div>
      <div>
        <label>PIN</label>
        <input name="pin" type="password" inputmode="numeric" autocomplete="off" required>
      </div>
    </div>

    <p id="capStatus" class="muted">Reading printer capabilities…</p>
    <div id="printerMeta"></div>

    <hr>

    <div class="section-title"><b>Pages & layout</b></div>
    <div class="grid">
      <div>
        <label>Pages</label>
        <input name="page_range" value="all" placeholder="all or 1-3,5,8-10">
      </div>
      <div>
        <label>Odd / even</label>
        <select name="odd_even">
          <option value="all">All selected pages</option>
          <option value="odd">Odd only</option>
          <option value="even">Even only</option>
        </select>
      </div>
      <div>
        <label>Orientation</label>
        <select name="orientation" id="orientation">
          <option value="portrait">Portrait</option>
          <option value="landscape">Landscape</option>
        </select>
      </div>
      <div>
        <label>Pages per sheet</label>
        <select name="nup" id="nup"></select>
      </div>
      <div>
        <label>Scaling</label>
        <select name="scaling">
          <option value="fit">Fit to printable cell</option>
          <option value="actual">Actual / no enlargement</option>
          <option value="percent">Custom % of fitted size</option>
        </select>
      </div>
      <div>
        <label>Scale %</label>
        <input type="number" name="scale_percent" value="100" min="10" max="100">
      </div>
    </div>

    <div style="height:13px"></div>
    <label>Margins (mm): top · right · bottom · left</label>
    <div class="grid4">
      <input type="number" step=".5" name="margin_top" value="5" min="0" max="50">
      <input type="number" step=".5" name="margin_right" value="5" min="0" max="50">
      <input type="number" step=".5" name="margin_bottom" value="5" min="0" max="50">
      <input type="number" step=".5" name="margin_left" value="5" min="0" max="50">
    </div>

    <hr>

    <div class="section-title"><b>Selected printer settings</b></div>
    <div class="grid">
      <div>
        <label>Paper</label>
        <select name="paper_id" id="paper"></select>
      </div>
      <div>
        <label>Paper source / tray</label>
        <select name="source_id" id="source"></select>
      </div>
      <div>
        <label>Copies</label>
        <input type="number" name="copies" id="copies" value="1" min="1" max="1">
      </div>
      <div id="duplexBox">
        <label>Two-sided</label>
        <select name="duplex" id="duplex">
          <option value="simplex">One-sided</option>
          <option value="long">Two-sided · long edge</option>
          <option value="short">Two-sided · short edge</option>
        </select>
      </div>
      <div id="colorBox">
        <label>Color</label>
        <select name="color" id="color">
          <option value="auto">Driver default</option>
          <option value="color">Color</option>
          <option value="mono">Black & white</option>
        </select>
      </div>
      <div id="collateBox">
        <label>Collate</label>
        <select name="collate" id="collate">
          <option value="off">Off</option>
          <option value="on">On</option>
        </select>
      </div>
      <div>
        <label>Resolution / quality</label>
        <select id="resolution"></select>
        <input type="hidden" name="resolution_x" id="resolutionX">
        <input type="hidden" name="resolution_y" id="resolutionY">
      </div>
    </div>

    <p class="muted">
      Options above are rebuilt whenever you choose another printer. Driver-specific/private
      options that Windows does not publish as standard capabilities are preserved in the
      driver's DEVMODE but cannot be converted automatically into generic web controls.
    </p>

    <button class="primary" type="submit">PRINT</button>
  </form>

  <div id="message" class="card">Ready.</div>

  <div class="card">
    <div class="section-title"><b>Queue</b></div>
    <button type="button" onclick="refreshJobs()">Refresh queue</button>
    <div id="jobs"></div>
  </div>

</div>

<script>
const form = document.getElementById("printForm");
const printerSelect = document.getElementById("printerSelect");
const capStatus = document.getElementById("capStatus");
const printerMeta = document.getElementById("printerMeta");
const message = document.getElementById("message");
let caps = null;

// ---------- Theme ----------
const themeMode = document.getElementById("themeMode");
const mediaDark = matchMedia("(prefers-color-scheme: dark)");

function applyTheme(mode) {
  localStorage.setItem("phoneprint-theme", mode);
  themeMode.value = mode;
  const resolved = mode === "auto" ? (mediaDark.matches ? "dark" : "light") : mode;
  document.documentElement.dataset.resolvedTheme = resolved;
}

themeMode.addEventListener("change", () => applyTheme(themeMode.value));
mediaDark.addEventListener?.("change", () => {
  if (themeMode.value === "auto") applyTheme("auto");
});
applyTheme(localStorage.getItem("phoneprint-theme") || "auto");

// ---------- Printer capabilities ----------
function addOption(select, value, text, selected=false) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = text;
  o.selected = selected;
  select.appendChild(o);
}

function clear(select) { select.innerHTML = ""; }

function defaultMatches(field, value) {
  return Number(caps?.defaults?.[field]) === Number(value);
}

async function loadCapabilities() {
  const printer = printerSelect.value;
  capStatus.textContent = "Reading printer capabilities…";
  printerMeta.innerHTML = "";
  form.querySelector("button.primary").disabled = true;

  try {
    const u = new URL("/api/capabilities", location.origin);
    u.searchParams.set("printer", printer);
    const r = await fetch(u);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Capability query failed.");
    caps = data;

    // Metadata
    const p = data.printer;
    let meta = `<span class="badge">${esc(p.driver || "Unknown driver")}</span>`;
    meta += `<span class="badge">Port: ${esc(p.port || "unknown")}</span>`;
    if (p.virtual) meta += `<span class="badge">Virtual / file printer</span>`;
    printerMeta.innerHTML = meta;

    // Paper
    const paper = document.getElementById("paper");
    clear(paper);
    addOption(paper, "", "Driver default", !data.defaults.paper_id);
    for (const x of data.papers) {
      let label = x.name;
      if (x.size) label += ` — ${x.size.width_mm} × ${x.size.height_mm} mm`;
      addOption(paper, x.id, label, defaultMatches("paper_id", x.id));
    }

    // Sources / trays
    const source = document.getElementById("source");
    clear(source);
    addOption(source, "", "Driver default", !data.defaults.source_id);
    for (const x of data.bins) {
      addOption(source, x.id, x.name, defaultMatches("source_id", x.id));
    }

    // Copies
    const copies = document.getElementById("copies");
    copies.max = Math.max(1, data.max_copies);
    copies.value = Math.min(
      Math.max(1, Number(data.defaults.copies || 1)),
      Math.max(1, data.max_copies)
    );

    // Duplex
    const duplex = document.getElementById("duplex");
    const duplexBox = document.getElementById("duplexBox");
    duplex.disabled = !data.duplex;
    duplexBox.style.opacity = data.duplex ? "1" : ".45";
    if (!data.duplex) duplex.value = "simplex";
    else if (Number(data.defaults.duplex) === 2) duplex.value = "long";
    else if (Number(data.defaults.duplex) === 3) duplex.value = "short";
    else duplex.value = "simplex";

    // Color
    const color = document.getElementById("color");
    const colorBox = document.getElementById("colorBox");
    clear(color);
    addOption(color, "auto", "Driver default");
    if (data.color) addOption(color, "color", "Color", Number(data.defaults.color) === 2);
    addOption(color, "mono", "Black & white", Number(data.defaults.color) === 1);
    colorBox.style.opacity = data.color ? "1" : ".75";

    // Collate
    const collate = document.getElementById("collate");
    const collateBox = document.getElementById("collateBox");
    collate.disabled = !data.collate;
    collateBox.style.opacity = data.collate ? "1" : ".45";
    collate.value = data.collate && Number(data.defaults.collate) === 1 ? "on" : "off";

    // Resolution
    const resolution = document.getElementById("resolution");
    clear(resolution);
    addOption(resolution, "", "Driver default");
    for (const rr of data.resolutions) {
      const v = `${rr.x},${rr.y}`;
      const selected =
        Number(data.defaults.quality) === rr.x &&
        Number(data.defaults.y_resolution) === rr.y;
      addOption(resolution, v, rr.x === rr.y ? `${rr.x} dpi` : `${rr.x} × ${rr.y} dpi`, selected);
    }
    syncResolution();

    // N-up (software-side; available even if driver doesn't expose N-up)
    const nup = document.getElementById("nup");
    clear(nup);
    for (const n of data.software_nup) {
      addOption(nup, n, String(n), n === 1);
    }

    capStatus.textContent =
      `${data.papers.length} paper sizes · ${data.bins.length} sources · ` +
      `${data.resolutions.length} resolutions · max ${data.max_copies} copies`;

    if (p.virtual) {
      capStatus.textContent +=
        " · Note: file/virtual printers may open a Save As dialog on the laptop.";
    }

    form.querySelector("button.primary").disabled = false;
  } catch (err) {
    capStatus.textContent = "Could not read this printer: " + err.message;
    capStatus.className = "bad";
  }
}

function syncResolution() {
  const v = document.getElementById("resolution").value;
  const x = document.getElementById("resolutionX");
  const y = document.getElementById("resolutionY");

  if (!v) {
    x.value = "";
    y.value = "";
  } else {
    const [rx, ry] = v.split(",");
    x.value = rx;
    y.value = ry;
  }
}

printerSelect.addEventListener("change", loadCapabilities);
document.getElementById("resolution").addEventListener("change", syncResolution);

// ---------- Printing ----------
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  syncResolution();

  message.className = "card";
  message.textContent = "Uploading and preparing print job…";

  try {
    const r = await fetch("/print", {
      method: "POST",
      body: new FormData(form)
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Print failed.");
    message.classList.add("ok");
    message.textContent = `Accepted as PhonePrint job #${data.local_job_id}.`;
    refreshJobs();
  } catch (err) {
    message.classList.add("bad");
    message.textContent = err.message;
  }
});

// ---------- Queue ----------
async function refreshJobs() {
  const pin = form.elements.pin.value;
  if (!pin) {
    document.getElementById("jobs").textContent = "Enter the PIN first.";
    return;
  }

  const u = new URL("/api/jobs", location.origin);
  u.searchParams.set("printer", printerSelect.value);
  u.searchParams.set("pin", pin);

  try {
    const r = await fetch(u);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Queue read failed.");

    let html = "<h3>PhonePrint jobs</h3>";
    if (!data.local_jobs.length) html += '<div class="muted">None yet.</div>';

    for (const j of data.local_jobs) {
      html += `<div class="job"><b>#${j.id}</b> ${esc(j.file)}<br>${esc(j.status)}`;
      if (j.error) html += `<br><span class="bad">${esc(j.error)}</span>`;
      html += "</div>";
    }

    html += "<h3>Windows spooler</h3>";
    if (!data.windows_jobs.length) html += '<div class="muted">Queue is empty.</div>';

    for (const j of data.windows_jobs) {
      html += `<div class="job"><b>#${j.id}</b> ${esc(j.document)}<br>${esc(j.status || "Queued / printing")}`;
      html += `<br><button class="smallbtn" type="button" onclick="cancelJob(${j.id})">Cancel</button></div>`;
    }

    document.getElementById("jobs").innerHTML = html;
  } catch (err) {
    document.getElementById("jobs").innerHTML = `<span class="bad">${esc(err.message)}</span>`;
  }
}

async function cancelJob(id) {
  if (!confirm(`Cancel Windows print job #${id}?`)) return;

  const body = new URLSearchParams();
  body.set("pin", form.elements.pin.value);
  body.set("printer", printerSelect.value);

  const r = await fetch(`/api/cancel/${id}`, {
    method:"POST",
    headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body
  });

  const data = await r.json();
  if (!r.ok) alert(data.error || "Cancel failed.");
  refreshJobs();
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;"
  }[c]));
}

loadCapabilities();
</script>
</body>
</html>
"""


# ============================================================
# HTTP routes
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


@app.get("/api/printers")
def api_printers():
    return jsonify(
        printers=enum_printer_infos(),
        default=default_printer_name(),
    )


@app.get("/api/capabilities")
def api_capabilities():
    printer = request.args.get("printer", "")
    if printer not in installed_printer_names():
        return jsonify(error="Unknown or unavailable Windows printer."), 404

    try:
        return jsonify(get_capabilities(printer))
    except Exception as exc:
        return jsonify(error=f"Capability query failed: {exc}"), 500


@app.post("/print")
def submit_print():
    global job_counter

    try:
        require_pin(request.form.get("pin", ""))

        printer = request.form.get("printer", "")
        if printer not in installed_printer_names():
            return jsonify(error="Selected printer is not currently installed."), 400

        caps = get_capabilities(printer)
        settings = parse_settings(request.form, caps)

        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify(error="Choose a PDF file."), 400
        if not f.filename.lower().endswith(".pdf"):
            return jsonify(error="PhonePrint currently accepts PDF files only."), 400

        safe = secure_filename(f.filename) or "document.pdf"
        path = UPLOAD_DIR / f"{secrets.token_hex(8)}_{safe}"
        f.save(path)

        try:
            probe = pymupdf.open(path)
            if not probe.is_pdf:
                raise ValueError("Not a PDF.")
            probe.close()
        except Exception as exc:
            path.unlink(missing_ok=True)
            return jsonify(error=f"Invalid PDF: {exc}"), 400

        with job_lock:
            job_counter += 1
            local_id = job_counter
            local_jobs[local_id] = {
                "id": local_id,
                "file": safe,
                "printer": printer,
                "status": "Queued",
                "created": time.strftime("%H:%M:%S"),
            }

        thread = threading.Thread(
            target=do_print,
            args=(str(path), safe, printer, settings, local_id),
            daemon=True,
        )
        thread.start()

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
            relevant = [
                j for j in local_jobs.values()
                if j.get("printer") == printer
            ][-20:][::-1]

        return jsonify(
            local_jobs=relevant,
            windows_jobs=windows_queue(printer),
        )
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
    printers = enum_printer_infos()

    print("=" * 68)
    print("PhonePrint v2")
    print(f"PIN: {PRINT_PIN}")
    print(f"Android URL: http://10.17.13.77:{PORT}")
    print()
    print("Installed Windows printers:")
    for p in printers:
        marker = "*" if p["name"] == default_printer_name() else " "
        print(f" {marker} {p['name']}  [{p['driver']}]  port={p['port']}")
    print("=" * 68)

    app.run(host=HOST, port=PORT, debug=False, threaded=True)

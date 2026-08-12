PhonePrint v2 - Windows Dynamic Printer Edition
==================================================

What changed
------------
- No printer model is hard-coded.
- Lists every printer installed in Windows:
  physical printers, network printers, USB printers, and virtual printers.
- Selecting another printer in the Android browser immediately reloads its
  standard Windows capabilities.
- Dynamically reads:
  * paper sizes
  * paper dimensions
  * paper trays / sources
  * duplex support
  * color capability
  * maximum copies
  * collation support
  * supported resolutions
  * Windows DEVMODE defaults
- Auto / Light / Dark UI.
- Keeps PhonePrint's own PDF page controls:
  page ranges, odd/even, margins, scaling and N-up.
- Windows spooler queue/status/cancel support.

Install
-------
Open Anaconda Prompt:

  cd C:\PhonePrint
  conda create -n phoneprint python=3.12 -y
  conda activate phoneprint
  pip install -r requirements.txt
  python server.py

Then on Android:

  http://[IP]:5000

Security
--------
A random 6-digit PIN is printed in the laptop console each time PhonePrint
starts. You can fix the PIN yourself by setting an environment variable:

  set PRINT_PIN=123456
  python server.py

Do not expose port 5000 to the public Internet. This is intended for a trusted
local LAN/Wi-Fi.

Important Windows limitation
----------------------------
Windows printer drivers have two kinds of settings:

1. Standard/public capabilities and DEVMODE settings.
   PhonePrint can read and expose these dynamically.

2. Vendor-specific/private driver settings.
   Examples can include secure-print PINs, toner-save modes, booklet finishing,
   stapling rules, special Canon/HP/Fuji vendor options, and so on.

Those private settings are opaque bytes owned by the printer driver. There is
no universal Windows API that converts every manufacturer's private settings
into generic field names/options for a web page. PhonePrint therefore starts
from the driver's existing DEVMODE so those private bytes are preserved, but
it cannot automatically turn all private settings into HTML controls.

Virtual printers
----------------
Adobe PDF, Microsoft Print to PDF, XPS and similar printers are listed just like
any other installed Windows printer and their capabilities are queried.

However, many virtual/file printers require a Save As filename dialog. That
dialog appears on the Windows laptop because the printer driver owns it. A
browser on Android cannot automatically answer arbitrary vendor UI prompts.

Troubleshooting
---------------
If the laptop can open:
  http://127.0.0.1:5000

but Android cannot open:
  http://[IP]:5000

then the problem is network reachability / Wi-Fi client isolation / Windows
Firewall rather than PhonePrint.


v2.1 fix
--------
- Fixed DeviceCapabilitiesW DLL binding:
  it is loaded from winspool.drv, not gdi32.dll.
- Updated PyMuPDF import from deprecated `fitz` to `pymupdf`.


v2.2 native printer-DC fix
--------------------------
Fixes:
  'PyCDC' object has no attribute 'CreateDC'

PhonePrint no longer uses the version-dependent PyCDC.CreateDC call for
printing. It now uses:
  DocumentPropertiesW -> full driver DEVMODE buffer
  CreateDCW            -> native printer device context

This has two advantages:
- works independently of pywin32 PyCDC method differences;
- keeps the printer driver's private dmDriverExtra data in the DEVMODE buffer.

If a particular driver rejects a setting, PhonePrint reports the
DocumentPropertiesW validation error instead of silently applying a bad
DEVMODE.


v2.3 UI redesign
----------------
- Mobile-first responsive layout.
- Cleaner desktop two-column layout.
- Very visible "Pages to print" section.
- All / Odd / Even page buttons plus custom ranges.
- Sticky Print button on phones.
- Larger touch targets.
- Printer selector and queue grouped cleanly on desktop.
- Auto / Light / Dark themes.
- Printer capability controls still refresh when the selected printer changes.

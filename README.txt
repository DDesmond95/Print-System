PhonePrint v3
=============

What v3 adds
------------
- Upload PDF first and inspect it.
- Detects real page count.
- Rejects zero-page PDFs.
- Live page-range validation.
- Clear error when a requested page does not exist.
- Odd / even filtering.
- Clickable page thumbnails.
- Layout preview rendered from the real PDF.
- Preview responds to:
  * page range
  * odd/even
  * paper size
  * orientation
  * margins
  * scaling
  * pages per sheet
- Sheet count summary.
- Per-printer remembered settings in browser localStorage.
- Named presets in browser localStorage.
- Printer queue view + cancel.
- Windows spooler printer status text.
- Auto / Light / Dark themes.
- Mobile-first UI with sticky Print button.
- Desktop two-column UI.
- "More settings" keeps the main interface simpler.
- Standard capabilities refresh whenever printer changes.
- Native DocumentPropertiesW + CreateDCW printing path preserved.

Install
-------
Open Anaconda Prompt:

  cd "D:\CodeAlpha\Projects\IN_PROGRESS\Print System"
  conda activate phoneprint
  pip install -r requirements.txt
  python server.py

Open on Android:
  http://[IP]:5000

Security
--------
The console prints a random PIN each time the server starts.

Optional fixed PIN:
  set PRINT_PIN=123456
  python server.py

Do not expose port 5000 to the public Internet.

Preview limits
--------------
The preview represents PhonePrint-controlled layout:
- page selection
- odd/even
- paper dimensions reported by Windows
- orientation
- margins
- N-up
- scaling

It cannot exactly reproduce every vendor-private HP/Canon/Fuji/Adobe setting
because those options can live in opaque driver-private DEVMODE data.

Printer status note
-------------------
The status displayed is Windows spooler status. It is useful for states reported
to Windows, but some printer hardware/driver combinations do not report a truly
real-time physical device state while idle.

Virtual printers
----------------
Adobe PDF / Microsoft Print to PDF / XPS can appear in the printer list.
Some virtual printers will still open a Save As window on the Windows laptop.

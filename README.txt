PhonePrint for Windows
======================

1. Put this folder somewhere on your Windows laptop, e.g.
   C:\PhonePrint

2. Open "Anaconda Prompt" or "Miniconda Prompt":

   conda create -n phoneprint python=3.12 -y
   conda activate phoneprint
   pip install -r requirements.txt

3. Make sure normal Windows printing to your FX DocuPrint M265 z already works.

4. Run:

   python server.py

   or double-click run_phoneprint.bat after the environment exists.

5. Windows Firewall:
   Allow Python on your CURRENT network profile when prompted.
   Do NOT turn the firewall off.

6. The console prints a six-digit PIN and:
   http://10.17.13.77:5000

   Open that address in Chrome on Android.

7. If the phone cannot open the page but the laptop can open
   http://127.0.0.1:5000
   then your Wi-Fi is blocking phone-to-laptop connections (or Windows Firewall is).

Notes
-----
- PDF only.
- Pages support "all" or ranges such as 1-3,5,8-10.
- Odd/even filtering happens after the range selection.
- N-up is done by PhonePrint itself.
- Margins are inside the driver's printable region, not true edge-to-edge physical margins.
- Scale percentage is a percentage of "fit", and is capped at the selected N-up cell so pages do not overlap.
- The code asks the Windows printer driver for orientation, paper, duplex, copies, and print quality through DEVMODE.
- Driver-specific features such as toner-save, secure print, tray locks, booklet mode, etc. are not exposed by this version.
- The FX DocuPrint M265 class is monochrome, so no color selector is shown.

# pm290c-ble-python

A Python script to print text and images to the **PM290C portable thermal printer** over Bluetooth Low Energy (BLE), using the TSPL (TSC Printer Language) protocol.

The PM290C protocol was reverse-engineered from a [PacketLogger](https://developer.apple.com/documentation/bluetooth/logging-bluetooth-packets) capture of the [Labelnize](https://apps.apple.com/us/app/labelnize/id6479abortthat) iOS app. This project replaces the need for the proprietary app, enabling command-line printing from macOS and Linux.

Built at the [Makeability Lab](https://makeabilitylab.cs.washington.edu/), University of Washington.

## Features

- **Text printing** with configurable font size and centering
- **Image printing** — any image format Pillow supports (PNG, JPEG, BMP, etc.)
- Auto-scales images to the 384px (54mm) print width
- Adjustable print density (1–15)
- Auto-discovers the printer via BLE scan

## Hardware

| Spec | Value |
|------|-------|
| Printer | PM290C (54mm thermal, continuous roll) |
| Resolution | 203 DPI (384 pixels across 54mm) |
| Interface | Bluetooth Low Energy (BLE) |
| Protocol | TSPL (TSC Printer Language) |

## Requirements

- Python 3.8+
- macOS or Linux (BLE support via [Bleak](https://github.com/hbldh/bleak))
- PM290C printer, powered on and **not** connected to a phone app

### Setup

```bash
git clone https://github.com/makeabilitylab/pm290c-ble-python.git
cd pm290c-ble-python
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# Print text (centered)
python pm290c_tspl_print.py "Hello World!"

# Print with larger font
python pm290c_tspl_print.py --font-size 48 "Big Text"

# Print an image
python pm290c_tspl_print.py --image photo.png

# Adjust print darkness (1-15, default 10)
python pm290c_tspl_print.py --density 12 "Darker print"

# Verbose mode (shows TSPL commands and BLE traffic)
python pm290c_tspl_print.py -v --image fortune.png
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `text` | Text string to print | — |
| `--image FILE` | Image file to print | — |
| `--font-size N` | Font size for text mode | 24 |
| `--density N` | Print density, 1–15 | 10 |
| `--height-mm N` | Label height in mm (auto-calculated if omitted) | auto |
| `-v, --verbose` | Show TSPL commands and BLE debug info | off |

## BLE Protocol Details

The PM290C exposes these GATT characteristics:

| UUID | Handle | Purpose |
|------|--------|---------|
| `0000ff02` | 0x0006 | **Write** — send TSPL commands and bitmap data |
| `0000ff01` | 0x0008 | **Notify** — printer responses (battery, status) |
| `0000ae3b` | 0x0082 | **Init** — handshake packet required before printing |
| `0000ff03` | 0x000D | **Notify** — secondary status channel |

### Command sequence

1. Subscribe to notification characteristics (`ff01`, `ff03`)
2. Send init packet to `ae3b`
3. Query battery via `BATTERY?\r\n` (optional, confirms link)
4. Send status query `\x1b\x21\x3f\r\n`
5. Send TSPL payload: `SIZE`, `GAP`, `DIRECTION`, `DENSITY`, `CLS`, `BITMAP` data, then `PRINT 1,1`

Data is sent in 500-byte chunks (MTU is 503) with a short delay between chunks.

## How It Works

The script converts text or images into a 1-bit bitmap, wraps it in TSPL commands, and streams it to the printer over BLE:

1. **Text mode:** Renders the string to a Pillow image using available system fonts, centered on the 384px-wide canvas.
2. **Image mode:** Opens the image, scales to 384px width (preserving aspect ratio), and converts to 1-bit.
3. The 1-bit image is packed into TSPL's bitmap format (MSB first, 0 = black dot, 1 = white) and sent as a `BITMAP` command followed by `PRINT`.

## Troubleshooting

**Printer not found**
- Make sure the PM290C is powered on
- Disconnect it from any phone apps (Labelnize, etc.) — only one BLE central can connect at a time
- Check that Bluetooth is enabled on your computer

**Image prints inverted (white/black swapped)**
- This can happen if the source image has an unusual color profile. Try converting to RGB first: `convert input.png -colorspace sRGB output.png`

**Image cuts off partway**
- Try increasing the chunk delay: edit `asyncio.sleep(0.03)` in `send_chunked` to `0.05` or higher
- Very large images may exceed the printer's buffer; try reducing image resolution

## Reverse Engineering Notes

The protocol was captured using Apple's [PacketLogger](https://developer.apple.com/documentation/bluetooth/logging-bluetooth-packets) while the Labelnize iOS app printed to the PM290C. Key findings:

- The printer speaks **TSPL** (TSC Printer Language), a well-documented label printer protocol
- An init handshake packet (`FE DC BA C0 07 00 06 00 FF FF FF FF FF EF`) must be sent to characteristic `ae3b` before any TSPL commands
- The `BITMAP` command uses **mode 1** (overwrite), with pixel data packed MSB-first
- The `PRINT` command must follow the bitmap data (not precede it) to ensure the full image is buffered before printing

## License

MIT

## Acknowledgments

- [Bleak](https://github.com/hbldh/bleak) — cross-platform BLE library for Python
- [TSC TSPL documentation](https://www.tscprinters.com/) — TSPL command reference
- Apple PacketLogger — BLE packet capture tool
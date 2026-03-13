#!/usr/bin/env python3
"""
PM290C Thermal Printer — BLE TSPL print script.

Protocol reverse-engineered from PacketLogger capture of the Labelnize iOS app.
The PM290C uses TSPL (TSC Printer Language) over BLE.

Write characteristic: 0000ff02 (handle 0x0006)
Notify characteristic: 0000ff01 (handle 0x0008)
Init characteristic: 0000ae3b (handle 0x0082)

Requirements:
  pip install bleak Pillow

Usage:
  python pm290c_tspl_print.py "Hello World!"
  python pm290c_tspl_print.py --font-size 48 "Big Text"
  python pm290c_tspl_print.py --image photo.png
  python pm290c_tspl_print.py --density 10 "Dark print"
"""

import argparse
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

# BLE UUIDs (from PacketLogger capture)
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"   # handle 0x0006
NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"  # handle 0x0008 (responses)
INIT_UUID = "0000ae3b-0000-1000-8000-00805f9b34fb"    # handle 0x0082
NOTIFY2_UUID = "0000ff03-0000-1000-8000-00805f9b34fb" # handle 0x000D (status)

# PM290C print specs
PRINT_WIDTH_PX = 384       # 54mm at 203 DPI
BYTES_PER_ROW = 48         # 384 / 8
PAPER_WIDTH_MM = 54

# Init packet sent to ae3b before any commands (from capture)
AE3B_INIT = bytes([0xFE, 0xDC, 0xBA, 0xC0, 0x07, 0x00, 0x06, 0x00,
                   0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xEF])

# BLE chunk size
CHUNK_SIZE = 500  # MTU was 503


def notification_handler(sender, data):
    """Callback for BLE notifications."""
    try:
        text = data.decode('ascii', errors='replace').strip()
        if text:
            print(f"  << {text}")
    except Exception:
        print(f"  << {data.hex()}")


def text_to_bitmap(text, font_size=24, width_px=PRINT_WIDTH_PX):
    """Render text to 1-bit bitmap. Returns (rows, raw_bytes)."""
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    # Measure text
    dummy = Image.new('1', (width_px, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    img_h = text_h + 20

    # Render (white background, black text)
    img = Image.new('1', (width_px, img_h), color=1)
    draw = ImageDraw.Draw(img)
    x = max(0, (width_px - text_w) // 2)
    draw.text((x, 10), text, font=font, fill=0)

    return image_obj_to_bitmap(img)


def image_to_bitmap(image_path, width_px=PRINT_WIDTH_PX):
    from PIL import Image, ImageOps

    img = Image.open(image_path)
    # Convert to grayscale first (handles RGBA, palette, etc.)
    img = img.convert('L')
    ratio = width_px / img.width
    new_h = int(img.height * ratio)
    img = img.resize((width_px, new_h))
    # Threshold to 1-bit without dithering for clean line art
    img = img.point(lambda x: 0 if x < 128 else 255, '1')

    # Optional: Add extra blank rows at the bottom to ensure clean cut
    pad_rows = 80  # ~10mm at 203 DPI
    padded = Image.new('1', (img.width, img.height + pad_rows), color=0)
    padded.paste(img, (0, 0))
    img = padded

    return image_obj_to_bitmap(img)


def image_obj_to_bitmap(img):
    """Convert a Pillow 1-bit image to raw bitmap bytes for TSPL BITMAP command.

    TSPL BITMAP format: 1 bit per pixel, MSB first, 0=white, 1=black.
    Pillow '1' mode: 0=black, 255=white.
    So: Pillow 0 (black) -> bit 1, Pillow 255 (white) -> bit 0.

    Returns (num_rows, raw_bytes).
    """
    width = img.width
    height = img.height
    bpr = width // 8

    raw = bytearray()
    for y in range(height):
        for bx in range(bpr):
            byte_val = 0
            for bit in range(8):
                px = bx * 8 + bit
                if px < width:
                    pixel = img.getpixel((px, y))
                    if pixel == 0:  # black in Pillow
                        byte_val |= (1 << (7 - bit))
            raw.append(byte_val)

    return height, bytes(raw)


async def send_chunked(client, uuid, data, chunk_size=CHUNK_SIZE):
    """Send data in BLE-MTU-sized chunks."""
    t0 = time.time()
    num_chunks = (len(data) + chunk_size - 1) // chunk_size
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i + chunk_size]
        await client.write_gatt_char(uuid, chunk, response=False)
        await asyncio.sleep(0.2)
    print(f"  Chunks sent: {num_chunks} chunks, {len(data)} bytes [{time.time()-t0:.2f}s]")


async def main_async():
    t_start = time.time()

    parser = argparse.ArgumentParser(
        description="Print to PM290C via BLE (TSPL protocol)"
    )
    parser.add_argument("text", nargs="?", default=None,
                        help="Text to print")
    parser.add_argument("--image", type=str, default=None,
                        help="Image file to print")
    parser.add_argument("--font-size", type=int, default=24,
                        help="Font size (default: 24)")
    parser.add_argument("--density", type=int, default=10,
                        help="Print density 1-15 (default: 10)")
    parser.add_argument("--height-mm", type=int, default=None,
                        help="Label height in mm (auto-calculated if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    if args.text is None and args.image is None:
        parser.error("Provide text to print or --image")

    # Build bitmap
    t0 = time.time()
    if args.image:
        print(f"Loading image: {args.image}")
        num_rows, bitmap_data = image_to_bitmap(args.image)
    else:
        print(f"Rendering: {args.text!r} (size {args.font_size})")
        num_rows, bitmap_data = text_to_bitmap(args.text, font_size=args.font_size)
    print(f"Bitmap: {num_rows} rows x {PRINT_WIDTH_PX}px ({len(bitmap_data)} bytes) [{time.time()-t0:.2f}s]")

    # Calculate height in mm (203 DPI = 8 dots/mm)
    height_mm = args.height_mm or max(1, num_rows // 8)

    # Scan for printer
    # print("Scanning for PM290C...")
    # t0 = time.time()
    # devices = await BleakScanner.discover(timeout=10.0)
    # target = next((d for d in devices if d.name and "PM290" in d.name.upper()), None)
    # if not target:
    #     print("PM290C not found! Is it on and not connected to your laptop?")
    #     sys.exit(1)
    # print(f"Found: {target.name} ({target.address}) [{time.time()-t0:.2f}s]")
    print("Scanning for PM290C...")
    t0 = time.time()
    target = await BleakScanner.find_device_by_name("PM290C", timeout=10.0)
    if not target:
        print("PM290C not found! Is it on and not connected to your phone?")
        sys.exit(1)
    print(f"Found: {target.name} ({target.address}) [{time.time()-t0:.2f}s]")

    # Connect
    print("Connecting...")
    t0 = time.time()
    async with BleakClient(target.address) as client:
        print(f"Connected. MTU={client.mtu_size} [{time.time()-t0:.2f}s]")

        # Subscribe to notifications
        await client.start_notify(NOTIFY_UUID, notification_handler)
        try:
            await client.start_notify(NOTIFY2_UUID, notification_handler)
        except Exception:
            pass
        await asyncio.sleep(0.3)

        # Send ae3b init packet (from capture)
        t0 = time.time()
        if args.verbose:
            print(f"  >> ae3b init: {AE3B_INIT.hex()}")
        await client.write_gatt_char(INIT_UUID, AE3B_INIT, response=False)
        await asyncio.sleep(0.2)
        print(f"Init packet sent [{time.time()-t0:.2f}s]")

        # Query battery (optional, confirms communication)
        await client.write_gatt_char(WRITE_UUID, b'BATTERY?\r\n', response=False)
        await asyncio.sleep(0.2)

        # Build TSPL command sequence (exactly matching Labelnize capture)
        tspl_header = (
            f"SIZE {PAPER_WIDTH_MM} mm,{height_mm} mm\r\n"
            f"GAP 0,0\r\n"
            f"DIRECTION 0,0\r\n"
            f"DENSITY {args.density}\r\n"
            f"CLS\r\n"
            f"PRINT 1,1\r\n"
            f"BITMAP 0,0,{BYTES_PER_ROW},{num_rows},1,"
        ).encode('ascii')

        # Full payload: TSPL header + raw bitmap data + \r\n
        payload = tspl_header + bitmap_data + b'\r\n'

        print(f"Sending {len(payload)} bytes...")
        if args.verbose:
            header_preview = tspl_header.decode('ascii')
            print(f"  TSPL commands:\n    {header_preview.replace(chr(13)+chr(10), chr(10)+'    ')}")

        # Send print status query (as the app does before printing)
        await client.write_gatt_char(WRITE_UUID, b'\x1b\x21\x3f\r\n', response=False)
        await asyncio.sleep(0.3)

        # Send the full TSPL payload in chunks
        await send_chunked(client, WRITE_UUID, payload)

        print("Data sent. Waiting for printer...")
        t0 = time.time()
        await asyncio.sleep(5.0)
        print(f"Wait complete [{time.time()-t0:.2f}s]")

        # Cleanup
        try:
            await client.stop_notify(NOTIFY_UUID)
            await client.stop_notify(NOTIFY2_UUID)
        except Exception:
            pass

    print(f"Done! Total elapsed: {time.time()-t_start:.2f}s")


def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
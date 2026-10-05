"""Render attune-icon.svg into attune.ico (256, 64, 48, 32, 16; PNG entries, 32-bit with
alpha) and a preview PNG beside it. Run with the lean venv, which has Playwright:

    mixer-ng\\.venv\\Scripts\\python.exe attune\\desktop\\installer\\make_icon.py

Why Playwright and not an image library: the lean venv carries no Pillow and this PC has
no ImageMagick, while Playwright is already there for the README pictures and renders the
SVG exactly as the window's own engine would. PNG-compressed entries in an .ico have been
valid since Windows Vista and are what Explorer, the taskbar and Inno Setup read.
"""
import os
import struct
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "attune-icon.svg")
ICO = os.path.join(HERE, "attune.ico")
PREVIEW = os.path.join(HERE, "attune-icon-preview.png")
SIZES = (256, 64, 48, 32, 16)


def render(page, svg_text, size):
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(f"<html><body style='margin:0;background:transparent'>"
                     f"<div style='width:{size}px;height:{size}px'>{svg_text}</div></body></html>")
    page.evaluate("() => { const s = document.querySelector('svg'); s.setAttribute('width', '100%'); s.setAttribute('height', '100%'); }")
    return page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})


def pack_ico(pngs):
    """An .ico is a 6-byte header, one 16-byte entry per image, then the images."""
    head = struct.pack("<HHH", 0, 1, len(pngs))
    entries, body = b"", b""
    offset = 6 + 16 * len(pngs)
    for size, data in pngs:
        w = h = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    return head + entries + body


def main():
    svg_text = open(SVG, encoding="utf-8").read()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(device_scale_factor=1)
        pngs = [(s, render(page, svg_text, s)) for s in SIZES]
        # the preview: every size side by side on the window's own dark, for a look
        page.set_viewport_size({"width": 256 + 64 + 48 + 32 + 16 + 6 * 12, "height": 280})
        tiles = "".join(f"<div style='width:{s}px;height:{s}px;flex:none'>{svg_text}</div>" for s in SIZES)
        page.set_content(f"<html><body style='margin:0;background:#17191c'><div style='display:flex;align-items:flex-end;gap:12px;padding:12px'>{tiles}</div></body></html>")
        page.evaluate("() => { for (const s of document.querySelectorAll('svg')) { s.setAttribute('width', '100%'); s.setAttribute('height', '100%'); } }")
        page.screenshot(path=PREVIEW)
        browser.close()
    data = pack_ico(pngs)
    with open(ICO, "wb") as f:
        f.write(data)
    # read it back independently of the writer
    b = open(ICO, "rb").read()
    n = struct.unpack("<H", b[4:6])[0]
    got = [(b[6 + 16 * i] or 256, struct.unpack("<I", b[6 + 16 * i + 8:6 + 16 * i + 12])[0]) for i in range(n)]
    print(f"wrote {ICO}: {n} images {got}, {len(b)} bytes; preview {PREVIEW}")
    assert [g[0] for g in got] == list(SIZES)


if __name__ == "__main__":
    sys.exit(main())

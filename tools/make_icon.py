"""Bake ui/q_console.ico from a hand-drawn 16x16 pixel map.

Stdlib only (zlib does the PNG compression). Run it again after editing PIXELS:

    python tools/make_icon.py

Every exported size is an integer nearest-neighbour multiple of the 16x16 art,
so the pixels stay square and crisp instead of being resampled into mush - that
is the whole point of drawing at 16 and scaling up rather than drawing at 256
and letting Windows scale down.

The art: three ascending bars on a dark rounded tile. Orange is Claude Code and
teal is Codex, the same two accents the dashboard uses; the green bar is the
headroom read. It reads as "usage meter" at 16 px and does not collide with the
PowerShell chevron the window used to inherit.
"""

from __future__ import annotations

import os
import struct
import zlib

PALETTE = {
    ".": (0, 0, 0, 0),            # transparent (rounded corners)
    "+": (58, 69, 83, 255),       # tile edge
    "#": (21, 27, 36, 255),       # tile fill
    "d": (57, 66, 78, 255),       # baseline
    "w": (207, 214, 222, 255),    # prompt chevron
    "o": (224, 130, 87, 255),     # Claude orange
    "t": (55, 201, 163, 255),     # Codex teal
    "g": (61, 220, 132, 255),     # headroom green
}

PIXELS = [
    "..############..",
    ".##############.",
    "################",
    "##ww#######ggg##",
    "###ww######ggg##",
    "####ww#####ggg##",
    "###ww##ttt#ggg##",
    "##ww###ttt#ggg##",
    "#######ttt#ggg##",
    "###ooo#ttt#ggg##",
    "###ooo#ttt#ggg##",
    "###ooo#ttt#ggg##",
    "###ooo#ttt#ggg##",
    "###ddddddddddd##",
    ".##############.",
    "..############..",
]

SIZES = [16, 24, 32, 48, 64, 128, 256]


def base_rgba():
    assert len(PIXELS) == 16, "art must be 16 rows"
    rows = []
    for line in PIXELS:
        assert len(line) == 16, "row %r is not 16 wide" % line
        rows.append([PALETTE[ch] for ch in line])
    return rows


def scaled(rows, size):
    """Nearest-neighbour to `size`. Non-multiples of 16 (24) get chunky but
    crisp rows, which still beats a blurred downscale of a bigger bitmap."""
    out = []
    for y in range(size):
        src = rows[min(15, y * 16 // size)]
        out.append([src[min(15, x * 16 // size)] for x in range(size)])
    return out


def png_bytes(rows) -> bytes:
    size = len(rows)
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def build_ico(path: str) -> None:
    art = base_rgba()
    images = [(size, png_bytes(scaled(art, size))) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for size, blob in images:
        entries += struct.pack(
            "<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0,
            0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(bytes(entries))
        for _, blob in images:
            fh.write(blob)


def build_preview(path: str, scale: int = 12) -> None:
    """A big PNG of the same art, so the icon can be eyeballed without hunting
    for it in a taskbar."""
    with open(path, "wb") as fh:
        fh.write(png_bytes(scaled(base_rgba(), 16 * scale)))


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ico = os.path.join(root, "ui", "q_console.ico")
    build_ico(ico)
    print("wrote %s (%d bytes, sizes %s)"
          % (ico, os.path.getsize(ico), ", ".join(str(s) for s in SIZES)))

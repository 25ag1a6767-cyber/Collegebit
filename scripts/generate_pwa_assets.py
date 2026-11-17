"""Generate opaque maskable and iOS PWA PNG icons using Python stdlib only."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_rgba_png(path: Path) -> tuple[int, int, bytearray]:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a valid PNG")
    pos = 8
    width = height = bit_depth = color_type = 0
    idat_chunks: list[bytes] = []
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        chunk = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, comp, flt, inter = struct.unpack(">IIBBBBB", chunk)
            if bit_depth != 8 or color_type not in (2, 6) or comp != 0 or flt != 0 or inter != 0:
                raise ValueError(f"Unsupported PNG format in {path}: depth={bit_depth} color={color_type}")
        elif ctype == b"IDAT":
            idat_chunks.append(chunk)
        elif ctype == b"IEND":
            break

    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    decompressed = zlib.decompress(b"".join(idat_chunks))
    rgba = bytearray(width * height * 4)
    prev_row = bytearray(stride)
    idx = 0
    for y in range(height):
        ftype = decompressed[idx]
        idx += 1
        row = bytearray(decompressed[idx : idx + stride])
        idx += stride
        if ftype == 1:  # Sub
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev_row[i]
                c = prev_row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(a, b, c)) & 0xFF
        elif ftype != 0:
            raise ValueError(f"Unsupported PNG filter {ftype}")
        prev_row = row

        out_row = y * width * 4
        if bpp == 4:
            rgba[out_row : out_row + width * 4] = row
        else:
            for x in range(width):
                s = x * 3
                d = out_row + x * 4
                rgba[d] = row[s]
                rgba[d + 1] = row[s + 1]
                rgba[d + 2] = row[s + 2]
                rgba[d + 3] = 255
    return width, height, rgba


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def encode_rgb_png(path: Path, width: int, height: int, rgb: bytearray | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    stride = width * 3
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        start = y * stride
        scanlines.extend(rgb[start : start + stride])
    idat = zlib.compress(bytes(scanlines), level=9)
    png_bytes = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
    path.write_bytes(png_bytes)


def compose_centered_icon(
    src_w: int,
    src_h: int,
    src_rgba: bytearray,
    out_w: int,
    out_h: int,
    bg_rgb: tuple[int, int, int],
    scale_ratio: float | None = None,
    target_px: int | None = None,
) -> bytearray:
    br, bg, bb = bg_rgb
    out = bytearray([br, bg, bb]) * (out_w * out_h)
    if target_px is not None:
        target_box = max(1, min(out_w, out_h, target_px))
    else:
        ratio = 0.72 if scale_ratio is None else scale_ratio
        target_box = max(1, int(round(min(out_w, out_h) * ratio)))
    x0 = (out_w - target_box) // 2
    y0 = (out_h - target_box) // 2

    for dy in range(target_box):
        sy = min(src_h - 1, (dy * src_h) // target_box)
        oy = y0 + dy
        for dx in range(target_box):
            sx = min(src_w - 1, (dx * src_w) // target_box)
            si = (sy * src_w + sx) * 4
            sr, sg, sb, sa = src_rgba[si], src_rgba[si + 1], src_rgba[si + 2], src_rgba[si + 3]
            if sa == 0:
                continue
            ox = x0 + dx
            oi = (oy * out_w + ox) * 3
            if sa == 255:
                out[oi] = sr
                out[oi + 1] = sg
                out[oi + 2] = sb
            else:
                alpha = sa / 255.0
                inv = 1.0 - alpha
                out[oi] = int(sr * alpha + out[oi] * inv + 0.5)
                out[oi + 1] = int(sg * alpha + out[oi + 1] * inv + 0.5)
                out[oi + 2] = int(sb * alpha + out[oi + 2] * inv + 0.5)
    return out


IPHONE_SPLASH_SIZES = [
    (750, 1334),
    (828, 1792),
    (1125, 2436),
    (1170, 2532),
    (1179, 2556),
    (1290, 2796),
]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    assets = root / "frontend" / "assets"
    src_w, src_h, src_rgba = decode_rgba_png(assets / "icon-512.png")

    # Maskable icon places artwork cleanly inside the 80% Android adaptive-icon safe zone.
    maskable_rgb = compose_centered_icon(src_w, src_h, src_rgba, 512, 512, (18, 55, 42), scale_ratio=0.72)
    encode_rgb_png(assets / "icon-maskable-512.png", 512, 512, maskable_rgb)
    print("Generated", assets / "icon-maskable-512.png")

    # iOS requires an opaque RGB (no alpha transparency) 180x180 Home Screen icon.
    apple_icon_rgb = compose_centered_icon(src_w, src_h, src_rgba, 180, 180, (18, 55, 42), scale_ratio=0.80)
    encode_rgb_png(assets / "apple-touch-icon.png", 180, 180, apple_icon_rgb)
    print("Generated", assets / "apple-touch-icon.png")

    # Compact portrait splash screens matching #faf9f5 light theme.
    splash_dir = assets / "splash"
    for w, h in IPHONE_SPLASH_SIZES:
        badge_px = max(180, min(280, w // 4))
        splash_rgb = compose_centered_icon(src_w, src_h, src_rgba, w, h, (250, 249, 245), target_px=badge_px)
        out_path = splash_dir / f"apple-splash-{w}-{h}.png"
        encode_rgb_png(out_path, w, h, splash_rgb)
        print("Generated", out_path, out_path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()

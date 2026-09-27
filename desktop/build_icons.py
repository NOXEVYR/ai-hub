"""Offline conversion of the selected SVG to PNG and 32-bit DIB Windows icons.

Requires an already installed ImageMagick; this development helper downloads nothing.
Only the SVG title changes. Geometry, gradients and all remaining source bytes stay intact.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def dib_frame(size, rgba):
    if len(rgba) != size * size * 4:
        raise ValueError('Unexpected renderer pixel count')
    pixels, mask = bytearray(), bytearray()
    mask_stride = ((size + 31) // 32) * 4
    for y in range(size - 1, -1, -1):
        row_mask = bytearray(mask_stride)
        for x in range(size):
            r, g, b, a = rgba[(y * size + x) * 4:(y * size + x + 1) * 4]
            pixels.extend((b, g, r, a))
            if a == 0:
                row_mask[x // 8] |= 0x80 >> (x % 8)
        mask.extend(row_mask)
    header = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0,
                         len(pixels) + len(mask), 0, 0, 0, 0)
    return header + pixels + mask


def build(source, frontend, output, renderer):
    original = source.read_bytes()
    svg = original.decode('utf-8-sig')
    renamed, count = re.subn(r'(<title\b[^>]*>).*?(</title>)',
                             r'\g<1>曜核\g<2>', svg, count=1, flags=re.S)
    if count != 1:
        raise ValueError('Expected a single SVG title')
    frontend.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    named_svg = frontend / 'brand-lumacore.svg'
    named_svg.write_bytes(renamed.encode('utf-8'))
    (frontend / 'brand.svg').write_bytes(named_svg.read_bytes())
    master = output / 'render-master-2048.png'
    subprocess.run([renderer, '-background', 'none', '-density', '384', str(named_svg),
                    '-resize', '2048x2048', '-depth', '8', 'PNG32:' + str(master)], check=True)
    png = frontend / 'brand-lumacore.png'
    subprocess.run([renderer, str(master), '-filter', 'Lanczos', '-resize', '512x512',
                    '-depth', '8', 'PNG32:' + str(png)], check=True)
    frames = []
    for size in SIZES:
        small = output / ('lumacore-%s.png' % size)
        subprocess.run([renderer, str(master), '-filter', 'Lanczos', '-resize', '%sx%s' % (size, size),
                        '-depth', '8', 'PNG32:' + str(small)], check=True)
        rgba = subprocess.run([renderer, str(small), '-depth', '8', 'RGBA:-'],
                              check=True, capture_output=True).stdout
        frames.append(dib_frame(size, rgba))
    offset = 6 + 16 * len(frames)
    directory = bytearray(struct.pack('<HHH', 0, 1, len(frames)))
    for size, frame in zip(SIZES, frames):
        directory.extend(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(frame), offset))
        offset += len(frame)
    ico = bytes(directory) + b''.join(frames)
    (frontend / 'brand-lumacore.ico').write_bytes(ico)
    (frontend / 'brand.ico').write_bytes(ico)
    if source.read_bytes() != original:
        raise RuntimeError('Original SVG changed during rendering')
    manifest = {'display_name': '曜核', 'source_name': source.name,
                'source_sha256': hashlib.sha256(original).hexdigest(),
                'svg_sha256': hashlib.sha256(named_svg.read_bytes()).hexdigest(),
                'ico_sha256': hashlib.sha256(ico).hexdigest(),
                'png_sha256': hashlib.sha256(png.read_bytes()).hexdigest(),
                'ico_sizes': list(SIZES), 'ico_encoding': '32-bit BGRA DIB + AND mask',
                'png_size': [512, 512], 'geometry_changed': False}
    (output / 'icon-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--frontend', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--renderer', default='magick')
    args = parser.parse_args()
    build(args.source, args.frontend, args.output, args.renderer)

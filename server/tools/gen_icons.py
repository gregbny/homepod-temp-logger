"""Generate the PWA icons (thermometer) as PNG, pure stdlib (zlib+struct).
Blue gradient background + white thermometer with red mercury. 3x3 anti-aliasing."""
import struct, zlib, math, os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

TOP = (57, 135, 229)     # #3987e5
BOT = (28, 92, 171)      # #1c5cab
WHITE = (255, 255, 255)
RED = (227, 73, 72)      # #e34948

def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))

def cap_dist(px, py, ax, ay, bx, by):
    """Distance from point to segment AB (for capsule shapes)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)

def coverage(nx, ny, seg, r):
    (ax, ay, bx, by) = seg
    return 1.0 if cap_dist(nx, ny, ax, ay, bx, by) <= r else 0.0

# Normalized geometry [0,1]
CASE_SEG = (0.5, 0.20, 0.5, 0.72); CASE_R = 0.075
BULB_C = (0.5, 0.72); BULB_R = 0.155
RED_SEG = (0.5, 0.40, 0.5, 0.72); RED_R = 0.032
REDBULB_R = 0.105

def blend(bg, fg, cov):
    return tuple(int(round(bg[i] + (fg[i] - bg[i]) * cov)) for i in range(3))

def render(size):
    rows = []
    ss = 3
    for y in range(size):
        row = bytearray()
        for x in range(size):
            wc = rc = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    nx = (x + (sx + 0.5) / ss) / size
                    ny = (y + (sy + 0.5) / ss) / size
                    inside_case = (cap_dist(nx, ny, *CASE_SEG) <= CASE_R or
                                   math.hypot(nx - BULB_C[0], ny - BULB_C[1]) <= BULB_R)
                    inside_red = (cap_dist(nx, ny, *RED_SEG) <= RED_R or
                                  math.hypot(nx - BULB_C[0], ny - BULB_C[1]) <= REDBULB_R)
                    if inside_case:
                        wc += 1
                    if inside_red:
                        rc += 1
            wc /= ss * ss; rc /= ss * ss
            bg = lerp(TOP, BOT, y / size)
            px = blend(bg, WHITE, wc)
            px = blend(px, RED, rc)          # mercury on top of the white casing
            row += bytes(px)
        rows.append(bytes(row))
    return rows

def write_png(path, size):
    rows = render(size)
    raw = b"".join(b"\x00" + r for r in rows)          # filter 0 per scanline
    comp = zlib.compress(raw, 9)
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    print(f"{path} ({size}x{size}, {len(png)} bytes)")

os.makedirs(OUT, exist_ok=True)
write_png(f"{OUT}/icon-512.png", 512)
write_png(f"{OUT}/icon-192.png", 192)
write_png(f"{OUT}/apple-touch-icon.png", 180)
print("OK")

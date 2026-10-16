"""Draw the PWA icons: a red gift box with a gold ribbon on a coal background.

Run: .venv/bin/python tools/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent.parent / "app" / "static" / "icons"
COAL, RED, GOLD = (28, 25, 23), (185, 28, 28), (234, 179, 8)


def draw(size: int, art_scale: float, rounded: bool) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=COAL)
    else:  # maskable: full bleed, the launcher applies the shape
        d.rectangle((0, 0, size, size), fill=COAL)

    s = size * art_scale
    ox = oy = (size - s) / 2
    box = lambda x0, y0, x1, y1: (ox + x0 * s, oy + y0 * s, ox + x1 * s, oy + y1 * s)
    d.rectangle(box(0.12, 0.42, 0.88, 0.92), fill=RED)            # box body
    d.rectangle(box(0.06, 0.30, 0.94, 0.44), fill=RED)            # lid
    d.rectangle(box(0.44, 0.30, 0.56, 0.92), fill=GOLD)           # vertical ribbon
    d.rectangle(box(0.06, 0.34, 0.94, 0.40), fill=GOLD)           # ribbon across the lid
    d.ellipse(box(0.24, 0.10, 0.50, 0.32), outline=GOLD, width=max(2, int(s * 0.05)))  # bow
    d.ellipse(box(0.50, 0.10, 0.76, 0.32), outline=GOLD, width=max(2, int(s * 0.05)))
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, 0.78, rounded=True).save(OUT / "icon-192.png")
    draw(512, 0.78, rounded=True).save(OUT / "icon-512.png")
    draw(512, 0.62, rounded=False).save(OUT / "maskable-512.png")  # inside the 80% safe zone
    print("wrote", sorted(p.name for p in OUT.iterdir()))

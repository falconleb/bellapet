"""One-time script to generate PWA icons."""
from PIL import Image, ImageDraw, ImageFont
import os

OUT = os.path.join(os.path.dirname(__file__), 'static', 'img')
os.makedirs(OUT, exist_ok=True)

BG   = (26, 26, 30)
GOLD = (212, 167, 44)
PAW  = "🐾"


def make_icon(size):
    img = Image.new('RGBA', (size, size), BG)
    d   = ImageDraw.Draw(img)
    # rounded rect background
    margin = size // 10
    d.rounded_rectangle([margin, margin, size - margin, size - margin],
                        radius=size // 5, fill=GOLD)
    # paw emoji text
    try:
        fsize = int(size * 0.52)
        font  = ImageFont.truetype("seguiemj.ttf", fsize)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", int(size * 0.45))
        except Exception:
            font = ImageFont.load_default()
    # draw centered paw symbol (fallback: P)
    symbol = "P"
    bbox   = d.textbbox((0, 0), symbol, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2
    d.text((x, y), symbol, fill=BG, font=font)
    return img


for sz in (192, 512):
    path = os.path.join(OUT, f'icon-{sz}.png')
    make_icon(sz).save(path, 'PNG')
    print(f'OK {path}')

print('Done.')

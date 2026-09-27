"""Draw packaging/icon.ico (the house mark from the app's sidebar). Needs Pillow; run once."""
from pathlib import Path

from PIL import Image, ImageDraw

BLUE, WHITE = (31, 95, 191, 255), (255, 255, 255, 255)
SIZE, PAD = 1024, 190  # draw large, then scale down smoothly


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24, 24, SIZE - 24, SIZE - 24), radius=210, fill=BLUE)
    s = (SIZE - 2 * PAD) / 24  # the sidebar icon uses a 24x24 grid

    def pt(x: float, y: float) -> tuple[float, float]:
        return (PAD + x * s, PAD + y * s)

    width = round(2.3 * s)
    for path in ([(3, 11), (12, 4), (21, 11)], [(5, 10), (5, 20), (19, 20), (19, 10)], [(10, 20), (10, 14), (14, 14), (14, 20)]):
        d.line([pt(*p) for p in path], fill=WHITE, width=width, joint="curve")
        for p in (path[0], path[-1]):  # round the ends
            x, y = pt(*p)
            d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=WHITE)
    return img


if __name__ == "__main__":
    big = draw().resize((256, 256), Image.LANCZOS)
    out = Path(__file__).with_name("icon.ico")
    big.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(Path(__file__).with_name("icon.png"))
    print("wrote", out)

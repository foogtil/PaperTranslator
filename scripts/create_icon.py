"""Convert the selected 文献译境 artwork into Windows icon sizes.

The checked-in icon.png is the original transparent master. Conversion does
not redraw the artwork and requires no image-generation service or API key.
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def main():
    with Image.open(ASSETS / "icon.png") as source:
        image = source.convert("RGBA")
    if image.width != image.height or image.width < 256:
        raise SystemExit("icon.png must be a square image of at least 256 pixels")
    image.save(ASSETS / "icon.ico", sizes=[(size, size) for size in SIZES])
    image.resize((256, 256), Image.Resampling.LANCZOS).save(ASSETS / "icon-preview.png")


if __name__ == "__main__":
    main()

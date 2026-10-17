from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(image)
d.rounded_rectangle((8, 8, 248, 248), radius=46, fill="#172742")
d.rounded_rectangle((56, 35, 199, 221), radius=12, fill="white")
d.polygon([(156, 35), (199, 78), (156, 78)], fill="#b8cbf4")
d.line((76, 99, 169, 99), fill="#d3ddef", width=8)
d.line((76, 120, 158, 120), fill="#d3ddef", width=8)
d.rounded_rectangle((72, 145, 226, 209), radius=18, fill="#285fe8")
font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 39)
d.text((88, 148), "A 译", font=font, fill="white")
(root / "assets").mkdir(exist_ok=True)
image.save(root / "assets" / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

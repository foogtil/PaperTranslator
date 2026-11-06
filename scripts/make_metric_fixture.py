"""Rebuild the original, synthetic font-metrics fixture (optional fontTools)."""
from io import BytesIO
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
import pymupdf as fitz


def main():
    builder = FontBuilder(1000, isTTF=True)
    names = ['.notdef', 'space', 'A', 'B', 'C']
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({32: 'space', 65: 'A', 66: 'B', 67: 'C'})
    glyphs = {}
    for name in names:
        pen = TTGlyphPen(None)
        if name not in ('space', '.notdef'):
            pen.moveTo((50, 0))
            pen.lineTo((450, 0))
            pen.lineTo((450, 500))
            pen.lineTo((50, 500))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 0) for name in names})
    builder.setupHorizontalHeader(ascent=541, descent=0)
    builder.setupNameTable({'familyName': 'MetricProbe', 'styleName': 'Regular',
                           'uniqueFontIdentifier': 'MetricProbe', 'fullName': 'MetricProbe', 'psName': 'MetricProbe'})
    builder.setupOS2(sTypoAscender=541, sTypoDescender=0, usWinAscent=541, usWinDescent=0)
    builder.setupPost()
    buffer = BytesIO()
    builder.save(buffer)
    target = Path(__file__).resolve().parents[1] / 'assets' / 'selftest' / 'metric-font.pdf'
    target.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_font(fontname='probe', fontbuffer=buffer.getvalue())
        page.insert_text((50, 70), 'ABC ABC', fontname='probe', fontsize=20)
        doc.save(target)


if __name__ == '__main__':
    main()

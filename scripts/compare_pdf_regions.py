"""Measure region fragmentation without modifying or uploading the PDF."""
from collections import Counter
import json
import sys
from pathlib import Path
import pymupdf
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from papertranslator.extraction import native_blocks

all_blocks = []
source_glyphs = 0
with pymupdf.open(sys.argv[1]) as document:
    for index, page in enumerate(document):
        page.set_rotation(0)
        data = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
        source_glyphs += sum(not c["c"].isspace() for block in data["blocks"] if block.get("type") == 0 for line in block["lines"] for span in line["spans"] for c in span["chars"])
        all_blocks.extend(native_blocks(page, index))
prose = [b for b in all_blocks if not b.protected]
print(json.dumps({"total_regions": len(all_blocks), "translatable_regions": len(prose),
                  "source_glyphs": source_glyphs, "mapped_glyphs": sum(len(b.erasers) for b in all_blocks),
                  "short_regions": {str(n): sum(len(b.text) <= n for b in prose) for n in (1, 2, 3, 5, 10, 20)},
                  "samples": [b.text[:180] for b in prose[:8]]}, ensure_ascii=False, indent=2))

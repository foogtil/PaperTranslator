"""Read-only job diagnostics; never read settings or credentials."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys

import pymupdf

root = Path(sys.argv[1])
paths = [root] if root.is_file() else sorted(root.glob("*EDG*.ptrans.json"))
for path in paths:
    data = json.loads(path.read_text(encoding="utf-8"))
    blocks = data.get("blocks", [])
    pending = [b for b in blocks if not b.get("protected") and b.get("status") not in ("translated", "edited", "preserved")]
    summary = {
        "project": str(path), "source": data.get("source"), "model": data.get("provider", {}).get("model"),
        "blocks": len(blocks), "status_counts": dict(Counter(b.get("status") for b in blocks)), "pending": len(pending),
        "pending_length_buckets": {str(n): sum(len(b.get("text", "")) <= n for b in pending) for n in (1, 2, 3, 5, 10, 20)},
        "pending_samples": [{"id": b["id"], "page": b["page"] + 1, "text": b["text"][:80], "notes": b.get("notes", [])} for b in pending[:16]],
        "failed_samples": [{"id": b["id"], "text": b["text"][:80], "notes": b.get("notes", [])} for b in blocks if b.get("status") == "failed"][:12],
    }
    source = Path(data.get("source", ""))
    if source.is_file():
        with pymupdf.open(source) as document:
            spans = [span for page in list(document)[:2] for block in page.get_text("dict")["blocks"] if block.get("type") == 0 for line in block["lines"] for span in line["spans"]]
            summary["first_two_pages_spans"] = len(spans)
            summary["short_span_samples"] = [{"text": s["text"], "font": s["font"], "size": round(s["size"], 2)} for s in spans if len(s["text"].strip()) <= 5][:20]
    print(json.dumps(summary, ensure_ascii=False, indent=2))

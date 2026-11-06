"""Compare serialized text input on identical synthetic documents, without APIs."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from papertranslator.models import Block, Options, Project, Provider, Unit
from papertranslator.translation import PLACEHOLDER, translate_project


def measure(compact, workers=2, response_delay=0):
    options = Options(vision_review=False, compact_context=compact, translation_workers=workers)
    blocks, units = [], []
    for page in range(14):
        for paragraph in range(12):
            text = f"Section {page + 1}, paragraph {paragraph + 1}. " + (
                "The matrix representation preserves the graph structure and its relationships. " * 9)
            blocks.append(Block(f"p{page}-{paragraph}", page, [20, 20, 500, 70], text))
        if page in (3, 8):
            identity = f"figure-{page}"
            members = [Block(f"f{page}-{i}", page, [20, 80, 150, 100],
                             f"Training accuracy experiment {i}", unit_id=identity, role="figure_label")
                       for i in range(24)]
            blocks.extend(members)
            units.append(Unit(identity, page, "figure", [20,80,500,400], f"Figure {page}", [b.id for b in members]))
    project = Project("synthetic-no-pdf", "", vars(options), blocks=blocks, units=units)
    client = Mock(provider=Provider(vision=False), cancel=threading.Event())
    calls = []
    def complete(messages):
        calls.append(copy.deepcopy(messages))
        if response_delay:
            time.sleep(response_delay)
        value = json.loads(messages[-1]["content"])
        return json.dumps({"segments": [{"id": s["id"], "translation":
            "离线示例译文 " + " ".join(PLACEHOLDER.findall(s["source"]))} for s in value["segments"]]})
    client.complete.side_effect = complete
    start = time.perf_counter()
    translate_project(project, options, client, lambda: None, lambda _: None)
    elapsed = time.perf_counter() - start
    text = "".join(m["content"] for request in calls for m in request)
    return {"requests": len(calls), "input_characters": len(text), "input_utf8_bytes": len(text.encode()), "elapsed_seconds": round(elapsed, 3),
            "translated_regions": sum(b.status == "translated" for b in blocks),
            "requested_ids": [s["id"] for request in calls for s in json.loads(request[-1]["content"])["segments"]]}


def main():
    full, compact = measure(False), measure(True)
    ids_match = sorted(full.pop("requested_ids")) == sorted(compact.pop("requested_ids"))
    result = {"full_context": full, "compact_context": compact, "same_requested_ids": ids_match,
              "input_character_reduction_percent": round(100 * (1 - compact["input_characters"] / full["input_characters"]), 2),
              "note": "Synthetic input-text comparison only; not measured provider tokens, fees or real translation quality."}
    result["ok"] = ids_match and compact["translated_regions"] == full["translated_regions"] and compact["input_characters"] < full["input_characters"]
    serial, parallel = measure(True, workers=1, response_delay=0.04), measure(True, workers=2, response_delay=0.04)
    same_ids = sorted(serial.pop("requested_ids")) == sorted(parallel.pop("requested_ids"))
    result["simulated_latency"] = {"response_delay_seconds": 0.04, "serial": serial, "parallel": parallel,
        "same_requested_ids": same_ids,
        "note": "Synthetic HTTP waiting only; real speed depends on service quotas, OCR, rendering and network."}
    result["ok"] = result["ok"] and same_ids and serial["input_characters"] == parallel["input_characters"]
    output = ROOT / "test-output" / "token-optimization" / "context-comparison.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

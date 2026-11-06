"""Bound supporting context without truncating any requested translation unit."""
from collections import defaultdict
from .models import has_translation, block_enabled


def _excerpt(text, limit):
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


class TranslationContext:
    def __init__(self, project, options=None):
        self.by_page = defaultdict(list)
        blocks = [b for b in project.blocks if options is None or block_enabled(b, options)]
        self.by_id = {b.id: b for b in blocks}
        self.intro = [b for b in blocks if b.page < 2]
        for block in blocks:
            self.by_page[block.page].append(block)

    def fields(self, blocks, figure, compact=True):
        current = {b.id for b in blocks}
        members = [self.by_id[i] for i in figure.block_ids if i in self.by_id] if figure else []
        page = self.by_page[blocks[0].page]
        if compact:
            # Requested paragraphs/figure labels already occur in segments.
            # Keep nearby context and a bounded introduction only once.
            excluded = current | {b.id for b in members}
            seen = {b.text.strip() for b in blocks + members}
            positions = [i for i, b in enumerate(page) if b.id in excluded]
            nearby = []
            if positions:
                first, last = min(positions), max(positions)
                before = [b for b in page[:first] if b.id not in excluded][-2:]
                after = [b for b in page[last + 1:] if b.id not in excluded][:2]
                nearby = before + after
            def collect(candidates, budget, per_block):
                parts = []
                for b in candidates:
                    text = b.text.strip()
                    if b.id in excluded or not text or text in seen or budget <= 1:
                        continue
                    snippet = _excerpt(text, min(per_block, budget - 1))
                    parts.append(snippet)
                    seen.add(text)
                    budget -= len(snippet) + 1
                return "\n".join(parts)
            page_context = collect(nearby, 2000, 700)
            document_context = collect(self.intro, 2400, 1600)
        else:
            page_context = "\n".join(b.text for b in page)[:12000]
            document_context = "\n".join(b.text for b in self.intro)[:9000]
        fields = {"document_context": document_context, "page_context": page_context}
        if figure:
            labels = []
            for b in members:
                item = {"id": b.id}
                if not compact or b.id not in current:
                    item.update(source=b.text, role=b.role)
                    if compact and b.translation and has_translation(b):
                        item["translation"] = b.translation
                if not compact:
                    item.update(angle=b.angle, bbox=b.rect)
                labels.append(item)
            fields["unit"] = {"id": figure.id, "kind": "complete_figure",
                              "title": figure.title, "labels_and_caption": labels}
        return fields

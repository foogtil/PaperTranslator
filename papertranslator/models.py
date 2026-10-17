from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
import hashlib
import json
import os
import threading
import time
from typing import Any


class Cancelled(Exception):
    pass


def checkpoint(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("操作已取消；已完成的进度已保存。")


def digest_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    # On Windows, a reader (including the UI or an antivirus scanner) can hold
    # a short-lived handle without FILE_SHARE_DELETE. Retry only sharing/access
    # conflicts, with a bounded wait, keeping the previous snapshot intact.
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            break
        except PermissionError as error:
            if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 7:
                raise
            time.sleep(min(0.01 * 2 ** attempt, 0.25))


def dataclass_from(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Provider:
    name: str = "DeepSeek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    api_key: str = field(default="", repr=False)
    auth_header: str = "Authorization"
    vision: bool = True
    timeout: int = 180
    retries: int = 3
    input_price: float = 0.0
    output_price: float = 0.0
    currency: str = "CNY"

    def public_dict(self):
        return {k: v for k, v in asdict(self).items() if k != "api_key"}


PRESETS = {
    "DeepSeek": Provider(),
    "MiMo": Provider(name="MiMo", base_url="https://api.xiaomimimo.com/v1", model="mimo-v2.6-pro", auth_header="api-key"),
    "自定义": Provider(name="自定义", base_url="", model="", vision=False),
}

MODEL_PRESETS = {
    "DeepSeek": ("deepseek-v4-pro", "deepseek-flash"),
    "MiMo": ("mimo-v2.6-pro", "mimo-v2.6-flash"),
    "自定义": (),
}

LANGUAGES = ["自动检测", "英语", "简体中文", "繁体中文", "日语", "韩语", "德语", "法语", "西班牙语", "葡萄牙语", "俄语", "阿拉伯语", "印地语"]


@dataclass
class Options:
    source_language: str = "英语"
    target_language: str = "简体中文"
    page_range: str = ""
    ocr: bool = True
    ocr_dpi: int = 220
    ocr_rotations: bool = True
    vision_review: bool = True
    minimum_font: float = 6.0
    translate_references: bool = True
    glossary: dict[str, str] = field(default_factory=dict)
    font_path: str = ""
    batch_chars: int = 4500
    translate_figures: bool = True
    pdf_review_marks: bool = False
    export_json: bool = False

    def validate(self):
        if not self.target_language.strip() or self.target_language == "自动检测":
            raise ValueError("请选择或输入目标语言。")
        if not 96 <= self.ocr_dpi <= 400:
            raise ValueError("OCR DPI 应在 96 至 400 之间。")
        if not 3 <= self.minimum_font <= 24:
            raise ValueError("最小字号应在 3 至 24 pt 之间。")
        if self.font_path and not Path(self.font_path).is_file():
            raise ValueError("自定义字体不存在。")


@dataclass
class Block:
    id: str
    page: int
    rect: list[float]
    text: str
    kind: str = "native"
    angle: float = 0.0
    vertical: bool = False
    fontsize: float = 10.0
    color: int = 0
    bold: bool = False
    confidence: float = 1.0
    quad: list[list[float]] = field(default_factory=list)
    erasers: list[list[float]] = field(default_factory=list)
    translation: str = ""
    status: str = "pending"
    notes: list[str] = field(default_factory=list)
    approved: bool = False
    protected: bool = False
    local_width: float = 0.0
    local_height: float = 0.0
    role: str = "text"
    unit_id: str = ""
    template: str = ""
    inline: list[dict] = field(default_factory=list)

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    rotation: int
    analyzed: bool = False
    vision_analyzed: bool = False


@dataclass
class Unit:
    id: str
    page: int
    kind: str
    rect: list[float]
    title: str = ""
    block_ids: list[str] = field(default_factory=list)


@dataclass
class Project:
    source: str
    source_hash: str
    options: dict
    pages: list[PageInfo] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    selected_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "requests": 0})
    output: str = ""
    provider: dict = field(default_factory=dict)
    schema: int = 1
    extraction_version: int = 1
    units: list[Unit] = field(default_factory=list)
    # Never serialize credentials, even if Options gains new fields later.

    def save(self, path: str | Path):
        atomic_json(path, asdict(self))

    @classmethod
    def load(cls, path: str | Path, verify: bool = True):
        # Release the Windows file handle before parsing a large JSON snapshot,
        # so it cannot block the worker's next atomic replacement.
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            raise ValueError("不支持的项目版本。")
        data["pages"] = [dataclass_from(PageInfo, p) for p in data.get("pages", [])]
        data["blocks"] = [dataclass_from(Block, b) for b in data.get("blocks", [])]
        data["units"] = [dataclass_from(Unit, u) for u in data.get("units", [])]
        project = dataclass_from(cls, data)
        if verify and (not Path(project.source).is_file() or digest_file(project.source) != project.source_hash):
            raise ValueError("原 PDF 不存在或内容已变化；为避免错位，请重新分析。")
        return project


def parse_pages(spec: str, count: int) -> list[int]:
    if not spec.strip():
        return list(range(count))
    result = set()
    for part in spec.replace("，", ",").split(","):
        try:
            nums = part.strip().split("-")
            if len(nums) == 1:
                first = last = int(nums[0])
            elif len(nums) == 2:
                first, last = map(int, nums)
            else:
                raise ValueError
        except ValueError:
            raise ValueError("页码格式错误。示例：1-5,8,10-12") from None
        if not 1 <= first <= last <= count:
            raise ValueError(f"页码应在 1 至 {count} 之间，且起始页不大于结束页。")
        result.update(range(first - 1, last))
    return sorted(result)

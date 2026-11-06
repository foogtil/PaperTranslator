from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
import hashlib
import json
import os
import threading
import tempfile
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
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    tmp = Path(name)
    # On Windows, a reader (including the UI or an antivirus scanner) can hold
    # a short-lived handle without FILE_SHARE_DELETE. Retry only sharing/access
    # conflicts, with a bounded wait, keeping the previous snapshot intact.
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as error:
                if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 7:
                    raise
                time.sleep(min(0.01 * 2 ** attempt, 0.25))
    finally:
        tmp.unlink(missing_ok=True)


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
    compact_context: bool = True
    translation_workers: int = 2
    translate_figures: bool = True
    pdf_review_marks: bool = False
    export_json: bool = False

    def validate(self):
        if type(self.translation_workers) is not int or not 1 <= self.translation_workers <= 4:
            raise ValueError("同时翻译请求数应为 1 至 4。")
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
    glyphs: list[list[float]] = field(default_factory=list)
    layout_stale: bool = False

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    def accept_layout(self):
        self.layout_stale = False
        self.notes = [n for n in self.notes if not n.startswith("公式或段落结构已更新；")]

    def clear_export_notes(self):
        # Recompute export diagnostics on each attempt. OCR/model/manual review
        # notes remain; only known layout checks are transient.
        prefixes = ("译文在原区域内无法以最低 ", "相邻译文排版重叠，", "译文与图片或相邻内容相交，",
                    "公式与段落文字相交，", "公式上下标超出段落边界，", "公式纵横比例校验未通过，",
                    "段内公式占位符缺失或重复，", "段内公式排版未完整定位，", "旋转或竖排区域与相邻内容相交，",
                    "原页含尚未应用的涂黑批注", "文字与受保护公式重叠，", "文字区域超出页面", "缺少译文，",
                    "扫描文字区域跨越图片边界，", "OCR 置信度低于 75%", "图中文字背景复杂，",
                    "图中文字与图形重叠较多，", "图像修补编码失败", "竖排公式在原区域内无法完整放下，",
                    "译文改变了普通字体公式，", "原文字形删除校验未通过，")
        self.notes = [n for n in self.notes if not n.startswith(prefixes)]


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    rotation: int
    analyzed: bool = False
    vision_analyzed: bool = False


def has_translation(block: Block) -> bool:
    """A layout overflow retains a valid translation for the next export."""
    return not block.layout_stale and (block.status in ("translated", "edited", "preserved") or (
        block.status == "overflow" and bool(block.translation.strip())))


def block_enabled(block: Block, options: Options) -> bool:
    # Captions remain prose; only labels within a figure follow this switch.
    return options.translate_figures or block.role != 'figure_label'


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
        from .locking import identity, job_lock, save_guard
        with save_guard, job_lock(self.source):
            key = identity(path)
            snapshots = getattr(self, '_snapshots', {})
            expected = snapshots.get(key)
            if expected is not None and (not Path(path).exists() or digest_file(path) != expected):
                raise ValueError('此项目已由另一个窗口更新，请重新载入校对后再保存，避免覆盖较新的进度。')
            atomic_json(path, asdict(self))
            snapshots[key] = digest_file(path)
            self._snapshots = snapshots

    @classmethod
    def load(cls, path: str | Path, verify: bool = True):
        # Release the Windows file handle before parsing a large JSON snapshot,
        # so it cannot block the worker's next atomic replacement.
        contents = Path(path).read_bytes()
        data = json.loads(contents)
        if data.get("schema") != 1:
            raise ValueError("不支持的项目版本。")
        data["pages"] = [dataclass_from(PageInfo, p) for p in data.get("pages", [])]
        data["blocks"] = [dataclass_from(Block, b) for b in data.get("blocks", [])]
        data["units"] = [dataclass_from(Unit, u) for u in data.get("units", [])]
        project = dataclass_from(cls, data)
        from .locking import identity
        project._snapshots = {identity(path): hashlib.sha256(contents).hexdigest()}
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

# 文献译境 · PaperTranslator

[English](README.md) | **简体中文**

[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0--only-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](pyproject.toml)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-0078d4.svg)](#系统要求)

PaperTranslator 是一个 Windows 桌面端的 **PDF 原位翻译与人工校对** 工具，面向计算机、数学等英文文献。它按完整段落翻译，把同一张图的标签和图题放在同一上下文中，保护公式、数值、URL、DOI 和引用编号，并将译文写回原页面区域，而不是另外生成一份纯文本。

默认方向为英语 → 简体中文。内置 DeepSeek、Xiaomi MiMo 预设，也支持任意兼容 Chat Completions 的接口。

> 源码保存在本仓库；预编译的 Windows 程序发布在 [Releases](https://github.com/foogtil/PaperTranslator/releases) 页面，由维护者单独上传。也可以按下面的“从源码运行”或“构建 Windows 程序”操作。

## 下载

预编译的 Windows 程序发布在 [Releases](https://github.com/foogtil/PaperTranslator/releases) 页面。每个 Release 应同时包含：

- 便携 ZIP（内含许可和第三方源码）；
- 单文件 `.exe`；
- 源码 ZIP；
- 经过校验的 `vendor-sources` 源码归档（AGPL 对应源码）；
- SHA-256 校验文件。

## 界面截图

文档截图使用合成的示例 PDF 和预先编写的测试译文，不包含第三方论文内容。

| 主界面 | API 与排版设置 |
|---|---|
| ![主界面](docs/screenshots/home.png) | ![API 设置](docs/screenshots/api-settings.png) |

### 段落与整图校对

![校对与预览](docs/screenshots/review.png)

### 图像与表格翻译

![图像对照](docs/screenshots/figure-comparison.png)

![表格对照](docs/screenshots/table-comparison.png)

## 功能

- **保持位置**：页数、页面尺寸、CropBox、旋转和未选中页面保持不变；译文可搜索、可复制。
- **按完整段落翻译**：根据行距、缩进、字体和栏位重建段落，避免按单词逐条翻译。
- **整图翻译**：把嵌入图像、矢量图和多面板图归为同一张图，所有标签和图题共享上下文。
- **公式保护**：段内公式以原 PDF 的矢量片段随译文排版；编辑框中的 `[[MATH_n]]` 是公式标记，必须保留。
- **本地 OCR**：RapidOCR + ONNX Runtime 在 CPU 上识别扫描页和图内文字，内置中英文模型，无需显卡。
- **可选视觉核对**：所选模型支持图片时，可发送页面截图，补充漏识别、竖排和非中英文区域；纯文本接口退回 OCR 上下文。
- **占位符保护**：数值、公式、URL、DOI 和引用编号在请求前替换为占位符；模型改变或遗漏时拒绝采用结果。
- **人工校对流程**：修改译文、修正标签、框选补充 OCR 区域、保存进度并重新导出，无需重新请求 API。
- **可选批注与 JSON**：黄色“待校对”批注和 `.ptrans.json` / `.report.json` 导出默认关闭。
- **接口可配置**：DeepSeek、Xiaomi MiMo 和自定义 Chat Completions；模型 ID、Base URL、鉴权头均可修改。
- **无内置密钥、无遥测**：API Key 默认只驻留本次会话；勾选“记住密钥”时使用 Windows DPAPI 按当前用户加密。

## 系统要求

- Windows 10 / 11 x64。
- Python 3.11 或更高版本（本次使用 Python 3.13 x64 验证）。
- 建议 8 GB 及以上内存。
- CPU 运行 OCR，不要求 NVIDIA 显卡。
- 能访问所选服务商的 API。根据你的网络环境，可能需要代理或 VPN。

## 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

也可以安装为包后使用命令行入口：

```powershell
python -m pip install -e .
paper-translator
```

`requirements.txt` 固定了本次验证的版本，并包含用于打包的 PyInstaller。只运行源码时，也可以用 `pip install -e .` 安装运行时依赖。

## 使用步骤

1. 打开 **03 接口与规则**，选择 DeepSeek、Xiaomi MiMo 或“自定义”。填写自己的 API Key，点击 **测试连接**。只有模型支持图片时才勾选“此模型支持图片输入”。
2. 点击 **添加 PDF** 或直接拖入文件。选择原文/目标语言、页码范围和输出目录。
3. 点击 **分析版面 / 估算费用**，检查识别出的段落、整图和公式区域。
4. 点击 **开始 / 继续翻译**。任务会保存进度；取消后可以继续。排版步骤会显示已完成量，某个步骤 60 秒没有新进展时会提示“暂无新进展”。
5. 打开 **02 校对与预览**，对照原文和译文。可以修改文字、修正图内标签、框选补充 OCR 区域，然后导出校对后的 PDF。

输出文件名为 `文献名.标识.译文.pdf`，原 PDF 不会被覆盖。只有打开对应选项时，才会在 PDF 旁另外生成 `.ptrans.json` 和 `.report.json`。

### 导出选项

- **PDF 显示待校对批注（黄色图标）**：默认关闭。打开时，在需要人工检查的区域添加黄色提示；关闭时仍在校对界面和内部记录中保留问题。
- **同时导出 JSON（校对项目和报告）**：默认关闭。打开时，在 PDF 旁生成可转移的项目文件和报告，便于换电脑后继续校对。

两项都只影响重新导出，不需要重新请求翻译 API。关闭某项不会自动删除旧版已经生成的文件。

### 排版长时间不前进

先使用 **取消任务** 协作取消。排版底层调用一直不返回时，可以点击 **强制停止排版** 并确认。程序只停止自己的工作进程；已保存的译文、原 PDF 和之前成功导出的 PDF 会保留，未完成的本次排版会被放弃，可以重新导出。不要同时运行两个版本写同一份任务。

## 实测参考

一次真实运行，14 页英文论文（含图、表）：

| 项目 | 数值 |
|---|---|
| 质量设置 | 质量优先选项全开：四方向识别、整图翻译、视觉核对 |
| 模型 | DeepSeek `deepseek-flash` |
| 网络 | 代理（VPN）环境 |
| 耗时 | 约 34 分钟 |
| 费用 | **实际账单 ¥1.04**（DeepSeek 错峰价） |

这是单次运行，不是基准测试。耗时和费用会随页数、图片密度、重试次数、网络状况和服务商价格变化。该次运行仍产生待校对项：本程序面向人工校对，不承诺无人值守、全文无错误。

## 实现方式

1. **提取**：PyMuPDF 读取文字片段、图像、矢量图形和页面几何；按字体、行距和栏位组合段落与整图，识别公式和不可翻译内容。
2. **保护**：数值、公式、URL、DOI、引用编号替换为占位符；作者、单位、符号等也可由规则保护。
3. **翻译**：段落和整图上下文发送到流式 Chat Completions 接口。空译文、缺字段或占位符错误按区域重试；仍失败时保留原文并标记待校对。
4. **排版**：译文写回原区域，嵌入字体，重新换行并在最低字号限制内缩小；放不下时不静默截断。
5. **校对**：低置信度 OCR、溢出、重叠、公式冲突和图像修补不确定等情况记录在项目和校对界面中。

## 限制

- 自动段落、整图和公式分类是启发式的。双栏、跨页段落、密集子图和特殊排版仍需人工检查。
- 极小文字、弯曲文字、竖排、文字与曲线粘连、严重模糊扫描件可能漏识别或替换不完美。
- 放不下、与公式重叠或 OCR 置信度低的区域会保留原文并标出；隐藏黄色批注不代表问题已经解决。
- 不承诺原字体、行数或每个字的坐标完全不变。
- 仅面向 Windows；程序未做商业代码签名，也未在每台 Windows 设备、DPI 设置和驱动组合上验证。
- 翻译质量、价格和可用性取决于你配置的外部服务商。

## 隐私与数据

- 软件无遥测、无自动上传服务器；解析和本地 OCR 在本机执行。
- 点击翻译后，文字会发送到你配置的服务商；开启视觉核对或整图翻译时，还会发送页面截图或局部图片。
- API Key 默认只驻留本次会话。勾选“记住密钥”后，使用 Windows DPAPI 按当前用户加密保存在 `%APPDATA%\PaperTranslator\settings.json`。
- 项目、报告和内部缓存保存在本机，可能包含原文和译文；分享前请自行判断。
- 不要用它处理你不允许发送给第三方 API 的稿件。

## 构建 Windows 程序

```powershell
python scripts\fetch_sources.py   # 下载并校验第三方源码归档
python scripts\build.py           # 目录版、单文件版、源码 ZIP、许可材料
```

如果 `vendor-sources/` 缺失或校验失败，构建会直接拒绝打包。这是有意设计：发布的可执行文件包含 PyMuPDF/MuPDF，必须同时提供对应源码和声明。验证记录见 `TESTING.md`，默认决定见 `DEFAULTS.md`。

## 许可

本项目按 **GNU Affero General Public License v3.0 only**（`AGPL-3.0-only`）提供。

- 版权：Copyright (C) 2026 foogtil。
- 全文：[LICENSE](LICENSE)；补充声明：[NOTICE](NOTICE)。
- 第三方组件及许可：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 `licenses/`。
- PyMuPDF / MuPDF 由 Artifex 以 AGPL-3.0 或商业许可双许可发布。本项目使用 AGPL-3.0 方案；如需非 AGPL 分发，应取得 PyMuPDF 商业许可或替换 PyMuPDF。
- 如果你分发构建出的可执行文件，必须同时提供对应源码（包括准确版本的 PyMuPDF/MuPDF 源码）和第三方声明。Release 中的 `vendor-sources/` 归档是构建脚本使用的、经过校验的源码集合。

本程序按“现状”提供，不附带任何明示或暗示担保，包括适销性和特定用途适用性担保。详见 AGPL 全文。

## 贡献

欢迎提交 Issue 和 Pull Request。提交贡献即表示你同意其按 `AGPL-3.0-only` 分发。

提交 PR 前请运行：

```powershell
python -m unittest discover -s tests -v
```

## 致谢

PyMuPDF / MuPDF（Artifex）、RapidOCR 与 PaddleOCR、ONNX Runtime、OpenCV、NumPy、Pillow、HTTPX、TkinterDnD2 / tkdnd、PyInstaller。完整列表和许可位置见 `THIRD_PARTY_NOTICES.md`。

DeepSeek 和小米 MiMo 是外部服务，与本项目没有赞助或背书关系。

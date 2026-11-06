# 验证记录与边界

自动回归使用自建的合成 PDF，不包含真实 API 密钥；另对用户文献做只读结构检查。验证日期：2026-10-16 至 2026-10-17。

自动测试覆盖页码解析、数值 / 公式 / 引用占位符完整性、段落 ID 对应、鉴权头、SSE 响应、取消、缓存复用和模型变化、原文哈希变化，以及导出溢出时保留原文。

PDF 集成检查包含双栏正文、表格、公式、图形线条、超链接、书签、90/180/270 度标签、斜向标签、页面旋转和 CropBox。图像 OCR 使用实际捆绑的 ONNX 模型，接口翻译使用模拟传输返回明确的测试文字。GUI 检查创建实际 Tk 窗口、载入预览及拖放组件。

对应机器可运行：

```powershell
PaperTranslator.exe --self-test selftest-report.json
```

成功时报告 `ok: true`。即使报告成功，`raster_unresolved` 中仍可能包含低置信度区域，它验证的是正确标出问题，不代表 OCR 结果全部可靠。

0.1.0 的本机结果：17 项源码回归测试通过；目录版与单文件版分别以清空 Python 搜索环境路径的子进程进行自检，均返回 `ok: true`，各包含 15 项组件与 PDF 检查。Windows 高缩放界面检查后加入自适应窗口和滚动区域，并固定任务操作栏。独立 Windows 设备上的测试尚未进行。

0.1.1 修复验证增加：批次中一条空译文时其余结果先保存、仅重试空 ID、重试次数上限、取消时保留部分成功、占位符错误局部重试、网络失败不触发批次递归、缺失/空/null/重复 ID 区分，以及明确保留原文时允许空译文字段。新增字体切换正文合并、连字还原、表格单元格分离、数学字体保护及旧缓存进度保持测试。合计 29 项源码回归测试全部通过。新版目录版与单文件版分别在清空 Python 搜索环境路径的子进程中自检，均以退出码 0 返回 `ok: true`，各 16 项检查全部通过，包含新增的局部重试检查。本机报告位于 `test-output/v0.1.1-portable/report.json` 和 `test-output/v0.1.1-single/report.json`，不含真实 API 响应。

对本机 `一份 14 页示例论文` 的只读离线检查显示，新分块产生 1,350 个可翻译区域；原可提取文字中的 70,866 个非空白字符均被映射。该检查不调用 AI，也不改写原 PDF 或原任务缓存，不能代替真实翻译和排版质量验收。

0.2.0 验证：42 项源码回归测试全部通过。新增覆盖完整段落、缩进分段、双栏分离、小页眉与大标题分离、跨 PDF 对象连续段落、段内公式占位符及原矢量公式、公式线条随排版移动且保留图表线、公式缺失时保留整段原文、整图与图题分组、30 个标签仍为同一整图请求、整图输入不支持时的文字上下文回退、扫描 OCR 关闭后仍执行图内 OCR、OCR 正文行合并，以及整页扫描不误当整图。旧版把数学字体全部拆开的测试已改为验证它们作为原公式对象内嵌在整段中。

0.2.0 的目录版和单文件版各 19 项打包检查全部通过，均退出码 0、`ok: true`，包含段落与整图树、整图选中总览、段内矢量公式、链接、OCR 与拖放。检查使用清空 Python 搜索环境的子进程；报告分别位于 `test-output/v0.2.0-portable/report.json` 和 `test-output/v0.2.0-single/report.json`。打包检查中发现的整图总览预览错误已修复并复检通过。两个 exe 中的全部应用模块均经字节码核对，与当前源码一致。

本机该论文第 2–3、5、9–10 页进行了离线图内 OCR 与分组检查，检测到第 1–4 张图，并归入其标签与图题。正文和公式边界会随 PDF 排版特征变化，自动结果仍需人工查看。上述检查未使用付费 API，不代表文献的真实 AI 翻译已完成。0.1.1 的 1,350 区域统计仅为旧版记录，不作为新版段落数量。

`scripts/make_structure_demo.py` 生成的“段落与整图示例”采用预先编写的完整段落及图内标签译文，展示中文段落、段内公式、整图曲线、数值、横竖标签与图题。示例标明离线排版演示，输出报告无待校对项，已渲染查看。它不评估真实模型的术语和翻译质量。

0.2.1 编码修复：使用含中文的模拟密钥复现了截图中完全相同的 `ascii`、`position 228-229` 异常；调用栈位于 HTTPX 请求头构造阶段，模拟网络传输未被调用。50 项源码回归测试全部通过，新增检查非 ASCII / 全角 / 不可见字符、内部空白及控制字符、粘贴 Bearer 前缀、非法鉴权头、首尾普通空白、MiMo 请求头、中文正文与模型字段的 UTF-8 JSON，以及界面在分析和后台任务启动前阻止无效鉴权。错误提示不打印密钥。真实用户密钥是否混入字符仍需在设置中核查；本次未读取、解密、修改或发送用户真实密钥，也未调用付费 API。

0.2.1 目录版与单文件版各 20 项自检全部通过，退出码均为 0，`ok: true`；新增打包鉴权校验检查。报告分别为 `test-output/v0.2.1-portable/report.json`、`test-output/v0.2.1-single/report.json`。两版 exe 的全部应用模块经字节码核对，均与当前源码一致。没有在独立的未安装 Python 电脑上执行测试。

0.2.2 界面与稳定性修复：全部 10 个复选框使用自定义“√”图形，实际 Tk 控件的点击切换与恢复均经检查；顶部移除“计算机 · 数学”。PDF / OCR / 翻译 / 导出从界面中的后台线程迁移到独立工作进程，主窗口通过 IPC 收取消息；结束后关闭进程、队列与句柄，关闭窗口前取消其定时回调。预览复用左右两张当前页图片，选择区域仅绘制边框；翻页 / 缩放 / 输出文件改变时才更新图片，并限制单张预览的像素和边长。日志总量和单条长度受限，消息处理每轮最多 100 条 / 约 20 毫秒，让出时间处理 Windows 绘制和输入。

本轮保留原 50 项源码测试，新增 6 项工作进程 / 消息回归，均通过；覆盖 250 条消息积压时主动让出、任务开始前取消、IPC 日志隐藏模拟密钥、单文件失败后继续队列、导出失败保存项目，以及翻译后沿用原任务标识自动导出。源码离线自检共 27 项通过，包括实际 spawn 子进程分析、取消、导出和界面心跳；不调用付费 API。

`scripts/check_gui_stability.py` 使用生成的 4 页与 40 页 PDF、独立测试设置，创建实际可见 Tk 窗口，执行 1500 次区域选择 / 翻页 / 缩放 / 标签切换并记录资源；另通过 App 的实际 IPC 路径执行 40 页分析与排版、取消以及运行中关闭。最终复检中，第 250–1500 次操作的 Tk 图片数量恒为 10（含系统图标与复选框），进程句柄从 300 降至 298，GDI 对象恒为 66。界面心跳 3065 次，最大间隔约 0.243 秒，没有 Tk 回调异常，关闭后无遗留定时回调报错。报告为 `test-output/v0.2.2-stability/report.json`。此为本机连续操作与资源稳定性检查，不是多小时、多设备或驱动兼容性保证；截图本身不足以证明所有黑块现象只有一种根因。

0.2.2 目录版与单文件版各 27 项自检全部通过，均退出码 0、`ok: true`。检查清空 Python 搜索路径，覆盖离线 OCR、Tk / 拖放、10 个复选框状态、标题、预览复用、日志上限和冻结 EXE 的 spawn 分析 / 取消 / 导出。报告分别为 `test-output/v0.2.2-portable/report.json` 与 `test-output/v0.2.2-single/report.json`；全部应用模块（含工作进程与界面检查模块）经字节码核对，与交付源码一致。未在独立的未安装 Python 电脑上执行测试。

0.2.3 排版性能：对一份已保存的 14 页任务作只读检查，307 个区域为 translated、982 个区域为 preserved。将其复制到本机测试目录后离线导出，不调用任何 API。旧算法诊断在 20 / 40 / 60 秒等采样中持续位于 `add_redact_annot → JM_add_annot_id`，即为每字符创建批注并重复扫描已有名称；仅诊断副本被停止，正在运行的旧程序和原项目没有被终止或改写。新版每批最多 256 个独立字形 QuadPoints，保留字形中心坐标和空隙，不扩大为段落整块删除。

新增 4 项排版回归：批量与逐字符处理在普通页、CropBox、90/270 度文字场景中，提取文字和渲染像素完全一致；删除两端字符时保留中间 KEEP；5000 个字形区域只创建 20 个批注；按批取消；以及完整分阶段进度和临时文件登记。另检查导出完成后不覆盖下一篇分析状态。连续操作测试发现并复现了 Windows 下主窗口读取项目时阻止后台原子替换的 WinError 5；项目读取现改为先释放句柄再解析，保存遇到短暂的共享 / 访问冲突时最多尝试 8 次、等待总计约 0.81 秒；持续失败时保留旧缓存并报错。新增短暂占用后保存成功、持续占用时有限重试且旧记录不变的 2 项测试。源码 GUI / 组件自检 30 项通过，新增排版阶段事件、计数条 / 61 秒无新进展提示，以及模拟底层保存调用不返回时强制结束子进程、回收通信端、清理其临时文件、保留缓存及既有输出的检查。强制停止测试为明确的离线故障注入，不代表真的等待了 60 秒或发生真实死锁。

该 14 页任务使用已有译文的最终离线导出本机耗时约 28.2 秒（含第 3 / 9 页预览渲染），生成 2195 条处理进度、268 个写回区域，报告仍有 182 个待校对项。第 3、9 页已渲染查看，包含公式、曲线图与标签；可见局部公式 / 正文及残留原文问题，输出不作为已完成全文质量验收的译稿。该项只验证排版能结束及标出问题，不重新评价模型译文，也不保证其他 PDF 的耗时。原有任务记录、原 PDF 和真实 API 密钥均未改写。真实任务输出及诊断日志在本机 `test-output/v0.2.3-diagnosis/`，不进入公开发布包。

0.2.3 最终源码回归 63 项全部通过。Windows 文件占用修复后，连续 3 次由实际 App 进程通信发起 40 页导出均成功、无错误。1500 次可见窗口连续操作与 40 页分析 / 导出 / 取消 / 关闭再次通过：第 250–1500 次 GDI 对象恒为 66、进程句柄恒为 325、Tk 图片恒为 10；心跳 1574 次，最大间隔约 0.122 秒，无 Tk 回调错误。报告为 `test-output/v0.2.3-stability/report.json`。

0.2.3 目录版、单文件版均在清空 Python 搜索环境路径后以退出码 0 返回 `ok: true`，各 30 项打包自检全部通过，包含独立进程排版、阶段进度、超时提示与强制停止后的缓存 / 临时文件处理。报告为 `test-output/v0.2.3-portable/report.json` 和 `test-output/v0.2.3-single/report.json`。两版 exe 中全部应用模块经字节码核对，与最终源码一致。

0.2.4 导出选项：67 项源码回归全部通过。新增覆盖黄色批注 / JSON 的四种组合、保留作者批注、JSON 项目重新载入、旧项目复制到内部且原文件不变、输出目录不同则缓存不同，以及使用已保存译文的队列自动导出只产生一个 PDF 且不调用 API。旧 Options 缺少两个字段时默认关闭，导出开关变化不改变翻译指纹。

源码组件与实际 Tk / 工作进程自检 35 项全部通过，包含全部 14 个复选框、顶部开源文案、服务商候选切换及各自手填模型保存、导出偏好持久化、打开项目不重置偏好、默认 PDF 单独导出，以及实际子进程开启批注与双 JSON 导出。报告为 `test-output/v0.2.4-source/report.json`。自检始终使用隔离设置和模拟密钥。

使用该 14 页文献的已有译文副本，关闭批注及 JSON 后离线导出耗时约 28.8 秒，输出目录仅一个 PDF，程序黄色待校对批注数为 0；内部报告仍保留 182 个待校对项。第 2 页已渲染查看，截图中的黄色图标消失；图内仍有原文，隐藏提示不代表全文质量已经验收。输出位于本机 `test-output/v0.2.4-clean-example/`，不进入公开包，未调用任何 API、未改写原 PDF 或原任务。

0.2.4 可见窗口 1500 次连续操作及 40 页分析 / 导出 / 取消 / 关闭全部通过，未出现 Tk 回调错误。第 250–1500 次采样的 GDI 对象恒为 68、用户对象恒为 168、进程句柄恒为 326、Tk 图片恒为 10；界面心跳 1771 次，最大间隔约 0.158 秒。报告为 `test-output/v0.2.4-stability/report.json`。这是本机连续操作验证，不等同于多小时、多设备验证。

0.2.4 目录版与单文件版各 35 项打包自检全部通过，退出码均为 0、`ok: true`。检查清空外部 Python 环境路径，覆盖离线 OCR、模型候选、导出偏好、单独 PDF、批注和双 JSON 开关以及实际冻结子进程的分析 / 导出 / 取消 / 强制停止。报告为 `test-output/v0.2.4-portable/report.json` 和 `test-output/v0.2.4-single/report.json`；两个 exe 的全部应用模块均经字节码核对，与当前源码一致。未在独立的未安装 Python 电脑上测试。

0.2.4 发布前整理：根目录 `LICENSE` 补齐为依赖源码包中提供的完整 AGPL v3 文本（34,520 字节），构建脚本不再用依赖库的简短 COPYING 声明覆盖项目许可。重新打包后核对根目录、目录版内部和单文件版内部的 LICENSE 字节一致，两版应用代码仍与源码一致；两版各 35 项自检再次全部通过。`release/` 统一放置当前版单文件 EXE、便携 ZIP、源码 ZIP 和 SHA-256 清单；Git 忽略发布二进制、构建缓存及已随便携包提供的依赖源码大归档，保留依赖来源清单和获取脚本。

发布整理时清除了旧版本、虚拟环境、构建目录和中间测试输出，保留当前源码、测试代码、依赖材料与本记录。上文各 `test-output/` 路径记录的是检查发生时的位置，原始测试产物不再随当前文件夹保留。

1.0.0 界面语言与仓库链接：73 项源码回归全部通过。新增覆盖原文语言列表与全部 UI 语言的对应、各语言文案完整性和格式占位符、系统语言解析及未知语言回退、翻译语言和服务商选择保持内部原值、动态提示翻译、原文路径不被本地化，以及语言偏好只修改 `ui_language` 而不解密或重写加密密钥。初次全套运行有一次测试夹具直接写入临时 JSON 时发生 OSError 22，单独重跑该模块及最终全套均通过，未改写应用任务保存逻辑。

源码自检 40 项通过，新增 GitHub 点击目标、简体中文默认值、控件位于副标题下方、语言偏好与任务独立保存，以及 12 种语言加“跟随系统”共 13 个选项分别启动实际 Tk 窗口；核对导航和按钮翻译、原文编辑内容原样保留、自定义服务商内部标识正确。自检使用隔离设置、模拟密钥和合成 PDF，不访问付费 API。报告记录在 `test-output/v1.0.0-source/report.json`。

`scripts/check_localized_ui.py` 在 1050×740 的最小测试窗口下检查全部 13 个选项，语言控件和开源链接均位于可见边界内，无 Tk 回调异常；生成了简中、英语、德语、阿拉伯语、印地语截图。简中、英语、阿拉伯语截图已检查，窗口内原生文字可显示。截图用 HWND 捕获物理像素，避免 Windows DPI 虚拟化导致截图裁切。报告为 `test-output/v1.0.0-languages/report.json`。语言包未经过全部语种母语使用者校对；外部返回和部分诊断日志按原文显示。

1.0.0 的 1500 次可见窗口连续操作和实际 40 页分析 / 导出 / 取消 / 关闭检查通过，无回调错误。第 250–1500 次采样 GDI 对象恒为 67、用户对象恒为 171、进程句柄恒为 324、Tk 图片恒为 10。心跳 2193 次，最大间隔约 0.158 秒。报告为 `test-output/v1.0.0-stability/report.json`；不代替多小时、多设备验证。

1.0.0 目录版、单文件版在移除外部 Python 环境路径后各 40 项自检全部通过，退出码均为 0，`ok: true`。包含 13 个语言选项实际启动、语言偏好隔离保存、文献内容保留、GitHub 链接目标、离线 OCR、实际冻结子进程分析 / 导出 / 取消及强制停止。两个 exe 全部应用模块的字节码均与当前源码一致；两版内置 `locales.tsv` 与源码逐字节一致，单文件内完整 LICENSE 也与根目录一致。报告记录为 `test-output/v1.0.0-portable/report.json` 和 `test-output/v1.0.0-single/report.json`。

源码测试报告、打包自检报告和本机视觉检查结果由交付时写入此文档或随包报告。未完成的验证包括：

- 使用用户实际 DeepSeek / MiMo 账户完成端到端翻译及图片请求。
- 真实计算机与数学论文的术语、证明、公式分类与参考文献质量。
- 密集曲线图、小字号、弯曲标注、模糊扫描件及复杂竖排的自动替换正确性。
- 没有安装 Python 的独立 Windows 10 / 11 电脑、不同 DPI 与硬件的测试。

以上仍需在真实账户、代表性论文和独立 Windows 设备上验收；软件没有宣称对任意 PDF 做到全自动、无错误、逐字位置完全一致。

## 1.1.0 Token optimization verification — 2026-10-28

- 90 offline unit tests passed, including 17 new context/usage/retry regression tests.
- Source self-test: all 44 checks passed, including OCR, GUI/worker events, persistence, original PDF geometry, and the new context/usage checks. Raster fixture still flags two regions for review, as before; this is not a claim of perfect OCR.
- Synthetic full-vs-compact comparison: 216 translated IDs and 30 requests in both modes; input text 741038 -> 309984 characters (58.17% lower). This is a serialized-text measurement, not measured provider tokens, fees, or real translation quality.
- Reports: `test-output/token-optimization/`. All API tests use offline transports and isolated settings; no user API keys or AppData jobs are accessed.
- New options and usage messages are covered in all existing UI languages.
- Frozen EXE validation and retained-version integrity results are recorded below after packaging.

### Concurrency follow-up

- 98 unit tests passed, including 8 concurrency tests: identical serial/parallel payloads, bounds, per-thread retry state, cancellation, retained results, atomic saves, usage sums, serialized IPC, and PDF crops on the owner thread.
- Source self-test before the additional frozen HTTP concurrency probe: all 45 checks passed. Final packaged self-tests include that additional probe (46 checks).
- Simulated 30-response latency: 1.271 seconds with 1 worker, 0.643 seconds with 2 workers, unchanged inputs and requested IDs. This is not a live-service speed guarantee.
- GUI stability run before the concurrency selector: 1500 operations and a 40-page fixture export passed; GDI 64, user handles 165, process handles 274, Tk images 10 stayed stable, no callbacks failed.
- New context control fitted all 13 UI choices at 1050×740. Usage remains visible in the fixed queue footer; settings hints wrap in equal-width columns.
- Other-version integrity: all 1602 protected files matched their pre-edit hashes.

### Final 1.1.0 distribution

- Standalone and directory executables each passed all 46 offline checks with external Python paths removed. Exit code 0 for both. Reports: `test-output/v1.1.0-single/report.json` and `test-output/v1.1.0-portable/report.json`. The frozen probes include two simultaneous HTTP requests with correct ID mapping and all real spawned-worker paths.
- Both executables contain application bytecode matching every current Python source module. The standalone locale catalog and LICENSE match source bytes. No settings.json, .env, saved private projects or logs are bundled.
- At 1050×740, usage and the concurrency control fitted all 13 UI choices, with default concurrency 2. Report: `test-output/token-optimization/ui-v110/report.json`.
- Original 1.0.0 was restored from the pre-edit backup and verified against 1427 archive/hash entries. Original 0.2.4 files remain unchanged. New builds now also retain a versioned standalone EXE in release/.

## 1.1.0 layout revision — 2026-10-30

- 110 offline unit tests passed. The 12 new layout tests cover split hats/scripts, fractional formula dimensions, baseline alignment, picture exclusion, complete wrapped text, 90/270-degree pages, local rotation transforms, formula labels inside figures, overflow, cancellation, stale saved drafts after a failed retry, and visible collisions. The existing header/title separation regression also passes.
- Source GUI/OCR/worker self-test passed all 52 checks. Six new probes run inside both source and packaged self-tests: whole formula grouping, vector geometry/baselines, photo pixels, complete wrapped text, stale-draft retention, and overflow retention.
- Real-document offline checks: the selected ARIMA paper pages 8–9 exported 18 prepared translation regions in approximately 3.7 seconds; two labels touching underbraces safely retained the original text and remained flagged for review. Human-prepared Chinese text and current formula markers were used as layout input, not a live model-quality evaluation.
- The selected author-biography page exported 17 regions in approximately 1.3 seconds. All five photo rectangles rendered identically to the source at 144 DPI. Existing translated text was reused for this layout test; no paid API request was made.
- Those three output pages were rendered and visually reviewed. Native formula content remains vector-based. A broad OCR repair box crossing a picture boundary now retains the original image rather than inpainting the photograph.
- Tests use isolated settings and synthetic jobs. Real source PDFs, existing user output PDFs and AppData jobs/settings are not modified. Private document diagnostics stay under `test-output/layout-fix-20261030/`, outside distribution whitelists.
- Both revised 1.1.0 executables passed all 52 packaged self-tests with external Python search paths removed, exit code 0. This includes the six new layout probes and real spawned-worker, OCR and multilingual GUI checks. Reports: `test-output/layout-fix-20261030/single/report.json` and `test-output/layout-fix-20261030/portable/report.json`.
- Every application module in both executables matches the current Python source bytecode. This is a local packaging check, not a test on a separate computer without Python installed.

## 1.1.0 five review fixes — 2026-10-30

- Added 12 regression tests covering version-3/4 manual source corrections, direction/vertical/keep flags, changed paragraph boundaries without duplicated glyphs, page-range expansion/shrink/re-expansion, retained usage, selected-page-only requests, reanalysis reuse, and continued language-change invalidation.
- Final full suite: all 122 unit tests passed in approximately 21.9 seconds.
- Formula checks cover ordinary-font vector objects, rejected simulated operator changes, saved/manual formula validation, natural-language compound and metric labels, Chinese text adjacent to algebra, invalid/extended expressions, vertical vector formulas, missing vertical markers, and recomputed overflow warnings while retaining OCR notes.
- The source self-test passed all 58 checks. Six additional packaged probes cover ordinary-font formula extraction and request protection, vertical formulas, manual-upgrade retention, retained page-range work/usage, and resolved overflow notes.
- A fresh 1500-operation GUI run plus 40-page analysis/export/cancel/close passed. GDI 64, USER 168, handles 274 and Tk images 10 stayed constant in iterations 250–1500. No callbacks failed; maximum heartbeat gap was approximately 0.230 seconds. This is not a multi-hour or multi-device test.
- The user's selected pages were re-exported offline: the formula pages retained 18 prepared regions with two underbrace labels still safely left for review; all five photo rectangles remained pixel-identical at 144 DPI. No source document, user output, AppData task or real API key was modified.
- Evidence is under `test-output/five-fixes-20261030/`, outside release whitelists. All API behavior tests use offline simulated responses.
- Final standalone and portable executables each passed all 58 packaged self-tests with external Python search paths removed, exit code 0. Both executables' application modules match the final source bytecode. Reports: `test-output/five-fixes-20261030/single/report.json` and `test-output/five-fixes-20261030/portable/report.json`.

## 1.1.0 font and table revision — 2026-10-31

- All 135 unit tests passed (approximately 25.1 seconds). Thirteen new tests cover the reproduced short-ascent font erasure failure, baseline-based erasure in cardinal directions/cropped pages, unchanged neighbours, pre-overlay rejection of incomplete or unintended deletion, numeric table positions and neighbouring prose, version-5 cache migration, DOI retemplating, short title tails, dense-cell margins, exact spatial collision queries, and high-zoom raster erasure.
- Source, standalone and portable self-tests each passed all 63 checks. Five added frozen checks cover the nonstandard font fixture, numeric table extraction, adjacent prose, DOI recognition, and raster repair without gray glyph outlines at 600 DPI while retaining an adjacent axis. Packaged tests removed external Python search paths; this remains a local test, not a separate-device compatibility claim.
- Every application Python module in both executables matches current source bytecode. The custom metric-font fixture uses original rectangle glyphs; its optional regeneration script is included. No third-party paper is bundled as a fixture.
- The user's full 17-page sample was re-exported offline from an isolated cache copy. Existing translations were reused; the newly separated title/table text was reviewed locally. No paid API calls or saved keys were used, and the original PDF, original translated PDF and actual AppData task were not overwritten.
- The sample's 313 modified regions were checked with rendered pages. Dense/complex regions that still cannot fit safely retain their original text for review. This is a layout repair, not a claim that all model wording and all OCR labels have been semantically verified.
- Public packages exclude `test-output/` and the private repaired-PDF folder. The detailed local evidence is under `test-output/disease-20261031/`.
# 1.1.0 workflow revision — 2026-11-02

- All 149 unit tests passed in 34.317 seconds. Fourteen added tests cover task identity across output directories, explicitly opened project continuation, old-cache discovery, wrong-project rejection, interprocess ownership and forced-process-exit release, stale editor protection, unique atomic temporary files, source updates with preserved history, changed export ranges, unanalyzed export pages, disabled cached figure labels and OCR filtering, configured read timeouts without automatic resubmission, and real socket cancellation/reconnection.
- Source, standalone and portable self-tests each passed all 72 checks. The nine added packaged checks include actual process locks and a temporary HTTP server bound to 127.0.0.1; no external provider or real API key is used. Packaged checks remove external Python search paths.
- Both executable archives contain application bytecode matching all current source modules, including the asynchronous socket bridge and task-lock implementation.
- The actual GUI continuation path preserved the manually corrected paragraph and requested only the pending paragraph. The earlier audit probe now retains both cached translations after changing output directories, sends no disabled image labels after actual OCR, and makes one request on a simulated read timeout.
- 1,500 GUI operations plus a 40-page analysis/export/cancel/close run passed. From iteration 250 through 1,500, GDI objects stayed at 64, user objects at 168, handles at 274 and Tk images at 10. No callback errors; largest heartbeat gap approximately 0.159 seconds. This is not a multi-hour or separate-device guarantee.
- Evidence: `test-output/fix-20261102/`, excluded from public source and executable packages. The previous diagnostic report is retained separately under `test-output/audit-20261102/`.


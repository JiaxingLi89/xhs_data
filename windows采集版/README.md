# Windows 独立采集版

将整个文件夹复制到 Windows 11 或以上的 64 位电脑，例如 `C:\研究资料\windows采集版`。本包从零开始，首次需要联网安装依赖并扫码登录。Mac 的数据、登录会话和虚拟环境没有复制过来。

本包的 Windows 实机兼容性及平台接口尚待验证；在 Mac 上通过离线测试不代表 Windows 实采已经成功。Windows 10 不在本次支持范围。

## 第一次使用

1. 安装 **64 位 Python 3.12**：[Python 官方下载](https://www.python.org/downloads/windows/)。安装时勾选 Python Launcher 和 Add Python to PATH。
2. 准备已安装的 Microsoft Edge 或 [Google Chrome](https://www.google.com/chrome/)，使用普通桌面用户登录 Windows。
3. 双击 `01_install.cmd`，直接回车或输入 `1` 使用清华镜像，输入 `2` 使用官方源，输入 `0` 取消。脚本只创建本文件夹内的 `.venv`，不需要管理员运行。安装失败与后续环境检查失败会分开提示。
4. 安装完成后，如需改用 Edge，先双击 `08_browser.cmd` 选择浏览器，再双击 `02_check.cmd`：`live_ready: true`、`issues: []` 表示环境检查通过，不代表已经成功采到数据。
5. 可先双击 `03_demo.cmd`。这会生成模拟数据，终端给出 Excel 路径；`manifest.json` 中 `synthetic_demo: true`，不能用于真实研究。
6. 双击 `04_login.cmd`，在打开的浏览器里扫码。成功后自动开始采集，无需同时启动其他采集窗口。
7. 任务结束后双击 `07_export.cmd`，按终端路径打开 `research.xlsx`，核对正文和评论。

依赖包含 Playwright、xhshow 和 Windows 时区数据 tzdata；版本范围见 `requirements-live.txt`，安装后 `requirements-resolved.txt` 记录实际解析版本。联网安装失败时保留错误信息；可在命令行用 `py -3.12 scripts\setup_live.py --wheels "C:\本地wheel目录"` 离线安装，wheel 需要匹配 Windows 和 Python 3.12，并包含全部依赖。

## 双击脚本用途

| 文件 | 功能 |
|---|---|
| `01_install.cmd` | 选择清华镜像或官方源，安装依赖并检查 |
| `02_check.cmd` | 检查系统、Python、Chrome、依赖、时区与适配器快照 |
| `03_demo.cmd` | 离线演示，不访问小红书 |
| `04_login.cmd` | 登录确认后自动采集 |
| `05_run.cmd` | 使用保存的会话继续采集 |
| `06_status.cmd` | 查看数量、配额、登录等待与暂停状态 |
| `07_export.cmd` | 导出 CSV、Excel 和 manifest |
| `08_browser.cmd` | 切换 Edge 或 Chrome，保留数据与配额 |

脚本自动进入所在目录，支持中文和空格路径；不需要激活虚拟环境。处理完成后窗口保留，按任意键关闭。中途停止采集使用 `Ctrl+C`，已提交的数据保留。不要同时双击多个采集入口。

## 安装源与浏览器切换

清华源为 `https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`，官方源为 `https://pypi.org/simple`。一次只指定一个源，不自动换源、不修改全局 pip 设置。镜像可能改善下载速度，但不保证所有网络下都能安装，版本也可能存在同步延迟。[镜像官方说明](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)

在文件夹地址栏输入 `cmd` 后，可任选一条命令指定来源：

```bat
py -3.12 scripts\setup_live.py --source tuna
py -3.12 scripts\setup_live.py --source pypi
```

命令行不指定来源时默认清华镜像；`--menu` 显示菜单。离线 `--wheels` 模式不显示菜单、不访问安装源，不能同时指定 `--source`。

若提示“依赖已安装，但环境检查未通过”，先看具体原因。电脑只有 Edge 时，双击 `08_browser.cmd` 切到 Edge，再运行 `02_check.cmd`，无需重复下载依赖。

浏览器菜单显示当前浏览器和安装检测结果：`1` 选择 Edge，`2` 选择 Chrome，`0` 或回车取消。未检测到浏览器时不会修改配置。切换前先结束正在运行的采集任务；程序运行中切换会被拒绝。

Chrome 沿用 `data/browser-profile`，Edge 使用 `data/browser-profile-msedge`。首次使用另一个浏览器可能需要扫码，切回后可复用各自会话。数据库、关键词、阶段、当天配额和暂停状态不变。自定义 `data_dir` 时，会话目录跟随该配置。

如果旧版曾手动让 Edge 或 Chromium 使用共用目录，本次不会自动识别或搬移会话；改用独立目录后重新扫码。Chromium 仍可手动配置，使用 `browser-profile-chromium`，不列入双击菜单。

环境检查显示 `browser_channel`、检测到的 `browser_executable` 和实际 `browser_profile`；路径检测通过不等于浏览器已启动验证。

## 怎样看懂运行状态

- 浏览器是 `about:blank` 也可能正常：已有会话通过服务端确认后，后台接口采集无需打开笔记页面。
- 缺少完整凭证时跳过无效登录检查；扫码后仍需服务端确认。有 Cookie 不代表登录必然有效。
- 每次受控请求间隔 120–300 秒，终端每 30 秒报告步骤、剩余时间和完成数量；提示更新不发送额外平台请求。
- “完成笔记数”是数据库累计值。正文已保存而评论尚未结束时，完成数量可能仍为 0。
- 每日按北京时间计算，试采和正式阶段均最多 700 次受控请求，包含登录确认、搜索、正文、评论及失败尝试。700 是上限，不保证每天达到；浏览器页面资源请求另行统计，不属于此受控接口请求额度。
- 默认不限制每天或每次处理篇数，不再限制单次运行 4 小时，也不再限制每天一页搜索。请求间隔仍为 120–300 秒。
- 默认跨日继续采集，按北京时间将新请求计入新日期；跨日保留请求间隔和评论游标，不会补发积压请求。达到当天请求上限时仍保存进度并退出，由你次日双击 `05_run.cmd` 继续。同一天重启、重新登录或切换阶段都不清零已用额度。
- Windows 采集期间自动申请阻止系统自动睡眠，允许屏幕熄灭；任务结束、异常或 Ctrl+C 后释放，不修改系统电源计划。请保持接通电源，主动睡眠、合盖、关机仍可能中断任务；启用失败会在终端提示。
- 请求间隔仍为 120–300 秒，平均每小时约 17 次；700 是每日上限，不是预计完成量。正文与每页一级评论分别消耗请求，一篇评论较多的笔记会占用多次请求。
- `06_status.cmd` 增加正文保存数、评论页数/条数、评论采集中笔记，以及 `last_run`（最近运行）和 `previous_run`（上次运行）。运行记录包含开始、最后活动、结束时间和停止原因；时间为 Unix 秒。强制结束进程无法写入结束时间，下次启动采集时会把遗留运行标为 `unclean_exit`，不会猜测具体中断时刻。旧版本运行无法补出此前未记录的原因。
- 启动脚本统一为 ASCII 与 CRLF，中文结尾提示由 `scripts/launcher_message.py` 输出，避免由批处理解析器处理中文。环境缺失时使用英文安装提示，并保留失败退出码。
- 一级评论默认尽量采集至接口返回结束，不再限制每篇 50 条或 20 页。请求额度耗尽时保留游标，下次继续；不采集二级回复。
- 完整样本需要查看 `state=complete`；`comment_cap` 表示评论截断，`exhausted` 表示接口当前返回结束，不代表全部历史评论。

真实成功需要：导出至少一篇可与网页核对的真实正文，且 `synthetic_demo: false`；评论需要另核对有评论的笔记及 `note_id` 关联。仅生成空 Excel 或登录成功不足以证明采集成功。

## 文件和输出位置

`config.json` 修改关键词和限额；`xhs_research/` 为源码；`tests/` 为测试；`third_party/` 为来源和许可证。`SOURCE.json` 记录复制来源提交和当时工作文件哈希，包含未提交改进的说明。

运行后生成：`.venv/` 是本机环境；`data/` 是真实数据库、断点及浏览器会话；`exports/时间戳/` 是手动导出的真实结果；`demo_output/` 仅为模拟结果。每次导出生成新文件夹，不自动覆盖前次导出。

`research.xlsx` 有笔记、评论、来源三个工作表；同目录包含 `notes.csv`、`comments.csv`、`sources.csv`、`manifest.json`。CSV 保留完整长文本，Excel 单元格最多 32767 字符；样本属于平台返回顺序下的方便样本，不是随机或全量样本。

## 评论排序与配置

### 商业推广初筛与人工复核

双击 `09_review.cmd` 打开离线菜单：运行本地初筛、导出待复核疑似帖子、导出全部帖子，或导入复核 CSV。已有帖子需运行一次初筛；新采集的正文保存后自动初筛。不会调用外部模型或增加平台请求，不改变试采审核要求。

自动标签为“疑似商业推广”“未发现推广线索”“无法判断”。`promotion-rules-v1` 仅检查标题、正文和标签中的合作声明、赞助/赠品测评、优惠码、购买引导、私信购买、代理招募等文字线索，保存命中规则、原文片段和位置。品牌名、正面评价或点赞量不单独作为证据。否定和引用也可能命中，须结合全文复核；未命中不能证明帖子是真实分享。

复核 CSV 位于 `exports/reviews/`，默认只含疑似且未有效复核的帖子；菜单的“全部帖子”可用于检查漏判或修改已有复核。只编辑 `human_review_label` 和 `human_review_note`，保持其他列不变并保存为 UTF-8 CSV。人工标签仅允许：**明确商业推广、疑似商业推广、普通经验分享、无法判断**。空白标签不清除旧复核；若要撤销“明确商业推广”，请选择其他有效标签后重新导入。

导入遇到重复/未知笔记 ID、非法标签、正文哈希已过期或 CSV 列错误时整批不写入，并报告行号。人工修改保留历史；重跑规则不会覆盖人工复核。若正文变化，旧复核仍留存，但不再用于排除，需重新导出复核。

`07_export.cmd` 保留原有全量 CSV/Excel，并在同一导出目录新增 `without_confirmed_promotion/` 筛选版。**仅排除人工确认为“明确商业推广”且对应当前正文的帖子及关联评论、来源**；自动疑似、无法判断和未复核帖子均保留。筛选版来源只保留有导出正文的笔记，不包含待采候选来源。全量数据库不删除任何记录。是否仅分析完整采集笔记仍需另筛选 `state=complete`。

笔记导出新增 `promotion_label`、`promotion_evidence`、`classification_version`、`classification_status`、分类时间/哈希、`human_review_label`、复核说明/时间、`human_review_current` 和 `exclude_confirmed_promotion`。`manifest.json` 记录全量/筛选版计数、排除数、有效复核覆盖率和规则版本；两套数据供后续敏感性分析使用，不自动判断研究结论是否变化。

数据库升级到 schema 3；升级 schema 2 前自动生成 `research.pre-promotion-<随机标识>.sqlite3` 一致性备份。独立表 `promotion_annotations` 保存当前标注，`promotion_review_history` 保存人工变更历史。初筛失败保留正文，并标记待处理；可稍后重新初筛。升级和复核命令使用采集进程锁，运行中的旧爬虫须先结束再使用新版本。

命令行等价入口（在项目目录运行）：

```powershell
.\.venv\Scripts\python.exe -m xhs_research classify
.\.venv\Scripts\python.exe -m xhs_research review-export
.\.venv\Scripts\python.exe -m xhs_research review-export --all
.\.venv\Scripts\python.exe -m xhs_research review-import "复核CSV完整路径"
```

### 用户 ID、IP 属地和旧数据补采

- 新采集笔记的 `detail` JSON 保留 `author_user_id`、`author_ip_location`、`identity_observed_at`；评论表新增 `commenter_user_id`、`commenter_ip_location`、`identity_observed_at`。用户 ID 为原始平台字符串，支持作者与评论者之间、不同笔记之间关联。
- 作者 ID 来自笔记详情 `note_card.user.user_id`，作者属地来自 `note_card.ip_location`；评论者 ID 来自评论 `user_info.user_id`，属地来自该条评论 `ip_location`。不会用作者属地代替评论者属地，不访问用户主页。接口字段缺失、空值或类型异常时留空；仍保留正文和评论。
- 属地是平台在对应内容接口返回的原值，不等于常住省份。`identity_observed_at` 为获取新增字段的 Unix 秒时间；历史补采值不代表原始发布时间或原采集时间的属地。不新增昵称、头像、二级回复或媒体下载。
- 首次打开旧数据库时，在采集进程锁保护下升级到当前 schema 3（包含身份字段和推广标注）；schema 1 升级前以 SQLite 一致性备份生成同目录的 `research.pre-identity-<随机标识>.sqlite3`。不要使用旧版本程序继续写升级后的数据库。
- 下次启动采集时自动为旧笔记建立一次性独立补采队列，优先重新获取详情、从评论首页遍历并去重补字段。查看状态、导出只会进行必要的结构升级，不启动网络补采。补采使用当前 700 次日额度和请求间隔，支持跨日与中断续接。
- 补采保留原笔记状态、原评论游标、正文、评论内容和原采集时间；新增评论正常保存，已有非空用户 ID 和属地不被覆盖。补采使用 `identity_jobs`、`identity_cursors` 独立记录进度，不将原完成笔记改为未完成。试采满 50 篇仍可补字段，但新笔记仍需正式阶段审核。
- `06_status.cmd` 的 `identity_backfill` 显示队列状态和缺失字段数量；导出的 `manifest.json` 包含每篇补采状态和原因。`complete/exhausted` 只表示接口翻页结束，仍可能有缺失字段；已删除、链接失效、重复游标等记为 `error`，不会无限重试。未正常完成的补采保留断点，下次继续。
- CSV/Excel 同步导出新增字段，XLSX 的用户 ID 为文本单元格。CSV 用表格软件打开时，应将 ID 列按文本导入以保留长数字和前导零。导出含原始用户 ID 与 IP 属地，不含登录凭证。

在 `config.json` 修改 `comment_sort` 后，双击 `07_export.cmd` 即可按新规则生成导出，无需重新采集：

| 值 | 导出顺序 |
|---|---|
| `likes_desc` | 默认，点赞数从高到低 |
| `time_asc` | 发布时间从早到晚 |
| `time_desc` | 发布时间从晚到早 |

评论先按帖子分组，再按所选规则排序；缺失或无法解析的数值置后，相同值按评论 ID 排列。点赞按数值比较（支持万、亿、k、w），时间支持 Unix 秒、毫秒及 ISO 日期；无时区日期按配置时区解释。导出字段保留平台原值，CSV 与 Excel 顺序一致。排序只作用于已经采到的评论；采集仍按平台游标翻页，不表示平台优先返回最高赞评论。

`manifest.json` 记录排序规则及每篇帖子的评论状态。`interface_exhausted: 1` 仅表示接口当前返回结束，不能证明覆盖全部历史评论；异常中止或尚未采完的帖子继续保留状态和原因。

以下字段支持 JSON 的 `null`（不加引号）表示不限：`daily_notes_pilot`、`daily_notes_formal`、`notes_per_run`、`run_timeout_seconds`、`comments_per_note`、`max_comment_pages`。当前配置均为 `null`；已有自定义配置中的正整数仍有效。每日请求上限仍须为正整数，试采与正式阶段分别由 `daily_requests_pilot` 和 `daily_requests_formal` 设置。

升级后首次运行采集时，程序会将原来因评论条数或页数上限停止的帖子一次性加入补采队列，从第一页重新翻页并按评论 ID 去重，补回原先截断页的剩余评论。原正文、评论及当天请求计数保留；已自然结束或因其他异常停止的帖子不会被重置。状态查看和导出不会触发补采迁移。

**按历史月份采集暂未实现。** 本版仍按配置的关键词和平台排序搜索，没有提供“2021 年 1 月至今逐月搜索”或日期过滤，不以导出排序替代历史月份覆盖。候选不足时继续搜索下一页；当前搜索结束后保存进度，不自动循环重搜。连续两页没有新候选时也会停止该关键词，搜索结束不代表全部历史帖子已发现。

不要分享 `data/` 中的凭证；Windows 的文件权限由用户账户和 NTFS ACL 管理，代码中的 `chmod` 不等同于 macOS 的私有目录权限。建议保存在个人用户目录，避免共享盘或自动同步目录。

Windows 与 Mac 的配额独立，不会跨电脑共享；同一账号不要两台电脑同时运行。不要删除数据库来清除等待、暂停或配额。

## 正式采集与暂停恢复

在文件夹地址栏输入 `cmd` 回车，可在当前目录打开命令提示符。

完成 50 篇并实际核对至少 10 篇后：

```bat
.venv\Scripts\python.exe -m xhs_research promote --reviewed-notes 10
.venv\Scripts\python.exe -m xhs_research run
```

`10` 是你申报的人工核对数量，程序不会验证你是否真的完成核对。正式阶段每天最多 700 次受控请求，累计目标 1500 篇（含试采）；默认不限制每天/每次篇数及每篇评论数。阶段切换不清空数据或当天额度。已通过审核的正式任务不会因为旧评论补采而撤销审核资格。

等待登录时重新使用 `04_login.cmd`。网络中断至少退避 15 分钟；遇到平台验证、限流或协议错误先查看原因，不反复重试。人工检查、问题处理且冷却结束后才能使用：

```bat
.venv\Scripts\python.exe -m xhs_research resume
.venv\Scripts\python.exe -m xhs_research run
```

重新登录不会清除风控暂停。`protocol_error` 需要检查适配器。浏览器启动失败时检查 Chrome 安装与会话目录占用；本版明确开启沙箱，不自动关闭沙箱重试。

本版不支持定时任务；`schedule` 会明确拒绝且不生成 macOS 文件。现有命令行接口保留，自定义配置放在子命令前，例如 `.venv\Scripts\python.exe -m xhs_research --config "config.json" run`。

## 自测与待验收

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Windows 实机还需核验：中文和空格路径的双击脚本、Chrome 沙箱启动、首次扫码、再次启动复用会话、重复启动被阻止，以及至少一篇真实正文与评论导出。详见 `VALIDATION.md`。程序低频运行不保证不会触发平台风控。

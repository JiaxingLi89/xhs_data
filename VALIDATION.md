# 实施与验收记录

返回 [入门指南](README.md)。本文件区分离线验证、本机环境检查和真实采集验收。

## 本次检查：2026-11-17

- 项目 `.venv` 使用 Python 3.14.7，运行 `.venv/bin/python -m unittest discover -v`，35 项测试全部通过。
- 运行 `.venv/bin/python -m xhs_research doctor`，结果为 `core_ready: true`、`live_ready: false`，退出码 2。
- `issues` 列出 `playwright==1.61.0` 和 `xhshow==0.2.0` 未安装。此结果不能证明平台接口是否可用。
- 本次整理 README、维护文档和验收记录；没有安装依赖、登录账号、启动真实采集或启用定时任务。
- 本次没有重新尝试联网安装。下方 `Operation not permitted` 是 2026-11-13 的历史记录，不能据此断言当前网络仍不可用。

## 仍待验证

按 README 顺序完成依赖安装与环境检查、真实账号登录、笔记及评论采集和导出核对。取得可核对的真实数据后才能确认对应链路可用；再累计完成 50 篇试采、至少 10 篇人工核对，决定是否转正式阶段。

---

# 历史记录：2026-11-13

## 已验证

- Python 3.9.6、Miniconda Python 3.14.7，以及项目专用Python 3.14.7虚拟环境中，35项离线测试均通过。
- 语法编译通过。受环境限制，编译缓存使用`/private/tmp/xhs-research-pycache`，不写入受限的用户缓存目录。
- 命令行帮助、环境检查、离线演示、定时任务文件生成均已运行。
- 演示生成20条候选、5篇完整笔记、250条一级评论；受控请求尝试为22次，满足5篇/30次的试采预算。
- 演示Excel使用openpyxl实际打开，三个工作表分别读取到5、250、20行数据；ZIP与XML结构检查通过。
- 测试覆盖登录失效、断网不重登、扫码后自动续采、登录等待跨日持久化、HTTP 200风控、限流冷却、重复游标、分页事务回滚、请求签名时机和导出凭证排除。

## 已准备但未激活

- 项目`.venv`：从本机已有Miniconda Python 3.14创建，未修改全局Python环境。
- 定时任务：项目`schedule/`中的plist，默认10:00和16:00，未加载到launchd。
- 真实账号数据目录及浏览器会话：尚未登录，不含真实采集数据。

## 当时的阻塞与接续

安装脚本已实际尝试安装固定版本的Playwright和xhshow；PyPI元数据请求返回`URLError: [Errno 1] Operation not permitted`。pip报告无法取得可用版本列表，不能据此判断所需版本不存在。本机缓存中未找到这两个安装包。

在允许访问PyPI的终端中执行：

```bash
python3 scripts/setup_live.py
.venv/bin/python -m xhs_research doctor
.venv/bin/python -m xhs_research login
```

也可使用预先准备好的对应Python／macOS架构wheel目录：`python3 scripts/setup_live.py --wheels /目录`。

实采依赖安装、真实账号扫码、接口可用性、50篇试采及10篇人工核对仍未完成。离线验收与真实采集验收分别记录，未将模拟数据计入真实研究样本。


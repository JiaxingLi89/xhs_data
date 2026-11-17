# 上游来源与固定版本

`xhs_research/mediacrawler_protocol.py` 是 MediaCrawler 小红书只读接口的精简适配，
不是完整 MediaCrawler 安装。这样可以复用已审阅的请求协议，同时让本项目统一管理限速、
登录、重试和分页，避免上游批量流程或1秒重试绕过请求预算。

- 仓库：https://github.com/NanmiCoder/MediaCrawler
- 实际审阅来源：2026-11-13 的 `main` 分支网页源码。
- 相关修复提交：`d594c20c13f46c368bfec133de3a4f1a97cd4d79`。当前环境无法下载该提交完整快照，未将它冒称为本地源码的精确来源。
- 请求结构：`media_platform/xhs/client.py`
- 搜索会话编号：`media_platform/xhs/help.py`
- 签名库接口：`media_platform/xhs/playwright_sign.py`
- 原版权：`Copyright (c) 2025 relakkes@gmail.com`
- 许可：保留在 `MediaCrawler.LICENSE`，限非商业学习和研究。
- 实采依赖固定为 Playwright 1.61.0、xhshow 0.2.0；升级需重新验收，不自动更新。
- `upstream.lock.json` 记录本地适配文件哈希；它验证实现快照，不代表已通过真实账号试采。

签名算法由 xhshow 提供，本项目不自行实现。浏览器使用正常持久化配置，不轮换账号或代理。


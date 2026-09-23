# yysls-news

B站指定 UP 主动态与燕云十六声官网新闻监控，并通过 QQ 官方 Bot API v2 推送图片。

## 当前实现

- B站使用 `bilibili-api-python` 发现和去重动态，由 Playwright 截取动态详情页正文为单张长图。
- 管理页面支持 B站扫码登录，Credential 使用 Fernet 加密保存，Worker 低频检查和刷新登录态。
- 官网从公开新闻列表发现文章并保存详情 URL；推送时由 Playwright 打开详情页并截取 `#NIE-art` 原始内容容器。
- 只保存标题、来源、URL 等采集元数据，不持久化动态正文或文章 HTML。动态文字仅在采集时用于关键词筛选。
- 截图、图片上传或图片消息发送失败时，直接发送“标题已更新，点击查看：原文 URL”的纯文本消息。
- 内容和推送任务使用 SQLite Outbox 事务入库，支持游标、去重、重试和失败记录。
- 已有数据库启动时会移除旧版正文及闲置字段；官网分页误采集的待推送任务会标记失败，无关联记录的分页内容会清理。
- QQBot 使用 AppID/AppSecret 自动获取 AccessToken；图片先上传 `file_info`，再发送 `msg_type=7`。
- QQBot Gateway 监听单聊和群聊 @ 事件，支持通过一次性绑定码自动识别 `user_openid`/`group_openid`。
- 管理页面支持 UP、官网来源、QQBot 配置、自动绑定目标、群聊/单聊 OpenID、目标名称、消息模式、各来源轮询间隔、实时运行日志和历史推送记录。

## 启动

PowerShell：

```powershell
python -m venv ".venv"
& ".venv\Scripts\python.exe" -m pip install -e ".[dev]"
Copy-Item ".env.example" ".env"
& ".venv\Scripts\python.exe" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将上一步生成的值填入 `.env` 的 `APP_ENCRYPTION_KEY`，并设置至少 8 位 `ADMIN_PASSWORD`。然后用一个命令同时启动管理服务和 Worker：

```powershell
& ".venv\Scripts\yysls-news.exe" all
```

`all` 模式会在同一个进程中运行 Web 管理页面、采集轮询和推送队列，按 `Ctrl+C` 会一起停止。需要单独排查时，仍可使用 `web` 或 `worker` 模式。

打开 `http://127.0.0.1:43100/`，使用 `.env` 中的管理账号和初始密码登录。首次登录会强制修改管理密码；新密码会以不可逆哈希保存到 SQLite，后续重启仍使用新密码。

首次使用顺序：

1. 修改初始管理密码。
2. 点击“生成二维码”，使用 B站 App 扫码；页面会每 2 秒自动检查状态，登录成功后显示当前账号昵称。
3. 添加需要监控的 UID，显示名称留空即可自动获取 UP 主昵称；首次轮询只建立基线，不推送历史动态。
4. 在 QQ 开放平台配置 AppID/AppSecret。进入“推送目标”页面，选择“群聊”或“单聊”并点击“生成绑定码”。
   - 单聊：目标用户直接给机器人发送绑定码。
   - 群聊：在目标群内 @机器人后发送绑定码。
   - 绑定码为 8 位字符，只能使用一次，有效期 3 分钟；成功后会自动写入目标列表。
5. 选择图片模式。官网和 B站统一使用 Playwright 生成完整图片；Playwright 只在 Worker 处理图片时按需启动，不是独立常驻服务。

自动绑定依赖 `all` 或 `worker` 模式中的 QQ Gateway 监听。若监听状态显示异常，需要在 QQ 开放平台确认已开通单聊/群聊事件权限；也可以直接在管理页面手动录入已有的 `user_openid` 或 `group_openid`。

绑定群聊时会尝试通过 QQBot 群资料接口读取群名称；单聊名称以事件中携带的信息为准，若平台未返回昵称，页面仍会保留 OpenID，也可以在“推送目标”中手动填写显示名称。运行日志页面展示当前进程最近 500 条日志，服务重启后会重新开始记录。“推送记录”页面会持久化自动推送、历史内容测试推送和 QQBot 测试消息的成功/失败结果；自动重试会按尝试次数分别记录。

如果环境尚未安装 Chromium：

```powershell
& ".venv\Scripts\playwright.exe" install chromium --only-shell
```

## 校验

```powershell
& ".venv\Scripts\python.exe" -m pytest
& ".venv\Scripts\ruff.exe" check "src" "tests"
```

当前测试只覆盖解析、去重、加密、数据库 Outbox、Playwright 容器截图、QQBot 请求构造和管理接口，不会代替真实 B站扫码、动态详情页截图或 QQBot 发送联调。

## Linux 部署

Debian/Ubuntu 服务器可以使用 `deploy/install.sh` 完成首次安装，使用 `deploy/update.sh` 拉取仓库更新并重启服务。详细步骤见 [deploy/README.md](./deploy/README.md)。

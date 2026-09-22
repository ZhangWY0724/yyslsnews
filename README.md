# yysls-news

B站指定 UP 主动态与燕云十六声官网新闻监控，并通过 QQ 官方 Bot API v2 推送图片。

## 当前实现

- B站使用 `bilibili-api-python` 获取结构化动态，不访问 B站动态网页截图。
- 管理页面支持 B站扫码登录，Credential 使用 Fernet 加密保存，Worker 低频检查和刷新登录态。
- 官网从公开新闻列表发现文章，再由 Playwright 打开详情页并截取 `#NIE-art` 原始内容容器；失败时使用同一 Playwright 引擎渲染本地 HTML 模板。
- B站使用结构化数据套用本地 HTML 模板，再由 Playwright 截图；两类来源统一使用同一个图片渲染器。
- 内容和推送任务使用 SQLite Outbox 事务入库，支持游标、去重、重试和失败记录。
- QQBot 使用 AppID/AppSecret 自动获取 AccessToken；图片先上传 `file_info`，再发送 `msg_type=7`。
- QQBot Gateway 监听单聊和群聊 @ 事件，支持通过一次性绑定码自动识别 `user_openid`/`group_openid`。
- 管理页面支持 UP、官网来源、QQBot 配置、自动绑定目标、群聊/单聊 OpenID、消息模式和轮询间隔。

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

打开 `http://127.0.0.1:43100/`，使用 `.env` 中的管理账号登录。

首次使用顺序：

1. 点击“生成二维码”，使用 B站 App 扫码；页面会每 2 秒自动检查状态，登录成功后显示当前账号昵称。
2. 添加需要监控的 UID，显示名称留空即可自动获取 UP 主昵称；首次轮询只建立基线，不推送历史动态。
3. 在 QQ 开放平台配置 AppID/AppSecret。进入“推送目标”页面，选择“群聊”或“单聊”并点击“生成绑定码”。
   - 单聊：目标用户直接给机器人发送绑定码。
   - 群聊：在目标群内 @机器人后发送绑定码。
   - 绑定码为 8 位字符，只能使用一次，有效期 3 分钟；成功后会自动写入目标列表。
4. 选择图片模式。官网和 B站统一使用 Playwright 生成完整图片；Playwright 只在 Worker 处理图片时按需启动，不是独立常驻服务。

自动绑定依赖 `all` 或 `worker` 模式中的 QQ Gateway 监听。若监听状态显示异常，需要在 QQ 开放平台确认已开通单聊/群聊事件权限；也可以直接在管理页面手动录入已有的 `user_openid` 或 `group_openid`。

如果环境尚未安装 Chromium：

```powershell
& ".venv\Scripts\playwright.exe" install chromium --only-shell
```

## 校验

```powershell
& ".venv\Scripts\python.exe" -m pytest
& ".venv\Scripts\ruff.exe" check "src" "tests"
```

当前测试只覆盖解析、去重、加密、数据库 Outbox、Playwright 本地模板截图、QQBot 请求构造和管理接口，不会代替真实 B站扫码、官网网络访问或 QQBot 发送联调。

## Linux 部署

Debian/Ubuntu 服务器可以使用 `deploy/install.sh` 完成首次安装，使用 `deploy/update.sh` 拉取仓库更新并重启服务。详细步骤见 [deploy/README.md](./deploy/README.md)。

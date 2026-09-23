# AGENTS.md

## 项目定位

本仓库用于开发一个资讯监控与 QQ 官方机器人图片推送服务：

- 监控指定 B 站 UP 主动态。
- 监控燕云十六声官网新闻。
- 将内容转换为 Playwright 截图，并通过 QQ 官方机器人 API v2 推送图片。
- 使用 QQ 官方机器人 API v2 推送到群聊或单聊。
- 通过管理页面配置监控源、凭据、推送目标和轮询策略。

详细架构以根目录的 [方案设计.md](./方案设计.md) 为准。

## 编码前必须遵守的边界

### B站

B站动态发现和去重必须使用结构化接口；图片推送直接截取动态详情页正文，尽量生成一张长图：

```text
bilibili-api-python
    → 结构化动态数据
    → 本地解析模型
    → 动态详情页正文截图
    → Playwright 截图生成图片
    → QQBot 图片消息
```

网页 DOM 仅用于定位截图区域，不作为动态发现、正文解析或去重接口。

参考实现：

- https://github.com/Soulter/astrbot_plugin_bilibili
- https://raw.githubusercontent.com/Soulter/astrbot_plugin_bilibili/master/bili_client.py
- https://raw.githubusercontent.com/Soulter/astrbot_plugin_bilibili/master/services/listener.py
- https://raw.githubusercontent.com/Soulter/astrbot_plugin_bilibili/master/core/models.py
- https://raw.githubusercontent.com/Soulter/astrbot_plugin_bilibili/master/services/subscription_service.py

可以参考：

- `BiliClient` 对 B站库的集中封装。
- `user.User(uid).get_dynamics_new()` 的动态获取方式。
- 按 UID 合并请求。
- `id_str`、`last_id`、`recent_ids` 增量去重。
- 首次订阅初始化游标，避免历史动态批量推送。
- 视频、图文、文字、专栏、转发动态的类型分派。

### B站登录态

第一阶段必须包含本项目自己的 B站扫码登录和凭据生命周期管理：

- 生成二维码并在管理页面展示。
- 后端轮询扫码状态。
- 提取 `sessdata`、`bili_jct`、`dedeuserid`、刷新凭据等字段。
- 加密持久化 Credential。
- 定期检查和刷新登录态。
- 失效时标记 `relogin_required` 并暂停 B站轮询。
- 支持手动 Cookie 录入作为备用方式。

参考登录实现：

- https://raw.githubusercontent.com/Soulter/astrbot_plugin_bilibili/master/core/bili_login.py
- https://raw.githubusercontent.com/Soulter/astrbot_plugin_bilibili/master/core/credential_lifecycle.py

扫码登录只负责 B站账号认证，不得和 QQBot 的 AccessToken、推送队列或机器人会话混在一起。412 或频率异常时必须退避，不得自动无限重试扫码。

禁止复制：

- AstrBot 指令、会话和 UMO。
- AstrBot `MessageChain`、`At`、`AtAll`。
- AstrBot 的机器人消息分发。
- 直播监控，除非用户后续明确提出。

### 官网

官网使用公开 HTML 页面采集：

```text
新闻列表页 → 标题和详情 URL 入库 → 推送时打开详情页 → Playwright 截取 `#NIE-art`
```

官网图片推送直接打开文章详情页，截取 `#NIE-art` 容器以保留官网原始样式。列表分页链接不得作为文章入库；页面或容器不可访问时发送标题和原文 URL 的纯文本消息。

不要依赖未确认的官网内部接口。解析器必须保留 HTML fixture 或等效测试样本。

### QQBot

QQBot 使用官方 OpenAPI v2，不实现泛化 Webhook：

- API：`https://api.bot.qq.com`。
- 用户配置：`AppID`、`AppSecret`。
- 程序自动获取和刷新 `AccessToken`。
- Header：`Authorization: QQBot ACCESS_TOKEN`。
- 群聊目标使用 `group_openid`。
- 单聊目标使用 `user_openid`。
- 图片使用 `msg_type=7`，先上传取得 `file_info`，再发送。
- 单聊和群聊上传接口隔离，不能混用 `file_info`。

官方资料：

- https://bot.q.qq.com/wiki/develop/api-v2/
- https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html
- https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html
- https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html
- https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html

不要让用户手动填写 AccessToken。不要把普通 QQ 群号误当成 `group_openid`。

## 可靠性要求

采集内容和推送任务必须使用 Outbox 思路：

```text
内容入库 + delivery_task 入库
    → 更新游标
    → Worker 渲染、上传、发送
```

不要在调用 QQBot 成功之前丢弃唯一的动态/文章记录。

推送任务至少需要记录：

- 状态。
- 重试次数。
- 下次重试时间。
- QQ 消息 ID。
- QQ `trace_id`。
- 最后一次错误。

临时网络错误、429、5xx 可以重试；权限错误、目标不存在、消息格式错误应标记为永久失败并在管理页面展示。

## 图片渲染要求

- B站：截取动态详情页正文为一张长图；截图失败时发送标题和原文 URL 的纯文本消息。
- 官网：直接截取 `#NIE-art` 正文容器；截图失败时发送标题和原文 URL 的纯文本消息。
- B站优先保持单张长图，不能静默截断。
- 纯文本兜底消息必须附原文链接。
- 图片上传或发送失败时直接发送带原文 URL 的纯文本消息；纯文本发送失败仍由 Outbox 记录并重试。
- 图片资源应设置超时；生成的任务截图发送后清理。

## 安全要求

- AppSecret、B站 Cookie 和应用加密密钥不能提交到仓库。
- 日志不能输出完整凭据、AccessToken 或 Cookie。
- 管理页面必须有认证保护。
- 凭据在数据库中加密保存，页面只显示脱敏值。
- 不实现 DLL 注入、证书固定绕过、反爬绕过或游戏客户端拦截。
- B站轮询保持低频，默认间隔建议不低于 5 分钟。

## 工程约束

- 先读现有代码和方案，再修改文件。
- 优先使用简单的模块化单体，不提前引入复杂分布式组件。
- B站库调用只能出现在 B站适配层。
- QQBot 请求只能出现在 QQBot 客户端和上传器中。
- 采集、解析、渲染、推送必须保持职责分离。
- 注释使用简体中文，并保持代码风格统一。
- 使用 `rg` 搜索代码和文件。
- 文件编辑使用 `apply_patch`。
- 修改后进行静态检查、单元测试和必要的真实接口测试，并明确区分测试类型。
- 未经用户明确要求，不执行 `git commit`、`git push`、分支创建或重置操作。

## 当前阶段

当前已完成第一版业务骨架：项目配置、SQLite 数据模型、B站扫码登录与低频登录态检查、B站结构化采集、官网 HTML 采集、Outbox 推送队列、统一 Playwright 图片渲染、QQBot 官方 API v2 客户端以及 FastAPI 管理页面。

后续开发优先补充真实接口联调、官网选择器适配、图片资源加载保障、推送记录管理和部署配置；不得跳过凭据安全、采集游标、去重和推送队列。

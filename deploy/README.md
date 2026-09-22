# Linux 部署

当前脚本面向 Debian/Ubuntu 系统，使用 `systemd` 管理 `all` 模式服务。

## 首次部署

服务器安装 Git 后，在目标目录拉取仓库：

```bash
sudo mkdir -p /opt/yysls-news
sudo chown "$USER":"$USER" /opt/yysls-news
git clone <仓库地址> /opt/yysls-news
cd /opt/yysls-news
sudo bash deploy/install.sh
```

`install.sh` 会完成以下工作：

- 安装 Python、Git、编译工具和虚拟环境依赖。
- 创建系统用户 `yysls-news`。
- 创建 `.venv` 并安装项目。
- 安装 Playwright Chromium 及系统依赖。
- 在 `/etc/yysls-news/yysls-news.env` 生成生产配置。
- 生成随机的初始管理密码并在安装输出中显示一次。
- 安装并启动 `yysls-news.service`。

配置文件不放在仓库内，因此后续 Git 更新不会覆盖密钥、数据库和管理员密码。

## 配置和查看日志

编辑服务器配置：

```bash
sudo nano /etc/yysls-news/yysls-news.env
sudo systemctl restart yysls-news
```

常用命令：

```bash
sudo systemctl status yysls-news
sudo journalctl -u yysls-news -f
curl http://127.0.0.1:43100/health
```

默认只监听 `127.0.0.1:43100`，建议通过 Nginx 或 Caddy 对外提供 HTTPS。可以参考同目录的 `nginx.conf.example`，将 `news.example.com` 替换为实际域名后，再用 Certbot 配置证书。

## 一键更新

服务器上的代码没有本地修改时，执行：

```bash
cd /opt/yysls-news
sudo bash deploy/update.sh
```

更新脚本会执行 `git pull --ff-only`、更新 Python 依赖、同步 Playwright Chromium，然后重启并检查 `systemd` 服务状态。若服务器工作区有未提交修改，脚本会停止，避免覆盖人工修改。

## 发布新版本

本地完成修改并推送到远程仓库后，服务器执行：

```bash
cd /opt/yysls-news
sudo bash deploy/update.sh
```

不要把 `.env`、B站 Cookie、QQBot AppSecret 或 `/etc/yysls-news/yysls-news.env` 提交到仓库。

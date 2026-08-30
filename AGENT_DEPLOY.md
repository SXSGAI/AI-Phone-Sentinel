# AGENT_DEPLOY · AI Agent 部署执行清单

> 本文档写给 AI 编程助手（ZCode / Claude Code / Cursor / Aider 等）。
> 当用户要求"部署小暖服务端"时，Agent 应按本清单逐步执行，遇到失败先自行诊断修复，无法解决再向用户报告。
> 任何一步都**不要**把用户密钥打印到终端输出或写入任何会提交到 git 的文件。

## 目标

将本仓库的小暖服务端部署到用户的 Ubuntu 22.04 云服务器，最终状态验收标准：

```bash
curl -s http://127.0.0.1:<PORT>/api/poll
# 返回 {"due":false,"call_time":"19:00"} 即成功
```

## 需要向用户收集的信息

1. 服务器 IP 与 SSH 登录方式（密码或密钥路径）
2. SSH 用户名（root 或具备免密 sudo 的用户）
3. 部署目录（默认建议 `/opt/xiaonuan`）
4. 三样密钥（Moonshot LLM Key / MiMo ASR+TTS Key / 企业微信 Webhook，后两项可选）

## 执行清单

### 1. 环境检查
```bash
ssh <user>@<ip> "lsb_release -ds; python3 --version; timedatectl | grep 'Time zone'"
```
要求：Ubuntu 22.04、Python 3.10+。缺失则：`sudo apt update && sudo apt install -y python3 python3-venv python3-pip`。

### 2. 上传代码
```bash
scp -r ./ <user>@<ip>:/opt/xiaonuan
# 或 rsync -av --exclude '.env' --exclude 'demo.db' --exclude '__pycache__' ./ <user>@<ip>:/opt/xiaonuan/
```
确认 `.env` 在部署目录中（由第 3 步生成或用户手动上传）。

### 3. 生成 .env（交互式）
在服务器上创建 `.env`，模板如下，占位符由用户提供：
```
LLM_BASE_URL=https://api.moonshot.cn/v1
LLM_API_KEY=<用户提供>
LLM_MODEL=kimi-k2.5
ASR_PROVIDER=mimo
ASR_BASE_URL=https://api.xiaomimimo.com/v1
ASR_API_KEY=<用户提供>
ASR_MODEL=mimo-v2.5-asr
TTS_PROVIDER=mimo
MIMO_TTS_MODEL=mimo-v2.5-tts
MIMO_TTS_VOICE=茉莉
TTS_VOICE=zh-CN-XiaoxiaoNeural
WECOM_WEBHOOK=<用户提供，可留空>
```

### 4. 安装与启动
```bash
cd /opt/xiaonuan
python3 -m venv venv
./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```
时区（定时来电依赖）：`sudo timedatectl set-timezone Asia/Shanghai`

### 5. systemd 注册（开机自启 + 崩溃自愈）
```ini
# /etc/systemd/system/xiaonuan.service
[Unit]
Description=XiaoNuan AI Companion Server
After=network.target
[Service]
WorkingDirectory=/opt/xiaonuan
ExecStart=/opt/xiaonuan/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
```
`systemctl daemon-reload && systemctl enable --now xiaonuan`

### 6. 防火墙 / 安全组
提醒用户在云控制台放行所选端口（TCP 入站）。安全建议：端口选 80/443 等常规端口，或挂 Nginx 反代。

### 7. 健康检查（验收标准）
```bash
curl -s http://127.0.0.1:<PORT>/api/poll      # {"due":false,"call_time":"19:00"}
curl -s http://127.0.0.1:<PORT>/api/config    # llm_ready:true / asr:true / wecom_ready 视配置
```
两项均符合 → 部署成功。向用户报告公网访问地址，并提醒：
- 手机 App 设置页填 `http://服务器IP:端口`
- 演示手机做电池白名单（i管家信任应用）

## 失败处理速查

| 症状 | 原因与修复 |
|---|---|
| ensurepip is not available | `sudo apt install python3.10-venv`（先 apt update） |
| venv 文件删不掉 | venv 由 sudo 创建，用 `sudo rm -rf venv` |
| no such table: memory | 在服务运行中删除了 demo.db——正确顺序：停服 → 删库 → 启服（init_db 会建表） |
| WeCom 推送失败 | webhook 不完整或被限流；确认以 qyapi.weixin.qq.com 开头且 errcode=0 |
| Kimi 报 400 invalid temperature | kimi 系模型不接受自定义温度——确认使用本仓库最新 server.py（已按模型名自动省略） |
| 简报内容为空 | kimi 思考模型 token 预算不足——确认 max_tokens ≥ 1024（离线轮） |
| 手机连不上公网地址 | 云安全组未放行端口——控制台添加 TCP 入站规则 |

#!/usr/bin/env bash
# AI 电话哨兵 · 服务器一键部署（Ubuntu 22.04，root 执行）
set -e
cd "$(dirname "$0")"

echo "=== 1/5 时区设为北京时间（定时来电依赖此设置）==="
timedatectl set-timezone Asia/Shanghai 2>/dev/null || echo "(跳过：无 timedatectl 权限)"

echo "=== 2/5 Python 环境 ==="
if ! command -v python3 >/dev/null; then
  apt update -qq && apt install -y -qq python3 python3-venv python3-pip
fi
python3 -m venv venv
./venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple --upgrade pip
./venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

echo "=== 3/5 注册 systemd 服务（开机自启 + 崩溃自动重启）==="
cat > /etc/systemd/system/xiaonuan.service <<UNIT
[Unit]
Description=XiaoNuan AI Companion Server
After=network.target
[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now xiaonuan

echo "=== 4/5 健康检查 ==="
sleep 4
curl -s http://127.0.0.1:8000/api/poll || { echo "[FAIL] 启动失败，日志："; journalctl -u xiaonuan -n 20 --no-pager; exit 1; }
echo

echo "=== 5/5 完成 ==="
IP=$(curl -s ifconfig.me || hostname -I | awk '{print $1}')
echo "服务已就绪：http://$IP:8000"
echo "手机 App 设置里填这个地址即可。"
echo "别忘了：云控制台的安全组放行 TCP 8000 端口！"

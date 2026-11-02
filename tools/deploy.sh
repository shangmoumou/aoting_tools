#!/bin/bash
# 发布流程：改代码 -> 跑这个 -> 用户刷新页面看到更新提示
#
#   1. 重新生成 version.json（扫描 apps/*/app.json，校验版本一致性）
#   2. rsync 到服务器
#
# 用前把下面三个变量改成你自己的。
# Windows 用户可用 tools/deploy.bat（scp 版）。
set -e

PORTAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_USER="root"
REMOTE_HOST="你的服务器IP"
REMOTE_ROOT="/var/www/tools"

echo "==> 生成 version.json"
cd "$PORTAL_DIR"
python3 tools/make_version.py

echo "==> 同步到 $REMOTE_HOST:$REMOTE_ROOT"
rsync -avz --delete \
      --exclude 'tools/' \
      "$PORTAL_DIR/" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_ROOT/"

echo "==> 完成。用户刷新页面即会看到更新提示。"

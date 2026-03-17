#!/bin/bash
# 打包 XQPS 项目为 xqps.zip，所有文件位于 target 目录内

set -e
cd "$(dirname "$0")"

# 创建临时目标目录
TARGET_DIR="xqps"
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"

# 复制所有文件（排除不需要的）到 target 目录
rsync -av --progress . "$TARGET_DIR" \
  --exclude=".git*" \
  --exclude="__pycache__" \
  --exclude="*.pyc" \
  --exclude="*.pyo" \
  --exclude=".DS_Store" \
  --exclude=".env" \
  --exclude=".env.*" \
  --exclude=".streamlit/secrets.toml" \
  --exclude="*.streamlit/*.local.toml" \
  --exclude="demo_users.json" \
  --exclude="demo_users_hr.json" \
  --exclude="xqps/*" \
  --exclude="backup/*" \
  --exclude="archived/*" \
  --exclude="target" \
  --exclude="xqps.zip"

# 打包 target 目录
zip -r "xqps.zip" "$TARGET_DIR"

# 清理临时目录
rm -rf "$TARGET_DIR"

echo "已生成: $(pwd)/xqps.zip (解压后所有文件都在 target 目录中)"
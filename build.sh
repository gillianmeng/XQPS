#!/bin/bash
# 打包 XQPS 项目为 xqps.zip，排除敏感文件和无关目录

set -e
cd "$(dirname "$0")"

OUTPUT="xqps.zip"
# 删除旧包
rm -f "$OUTPUT"

zip -r "$OUTPUT" . \
  -x "*.git*" \
  -x "*__pycache__*" \
  -x "*.pyc" \
  -x "*.pyo" \
  -x ".DS_Store" \
  -x ".env" \
  -x ".env.*" \
  -x ".streamlit/secrets.toml" \
  -x "*.streamlit/*.local.toml" \
  -x "demo_users.json" \
  -x "demo_users_hr.json" \
  -x "xqps/*" \
  -x "backup/*" \
  -x "archived/*" \
  -x "xqps.zip"

echo "已生成: $(pwd)/$OUTPUT"

#!/bin/bash
# 启动 XQPS 绩效管理系统服务

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PORT="${PORT:-8501}"
APP_ENV="${APP_ENV:-staging}"
ENABLE_DEMO_LOGIN="${ENABLE_DEMO_LOGIN:-true}"

# 非 Docker 直接部署时，确保后台配置（公告等）可写。若未设置则使用 /data/xqps_config
if [ -z "$XQPS_CONFIG_DIR" ]; then
  export XQPS_CONFIG_DIR=/data/xqps_config
  mkdir -p "$XQPS_CONFIG_DIR" 2>/dev/null && chmod 755 "$XQPS_CONFIG_DIR" 2>/dev/null || true
fi

echo "启动 XQPS 服务 (端口: $PORT)..."
echo "人力资源部演示:     http://localhost:$PORT/?demo_entry=1&demo_dept=hr"
echo "金融产品与研究部:   http://localhost:$PORT/?demo_entry=1&demo_dept=fin_product"
echo "金融运营部演示:     http://localhost:$PORT/?demo_entry=1&demo_dept=fin_ops"
echo "合并多部门演示:     http://localhost:$PORT/?demo_entry=1"
echo ""

APP_ENV="$APP_ENV" ENABLE_DEMO_LOGIN="$ENABLE_DEMO_LOGIN" /data/conda/envs/py310/bin/python -m streamlit run new_app.py --server.address 0.0.0.0 --server.port "$PORT"

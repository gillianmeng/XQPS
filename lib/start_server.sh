#!/bin/bash
# 启动 XQPS 绩效管理系统服务

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PORT="${PORT:-8501}"
APP_ENV="${APP_ENV:-staging}"
ENABLE_DEMO_LOGIN="${ENABLE_DEMO_LOGIN:-true}"

echo "启动 XQPS 服务 (端口: $PORT)..."
echo "人力资源部演示: http://localhost:$PORT/?demo_entry=1&demo_dept=hr"
echo "研发质量部演示:  http://localhost:$PORT/?demo_entry=1"
echo ""

APP_ENV="$APP_ENV" ENABLE_DEMO_LOGIN="$ENABLE_DEMO_LOGIN" streamlit run new_app.py --server.port "$PORT"

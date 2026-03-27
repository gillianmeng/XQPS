#!/bin/bash
# 测试环境一键启动 - 仅启动主应用，演示入口通过 URL 直接访问
# 与 bin/start_server.sh 一致：可用 PORT 覆盖端口，例如 PORT=8502 ./run_demo.sh

cd "$(dirname "$0")"

PORT="${PORT:-8501}"

echo "正在启动主应用 (端口 $PORT)..."
echo ""
echo "启动成功后，在浏览器访问："
echo "  合并多部门演示:   http://localhost:$PORT/?demo_entry=1"
echo "  人力资源部演示:   http://localhost:$PORT/?demo_entry=1&demo_dept=hr"
echo "  财富顾问部演示:   http://localhost:$PORT/?demo_entry=1&demo_dept=wealth"
echo "  研发质量部演示:   http://localhost:$PORT/?demo_entry=1&demo_dept=rd"
echo "  资产管理部演示:   http://localhost:$PORT/?demo_entry=1&demo_dept=asset"
echo "  金融产品与研究部: http://localhost:$PORT/?demo_entry=1&demo_dept=fin_product"
echo "  金融运营部演示:   http://localhost:$PORT/?demo_entry=1&demo_dept=fin_ops"
echo "  正式登录:         http://localhost:$PORT/"
echo ""

APP_ENV=staging ENABLE_DEMO_LOGIN=true streamlit run new_app.py --server.port "$PORT"

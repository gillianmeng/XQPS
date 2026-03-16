#!/bin/bash
# 测试环境一键启动 - 仅启动主应用，演示入口通过 URL 直接访问

cd "$(dirname "$0")"

echo "正在启动主应用 (端口 8501)..."
echo ""
echo "启动成功后，在浏览器访问："
echo "  人力资源部演示: http://localhost:8501/?demo_entry=1&demo_dept=hr"
echo "  研发质量部演示: http://localhost:8501/?demo_entry=1"
echo "  正式登录:       http://localhost:8501/"
echo ""

APP_ENV=staging ENABLE_DEMO_LOGIN=true streamlit run new_app.py --server.port 8501

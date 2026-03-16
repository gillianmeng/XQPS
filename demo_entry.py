"""
演示测试入口 - 独立入口，不影响主应用其他模块。

用法：
  1. 先启动主应用：streamlit run new_app.py --server.port 8501
  2. 启动演示入口：streamlit run demo_entry.py --server.port 8502
  3. 访问 http://localhost:8502 进入演示专用登录页（仅展示测试账号选择，使用 demo_users.json 中的真实工号）

环境要求：ENABLE_DEMO_LOGIN=true 且 APP_ENV≠production
"""
import os
import streamlit as st

# 目标地址：主应用 URL，默认 localhost:8501
DEMO_ENTRY_TARGET = os.getenv("DEMO_ENTRY_TARGET", "http://localhost:8501")
DEMO_URL = f"{DEMO_ENTRY_TARGET.rstrip('/')}/?demo_entry=1"

st.set_page_config(page_title="演示入口 - XQPS", page_icon="🎬", layout="centered")

st.markdown("## 🎬 绩效系统 - 研发质量保障部演示入口")
st.caption("此入口展示研发质量保障部员工（罗程轶、吴昊等），使用真实工号测试。")

st.link_button("🚀 进入研发质量部演示登录", DEMO_URL, type="primary", use_container_width=True)

st.markdown("---")
st.caption("人力资源部演示请使用 demo_entry_hr.py（端口 8503）")
st.caption("若主应用运行在其他地址，请设置环境变量 DEMO_ENTRY_TARGET")

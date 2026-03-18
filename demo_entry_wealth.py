"""
财富顾问部 - 演示测试入口（用于测试「是否关联奖金」等逻辑）

用法：
  1. 先启动主应用：streamlit run new_app.py --server.port 8501
  2. 访问 http://localhost:8501/?demo_entry=1&demo_dept=wealth 进入财富顾问部演示登录页

数据来源：demo_users_wealth.json（需先用 get_open_ids.py 拉取财富顾问部真实员工 open_id 生成）

环境要求：ENABLE_DEMO_LOGIN=true 且 APP_ENV≠production
"""
import os
import streamlit as st

DEMO_ENTRY_TARGET = os.getenv("DEMO_ENTRY_TARGET", "http://localhost:8501")
DEMO_WEALTH_URL = f"{DEMO_ENTRY_TARGET.rstrip('/')}/?demo_entry=1&demo_dept=wealth"

st.set_page_config(page_title="财富顾问部演示 - XQPS", page_icon="💰", layout="centered")

st.markdown("## 💰 绩效系统 - 财富顾问部演示入口")
st.caption("此入口用于测试「是否关联奖金」等飞书多维表格逻辑配置，使用真实工号。")

st.link_button("🚀 进入财富顾问部演示登录", DEMO_WEALTH_URL, type="primary", use_container_width=True)

st.markdown("---")
st.markdown("**数据说明**：需配置 `demo_users_wealth.json`，可运行 `python3 get_open_ids.py 财富顾问部` 从飞书拉取真实员工信息。")
st.caption(f"目标地址：`{DEMO_WEALTH_URL}`")

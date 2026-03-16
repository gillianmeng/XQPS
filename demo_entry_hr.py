"""
人力资源部 - 演示测试入口（独立入口，与研发质量部入口分离，避免混淆）

用法：
  1. 先启动主应用：streamlit run new_app.py --server.port 8501
  2. 启动人力资源部演示入口：streamlit run demo_entry_hr.py --server.port 8503
  3. 访问 http://localhost:8503 进入人力资源部演示登录页

数据来源：demo_users_hr.json（需先用 get_open_ids.py 拉取人力资源部真实员工 open_id 生成）

环境要求：ENABLE_DEMO_LOGIN=true 且 APP_ENV≠production
"""
import os
import streamlit as st

DEMO_ENTRY_TARGET = os.getenv("DEMO_ENTRY_TARGET", "http://localhost:8501")
DEMO_HR_URL = f"{DEMO_ENTRY_TARGET.rstrip('/')}/?demo_entry=1&demo_dept=hr"

st.set_page_config(page_title="人力资源部演示 - XQPS", page_icon="👥", layout="centered")

st.markdown("## 👥 绩效系统 - 人力资源部演示入口")
st.caption("此入口仅展示人力资源部员工，使用真实工号测试，与研发质量部入口分离。")

st.link_button("🚀 进入人力资源部演示登录", DEMO_HR_URL, type="primary", use_container_width=True)

st.markdown("---")
st.markdown("**数据说明**：需配置 `demo_users_hr.json`，可运行 `python get_open_ids.py 人力资源部` 从飞书拉取真实员工信息。")
st.caption(f"目标地址：`{DEMO_HR_URL}`")

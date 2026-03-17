import streamlit as st
import requests
import urllib.parse
import time
from collections import Counter
import os
import json
import csv
import io
import math
import pandas as pd
import altair as alt

# --- 配置区：严禁在代码中硬编码密钥 ---
# 支持两种来源（优先级从高到低）：
# 1) Streamlit secrets（推荐生产）：.streamlit/secrets.toml
# 2) 系统环境变量（推荐容器/ECS）：export FEISHU_APP_ID=...

def _get_config_value(key: str, env_key: str, default: str | None = None) -> str | None:
    try:
        if hasattr(st, "secrets") and key in st.secrets and st.secrets.get(key):
            return str(st.secrets.get(key))
    except Exception:
        pass
    return os.getenv(env_key, default)


APP_ID = _get_config_value("FEISHU_APP_ID", "FEISHU_APP_ID")
APP_SECRET = _get_config_value("FEISHU_APP_SECRET", "FEISHU_APP_SECRET")
APP_TOKEN = _get_config_value("FEISHU_APP_TOKEN", "FEISHU_APP_TOKEN")
TABLE_ID = _get_config_value("FEISHU_TABLE_ID", "FEISHU_TABLE_ID")
REDIRECT_URI = _get_config_value("REDIRECT_URI", "REDIRECT_URI", "http://localhost:8501")
APP_ENV = (_get_config_value("APP_ENV", "APP_ENV", "production") or "production").lower()
IS_PROD = APP_ENV in ("prod", "production")
ENABLE_DEMO_LOGIN = (_get_config_value("ENABLE_DEMO_LOGIN", "ENABLE_DEMO_LOGIN", "false") or "false").lower() == "true"
ENABLE_DEV_TOOLS = (_get_config_value("ENABLE_DEV_TOOLS", "ENABLE_DEV_TOOLS", "false") or "false").lower() == "true"

missing = [name for name, val in [("FEISHU_APP_ID", APP_ID), ("FEISHU_APP_SECRET", APP_SECRET), ("FEISHU_APP_TOKEN", APP_TOKEN), ("FEISHU_TABLE_ID", TABLE_ID)] if not val]
if missing:
    st.error(f"配置缺失：{', '.join(missing)}。请在环境变量或 `.streamlit/secrets.toml` 中配置。")
    st.stop()

SCORE_OPTIONS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
GRADE_OPTIONS = ["S", "A", "B+", "B", "B-", "C"]
# 绩效等级图表配色（S/A蓝、B+绿、B灰、B-橙、C红），全应用统一
GRADE_CHART_COLORS = ["#4CAFEE", "#4CAFEE", "#8BC34A", "#90A4AE", "#FFC107", "#F44336"]

# --- 初始化会话状态 ---
if 'user_info' not in st.session_state:
    st.session_state.user_info = None
if 'role' not in st.session_state:
    st.session_state.role = None
if 'goal_count' not in st.session_state:
    st.session_state.goal_count = 3
if 'feishu_record' not in st.session_state:
    st.session_state.feishu_record = {}
if 'feishu_record_id' not in st.session_state:
    st.session_state.feishu_record_id = None
if 'selected_subordinate_id' not in st.session_state:
    st.session_state.selected_subordinate_id = None

# --- 飞书原生 API 安全接口 ---
REQUEST_TIMEOUT_SEC = 12
REQUEST_RETRY_TIMES = 2


def _request_json_with_retry(method, url, headers=None, params=None, json_data=None):
    last_err = None
    for attempt in range(REQUEST_RETRY_TIMES + 1):
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < REQUEST_RETRY_TIMES:
                time.sleep(0.35 * (attempt + 1))
    return {"code": -1, "msg": f"请求失败：{last_err}"}


@st.cache_data(ttl=300)
def get_tenant_token():
    token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    res = _request_json_with_retry(
        "POST",
        token_url,
        json_data={"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    return res.get("tenant_access_token")

def get_feishu_user(code):
    tenant_token = get_tenant_token()
    if not tenant_token:
        return None, "获取 Token 失败"
    user_url = "https://open.feishu.cn/open-apis/authen/v1/access_token"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    payload = {"grant_type": "authorization_code", "code": code}
    res = _request_json_with_retry("POST", user_url, headers=headers, json_data=payload)
    if res.get("code") == 0:
        return res.get("data"), None
    return None, res.get('msg')

@st.cache_data(ttl=60)
def fetch_all_records_safely(app_token, table_id):
    tenant_token = get_tenant_token()
    if not tenant_token:
        return []
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    all_items = []
    page_token = ""
    has_more = True
    while has_more:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        res = _request_json_with_retry("GET", url, headers=headers, params=params)
        if res.get("code") == 0:
            data = res.get("data", {})
            all_items.extend(data.get("items", []))
            has_more = data.get("has_more", False)
            page_token = data.get("page_token", "")
        else:
            break
    return all_items

def get_record_by_openid_safely(app_token, table_id, target_openid, fallback_name="", fallback_emp_id=""):
    all_records = fetch_all_records_safely(app_token, table_id)
    def _to_text(v):
        if isinstance(v, list):
            if v and isinstance(v[0], dict):
                return str(v[0].get("name") or v[0].get("text") or "")
            return str(v[0]) if v else ""
        if isinstance(v, dict):
            return str(v.get("name") or v.get("text") or "")
        return str(v or "")
    # 1) 优先按 open_id 命中
    for record in all_records:
        fields = record.get("fields", {})
        value = fields.get("姓名")
        if value is None:
            continue
        if isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict) and value[0].get("id") == target_openid:
            return record
        elif isinstance(value, dict) and value.get("id") == target_openid:
            return record
    # 2) demo 场景兜底：按姓名 / 工号命中
    fb_name = str(fallback_name or "").strip()
    fb_emp = str(fallback_emp_id or "").strip()
    if fb_name or fb_emp:
        for record in all_records:
            fields = record.get("fields", {})
            rec_name = _to_text(fields.get("姓名")).strip()
            rec_emp = _to_text(fields.get("工号") or fields.get("员工工号")).strip()
            if (fb_name and rec_name == fb_name) or (fb_emp and rec_emp == fb_emp):
                return record
    return None

@st.cache_data(ttl=300)
def fetch_table_field_names(app_token, table_id):
    tenant_token = get_tenant_token()
    if not tenant_token:
        return set()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    page_token = ""
    has_more = True
    field_names = set()
    while has_more:
        params = {"page_size": 200}
        if page_token:
            params["page_token"] = page_token
        res = _request_json_with_retry("GET", url, headers=headers, params=params)
        if res.get("code") != 0:
            break
        data = res.get("data", {})
        for item in data.get("items", []):
            name = str(item.get("field_name", "")).strip()
            if name:
                field_names.add(name)
        has_more = data.get("has_more", False)
        page_token = data.get("page_token", "")
    return field_names


def update_record_safely(app_token, table_id, record_id, update_data):
    tenant_token = get_tenant_token()
    if not tenant_token:
        return False, "获取 Token 失败"

    valid_fields = fetch_table_field_names(app_token, table_id)
    if valid_fields:
        cleaned = {k: v for k, v in (update_data or {}).items() if k in valid_fields}
        dropped = [k for k in (update_data or {}).keys() if k not in cleaned]
        if dropped and not cleaned:
            return False, f"字段不存在：{', '.join(dropped)}"
        update_data = cleaned

    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}
    payload = {"fields": update_data}
    res = _request_json_with_retry("PUT", url, headers=headers, json_data=payload)
    if res.get("code") == 0:
        return True, "成功"
    else:
        return False, res.get("msg", str(res))

def calculate_grade(score):
    if score == 0.0: return "-"
    if score >= 4.5: return "S"
    elif score >= 4.0: return "A"
    elif score >= 3.5: return "B+"
    elif score >= 3.0: return "B"
    elif score >= 2.5: return "B-"
    else: return "C"

def load_demo_users(demo_dept=None):
    """
    读取本地 demo 用户配置。
    demo_dept: None 或 "rd" -> 研发质量保障部 (demo_users.json)
    demo_dept: "hr" -> 人力资源部 (demo_users_hr.json)
    """
    if demo_dept == "hr":
        candidate_files = ["demo_users_hr.json", "demo_users_hr.example.json"]
    else:
        candidate_files = ["demo_users.json", "demo_users.example.json"]
    raw_users = []
    for f in candidate_files:
        if not os.path.exists(f):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                raw_users = data.get("users", [])
            elif isinstance(data, list):
                raw_users = data
            else:
                raw_users = []
            if raw_users:
                break
        except Exception:
            raw_users = []

    users = []
    for item in raw_users:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "员工")).strip()
        if role not in ["员工", "管理者"]:
            continue
        name = str(item.get("name", "")).strip()
        open_id = str(item.get("open_id", "")).strip()
        emp_id = str(item.get("emp_id", "")).strip()
        job_title = str(item.get("job_title", "未分配")).strip()
        label = str(item.get("label", "")).strip() or f"{name} (工号: {emp_id})"
        if not (name and open_id and emp_id):
            continue
        users.append({
            "label": label,
            "name": name,
            "open_id": open_id,
            "emp_id": emp_id,
            "job_title": job_title,
            "role": role
        })
    return users

# --- 登录页面逻辑 ---
def login_page():
    demo_dept = st.query_params.get("demo_dept", "").strip() or None  # "hr"=人力资源部
    is_demo_entry = st.query_params.get("demo_entry") == "1"
    show_demo_only = is_demo_entry and ENABLE_DEMO_LOGIN and not IS_PROD

    if is_demo_entry and not (ENABLE_DEMO_LOGIN and not IS_PROD):
        st.header("🎯 绩效管理系统")
        st.warning("⚠️ 演示入口未开启。请设置 ENABLE_DEMO_LOGIN=true 且 APP_ENV≠production 后使用。")
        st.link_button("← 返回登录页", "?", use_container_width=True)
        return

    st.header("🎯 绩效管理系统 - 内部开发版")

    if show_demo_only:
        dept_label = "人力资源部" if demo_dept == "hr" else "研发质量保障部"
        st.markdown("### 🎬 演示测试入口")
        st.caption(f"选择真实员工账号进行演示登录（{dept_label}）")
    else:
        st.markdown("### 🔐 飞书账号正式登录")
    if "code" in st.query_params and not show_demo_only:
        code = st.query_params["code"]
        with st.spinner("正在验证飞书身份..."):
            user_data, error_msg = get_feishu_user(code)
            if user_data:
                st.session_state.user_info = user_data
                # 正式登录后，角色由飞书档案字段「角色」决定
                st.session_state.role = None
                st.query_params.clear()
                st.rerun()
            else:
                st.error(f"❌ 授权失败。飞书底层拦截原因：{error_msg}")
                if st.button("🔄 清除失效 Code 并重新登录", key="btn_clear_code"):
                    st.query_params.clear()
                    st.rerun()
    elif not show_demo_only:
        encoded_uri = urllib.parse.quote(REDIRECT_URI)
        base_url = "https://open.feishu.cn/open-apis/authen/v1/user_auth_page_beta"
        params = f"?app_id={APP_ID}&redirect_uri={encoded_uri}&state=testing"
        st.info("请使用您的企业飞书账号授权登录")
        st.link_button("🔗 飞书一键授权登录 (正式入口)", base_url + params)

    # 演示入口：demo_entry=1 时仅展示此块；否则在飞书登录下方展示
    if ENABLE_DEMO_LOGIN and not IS_PROD:
        if not show_demo_only:
            st.markdown("---")
            st.markdown("### 🛠️ 演示与测试通道")
        demo_users = load_demo_users(demo_dept)
        mgr_users = [u for u in demo_users if u["role"] == "管理者"]
        emp_users = [u for u in demo_users if u["role"] == "员工"]
        col1, col2 = st.columns(2)
        with col1:
            st.write("🧑‍💼 **管理者模拟入口**")
            if mgr_users:
                selected_mgr_label = st.selectbox("选择管理者测试账号", [u["label"] for u in mgr_users], key="demo_mgr_select", label_visibility="collapsed")
                if st.button("🛠️ 管理者登录", use_container_width=True, key="btn_login_mgr"):
                    selected_mgr = next((u for u in mgr_users if u["label"] == selected_mgr_label), None)
                    if selected_mgr:
                        st.session_state.user_info = {
                            "name": selected_mgr["name"],
                            "open_id": selected_mgr["open_id"],
                            "emp_id": selected_mgr["emp_id"],
                            "job_title": selected_mgr["job_title"]
                        }
                        st.session_state.role = "管理者"
                        st.rerun()
            else:
                st.caption("未配置管理者测试账号")

        with col2:
            st.write("🧑‍💻 **下属测试入口 (Demo 数据)**")
            if emp_users:
                selected_emp_label = st.selectbox("选择员工测试账号", [u["label"] for u in emp_users], key="demo_emp_select", label_visibility="collapsed")
                if st.button("🛠️ 登录填报 (选定下属)", use_container_width=True, key="btn_login_emp"):
                    selected_emp = next((u for u in emp_users if u["label"] == selected_emp_label), None)
                    if selected_emp:
                        st.session_state.user_info = {
                            "name": selected_emp["name"],
                            "open_id": selected_emp["open_id"],
                            "emp_id": selected_emp["emp_id"],
                            "job_title": selected_emp["job_title"]
                        }
                        st.session_state.role = "员工"
                        st.rerun()
            else:
                st.caption("未配置员工测试账号")

        if not demo_users:
            if demo_dept == "hr":
                st.info("💡 提示：未读取到 demo_users_hr.json，可运行 `python3 get_open_ids.py 人力资源部` 生成。")
            else:
                st.info("💡 提示：未读取到 demo_users.json，可参考 demo_users.example.json 创建本地测试账号。")
        if show_demo_only:
            st.markdown("---")
            st.link_button("← 返回正式登录入口", "?", use_container_width=True)

def jump_to_subordinate(sub_id):
    st.session_state.selected_subordinate_id = sub_id

def return_to_self():
    st.session_state.selected_subordinate_id = None

# --- 主应用逻辑 ---
def main_app():
    # --- 注入自定义 CSS ---
    st.markdown("""
    <style>
    /* 全局基础字体：正文统一 14px */
    body, [data-testid="stMarkdown"] p, [data-testid="stMarkdown"] li, [data-testid="stMarkdown"] label, [data-testid="stMarkdown"] span {
        font-size: 14px !important;
    }
    /* 全局标题：统一字号（强制命中 st.header 与 markdown 标题） */
    .stApp h1,
    .stApp h2,
    .stApp h3,
    .stApp h4,
    .stApp h5,
    .stApp h6,
    [data-testid="stMarkdown"] h1,
    [data-testid="stMarkdown"] h2,
    [data-testid="stMarkdown"] h3,
    [data-testid="stMarkdown"] h4,
    [data-testid="stMarkdown"] h5,
    [data-testid="stMarkdown"] h6 {
        font-size: 24px !important;
        line-height: 1.25 !important;
    }
    .section-title {
        font-size: 16px;
        font-weight: 700;
        margin: 0 0 10px 0;
        color: #FAFAFA;
    }
    /* 业务模块标题（统一 24px） */
    .module-title {
        font-size: 24px !important;
        font-weight: 700 !important;
        line-height: 1.25 !important;
        margin: 0 0 8px 0 !important;
        color: #FAFAFA !important;
    }
    .hero-title {
        font-size: 28px !important;
        font-weight: 700 !important;
        line-height: 1.25 !important;
        margin: 0 0 8px 0 !important;
        color: #FAFAFA !important;
    }

    /* 允许所有文本域横向和纵向拉伸 */
    textarea {
        resize: both !important;
    }

    /* 文本框右下角拖拽区域样式 */
    textarea::-webkit-resizer {
        width: 24px !important;
        height: 24px !important;
        background-color: transparent !important;
        background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="%23888888" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="14 20 20 20 20 14"></polyline><line x1="10" y1="10" x2="20" y2="20"></line></svg>') !important;
        background-repeat: no-repeat !important;
        background-position: bottom 4px right 4px !important; 
    }
    /* 文本框占位提示：更小、更浅色 */
    textarea::placeholder {
        font-size: 12px !important;
        color: #777777 !important;
    }
    [data-testid="stSidebar"] .stButton button {
        font-size: 14px !important;
        min-height: 32px !important;
        height: 32px !important;
        padding: 0px 8px !important;
    }

    /* 隐藏路标盒子 */
    div.element-container:has(.save-marker) {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
    }

    /* 草稿按钮幽灵绿 */
    div.element-container:has(.save-marker) + div.element-container button {
        background-color: rgba(46, 125, 50, 0.2) !important;
        color: #81c784 !important;
        border: 1px solid #4caf50 !important;
    }
    div.element-container:has(.save-marker) + div.element-container button:hover {
        background-color: rgba(46, 125, 50, 0.4) !important;
        color: #ffffff !important;
        border-color: #81c784 !important;
    }

    /* 分页标签：统一字号与风格（不再固定） */
    [data-testid="stTabs"] {
        gap: 8px;
    }
    /* 隐藏底部高亮线，使用按钮本身区分选中态 */
    [data-testid="stTabs"] div[data-baseweb="tab-highlight"] {
        background-color: transparent !important;
        height: 0px !important;
    }
    /* 基础 Tab 按钮 */
    [data-testid="stTabs"] button[role="tab"] {
        padding: 8px 16px !important;
        transition: all 0.2s ease !important;
        font-size: 15px !important;
        font-weight: 600 !important;
        color: #b0b0b0 !important;
        border: 1px solid rgba(255,255,255,0.16) !important;
        border-radius: 8px !important;
        background-color: rgba(255,255,255,0.02) !important;
        min-height: 38px !important;
    }
    /* 选中态：仅改颜色/粗细，不改字号 */
    [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
        background-color: rgba(30, 144, 255, 0.16) !important;
        color: #8fc9ff !important;
        font-size: 15px !important;
        font-weight: 700 !important;
        border: 1px solid #1E90FF !important;
        box-shadow: none !important;
        transform: none !important;
    }
    [data-testid="stTabs"] button[role="tab"]:hover {
        background-color: rgba(30, 144, 255, 0.08) !important;
        border-color: rgba(30, 144, 255, 0.35) !important;
    }
    [data-testid="stTabs"] button[role="tab"]:active {
        transform: none !important;
        box-shadow: none !important;
    }
    /* 指标（Metric）与正文一致 14px */
    [data-testid="stMetricValue"] {
        font-size: 14px !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 14px !important;
    }
    /* 报表 KPI 数字按钮（用于下钻） */
    div.element-container:has(.report-kpi-total),
    div.element-container:has(.report-kpi-done),
    div.element-container:has(.report-kpi-rate),
    div.element-container:has(.report-kpi-pending),
    div.element-container:has(.report-kpi-grade-s),
    div.element-container:has(.report-kpi-grade-a),
    div.element-container:has(.report-kpi-grade-bp),
    div.element-container:has(.report-kpi-grade-b),
    div.element-container:has(.report-kpi-grade-bm),
    div.element-container:has(.report-kpi-grade-c) {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
        min-height: 0 !important;
        height: 0 !important;
    }
    div.element-container:has(.report-kpi-total) + div.element-container,
    div.element-container:has(.report-kpi-done) + div.element-container,
    div.element-container:has(.report-kpi-rate) + div.element-container,
    div.element-container:has(.report-kpi-pending) + div.element-container,
    div.element-container:has(.report-kpi-grade-s) + div.element-container,
    div.element-container:has(.report-kpi-grade-a) + div.element-container,
    div.element-container:has(.report-kpi-grade-bp) + div.element-container,
    div.element-container:has(.report-kpi-grade-b) + div.element-container,
    div.element-container:has(.report-kpi-grade-bm) + div.element-container,
    div.element-container:has(.report-kpi-grade-c) + div.element-container {
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        margin-top: 2px !important;
    }
    div.element-container:has(.report-kpi-total) + div.element-container button,
    div.element-container:has(.report-kpi-done) + div.element-container button,
    div.element-container:has(.report-kpi-rate) + div.element-container button,
    div.element-container:has(.report-kpi-pending) + div.element-container button,
    div.element-container:has(.report-kpi-grade-s) + div.element-container button,
    div.element-container:has(.report-kpi-grade-a) + div.element-container button,
    div.element-container:has(.report-kpi-grade-bp) + div.element-container button,
    div.element-container:has(.report-kpi-grade-b) + div.element-container button,
    div.element-container:has(.report-kpi-grade-bm) + div.element-container button,
    div.element-container:has(.report-kpi-grade-c) + div.element-container button {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        padding: 0 !important;
        min-height: 44px !important;
        height: 44px !important;
        font-size: 36px !important;
        font-weight: 900 !important;
        line-height: 1 !important;
    }
    div.element-container:has(.report-kpi-total) + div.element-container button *,
    div.element-container:has(.report-kpi-done) + div.element-container button *,
    div.element-container:has(.report-kpi-rate) + div.element-container button *,
    div.element-container:has(.report-kpi-pending) + div.element-container button *,
    div.element-container:has(.report-kpi-grade-s) + div.element-container button *,
    div.element-container:has(.report-kpi-grade-a) + div.element-container button *,
    div.element-container:has(.report-kpi-grade-bp) + div.element-container button *,
    div.element-container:has(.report-kpi-grade-b) + div.element-container button *,
    div.element-container:has(.report-kpi-grade-bm) + div.element-container button *,
    div.element-container:has(.report-kpi-grade-c) + div.element-container button * {
        font-weight: 900 !important;
    }
    div.element-container:has(.report-kpi-total) + div.element-container button { color: #42A5F5 !important; }
    div.element-container:has(.report-kpi-done) + div.element-container button { color: #26A69A !important; }
    div.element-container:has(.report-kpi-rate) + div.element-container button { color: #FFA726 !important; }
    div.element-container:has(.report-kpi-pending) + div.element-container button { color: #EF5350 !important; }
    div.element-container:has(.report-kpi-grade-s) + div.element-container button { color: #4CAF50 !important; }
    div.element-container:has(.report-kpi-grade-a) + div.element-container button { color: #42A5F5 !important; }
    div.element-container:has(.report-kpi-grade-bp) + div.element-container button { color: #66BB6A !important; }
    div.element-container:has(.report-kpi-grade-b) + div.element-container button { color: #90A4AE !important; }
    div.element-container:has(.report-kpi-grade-bm) + div.element-container button { color: #FFB74D !important; }
    div.element-container:has(.report-kpi-grade-c) + div.element-container button { color: #EF5350 !important; }

    /* 绩效概览数字样式：更醒目 */
    .report-kpi-label {
        font-size: 16px;
        color: #b7bdc8;
        text-align: center;
        font-weight: 800;
    }
    .report-grade-label {
        font-size: 16px;
        color: #b7bdc8;
        text-align: center;
        font-weight: 800;
    }
    /* 提示框文案统一缩小（info/warning/error/success） */
    [data-testid="stAlert"] p,
    [data-testid="stAlert"] div {
        font-size: 13px !important;
        line-height: 1.4 !important;
    }
    [data-testid="stAlert"] {
        padding-top: 6px !important;
        padding-bottom: 6px !important;
    }

    /* 上级评分页：更紧凑的分隔线与列表行 */
    hr.sub-hr {
        margin: 10px 0px !important;
        border: none !important;
        border-top: 1px solid rgba(255,255,255,0.08) !important;
    }
    .sub-list-head {
        font-size: 14px;
        color: #b0b0b0;
        margin: 0 0 10px 0;
        font-weight: 700;
        text-align: center;
        white-space: nowrap;
    }
    .sub-list-cell {
        font-size: 14px;
        margin: 0;
        padding: 10px 12px;
        line-height: 1.6;
        text-align: center;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .sub-list-cell-multiline {
        line-height: 1.8;
        padding: 12px 12px;
    }
    /* 综合调整：待调整名单表格列间距缩小，第一二列空隙适中 */
    [data-testid="stHorizontalBlock"]:has(.sub-list-head),
    [data-testid="stHorizontalBlock"]:has(.sub-list-cell) {
        gap: 8px !important;
    }
    /* 综合调整：待调整名单表格列内容居中，避免挤在右侧 */
    div:has(#vp-adjust-table) ~ * [data-testid="column"] > div,
    div:has(#dept-adjust-table) ~ * [data-testid="column"] > div,
    [data-testid="column"]:has(.sub-list-head) > div,
    [data-testid="column"]:has(.sub-list-cell) > div {
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        width: 100% !important;
    }
    /* 搜索/筛选：全局统一小字号（强制命中） */
    .stApp [data-baseweb="input"] input {
        font-size: 12px !important;
        line-height: 1.25 !important;
    }
    .stApp [data-baseweb="select"] * {
        font-size: 12px !important;
        line-height: 1.25 !important;
    }
    .stApp [data-baseweb="popover"] * {
        font-size: 12px !important;
        line-height: 1.25 !important;
    }
    /* 综合调整筛选行：输入框和下拉文案居中 */
    input[aria-label="搜索工号、姓名"] {
        text-align: center !important;
        height: 40px !important;
        font-size: 11px !important;
        font-weight: 500 !important;
    }
    input[aria-label="搜索工号、姓名"]::placeholder {
        font-size: 11px !important;
    }
    div[data-baseweb="select"]:has(input[aria-label="部门"]) * ,
    div[data-baseweb="select"]:has(input[aria-label="状态"]) * ,
    div[data-baseweb="select"]:has(input[aria-label="考核等级"]) * {
        text-align: center !important;
        font-size: 11px !important;
        color: #b0b0b0 !important;
    }
    /* 兼容不同DOM：确保筛选框已选文字和输入文字都与左侧搜索框一致 */
    div[data-baseweb="select"]:has(input[aria-label="部门"]),
    div[data-baseweb="select"]:has(input[aria-label="状态"]),
    div[data-baseweb="select"]:has(input[aria-label="考核等级"]) {
        font-size: 11px !important;
    }
    div[data-baseweb="select"]:has(input[aria-label="部门"]) > div,
    div[data-baseweb="select"]:has(input[aria-label="状态"]) > div,
    div[data-baseweb="select"]:has(input[aria-label="考核等级"]) > div,
    div[data-baseweb="select"]:has(input[aria-label="部门"]) span,
    div[data-baseweb="select"]:has(input[aria-label="状态"]) span,
    div[data-baseweb="select"]:has(input[aria-label="考核等级"]) span {
        font-size: 11px !important;
        line-height: 1.25 !important;
    }
    div[data-baseweb="select"]:has(input[aria-label="部门"]) > div,
    div[data-baseweb="select"]:has(input[aria-label="状态"]) > div,
    div[data-baseweb="select"]:has(input[aria-label="考核等级"]) > div {
        min-height: 40px !important;
        background: rgba(90, 95, 105, 0.35) !important;
        border-color: rgba(255,255,255,0.2) !important;
        box-shadow: none !important;
    }
    /* 下拉候选项字体统一缩小 */
    div[role="listbox"] div[role="option"] {
        font-size: 11px !important;
        line-height: 1.3 !important;
    }
    /* 筛选框已选值/输入值最终强制（覆盖 baseweb 默认） */
    .stApp div[data-baseweb="select"] > div,
    .stApp div[data-baseweb="select"] input {
        font-size: 11px !important;
        font-weight: 500 !important;
    }
    /* 员工列表动作按钮规范（限定在 .mgr-sub-list，避免全局 :has 导致卡顿）
       统一为「填充色块 + 文本」按钮，行内两行信息垂直居中。 */
    .mgr-sub-list div.element-container:has(.xqps-btn-remind),
    .mgr-sub-list div.element-container:has(.xqps-btn-view),
    .mgr-sub-list div.element-container:has(.xqps-btn-evaluate),
    .mgr-sub-list div.element-container:has(.xqps-btn-adjust) {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
        height: 0 !important;
        min-height: 0 !important;
    }
    .mgr-sub-list div.element-container:has(.xqps-btn-remind) + div.element-container,
    .mgr-sub-list div.element-container:has(.xqps-btn-view) + div.element-container,
    .mgr-sub-list div.element-container:has(.xqps-btn-evaluate) + div.element-container,
    .mgr-sub-list div.element-container:has(.xqps-btn-adjust) + div.element-container {
        margin-top: 0 !important;
        padding-top: 0 !important;
        margin-bottom: 0 !important;
        padding-bottom: 0 !important;
        min-height: 56px !important;
        height: 56px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
    }
    .mgr-sub-list div.element-container:has(.xqps-btn-remind) + div.element-container button,
    .mgr-sub-list div.element-container:has(.xqps-btn-view) + div.element-container button,
    .mgr-sub-list div.element-container:has(.xqps-btn-evaluate) + div.element-container button,
    .mgr-sub-list div.element-container:has(.xqps-btn-adjust) + div.element-container button,
    div.element-container:has(.xqps-btn-collapse) + div.element-container button {
        font-size: 13px !important;
        font-weight: 500 !important;
        line-height: 1.2 !important;
        min-height: 32px !important;
        height: 32px !important;
        padding: 4px 14px !important;
        border-radius: 8px !important;
        border: none !important;
        box-shadow: none !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        margin: 0 !important;
        vertical-align: middle !important;
    }
    .mgr-sub-list div.element-container:has(.xqps-btn-remind) + div.element-container button {
        background: #FFA726 !important;
        color: #1a1a1a !important;
    }
    .mgr-sub-list div.element-container:has(.xqps-btn-view) + div.element-container button,
    div.element-container:has(.xqps-btn-collapse) + div.element-container button {
        background: #42A5F5 !important;
        color: #ffffff !important;
    }
    .mgr-sub-list div.element-container:has(.xqps-btn-evaluate) + div.element-container button {
        background: #26A69A !important;
        color: #ffffff !important;
    }
    .mgr-sub-list div.element-container:has(.xqps-btn-adjust) + div.element-container button {
        background: #5C6BC0 !important;
        color: #ffffff !important;
    }
    /* 统一按钮容器间距，避免刷新后行高抖动 */
    .mgr-sub-list div[data-testid="stButton"] {
        margin: 0 !important;
    }
    .mgr-sub-list div[data-testid="stButton"] > button {
        margin: 0 !important;
        align-self: center !important;
    }
    .mgr-sub-list div.element-container {
        margin-top: 0 !important;
        margin-bottom: 0 !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
    }
    .mgr-sub-list [data-testid="stMarkdownContainer"] p {
        margin: 0 !important;
    }
    /* 隐藏综合调整按钮路标 */
    div.element-container:has(.dept-draft-marker),
    div.element-container:has(.vp-draft-marker),
    div.element-container:has(.dept-confirm-marker),
    div.element-container:has(.vp-confirm-marker) {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
        min-height: 0 !important;
        height: 0 !important;
    }
    /* 综合调整：行内暂存按钮，统一为小号填充文本按钮（两行信息居中对齐）
       同时兼容「同容器」和「相邻容器」两种 DOM 结构，避免颜色缺失。 */
    div.element-container:has(.dept-draft-marker) button,
    div.element-container:has(.vp-draft-marker) button,
    div.element-container:has(.dept-draft-marker) + div.element-container button,
    div.element-container:has(.vp-draft-marker) + div.element-container button {
        background: #42A5F5 !important;
        background-color: #42A5F5 !important;
        color: #ffffff !important;
        border: none !important;
        box-shadow: none !important;
        min-height: 32px !important;
        height: 32px !important;
        padding: 4px 16px !important;
        border-radius: 8px !important;
        margin: 0 !important;
        font-size: 13px !important;
        font-weight: 500 !important;
        line-height: 1.2 !important;
        box-sizing: border-box !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
    }
    div.element-container:has(.dept-draft-marker) + div.element-container,
    div.element-container:has(.vp-draft-marker) + div.element-container {
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        min-height: 56px !important;
        height: 56px !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    div.element-container:has(.dept-draft-marker) + div.element-container button:hover,
    div.element-container:has(.vp-draft-marker) + div.element-container button:hover {
        color: #ffffff !important;
    }
    /* 综合调整：确认本次调整（红色最终确认样式，去边框/发光） */
    div.element-container:has(.dept-confirm-marker) + div.element-container button,
    div.element-container:has(.vp-confirm-marker) + div.element-container button {
        background: #e53935 !important;
        color: #ffffff !important;
        border: none !important;
        box-shadow: none !important;
        min-height: 34px !important;
        height: 34px !important;
        border-radius: 10px !important;
        font-size: 14px !important;
        font-weight: 700 !important;
    }
    div.element-container:has(.dept-confirm-marker) + div.element-container,
    div.element-container:has(.vp-confirm-marker) + div.element-container {
        margin-top: 2px !important;
        margin-bottom: 6px !important;
    }
    div.element-container:has(.dept-confirm-marker) + div.element-container button:hover,
    div.element-container:has(.vp-confirm-marker) + div.element-container button:hover {
        background: #d32f2f !important;
        border-color: #ef5350 !important;
    }
    /* 综合调整操作列：仅图标、无边框、无底色（精准命中相邻按钮容器） */
    div.element-container:has(.dept-icon-save) + div.element-container button,
    div.element-container:has(.dept-icon-submit) + div.element-container button,
    div.element-container:has(.vp-icon-save) + div.element-container button,
    div.element-container:has(.vp-icon-submit) + div.element-container button {
        border: none !important;
        border-color: transparent !important;
        background: transparent !important;
        background-color: transparent !important;
        box-shadow: none !important;
        min-height: 20px !important;
        height: 20px !important;
        padding: 0 !important;
        margin: 0 !important;
        font-size: 18px !important;
        line-height: 1 !important;
    }
    div.element-container:has(.dept-icon-save) + div.element-container button:hover,
    div.element-container:has(.dept-icon-submit) + div.element-container button:hover,
    div.element-container:has(.vp-icon-save) + div.element-container button:hover,
    div.element-container:has(.vp-icon-submit) + div.element-container button:hover {
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    </style>
    """, unsafe_allow_html=True)
    
    def extract_text(val, default="未获取"):
        if val is None or val == "": return default
        if isinstance(val, list):
            res = []
            for item in val:
                if isinstance(item, dict):
                    txt = item.get("name") or item.get("text") or item.get("full_name") or item.get("en_name")
                    res.append(str(txt) if txt else str(item))
                else:
                    res.append(str(item))
            return ", ".join(res) if res else default
        if isinstance(val, dict):
            txt = val.get("name") or val.get("text") or val.get("full_name")
            return str(txt) if txt else str(val)
        return str(val)

    def normalize_dept_text(val):
        raw = extract_text(val, "").strip()
        if raw in ["", "未获取", "-", "--", "—"]:
            return ""
        raw = raw.replace("—", "-").replace("－", "-")
        parts = [p.strip() for p in raw.split("-") if p.strip() and p.strip() not in ["未获取", "-", "--", "—"]]
        return "-".join(parts).strip("-").strip()

    def _clean_dept_name(v):
        """
        全局部门名称清洗：用于一级部门展示和比较。
        """
        t = extract_text(v, "").strip()
        if t in ["", "未获取", "-", "--", "—"]:
            return ""
        t = t.replace("—", "-").replace("－", "-")
        parts = [p.strip() for p in t.split("-") if p.strip() and p.strip() not in ["-", "--", "—", "未获取"]]
        return "-".join(parts).strip("-").strip()

    def build_dept_chain(fields_obj):
        d2 = normalize_dept_text(fields_obj.get("二级部门"))
        d3 = normalize_dept_text(fields_obj.get("三级部门"))
        d4 = normalize_dept_text(fields_obj.get("四级部门"))
        return "-".join([d for d in [d2, d3, d4] if d]).strip("-").strip()

    def action_button(action_type, label, key, use_container_width=True, disabled=False, on_click=None):
        """
        统一的文本按钮：填充色块 + 文字，配合 CSS 控制颜色与对齐。
        action_type 仅用于打上标记 class，具体样式在全局 CSS 中统一控制。
        """
        st.markdown(
            f"<div class='xqps-btn-{action_type}' style='width:0;height:0;overflow:hidden;'></div>",
            unsafe_allow_html=True,
        )
        return st.button(
            label,
            key=key,
            use_container_width=use_container_width,
            disabled=disabled,
            on_click=on_click,
        )

    # 1. 登录与获取飞书档案
    if not st.session_state.feishu_record_id and st.session_state.feishu_record_id != "NOT_FOUND":
        with st.spinner("正在同步您的飞书档案数据..."):
            current_open_id = st.session_state.user_info.get("open_id") or st.session_state.user_info.get("id")
            try:
                record = get_record_by_openid_safely(
                    APP_TOKEN,
                    TABLE_ID,
                    current_open_id,
                    fallback_name=st.session_state.user_info.get("name", ""),
                    fallback_emp_id=st.session_state.user_info.get("emp_id", ""),
                )
                if isinstance(record, dict):
                    fields = record.get("fields", {})
                    st.session_state.feishu_record = fields
                    st.session_state.feishu_record_id = record.get("record_id")
                    
                    if 'data_initialized' not in st.session_state:
                        fetched_goal_count = 3
                        for i in range(5, 3, -1):
                            if fields.get(f"工作目标{i}及总结") or fields.get(f"工作目标{i}权重", 0):
                                fetched_goal_count = i
                                break
                        st.session_state.goal_count = max(3, fetched_goal_count)
                        
                        for i in range(1, 6):
                            w = fields.get(f"工作目标{i}权重", 0)
                            try: w = int(float(w))
                            except: w = 0
                            st.session_state[f"obj_weight_{i}"] = w
                            st.session_state[f"obj_summary_{i}"] = fields.get(f"工作目标{i}及总结", "")
                            
                            sc = fields.get(f"工作目标{i}自评得分", 0.0)
                            try: sc = float(sc)
                            except: sc = 0.0
                            if sc not in SCORE_OPTIONS: sc = 0.0
                            st.session_state[f"obj_score_{i}"] = sc

                        st.session_state["comp_summary"] = fields.get("通用能力总结", "")
                        c_sc = fields.get("通用能力自评得分", 0.0)
                        try: c_sc = float(c_sc)
                        except: c_sc = 0.0
                        if c_sc not in SCORE_OPTIONS: c_sc = 0.0
                        st.session_state["comp_score"] = c_sc
                        
                        st.session_state["lead_summary"] = fields.get("领导力总结", "")
                        l_sc = fields.get("领导力自评得分", 0.0)
                        try: l_sc = float(l_sc)
                        except: l_sc = 0.0
                        if l_sc not in SCORE_OPTIONS: l_sc = 0.0
                        st.session_state["lead_score"] = l_sc
                        st.session_state.data_initialized = True
                else:
                    st.session_state.feishu_record_id = "NOT_FOUND"
                    st.toast("⚠️ 未在飞书找到此ID的档案。", icon="⚠️")
            except Exception as e:
                st.session_state.feishu_record_id = "NOT_FOUND"
                st.error(f"⚠️ 连接飞书异常: {e}")

    # 2. 准备基础数据与个人计算
    fields = st.session_state.feishu_record
    role_from_record = extract_text(fields.get("角色"), "").strip()
    if st.session_state.role not in ["员工", "管理者"]:
        st.session_state.role = role_from_record if role_from_record in ["员工", "管理者"] else "员工"
    is_submitted = (extract_text(fields.get("自评是否提交")).strip() == "是")
    
    user_name = st.session_state.user_info.get('name', '未知用户')
    emp_id = extract_text(fields.get('工号') or fields.get('员工工号'), st.session_state.user_info.get('emp_id', '未绑定'))
    job_title = extract_text(fields.get('岗位') or fields.get('职位'), st.session_state.user_info.get('job_title', '未分配'))
    current_cycle = (
        extract_text(fields.get("绩效考核周期"), "").strip()
        or extract_text(fields.get("考核周期"), "").strip()
        or extract_text(fields.get("本次绩效考核周期"), "").strip()
        or "2026上半年"
    )
    dept_parts = [d for d in [extract_text(fields.get(f'{k}级部门'), "") for k in ["一", "二", "三", "四"]] if d and d != "未获取"]
    department = "-".join(dept_parts) if dept_parts else "未获取"
    manager = extract_text(fields.get('直接评价人') or fields.get('评价人'))
    vp = extract_text(fields.get('分管高管') or fields.get('高管'))
    dept_head = extract_text(fields.get('一级部门负责人') or fields.get('部门负责人'))
    hrbp = extract_text(fields.get('HRBP') or fields.get('HRBP Lead'))

    # -- 本人自评算分验证逻辑 (前置计算) --
    target_weight = 60 if st.session_state.role == "管理者" else 80
    total_weight = sum(st.session_state.get(f"obj_weight_{i}", 0) for i in range(1, st.session_state.goal_count + 1))
    
    empty_summaries, too_short_summaries, too_long_summaries, unscored_goals = [], [], [], []
    for i in range(1, st.session_state.goal_count + 1):
        text_len = len(st.session_state.get(f"obj_summary_{i}", "").strip())
        if text_len == 0: empty_summaries.append(i)
        elif 0 < text_len < 100: too_short_summaries.append(i)
        elif text_len > 5000: too_long_summaries.append(i)
        if st.session_state.get(f"obj_score_{i}", 0.0) == 0.0: unscored_goals.append(i)
            
    comp_text_len = len(st.session_state.get("comp_summary", "").strip())
    comp_empty, comp_too_short, comp_too_long, comp_unscored = (comp_text_len == 0), (0 < comp_text_len < 100), (comp_text_len > 5000), (st.session_state.get("comp_score", 0.0) == 0.0)
    lead_empty, lead_too_short, lead_too_long, lead_unscored = False, False, False, False
    if st.session_state.role == "管理者":
        lead_text_len = len(st.session_state.get("lead_summary", "").strip())
        lead_empty, lead_too_short, lead_too_long, lead_unscored = (lead_text_len == 0), (0 < lead_text_len < 100), (lead_text_len > 5000), (st.session_state.get("lead_score", 0.0) == 0.0)

    weight_valid = (total_weight == target_weight)
    summaries_valid = not (empty_summaries or too_short_summaries or too_long_summaries or comp_empty or comp_too_short or comp_too_long or lead_empty or lead_too_short or lead_too_long)
    scores_valid = not (unscored_goals or comp_unscored or lead_unscored)
    step1_can_submit = weight_valid and summaries_valid and scores_valid

    current_self_score = 0.0
    for i in range(1, st.session_state.goal_count + 1):
        current_self_score += st.session_state.get(f"obj_score_{i}", 0.0) * (st.session_state.get(f"obj_weight_{i}", 0) / 100.0)
    current_self_score += st.session_state.get("comp_score", 0.0) * 0.20
    if st.session_state.role == "管理者": current_self_score += st.session_state.get("lead_score", 0.0) * 0.20
    current_self_score = round(current_self_score, 2)
    self_grade = calculate_grade(current_self_score)

    # 3. 若为管理者则拉取下属数据
    real_subordinates = []
    my_all_subs = []
    all_records_snapshot = []
    is_dept_head = False
    is_vp = False
    if st.session_state.role == "管理者" and is_submitted:
        with st.spinner("正在拉取团队数据..."):
            all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
            all_records_snapshot = all_records
            for record in all_records:
                rec_fields = record.get("fields", {})
                rec_manager = extract_text(rec_fields.get("直接评价人") or rec_fields.get("评价人"))
                if user_name in rec_manager:
                    my_all_subs.append(record)
                    if extract_text(rec_fields.get("自评是否提交")).strip() == "是":
                        real_subordinates.append(record)

            # 识别当前用户是否为一级部门负责人 / 分管高管
            for record in all_records:
                rec_fields = record.get("fields", {})
                dept_head_str = extract_text(rec_fields.get("一级部门负责人"), "").strip()
                vp_str = extract_text(rec_fields.get("分管高管") or rec_fields.get("高管"), "").strip()
                if user_name and user_name in dept_head_str:
                    is_dept_head = True
                if user_name and user_name in vp_str:
                    is_vp = True

    # 4. 侧边栏渲染 (从上到下严格顺序)
    st.sidebar.markdown(f"### 👋 欢迎 {user_name}！")
    st.sidebar.markdown("---")

    st.sidebar.markdown("### 📅 绩效考核周期")
    st.sidebar.markdown(f"""
        <div style="background-color: rgba(38, 39, 48, 0.8); padding: 15px; border-radius: 8px; border: 1px solid #333;">
            <div style="margin-bottom: 10px; color: #b0b0b0; font-size: 14px;">{current_cycle}</div>
            <div style="margin-bottom: 10px; color: #b0b0b0; font-size: 14px;">直接评价人：{manager}</div>
            <div style="margin-bottom: 10px; color: #b0b0b0; font-size: 14px;">一级部门负责人：{dept_head}</div>
            <div style="margin-bottom: 10px; color: #b0b0b0; font-size: 14px;">分管高管：{vp}</div>
            <div style="color: #b0b0b0; font-size: 14px;">HRBP： {hrbp}</div>
        </div>
        """, unsafe_allow_html=True)

    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:12px 0;'/>", unsafe_allow_html=True)

    st.sidebar.markdown("### ℹ️ 员工信息")
    dept_display = " 丨 ".join(dept_parts) if dept_parts else "未获取"
    st.sidebar.markdown(f"""
        <div style="background-color: rgba(38, 39, 48, 0.8); padding: 15px; border-radius: 8px; border: 1px solid #333;">
            <div style="margin-bottom: 10px; color: #b0b0b0; font-size: 14px;">{user_name} 丨 {emp_id}</div>
            <div style="margin-bottom: 10px; color: #b0b0b0; font-size: 14px;">{dept_display}</div>
            <div style="color: #b0b0b0; font-size: 14px;">{job_title} | {st.session_state.role}</div>
        </div>
        """, unsafe_allow_html=True)
    st.sidebar.markdown("---")

    is_evaluating_sub = (st.session_state.role == "管理者" and st.session_state.selected_subordinate_id is not None)
    step2_can_submit = False

    # --- 统一验证模块区域 (紧贴个人信息下方) ---
    if is_evaluating_sub:
        # 【管理者评估下属的验证模块】
        sub_id_str = st.session_state.selected_subordinate_id
        current_sub = next((s for s in real_subordinates if s["record_id"] == sub_id_str), None)
        if current_sub:
            sub_f = current_sub.get("fields", {})
            
            # 计算工作目标总权重
            sub_weight_sum = 0
            for i in range(1, 6):
                w = sub_f.get(f"工作目标{i}权重", 0)
                try: sub_weight_sum += int(float(w))
                except: pass
            
            # 判断是否包含领导力模块
            sub_role = extract_text(sub_f.get("角色", "")).strip() 
            has_leadership = (sub_role == "管理者") 
            
            # 检查各项模块是否已打分
            val_work = st.session_state.get(f"mgr_work_score_{sub_id_str}")
            if val_work is None: val_work = float(sub_f.get("工作目标上级评分", 0.0))
            mgr_work_unscored = (val_work == 0.0)
            
            val_comp = st.session_state.get(f"mgr_comp_score_{sub_id_str}")
            if val_comp is None: val_comp = float(sub_f.get("通用能力上级评分", 0.0))
            mgr_comp_unscored = (val_comp == 0.0)
            
            val_lead = 0.0
            if has_leadership:
                val_lead_raw = st.session_state.get(f"mgr_lead_score_{sub_id_str}")
                if val_lead_raw is None: val_lead = float(sub_f.get("领导力上级评分", 0.0))
                else: val_lead = val_lead_raw
            mgr_lead_unscored = has_leadership and (val_lead == 0.0)
            
            val_comment = st.session_state.get(f"mgr_comment_{sub_id_str}")
            if val_comment is None:
                val_comment = extract_text(sub_f.get("考核评语", ""), "")
            val_comment = str(val_comment).strip()
            mgr_comment_empty = (len(val_comment) == 0) or (val_comment in ["0", "未获取", "None"])
            
            step2_can_submit = not (mgr_work_unscored or mgr_comp_unscored or mgr_lead_unscored or mgr_comment_empty)
            
            st.sidebar.markdown("### 🚦 验证模块")
            if step2_can_submit:
                st.sidebar.success("✅ 该下属所有评分项与评语已填写完整")
            else:
                if mgr_work_unscored: st.sidebar.warning("⚠️ 工作目标整体未打分")
                if mgr_comp_unscored: st.sidebar.warning("⚠️ 通用能力未打分")
                if mgr_lead_unscored: st.sidebar.warning("⚠️ 领导力模块未打分")
                if mgr_comment_empty: st.sidebar.warning("⚠️ 考核评语未填写")
                
            if sub_weight_sum not in [60, 80]:
                st.sidebar.error(f"❌ 预警: 该下属设定的工作总权重为 {sub_weight_sum}%，不符合标准规范。")
            st.sidebar.markdown("---")
            
    else:
        # 【本人自评的验证模块与结果展示】
        st.sidebar.markdown("### 🚦 验证模块")
        
        if step1_can_submit:
            st.sidebar.success("✅ 您的所有自评项与权重已填写完整")
        else:
            if not weight_valid: 
                st.sidebar.error(f"❌ 工作权重需为 {target_weight}%，当前: {total_weight}%")
                
            if empty_summaries: st.sidebar.warning(f"⚠️ 目标未填内容: 目标 {', '.join(map(str, empty_summaries))}")
            if comp_empty: st.sidebar.warning("⚠️ 通用能力未填内容")
            if lead_empty: st.sidebar.warning("⚠️ 领导力未填内容")
            if unscored_goals: st.sidebar.warning(f"⚠️ 目标未打分: 目标 {', '.join(map(str, unscored_goals))}")
            if comp_unscored: st.sidebar.warning("⚠️ 通用能力未打分")
            if lead_unscored: st.sidebar.warning("⚠️ 领导力未打分")
            if too_short_summaries: st.sidebar.warning(f"⚠️ 字数不足(≥100字): 目标 {', '.join(map(str, too_short_summaries))}")
            if too_long_summaries: st.sidebar.error(f"❌ 字数超限: 目标 {', '.join(map(str, too_long_summaries))}")
            if comp_too_short: st.sidebar.warning("⚠️ 通用能力总结字数不足(≥100字)")
            if lead_too_short: st.sidebar.warning("⚠️ 领导力维度字数不足(≥100字)")
            
        st.sidebar.markdown("---")
        
        st.sidebar.markdown("### 📊 结果模块")
        st.sidebar.markdown(f"""
        <div style="background-color: #262730; padding: 15px; border-radius: 8px; border: 1px solid #333;">
            <div style="margin-bottom: 12px; color: #FAFAFA; font-size: 15px;">
                <span style="display:inline-block; width: 75px; color: #b0b0b0;">自评得分：</span> 
                <span style="color: #1E90FF; font-weight: bold; font-size: 18px;">{current_self_score}</span>
            </div>
            <div style="margin-bottom: 12px; color: #FAFAFA; font-size: 15px;">
                <span style="display:inline-block; width: 75px; color: #b0b0b0;">自评等级：</span> 
                <span style="color: #1E90FF; font-weight: bold; font-size: 18px;">{self_grade}</span>
            </div>
            <div style="margin-bottom: 12px; color: #757575; font-size: 15px;">
                <span style="display:inline-block; width: 105px;">最终考核结果：</span> 待审批
            </div>
            <div style="color: #757575; font-size: 15px;">
                <span style="display:inline-block; width: 105px;">考核评语：</span> 待审批
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.sidebar.markdown("---")

    # --- 开发者工具（默认关闭） ---
    if ENABLE_DEV_TOOLS and not IS_PROD:
        st.sidebar.markdown("### 🛠️ 开发者工具")
        if st.sidebar.button("🔄 重置提交状态 (解锁表单)", use_container_width=True):
            if st.session_state.feishu_record_id and st.session_state.feishu_record_id != "NOT_FOUND":
                with st.spinner("强制解锁中..."):
                    update_record_safely(APP_TOKEN, TABLE_ID, st.session_state.feishu_record_id, {"自评是否提交": None})
                    st.session_state.feishu_record["自评是否提交"] = ""
                    st.session_state.selected_subordinate_id = None
                    time.sleep(1)
                    st.rerun()
    if st.sidebar.button("🚪 退出登录", use_container_width=True):
        st.session_state.clear()
        st.rerun()

    # 5. 主体内容区渲染 (动态权限版)
    # 导航改版：取消「综合调整」，将「一级部门负责人调整」「分管高管调整」提升为一级导航
    # 展示逻辑：一级部门负责人只看到「一级部门负责人调整」；分管高管+一级部门负责人看到两个；仅分管高管只看到「分管高管调整」
    if st.session_state.role == "管理者":
        tab_list = ["📝 员工自评", "👥 上级评分"]
        if is_dept_head:
            tab_list.append("📌 一级部门负责人调整")
        if is_vp:
            tab_list.append("📌 分管高管调整")
        tab_list.append("📊 视图与报表")  # 原「公司审批」，先作为视图与报表占位
        tab_list.append("📂 历史信息")
    else:
        # 员工个人只看两个标签
        tab_list = ["📝 员工自评", "📂 历史信息"]

    tabs = st.tabs(tab_list)
    idx_self = 0
    idx_mgr = 1 if st.session_state.role == "管理者" else 0
    idx_dept_head = tab_list.index("📌 一级部门负责人调整") if "📌 一级部门负责人调整" in tab_list else None
    idx_vp = tab_list.index("📌 分管高管调整") if "📌 分管高管调整" in tab_list else None
    idx_reports = tab_list.index("📊 视图与报表") if "📊 视图与报表" in tab_list else None

    # ==========================================
    # 🟢 模块 1：员工自评 (索引 0)
    # ==========================================
    with tabs[idx_self]:
        if "ui_comp_summary" not in st.session_state:
            st.session_state["ui_comp_summary"] = extract_text(fields.get("通用能力总结", ""))
        if "ui_comp_score" not in st.session_state:
            c_score = fields.get("通用能力自评得分", 0.0)
            st.session_state["ui_comp_score"] = float(c_score) if c_score else 0.0
            
        if "ui_lead_summary" not in st.session_state:
            st.session_state["ui_lead_summary"] = extract_text(fields.get("领导力总结", ""))
        if "ui_lead_score" not in st.session_state:
            l_score = fields.get("领导力自评得分", 0.0)
            st.session_state["ui_lead_score"] = float(l_score) if l_score else 0.0
        
        st.markdown("<div class='hero-title'>🎯 当前绩效目标设定与自评</div>", unsafe_allow_html=True)
        if is_submitted:
            st.success("🔒 您的自评已提交，当前表单不可修改。")
        
        st.markdown("<div class='module-title'>💼 工作模块</div>", unsafe_allow_html=True)
        st.info(f"💡 提示：工作模块总体占比 {target_weight}% (各目标权重之和必须等于 {target_weight}%)")
        hint_placeholder = "如需更大操作区域，可拖动文本框右下角放大区域。"

        for i in range(1, st.session_state.goal_count + 1):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.text_area(
                    f"工作目标{i}及总结",
                    height=110,
                    disabled=is_submitted,
                    key=f"obj_summary_{i}",
                    placeholder=hint_placeholder,
                )
            with col_right:
                st.number_input(f"工作目标{i}权重(%)", min_value=0, max_value=100, step=5, disabled=is_submitted, key=f"obj_weight_{i}")
                st.selectbox(f"工作目标{i}自评得分", options=SCORE_OPTIONS, disabled=is_submitted, key=f"obj_score_{i}")
            st.markdown("---")
            
        if not is_submitted:
            col_add, col_del = st.columns(2)
            with col_add:
                if st.session_state.goal_count < 5:
                    if st.button("➕ 添加工作目标", use_container_width=True):
                        st.session_state.goal_count += 1
                        st.rerun()
            with col_del:
                if st.session_state.goal_count > 3:
                    if st.button("➖ 删除最后目标", use_container_width=True):
                        st.session_state.pop(f"obj_summary_{st.session_state.goal_count}", None)
                        st.session_state.pop(f"obj_weight_{st.session_state.goal_count}", None)
                        st.session_state.pop(f"obj_score_{st.session_state.goal_count}", None)
                        st.session_state.goal_count -= 1
                        st.rerun()

        st.markdown("<div class='module-title'>🧠 能力模块</div>", unsafe_allow_html=True)
        if st.session_state.role == "员工":
            st.info("💡 提示：通用能力占比 20%")
            col_comp_left, col_comp_right = st.columns([3, 1])
            with col_comp_left:
                st.text_area(
                    "结合考核期工作实际情况，从「思考、行动、写作、成长」四个维度总结",
                    height=110,
                    disabled=is_submitted,
                    key="comp_summary",
                    placeholder=hint_placeholder,
                )
            with col_comp_right: st.selectbox("通用能力自评得分", options=SCORE_OPTIONS, disabled=is_submitted, key="comp_score")
        elif st.session_state.role == "管理者":
            st.info("💡 提示：通用能力占比 20%、领导力占比 20%")
            col_cap_left, col_cap_right = st.columns(2)
            with col_cap_left:
                st.text_area(
                    "结合考核期工作实际情况，从「思考、行动、写作、成长」四个维度总结",
                    height=110,
                    disabled=is_submitted,
                    key="comp_summary",
                    placeholder=hint_placeholder,
                )
                st.selectbox("通用能力自评得分", options=SCORE_OPTIONS, disabled=is_submitted, key="comp_score")
            with col_cap_right:
                st.text_area(
                    "请结合考核周期工作实际情况，从「领导力」维度进行阐述与总结",
                    height=110,
                    disabled=is_submitted,
                    key="lead_summary",
                    placeholder=hint_placeholder,
                )
                st.selectbox("领导力自评得分", options=SCORE_OPTIONS, disabled=is_submitted, key="lead_score")
                
        if not is_submitted:
            st.markdown("---")
            col_submit, col_save = st.columns(2)
            payload_data = {}
            for idx in range(1, st.session_state.goal_count + 1):
                payload_data[f"工作目标{idx}及总结"] = st.session_state.get(f"obj_summary_{idx}", "")
                payload_data[f"工作目标{idx}权重"] = st.session_state.get(f"obj_weight_{idx}", 0)
                payload_data[f"工作目标{idx}自评得分"] = st.session_state.get(f"obj_score_{idx}", 0.0)
            
            payload_data["通用能力总结"] = st.session_state.get("comp_summary", "")
            payload_data["通用能力自评得分"] = st.session_state.get("comp_score", 0.0)
            if st.session_state.role == "管理者":
                payload_data["领导力总结"] = st.session_state.get("lead_summary", "")
                payload_data["领导力自评得分"] = st.session_state.get("lead_score", 0.0)
            
            payload_data["自评得分"] = current_self_score
            payload_data["自评等级"] = self_grade

            with col_submit:
                if st.button("确认提交", type="primary", use_container_width=True, disabled=not step1_can_submit):
                    with st.spinner("锁定表单更新至飞书..."):
                        submit_data = payload_data.copy()
                        submit_data["自评是否提交"] = "是"
                        success, error_msg = update_record_safely(APP_TOKEN, TABLE_ID, st.session_state.feishu_record_id, submit_data)
                        if success:
                            st.success("✅ 提交成功！")
                            for k, v in submit_data.items(): st.session_state.feishu_record[k] = v
                            st.balloons()  
                            time.sleep(1.5)
                            st.rerun()
                        else: st.error(f"❌ 锁定失败！{error_msg}")
                        
            with col_save:
                st.markdown("<div class='save-marker'></div>", unsafe_allow_html=True)
                if st.button("保存草稿", use_container_width=True, disabled=not step1_can_submit):
                    with st.spinner("同步数据至飞书..."):
                        success, error_msg = update_record_safely(APP_TOKEN, TABLE_ID, st.session_state.feishu_record_id, payload_data)
                        if success:
                            for k, v in payload_data.items(): st.session_state.feishu_record[k] = v
                            st.success("✅ 草稿已成功保存至飞书！")
                        else: st.error(f"❌ 暂存失败！{error_msg}")
                        
            st.info("💡 提示：点击「确认提交」即意味着本次自评结束，不可再修改。")        

    # ==========================================
    # 🟢 模块 2：管理者专属权限 
    # ==========================================
    if st.session_state.role == "管理者":
        with tabs[idx_mgr]:
            if is_submitted:
                # --- 1. 下属评估进展看板 ---
                total_subs = len(my_all_subs)
                submitted_subs = len(real_subordinates)
                
                rated_subs = 0
                drafted_subs = 0
                grade_list = []
                
                for sub in real_subordinates:
                    sub_f = sub.get("fields", {})
                    is_mgr_done = extract_text(sub_f.get("上级评价是否完成")).strip() == "是"
                    current_grade = extract_text(sub_f.get("考核结果")).strip()
                    
                    if is_mgr_done:
                        rated_subs += 1
                    elif current_grade and current_grade not in ["", "未获取", "-"]:
                        drafted_subs += 1
                        
                    if current_grade and current_grade not in ["", "未获取", "-"]:
                        grade_list.append(current_grade)
                        
                unrated_subs = submitted_subs - rated_subs - drafted_subs
                

                # 模块 1：下属评估进展（单列）- 样式参照实际人数表格：16px 粗体，标签灰 #b7bdc8，数字配色
                st.markdown("<div class='module-title'>👥 下属评估进展</div>", unsafe_allow_html=True)
                st.markdown(
                    f"""
                    <div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;">
                        <div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;">
                            <span style="color:#b7bdc8;">总下属：<span style="color:#4CAFEE;">{total_subs}</span> 人</span>
                            <span style="color:#b7bdc8;">已交自评：<span style="color:#8BC34A;">{submitted_subs}</span> 人</span>
                            <span style="color:#b7bdc8;">已评：<span style="color:#00BCD4;">{rated_subs}</span> 人</span>
                            <span style="color:#b7bdc8;">暂存：<span style="color:#FFC107;">{drafted_subs}</span> 人</span>
                            <span style="color:#b7bdc8;">待评：<span style="color:#F44336;">{unrated_subs}</span> 人</span>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.markdown("---")

                # 模块 3：下属评估名单（单列容器，内部用列展示字段）
                st.markdown("<div class='module-title'>👇 下属评估名单</div>", unsafe_allow_html=True)
                if not my_all_subs:
                    st.info("💡 提示：当前暂无已提交自评的下属。")
                else:
                    # 顶部筛选：与综合调整统一（工号姓名 / 部门 / 状态 / 考核等级）
                    f1, f2, f3, f4 = st.columns(4, gap="small")
                    q_name_emp = f1.text_input(
                        "搜索工号、姓名",
                        placeholder="🔎 搜索工号、姓名",
                        key="mgr_filter_name_emp",
                        label_visibility="collapsed",
                    )
                    dept_options = set()
                    for s in my_all_subs:
                        ff = s.get("fields", {})
                        _chain = build_dept_chain(ff) or normalize_dept_text(ff.get("一级部门"))
                        if _chain:
                            dept_options.add(_chain)
                    dept_options = sorted(dept_options)
                    q_dept = f2.selectbox(
                        "部门",
                        ["全部部门"] + dept_options,
                        key="mgr_filter_dept",
                        label_visibility="collapsed",
                    )
                    q_status = f3.selectbox(
                        "状态",
                        ["全部状态", "未自评", "待评价", "暂存", "已完成"],
                        key="mgr_filter_status",
                        label_visibility="collapsed",
                    )
                    q_mgr_grade = f4.selectbox(
                        "考核等级",
                        ["全部考核等级"] + GRADE_OPTIONS + ["-"],
                        key="mgr_filter_grade",
                        label_visibility="collapsed",
                    )
                    sort_order = {"S": 1, "A": 2, "B+": 3, "B": 4, "B-": 5, "C": 6}
                    q1 = q_name_emp.strip().lower()
                    q2 = q_dept.strip().lower()
                    filtered_subs = []
                    for s in my_all_subs:
                        f = s.get("fields", {})
                        n = extract_text(f.get("姓名"), "").strip()
                        e = extract_text(f.get("工号") or f.get("员工工号"), "").strip()
                        dept_chain = build_dept_chain(f) or normalize_dept_text(f.get("一级部门")) or "未分配部门"
                        s_submitted = extract_text(f.get("自评是否提交")).strip() == "是"
                        s_mgr_done = extract_text(f.get("上级评价是否完成")).strip() == "是"
                        current_grade_filter = extract_text(f.get("考核结果", "-")).strip() or "-"
                        is_draft_filter = (not s_mgr_done) and (current_grade_filter in GRADE_OPTIONS)
                        if not s_submitted:
                            status_str = "未自评"
                        elif s_mgr_done:
                            status_str = "已完成"
                        elif is_draft_filter:
                            status_str = "暂存"
                        else:
                            status_str = "待评价"

                        if q1 and (q1 not in n.lower() and q1 not in e.lower()):
                            continue
                        if q_dept != "全部部门" and (q2 not in dept_chain.lower()):
                            continue
                        if q_status != "全部状态" and q_status != status_str:
                            continue
                        if q_mgr_grade != "全部考核等级":
                            has_grade = current_grade_filter in GRADE_OPTIONS
                            if q_mgr_grade == "-" and has_grade:
                                continue
                            if q_mgr_grade in GRADE_OPTIONS and current_grade_filter != q_mgr_grade:
                                continue
                        filtered_subs.append(s)
                    has_filter = bool(q1) or q_dept != "全部部门" or q_status != "全部状态" or q_mgr_grade != "全部考核等级"
                    if not filtered_subs and has_filter:
                        st.caption("未找到匹配的下属")
                    st.markdown("<div class='mgr-sub-list'>", unsafe_allow_html=True)
                    # 表头：与综合调整风格统一（多行展示）
                    h1, h2, h3, h4, h5, h6 = st.columns([2.2, 3.2, 1.2, 1.2, 1.2, 2.0])
                    h1.markdown("<div class='sub-list-head'>姓名（工号）</div>", unsafe_allow_html=True)
                    h2.markdown("<div class='sub-list-head'>子部门/岗位</div>", unsafe_allow_html=True)
                    h3.markdown("<div class='sub-list-head'>自评等级</div>", unsafe_allow_html=True)
                    h4.markdown("<div class='sub-list-head'>考核等级</div>", unsafe_allow_html=True)
                    h5.markdown("<div class='sub-list-head'>状态</div>", unsafe_allow_html=True)
                    h6.markdown("<div class='sub-list-head'>操作</div>", unsafe_allow_html=True)
                    st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)

                    for sub in filtered_subs:
                        sub_f = sub.get("fields", {})
                        s_emp_id = extract_text(sub_f.get("工号") or sub_f.get("员工工号"), "未知工号")
                        s_name = extract_text(sub_f.get("姓名"), "未知姓名")
                        s_job = extract_text(sub_f.get("岗位") or sub_f.get("职位"), "未分配")
                        s_id = sub.get("record_id")

                        self_grade = extract_text(sub_f.get("自评等级", "-")).strip()
                        current_grade = extract_text(sub_f.get("考核结果")).strip()
                        is_mgr_done = extract_text(sub_f.get("上级评价是否完成")).strip() == "是"
                        is_self_submitted = extract_text(sub_f.get("自评是否提交")).strip() == "是"

                        grade_diff = 0
                        if self_grade in sort_order and current_grade in sort_order:
                            grade_diff = abs(sort_order[current_grade] - sort_order[self_grade])

                        warning_icon = ""
                        if grade_diff >= 2:
                            warning_icon = "<span title='请注意：你的评分与员工自评分差异较大' style='cursor:help; font-size:12px; margin-left:2px; color:#ff5252;'>ⓘ</span>"

                        # 状态与按钮类型：区分「未自评 / 暂存 / 已完成 / 待评价」
                        is_draft = (not is_mgr_done) and (current_grade and current_grade not in ["", "未获取", "-"])
                        if not is_self_submitted:
                            status_html = "<span style='color:#FFA500;'>未自评</span>"
                            action_type = "remind"
                        elif is_mgr_done:
                            color = "#00e676"
                            status_html = f"<span style='color:{color}; font-weight:800;'>已完成{warning_icon}</span>"
                            action_type = "view"
                        elif is_draft:
                            # 已有考核等级但未提交，视为暂存
                            status_html = f"<span style='color:#FFA500;'>暂存{warning_icon}</span>"
                            action_type = "adjust"
                        else:
                            status_html = "<span style='color:#1E90FF;'>待评价</span>"
                            action_type = "evaluate"

                        # 自评等级：未自评显示 -，已自评直接显示等级
                        disp_grade = "-" if not is_self_submitted else (self_grade if self_grade not in ["", "未获取", "None"] else "-")
                        # 考核等级：已完成 ✅，暂存 💾，否则 -
                        if current_grade and current_grade not in ["", "未获取", "-"]:
                            if is_mgr_done:
                                disp_mgr_grade = f"✅ {current_grade}"
                            elif is_draft:
                                disp_mgr_grade = f"💾 {current_grade}"
                            else:
                                disp_mgr_grade = current_grade
                        else:
                            disp_mgr_grade = "-"

                        dept_chain = build_dept_chain(sub_f) or normalize_dept_text(sub_f.get("一级部门")) or "未分配部门"
                        c1, c2, c3, c4, c5, c6 = st.columns([2.2, 3.2, 1.2, 1.2, 1.2, 2.0], vertical_alignment="center")
                        c1.markdown(
                            f"<div class='sub-list-cell' style='color:#E0E0E0; white-space:normal;'><b>{s_name}</b><br>（{s_emp_id}）</div>",
                            unsafe_allow_html=True,
                        )
                        c2.markdown(
                            f"<div class='sub-list-cell' style='color:#b0b0b0; white-space:normal;' title='{dept_chain} | {s_job}'>{dept_chain}<br>{s_job}</div>",
                            unsafe_allow_html=True,
                        )
                        c3.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0;'>{disp_grade}</div>", unsafe_allow_html=True)
                        c4.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0;'>{disp_mgr_grade}</div>", unsafe_allow_html=True)
                        c5.markdown(f"<div class='sub-list-cell'>{status_html}</div>", unsafe_allow_html=True)

                        with c6:
                            if action_type == "remind":
                                if action_button("remind", "提醒", key=f"btn_remind_{s_id}"):
                                    st.toast("飞书机器人提醒功能开发中，敬请期待", icon="🔔")
                            elif action_type == "view":
                                if action_button("view", "查看", key=f"btn_view_{s_id}"):
                                    jump_to_subordinate(s_id)
                                    st.rerun()
                            elif action_type == "adjust":
                                if action_button("adjust", "去调整", key=f"btn_adjust_{s_id}"):
                                    jump_to_subordinate(s_id)
                                    st.rerun()
                            else:
                                if action_button("evaluate", "去评价", key=f"btn_jump_{s_id}"):
                                    jump_to_subordinate(s_id)
                                    st.rerun()
                        st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)

                    st.markdown("</div>", unsafe_allow_html=True)

                st.markdown("---")

                # --- 3. 管理者评估下属的具体表单 ---
                if is_evaluating_sub:
                    current_sub = next((s for s in real_subordinates if s["record_id"] == st.session_state.selected_subordinate_id), None)
                    if current_sub:
                        sub_f = current_sub.get("fields", {})
                        sub_id_str = current_sub["record_id"]
                        disp_emp_id = extract_text(sub_f.get("工号") or sub_f.get("员工工号"), "未知工号")
                        disp_name = extract_text(sub_f.get("姓名"), "未知姓名")
                        disp_job = extract_text(sub_f.get("岗位") or sub_f.get("职位"), "未分配")
                        disp_score = str(sub_f.get("自评得分", "暂无")) 
                        disp_grade = str(sub_f.get("自评等级", "暂无")) 
                        disp_last_perf = extract_text(sub_f.get("上一次绩效考试结果", "暂无"))
                        
                        st.markdown("---")
                        col_head1, col_head2 = st.columns([3, 1])
                        with col_head1:
                            st.markdown(f"<div class='module-title'>📝 正在评估：{disp_name}</div>", unsafe_allow_html=True)
                            st.markdown(
                                f"""
                                <div style='font-size:14px; color:#E0E0E0; margin-top:6px; line-height:1.6;'>
                                    <div>工号：{disp_emp_id}</div>
                                    <div>岗位：{disp_job}</div>
                                    <div>自评得分：{disp_score}</div>
                                    <div>自评等级：{disp_grade}</div>
                                    <div>上一次绩效：{disp_last_perf}</div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                        with col_head2:
                            action_button("collapse", "收起面板", key=f"btn_collapse_panel_{sub_id_str}", on_click=return_to_self)
                        st.write("")
                        
                        st.markdown("<div class='module-title'>💼 工作模块展示与评分</div>", unsafe_allow_html=True)
                        sub_weight_sum = 0
                        
                        sub_goal_count = 3
                        for i in range(5, 3, -1):
                            if sub_f.get(f"工作目标{i}及总结") or sub_f.get(f"工作目标{i}权重", 0):
                                sub_goal_count = i
                                break
                        
                        for i in range(1, sub_goal_count + 1): 
                            sub_obj_text = sub_f.get(f"工作目标{i}及总结", f"未填写工作目标{i}")
                            raw_weight = sub_f.get(f"工作目标{i}权重", 0)
                            try: sub_weight = int(float(raw_weight))
                            except: sub_weight = 0
                            raw_score = sub_f.get(f"工作目标{i}自评得分", 0.0)

                            st.markdown(f"**🎯 工作目标 {i}** <span style='font-size:14px; color:#888;'>(权重: {sub_weight}% | 自评: {raw_score}分)</span>", unsafe_allow_html=True)
                            st.text_area("隐藏标签", value=sub_obj_text, height=80, disabled=True, key=f"ui_sub_obj_{i}_{sub_id_str}", label_visibility="collapsed")
                            st.write("")
                            sub_weight_sum += sub_weight
                        
                        saved_work_score = sub_f.get("工作目标上级评分", 0.0)
                        try: work_idx = SCORE_OPTIONS.index(float(saved_work_score))
                        except: work_idx = 0
                        
                        st.info(f"💡 提示：该下属工作目标总权重为 **{sub_weight_sum}%**")
                        mgr_work_score = st.selectbox("🌟 工作目标整体上级评分", options=SCORE_OPTIONS, index=work_idx, key=f"mgr_work_score_{sub_id_str}")
                        st.markdown("---")

                        st.markdown("<div class='module-title'>🧠 通用能力模块展示与评分</div>", unsafe_allow_html=True)
                        sub_comp_text = sub_f.get("通用能力总结", "未填写")
                        sub_comp_score = sub_f.get("通用能力自评得分", 0.0)
                        saved_comp_score = sub_f.get("通用能力上级评分", 0.0)
                        try: comp_idx = SCORE_OPTIONS.index(float(saved_comp_score))
                        except: comp_idx = 0

                        st.markdown(f"**🧠 通用能力总结** <span style='font-size:14px; color:#888;'>(自评: {sub_comp_score}分)</span>", unsafe_allow_html=True)
                        st.text_area("隐藏标签", value=sub_comp_text, height=100, disabled=True, key=f"ui_sub_comp_{sub_id_str}", label_visibility="collapsed")
                        st.write("")
                        
                        mgr_comp_score = st.selectbox("🌟 通用能力上级评分", options=SCORE_OPTIONS, index=comp_idx, key=f"mgr_comp_score_{sub_id_str}")
                        
                        sub_role = extract_text(sub_f.get("角色", "")).strip() 
                        has_leadership = (sub_role == "管理者") 
                        
                        mgr_lead_score = 0.0
                        if has_leadership:
                            sub_lead_text = extract_text(sub_f.get("领导力总结", ""))
                            sub_lead_score = sub_f.get("领导力自评得分", 0.0)
                            saved_lead_score = sub_f.get("领导力上级评分", 0.0)
                            try: lead_idx = SCORE_OPTIONS.index(float(saved_lead_score))
                            except: lead_idx = 0
                            
                            st.markdown("<div class='module-title'>👑 领导力模块展示与评分</div>", unsafe_allow_html=True)
                            st.markdown(f"**👑 领导力总结** <span style='font-size:14px; color:#888;'>(自评: {sub_lead_score}分)</span>", unsafe_allow_html=True)
                            st.text_area("隐藏标签", value=sub_lead_text, height=100, disabled=True, key=f"ui_sub_lead_{sub_id_str}", label_visibility="collapsed")
                            st.write("")
                            
                            mgr_lead_score = st.selectbox("🌟 领导力上级评分", options=SCORE_OPTIONS, index=lead_idx, key=f"mgr_lead_score_{sub_id_str}")
                        st.markdown("---")
                        comp_weight = 20 if has_leadership else (100 - sub_weight_sum)
                        lead_weight = 20 if has_leadership else 0
                        
                        current_total_score = (mgr_work_score * (sub_weight_sum / 100.0)) + \
                                              (mgr_comp_score * (comp_weight / 100.0)) + \
                                              (mgr_lead_score * (lead_weight / 100.0))
                                              
                        current_total_score = round(current_total_score, 2)
                        current_grade = calculate_grade(current_total_score)
                        
                        st.markdown(
                            f"<div class='module-title'>📈 考核总得分预览：{current_total_score} 分 ｜ 绩效等级：{current_grade}</div>",
                            unsafe_allow_html=True,
                        )
                        st.caption(f"(工作模块权重 {sub_weight_sum}%，能力模块权重 {comp_weight}%)")
                        
                        saved_comment_raw = extract_text(sub_f.get("考核评语", ""), "").strip()
                        saved_comment = saved_comment_raw if saved_comment_raw and saved_comment_raw not in ["未获取", "None", "0"] else ""
                        st.text_area("✍️ 考核评语", value=saved_comment, height=100, placeholder="请输入对该下属的整体评价...", key=f"mgr_comment_{sub_id_str}")
                        
                        sub_update_data = {
                            "考核得分": current_total_score,
                            "考核结果": current_grade,
                            "考核评语": st.session_state.get(f"mgr_comment_{sub_id_str}", ""),
                            "工作目标上级评分": st.session_state.get(f"mgr_work_score_{sub_id_str}", 0.0),
                            "通用能力上级评分": st.session_state.get(f"mgr_comp_score_{sub_id_str}", 0.0)
                        }

                        if has_leadership:
                            sub_update_data["领导力上级评分"] = st.session_state.get(f"mgr_lead_score_{sub_id_str}", 0.0)

                        st.markdown("---")
                        col_sub_submit, col_sub_save = st.columns(2)
                        
                        with col_sub_submit:
                            if st.button("✅ 确认提交打分", type="primary", use_container_width=True, disabled=not step2_can_submit):
                                with st.spinner("正在提交并锁定该下属绩效..."):
                                    final_data = sub_update_data.copy()
                                    final_data["上级评价是否完成"] = "是"
                                    # 一级部门负责人无法在一级部门环节调节自己，提交时直接写入一级部门调整考核结果=考核结果、一级部门调整完毕=是
                                    dept_head_str = extract_text(sub_f.get("一级部门负责人") or sub_f.get("部门负责人"), "").strip()
                                    is_dept_head_self = (disp_name in dept_head_str) or any(p.strip() == disp_name for p in dept_head_str.split(",") if p.strip())
                                    if is_dept_head_self and current_grade in GRADE_OPTIONS:
                                        final_data["一级部门调整考核结果"] = current_grade
                                        final_data["一级部门调整完毕"] = "是"

                                    success, error_msg = update_record_safely(APP_TOKEN, TABLE_ID, st.session_state.selected_subordinate_id, final_data)
                                    if success:
                                        st.success(f"✅ 已成功提交！")
                                        st.balloons()
                                        time.sleep(1.5)
                                        st.session_state.selected_subordinate_id = None
                                        st.rerun()
                                    else:
                                        st.error(f"❌ 提交失败：{error_msg}")

                        with col_sub_save:
                            st.markdown("<div class='save-marker'></div>", unsafe_allow_html=True)
                            if st.button("💾 保存草稿", use_container_width=True):
                                with st.spinner("正在暂存分数和评语..."):
                                    success, error_msg = update_record_safely(APP_TOKEN, TABLE_ID, st.session_state.selected_subordinate_id, sub_update_data)
                                    if success:
                                        st.success(f"✅ 草稿已成功保存至飞书！")
                                    else:
                                        st.error(f"❌ 暂存失败：{error_msg}")
                        st.info("💡 提示：点击「确认提交打分」即意味着对该下属的本次评估结束，不可再修改。")
                    else:
                        st.error("未找到对应下属的数据，请返回重试。")
                        st.button("🔙 返回", on_click=return_to_self)
            else:
                st.info("💡 提示：尚未提交个人自评，暂无法进行下级评估。")

        # ===== 一级部门负责人调整（一级导航） =====
        if idx_dept_head is not None:
            with tabs[idx_dept_head]:
                if not is_dept_head:

                    st.info("💡 提示：当前您不是任何员工的一级部门负责人，暂无可调整名单。")

                else:

                    all_records = all_records_snapshot or fetch_all_records_safely(APP_TOKEN, TABLE_ID)

                    dept_head_records = []

                    for rec in all_records:

                        f = rec.get("fields", {})

                        dept_head_str = extract_text(f.get("一级部门负责人") or f.get("部门负责人"), "").strip()

                        emp_name = extract_text(f.get("姓名"), "").strip()

                        # 逻辑：该员工在多维表中标记的「一级部门负责人」是当前用户，且不是本人，即纳入本次调整范围

                        if user_name and user_name in dept_head_str and emp_name != user_name:

                            dept_head_records.append(rec)


                    total_cnt = len(dept_head_records)

                    modified_cnt = 0

                    for rec in dept_head_records:

                        f = rec.get("fields", {})

                        mgr_g = extract_text(f.get("考核结果", "-")).strip() or "-"

                        adj_g = extract_text(f.get("一级部门调整考核结果", "-")).strip() or "-"

                        if mgr_g in GRADE_OPTIONS and adj_g in GRADE_OPTIONS and adj_g != mgr_g:

                            modified_cnt += 1


                    st.markdown("<div class='module-title'>📌 一级部门负责人调整进展</div>", unsafe_allow_html=True)

                    st.markdown(

                        f"""

                        <div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;">

                            <div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;">

                                <span style="color:#b7bdc8;">覆盖人数：<span style="color:#4CAFEE;">{total_cnt}</span> 人</span>

                                <span style="color:#b7bdc8;">调整人数：<span style="color:#8BC34A;">{modified_cnt}</span> 人</span>

                            </div>

                        </div>

                        """,

                        unsafe_allow_html=True,

                    )

                    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)


                    st.markdown("<div class='module-title'>👇 待调整名单</div>", unsafe_allow_html=True)

                    dept_msg_box = st.empty()

                    if not dept_head_records:

                        st.info("💡 提示：暂无需您调整的员工。")

                    else:

                        # 顶部筛选：工号姓名 / 部门 / 状态 / 考核等级

                        f1, f2, f3, f4 = st.columns(4, gap="small")

                        q_name_emp = f1.text_input("搜索工号、姓名", placeholder="🔎 搜索工号、姓名", key="dept_filter_name_emp", label_visibility="collapsed")

                        # 部门下拉（展示二级-三级-四级部门链路）

                        dept_options = set()

                        for rec in dept_head_records:

                            ff = rec.get("fields", {})

                            _chain = build_dept_chain(ff)

                            if _chain:

                                dept_options.add(_chain)

                        dept_options = sorted(dept_options)

                        q_dept = f2.selectbox(

                            "部门",

                            ["全部部门"] + dept_options,

                            key="dept_filter_dept",

                            label_visibility="collapsed",

                        )

                        q_status = f3.selectbox(

                            "状态",

                            ["全部状态", "待上级评分", "待调整", "未自评", "已改"],

                            key="dept_filter_status",

                            label_visibility="collapsed",

                        )

                        q_mgr_grade = f4.selectbox(

                            "考核等级",

                            ["全部调整等级"] + GRADE_OPTIONS + ["-"],

                            key="dept_filter_mgr_grade",

                            label_visibility="collapsed",

                        )


                        filtered_dept_records = []

                        q1 = q_name_emp.strip().lower()

                        q2 = q_dept.strip().lower()

                        for rec in dept_head_records:

                            f = rec.get("fields", {})

                            name = extract_text(f.get("姓名"), "").strip()

                            emp = extract_text(f.get("工号") or f.get("员工工号"), "").strip()

                            dept_chain = build_dept_chain(f)

                            mgr_grade = extract_text(f.get("考核结果", "-")).strip() or "-"

                            adj_grade = extract_text(f.get("一级部门调整考核结果", "-")).strip() or "-"

                            done_flag = extract_text(f.get("一级部门调整完毕", "")).strip() == "是"

                            self_submitted = extract_text(f.get("自评是否提交", "")).strip() == "是"

                            has_mgr_grade = mgr_grade in GRADE_OPTIONS

                            has_adj_grade = adj_grade in GRADE_OPTIONS

                            is_modified_not_submitted = (not done_flag and has_mgr_grade and adj_grade in GRADE_OPTIONS and adj_grade != mgr_grade)

                            status = "未自评" if not self_submitted else ("已完成调整" if done_flag else ("已改" if is_modified_not_submitted else ("待调整" if has_mgr_grade else "待上级评分")))


                            if q1 and (q1 not in name.lower() and q1 not in emp.lower()):

                                continue

                            if q_dept != "全部部门" and (q2 not in dept_chain.lower()):

                                continue

                            if q_status != "全部状态" and q_status != status:

                                continue

                            if q_mgr_grade != "全部调整等级":

                                if q_mgr_grade == "-" and has_adj_grade:

                                    continue

                                if q_mgr_grade in GRADE_OPTIONS and adj_grade != q_mgr_grade:

                                    continue


                            filtered_dept_records.append(rec)


                        st.info("💡 提示：默认为前序调整结果。全部调整完毕请点击「确认本次调整」按钮。提交后筛选框无效，且不能修改。")

                        st.markdown("<div class='dept-confirm-marker'></div>", unsafe_allow_html=True)

                        if st.button("确认本次调整", key="btn_dept_confirm_all", use_container_width=True):

                            ok_cnt = 0

                            fail_cnt = 0

                            skip_cnt = 0

                            fail_msgs = []

                            with st.spinner("正在批量确认，请稍候..."):

                                for rec in dept_head_records:

                                    ff = rec.get("fields", {})

                                    r_id_all = rec.get("record_id")

                                    mgr_grade_all = extract_text(ff.get("考核结果", "-")).strip() or "-"

                                    if mgr_grade_all not in GRADE_OPTIONS:

                                        skip_cnt += 1

                                        continue

                                    adj_existing = extract_text(ff.get("一级部门调整考核结果", "")).strip()

                                    default_grade = adj_existing if adj_existing in GRADE_OPTIONS else mgr_grade_all

                                    selected_grade = st.session_state.get(f"dept_adj_grade_{r_id_all}", default_grade)

                                    if selected_grade not in GRADE_OPTIONS:

                                        selected_grade = default_grade

                                    update_data = {

                                        "一级部门调整考核结果": selected_grade,

                                        "一级部门调整完毕": "是",

                                    }

                                    ok, msg = update_record_safely(APP_TOKEN, TABLE_ID, r_id_all, update_data)

                                    if ok:

                                        ok_cnt += 1

                                    else:

                                        fail_cnt += 1

                                        if msg and msg not in fail_msgs:

                                            fail_msgs.append(msg[:80])

                                    time.sleep(0.25)

                            if fail_cnt == 0:

                                info_txt = f"已确认本次调整，共完成 {ok_cnt} 人。"

                                if skip_cnt > 0:

                                    info_txt += f"（跳过 {skip_cnt} 人：无考核结果）"

                                dept_msg_box.info(info_txt)

                                fetch_all_records_safely.clear()

                            else:

                                err_txt = f"确认完成 {ok_cnt} 人，失败 {fail_cnt} 人。"

                                if fail_msgs:

                                    err_txt += f" 错误：{fail_msgs[0]}"

                                dept_msg_box.error(err_txt)

                            time.sleep(0.6)

                            st.rerun()


                        # 表头：姓名(工号) / 部门(二-三-四)+岗位 / 自评等级 / 考核等级 / 调整等级（表头不断行，列间距适中）

                        st.markdown("<div id='dept-adjust-table' style='display:none;'></div>", unsafe_allow_html=True)

                        h1, h2, h3, h4, h5 = st.columns([1.8, 3.6, 1.3, 1.3, 1.6], gap="small")

                        h1.markdown("<div class='sub-list-head'>姓名（工号）</div>", unsafe_allow_html=True)

                        h2.markdown("<div class='sub-list-head'>子部门/岗位</div>", unsafe_allow_html=True)

                        h3.markdown("<div class='sub-list-head'>自评等级</div>", unsafe_allow_html=True)

                        h4.markdown("<div class='sub-list-head'>考核等级</div>", unsafe_allow_html=True)

                        h5.markdown("<div class='sub-list-head' style='color:#66b2ff; font-weight:800;'>调整等级</div>", unsafe_allow_html=True)

                        st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)


                        for rec in filtered_dept_records:

                            f = rec.get("fields", {})

                            r_id = rec.get("record_id")

                            name = extract_text(f.get("姓名"), "未知姓名").strip()

                            emp = extract_text(f.get("工号") or f.get("员工工号"), "未知工号").strip()

                            job = extract_text(f.get("岗位") or f.get("职位"), "未分配").strip()

                            self_grade = extract_text(f.get("自评等级", "-")).strip() or "-"

                            mgr_grade = extract_text(f.get("考核结果", "-")).strip() or "-"

                            dept_chain = build_dept_chain(f)

                            # 一级部门负责人：默认取上级评分结果；若已有调整结果则展示当前调整值

                            adj_grade_field = extract_text(f.get("一级部门调整考核结果", "")).strip()

                            adj_grade_default = adj_grade_field if adj_grade_field in GRADE_OPTIONS else (mgr_grade if mgr_grade in GRADE_OPTIONS else "-")


                            c1, c2, c3, c4, c5 = st.columns([1.8, 3.6, 1.3, 1.3, 1.6], gap="small", vertical_alignment="center")

                            c1.markdown(

                                f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#E0E0E0; white-space:normal; text-align:center;'><b>{name}</b><br>（{emp}）</div>",

                                unsafe_allow_html=True,

                            )

                            c2.markdown(

                                f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#b0b0b0; white-space:normal; text-align:center;' title='{dept_chain} | {job}'>{dept_chain}<br>{job}</div>",

                                unsafe_allow_html=True,

                            )

                            c3.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{self_grade}</div>", unsafe_allow_html=True)

                            c4.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{mgr_grade}</div>", unsafe_allow_html=True)


                            # 调整等级下拉：与自评/上级评价一致，确认后不可修改，但已保存的调整等级必须展示

                            done_flag = extract_text(f.get("一级部门调整完毕", "")).strip() == "是"

                            disable_adjust = mgr_grade not in GRADE_OPTIONS or done_flag

                            adjust_options = GRADE_OPTIONS + ["-"]

                            if disable_adjust:

                                if done_flag and adj_grade_default in adjust_options:

                                    init_idx = adjust_options.index(adj_grade_default)

                                else:

                                    init_idx = adjust_options.index("-")

                            else:

                                try:

                                    init_idx = adjust_options.index(adj_grade_default) if adj_grade_default in adjust_options else adjust_options.index(mgr_grade)

                                except ValueError:

                                    init_idx = 0

                            is_modified = (adj_grade_default in GRADE_OPTIONS and mgr_grade in GRADE_OPTIONS and adj_grade_default != mgr_grade)

                            c5_inner1, c5_inner2 = c5.columns([4, 1], vertical_alignment="center")

                            with c5_inner1:

                                new_grade = st.selectbox(

                                    "选择等级",

                                    options=adjust_options,

                                    index=init_idx,

                                    key=f"dept_adj_grade_{r_id}",

                                    disabled=disable_adjust,

                                    label_visibility="collapsed",

                                )

                            with c5_inner2:

                                if is_modified:

                                    st.markdown("<div style='color:#1E90FF;font-size:12px;font-weight:700;line-height:38px;white-space:nowrap;' title='考核等级与调整等级不一致'>已改</div>", unsafe_allow_html=True)

                            # 选中等级即自动写入「一级部门调整考核结果」（仅当未确认且用户实际修改时保存）

                            if (not disable_adjust) and (new_grade in GRADE_OPTIONS) and (new_grade != adj_grade_default):

                                ok, msg = update_record_safely(

                                    APP_TOKEN,

                                    TABLE_ID,

                                    r_id,

                                    {"一级部门调整考核结果": new_grade},

                                )

                                if not ok:

                                    dept_msg_box.error(f"保存 {name} 调整失败：{msg}")

                            st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)


                        if not filtered_dept_records:

                            st.caption("当前筛选条件下暂无员工。")

                        else:

                            st.info("💡 提示：我也有底线的ಠ౪ಠ")


                        st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)


        # ===== 分管高管调整（一级导航） =====
        if idx_vp is not None:
            with tabs[idx_vp]:
                all_records = all_records_snapshot or fetch_all_records_safely(APP_TOKEN, TABLE_ID)

                vp_records = []

                for rec in all_records:

                    f = rec.get("fields", {})

                    vp_str = extract_text(f.get("分管高管") or f.get("高管"), "").strip()

                    emp_name = extract_text(f.get("姓名"), "").strip()

                    # 分管高管可见其名下全部员工（排除本人）

                    if user_name and user_name in vp_str and emp_name != user_name:

                        vp_records.append(rec)


                total_cnt = len(vp_records)

                modified_cnt = 0

                for rec in vp_records:

                    f = rec.get("fields", {})

                    mgr_g = extract_text(f.get("考核结果", "-")).strip() or "-"

                    adj_g = extract_text(f.get("分管高管调整考核结果", "-")).strip() or "-"

                    vp_base = extract_text(f.get("一级部门调整考核结果", "")).strip()

                    vp_base = vp_base if vp_base in GRADE_OPTIONS else mgr_g

                    if vp_base in GRADE_OPTIONS and adj_g in GRADE_OPTIONS and adj_g != vp_base:

                        modified_cnt += 1


                st.markdown("<div class='module-title'>📌 分管高管调整进展</div>", unsafe_allow_html=True)

                st.markdown(

                    f"""

                    <div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;">

                        <div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;">

                            <span style="color:#b7bdc8;">覆盖人数：<span style="color:#4CAFEE;">{total_cnt}</span> 人</span>

                            <span style="color:#b7bdc8;">调整人数：<span style="color:#8BC34A;">{modified_cnt}</span> 人</span>

                        </div>

                    </div>

                    """,

                    unsafe_allow_html=True,

                )

                st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)


                st.markdown("<div class='module-title'>👇 待调整名单</div>", unsafe_allow_html=True)

                vp_msg_box = st.empty()

                if not vp_records:

                    st.info("💡 提示：暂无需您调整的员工。")

                else:

                    # 顶部筛选：工号姓名 / 部门 / 状态 / 考核等级（与一级部门负责人一致）

                    f1, f2, f3, f4 = st.columns(4, gap="small")

                    q_name_emp = f1.text_input("搜索工号、姓名", placeholder="🔎 搜索工号、姓名", key="vp_filter_name_emp", label_visibility="collapsed")

                    dept_options = set()

                    for rec in vp_records:

                        ff = rec.get("fields", {})

                        _d1 = _clean_dept_name(ff.get("一级部门"))

                        if _d1:

                            dept_options.add(_d1)

                    dept_options = sorted(dept_options)

                    q_dept = f2.selectbox(

                        "部门",

                        ["全部部门"] + dept_options,

                        key="vp_filter_dept",

                        label_visibility="collapsed",

                    )

                    q_status = f3.selectbox(

                        "状态",

                        ["全部状态", "待上级评分", "待调整", "未自评", "已改"],

                        key="vp_filter_status",

                        label_visibility="collapsed",

                    )

                    q_mgr_grade = f4.selectbox(

                        "考核等级",

                        ["全部调整等级"] + GRADE_OPTIONS + ["-"],

                        key="vp_filter_mgr_grade",

                        label_visibility="collapsed",

                    )


                    filtered_vp_records = []

                    q1 = q_name_emp.strip().lower()

                    q2 = q_dept.strip().lower()

                    for rec in vp_records:

                        f = rec.get("fields", {})

                        name = extract_text(f.get("姓名"), "").strip()

                        emp = extract_text(f.get("工号") or f.get("员工工号"), "").strip()

                        dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"

                        mgr_grade = extract_text(f.get("考核结果", "-")).strip() or "-"

                        adj_grade = extract_text(f.get("分管高管调整考核结果", "-")).strip() or "-"

                        vp_base_grade = extract_text(f.get("一级部门调整考核结果", "")).strip()

                        vp_base_grade = vp_base_grade if vp_base_grade in GRADE_OPTIONS else mgr_grade

                        done_flag = extract_text(f.get("分管高管调整完毕", "")).strip() == "是"

                        self_submitted = extract_text(f.get("自评是否提交", "")).strip() == "是"

                        has_mgr_grade = mgr_grade in GRADE_OPTIONS

                        has_adj_grade = adj_grade in GRADE_OPTIONS

                        is_modified_not_submitted = (not done_flag and has_mgr_grade and adj_grade in GRADE_OPTIONS and vp_base_grade in GRADE_OPTIONS and adj_grade != vp_base_grade)

                        status = "未自评" if not self_submitted else ("已完成调整" if done_flag else ("已改" if is_modified_not_submitted else ("待调整" if has_mgr_grade else "待上级评分")))


                        if q1 and (q1 not in name.lower() and q1 not in emp.lower()):

                            continue

                        if q_dept != "全部部门" and (q2 not in dept_l1.lower()):

                            continue

                        if q_status != "全部状态" and q_status != status:

                            continue

                        if q_mgr_grade != "全部调整等级":

                            if q_mgr_grade == "-" and has_adj_grade:

                                continue

                            if q_mgr_grade in GRADE_OPTIONS and adj_grade != q_mgr_grade:

                                continue


                        filtered_vp_records.append(rec)


                    st.info("💡 提示：默认为前序调整结果。全部调整完毕请点击「确认本次调整」按钮。提交后筛选框无效，且不能修改。")

                    st.markdown("<div class='vp-confirm-marker'></div>", unsafe_allow_html=True)

                    if st.button("确认本次调整", key="btn_vp_confirm_all", use_container_width=True):

                        ok_cnt = 0

                        fail_cnt = 0

                        with st.spinner("正在批量确认，请稍候..."):

                            for rec in vp_records:

                                ff = rec.get("fields", {})

                                r_id_all = rec.get("record_id")

                                mgr_grade_all = extract_text(ff.get("考核结果", "-")).strip() or "-"

                                dept_done_all = extract_text(ff.get("一级部门调整完毕", "")).strip() == "是"

                                if not dept_done_all:

                                    continue

                                adj_existing = extract_text(ff.get("分管高管调整考核结果", "")).strip()

                                dept_adj_all = extract_text(ff.get("一级部门调整考核结果", "")).strip()

                                dept_adj_all = dept_adj_all if dept_adj_all in GRADE_OPTIONS else mgr_grade_all

                                default_grade = adj_existing if adj_existing in GRADE_OPTIONS else (dept_adj_all if dept_adj_all in GRADE_OPTIONS else mgr_grade_all)

                                selected_grade = st.session_state.get(f"vp_adj_grade_{r_id_all}", default_grade)

                                if selected_grade not in GRADE_OPTIONS:

                                    selected_grade = default_grade

                                update_data = {

                                    "分管高管调整考核结果": selected_grade,

                                    "分管高管调整完毕": "是",

                                }

                                ok, _msg = update_record_safely(APP_TOKEN, TABLE_ID, r_id_all, update_data)

                                if ok:

                                    ok_cnt += 1

                                else:

                                    fail_cnt += 1

                                time.sleep(0.25)

                        if fail_cnt == 0:

                            vp_msg_box.info(f"分管高管已确认调整，共完成 {ok_cnt} 人。")

                            fetch_all_records_safely.clear()

                        else:

                            vp_msg_box.error(f"确认完成 {ok_cnt} 人，失败 {fail_cnt} 人，请重试。")

                        time.sleep(0.6)

                        st.rerun()


                    # 表头与展示：姓名(工号)、一级部门/岗位、自评等级、考核等级、调整等级①、调整等级②（分散居中对齐，表头不断行，列间距适中）

                    st.markdown("<div id='vp-adjust-table' style='display:none;'></div>", unsafe_allow_html=True)

                    h1, h2, h3, h4, h5, h6 = st.columns([1.8, 3.6, 1.3, 1.3, 1.3, 1.7], gap="small")

                    h1.markdown("<div class='sub-list-head'>姓名（工号）</div>", unsafe_allow_html=True)

                    h2.markdown("<div class='sub-list-head'>一级部门/岗位</div>", unsafe_allow_html=True)

                    h3.markdown("<div class='sub-list-head'>自评等级</div>", unsafe_allow_html=True)

                    h4.markdown("<div class='sub-list-head'>考核等级</div>", unsafe_allow_html=True)

                    h5.markdown("<div class='sub-list-head'>调整等级Ⅰ<span style='cursor:help;font-size:12px;color:#b7bdc8;margin-left:2px;' title='一级部门调整考核结果'>ⓘ</span></div>", unsafe_allow_html=True)

                    h6.markdown("<div class='sub-list-head' style='color:#66b2ff; font-weight:800;'>调整等级Ⅱ<span style='cursor:help;font-size:12px;color:#66b2ff;margin-left:2px;' title='分管高管调整考核结果'>ⓘ</span></div>", unsafe_allow_html=True)

                    st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)


                    for rec in filtered_vp_records:

                        f = rec.get("fields", {})

                        r_id = rec.get("record_id")

                        name = extract_text(f.get("姓名"), "未知姓名").strip()

                        emp = extract_text(f.get("工号") or f.get("员工工号"), "未知工号").strip()

                        job = extract_text(f.get("岗位") or f.get("职位"), "未分配").strip()

                        self_grade = extract_text(f.get("自评等级", "-")).strip() or "-"

                        mgr_grade_raw = extract_text(f.get("考核结果", "-")).strip() or "-"

                        mgr_done = extract_text(f.get("上级评价是否完成", "")).strip() == "是"

                        # 1.1 考核等级：上级评价是否完成=是 时显示考核结果，否则 -

                        mgr_grade_display = mgr_grade_raw if mgr_done and mgr_grade_raw in GRADE_OPTIONS else "-"

                        vp_base_grade = extract_text(f.get("一级部门调整考核结果", "")).strip()

                        vp_base_grade = vp_base_grade if vp_base_grade in GRADE_OPTIONS else "-"

                        dept_done = extract_text(f.get("一级部门调整完毕", "")).strip() == "是"

                        # 1.2 调整等级①：一级部门调整完毕=是 时显示一级部门调整考核结果（一级部门负责人在上级评分提交时已自动写入）

                        adj1_display = vp_base_grade if dept_done else "-"

                        adj_grade_field = extract_text(f.get("分管高管调整考核结果", "")).strip()

                        # 1.3 调整等级②：默认一级部门调整考核结果，可编辑，已改标记

                        adj_grade_default = adj_grade_field if adj_grade_field in GRADE_OPTIONS else (vp_base_grade if vp_base_grade in GRADE_OPTIONS else "-")

                        dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"

                        vp_done = extract_text(f.get("分管高管调整完毕", "")).strip() == "是"


                        c1, c2, c3, c4, c5, c6 = st.columns([1.8, 3.6, 1.3, 1.3, 1.3, 1.7], gap="small", vertical_alignment="center")

                        c1.markdown(

                            f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#E0E0E0; white-space:normal; text-align:center;'><b>{name}</b><br>（{emp}）</div>",

                            unsafe_allow_html=True,

                        )

                        c2.markdown(

                            f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#b0b0b0; white-space:normal; text-align:center;' title='{dept_l1} | {job}'>{dept_l1}<br>{job}</div>",

                            unsafe_allow_html=True,

                        )

                        c3.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{self_grade}</div>", unsafe_allow_html=True)

                        c4.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{mgr_grade_display}</div>", unsafe_allow_html=True)

                        c5.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{adj1_display}</div>", unsafe_allow_html=True)


                        disable_adjust = (not dept_done) or vp_done

                        adjust_options = GRADE_OPTIONS + ["-"]

                        if disable_adjust:

                            if vp_done and adj_grade_default in adjust_options:

                                init_idx = adjust_options.index(adj_grade_default)

                            else:

                                init_idx = adjust_options.index("-")

                        else:

                            try:

                                fallback = vp_base_grade if vp_base_grade in GRADE_OPTIONS else mgr_grade_raw if mgr_grade_raw in GRADE_OPTIONS else "-"

                                init_idx = adjust_options.index(adj_grade_default) if adj_grade_default in adjust_options else adjust_options.index(fallback)

                            except ValueError:

                                init_idx = 0

                        is_modified = (adj_grade_default in GRADE_OPTIONS and vp_base_grade in GRADE_OPTIONS and adj_grade_default != vp_base_grade)

                        c6_inner1, c6_inner2 = c6.columns([4, 1], vertical_alignment="center")

                        with c6_inner1:

                            new_grade = st.selectbox(

                                "选择等级",

                                options=adjust_options,

                                index=init_idx,

                                key=f"vp_adj_grade_{r_id}",

                                disabled=disable_adjust,

                                label_visibility="collapsed",

                            )

                        with c6_inner2:

                            if is_modified:

                                st.markdown("<div style='color:#1E90FF;font-size:12px;font-weight:700;line-height:38px;white-space:nowrap;' title='调整等级Ⅱ与调整等级Ⅰ不一致'>已改</div>", unsafe_allow_html=True)

                        # 选中等级即自动写入「分管高管调整考核结果」（仅当用户实际修改时保存）

                        if (not disable_adjust) and (new_grade in GRADE_OPTIONS) and (new_grade != adj_grade_default):

                            ok, msg = update_record_safely(

                                APP_TOKEN,

                                TABLE_ID,

                                r_id,

                                {"分管高管调整考核结果": new_grade},

                            )

                            if not ok:

                                vp_msg_box.error(f"保存 {name} 调整失败：{msg}")

                        st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)


                    if not filtered_vp_records:

                        st.caption("当前筛选条件下暂无员工。")

                    else:

                        st.info("💡 提示：我也有底线的ಠ౪ಠ")


                    st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)

        if idx_reports is not None:
            with tabs[idx_reports]:
                report_records_all = all_records_snapshot or fetch_all_records_safely(APP_TOKEN, TABLE_ID)
                if not report_records_all:
                    st.info("💡 提示：暂无可用于报表展示的数据。")
                else:
                    # 固定报表范围与周期：不再显示“视图范围/考核周期”控件
                    scope_mode = "我的团队"

                    # 过滤范围：视图与调整权限保持一致
                    # - 分管高管：看到名下所有员工（按「分管高管 / 高管」字段，排除本人）
                    # - 一级部门负责人：看到本部门所有员工（按「一级部门负责人 / 部门负责人」字段，排除本人）
                    # - 普通管理者：沿用「直接评价人 / 评价人」作为我的团队
                    report_scope = "vp" if is_vp else ("dept_head" if is_dept_head else "mgr")
                    report_scoped = []
                    for rec in report_records_all:
                        rf = rec.get("fields", {})
                        emp_name = extract_text(rf.get("姓名"), "").strip()
                        if scope_mode == "全公司":
                            # 仅分管高管可切全公司
                            report_scoped.append(rec)
                        else:
                            if is_vp:
                                vp_str = extract_text(rf.get("分管高管") or rf.get("高管"), "").strip()
                                if user_name and user_name in vp_str and emp_name != user_name:
                                    report_scoped.append(rec)
                            elif is_dept_head:
                                dept_head_str = extract_text(rf.get("一级部门负责人") or rf.get("部门负责人"), "").strip()
                                if user_name and user_name in dept_head_str and emp_name != user_name:
                                    report_scoped.append(rec)
                            else:
                                rec_manager = extract_text(rf.get("直接评价人") or rf.get("评价人"), "").strip()
                                if user_name and user_name in rec_manager:
                                    report_scoped.append(rec)

                    # 周期筛选
                    def pick_cycle(ff):
                        for k in ["绩效考核周期", "考核周期", "本次绩效考核周期", "本次考核周期"]:
                            v = extract_text(ff.get(k), "").strip()
                            if v:
                                return v
                        return current_cycle

                    # 报表周期固定跟随员工信息周期
                    selected_cycle = current_cycle

                    report_records = []
                    for rec in report_scoped:
                        cyc = pick_cycle(rec.get("fields", {}))
                        if cyc == selected_cycle:
                            report_records.append(rec)

                    total_cnt = len(report_records)
                    if total_cnt == 0:
                        st.info("💡 提示：当前筛选条件下暂无数据。")
                    else:
                        # 聚合统计
                        target_set_cnt = 0
                        self_done_cnt = 0
                        mgr_done_cnt = 0
                        public_done_cnt = 0
                        grade_counts = Counter()
                        dept_stats = {}
                        member_cards = []

                        for rec in report_records:
                            f = rec.get("fields", {})
                            name = extract_text(f.get("姓名"), "未知姓名").strip()
                            emp = extract_text(f.get("工号") or f.get("员工工号"), "未知工号").strip()
                            job = extract_text(f.get("岗位") or f.get("职位"), "未分配").strip()
                            # 报表部门口径：
                            # - 分管高管：按一级部门展示
                            # - 一级部门负责人：按二级部门展示
                            # - 其他角色：默认按二级部门，缺失回退一级部门
                            dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
                            dept_l2 = normalize_dept_text(f.get("二级部门"))
                            if is_vp:
                                dept = dept_l1
                            elif is_dept_head:
                                dept = dept_l2 or dept_l1
                            else:
                                dept = dept_l2 or dept_l1

                            has_target = False
                            for i in range(1, 6):
                                if extract_text(f.get(f"工作目标{i}及总结"), "").strip():
                                    has_target = True
                                    break
                            if has_target:
                                target_set_cnt += 1

                            self_done = extract_text(f.get("自评是否提交", "")).strip() == "是"
                            mgr_done = extract_text(f.get("上级评价是否完成", "")).strip() == "是"
                            dept_done = extract_text(f.get("一级部门调整完毕", "")).strip() == "是"
                            public_done = extract_text(f.get("分管高管调整完毕", "")).strip() == "是"
                            if self_done:
                                self_done_cnt += 1
                            if mgr_done:
                                mgr_done_cnt += 1
                            if public_done:
                                public_done_cnt += 1

                            vp_adj = extract_text(f.get("分管高管调整考核结果", ""), "").strip()
                            dept_adj = extract_text(f.get("一级部门调整考核结果", ""), "").strip()
                            mgr_grade = extract_text(f.get("考核结果", ""), "").strip()
                            final_grade = "-"
                            for cand in [vp_adj, dept_adj, mgr_grade]:
                                if cand in GRADE_OPTIONS:
                                    final_grade = cand
                                    break
                            if final_grade in GRADE_OPTIONS:
                                grade_counts[final_grade] += 1

                            dept_info = dept_stats.setdefault(dept, {"total": 0, "done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}})
                            dept_info["total"] += 1
                            if mgr_done:
                                dept_info["done"] += 1
                            if final_grade in GRADE_OPTIONS:
                                dept_info["grades"][final_grade] += 1

                            # 最终状态统一为「已公示」
                            if public_done or dept_done:
                                status_txt = "已公示"
                            elif mgr_done:
                                status_txt = "上级已评"
                            elif self_done:
                                status_txt = "自评已交"
                            elif has_target:
                                status_txt = "目标设定中"
                            else:
                                status_txt = "待启动"

                            member_cards.append({
                                "name": name,
                                "emp": emp,
                                "job": job,
                                "dept": dept,
                                "grade": final_grade,
                                "status": status_txt,
                            })

                        # 🏟️ 团队概览（所有管理者视图一致，仅包含等级分布+进度统计）
                        if not can_adjust_tab:
                            # 简化版：非调整权限管理者
                            st.markdown("<div class='module-title'>🧭 绩效等级分布</div>", unsafe_allow_html=True)
                            simple_grade_df = pd.DataFrame([{"等级": g, "人数": grade_counts.get(g, 0)} for g in ["S", "A", "B+", "B", "B-", "C"]])
                            _max_cnt = max(grade_counts.values(), default=0) if grade_counts else 0
                            _y_max = max(1, _max_cnt + 1)
                            _y_values = list(range(0, _y_max + 1))
                            simple_grade_chart = (
                                alt.Chart(simple_grade_df)
                                .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
                                .encode(
                                    x=alt.X("等级:N", sort=["S", "A", "B+", "B", "B-", "C"]),
                                    y=alt.Y("人数:Q", scale=alt.Scale(domain=[0, _y_max]), axis=alt.Axis(format="d", values=_y_values, title="人数")),
                                    tooltip=[alt.Tooltip("等级:N"), alt.Tooltip("人数:Q", format="d")],
                                    color=alt.Color("等级:N", legend=None, scale=alt.Scale(domain=["S", "A", "B+", "B", "B-", "C"], range=GRADE_CHART_COLORS))
                                )
                            )
                            st.altair_chart(simple_grade_chart, use_container_width=True)

                            st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                            st.markdown("<div class='module-title'>📈 考核进度统计</div>", unsafe_allow_html=True)
                            p1 = target_set_cnt / total_cnt if total_cnt else 0
                            p2 = self_done_cnt / total_cnt if total_cnt else 0
                            p3 = mgr_done_cnt / total_cnt if total_cnt else 0
                            st.markdown(
                                f"""
                                <div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;">
                                    <div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;">
                                        <span style="color:#b7bdc8;">目标设定：<span style="color:#4CAFEE;">{target_set_cnt}</span>/<span style="color:#b7bdc8;">{total_cnt}</span></span>
                                        <span style="color:#b7bdc8;">自我评价：<span style="color:#8BC34A;">{self_done_cnt}</span>/<span style="color:#b7bdc8;">{total_cnt}</span></span>
                                        <span style="color:#b7bdc8;">上级评价：<span style="color:#00BCD4;">{mgr_done_cnt}</span>/<span style="color:#b7bdc8;">{total_cnt}</span></span>
                                    </div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                            progress_df = pd.DataFrame([
                                {"阶段": "目标设定", "完成率": round(p1 * 100, 1), "已完成": target_set_cnt, "总数": total_cnt},
                                {"阶段": "自我评价", "完成率": round(p2 * 100, 1), "已完成": self_done_cnt, "总数": total_cnt},
                                {"阶段": "上级评价", "完成率": round(p3 * 100, 1), "已完成": mgr_done_cnt, "总数": total_cnt},
                            ])
                            progress_chart = (
                                alt.Chart(progress_df)
                                .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
                                .encode(
                                    y=alt.Y("阶段:N", sort=["目标设定", "自我评价", "上级评价"], axis=alt.Axis(title="阶段")),
                                    x=alt.X("完成率:Q", scale=alt.Scale(domain=[0, 100]), axis=alt.Axis(title="完成率 (%)")),
                                    color=alt.Color("阶段:N", legend=None, scale=alt.Scale(
                                        domain=["目标设定", "自我评价", "上级评价"],
                                        range=["#5B9BD5", "#70AD47", "#5BB5D5"],
                                    )),
                                    tooltip=[alt.Tooltip("阶段:N"), alt.Tooltip("已完成:Q", format="d"), alt.Tooltip("总数:Q", format="d"), alt.Tooltip("完成率:Q", format=".1f", title="完成率(%)")],
                                )
                                .properties(height=140)
                            )
                            st.altair_chart(progress_chart, use_container_width=True)

                            st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                            st.markdown("<div class='module-title'>🧾 部门绩效详情</div>", unsafe_allow_html=True)
                            dept_rows = []
                            for dept_name, dval in sorted(dept_stats.items(), key=lambda x: x[0]):
                                total_d = dval["total"]
                                done = dval["done"]
                                rate_val = round(done / total_d * 100, 1) if total_d else 0
                                rate = "100%" if rate_val == 100 else f"{rate_val}%"
                                dept_rows.append({
                                    "部门": dept_name,
                                    "总人数": total_d,
                                    "已完成": done,
                                    "完成率": rate,
                                    "S级": dval["grades"]["S"],
                                    "A级": dval["grades"]["A"],
                                    "B+级": dval["grades"]["B+"],
                                    "B级": dval["grades"]["B"],
                                    "B-级": dval["grades"]["B-"],
                                    "C级": dval["grades"]["C"],
                                })
                            dept_df = pd.DataFrame(dept_rows)
                            if not dept_df.empty:
                                dept_df = dept_df.style.set_properties(**{"text-align": "center"})
                            st.dataframe(dept_df, use_container_width=True, hide_index=True)
                        else:
                            # 有调整权限的管理者：一级部门负责人/分管高管统一同一套报表视图
                            st.markdown("<div class='module-title'>📊 绩效概览</div>", unsafe_allow_html=True)
                            st.info("💡 提示：此人数不含部门负责人，所有人数向下取整。")
                            completion_rate = 0 if total_cnt == 0 else round(mgr_done_cnt / total_cnt * 100, 1)
                            kpi1, kpi2, kpi3, kpi4 = st.columns(4)
                            drill_mode = st.session_state.get("report_member_kpi_drill", "all")
                            kpi1.markdown("<div class='report-kpi-label'>考核总人数</div>", unsafe_allow_html=True)
                            kpi1.markdown("<div class='report-kpi-total'></div>", unsafe_allow_html=True)
                            if kpi1.button(str(total_cnt), key="btn_kpi_total_drill", use_container_width=True):
                                st.session_state.report_member_kpi_drill = "all"
                                st.session_state.report_member_page = 1
                                st.rerun()

                            kpi2.markdown("<div class='report-kpi-label'>已完成评价</div>", unsafe_allow_html=True)
                            kpi2.markdown("<div class='report-kpi-done'></div>", unsafe_allow_html=True)
                            if kpi2.button(str(mgr_done_cnt), key="btn_kpi_done_drill", use_container_width=True):
                                st.session_state.report_member_kpi_drill = "done"
                                st.session_state.report_member_page = 1
                                st.rerun()

                            kpi3.markdown("<div class='report-kpi-label'>总体完成率</div>", unsafe_allow_html=True)
                            kpi3.markdown("<div class='report-kpi-rate'></div>", unsafe_allow_html=True)
                            if kpi3.button(f"{completion_rate}%", key="btn_kpi_rate_drill", use_container_width=True):
                                st.session_state.report_member_kpi_drill = "done"
                                st.session_state.report_member_page = 1
                                st.rerun()

                            kpi4.markdown("<div class='report-kpi-label'>剩余未评</div>", unsafe_allow_html=True)
                            kpi4.markdown("<div class='report-kpi-pending'></div>", unsafe_allow_html=True)
                            if kpi4.button(str(total_cnt - mgr_done_cnt), key="btn_kpi_pending_drill", use_container_width=True):
                                st.session_state.report_member_kpi_drill = "pending"
                                st.session_state.report_member_page = 1
                                st.rerun()

                            # 图二计算逻辑：人员基数=考核结果关联奖金的人员，比例向下取整
                            report_bonus = [r for r in report_records if extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"]
                            base_cnt = len(report_bonus) if report_bonus else total_cnt
                            grade_counts_bonus = Counter()
                            # 分管高管：预计算各部门的上限/实际人数，用于部门筛选展示
                            dept_grade_stats = {}  # dept -> {base_cnt, grade_counts, sa_theory, bp_theory, ...}
                            for rec in (report_bonus if report_bonus else report_records):
                                f = rec.get("fields", {})
                                vp_adj = extract_text(f.get("分管高管调整考核结果", ""), "").strip()
                                dept_adj = extract_text(f.get("一级部门调整考核结果", ""), "").strip()
                                mgr_grade = extract_text(f.get("考核结果", ""), "").strip()
                                final_grade = "-"
                                for cand in [vp_adj, dept_adj, mgr_grade]:
                                    if cand in GRADE_OPTIONS:
                                        final_grade = cand
                                        break
                                if final_grade in GRADE_OPTIONS:
                                    grade_counts_bonus[final_grade] += 1
                            if is_vp:
                                for rec in report_records:
                                    rf = rec.get("fields", {})
                                    dept_l1 = _clean_dept_name(rf.get("一级部门")) or "未分配部门"
                                    vp_adj = extract_text(rf.get("分管高管调整考核结果", ""), "").strip()
                                    dept_adj = extract_text(rf.get("一级部门调整考核结果", ""), "").strip()
                                    mgr_grade = extract_text(rf.get("考核结果", ""), "").strip()
                                    fg = "-"
                                    for cand in [vp_adj, dept_adj, mgr_grade]:
                                        if cand in GRADE_OPTIONS:
                                            fg = cand
                                            break
                                    dg = dept_grade_stats.setdefault(dept_l1, {"grade_counts": Counter(), "bonus_cnt": 0, "total_cnt": 0})
                                    dg["total_cnt"] += 1
                                    if extract_text(rf.get("是否绩效关联奖金"), "").strip() == "是":
                                        dg["bonus_cnt"] += 1
                                    if fg in GRADE_OPTIONS:
                                        dg["grade_counts"][fg] += 1
                                for dept_name, dg in dept_grade_stats.items():
                                    dg["base_cnt"] = dg["bonus_cnt"] if dg["bonus_cnt"] > 0 else dg["total_cnt"]
                                    if dg["base_cnt"] == 0:
                                        dg["base_cnt"] = 1
                                    bmc = dg["grade_counts"].get("B-", 0) + dg["grade_counts"].get("C", 0)
                                    dg["sa_theory"] = math.floor(dg["base_cnt"] * 0.20)
                                    bp_base = math.floor(dg["base_cnt"] * 0.15)
                                    bp_cap = math.floor(dg["base_cnt"] * 0.25)
                                    dg["bp_theory"] = min(bp_cap, bp_base + bmc)
                                    dg["sapb_theory"] = dg["sa_theory"] + dg["bp_theory"]
                                    dg["actual_sa"] = dg["grade_counts"].get("S", 0) + dg["grade_counts"].get("A", 0)
                                    dg["actual_bp"] = dg["grade_counts"].get("B+", 0)
                                    dg["actual_sapb"] = dg["actual_sa"] + dg["actual_bp"]
                                    dg["actual_b"] = dg["grade_counts"].get("B", 0)
                                    dg["actual_bm"] = dg["grade_counts"].get("B-", 0)
                                    dg["actual_c"] = dg["grade_counts"].get("C", 0)
                                    dg["actual_sum"] = dg["actual_sa"] + dg["actual_bp"] + dg["actual_b"] + dg["actual_bm"] + dg["actual_c"]
                            # 计算逻辑：S/A 20%向下取整；B+ 默认15%，每多一个B-/C可多一个B+，最高25%，向下取整；B 剔除S/A/B+/B-/C
                            bmc_actual = grade_counts_bonus.get("B-", 0) + grade_counts_bonus.get("C", 0)
                            sa_theory = math.floor(base_cnt * 0.20)
                            bp_base = math.floor(base_cnt * 0.15)
                            bp_cap = math.floor(base_cnt * 0.25)
                            bp_theory = min(bp_cap, bp_base + bmc_actual)
                            sapb_theory = sa_theory + bp_theory
                            actual_sa = grade_counts_bonus.get("S", 0) + grade_counts_bonus.get("A", 0)
                            actual_bp = grade_counts_bonus.get("B+", 0)
                            actual_sapb = actual_sa + actual_bp
                            actual_b = grade_counts_bonus.get("B", 0)
                            actual_bm = grade_counts_bonus.get("B-", 0)
                            actual_c = grade_counts_bonus.get("C", 0)
                            actual_sum = actual_sa + actual_bp + actual_b + actual_bm + actual_c
                            _cell_style = "font-size:14px;font-weight:700;white-space:nowrap;"
                            def _td(val, color):
                                return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}</td>"
                            def _td_with_hint(val, color, hint_text):
                                hint = f"<span title='{hint_text}' style='cursor:help;font-size:12px;margin-left:4px;color:#ff5252;'>ⓘ</span>"
                                return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}{hint}</td>"
                            def _th(txt):
                                return f"<th style='text-align:center;{_cell_style}'>{txt}</th>"
                            def _td_label(txt):
                                return f"<td style='text-align:center;color:#b7bdc8;{_cell_style}'>{txt}</td>"
                            def _td_text(val, color):
                                return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}</td>"
                            header_cells = _th("级别") + _th("S/A级别") + _th("B+级别") + _th("B+及以上级别") + _th("B级别") + _th("B-级别") + _th("C级别") + _th("SUM (人)")
                            def _td_colspan(val, color, colspan=1):
                                return f"<td style='text-align:center;color:{color};{_cell_style}' colspan='{colspan}'>{val}</td>"
                            bp_hint = "默认15%，根据实际的B-/C占比调整向上浮动"
                            _neutral = "#b7bdc8"
                            theory_cells = (
                                _td_label("上限人数")
                                + _td(sa_theory, _neutral)
                                + _td_with_hint(bp_theory, _neutral, bp_hint)
                                + _td(sapb_theory, _neutral)
                                + _td_text("剔除绩优/差", _neutral)
                                + _td_colspan("按实际评价", _neutral, colspan=2)
                                + _td(base_cnt, _neutral)
                            )
                            # 实际人数配色：参照 S/A蓝 #4CAFEE | B+绿 #8BC34A | B+及以上青 #00BCD4 | B灰 #90A4AE | B-橙 #FFC107 | C红 #F44336 | SUM灰 #b7bdc8
                            actual_cells = (
                                _td_label("实际人数")
                                + _td(actual_sa, "#4CAFEE")
                                + _td(actual_bp, "#8BC34A")
                                + _td(actual_sapb, "#00BCD4")
                                + _td(actual_b, "#90A4AE")
                                + _td(actual_bm, "#FFC107")
                                + _td(actual_c, "#F44336")
                                + _td(actual_sum, "#b7bdc8")
                            )
                            table_html = f"""
                            <div style='overflow-x:auto;'>
                            <div style='font-size:16px;color:#66b2ff;margin-bottom:8px;font-weight:800;'>分管范围总数</div>
                            <table style='width:100%;border-collapse:collapse;text-align:center;'>
                            <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{header_cells}</tr></thead>
                            <tbody>
                            <tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{theory_cells}</tr>
                            <tr>{actual_cells}</tr>
                            </tbody>
                            </table>
                            </div>
                            """
                            st.markdown(table_html, unsafe_allow_html=True)

                            if is_vp and dept_grade_stats:
                                st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                                dept_options_sorted = sorted(dept_grade_stats.keys())
                                _f1, _f2 = st.columns([1, 1])
                                with _f1:
                                    sel_dept = st.selectbox(
                                        "选择分管一级部门",
                                        options=["全部分管范围"] + dept_options_sorted,
                                        key="report_perf_dept_filter",
                                        label_visibility="collapsed",
                                    )
                                with _f2:
                                    st.markdown(
                                        "<div style='background:rgba(2,119,189,0.15);border:1px solid rgba(2,119,189,0.4);border-radius:6px;padding:8px 12px;font-size:13px;color:#66b2ff;'>💡 提示：选择分管一级部门</div>",
                                        unsafe_allow_html=True,
                                    )
                                if sel_dept and sel_dept != "全部分管范围":
                                    dg = dept_grade_stats[sel_dept]
                                    _cell_style = "font-size:14px;font-weight:700;white-space:nowrap;"
                                    def _td_d(val, color):
                                        return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}</td>"
                                    def _td_label_d(txt):
                                        return f"<td style='text-align:center;color:#b7bdc8;{_cell_style}'>{txt}</td>"
                                    def _td_colspan_d(val, color, colspan=1):
                                        return f"<td style='text-align:center;color:{color};{_cell_style}' colspan='{colspan}'>{val}</td>"
                                    header_cells_d = _th("级别") + _th("S/A级别") + _th("B+级别") + _th("B+及以上级别") + _th("B级别") + _th("B-级别") + _th("C级别") + _th("SUM (人)")
                                    theory_cells_d = (
                                        _td_label_d("部门上限人数")
                                        + _td_d(dg["sa_theory"], _neutral)
                                        + _td_with_hint(dg["bp_theory"], _neutral, bp_hint)
                                        + _td_d(dg["sapb_theory"], _neutral)
                                        + _td_text("剔除绩优/差", _neutral)
                                        + _td_colspan_d("按实际评价", _neutral, colspan=2)
                                        + _td_d(dg["base_cnt"], _neutral)
                                    )
                                    actual_cells_d = (
                                        _td_label_d("部门实际人数")
                                        + _td_d(dg["actual_sa"], "#4CAFEE")
                                        + _td_d(dg["actual_bp"], "#8BC34A")
                                        + _td_d(dg["actual_sapb"], "#00BCD4")
                                        + _td_d(dg["actual_b"], "#90A4AE")
                                        + _td_d(dg["actual_bm"], "#FFC107")
                                        + _td_d(dg["actual_c"], "#F44336")
                                        + _td_d(dg["actual_sum"], "#b7bdc8")
                                    )
                                    table_dept_html = f"""
                                    <div style='overflow-x:auto; margin-top:12px;'>
                                    <div style='font-size:16px;color:#66b2ff;margin-bottom:8px;font-weight:800;'>各分管部门总数：{sel_dept}</div>
                                    <table style='width:100%;border-collapse:collapse;text-align:center;'>
                                    <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{header_cells_d}</tr></thead>
                                    <tbody>
                                    <tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{theory_cells_d}</tr>
                                    <tr>{actual_cells_d}</tr>
                                    </tbody>
                                    </table>
                                    </div>
                                    """
                                    st.markdown(table_dept_html, unsafe_allow_html=True)

                            export_rows = []
                            for m in member_cards:
                                export_rows.append({
                                    "姓名": m["name"],
                                    "工号": m["emp"],
                                    "部门": m["dept"],
                                    "岗位": m["job"],
                                    "绩效等级": m["grade"],
                                    "当前状态": m["status"],
                                    "周期": selected_cycle,
                                    "视图范围": scope_mode,
                                })
                            csv_buffer = io.StringIO()
                            writer = csv.DictWriter(csv_buffer, fieldnames=list(export_rows[0].keys()) if export_rows else ["姓名"])
                            writer.writeheader()
                            if export_rows:
                                writer.writerows(export_rows)
                            csv_bytes = csv_buffer.getvalue().encode("utf-8-sig")

                            st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                            # 团队成员筛选与分页控制合并为同一行
                            pending_dept_drill = st.session_state.get("report_member_dept_pending", "")
                            if pending_dept_drill:
                                st.session_state.report_member_dept = pending_dept_drill
                                st.session_state.report_member_page = 1
                                st.session_state.report_member_dept_pending = ""
                            rf1, rf2, rf3, rf4, rf5, rf6 = st.columns([1.2, 1.2, 1.2, 1.2, 1.0, 1.0], gap="small")
                            rm_query = rf1.text_input(
                                "搜索工号、姓名",
                                placeholder="🔎 搜索工号、姓名",
                                key="report_member_query",
                                label_visibility="collapsed",
                            )
                            rm_dept_options = sorted({m.get("dept", "") for m in member_cards if m.get("dept", "")})
                            rm_dept = rf2.selectbox(
                                "部门",
                                ["全部部门"] + rm_dept_options,
                                key="report_member_dept",
                                label_visibility="collapsed",
                            )
                            rm_status_options = ["全部状态", "待启动", "目标设定中", "自评已交", "上级已评", "已公示"]
                            rm_status = rf3.selectbox(
                                "状态",
                                rm_status_options,
                                key="report_member_status",
                                label_visibility="collapsed",
                            )
                            rm_grade = rf4.selectbox(
                                "等级",
                                ["全部考核等级"] + GRADE_OPTIONS + ["-"],
                                key="report_member_grade",
                                label_visibility="collapsed",
                            )
                            page_size = rf5.selectbox(
                                "每页",
                                [6, 9, 12, 18],
                                index=1,
                                key="report_member_page_size",
                                label_visibility="collapsed",
                                format_func=lambda x: f"每页 {x}",
                            )

                            filtered_members = []
                            q_member = rm_query.strip().lower()
                            q_dept = rm_dept.strip().lower()
                            for m in member_cards:
                                m_name = str(m.get("name", ""))
                                m_emp = str(m.get("emp", ""))
                                m_dept = str(m.get("dept", ""))
                                m_status = str(m.get("status", ""))
                                m_grade = str(m.get("grade", "-"))
                                if q_member and (q_member not in m_name.lower() and q_member not in m_emp.lower()):
                                    continue
                                if rm_dept != "全部部门" and q_dept not in m_dept.lower():
                                    continue
                                if rm_status != "全部状态" and m_status != rm_status:
                                    continue
                                if rm_grade != "全部考核等级" and m_grade != rm_grade:
                                    continue
                                done_statuses = ["上级已评", "已公示"]
                                if drill_mode == "done" and m_status not in done_statuses:
                                    continue
                                if drill_mode == "pending" and m_status in done_statuses:
                                    continue
                                if drill_mode.startswith("grade:"):
                                    target_g = drill_mode.split(":", 1)[1]
                                    if target_g == "SA":
                                        if m_grade not in ("S", "A"):
                                            continue
                                    elif target_g == "SABP":
                                        if m_grade not in ("S", "A", "B+"):
                                            continue
                                    elif m_grade != target_g:
                                        continue
                                filtered_members.append(m)

                            total_pages = max(1, math.ceil(len(filtered_members) / page_size))
                            current_page = int(st.session_state.get("report_member_page", 1))
                            if current_page > total_pages:
                                st.session_state.report_member_page = 1
                                current_page = 1
                            page_no = rf6.selectbox(
                                "页码",
                                options=list(range(1, total_pages + 1)),
                                index=max(0, current_page - 1),
                                key="report_member_page",
                                label_visibility="collapsed",
                                format_func=lambda x: f"页码 {x}",
                            )
                            start_idx = (page_no - 1) * page_size
                            end_idx = start_idx + page_size
                            page_members = filtered_members[start_idx:end_idx]
                            st.caption(f"筛选后人数：{len(filtered_members)}")
                            st.caption(f"当前展示 {len(page_members)} / {len(filtered_members)} 人（第 {page_no}/{total_pages} 页）")
                            card_cols = st.columns(3)
                            for i, m in enumerate(page_members):
                                cc = card_cols[i % 3]
                                cc.markdown(
                                    f"""
                                    <div style="padding:10px; border:1px solid rgba(255,255,255,0.08); border-radius:8px; margin-bottom:8px;">
                                        <div style="font-size:16px; font-weight:700; color:#EAEAEA;">{m['name']} <span style="color:#66b2ff;">{m['grade']}</span></div>
                                        <div style="font-size:12px; color:#9aa0a6;">{m['dept']} · {m['job']}</div>
                                        <div style="font-size:12px; color:#b0b0b0;">{m['status']}</div>
                                    </div>
                                    """,
                                    unsafe_allow_html=True,
                                )

                            st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                            st.markdown("<div class='module-title'>📈 绩效视图</div>", unsafe_allow_html=True)
                            chart_l, chart_r = st.columns(2)
                            with chart_l:
                                st.markdown("**绩效等级分布**")
                                grade_order = ["S", "A", "B+", "B", "B-", "C"]
                                report_grade_df = pd.DataFrame([
                                    {"等级": g, "人数": grade_counts.get(g, 0), "等级显示": f"{g} ({grade_counts.get(g, 0)})"}
                                    for g in grade_order
                                ])
                                domain_display = [f"{g} ({grade_counts.get(g, 0)})" for g in grade_order]
                                donut = (
                                    alt.Chart(report_grade_df)
                                    .mark_arc(innerRadius=55, outerRadius=95)
                                    .encode(
                                        theta=alt.Theta("人数:Q"),
                                        color=alt.Color(
                                            "等级显示:N",
                                            scale=alt.Scale(
                                                domain=domain_display,
                                                range=GRADE_CHART_COLORS,
                                            ),
                                        ),
                                        tooltip=[alt.Tooltip("等级:N"), alt.Tooltip("人数:Q", format="d")],
                                    )
                                ).properties(height=320)
                                st.altair_chart(donut, use_container_width=True)
                            with chart_r:
                                st.markdown("**各部门考核完成率**")
                                dept_rate_rows = []
                                for dept_name, dval in sorted(dept_stats.items(), key=lambda x: x[0]):
                                    total_d = dval["total"]
                                    done = dval["done"]
                                    dept_rate_rows.append(
                                        {
                                            "部门": dept_name,
                                            "完成率": (round(done / total_d * 100, 1) if total_d else 0.0),
                                            "完成": done,
                                            "总数": total_d,
                                        }
                                    )
                                dept_rate_df = pd.DataFrame(dept_rate_rows)
                                dept_bar = (
                                    alt.Chart(dept_rate_df)
                                    .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
                                    .encode(
                                        y=alt.Y(
                                            "部门:N",
                                            sort="-x",
                                            axis=alt.Axis(labelLimit=180),
                                        ),
                                        x=alt.X("完成率:Q", scale=alt.Scale(domain=[0, 100])),
                                        tooltip=["部门", "完成", "总数", "完成率"],
                                        color=alt.value("#66BB6A"),
                                    )
                                ).properties(height=320)
                                st.altair_chart(dept_bar, use_container_width=True)

                            st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                            st.markdown("<div class='module-title'>🧾 部门绩效详情</div>", unsafe_allow_html=True)
                            dept_rows = []
                            for dept_name, dval in sorted(dept_stats.items(), key=lambda x: x[0]):
                                total_d = dval["total"]
                                done = dval["done"]
                                rate_val = round(done / total_d * 100, 1) if total_d else 0
                                rate = "100%" if rate_val == 100 else f"{rate_val}%"
                                dept_rows.append({
                                    "部门": dept_name,
                                    "总人数": total_d,
                                    "已完成": done,
                                    "完成率": rate,
                                    "S级": dval["grades"]["S"],
                                    "A级": dval["grades"]["A"],
                                    "B+级": dval["grades"]["B+"],
                                    "B级": dval["grades"]["B"],
                                    "B-级": dval["grades"]["B-"],
                                    "C级": dval["grades"]["C"],
                                })
                            dept_df = pd.DataFrame(dept_rows)
                            dept_df_display = dept_df.style.set_properties(**{"text-align": "center"}) if not dept_df.empty else dept_df
                            st.caption("点击表格任意行（包含数字单元格）可下钻到团队成员")
                            dept_select_event = st.dataframe(
                                dept_df_display,
                                use_container_width=True,
                                hide_index=True,
                                on_select="rerun",
                                selection_mode="single-row",
                                key="report_dept_table_drill",
                            )
                            selected_rows = (dept_select_event or {}).get("selection", {}).get("rows", [])
                            if selected_rows:
                                selected_idx = selected_rows[0]
                                if 0 <= selected_idx < len(dept_df):
                                    target_dept = str(dept_df.iloc[selected_idx]["部门"]).strip()
                                    if target_dept and st.session_state.get("report_member_dept") != target_dept:
                                        st.session_state.report_member_dept_pending = target_dept
                                        st.rerun()

                            st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
                            d1, d2 = st.columns([1, 1])
                            d1.download_button("📄 导出CSV", data=csv_bytes, file_name="绩效报表.csv", mime="text/csv", use_container_width=True)
                            d2.download_button("📘 导出Excel(兼容)", data=csv_bytes, file_name="绩效报表.xls", mime="application/vnd.ms-excel", use_container_width=True)

    # ==========================================
    # 🟢 模块 3：历史信息 (所有人可见，索引永远是列表最后一个)
    # ==========================================
    with tabs[-1]:
        st.markdown("<div class='module-title'>📂 历史绩效档案</div>", unsafe_allow_html=True)
        perf_cycle = extract_text(fields.get("上一次绩效考核对应周期", "暂无数据"))
        last_perf_result = extract_text(fields.get("上一次绩效考核结果", "暂无数据"))
        last_comment = extract_text(fields.get("上一次绩效考核评语", "暂无评语"))
        _grade_colors = {"S": "#4CAFEE", "A": "#4CAFEE", "B+": "#8BC34A", "B": "#90A4AE", "B-": "#FFC107", "C": "#F44336"}
        _res_color = _grade_colors.get(last_perf_result, "#b7bdc8")

        st.markdown(
            f"""
            <div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;">
                <div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;">
                    <span style="color:#b7bdc8;">上一次考核周期：<span style="color:#4CAFEE;">{perf_cycle}</span></span>
                    <span style="color:#b7bdc8;">上一次绩效结果：<span style="color:{_res_color};">{last_perf_result}</span></span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
        st.markdown("<div class='module-title'>✍️ 上一次绩效考核评语</div>", unsafe_allow_html=True)
        st.info(last_comment)

if st.session_state.user_info is None:
    login_page()
else:
    main_app()

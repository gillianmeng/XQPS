import streamlit as st
import requests
import urllib.parse
import time
from collections import Counter
import os
import json

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
def get_tenant_token():
    token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    res = requests.post(token_url, json={"app_id": APP_ID, "app_secret": APP_SECRET}).json()
    return res.get("tenant_access_token")

def get_feishu_user(code):
    tenant_token = get_tenant_token()
    if not tenant_token:
        return None, "获取 Token 失败"
    user_url = "https://open.feishu.cn/open-apis/authen/v1/access_token"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    payload = {"grant_type": "authorization_code", "code": code}
    res = requests.post(user_url, headers=headers, json=payload).json()
    if res.get("code") == 0:
        return res.get("data"), None
    return None, res.get('msg')

@st.cache_data(ttl=60)
def fetch_all_records_safely(app_token, table_id):
    tenant_token = get_tenant_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    all_items = []
    page_token = ""
    has_more = True
    while has_more:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        res = requests.get(url, headers=headers, params=params).json()
        if res.get("code") == 0:
            data = res.get("data", {})
            all_items.extend(data.get("items", []))
            has_more = data.get("has_more", False)
            page_token = data.get("page_token", "")
        else:
            break
    return all_items

def get_record_by_openid_safely(app_token, table_id, target_openid):
    all_records = fetch_all_records_safely(app_token, table_id)
    for record in all_records:
        fields = record.get("fields", {})
        value = fields.get("姓名")
        if value is None:
            continue
        if isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict) and value[0].get("id") == target_openid:
            return record
        elif isinstance(value, dict) and value.get("id") == target_openid:
            return record
    return None

def update_record_safely(app_token, table_id, record_id, update_data):
    tenant_token = get_tenant_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}
    payload = {"fields": update_data}
    res = requests.put(url, headers=headers, json=payload).json()
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

def load_demo_users():
    """
    读取本地 demo 用户配置，优先 demo_users.json，
    不存在则回退 demo_users.example.json。
    """
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
    st.header("🎯 绩效管理系统 - 内部开发版")

    st.markdown("### 🔐 飞书账号正式登录")
    if "code" in st.query_params:
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
    else:
        encoded_uri = urllib.parse.quote(REDIRECT_URI)
        base_url = "https://open.feishu.cn/open-apis/authen/v1/user_auth_page_beta"
        params = f"?app_id={APP_ID}&redirect_uri={encoded_uri}&state=testing"
        st.info("请使用您的企业飞书账号授权登录")
        st.link_button("🔗 飞书一键授权登录 (正式入口)", base_url + params)

    # 演示入口默认关闭，仅开发环境手动开启
    if ENABLE_DEMO_LOGIN and not IS_PROD:
        st.markdown("---")
        st.markdown("### 🛠️ 演示与测试通道")
        demo_users = load_demo_users()
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
            st.warning("未读取到 demo_users.json，可参考 demo_users.example.json 创建本地测试账号。")

def jump_to_subordinate(sub_id):
    st.session_state.selected_subordinate_id = sub_id

def return_to_self():
    st.session_state.selected_subordinate_id = None

# --- 主应用逻辑 ---
def main_app():
    # --- 注入自定义 CSS ---
    st.markdown("""
    <style>
    /* 全局基础字体：正文统一 14px，模块标题统一样式 */
    body, [data-testid="stMarkdown"] p, [data-testid="stMarkdown"] li {
        font-size: 14px !important;
    }
    .section-title {
        font-size: 16px;
        font-weight: 700;
        margin: 0 0 10px 0;
        color: #FAFAFA;
    }

    /* 允许所有文本域横向和纵向拉伸 */
    textarea {
        resize: both !important;
    }

    textarea::-webkit-resizer {
        width: 24px !important;
        height: 24px !important;
        background-color: transparent !important;
        background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="%23888888" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="14 20 20 20 20 14"></polyline><line x1="10" y1="10" x2="20" y2="20"></line></svg>') !important;
        background-repeat: no-repeat !important;
        background-position: bottom 4px right 4px !important; 
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

    /* 👇 导航栏专属发光与动态触感特效 (最新兼容版) */
    [data-testid="stTabs"] {
        gap: 8px; 
    }
    /* 1. 强制修改选中的底线（红线变电光蓝） */
    [data-testid="stTabs"] div[data-baseweb="tab-highlight"] {
        background-color: #1E90FF !important;
        height: 4px !important; /* 线条加厚 */
    }

    /* 2. 基础 Tab 按钮样式 */
    [data-testid="stTabs"] button[role="tab"] {
        padding: 10px 20px !important;
        transition: all 0.3s ease !important;
        font-size: 16px !important; 
        color: #b0b0b0 !important;
    }

    /* 3. 选中状态：字体变大、变粗、变蓝 */
    [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
        background-color: rgba(30, 144, 255, 0.1) !important;
        color: #66b2ff !important; 
        font-size: 20px !important; /* 字体加大到 20px */
        font-weight: 800 !important; /* 极粗体 */
        border: 1px solid #1E90FF !important;
        border-bottom: 2px solid transparent !important;
        box-shadow: 0px -4px 15px rgba(30, 144, 255, 0.4), inset 0px 2px 5px rgba(30, 144, 255, 0.2) !important;
        transform: translateY(-2px) !important;
    }
    [data-testid="stTabs"] button[role="tab"]:hover {
        background-color: rgba(30, 144, 255, 0.05) !important;
        border-color: rgba(30, 144, 255, 0.2) !important;
    }
    /* 点击瞬间的物理反馈 */
    [data-testid="stTabs"] button[role="tab"]:active {
        transform: translateY(2px) !important; 
        box-shadow: 0px -1px 5px rgba(30, 144, 255, 0.5) !important;
    }
    /* 指标（Metric）与正文一致 14px */
    [data-testid="stMetricValue"] {
        font-size: 14px !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 14px !important;
    }

    /* 上级评分页：更紧凑的分隔线与列表行 */
    hr.sub-hr {
        margin: 6px 0px !important;
        border: none !important;
        border-top: 1px solid rgba(255,255,255,0.08) !important;
    }
    .sub-list-head {
        font-size: 14px;
        color: #b0b0b0;
        margin: 0 0 8px 0;
        font-weight: 700;
        text-align: center;
    }
    .sub-list-cell {
        font-size: 14px;
        margin: 0;
        padding: 6px 0;
        line-height: 1.4;
        text-align: center;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    /* 下属名单操作按钮：小巧精致，颜色与状态列一致（标记列与按钮列相邻，用 + 精确匹配） */
    div:has(.xqps-btn-remind) + div button,
    div:has(.xqps-btn-view) + div button,
    div:has(.xqps-btn-evaluate) + div button {
        font-size: 13px !important;
        min-height: 24px !important;
        height: 24px !important;
        padding: 3px 10px !important;
        border-radius: 10px !important;
        border: none !important;
    }
    div:has(.xqps-btn-remind) + div button {
        background: #FFA500 !important;
        color: #1a1a1a !important;
    }
    div:has(.xqps-btn-view) + div button {
        background: #00e676 !important;
        color: #1a1a1a !important;
    }
    div:has(.xqps-btn-evaluate) + div button {
        background: #1E90FF !important;
        color: white !important;
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

    # 1. 登录与获取飞书档案
    if not st.session_state.feishu_record_id and st.session_state.feishu_record_id != "NOT_FOUND":
        with st.spinner("正在同步您的飞书档案数据..."):
            current_open_id = st.session_state.user_info.get("open_id") or st.session_state.user_info.get("id")
            try:
                record = get_record_by_openid_safely(APP_TOKEN, TABLE_ID, current_open_id)
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
    dept_parts = [d for d in [extract_text(fields.get(f'{k}级部门'), "") for k in ["一", "二", "三", "四"]] if d and d != "未获取"]
    department = "-".join(dept_parts) if dept_parts else "未获取"
    manager = extract_text(fields.get('直接评价人') or fields.get('评价人'))
    vp = extract_text(fields.get('分管高管') or fields.get('高管'))
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
    if st.session_state.role == "管理者" and is_submitted:
        with st.spinner("正在拉取团队数据..."):
            all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
            for record in all_records:
                rec_fields = record.get("fields", {})
                rec_manager = extract_text(rec_fields.get("直接评价人") or rec_fields.get("评价人"))
                if user_name in rec_manager:
                    my_all_subs.append(record)
                    if extract_text(rec_fields.get("自评是否提交")).strip() == "是":
                        real_subordinates.append(record)

    # 4. 侧边栏渲染 (从上到下严格顺序)
    st.sidebar.markdown(f"### 👋 欢迎 {user_name}（{emp_id}）！")

    # 另起一行：调整岗位和角色的顺序，并去掉“角色”二字
    st.sidebar.write(f"{job_title} | {st.session_state.role}")
    st.sidebar.markdown("---")
    
    st.sidebar.markdown("### ℹ️ 员工信息")
    st.sidebar.caption("绩效考核周期: 2026上半年")
    st.sidebar.caption(f"您的部门: {department}")
    st.sidebar.caption(f"直接评价人: {manager} | 分管高管: {vp}")
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
            if val_comment is None: val_comment = extract_text(sub_f.get("考核评语", ""))
            mgr_comment_empty = len(val_comment.strip()) == 0
            
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
    if st.session_state.role == "管理者":
        tab_list = ["📝 员工自评", "👥 上级评分", "⚖️ 综合调整", "✅ 公司审批", "📂 历史信息"]
    else:
        # 员工个人只看两个标签
        tab_list = ["📝 员工自评", "📂 历史信息"]

    tabs = st.tabs(tab_list)

    # ==========================================
    # 🟢 模块 1：员工自评 (索引 0)
    # ==========================================
    with tabs[0]:
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
        
        st.header("🎯 当前绩效目标设定与自评")
        if is_submitted:
            st.success("🔒 您的自评已提交，当前表单不可修改。")
        
        st.markdown("### 💼 工作模块")
        st.info(f"💡 提示：工作模块总体占比 {target_weight}% (各目标权重之和必须等于 {target_weight}%)")
        
        for i in range(1, st.session_state.goal_count + 1):
            col_left, col_right = st.columns([3, 1])
            with col_left:
                st.text_area(f"工作目标{i}及总结", height=110, disabled=is_submitted, key=f"obj_summary_{i}")
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

        if not is_submitted:
            st.caption("提示：可拖动每个文本框右下角放大编辑区域，便于撰写长文。")

        st.markdown("### 🧠 能力模块")
        if st.session_state.role == "员工":
            st.info("💡 提示：通用能力占比 20%")
            col_comp_left, col_comp_right = st.columns([3, 1])
            with col_comp_left: st.text_area("结合考核期工作实际情况，从「思考、行动、写作、成长」四个维度总结", height=110, disabled=is_submitted, key="comp_summary")
            with col_comp_right: st.selectbox("通用能力自评得分", options=SCORE_OPTIONS, disabled=is_submitted, key="comp_score")
        elif st.session_state.role == "管理者":
            st.info("💡 提示：通用能力占比 20%、领导力占比 20%")
            col_cap_left, col_cap_right = st.columns(2)
            with col_cap_left:
                st.text_area("结合考核期工作实际情况，从「思考、行动、写作、成长」四个维度总结", height=110, disabled=is_submitted, key="comp_summary")
                st.selectbox("通用能力自评得分", options=SCORE_OPTIONS, disabled=is_submitted, key="comp_score")
            with col_cap_right:
                st.text_area("请结合考核周期工作实际情况，从「领导力」维度进行阐述与总结", height=110, disabled=is_submitted, key="lead_summary")
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
                        
            st.error("💡 提示：点击「确认提交」即意味着本次自评结束，不可再修改。")        

    # ==========================================
    # 🟢 模块 2：管理者专属权限 
    # ==========================================
    if st.session_state.role == "管理者":
        with tabs[1]:
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
                
                grade_counts = Counter(grade_list)
                sort_order = {"S": 1, "A": 2, "B+": 3, "B": 4, "B-": 5, "C": 6}
                sorted_grades = sorted(grade_counts.items(), key=lambda x: sort_order.get(x[0], 99))
                grade_counts_map = {k: v for k, v in sorted_grades if k and k != "未获取"}
                total_grades = sum(grade_counts_map.values())
                if total_grades == 0:
                    dist_str = "暂无评分数据"
                else:
                    order_keys = ["S", "A", "B+", "B", "B-", "C"]
                    parts = []
                    for k in order_keys:
                        cnt = grade_counts_map.get(k, 0)
                        parts.append(f"{k}: {cnt}人")
                    dist_str = " | ".join(parts)

                # 模块 1：下属评估进展（单列）
                st.markdown("### 👥 下属评估进展")
                st.markdown(
                    f"""
                    <div style="font-size: 13px; color: #E0E0E0; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;">
                        <div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;">
                            <span><b>总下属：</b><span style="color:#1E90FF;">{total_subs}</span> 人</span>
                            <span><b>已交自评：</b><span style="color:#1E90FF;">{submitted_subs}</span> 人</span>
                            <span><b>已评：</b><span style="color:#00e676;">{rated_subs}</span> 人</span>
                            <span><b>暂存：</b><span style="color:#FFA500;">{drafted_subs}</span> 人</span>
                            <span><b>待评：</b><span style="color:#ff5252;">{unrated_subs}</span> 人</span>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                # 模块 2：团队等级分布（单列）
                st.markdown("### 📊 团队等级分布")
                if dist_str == "暂无评分数据":
                    st.markdown(
                        f"""
                        <div style="font-size: 13px; color: #E0E0E0; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444; text-align: center;">
                            {dist_str}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                else:
                    # 按 S/A/B+/B/B-/C 分散居中对齐，与进展块同风格，数字彩色显示
                    order_keys = ["S", "A", "B+", "B", "B-", "C"]
                    grade_colors = {"S": "#00e676", "A": "#1E90FF", "B+": "#1E90FF", "B": "#b0b0b0", "B-": "#FFA500", "C": "#ff5252"}
                    parts_html = []
                    for k in order_keys:
                        cnt = grade_counts_map.get(k, 0)
                        c = grade_colors.get(k, "#1E90FF")
                        parts_html.append(f"<span><b>{k}：</b><span style='color:{c};'>{cnt}</span> 人</span>")
                    st.markdown(
                        f"""
                        <div style="font-size: 13px; color: #E0E0E0; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;">
                            <div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;">
                                {"".join(parts_html)}
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

                st.markdown("---")

                # 模块 3：下属评估名单（单列容器，内部用列展示字段）
                st.markdown("### 👇 下属评估名单")
                if not my_all_subs:
                    st.info("当前暂无已提交自评的下属。")
                else:
                    # 搜索框：按工号、姓名、提交状态、考核等级过滤
                    search_term = st.text_input(
                        "搜索",
                        placeholder="🔎 搜索工号、姓名、状态（未自评/待评价/已完成）或考核等级（S/A/B+/B/B-/C）",
                        key="sub_list_search",
                        label_visibility="collapsed",
                    )
                    search_lower = search_term.strip().lower()
                    filtered_subs = my_all_subs
                    if search_lower:
                        filtered_subs = []
                        for s in my_all_subs:
                            f = s.get("fields", {})
                            n = extract_text(f.get("姓名"), "").lower()
                            e = extract_text(f.get("工号") or f.get("员工工号"), "").lower()
                            # 状态匹配
                            s_submitted = extract_text(f.get("自评是否提交")).strip() == "是"
                            s_mgr_done = extract_text(f.get("上级评价是否完成")).strip() == "是"
                            if not s_submitted:
                                status_str = "未自评"
                            elif s_mgr_done:
                                status_str = "已完成"
                            else:
                                status_str = "待评价"
                            # 考核等级匹配
                            mgr_grade = extract_text(f.get("考核结果", "")).strip().lower()
                            if search_lower in n or search_lower in e or search_lower in status_str or search_lower == mgr_grade:
                                filtered_subs.append(s)
                    if not filtered_subs and search_lower:
                        st.caption("未找到匹配的下属")
                    st.markdown("<div class='mgr-sub-list'>", unsafe_allow_html=True)
                    # 表头：姓名 / 工号 / 岗位 / 自评等级 / 考核等级 / 状态 / 操作
                    h1, h2, h3, h4, h5, h6, h7 = st.columns(7)
                    h1.markdown("<div class='sub-list-head'>下属姓名</div>", unsafe_allow_html=True)
                    h2.markdown("<div class='sub-list-head'>工号</div>", unsafe_allow_html=True)
                    h3.markdown("<div class='sub-list-head'>岗位</div>", unsafe_allow_html=True)
                    h4.markdown("<div class='sub-list-head'>自评等级</div>", unsafe_allow_html=True)
                    h5.markdown("<div class='sub-list-head'>考核等级</div>", unsafe_allow_html=True)
                    h6.markdown("<div class='sub-list-head'>状态</div>", unsafe_allow_html=True)
                    h7.markdown("<div class='sub-list-head'>操作</div>", unsafe_allow_html=True)
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
                            warning_icon = "<span title='请注意：你的评分与员工自评分差异较大' style='cursor:help; font-size:12px; margin-left:2px;'>ⓘ</span>"

                        # 状态与按钮类型
                        if not is_self_submitted:
                            status_html = "<span style='color:#FFA500;'>未自评</span>"
                            action_type = "remind"
                        elif is_mgr_done:
                            color = "#00e676"
                            status_html = f"<span style='color:{color}; font-weight:800;'>已完成{warning_icon}</span>"
                            action_type = "view"
                        else:
                            status_html = "<span style='color:#1E90FF;'>待评价</span>"
                            action_type = "evaluate"

                        # 自评等级：未自评显示 -，已自评直接显示等级
                        disp_grade = "-" if not is_self_submitted else (self_grade if self_grade not in ["", "未获取", "None"] else "-")
                        # 考核等级（上级评分后的结果）
                        disp_mgr_grade = current_grade if current_grade and current_grade not in ["", "未获取", "-"] else "-"

                        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
                        c1.markdown(f"<div class='sub-list-cell' style='color:#E0E0E0;'>📄 {s_name}</div>", unsafe_allow_html=True)
                        c2.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0;'>{s_emp_id}</div>", unsafe_allow_html=True)
                        c3.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0;' title='{s_job}'>{s_job}</div>", unsafe_allow_html=True)
                        c4.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0;'>{disp_grade}</div>", unsafe_allow_html=True)
                        c5.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0;'>{disp_mgr_grade}</div>", unsafe_allow_html=True)
                        c6.markdown(f"<div class='sub-list-cell'>{status_html}</div>", unsafe_allow_html=True)

                        with c7:
                            col_m, col_b = st.columns([0.001, 0.999])
                            with col_m:
                                st.markdown(f"<div class='xqps-btn-{action_type}' style='width:0;height:0;overflow:hidden;'></div>", unsafe_allow_html=True)
                            with col_b:
                                if action_type == "remind":
                                    if st.button("提醒", key=f"btn_remind_{s_id}", use_container_width=True):
                                        st.toast("飞书机器人提醒功能开发中，敬请期待", icon="🔔")
                                elif action_type == "view":
                                    if st.button("查看", key=f"btn_view_{s_id}", use_container_width=True):
                                        jump_to_subordinate(s_id)
                                        st.rerun()
                                else:
                                    if st.button("去评价", key=f"btn_jump_{s_id}", use_container_width=True):
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
                            st.markdown(f"### 📝 正在评估：{disp_name}")
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
                            st.button("收起面板", on_click=return_to_self, use_container_width=True)
                        st.write("")
                        
                        st.markdown("### 💼 工作模块展示与评分")
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
                        
                        st.info(f"💡 该下属工作目标总权重为：**{sub_weight_sum}%**")
                        mgr_work_score = st.selectbox("🌟 工作目标整体上级评分", options=SCORE_OPTIONS, index=work_idx, key=f"mgr_work_score_{sub_id_str}")
                        st.markdown("---")

                        st.markdown("### 🧠 能力模块展示与评分")
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
                            
                            st.markdown("#### 👑 领导力模块")
                            st.markdown(f"**👑 领导力总结** <span style='font-size:14px; color:#888;'>(自评: {sub_lead_score}分)</span>", unsafe_allow_html=True)
                            st.text_area("隐藏标签", value=sub_lead_text, height=100, disabled=True, key=f"ui_sub_lead_{sub_id_str}", label_visibility="collapsed")
                            st.write("")
                            
                            mgr_lead_score = st.selectbox("🌟 领导力上级评分", options=SCORE_OPTIONS, index=lead_idx, key=f"mgr_lead_score_{sub_id_str}")
                        st.markdown("---")

                        st.caption("提示：如需查看更长文本，可拖动下方各文本框右下角放大区域。")
                        
                        comp_weight = 20 if has_leadership else (100 - sub_weight_sum)
                        lead_weight = 20 if has_leadership else 0
                        
                        current_total_score = (mgr_work_score * (sub_weight_sum / 100.0)) + \
                                              (mgr_comp_score * (comp_weight / 100.0)) + \
                                              (mgr_lead_score * (lead_weight / 100.0))
                                              
                        current_total_score = round(current_total_score, 2)
                        current_grade = calculate_grade(current_total_score)
                        
                        st.markdown(f"### 📈 考核总得分预览：**{current_total_score} 分** ｜ 绩效等级：**{current_grade}**")
                        st.caption(f"(工作模块权重 {sub_weight_sum}%，能力模块权重 {comp_weight}%)")
                        
                        saved_comment = extract_text(sub_f.get("考核评语", ""))
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
                        st.error("💡 提示：点击「确认提交打分」即意味着对该下属的本次评估结束，不可再修改。")
                    else:
                        st.error("未找到对应下属的数据，请返回重试。")
                        st.button("🔙 返回", on_click=return_to_self)
            else:
                st.info("💡 尚未提交个人自评，暂无法进行下级评估。")

            with tabs[2]:
                st.write("🔧 综合调整功能开发中...")
            with tabs[3]:
                st.write("🔧 公司审批功能开发中...")

    # ==========================================
    # 🟢 模块 3：历史信息 (所有人可见，索引永远是列表最后一个)
    # ==========================================
    with tabs[-1]:
        st.markdown("### 📂 历史绩效档案")
        
        perf_cycle = extract_text(fields.get("上一次绩效考核对应周期", "暂无数据"))
        last_perf_result = extract_text(fields.get("上一次绩效考核结果", "暂无数据"))
        last_comment = extract_text(fields.get("上一次绩效考核评语", "暂无评语"))

        col_h1, col_h2 = st.columns(2)
        with col_h1: st.metric(label="考核周期", value=perf_cycle)
        with col_h2: st.metric(label="上一次绩效结果", value=last_perf_result)
            
        st.markdown("---")
        
        st.markdown("### ✍️ 上一次绩效考核评语")
        st.info(last_comment)

if st.session_state.user_info is None:
    login_page()
else:
    main_app()

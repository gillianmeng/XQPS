from __future__ import annotations
import streamlit as st
import requests
import urllib.parse
import time
from collections import Counter
from datetime import datetime
import os
import json
import csv
import io
import math
import pandas as pd
import altair as alt
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

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
if 'admin_role' not in st.session_state:
    st.session_state.admin_role = None
if 'admin_scope' not in st.session_state:
    st.session_state.admin_scope = None

def _resolve_admin_config_path():
    """确定 admin_config.json 的路径。优先可写目录，保证本地与部署环境一致。"""
    # 1. 环境变量指定（部署时显式设置）
    env_dir = os.environ.get("XQPS_CONFIG_DIR", "").strip()
    if env_dir:
        d = os.path.abspath(env_dir)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "admin_config.json")
    # 2. 项目目录，先尝试是否可写
    project_dir = os.path.dirname(os.path.abspath(__file__))
    _test = os.path.join(project_dir, ".xqps_write_test")
    try:
        with open(_test, "w") as f:
            f.write("1")
        os.remove(_test)
        return os.path.join(project_dir, "admin_config.json")
    except Exception:
        pass
    # 3. 项目目录不可写时，自动回退到用户目录（公司部署常见情况）
    fallback = os.path.expanduser("~/xqps_config")
    os.makedirs(fallback, exist_ok=True)
    return os.path.join(fallback, "admin_config.json")

ADMIN_CONFIG_PATH = _resolve_admin_config_path()
# 配置修改策略：无论部署环境如何，管理员均应通过「📋 后台配置」模块修改配置，勿直接编辑 admin_config.json。
ANNOUNCE_LOCATIONS = ["员工自评", "上级评分", "一级部门负责人调整", "分管高管调整"]
DOC_LINK_NAMES = ["雪球集团绩效管理制度", "雪球集团绩效管理实施细则", "绩效考核系统操作指引"]
DEFAULT_DOC_LINK = "https://xueqiu.feishu.cn/wiki/RL1OwdkJ9iQnRakIcj6cXKRSnfg"  # 雪球集团绩效管理制度 默认

# 考核周期等价与展示统一：「2026上半年」与「2026年上半年」视为同一周期，展示时统一为后者
CYCLE_EQUIVALENTS = [("2026上半年", "2026年上半年")]

def _cycles_match(a, b):
    """两周期是否等价（含 2026上半年=2026年上半年）"""
    if not a or not b:
        return a == b
    if a.strip() == b.strip():
        return True
    for x, y in CYCLE_EQUIVALENTS:
        if (a.strip() == x and b.strip() == y) or (a.strip() == y and b.strip() == x):
            return True
    return False

def _normalize_cycle_display(cycle):
    """将周期统一为展示格式（如 2026上半年 → 2026年上半年）"""
    if not cycle or not (cycle := (cycle or "").strip()):
        return cycle
    for old, preferred in CYCLE_EQUIVALENTS:
        if cycle == old:
            return preferred
    return cycle

def _read_admin_config():
    """读取管理员配置。所有配置修改应通过管理员后台「📋 后台配置」模块进行，勿直接编辑文件。"""
    try:
        if os.path.exists(ADMIN_CONFIG_PATH):
            with open(ADMIN_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _write_admin_config(data):
    """写入管理员配置。失败时返回 False（如无写权限）"""
    try:
        _dir = os.path.dirname(ADMIN_CONFIG_PATH)
        if _dir and not os.path.exists(_dir):
            os.makedirs(_dir, exist_ok=True)
        with open(ADMIN_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[XQPS] admin_config 写入失败: {e}", flush=True)
        return False

def _read_admin_cycle_override():
    """兼容旧接口：返回 display_cycle 或 legacy cycle"""
    return _read_display_cycle()

def _write_admin_cycle_override(cycle):
    """兼容旧接口"""
    return _write_display_cycle(cycle)

def _read_display_cycle():
    """前台展示的考核周期，以管理员选择为准。返回统一展示格式（2026上半年→2026年上半年）"""
    cfg = _read_admin_config()
    v = (cfg.get("display_cycle") or cfg.get("cycle") or "").strip()
    if v:
        return _normalize_cycle_display(v) or v
    return None

def _write_display_cycle(cycle):
    """设置前台展示的考核周期，切换后前台自动更新。写入时统一为展示格式（2026上半年→2026年上半年）"""
    cfg = _read_admin_config()
    v = (cycle or "").strip()
    if v:
        v = _normalize_cycle_display(v) or v
    cfg["display_cycle"] = v
    cfg["cycle"] = cfg["display_cycle"]  # 兼容
    return _write_admin_config(cfg)

def _resolve_cycle_config_key(cycle, configs):
    """解析周期在 configs 中的实际 key（等价周期可互查）"""
    if not cycle:
        return cycle
    if cycle in configs:
        return cycle
    for old, preferred in CYCLE_EQUIVALENTS:
        if cycle == preferred and old in configs:
            return old
        if cycle == old and preferred in configs:
            return preferred
    return cycle

def _read_cycle_config(cycle):
    """读取某考核周期的配置：announcements, doc_links, config_complete"""
    cfg = _read_admin_config()
    configs = cfg.get("cycle_configs") or {}
    key = _resolve_cycle_config_key(cycle, configs)
    c = configs.get(key) or {}
    legacy_ann = cfg.get("announcements") or {}
    legacy_links = cfg.get("doc_links") or {}
    return {
        "announcements": c.get("announcements") or legacy_ann,
        "doc_links": c.get("doc_links") or legacy_links,
        "config_complete": bool(c.get("config_complete")),
    }

def _write_cycle_config(cycle, announcements=None, doc_links=None, config_complete=None):
    """写入某考核周期的配置，None 表示不更新该字段。写入时统一使用展示格式作为 key"""
    c = (cycle or "").strip()
    if not c:
        return False
    c = _normalize_cycle_display(c) or c
    cfg = _read_admin_config()
    cfg.setdefault("cycle_configs", {})
    cfg["cycle_configs"].setdefault(c, {"announcements": {}, "doc_links": {}, "config_complete": False})
    entry = cfg["cycle_configs"][c]
    if announcements is not None:
        entry["announcements"] = announcements
    if doc_links is not None:
        entry["doc_links"] = doc_links
    if config_complete is not None:
        entry["config_complete"] = bool(config_complete)
    return _write_admin_config(cfg)

def _set_cycle_config_complete(cycle, complete=True):
    """标记某考核周期配置完成"""
    c = _normalize_cycle_display((cycle or "").strip()) or (cycle or "").strip()
    if not c:
        return False
    cfg = _read_admin_config()
    cfg.setdefault("cycle_configs", {})
    cfg["cycle_configs"].setdefault(c, {"announcements": {}, "doc_links": {}, "config_complete": False})
    cfg["cycle_configs"][c]["config_complete"] = bool(complete)
    return _write_admin_config(cfg)

def _read_admin_announcement(location, cycle=None):
    """读取指定位置的公告覆盖。cycle=None 时使用 display_cycle"""
    c = cycle or _read_display_cycle()
    if c:
        cc = _read_cycle_config(c)
        v = (cc.get("announcements") or {}).get(location) or ""
    else:
        ann = _read_admin_config().get("announcements") or {}
        v = ann.get(location) or ""
    return (v or "").strip() or None

def _write_admin_announcement(location, content, cycle=None):
    """写入公告。cycle=None 时写入 display_cycle 的配置"""
    c = cycle or _read_display_cycle()
    if not c:
        cfg = _read_admin_config()
        cfg.setdefault("announcements", {})
        cfg["announcements"][location] = content or ""
        return _write_admin_config(cfg)
    cc = _read_cycle_config(c)
    ann = dict(cc.get("announcements") or {})
    ann[location] = content or ""
    return _write_cycle_config(c, announcements=ann)

def _read_admin_doc_links(cycle=None):
    """读取文档链接配置。cycle=None 时使用 display_cycle"""
    c = cycle or _read_display_cycle()
    if c:
        cc = _read_cycle_config(c)
        links = cc.get("doc_links") or {}
    else:
        links = _read_admin_config().get("doc_links") or {}
    return {k: (v or "").strip() for k, v in links.items() if (v or "").strip()}

def _write_admin_doc_links(links, cycle=None):
    """写入文档链接配置。cycle=None 时写入 display_cycle 的配置"""
    c = cycle or _read_display_cycle()
    if not c:
        cfg = _read_admin_config()
        cfg["doc_links"] = links
        return _write_admin_config(cfg)
    cc = _read_cycle_config(c)
    return _write_cycle_config(c, doc_links=links)

def _get_doc_link(name, cycle=None):
    """获取文档链接，无配置时第一个用默认，其余返回 None"""
    all_links = _read_admin_doc_links(cycle)
    if name in all_links and all_links[name]:
        return all_links[name]
    if name == DOC_LINK_NAMES[0]:
        return DEFAULT_DOC_LINK
    return None

MODULE_KEYS = ["员工自评", "上级评分", "一级部门负责人调整", "分管高管调整", "视图与报表", "HRBP"]

def _read_module_edit_disabled():
    """读取各模块编辑关停状态，返回 {模块名: bool}"""
    cfg = _read_admin_config()
    d = cfg.get("module_edit_disabled") or {}
    return {k: bool(d.get(k)) for k in MODULE_KEYS}

def _write_module_edit_disabled(module_key, disabled):
    """设置指定模块的编辑关停状态"""
    cfg = _read_admin_config()
    cfg.setdefault("module_edit_disabled", {})
    cfg["module_edit_disabled"][module_key] = bool(disabled)
    return _write_admin_config(cfg)

def _read_full_shutdown():
    """读取一键全关停状态：True=所有人不可查看不可编辑（管理员可查看）"""
    cfg = _read_admin_config()
    return bool(cfg.get("module_shutdown") or cfg.get("full_shutdown"))

def _write_full_shutdown(shutdown):
    """写入一键全关停状态"""
    cfg = _read_admin_config()
    cfg["module_shutdown"] = bool(shutdown)
    cfg["full_shutdown"] = bool(shutdown)
    return _write_admin_config(cfg)

def _read_module_shutdown():
    """兼容旧接口：等同于 full_shutdown"""
    return _read_full_shutdown()

def _write_module_shutdown(shutdown):
    """兼容旧接口"""
    return _write_full_shutdown(shutdown)

def _is_module_disabled(module_key):
    """判断指定模块是否完全关停（不可查看）。管理员可绕过。module_key: 员工自评|上级评分|一级部门负责人调整|分管高管调整|视图与报表|HRBP"""
    if st.session_state.get("admin_role") == "admin":
        return False
    return _read_full_shutdown()

def _is_module_edit_disabled(module_key):
    """判断指定模块是否仅关闭编辑（可查看不可编辑）。module_key: 员工自评|上级评分|一级部门负责人调整|分管高管调整"""
    if _read_full_shutdown():
        return True
    return _read_module_edit_disabled().get(module_key, False)

def _read_result_visible():
    """员工是否可查看绩效结果和评语。True=已开放，False=待审批"""
    cfg = _read_admin_config()
    return bool(cfg.get("result_visible"))

def _write_result_visible(visible):
    """设置员工查看绩效结果和评语的权限"""
    cfg = _read_admin_config()
    cfg["result_visible"] = bool(visible)
    return _write_admin_config(cfg)

def _is_result_visible_for_user():
    """当前用户是否可查看绩效结果和评语。管理员始终可查看"""
    if st.session_state.get("admin_role") == "admin":
        return True
    return _read_result_visible()

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

def _extract_text(val, default="未获取"):
    """解析飞书多维表字段为文本（供登录等模块使用）"""
    if val is None or val == "":
        return default
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

def _clean_dept_name(v):
    t = _extract_text(v, "").strip()
    if t in ["", "未获取", "-", "--", "—"]:
        return ""
    t = t.replace("—", "-").replace("－", "-")
    parts = [p.strip() for p in t.split("-") if p.strip() and p.strip() not in ["-", "--", "—", "未获取"]]
    return "-".join(parts).strip("-").strip()


def _is_dept_head(rec):
    """该员工是否为所在一级部门的部门负责人（不能调整自己，故从部门统计中排除）"""
    name = _extract_text(rec.get("fields", {}).get("姓名"), "").strip()
    head_str = _extract_text(rec.get("fields", {}).get("一级部门负责人") or rec.get("fields", {}).get("部门负责人"), "").strip()
    for h in head_str.replace("，", ",").split(","):
        if h.strip() and name == h.strip():
            return True
    return False


def _is_executive(rec):
    """飞书表格「特殊判断」为「高管」的员工"""
    v = _extract_text(rec.get("fields", {}).get("特殊判断"), "").strip()
    return v == "高管"


def _build_detail_stats(records, extract_text_fn=None, dept_key="一级部门", exclude_exec_from_dept=False):
    """
    构建部门绩效详情统计：高管单列，各部门含一级部门负责人。
    返回 (exec_stats, dept_stats)，dept_stats 的 key 为部门名。
    dept_key: "一级部门" 或 "二级部门"（二级部门时若为空则退回到一级部门）
    exclude_exec_from_dept: 为 True 时（HRBP 视图），高管不进入 dept_stats，也不进入 exec_stats（高管由 exec_by_name 单独展示）
    exec_stats / dept_stats 结构: total, done, grades, sales_total, sales_done, sales_grades, non_sales_total, non_sales_done, non_sales_grades
    """
    ex = extract_text_fn or _extract_text

    def _fresh_stats():
        return {"total": 0, "done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}, "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS}, "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS}}
    exec_stats = _fresh_stats()
    dept_stats = {}
    for rec in (records or []):
        f = rec.get("fields", {})
        mgr_done = ex(f.get("上级评价是否完成"), "").strip() == "是"
        final_grade = ex(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
        if final_grade not in GRADE_OPTIONS:
            final_grade = "-"
        is_sales = ex(f.get("是否绩效关联奖金"), "").strip() == "否"
        is_exec = _is_executive(rec)
        if exclude_exec_from_dept and is_exec:
            continue  # HRBP：高管不进入部门统计，由 exec_by_name 单独展示
        if is_exec:
            t = exec_stats
        else:
            if dept_key == "二级部门":
                dept_name = _normalize_dept_text(f.get("二级部门")) or _clean_dept_name(f.get("一级部门")) or "未分配部门"
            else:
                dept_name = _clean_dept_name(f.get("一级部门")) or "未分配部门"
            t = dept_stats.setdefault(dept_name, _fresh_stats())
        t["total"] += 1
        if mgr_done:
            t["done"] += 1
        if final_grade in GRADE_OPTIONS:
            t["grades"][final_grade] += 1
        if is_sales:
            t["sales_total"] += 1
            if mgr_done:
                t["sales_done"] += 1
            if final_grade in GRADE_OPTIONS:
                t["sales_grades"][final_grade] += 1
        else:
            t["non_sales_total"] += 1
            if mgr_done:
                t["non_sales_done"] += 1
            if final_grade in GRADE_OPTIONS:
                t["non_sales_grades"][final_grade] += 1
    return exec_stats, dept_stats


def _normalize_dept_text(val):
    raw = _extract_text(val, "").strip()
    if raw in ["", "未获取", "-", "--", "—"]:
        return ""
    raw = raw.replace("—", "-").replace("－", "-")
    parts = [p.strip() for p in raw.split("-") if p.strip() and p.strip() not in ["未获取", "-", "--", "—"]]
    return "-".join(parts).strip("-").strip()

def _pick_cycle_from_fields(ff):
    """从飞书字段提取考核周期。返回统一展示格式（2026上半年→2026年上半年）"""
    for k in ["绩效考核周期", "考核周期", "本次绩效考核周期", "本次考核周期"]:
        v = _extract_text(ff.get(k), "").strip()
        if v:
            return _normalize_cycle_display(v) or v
    return "2026年上半年"

def _get_available_cycles(all_records):
    """从记录和已配置周期中获取可选考核周期列表，等价周期合并为统一展示格式"""
    cycles = set()
    for rec in (all_records or []):
        cyc = _pick_cycle_from_fields(rec.get("fields", {}))
        if cyc:
            cycles.add(_normalize_cycle_display(cyc) or cyc)
    cfg = _read_admin_config()
    for c in (cfg.get("cycle_configs") or {}).keys():
        cycles.add(_normalize_cycle_display(c) or c)
    return sorted(cycles) if cycles else ["2026年上半年"]

def _migrate_cycle_to_preferred(old_cycle, preferred_cycle):
    """
    将周期配置从旧格式迁移到统一展示格式（如 2026上半年 → 2026年上半年）。
    合并 cycle_configs，更新 display_cycle，写回后删除旧 key。
    """
    cfg = _read_admin_config()
    configs = cfg.get("cycle_configs") or {}
    old = (old_cycle or "").strip()
    preferred = (preferred_cycle or "").strip()
    if not old or not preferred or old == preferred:
        return False
    if old not in configs:
        if cfg.get("display_cycle") == old:
            cfg["display_cycle"] = preferred
            cfg["cycle"] = preferred
            return _write_admin_config(cfg)
        return False
    # 合并：preferred 优先，old 补充
    old_c = configs[old]
    new_c = configs.get(preferred) or {"announcements": {}, "doc_links": {}, "config_complete": False}
    for k in ["announcements", "doc_links"]:
        merge = dict(new_c.get(k) or {})
        for key, val in (old_c.get(k) or {}).items():
            if key not in merge or not (merge.get(key) or "").strip():
                merge[key] = val or ""
        new_c[k] = merge
    new_c["config_complete"] = new_c.get("config_complete") or old_c.get("config_complete")
    configs[preferred] = new_c
    del configs[old]
    cfg["cycle_configs"] = configs
    if cfg.get("display_cycle") == old:
        cfg["display_cycle"] = preferred
        cfg["cycle"] = preferred
    return _write_admin_config(cfg)

def _add_cycle_to_config(cycle):
    """添加新周期到配置（用于配置尚未有数据的下一考核周期）。写入时统一为展示格式"""
    c = (cycle or "").strip()
    if not c:
        return False
    c = _normalize_cycle_display(c) or c
    cfg = _read_admin_config()
    cfg.setdefault("cycle_configs", {})
    if c not in cfg["cycle_configs"]:
        cfg["cycle_configs"][c] = {"announcements": {}, "doc_links": {}, "config_complete": False}
        return _write_admin_config(cfg)
    return True

def _remove_cycle_from_config(cycle):
    """从配置中移除周期（仅移除配置，不影响飞书记录）。支持等价周期查找。"""
    c = (cycle or "").strip()
    if not c:
        return False
    cfg = _read_admin_config()
    configs = cfg.get("cycle_configs") or {}
    key = _resolve_cycle_config_key(c, configs)
    if key and key in configs:
        del configs[key]
        cfg["cycle_configs"] = configs
        return _write_admin_config(cfg)
    return False

def _render_admin_dashboard():
    """管理员全公司绩效视图：复制视图与报表格式，展示所有部门"""
    user_name = st.session_state.user_info.get("name", "管理员")
    all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
    if not all_records:
        st.info("💡 提示：暂无可用于报表展示的数据。")
        st.sidebar.markdown(f"### 👋 欢迎 {user_name}！")
        st.sidebar.markdown("**系统管理员**")
        st.sidebar.markdown("---")
        _cycles_nodata = _get_available_cycles([])
        _disp_nodata = _read_display_cycle() or (_cycles_nodata[0] if _cycles_nodata else "2026年上半年")
        st.sidebar.markdown("### 📅 当前展示周期")
        def _on_disp_nodata():
            v = st.session_state.get("admin_disp_nodata")
            if v:
                _write_display_cycle(v)
                st.rerun()
        _disp_norm = _normalize_cycle_display(_disp_nodata) or _disp_nodata
        st.sidebar.selectbox("展示周期", options=_cycles_nodata, index=_cycles_nodata.index(_disp_norm) if _disp_norm in _cycles_nodata else 0, key="admin_disp_nodata", label_visibility="collapsed", on_change=_on_disp_nodata)
        with st.sidebar.expander("📋 后台配置"):
            st.markdown("""
            <style>
            div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type button,
            div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type [data-testid="baseButton-secondary"] {
                font-size: 0.75em !important;
                background-color: rgba(33, 150, 243, 0.25) !important;
            }
            </style>
            """, unsafe_allow_html=True)
            st.info("💡 无论部署环境如何，请通过本模块修改配置，请勿直接编辑配置文件。")
            st.caption("绩效结果和评语查看权限")
            _rv_nd = _read_result_visible()
            _rn1, _rn2 = st.columns(2)
            with _rn1:
                if st.button("开放查看", key="admin_open_rv_nd", use_container_width=True, disabled=_rv_nd):
                    _write_result_visible(True)
                    st.success("已开放")
                    st.rerun()
            with _rn2:
                if st.button("关闭查看", key="admin_close_rv_nd", use_container_width=True, disabled=not _rv_nd):
                    _write_result_visible(False)
                    st.success("已关闭")
                    st.rerun()
            st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
            st.caption("配置周期")
            _nd_cfg_has_old = ("2026上半年" in (_read_admin_config().get("cycle_configs") or {})) or (_read_display_cycle() == "2026上半年")
            if _nd_cfg_has_old and st.button("🔄 将「2026上半年」统一为「2026年上半年」", key="admin_migrate_cyc_nd"):
                if _migrate_cycle_to_preferred("2026上半年", "2026年上半年"):
                    st.success("已迁移")
                    st.rerun()
                else:
                    st.info("已是最新格式")
            _new_cyc = st.text_input("添加新周期", key="admin_new_cyc_nodata", placeholder="如 2026下半年", label_visibility="collapsed")
            if st.button("➕ 添加", key="admin_add_cyc_nodata") and _new_cyc and _new_cyc.strip():
                _add_cycle_to_config(_new_cyc.strip())
                st.success("已添加")
                st.rerun()
            _rm_cyc = st.selectbox("选择要移除的周期", options=_cycles_nodata, key="admin_rm_cyc_nd", label_visibility="collapsed")
            if st.button("移除周期", key="admin_remove_cyc_nd"):
                if _remove_cycle_from_config(_rm_cyc):
                    st.success(f"已移除「{_rm_cyc}」")
                    st.rerun()
                else:
                    st.warning("该周期来自飞书记录，无法移除")
        st.sidebar.markdown("---")
        if st.sidebar.button("🚪 退出登录", use_container_width=True):
            st.session_state.clear()
            st.rerun()
        return

    _available_cycles = _get_available_cycles(all_records)
    _default_cycle = _available_cycles[0] if _available_cycles else "2026年上半年"
    _saved_cycle = _read_display_cycle() or _default_cycle
    current_cycle = _saved_cycle or _default_cycle
    report_records = []
    for rec in all_records:
        rf = rec.get("fields", {})
        cyc = _pick_cycle_from_fields(rf)
        if _cycles_match(cyc, current_cycle):
            report_records.append(rec)

    # 配额模块计算基数：仅受销售/非销售影响，不受分管高管/一级部门/最终考核结果筛选影响
    report_records_for_quota = list(report_records)

    # 按分管高管筛选
    _vp_names = set()
    for rec in report_records:
        vp_str = _extract_text(rec.get("fields", {}).get("分管高管") or rec.get("fields", {}).get("高管"), "").strip()
        for v in vp_str.replace("，", ",").split(","):
            if v.strip():
                _vp_names.add(v.strip())
    _vp_opts = ["全部"] + sorted(_vp_names)
    if "admin_vp_filter" not in st.session_state or st.session_state.admin_vp_filter not in _vp_opts:
        st.session_state.admin_vp_filter = "全部"
    _sel_vp = st.session_state.get("admin_vp_filter", "全部")

    # 全公司行：不受分管高管、一级部门筛选影响，保存筛选前数据
    report_records_for_allcompany = list(report_records)

    if _sel_vp and _sel_vp != "全部":
        # 管理员本人任分管高管时，部门统计需包含管理员本人，仅当所选分管高管为他人时才排除该高管
        report_records = [
            r for r in report_records
            if _sel_vp in _extract_text(r.get("fields", {}).get("分管高管") or r.get("fields", {}).get("高管"), "").strip()
            and (_extract_text(r.get("fields", {}).get("姓名"), "").strip() != _sel_vp or _sel_vp == user_name)
        ]

    # 分管高管配额：负责范围总体 = 该分管名下全员（不含本人），不受一级部门筛选影响
    if _sel_vp and _sel_vp != "全部":
        _vp_in_scope = lambda r: _sel_vp in _extract_text(r.get("fields", {}).get("分管高管") or r.get("fields", {}).get("高管"), "").strip()
        _vp_not_self = lambda r: _extract_text(r.get("fields", {}).get("姓名"), "").strip() != _sel_vp
        report_records_for_vp_quota = [r for r in report_records_for_quota if _vp_in_scope(r) and _vp_not_self(r)]
    else:
        report_records_for_vp_quota = []

    # 按一级部门筛选
    _dept_names = sorted({(_clean_dept_name(r.get("fields", {}).get("一级部门")) or "未分配部门") for r in report_records})
    _dept_opts = ["全部部门"] + _dept_names
    if "admin_dept_filter" not in st.session_state or st.session_state.admin_dept_filter not in _dept_opts:
        st.session_state.admin_dept_filter = "全部部门"
    _sel_dept = st.session_state.get("admin_dept_filter", "全部部门")
    if _sel_dept and _sel_dept != "全部部门":
        report_records = [r for r in report_records if (_clean_dept_name(r.get("fields", {}).get("一级部门")) or "未分配部门") == _sel_dept]

    # 上方 KPI：不随筛选变化，展示当前绩效进展。已完成评价=飞书「最终考核结果」不为空
    kpi_total = len(report_records_for_quota)
    kpi_done = sum(
        1 for r in report_records_for_quota
        if _extract_text(r.get("fields", {}).get("最终考核结果"), "").strip() in GRADE_OPTIONS
    )
    kpi_rate = round(kpi_done / kpi_total * 100, 1) if kpi_total else 0
    kpi_remaining = kpi_total - kpi_done

    total_cnt = len(report_records)
    if total_cnt == 0:
        st.info("💡 提示：当前考核周期暂无数据。可在侧边栏修改考核周期后重试。")
        st.sidebar.markdown("""
        <style>
        div[data-testid="stSidebar"] button[kind="primary"],
        div[data-testid="stSidebar"] [data-testid="baseButton-primary"] { font-size: 0.8em !important; }
        </style>
        """, unsafe_allow_html=True)
        st.sidebar.markdown(f"### 👋 欢迎 {user_name}！")
        st.sidebar.markdown("**系统管理员**")
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        _cycles_empty = _get_available_cycles(all_records)
        st.sidebar.markdown("### 📅 当前展示周期")
        st.sidebar.caption("切换后前台自动更新为该周期")
        def _on_display_cycle_change_empty():
            v = st.session_state.get("admin_display_cycle_empty")
            if v:
                _write_display_cycle(v)
                st.rerun()
        _cur_norm = _normalize_cycle_display(current_cycle) or current_cycle
        st.sidebar.selectbox("展示周期", options=_cycles_empty, index=_cycles_empty.index(_cur_norm) if _cur_norm in _cycles_empty else 0, key="admin_display_cycle_empty", label_visibility="collapsed", on_change=_on_display_cycle_change_empty)
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        with st.sidebar.expander("📋 后台配置", expanded=False):
            st.info("💡 无论部署环境如何，请通过本模块修改配置，请勿直接编辑配置文件。")
            st.markdown("""
            <style>
            div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type button,
            div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type [data-testid="baseButton-secondary"] {
                font-size: 0.75em !important;
                background-color: rgba(33, 150, 243, 0.25) !important;
            }
            div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type button[kind="primary"],
            div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type [data-testid="baseButton-primary"] {
                background-color: rgba(33, 150, 243, 0.5) !important;
            }
            </style>
            """, unsafe_allow_html=True)
            st.caption("绩效结果和评语查看权限")
            _result_visible_empty = _read_result_visible()
            _r1e, _r2e = st.columns(2)
            with _r1e:
                if st.button("开放查看", key="admin_open_result_empty", use_container_width=True, disabled=_result_visible_empty):
                    _write_result_visible(True)
                    st.success("已开放")
                    st.rerun()
            with _r2e:
                if st.button("关闭查看", key="admin_close_result_empty", use_container_width=True, disabled=not _result_visible_empty):
                    _write_result_visible(False)
                    st.success("已关闭")
                    st.rerun()
            st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
            st.caption("配置周期")
            _cfg_has_old = ("2026上半年" in (_read_admin_config().get("cycle_configs") or {})) or (_read_display_cycle() == "2026上半年")
            if _cfg_has_old and st.button("🔄 将「2026上半年」统一为「2026年上半年」", key="admin_migrate_cycle_empty"):
                if _migrate_cycle_to_preferred("2026上半年", "2026年上半年"):
                    st.success("已迁移，公告等配置已同步")
                    st.rerun()
                else:
                    st.info("已是最新格式")
            _new_cycle_empty = st.text_input("添加新周期（如 2026下半年）", key="admin_new_cycle_empty", placeholder="输入后点击添加", label_visibility="collapsed")
            if st.button("➕ 添加周期", key="admin_add_cycle_empty") and _new_cycle_empty and _new_cycle_empty.strip():
                _add_cycle_to_config(_new_cycle_empty.strip())
                st.success(f"已添加「{_new_cycle_empty.strip()}」")
                st.rerun()
            _config_cycle = st.selectbox("选择要配置的周期", options=_get_available_cycles(all_records), key="admin_config_cycle_empty", label_visibility="collapsed")
            if st.button("移除周期", key="admin_remove_cycle_empty"):
                if _remove_cycle_from_config(_config_cycle):
                    st.success(f"已移除「{_config_cycle}」")
                    st.rerun()
                else:
                    st.warning("该周期来自飞书记录，无法移除；或周期不存在于配置中")
            st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
            st.caption("公告")
            _cc = _read_cycle_config(_config_cycle)
            _ann_loc_cfg = st.selectbox("公告位置", options=ANNOUNCE_LOCATIONS, key="admin_ann_loc_cfg_empty", label_visibility="collapsed")
            _ann_val = st.text_area("公告内容", value=_cc["announcements"].get(_ann_loc_cfg) or "", key="admin_ann_cfg_empty", height=80, label_visibility="collapsed", placeholder="由管理员填写，留空则无公告")
            if st.button("保存公告", key="admin_save_ann_cfg_empty"):
                if _write_admin_announcement(_ann_loc_cfg, _ann_val, cycle=_config_cycle):
                    st.success("已保存")
                else:
                    st.error("保存失败，可能是目录无写权限。可设置环境变量 XQPS_CONFIG_DIR 指定可写路径。")
            st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
            st.caption("文档链接")
            _doc_links_cfg = dict(_read_admin_doc_links(_config_cycle))
            for i, _dn in enumerate(DOC_LINK_NAMES):
                _def = _doc_links_cfg.get(_dn) or (DEFAULT_DOC_LINK if i == 0 else "")
                _val = st.text_input(_dn, value=_def, key=f"admin_doc_cfg_empty_{i}", placeholder=f"粘贴 {_dn} 链接")
                _doc_links_cfg[_dn] = (_val or "").strip()
            if st.button("保存文档链接", key="admin_save_doc_cfg_empty"):
                if _write_admin_doc_links(_doc_links_cfg, cycle=_config_cycle):
                    st.success("已保存")
                else:
                    st.error("保存失败，可能是目录无写权限。可设置环境变量 XQPS_CONFIG_DIR 指定可写路径。")
            st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
            if st.button("✅ 配置完成", type="primary", key="admin_config_done_empty"):
                if _set_cycle_config_complete(_config_cycle, True):
                    st.success(f"已标记「{_config_cycle}」配置完成")
                    st.rerun()
                else:
                    st.error("保存失败，可能是目录无写权限。可设置环境变量 XQPS_CONFIG_DIR 指定可写路径。")
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        _edit_disabled_empty = _read_module_edit_disabled()
        _full_shutdown_empty = _read_full_shutdown()
        _pending_empty = st.session_state.get("admin_pending_action_empty", None)
        with st.sidebar.expander("🚨 关停权限", expanded=bool(_pending_empty)):
            st.caption("关闭编辑权限后，用户仅可查看不可操作。点击后需确认才执行。")
            if _pending_empty and _pending_empty.startswith("close_"):
                _mod = _pending_empty.replace("close_", "")
                _sn = {"员工自评": "员工自评", "上级评分": "上级评价", "一级部门负责人调整": "一级部门负责人调整", "分管高管调整": "分管高管调整"}.get(_mod, _mod)
                st.warning(f"确认关闭{_sn}编辑权限？")
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("确认", type="primary", key="admin_confirm_empty"):
                        _write_module_edit_disabled(_mod, True)
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
                with _c2:
                    if st.button("取消", key="admin_cancel_empty"):
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
            elif _pending_empty == "full":
                st.warning("确认一键关闭所有编辑和查看权限？管理员仍可查看全部内容。")
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("确认关停", type="primary", key="admin_confirm_full_empty"):
                        _write_full_shutdown(True)
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
                with _c2:
                    if st.button("取消", key="admin_cancel_full_empty"):
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
            elif _pending_empty == "open_full":
                st.warning("确认一键开启所有编辑和查看权限？")
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("确认开启", type="primary", key="admin_confirm_open_full_empty"):
                        _write_full_shutdown(False)
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
                with _c2:
                    if st.button("取消", key="admin_cancel_open_full_empty"):
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
            elif _pending_empty and _pending_empty.startswith("open_") and _pending_empty != "open_full":
                _mod = _pending_empty.replace("open_", "")
                _sn = {"员工自评": "员工自评", "上级评分": "上级评价", "一级部门负责人调整": "一级部门负责人调整", "分管高管调整": "分管高管调整"}.get(_mod, _mod)
                st.warning(f"确认开启{_sn}编辑权限？")
                _c1, _c2 = st.columns(2)
                with _c1:
                    if st.button("确认", type="primary", key="admin_confirm_open_empty"):
                        _write_module_edit_disabled(_mod, False)
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
                with _c2:
                    if st.button("取消", key="admin_cancel_open_empty"):
                        st.session_state.pop("admin_pending_action_empty", None)
                        st.rerun()
            else:
                def _row_empty(mod_key, close_key, open_key):
                    r1, r2 = st.columns(2)
                    with r1:
                        if st.button("关闭", key=close_key, use_container_width=True, disabled=_edit_disabled_empty.get(mod_key)):
                            st.session_state["admin_pending_action_empty"] = f"close_{mod_key}"
                            st.rerun()
                    with r2:
                        if st.button("开启", key=open_key, use_container_width=True, disabled=not _edit_disabled_empty.get(mod_key)):
                            st.session_state["admin_pending_action_empty"] = f"open_{mod_key}"
                            st.rerun()
                st.caption("员工自评")
                _row_empty("员工自评", "admin_close_self_empty", "admin_open_self_empty")
                st.caption("上级评价")
                _row_empty("上级评分", "admin_close_mgr_empty", "admin_open_mgr_empty")
                st.caption("一级部门负责人调整")
                _row_empty("一级部门负责人调整", "admin_close_dept_empty", "admin_open_dept_empty")
                st.caption("分管高管调整")
                _row_empty("分管高管调整", "admin_close_vp_empty", "admin_open_vp_empty")
                st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
                st.caption("一键操作")
                _full_r1, _full_r2 = st.columns(2)
                with _full_r1:
                    if st.button("一键关闭", type="primary", key="admin_btn_full_empty", use_container_width=True, disabled=_full_shutdown_empty):
                        st.session_state["admin_pending_action_empty"] = "full"
                        st.rerun()
                with _full_r2:
                    if st.button("一键开启", type="primary", key="admin_open_all_empty", use_container_width=True, disabled=not _full_shutdown_empty):
                        st.session_state["admin_pending_action_empty"] = "open_full"
                        st.rerun()
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        if st.sidebar.button("🚪 退出登录", use_container_width=True):
            st.session_state.clear()
            st.rerun()
        return

    target_set_cnt = self_done_cnt = mgr_done_cnt = dept_done_cnt = vp_done_cnt = 0
    grade_counts = Counter()
    dept_stats = {}
    step_people = {"target_set": [], "self_done": [], "mgr_done": [], "dept_done": [], "vp_done": []}
    grade_people = {g: [] for g in GRADE_OPTIONS}
    admin_member_cards = []
    for rec in report_records:
        f = rec.get("fields", {})
        name = _extract_text(f.get("姓名"), "未知").strip()
        emp = _extract_text(f.get("工号") or f.get("员工工号"), "").strip()
        job = _extract_text(f.get("岗位") or f.get("职位"), "").strip()
        dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
        person = {"name": name, "emp_id": emp, "dept": dept_l1}
        has_target = any(_extract_text(f.get(f"工作目标{i}及总结"), "").strip() for i in range(1, 6))
        if has_target:
            target_set_cnt += 1
            step_people["target_set"].append(person)
        self_done = _extract_text(f.get("自评是否提交"), "").strip() == "是"
        mgr_done = _extract_text(f.get("上级评价是否完成"), "").strip() == "是"
        dept_done = _extract_text(f.get("一级部门调整完毕"), "").strip() == "是"
        vp_done = _extract_text(f.get("分管高管调整完毕"), "").strip() == "是"
        if self_done:
            self_done_cnt += 1
            step_people["self_done"].append(person)
        if mgr_done:
            mgr_done_cnt += 1
            step_people["mgr_done"].append(person)
        if dept_done:
            dept_done_cnt += 1
            step_people["dept_done"].append(person)
        if vp_done:
            vp_done_cnt += 1
            step_people["vp_done"].append(person)
        vp_adj = _extract_text(f.get("分管高管调整考核结果"), "").strip()
        dept_adj = _extract_text(f.get("一级部门调整考核结果"), "").strip()
        mgr_grade = _extract_text(f.get("考核结果"), "").strip()
        final_from_field = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
        final_grade = "-"
        for cand in [vp_adj, dept_adj, mgr_grade, final_from_field]:
            if cand in GRADE_OPTIONS:
                final_grade = cand
                break
        if final_grade in GRADE_OPTIONS:
            grade_counts[final_grade] += 1
            grade_people[final_grade].append(person)
        if has_target:
            status_txt = "分管高管已调整" if vp_done else ("一级部门已调整" if dept_done else ("上级已评" if mgr_done else ("自评已交" if self_done else "目标设定中")))
        else:
            status_txt = "待启动"
        admin_member_cards.append({
            "name": name, "emp": emp, "dept": dept_l1, "job": job,
            "grade": final_grade if final_grade in GRADE_OPTIONS else "-",
            "status": status_txt,
        })
        _base = {"total": 0, "done": 0, "target_set": 0, "self_done": 0, "dept_done": 0, "vp_done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}}
        dept_info = dept_stats.setdefault(dept_l1, {
            **_base,
            "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS},
            "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS},
        })
        dept_info["total"] += 1
        if has_target:
            dept_info["target_set"] += 1
        if self_done:
            dept_info["self_done"] += 1
        if mgr_done:
            dept_info["done"] += 1
        if dept_done:
            dept_info["dept_done"] += 1
        if vp_done:
            dept_info["vp_done"] += 1
        if final_grade in GRADE_OPTIONS:
            dept_info["grades"][final_grade] += 1
        is_sales = _extract_text(f.get("是否绩效关联奖金"), "").strip() == "否"
        if is_sales:
            dept_info["sales_total"] += 1
            if mgr_done:
                dept_info["sales_done"] += 1
            if final_grade in GRADE_OPTIONS:
                dept_info["sales_grades"][final_grade] += 1
        else:
            dept_info["non_sales_total"] += 1
            if mgr_done:
                dept_info["non_sales_done"] += 1
            if final_grade in GRADE_OPTIONS:
                dept_info["non_sales_grades"][final_grade] += 1

    report_sales = [r for r in report_records if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "否"]
    report_non_sales = [r for r in report_records if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"]
    report_sales_for_vp_quota = [r for r in report_records_for_vp_quota if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "否"] if report_records_for_vp_quota else []
    report_non_sales_for_vp_quota = [r for r in report_records_for_vp_quota if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"] if report_records_for_vp_quota else []
    report_sales_for_allcompany = [r for r in report_records_for_allcompany if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "否"]
    report_non_sales_for_allcompany = [r for r in report_records_for_allcompany if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"]
    report_sales_for_quota = [r for r in report_records_for_quota if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "否"]
    report_non_sales_for_quota = [r for r in report_records_for_quota if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"]
    has_bonus_no = len(report_sales) > 0 and len(report_non_sales) > 0
    report_scope = report_sales if report_sales else report_records
    if has_bonus_no:
        if "report_bonus_scope_filter" not in st.session_state:
            st.session_state.report_bonus_scope_filter = "全部"
        _sk = st.session_state.report_bonus_scope_filter
        report_scope = report_records if _sk == "全部" else (report_sales if _sk == "销售" else report_non_sales)
    base_cnt = len(report_scope) if report_scope else total_cnt
    scope_done = sum(1 for r in (report_scope or []) if _extract_text(r.get("fields", {}).get("上级评价是否完成"), "").strip() == "是")
    completion_rate = 0 if base_cnt == 0 else round(scope_done / base_cnt * 100, 1)
    grade_counts_bonus = Counter()
    for rec in (report_scope or report_records):
        f = rec.get("fields", {})
        vp_adj = _extract_text(f.get("分管高管调整考核结果"), "").strip()
        dept_adj = _extract_text(f.get("一级部门调整考核结果"), "").strip()
        mgr_grade = _extract_text(f.get("考核结果"), "").strip()
        final_grade = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
        fg = "-"
        for cand in [vp_adj, dept_adj, mgr_grade, final_grade]:
            if cand in GRADE_OPTIONS:
                fg = cand
                break
        if fg in GRADE_OPTIONS:
            grade_counts_bonus[fg] += 1

    def _build_dept_grade_stats(recs, use_total_as_base=False):
        dgs = {}
        for rec in (recs or []):
            rf = rec.get("fields", {})
            dept_l1 = _clean_dept_name(rf.get("一级部门")) or "未分配部门"
            vp_adj = _extract_text(rf.get("分管高管调整考核结果"), "").strip()
            dept_adj = _extract_text(rf.get("一级部门调整考核结果"), "").strip()
            mgr_grade = _extract_text(rf.get("考核结果"), "").strip()
            final_grade = _extract_text(rf.get("最终绩效结果") or rf.get("最终考核结果"), "").strip()
            fg = "-"
            for cand in [vp_adj, dept_adj, mgr_grade, final_grade]:
                if cand in GRADE_OPTIONS:
                    fg = cand
                    break
            dg = dgs.setdefault(dept_l1, {"grade_counts": Counter(), "bonus_cnt": 0, "total_cnt": 0})
            dg["total_cnt"] += 1
            if _extract_text(rf.get("是否绩效关联奖金"), "").strip() == "是":
                dg["bonus_cnt"] += 1
            if fg in GRADE_OPTIONS:
                dg["grade_counts"][fg] += 1
        for dept_name, dg in dgs.items():
            dg["base_cnt"] = dg["total_cnt"] if use_total_as_base else (dg["bonus_cnt"] if dg["bonus_cnt"] > 0 else dg["total_cnt"])
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
        return dgs

    # 配额上限基数：仅受销售/非销售影响，用部门原数；实际人数：受全部筛选影响；筛选分管高管时上限基数也排除分管高管本人
    _recs_quota = report_records_for_quota if not has_bonus_no else (
        report_records_for_quota if st.session_state.get("report_bonus_scope_filter", "全部") == "全部"
        else (report_sales_for_quota if st.session_state.report_bonus_scope_filter == "销售" else report_non_sales_for_quota)
    )
    # 各部门配额：仅排除一级部门负责人；负责范围总体含分管高管本人
    _recs_quota_for_dept = _recs_quota
    if _sel_vp and _sel_vp != "全部":
        _vp_in = lambda r: _sel_vp in _extract_text(r.get("fields", {}).get("分管高管") or r.get("fields", {}).get("高管"), "").strip()
        _vp_self = lambda r: _extract_text(r.get("fields", {}).get("姓名"), "").strip() == _sel_vp
        _recs_quota_for_dept = [r for r in _recs_quota if _vp_in(r) or _vp_self(r)]
    _recs_quota_dept = [r for r in _recs_quota_for_dept if not _is_dept_head(r)]
    _recs_actual_dept = [r for r in report_records if not _is_dept_head(r)]
    _dept_quota_base = _build_dept_grade_stats(_recs_quota_dept, use_total_as_base=True)
    _dept_actual = _build_dept_grade_stats(_recs_actual_dept, use_total_as_base=True)
    dept_grade_stats = {}
    for dept_name in sorted(set(_dept_quota_base.keys()) | set(_dept_actual.keys())):
        qb = _dept_quota_base.get(dept_name, {})
        ac = _dept_actual.get(dept_name, {})
        dept_grade_stats[dept_name] = {
            "base_cnt": qb.get("base_cnt", 1),
            "sa_theory": qb.get("sa_theory", 0),
            "bp_theory": qb.get("bp_theory", 0),
            "sapb_theory": qb.get("sapb_theory", 0),
            "actual_sa": ac.get("actual_sa", 0),
            "actual_bp": ac.get("actual_bp", 0),
            "actual_sapb": ac.get("actual_sapb", 0),
            "actual_b": ac.get("actual_b", 0),
            "actual_bm": ac.get("actual_bm", 0),
            "actual_c": ac.get("actual_c", 0),
            "actual_sum": ac.get("actual_sum", 0),
            "grade_counts": ac.get("grade_counts", Counter()),
        }

    # 全公司总体配额：包含分管高管，仅受销售/非销售影响
    _recs_actual_allcompany = report_records_for_allcompany if st.session_state.get("report_bonus_scope_filter", "全部") == "全部" else (
        report_sales_for_allcompany if st.session_state.report_bonus_scope_filter == "销售" else report_non_sales_for_allcompany
    )
    _dept_quota_base_allcompany = _build_dept_grade_stats(_recs_quota, use_total_as_base=True)
    _dept_actual_allcompany = _build_dept_grade_stats(_recs_actual_allcompany, use_total_as_base=True)
    dept_grade_stats_allcompany = {}
    for dept_name in sorted(set(_dept_quota_base_allcompany.keys()) | set(_dept_actual_allcompany.keys())):
        qb = _dept_quota_base_allcompany.get(dept_name, {})
        ac = _dept_actual_allcompany.get(dept_name, {})
        dept_grade_stats_allcompany[dept_name] = {
            "base_cnt": qb.get("base_cnt", 1),
            "sa_theory": qb.get("sa_theory", 0),
            "bp_theory": qb.get("bp_theory", 0),
            "sapb_theory": qb.get("sapb_theory", 0),
            "actual_sa": ac.get("actual_sa", 0),
            "actual_bp": ac.get("actual_bp", 0),
            "actual_sapb": ac.get("actual_sapb", 0),
            "actual_b": ac.get("actual_b", 0),
            "actual_bm": ac.get("actual_bm", 0),
            "actual_c": ac.get("actual_c", 0),
            "actual_sum": ac.get("actual_sum", 0),
        }

    # 分管高管配额总数：负责范围总体 = 该分管名下全员（不含本人），仅受销售/非销售影响，不受一级部门影响
    dept_grade_stats_vp = {}
    if _sel_vp and _sel_vp != "全部" and report_records_for_vp_quota:
        _vp_in_scope = lambda r: _sel_vp in _extract_text(r.get("fields", {}).get("分管高管") or r.get("fields", {}).get("高管"), "").strip()
        _vp_not_self = lambda r: _extract_text(r.get("fields", {}).get("姓名"), "").strip() != _sel_vp
        _recs_quota_vp = [r for r in _recs_quota if _vp_in_scope(r) and _vp_not_self(r)]
        _recs_actual_vp = report_records_for_vp_quota if st.session_state.get("report_bonus_scope_filter", "全部") == "全部" else (
            report_sales_for_vp_quota if st.session_state.report_bonus_scope_filter == "销售" else report_non_sales_for_vp_quota
        )
        _dept_quota_base_vp = _build_dept_grade_stats(_recs_quota_vp, use_total_as_base=True)
        _dept_actual_vp = _build_dept_grade_stats(_recs_actual_vp, use_total_as_base=True)
        for dept_name in sorted(set(_dept_quota_base_vp.keys()) | set(_dept_actual_vp.keys())):
            qb = _dept_quota_base_vp.get(dept_name, {})
            ac = _dept_actual_vp.get(dept_name, {})
            dept_grade_stats_vp[dept_name] = {
                "base_cnt": qb.get("base_cnt", 1),
                "sa_theory": qb.get("sa_theory", 0),
                "bp_theory": qb.get("bp_theory", 0),
                "sapb_theory": qb.get("sapb_theory", 0),
                "actual_sa": ac.get("actual_sa", 0),
                "actual_bp": ac.get("actual_bp", 0),
                "actual_sapb": ac.get("actual_sapb", 0),
                "actual_b": ac.get("actual_b", 0),
                "actual_bm": ac.get("actual_bm", 0),
                "actual_c": ac.get("actual_c", 0),
                "actual_sum": ac.get("actual_sum", 0),
                "grade_counts": ac.get("grade_counts", Counter()),
            }

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
    _neutral = "#b7bdc8"
    _cell_style = "font-size:14px;font-weight:700;white-space:nowrap;"
    def _th(txt):
        return f"<th style='text-align:center;{_cell_style}'>{txt}</th>"
    def _td(val, color):
        return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}</td>"
    def _td_label(txt):
        return f"<td style='text-align:center;color:#b7bdc8;{_cell_style}'>{txt}</td>"
    def _td_with_hint(val, color, hint_text):
        hint = f"<span title='{hint_text}' style='cursor:help;font-size:12px;margin-left:4px;color:#90A4AE;'>ⓘ</span>"
        return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}{hint}</td>"
    def _td_over(val, color, is_over):
        if is_over:
            hint = "<span title='人数超过上限人数，请修改' style='cursor:help;font-size:12px;margin-left:4px;color:#F44336;'>ⓘ</span>"
            return f"<td style='text-align:center;color:#F44336;font-weight:800;border:1px solid #F44336;border-radius:4px;{_cell_style}'>{val}{hint}</td>"
        return _td(val, color)
    bp_hint = "默认15%，根据实际的B-/C占比调整向上浮动"
    _over_sa = actual_sa > sa_theory
    _over_bp = actual_bp > bp_theory
    _over_sapb = actual_sapb > sapb_theory

    st.sidebar.markdown("""
    <style>
    div[data-testid="stSidebar"] button[kind="primary"],
    div[data-testid="stSidebar"] [data-testid="baseButton-primary"] { font-size: 0.8em !important; }
    </style>
    """, unsafe_allow_html=True)
    st.sidebar.markdown(f"### 👋 欢迎 {user_name}！")
    st.sidebar.markdown("**系统管理员**")
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    st.sidebar.markdown("### 📅 当前展示周期")
    st.sidebar.caption("切换后前台自动更新为该周期")
    def _on_display_cycle_change():
        v = st.session_state.get("admin_display_cycle")
        if v:
            _write_display_cycle(v)
            st.rerun()
    _cur_norm = _normalize_cycle_display(current_cycle) or current_cycle
    _sel_display = st.sidebar.selectbox("展示周期", options=_available_cycles, index=_available_cycles.index(_cur_norm) if _cur_norm in _available_cycles else 0, key="admin_display_cycle", label_visibility="collapsed", on_change=_on_display_cycle_change)
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    with st.sidebar.expander("📋 后台配置", expanded=False):
        st.info("💡 无论部署环境如何，请通过本模块修改配置，请勿直接编辑配置文件。")
        st.markdown("""
        <style>
        div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type button,
        div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type [data-testid="baseButton-secondary"] {
            font-size: 0.75em !important;
            background-color: rgba(33, 150, 243, 0.25) !important;
        }
        div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type button[kind="primary"],
        div[data-testid="stSidebar"] [data-testid="stExpander"]:first-of-type [data-testid="baseButton-primary"] {
            background-color: rgba(33, 150, 243, 0.5) !important;
        }
        </style>
        """, unsafe_allow_html=True)
        st.caption("绩效结果和评语查看权限")
        _result_visible = _read_result_visible()
        _r1, _r2 = st.columns(2)
        with _r1:
            if st.button("开放查看", key="admin_open_result", use_container_width=True, disabled=_result_visible):
                _write_result_visible(True)
                st.success("已开放")
                st.rerun()
        with _r2:
            if st.button("关闭查看", key="admin_close_result", use_container_width=True, disabled=not _result_visible):
                _write_result_visible(False)
                st.success("已关闭")
                st.rerun()
        st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
        st.caption("配置周期")
        _cfg_has_old = ("2026上半年" in (_read_admin_config().get("cycle_configs") or {})) or (_read_display_cycle() == "2026上半年")
        if _cfg_has_old and st.button("🔄 将「2026上半年」统一为「2026年上半年」", key="admin_migrate_cycle"):
            if _migrate_cycle_to_preferred("2026上半年", "2026年上半年"):
                st.success("已迁移，公告等配置已同步")
                st.rerun()
            else:
                st.info("已是最新格式")
        _new_cycle = st.text_input("添加新周期（如 2026下半年）", key="admin_new_cycle", placeholder="输入后点击添加", label_visibility="collapsed")
        if st.button("➕ 添加周期", key="admin_add_cycle") and _new_cycle and _new_cycle.strip():
            _add_cycle_to_config(_new_cycle.strip())
            st.success(f"已添加「{_new_cycle.strip()}」")
            st.rerun()
        _config_cycle = st.selectbox("选择要配置的周期", options=_get_available_cycles(all_records), key="admin_config_cycle", label_visibility="collapsed")
        if st.button("移除周期", key="admin_remove_cycle"):
            if _remove_cycle_from_config(_config_cycle):
                st.success(f"已移除「{_config_cycle}」")
                st.rerun()
            else:
                st.warning("该周期来自飞书记录，无法移除；或周期不存在于配置中")
        st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
        st.caption("公告")
        _cc = _read_cycle_config(_config_cycle)
        _ann_loc_cfg = st.selectbox("公告位置", options=ANNOUNCE_LOCATIONS, key="admin_ann_loc_cfg", label_visibility="collapsed")
        _ann_val = st.text_area("公告内容", value=_cc["announcements"].get(_ann_loc_cfg) or "", key="admin_ann_cfg", height=80, label_visibility="collapsed", placeholder="由管理员填写，留空则无公告")
        if st.button("保存公告", key="admin_save_ann_cfg"):
            if _write_admin_announcement(_ann_loc_cfg, _ann_val, cycle=_config_cycle):
                st.success("已保存")
            else:
                st.error("保存失败，可能是目录无写权限。可设置环境变量 XQPS_CONFIG_DIR 指定可写路径。")
        st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
        st.caption("文档链接")
        _doc_links_cfg = dict(_read_admin_doc_links(_config_cycle))
        for i, _dn in enumerate(DOC_LINK_NAMES):
            _def = _doc_links_cfg.get(_dn) or (DEFAULT_DOC_LINK if i == 0 else "")
            _val = st.text_input(_dn, value=_def, key=f"admin_doc_cfg_{i}", placeholder=f"粘贴 {_dn} 链接")
            _doc_links_cfg[_dn] = (_val or "").strip()
        if st.button("保存文档链接", key="admin_save_doc_cfg"):
            if _write_admin_doc_links(_doc_links_cfg, cycle=_config_cycle):
                st.success("已保存")
            else:
                st.error("保存失败，可能是目录无写权限。可设置环境变量 XQPS_CONFIG_DIR 指定可写路径。")
        st.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:10px 0;'/>", unsafe_allow_html=True)
        if st.button("✅ 配置完成", type="primary", key="admin_config_done"):
            if _set_cycle_config_complete(_config_cycle, True):
                st.success(f"已标记「{_config_cycle}」配置完成")
                st.rerun()
            else:
                st.error("保存失败，可能是目录无写权限。可设置环境变量 XQPS_CONFIG_DIR 指定可写路径。")
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    _edit_disabled = _read_module_edit_disabled()
    _full_shutdown = _read_full_shutdown()
    _pending_action = st.session_state.get("admin_pending_action", None)

    def _render_confirm(module_key, action_label, on_confirm):
        st.warning(f"确认{action_label}？")
        _c1, _c2 = st.columns(2)
        with _c1:
            if st.button("确认", type="primary", key=f"admin_confirm_{module_key}"):
                on_confirm()
                st.session_state.pop("admin_pending_action", None)
                st.rerun()
        with _c2:
            if st.button("取消", key=f"admin_cancel_{module_key}"):
                st.session_state.pop("admin_pending_action", None)
                st.rerun()

    with st.sidebar.expander("🚨 关停权限", expanded=bool(_pending_action)):
        st.caption("关闭编辑权限后，用户仅可查看不可操作。点击后需确认才执行。")
        if _pending_action == "close_员工自评":
            _render_confirm("员工自评", "关闭员工自评编辑权限", lambda: _write_module_edit_disabled("员工自评", True))
        elif _pending_action == "open_员工自评":
            _render_confirm("员工自评", "开启员工自评编辑权限", lambda: _write_module_edit_disabled("员工自评", False))
        elif _pending_action == "close_上级评分":
            _render_confirm("上级评分", "关闭上级评价编辑权限", lambda: _write_module_edit_disabled("上级评分", True))
        elif _pending_action == "open_上级评分":
            _render_confirm("上级评分", "开启上级评价编辑权限", lambda: _write_module_edit_disabled("上级评分", False))
        elif _pending_action == "close_一级部门负责人调整":
            _render_confirm("一级部门", "关闭一级部门负责人调整编辑权限", lambda: _write_module_edit_disabled("一级部门负责人调整", True))
        elif _pending_action == "open_一级部门负责人调整":
            _render_confirm("一级部门", "开启一级部门负责人调整编辑权限", lambda: _write_module_edit_disabled("一级部门负责人调整", False))
        elif _pending_action == "close_分管高管调整":
            _render_confirm("分管高管", "关闭分管高管调整编辑权限", lambda: _write_module_edit_disabled("分管高管调整", True))
        elif _pending_action == "open_分管高管调整":
            _render_confirm("分管高管", "开启分管高管调整编辑权限", lambda: _write_module_edit_disabled("分管高管调整", False))
        elif _pending_action == "full":
            st.warning("确认一键关闭所有编辑和查看权限？管理员仍可查看全部内容。")
            _c1, _c2 = st.columns(2)
            with _c1:
                if st.button("确认关停", type="primary", key="admin_confirm_full"):
                    _write_full_shutdown(True)
                    st.session_state.pop("admin_pending_action", None)
                    st.rerun()
            with _c2:
                if st.button("取消", key="admin_cancel_full"):
                    st.session_state.pop("admin_pending_action", None)
                    st.rerun()
        elif _pending_action == "open_full":
            st.warning("确认一键开启所有编辑和查看权限？")
            _c1, _c2 = st.columns(2)
            with _c1:
                if st.button("确认开启", type="primary", key="admin_confirm_open_full"):
                    _write_full_shutdown(False)
                    st.session_state.pop("admin_pending_action", None)
                    st.rerun()
            with _c2:
                if st.button("取消", key="admin_cancel_open_full"):
                    st.session_state.pop("admin_pending_action", None)
                    st.rerun()
        else:
            def _row_buttons(label, mod_key, close_key, open_key):
                r1, r2 = st.columns(2)
                with r1:
                    if st.button("关闭", key=close_key, use_container_width=True, disabled=_edit_disabled.get(mod_key)):
                        st.session_state["admin_pending_action"] = f"close_{mod_key}"
                        st.rerun()
                with r2:
                    if st.button("开启", key=open_key, use_container_width=True, disabled=not _edit_disabled.get(mod_key)):
                        st.session_state["admin_pending_action"] = f"open_{mod_key}"
                        st.rerun()
            st.caption("员工自评")
            _row_buttons("员工自评", "员工自评", "admin_close_self", "admin_open_self")
            st.caption("上级评价")
            _row_buttons("上级评价", "上级评分", "admin_close_mgr", "admin_open_mgr")
            st.caption("一级部门负责人调整")
            _row_buttons("一级部门", "一级部门负责人调整", "admin_close_dept", "admin_open_dept")
            st.caption("分管高管调整")
            _row_buttons("分管高管", "分管高管调整", "admin_close_vp", "admin_open_vp")
            st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
            st.caption("一键操作")
            _full_r1, _full_r2 = st.columns(2)
            with _full_r1:
                if st.button("一键关闭", type="primary", key="admin_btn_full", use_container_width=True, disabled=_full_shutdown):
                    st.session_state["admin_pending_action"] = "full"
                    st.rerun()
            with _full_r2:
                if st.button("一键开启", type="primary", key="admin_open_all", use_container_width=True, disabled=not _full_shutdown):
                    st.session_state["admin_pending_action"] = "open_full"
                    st.rerun()
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    if st.sidebar.button("🚪 退出登录", use_container_width=True):
        st.session_state.clear()
        st.rerun()

    st.markdown("<div class='module-title'>📊 全公司绩效概览</div>", unsafe_allow_html=True)
    _kpi_html = f"""
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;">
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);" title="含管理员及分管高管在内，全公司参与考核人数；以下绩效统计按筛选展示">
            <div class="report-kpi-label" title="含管理员及分管高管在内，全公司参与考核人数；以下绩效统计按筛选展示" style="cursor:help;white-space:nowrap;">考核总人数 ⓘ</div>
            <div style="font-size:32px;font-weight:700;color:#42A5F5;margin-top:8px;">{kpi_total}</div>
        </div>
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);" title="对应「最终考核结果」不为空">
            <div class="report-kpi-label" title="对应「最终考核结果」不为空" style="cursor:help;white-space:nowrap;">已完成评价 ⓘ</div>
            <div style="font-size:32px;font-weight:700;color:#26A69A;margin-top:8px;">{kpi_done}</div>
        </div>
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);">
            <div class="report-kpi-label" style="white-space:nowrap;">总体完成率</div>
            <div style="font-size:32px;font-weight:700;color:#FFA726;margin-top:8px;">{kpi_rate}%</div>
        </div>
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);">
            <div class="report-kpi-label" style="white-space:nowrap;">剩余未评</div>
            <div style="font-size:32px;font-weight:700;color:#EF5350;margin-top:8px;">{kpi_remaining}</div>
        </div>
    </div>
    """
    st.markdown(_kpi_html, unsafe_allow_html=True)

    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title'>📋 各部门考核环节进度</div>", unsafe_allow_html=True)
    _f1, _f2, _f3 = st.columns(3)
    with _f1:
        st.selectbox("分管高管", options=_vp_opts, key="admin_vp_filter", format_func=lambda x: "全部" if x == "全部" else x)
    with _f2:
        st.selectbox("一级部门", options=_dept_opts, key="admin_dept_filter", format_func=lambda x: "全部部门" if x == "全部部门" else x)
    with _f3:
        if has_bonus_no:
            st.markdown(
                '<div style="border-left: 4px solid #26A69A; padding: 8px 0 8px 12px; border-radius: 0 6px 6px 0; background: rgba(38, 166, 154, 0.06);"><span style="color: #26A69A; font-weight: 600; font-size: 14px;">销售/非销售</span></div>',
                unsafe_allow_html=True,
            )
            st.selectbox("销售/非销售", options=["全部", "销售", "非销售"], key="report_bonus_scope_filter", label_visibility="collapsed")
        else:
            st.caption("")
    _filter_active = []
    if _sel_vp and _sel_vp != "全部":
        _filter_active.append(f"分管高管：{_sel_vp}")
    if _sel_dept and _sel_dept != "全部部门":
        _filter_active.append(f"一级部门：{_sel_dept}")
    if has_bonus_no and st.session_state.get("report_bonus_scope_filter", "全部") != "全部":
        _filter_active.append(f"销售/非销售：{st.session_state.report_bonus_scope_filter}")
    if _filter_active:
        _hint = f"💡 当前筛选：{' | '.join(_filter_active)}"
        _stat_parts = []
        if _sel_vp and _sel_vp != "全部":
            _stat_parts.append("统计均不包含分管高管本人")
        if _sel_dept and _sel_dept != "全部部门":
            _stat_parts.append("一级部门不含一级部门负责人本人")
        if _stat_parts:
            _hint += "。" + "，".join(_stat_parts) + "。"
        st.markdown(
            f"<div style='background:rgba(2,119,189,0.15);border:1px solid rgba(2,119,189,0.4);border-radius:6px;padding:8px 12px;font-size:13px;color:#66b2ff;margin-top:8px;margin-bottom:24px;'>{_hint}</div>",
            unsafe_allow_html=True,
        )
    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
    _dept_stats_for_table = dept_stats
    _recs_for_allcompany = report_records_for_allcompany
    if has_bonus_no and st.session_state.report_bonus_scope_filter != "全部":
        _recs_for_allcompany = report_sales_for_allcompany if st.session_state.report_bonus_scope_filter == "销售" else report_non_sales_for_allcompany
        _dept_stats_for_table = {}
        _recs = report_sales if st.session_state.report_bonus_scope_filter == "销售" else report_non_sales
        for rec in _recs:
            f = rec.get("fields", {})
            dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
            has_target = any(_extract_text(f.get(f"工作目标{i}及总结"), "").strip() for i in range(1, 6))
            self_done = _extract_text(f.get("自评是否提交"), "").strip() == "是"
            mgr_done = _extract_text(f.get("上级评价是否完成"), "").strip() == "是"
            dept_done = _extract_text(f.get("一级部门调整完毕"), "").strip() == "是"
            vp_done = _extract_text(f.get("分管高管调整完毕"), "").strip() == "是"
            vp_adj = _extract_text(f.get("分管高管调整考核结果"), "").strip()
            dept_adj = _extract_text(f.get("一级部门调整考核结果"), "").strip()
            mgr_grade = _extract_text(f.get("考核结果"), "").strip()
            final_from_field = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
            final_grade = "-"
            for cand in [vp_adj, dept_adj, mgr_grade, final_from_field]:
                if cand in GRADE_OPTIONS:
                    final_grade = cand
                    break
            _base = {"total": 0, "done": 0, "target_set": 0, "self_done": 0, "dept_done": 0, "vp_done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}}
            di = _dept_stats_for_table.setdefault(dept_l1, {**_base, "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS}, "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS}})
            di["total"] += 1
            if has_target:
                di["target_set"] += 1
            if self_done:
                di["self_done"] += 1
            if mgr_done:
                di["done"] += 1
            if dept_done:
                di["dept_done"] += 1
            if vp_done:
                di["vp_done"] += 1
            if final_grade in GRADE_OPTIONS:
                di["grades"][final_grade] += 1
    # 全公司行：不受一级部门筛选影响，从 _recs_for_allcompany 汇总
    _dept_stats_for_allcompany = {}
    for rec in _recs_for_allcompany:
        f = rec.get("fields", {})
        dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
        has_target = any(_extract_text(f.get(f"工作目标{i}及总结"), "").strip() for i in range(1, 6))
        self_done = _extract_text(f.get("自评是否提交"), "").strip() == "是"
        mgr_done = _extract_text(f.get("上级评价是否完成"), "").strip() == "是"
        dept_done = _extract_text(f.get("一级部门调整完毕"), "").strip() == "是"
        vp_done = _extract_text(f.get("分管高管调整完毕"), "").strip() == "是"
        vp_adj = _extract_text(f.get("分管高管调整考核结果"), "").strip()
        dept_adj = _extract_text(f.get("一级部门调整考核结果"), "").strip()
        mgr_grade = _extract_text(f.get("考核结果"), "").strip()
        final_from_field = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
        final_grade = "-"
        for cand in [vp_adj, dept_adj, mgr_grade, final_from_field]:
            if cand in GRADE_OPTIONS:
                final_grade = cand
                break
        _base = {"total": 0, "done": 0, "target_set": 0, "self_done": 0, "dept_done": 0, "vp_done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}}
        di = _dept_stats_for_allcompany.setdefault(dept_l1, {**_base, "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS}, "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS}})
        di["total"] += 1
        if has_target:
            di["target_set"] += 1
        if self_done:
            di["self_done"] += 1
        if mgr_done:
            di["done"] += 1
        if dept_done:
            di["dept_done"] += 1
        if vp_done:
            di["vp_done"] += 1
        if final_grade in GRADE_OPTIONS:
            di["grades"][final_grade] += 1
    _totals_allcompany = {"target_set": 0, "self_done": 0, "done": 0, "dept_done": 0, "vp_done": 0, "total": 0}
    for dval in _dept_stats_for_allcompany.values():
        for k in _totals_allcompany:
            _totals_allcompany[k] += dval.get(k, 0)
    def _pct(t, n):
        return f"{round(n / t * 100, 1)}%" if t and t > 0 else "0%"

    _step_cols = [
        ("目标设定", "target_set"),
        ("自评", "self_done"),
        ("上级评价", "done"),
        ("部门调整", "dept_done"),
        ("高管调整", "vp_done"),
    ]
    _table_rows = []
    _totals = {"target_set": 0, "self_done": 0, "done": 0, "dept_done": 0, "vp_done": 0, "total": 0}
    for dept_name, dval in sorted(_dept_stats_for_table.items(), key=lambda x: x[0]):
        t = dval.get("total", 0) or 1
        row = {"部门": dept_name}
        for label, key in _step_cols:
            n = dval.get(key, 0)
            _totals[key] = _totals.get(key, 0) + n
            row[f"{label}"] = n
            row[f"{label}%"] = _pct(t, n)
        row["总数"] = dval.get("total", 0)
        _totals["total"] += dval.get("total", 0)
        _table_rows.append(row)

    t_all = int(_totals_allcompany.get("total", 0) or 1)
    _summary_row = {"部门": "全公司"}
    for label, key in _step_cols:
        n = int(_totals_allcompany.get(key, 0) or 0)
        _summary_row[f"{label}"] = n
        _summary_row[f"{label}%"] = _pct(t_all, n)
    _summary_row["总数"] = int(_totals_allcompany.get("total", 0) or 0)
    _table_rows.insert(0, _summary_row)

    if _table_rows:
        _step_headers = []
        for label, _ in _step_cols:
            _step_headers.extend([label, f"{label}%"])
        _all_cols = ["部门"] + _step_headers + ["总数"]
        _step_df = pd.DataFrame(_table_rows, columns=_all_cols)

        def _admin_step_style(row):
            is_total = str(row.get("部门", "")) == "全公司"
            base = "font-weight: 700; background-color: rgba(33,150,243,0.18); color: #42A5F5; border-top: 2px solid rgba(33,150,243,0.5); border-bottom: 2px solid rgba(33,150,243,0.5);" if is_total else ""
            return [base or "text-align: center;"] * len(row)

        _step_df = _step_df.style.set_properties(**{"text-align": "center"}).apply(_admin_step_style, axis=1)
        st.dataframe(_step_df, use_container_width=True, hide_index=True)
    else:
        st.caption("暂无数据")

    # 配额模块：各部门 S/A、B+ 等上限与实际人数（与上方筛选联动，仅展示当前筛选范围内的部门）
    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title'>📊 配额模块</div>", unsafe_allow_html=True)
    _dept_grade_stats_filtered = {k: v for k, v in dept_grade_stats.items() if k in _dept_stats_for_table}
    if _dept_grade_stats_filtered:
        _q_cell = "font-size:14px;font-weight:700;white-space:nowrap;"
        def _q_th(t):
            return f"<th style='text-align:center;{_q_cell}'>{t}</th>"
        def _q_td(v, c):
            return f"<td style='text-align:center;color:{c};{_q_cell}'>{v}</td>"
        def _q_label(t):
            return f"<td style='text-align:center;color:#b7bdc8;{_q_cell}'>{t}</td>"
        def _q_hint(v, c, h):
            return f"<td style='text-align:center;color:{c};{_q_cell}'>{v}<span title='{h}' style='cursor:help;font-size:12px;margin-left:4px;color:#90A4AE;'>ⓘ</span></td>"
        def _q_over(v, c, over):
            if over:
                return f"<td style='text-align:center;color:#F44336;font-weight:800;border:1px solid #F44336;border-radius:4px;{_q_cell}'>{v}<span title='人数超过上限人数，请修改' style='cursor:help;font-size:12px;margin-left:4px;color:#F44336;'>ⓘ</span></td>"
            return f"<td style='text-align:center;color:{c};{_q_cell}'>{v}</td>"
        def _q_colspan(v, c, col=1):
            return f"<td style='text-align:center;color:{c};{_q_cell}' colspan='{col}'>{v}</td>"
        _q_header = _q_th("级别") + _q_th("S/A级别") + _q_th("B+级别") + _q_th("B+及以上级别") + _q_th("B级别") + _q_th("B-级别") + _q_th("C级别") + _q_th("SUM (人)")
        _q_bp_hint = "默认15%，根据实际的B-/C占比调整向上浮动"
        # 全公司总体配额表：S/A=20%、B+=15% 按总人数统一计算，不用各部门 floor 后求和
        if dept_grade_stats_allcompany:
            _all_base = sum(dg.get("base_cnt", 0) for dg in dept_grade_stats_allcompany.values())
            _all_actual_sa = sum(dg.get("actual_sa", 0) for dg in dept_grade_stats_allcompany.values())
            _all_actual_bp = sum(dg.get("actual_bp", 0) for dg in dept_grade_stats_allcompany.values())
            _all_bmc = sum(dg.get("actual_bm", 0) + dg.get("actual_c", 0) for dg in dept_grade_stats_allcompany.values())
            _all_sa_theory = math.floor(_all_base * 0.20) if _all_base else 0
            _all_bp_base = math.floor(_all_base * 0.15) if _all_base else 0
            _all_bp_cap = math.floor(_all_base * 0.25) if _all_base else 0
            _all_bp_theory = min(_all_bp_cap, _all_bp_base + _all_bmc)
            _all_sapb_theory = _all_sa_theory + _all_bp_theory
            _all_actual_sapb = _all_actual_sa + _all_actual_bp
            _all_actual_b = sum(dg.get("actual_b", 0) for dg in dept_grade_stats_allcompany.values())
            _all_actual_bm = sum(dg.get("actual_bm", 0) for dg in dept_grade_stats_allcompany.values())
            _all_actual_c = sum(dg.get("actual_c", 0) for dg in dept_grade_stats_allcompany.values())
            _all_actual_sum = sum(dg.get("actual_sum", 0) for dg in dept_grade_stats_allcompany.values())
            _all_tot = {
                "sa_theory": _all_sa_theory, "bp_theory": _all_bp_theory, "sapb_theory": _all_sapb_theory,
                "base_cnt": _all_base,
                "actual_sa": _all_actual_sa, "actual_bp": _all_actual_bp, "actual_sapb": _all_actual_sapb,
                "actual_b": _all_actual_b, "actual_bm": _all_actual_bm, "actual_c": _all_actual_c, "actual_sum": _all_actual_sum,
            }
            _all_over_sa = _all_tot["actual_sa"] > _all_tot["sa_theory"]
            _all_over_bp = _all_tot["actual_bp"] > _all_tot["bp_theory"]
            _all_over_sapb = _all_tot["actual_sapb"] > _all_tot["sapb_theory"]
            _all_c_sa = "#F44336" if _all_over_sa else "#4CAFEE"
            _all_c_bp = "#F44336" if _all_over_bp else "#8BC34A"
            _all_c_sapb = "#F44336" if _all_over_sapb else "#00BCD4"
            _all_theory_row = (
                _q_label("上限人数")
                + _q_td(_all_tot["sa_theory"], "#b7bdc8")
                + _q_hint(_all_tot["bp_theory"], "#b7bdc8", _q_bp_hint)
                + _q_td(_all_tot["sapb_theory"], "#b7bdc8")
                + _q_label("剔除绩优/差")
                + _q_colspan("按实际评价", "#b7bdc8", 2)
                + _q_td(_all_tot["base_cnt"], "#b7bdc8")
            )
            _all_actual_row = (
                _q_label("实际人数")
                + _q_over(_all_tot["actual_sa"], _all_c_sa, _all_over_sa)
                + _q_over(_all_tot["actual_bp"], _all_c_bp, _all_over_bp)
                + _q_over(_all_tot["actual_sapb"], _all_c_sapb, _all_over_sapb)
                + _q_td(_all_tot["actual_b"], "#90A4AE")
                + _q_td(_all_tot["actual_bm"], "#FFC107")
                + _q_td(_all_tot["actual_c"], "#F44336")
                + _q_td(_all_tot["actual_sum"], "#b7bdc8")
            )
            _all_quota_html = f"""
            <div style='overflow-x:auto; margin-bottom:16px;'>
            <div style='font-size:16px;font-weight:700;color:#66b2ff;margin-bottom:8px;'>全公司总体配额</div>
            <table style='width:100%;border-collapse:collapse;text-align:center;border:1px solid rgba(255,255,255,0.15);'>
            <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{_q_header}</tr></thead>
            <tbody>
            <tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{_all_theory_row}</tr>
            <tr>{_all_actual_row}</tr>
            </tbody>
            </table>
            </div>
            """
            st.markdown(_all_quota_html, unsafe_allow_html=True)
        # 分管高管配额总数：S/A=20%、B+=15% 按总人数统一计算，不用各部门 floor 后求和
        if _sel_vp and _sel_vp != "全部" and dept_grade_stats_vp:
            _vp_base = sum(dg.get("base_cnt", 0) for dg in dept_grade_stats_vp.values())
            _vp_actual_sa = sum(dg.get("actual_sa", 0) for dg in dept_grade_stats_vp.values())
            _vp_actual_bp = sum(dg.get("actual_bp", 0) for dg in dept_grade_stats_vp.values())
            _vp_bmc = sum(dg.get("actual_bm", 0) + dg.get("actual_c", 0) for dg in dept_grade_stats_vp.values())
            _vp_sa_theory = math.floor(_vp_base * 0.20) if _vp_base else 0
            _vp_bp_base = math.floor(_vp_base * 0.15) if _vp_base else 0
            _vp_bp_cap = math.floor(_vp_base * 0.25) if _vp_base else 0
            _vp_bp_theory = min(_vp_bp_cap, _vp_bp_base + _vp_bmc)
            _vp_sapb_theory = _vp_sa_theory + _vp_bp_theory
            _vp_actual_sapb = _vp_actual_sa + _vp_actual_bp
            _vp_actual_b = sum(dg.get("actual_b", 0) for dg in dept_grade_stats_vp.values())
            _vp_actual_bm = sum(dg.get("actual_bm", 0) for dg in dept_grade_stats_vp.values())
            _vp_actual_c = sum(dg.get("actual_c", 0) for dg in dept_grade_stats_vp.values())
            _vp_actual_sum = sum(dg.get("actual_sum", 0) for dg in dept_grade_stats_vp.values())
            _vp_tot = {
                "sa_theory": _vp_sa_theory, "bp_theory": _vp_bp_theory, "sapb_theory": _vp_sapb_theory,
                "base_cnt": _vp_base,
                "actual_sa": _vp_actual_sa, "actual_bp": _vp_actual_bp, "actual_sapb": _vp_actual_sapb,
                "actual_b": _vp_actual_b, "actual_bm": _vp_actual_bm, "actual_c": _vp_actual_c, "actual_sum": _vp_actual_sum,
            }
            _vp_over_sa = _vp_tot["actual_sa"] > _vp_tot["sa_theory"]
            _vp_over_bp = _vp_tot["actual_bp"] > _vp_tot["bp_theory"]
            _vp_over_sapb = _vp_tot["actual_sapb"] > _vp_tot["sapb_theory"]
            _vp_c_sa = "#F44336" if _vp_over_sa else "#4CAFEE"
            _vp_c_bp = "#F44336" if _vp_over_bp else "#8BC34A"
            _vp_c_sapb = "#F44336" if _vp_over_sapb else "#00BCD4"
            _vp_theory_row = (
                _q_label("上限人数")
                + _q_td(_vp_tot["sa_theory"], "#b7bdc8")
                + _q_hint(_vp_tot["bp_theory"], "#b7bdc8", _q_bp_hint)
                + _q_td(_vp_tot["sapb_theory"], "#b7bdc8")
                + _q_label("剔除绩优/差")
                + _q_colspan("按实际评价", "#b7bdc8", 2)
                + _q_td(_vp_tot["base_cnt"], "#b7bdc8")
            )
            _vp_actual_row = (
                _q_label("实际人数")
                + _q_over(_vp_tot["actual_sa"], _vp_c_sa, _vp_over_sa)
                + _q_over(_vp_tot["actual_bp"], _vp_c_bp, _vp_over_bp)
                + _q_over(_vp_tot["actual_sapb"], _vp_c_sapb, _vp_over_sapb)
                + _q_td(_vp_tot["actual_b"], "#90A4AE")
                + _q_td(_vp_tot["actual_bm"], "#FFC107")
                + _q_td(_vp_tot["actual_c"], "#F44336")
                + _q_td(_vp_tot["actual_sum"], "#b7bdc8")
            )
            _vp_quota_html = f"""
            <div style='overflow-x:auto; margin-bottom:16px;'>
            <div style='font-size:16px;font-weight:700;color:#66b2ff;margin-bottom:8px;'>分管范围总数</div>
            <table style='width:100%;border-collapse:collapse;text-align:center;border:1px solid rgba(255,255,255,0.15);'>
            <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{_q_header}</tr></thead>
            <tbody>
            <tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{_vp_theory_row}</tr>
            <tr>{_vp_actual_row}</tr>
            </tbody>
            </table>
            </div>
            """
            st.markdown(_vp_quota_html, unsafe_allow_html=True)
            if not (_sel_vp and _sel_vp != "全部" and _sel_dept and _sel_dept != "全部部门"):
                st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 配额统计口径：负责范围总体配额中，不含分管高管（因为自己不能调整自己）。另，负责范围总体配额由于增加了一级部门负责人，总体额度会大于所有部门配额之和。</div>", unsafe_allow_html=True)
        if _sel_vp and _sel_vp != "全部":
            st.markdown("<div style='font-size:16px;font-weight:700;color:#66b2ff;margin-bottom:8px;'>各分管部门总数</div>", unsafe_allow_html=True)
        _q_rows = []
        for dept_name in sorted(_dept_grade_stats_filtered.keys()):
            dg = _dept_grade_stats_filtered[dept_name]
            _q_rows.append(f"<tr style='background:rgba(102,178,255,0.08);'><td colspan='8' style='text-align:left;padding:8px 12px;font-weight:700;color:#66b2ff;'>{dept_name}</td></tr>")
            theory_row = (
                _q_label("上限人数")
                + _q_td(dg.get("sa_theory", 0), "#b7bdc8")
                + _q_hint(dg.get("bp_theory", 0), "#b7bdc8", _q_bp_hint)
                + _q_td(dg.get("sapb_theory", 0), "#b7bdc8")
                + _q_label("剔除绩优/差")
                + _q_colspan("按实际评价", "#b7bdc8", 2)
                + _q_td(dg.get("base_cnt", 0), "#b7bdc8")
            )
            _over_sa = dg.get("actual_sa", 0) > dg.get("sa_theory", 0)
            _over_bp = dg.get("actual_bp", 0) > dg.get("bp_theory", 0)
            _over_sapb = dg.get("actual_sapb", 0) > dg.get("sapb_theory", 0)
            _c_sa = "#F44336" if _over_sa else "#4CAFEE"
            _c_bp = "#F44336" if _over_bp else "#8BC34A"
            _c_sapb = "#F44336" if _over_sapb else "#00BCD4"
            actual_row = (
                _q_label("实际人数")
                + _q_over(dg.get("actual_sa", 0), _c_sa, _over_sa)
                + _q_over(dg.get("actual_bp", 0), _c_bp, _over_bp)
                + _q_over(dg.get("actual_sapb", 0), _c_sapb, _over_sapb)
                + _q_td(dg.get("actual_b", 0), "#90A4AE")
                + _q_td(dg.get("actual_bm", 0), "#FFC107")
                + _q_td(dg.get("actual_c", 0), "#F44336")
                + _q_td(dg.get("actual_sum", 0), "#b7bdc8")
            )
            _q_rows.append(f"<tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{theory_row}</tr>")
            _q_rows.append(f"<tr>{actual_row}</tr>")
        _q_html = f"""
        <div style='overflow-x:auto; margin-top:12px;'>
        <table style='width:100%;border-collapse:collapse;text-align:center;border:1px solid rgba(255,255,255,0.15);'>
        <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{_q_header}</tr></thead>
        <tbody>{''.join(_q_rows)}</tbody>
        </table>
        </div>
        """
        st.markdown(_q_html, unsafe_allow_html=True)
        if _sel_vp and _sel_vp != "全部" and _sel_dept and _sel_dept != "全部部门":
            st.markdown(f"<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 当前筛选：分管高管：{_sel_vp} | 一级部门：{_sel_dept}。统计均不包含分管高管本人，一级部门不含一级部门负责人本人。</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 配额统计口径：各部门配额中，不含一级部门负责人（因其不能调整自己）。</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>暂无配额数据</div>", unsafe_allow_html=True)

    # 部门绩效详情：高管单列，各部门含一级部门负责人，按分管高管分组
    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title'>🧾 部门绩效详情</div>", unsafe_allow_html=True)
    _admin_exec, _admin_dept = _build_detail_stats(report_records)
    _admin_vp_to_depts = {}
    _admin_exec_by_name = {}  # 按高管本人姓名统计，仅 特殊判断=高管 的人
    for rec in report_records:
        f = rec.get("fields", {})
        vp_str = _extract_text(f.get("分管高管") or f.get("高管"), "").strip()
        if _is_executive(rec):
            # 高管只统计一次，不随分管高管数量重复
            exec_name = _extract_text(f.get("姓名"), "").strip() or "未知"
            if exec_name not in _admin_exec_by_name:
                _admin_exec_by_name[exec_name] = {"total": 0, "done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}, "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS}, "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS}}
            t = _admin_exec_by_name[exec_name]
            mgr_done = _extract_text(f.get("上级评价是否完成"), "").strip() == "是"
            fg = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
            if fg not in GRADE_OPTIONS:
                fg = "-"
            is_sales = _extract_text(f.get("是否绩效关联奖金"), "").strip() == "否"
            t["total"] += 1
            if mgr_done:
                t["done"] += 1
            if fg in GRADE_OPTIONS:
                t["grades"][fg] += 1
            if is_sales:
                t["sales_total"] += 1
                if mgr_done:
                    t["sales_done"] += 1
                if fg in GRADE_OPTIONS:
                    t["sales_grades"][fg] += 1
            else:
                t["non_sales_total"] += 1
                if mgr_done:
                    t["non_sales_done"] += 1
                if fg in GRADE_OPTIONS:
                    t["non_sales_grades"][fg] += 1
        else:
            for vp in vp_str.replace("，", ",").split(","):
                vp = vp.strip()
                if not vp:
                    continue
                dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
                _admin_vp_to_depts.setdefault(vp, set()).add(dept_l1)
    _admin_dept_rows = []

    _indent = "\u2003\u2003"  # 全角空格，用于缩进

    def _admin_row(dept, scope, t, d, g, indent=0):
        rv = round(d / t * 100, 1) if t else 0
        disp_dept = (_indent * indent) + dept if indent else dept
        return {"部门": disp_dept, "口径": scope, "总人数": t, "已完成": d, "完成率": "100%" if rv == 100 else f"{rv}%", "S级": g["S"], "A级": g["A"], "B+级": g["B+"], "B级": g["B"], "B-级": g["B-"], "C级": g["C"]}
    # 高管-部门对应：exec 的 分管高管；以及 exec 是否「负责部门」（exec 是否在 vp_to_depts 的 key 中）
    _admin_exec_to_vps = {}
    _admin_exec_responsible = set()  # 负责部门的高管（本人是分管高管）
    for rec in report_records:
        if not _is_executive(rec):
            continue
        f = rec.get("fields", {})
        exec_name = _extract_text(f.get("姓名"), "").strip() or "未知"
        vp_str = _extract_text(f.get("分管高管") or f.get("高管"), "").strip()
        for vp in vp_str.replace("，", ",").split(","):
            vp = vp.strip()
            if vp:
                _admin_exec_to_vps.setdefault(exec_name, set()).add(vp)
    for vp in _admin_vp_to_depts.keys():
        if vp in _admin_exec_by_name:
            _admin_exec_responsible.add(vp)
    # 人力资源部、战略发展部：若其所有分管高管都不在特殊判断=高管，则单列
    _single_list_depts = {"人力资源部", "战略发展部"}
    _dept_to_vps = {}
    for _avp, depts in _admin_vp_to_depts.items():
        for d in depts:
            if d in _single_list_depts:
                _dept_to_vps.setdefault(d, set()).add(_avp)
    _dept_need_single_list = {d for d in _single_list_depts if d in _dept_to_vps and not any(vp in _admin_exec_by_name for vp in _dept_to_vps[d])}
    for _adn in sorted(_dept_need_single_list):
        dv = _admin_dept.get(_adn, {})
        if not dv or dv.get("total", 0) == 0:
            continue
        _admin_dept_rows.append(_admin_row(_adn, "总", dv["total"], dv["done"], dv["grades"], indent=0))
        if (dv.get("sales_total") or 0) > 0 and (dv.get("non_sales_total") or 0) > 0:
            _admin_dept_rows.append(_admin_row(_adn, "销售", dv["sales_total"], dv["sales_done"], dv.get("sales_grades", dv["grades"]), indent=1))
            _admin_dept_rows.append(_admin_row(_adn, "非销售", dv["non_sales_total"], dv["non_sales_done"], dv.get("non_sales_grades", dv["grades"]), indent=1))
    # 按分管高管分组：每个分管下 1) 先出「本分管且负责部门」的高管 2) 再出「本分管但不负责部门」的高管（缩进）3) 再出部门（缩进）
    for _avp in sorted(_admin_vp_to_depts.keys()):
        # 1) 本分管是高管且负责部门：高管（VP）无缩进
        if _avp in _admin_exec_by_name and _avp in _admin_exec_responsible:
            dv = _admin_exec_by_name[_avp]
            if dv["total"] > 0:
                _admin_dept_rows.append(_admin_row(f"高管（{_avp}）", "总", dv["total"], dv["done"], dv["grades"], indent=0))
                if (dv.get("sales_total") or 0) > 0 and (dv.get("non_sales_total") or 0) > 0:
                    _admin_dept_rows.append(_admin_row(f"高管（{_avp}）", "销售", dv["sales_total"], dv["sales_done"], dv.get("sales_grades", dv["grades"]), indent=1))
                    _admin_dept_rows.append(_admin_row(f"高管（{_avp}）", "非销售", dv["non_sales_total"], dv["non_sales_done"], dv.get("non_sales_grades", dv["grades"]), indent=1))
        # 2) 本分管下其他高管（不负责部门，归本分管管）：缩进 1
        for _exec_name in sorted(_admin_exec_by_name.keys()):
            if _exec_name == _avp:
                continue
            if _exec_name in _admin_exec_responsible:
                continue
            if _avp not in _admin_exec_to_vps.get(_exec_name, set()):
                continue
            dv = _admin_exec_by_name[_exec_name]
            if dv["total"] > 0:
                _admin_dept_rows.append(_admin_row(f"高管（{_exec_name}）", "总", dv["total"], dv["done"], dv["grades"], indent=1))
                if (dv.get("sales_total") or 0) > 0 and (dv.get("non_sales_total") or 0) > 0:
                    _admin_dept_rows.append(_admin_row(f"高管（{_exec_name}）", "销售", dv["sales_total"], dv["sales_done"], dv.get("sales_grades", dv["grades"]), indent=2))
                    _admin_dept_rows.append(_admin_row(f"高管（{_exec_name}）", "非销售", dv["non_sales_total"], dv["non_sales_done"], dv.get("non_sales_grades", dv["grades"]), indent=2))
        # 3) 本分管负责的部门：缩进 1（人力资源部、战略发展部若分管非高管已单列，此处跳过）
        for _adn in sorted(_admin_vp_to_depts.get(_avp, [])):
            if _adn in _dept_need_single_list:
                continue
            dv = _admin_dept.get(_adn, {})
            if not dv or dv.get("total", 0) == 0:
                continue
            _admin_dept_rows.append(_admin_row(_adn, "总", dv["total"], dv["done"], dv["grades"], indent=1))
            if (dv.get("sales_total") or 0) > 0 and (dv.get("non_sales_total") or 0) > 0:
                _admin_dept_rows.append(_admin_row(_adn, "销售", dv["sales_total"], dv["sales_done"], dv.get("sales_grades", dv["grades"]), indent=2))
                _admin_dept_rows.append(_admin_row(_adn, "非销售", dv["non_sales_total"], dv["non_sales_done"], dv.get("non_sales_grades", dv["grades"]), indent=2))
    # 4) 纯高管（不负责部门、分管为空）放最后
    _shown_execs = set()
    for row in _admin_dept_rows:
        dept = str(row.get("部门", ""))
        if "高管（" in dept and "）" in dept:
            start = dept.index("高管（") + 3
            end = dept.index("）", start)
            _shown_execs.add(dept[start:end].strip())
    for _exec_name in sorted(_admin_exec_by_name.keys()):
        if _exec_name in _shown_execs:
            continue
        dv = _admin_exec_by_name[_exec_name]
        if dv["total"] > 0:
            _admin_dept_rows.append(_admin_row(f"高管（{_exec_name}）", "总", dv["total"], dv["done"], dv["grades"], indent=0))
            if (dv.get("sales_total") or 0) > 0 and (dv.get("non_sales_total") or 0) > 0:
                _admin_dept_rows.append(_admin_row(f"高管（{_exec_name}）", "销售", dv["sales_total"], dv["sales_done"], dv.get("sales_grades", dv["grades"]), indent=1))
                _admin_dept_rows.append(_admin_row(f"高管（{_exec_name}）", "非销售", dv["non_sales_total"], dv["non_sales_done"], dv.get("non_sales_grades", dv["grades"]), indent=1))
    _admin_dept_df = pd.DataFrame(_admin_dept_rows)
    if not _admin_dept_df.empty:
        def _admin_dept_style(row):
            scope, dept = row.get("口径", ""), str(row.get("部门", ""))
            if "高管（" in dept:
                if scope == "总":
                    return ["font-weight: 700; font-size: 14px; background-color: rgba(255,193,7,0.12); color: #FFC107;"] * len(row)
                if scope in ("销售", "非销售"):
                    return ["font-size: 12px; background-color: rgba(255,193,7,0.06);"] * len(row)
            if scope == "总":
                return ["font-weight: 700; font-size: 14px; background-color: rgba(255,255,255,0.06);"] * len(row)
            if scope == "销售":
                return ["font-size: 12px; color: #81C784;"] * len(row)
            if scope == "非销售":
                return ["font-size: 12px; color: #64B5F6;"] * len(row)
            return [""] * len(row)
        _admin_dept_df = _admin_dept_df.style.set_properties(**{"text-align": "center"}).apply(_admin_dept_style, axis=1)
    st.dataframe(_admin_dept_df, use_container_width=True, hide_index=True)
    st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 提示：各部门含一级部门负责人，高管单列；如果一级部门负责人和高管为同一人，则一级部门中不含，高管单列。</div>", unsafe_allow_html=True)

    # 管理员数据下载：可选择列导出
    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title'>📥 数据下载</div>", unsafe_allow_html=True)
    _flat_rows = []
    for rec in report_records:
        f = rec.get("fields", {})
        row = {}
        for k, v in f.items():
            row[k] = _extract_text(v, "")
        _flat_rows.append(row)
    _download_df = pd.DataFrame(_flat_rows)
    if not _download_df.empty:
        _all_cols = list(_download_df.columns)
        _sel_cols = st.multiselect("选择导出列", options=_all_cols, default=_all_cols, key="admin_download_col_sel")
        if not _sel_cols:
            _sel_cols = _all_cols
        _export_df = _download_df[_sel_cols]

        _csv_bytes = _export_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8")
        _ts = datetime.now().strftime("%Y%m%d_%H%M")
        _xl_bytes = b""
        _has_excel = False
        try:
            import openpyxl
            _xl_buffer = io.BytesIO()
            _export_df.to_excel(_xl_buffer, index=False, engine="openpyxl")
            _xl_bytes = _xl_buffer.getvalue()
            _has_excel = True
        except ImportError:
            pass
        _d1, _d2 = st.columns(2)
        with _d1:
            st.download_button("📥 下载 CSV", data=_csv_bytes, file_name=f"绩效数据_{current_cycle}_{_ts}.csv", mime="text/csv", key="admin_download_csv")
        with _d2:
            if _has_excel:
                st.download_button("📥 下载 Excel", data=_xl_bytes, file_name=f"绩效数据_{current_cycle}_{_ts}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="admin_download_excel")
            else:
                st.caption("💡 需安装 openpyxl 以启用 Excel 导出: pip install openpyxl")
    else:
        st.caption("暂无数据可下载")

def _check_admin_perm(record, user_name):
    """
    检查后台权限：返回 ("admin", None) | ("hrbp_lead", scope_depts) | ("hrbp", scope_depts) | (None, None)
    - 后台角色=系统管理员 → admin
    - 后台角色=HRBP Lead → hrbp_lead，scope=自己 HRBP Lead 列部门 + 下属 HRBP 负责部门
    - 后台角色=HRBP → hrbp，scope=姓名出现在 HRBP 列的记录的部门
    """
    if not record or not isinstance(record, dict):
        return None, None
    fields = record.get("fields", {})
    back_role = _extract_text(fields.get("后台角色"), "").strip()
    if back_role == "系统管理员":
        return "admin", None
    if back_role not in ("HRBP", "HRBP Lead"):
        return None, None
    perm = "hrbp_lead" if back_role == "HRBP Lead" else "hrbp"
    if back_role == "HRBP Lead":
        scope_depts = _compute_hrbp_lead_scope_with_subordinates(user_name)
    else:
        all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
        scope_depts = set()
        for rec in all_records:
            rf = rec.get("fields", {})
            col_val = _extract_text(rf.get("HRBP"), "").strip()
            if user_name and user_name in col_val:
                d1 = _extract_text(rf.get("一级部门"), "").strip()
                if d1 and d1 not in ("", "未获取", "-"):
                    scope_depts.add(d1)
        scope_depts = list(scope_depts) if scope_depts else None
    return perm, scope_depts if scope_depts else None

def _compute_hrbp_lead_scope_with_subordinates(user_name):
    """
    HRBP Lead 负责范围 = 自己姓名出现在 HRBP Lead 列的所有部门 + 下属 HRBP 姓名出现在 HRBP 列的所有部门。
    下属定义：同一条记录中 HRBP Lead 列包含当前用户、HRBP 列有某人，则该某人为下属。
    """
    if not user_name:
        return []
    all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
    scope_depts = set()
    subordinate_names = set()
    for rec in all_records:
        rf = rec.get("fields", {})
        lead_val = _extract_text(rf.get("HRBP Lead"), "").strip()
        if user_name not in lead_val:
            continue
        d1 = _extract_text(rf.get("一级部门"), "").strip()
        if d1 and d1 not in ("", "未获取", "-"):
            scope_depts.add(d1)
        hrbp_val = _extract_text(rf.get("HRBP"), "").strip()
        if hrbp_val:
            for h in hrbp_val.replace("，", ",").split(","):
                h = h.strip()
                if h:
                    subordinate_names.add(h)
    for rec in all_records:
        rf = rec.get("fields", {})
        hrbp_val = _extract_text(rf.get("HRBP"), "").strip()
        if not hrbp_val:
            continue
        for h in hrbp_val.replace("，", ",").split(","):
            h = h.strip()
            if h and h in subordinate_names:
                d1 = _extract_text(rf.get("一级部门"), "").strip()
                if d1 and d1 not in ("", "未获取", "-"):
                    scope_depts.add(d1)
                break
    return list(scope_depts)


def _compute_hrbp_scope_from_name(user_name, role=None):
    """根据姓名计算负责部门。role=hrbp_lead 时包含下属 HRBP 的范围，role=hrbp 时仅查 HRBP 列，否则查两列并集"""
    if not user_name:
        return []
    if role == "hrbp_lead":
        return _compute_hrbp_lead_scope_with_subordinates(user_name)
    all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
    scope_depts = set()
    cols = ["HRBP"] if role == "hrbp" else ["HRBP", "HRBP Lead"]
    for rec in all_records:
        rf = rec.get("fields", {})
        for col_name in cols:
            col_val = _extract_text(rf.get(col_name), "").strip()
            if user_name in col_val:
                d1 = _extract_text(rf.get("一级部门"), "").strip()
                if d1 and d1 not in ("", "未获取", "-"):
                    scope_depts.add(d1)
                break
    return list(scope_depts)

def _compute_hrbp_subordinates_by_dept(user_name):
    """HRBP Lead 负责部门下对应的 HRBP 人员：部门 -> [HRBP姓名列表]"""
    if not user_name:
        return {}
    all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
    dept_to_hrbp = {}
    for rec in all_records:
        rf = rec.get("fields", {})
        lead_val = _extract_text(rf.get("HRBP Lead"), "").strip()
        if user_name not in lead_val:
            continue
        d1 = _extract_text(rf.get("一级部门"), "").strip()
        if not d1 or d1 in ("", "未获取", "-"):
            continue
        hrbp_val = _extract_text(rf.get("HRBP"), "").strip()
        if hrbp_val:
            dept_to_hrbp.setdefault(d1, set()).add(hrbp_val)
    return {k: sorted(list(v)) for k, v in dept_to_hrbp.items()}

def _render_hrbp_dashboard():
    """HRBP / HRBP Lead 专属页面：负责部门绩效概览（仅展示其负责的一级部门）"""
    user_name = st.session_state.user_info.get("name", "HRBP")
    admin_role = st.session_state.get("admin_role", "hrbp")
    is_hrbp_lead = (admin_role == "hrbp_lead")
    scope_depts = st.session_state.get("admin_scope")
    if not scope_depts:
        scope_depts = _compute_hrbp_scope_from_name(user_name, role=admin_role)
    all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
    if not all_records:
        st.info("💡 提示：暂无可用于报表展示的数据。")
        st.sidebar.markdown(f"### 👋 欢迎 {user_name}！")
        st.sidebar.markdown("**HRBP Lead**" if is_hrbp_lead else "**HRBP**")
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        if st.sidebar.button("🚪 退出登录", use_container_width=True):
            st.session_state.clear()
            st.rerun()
        return

    def pick_cycle(ff):
        for k in ["绩效考核周期", "考核周期", "本次绩效考核周期", "本次考核周期"]:
            v = _extract_text(ff.get(k), "").strip()
            if v:
                return _normalize_cycle_display(v) or v
        return "2026年上半年"

    _default_cycle = pick_cycle(all_records[0].get("fields", {}))
    current_cycle = _read_admin_cycle_override() or _default_cycle
    report_records = []
    for rec in all_records:
        rf = rec.get("fields", {})
        emp_name = _extract_text(rf.get("姓名"), "未知").strip()
        if emp_name == user_name:
            continue
        cyc = pick_cycle(rf)
        if not _cycles_match(cyc, current_cycle):
            continue
        dept_l1 = _clean_dept_name(rf.get("一级部门")) or "未分配部门"
        if scope_depts and dept_l1 not in scope_depts:
            continue
        report_records.append(rec)

    # 分管高管、一级部门筛选（与管理员一致，选项限定在负责范围内）
    _hrbp_vp_names = set()
    for rec in report_records:
        vp_str = _extract_text(rec.get("fields", {}).get("分管高管") or rec.get("fields", {}).get("高管"), "").strip()
        for v in vp_str.replace("，", ",").split(","):
            if v.strip():
                _hrbp_vp_names.add(v.strip())
    _hrbp_vp_opts = ["全部"] + sorted(_hrbp_vp_names)
    _hrbp_dept_names = sorted({(_clean_dept_name(r.get("fields", {}).get("一级部门")) or "未分配部门") for r in report_records})
    _hrbp_dept_opts = ["全部部门"] + _hrbp_dept_names
    if "hrbp_vp_filter" not in st.session_state or st.session_state.hrbp_vp_filter not in _hrbp_vp_opts:
        st.session_state.hrbp_vp_filter = "全部"
    if "hrbp_dept_filter" not in st.session_state or st.session_state.hrbp_dept_filter not in _hrbp_dept_opts:
        st.session_state.hrbp_dept_filter = "全部部门"
    _sel_vp = st.session_state.get("hrbp_vp_filter", "全部")
    _sel_dept = st.session_state.get("hrbp_dept_filter", "全部部门")
    # 绩效概览 KPI：与管理员一致，不受分管高管/一级部门/销售非销售筛选影响
    report_records_for_hrbp_kpi = list(report_records)
    if _sel_vp and _sel_vp != "全部":
        report_records = [
            r for r in report_records
            if _sel_vp in _extract_text(r.get("fields", {}).get("分管高管") or r.get("fields", {}).get("高管"), "").strip()
            and (_extract_text(r.get("fields", {}).get("姓名"), "").strip() != _sel_vp or _sel_vp == user_name)
        ]
    # 负责范围总体配额：仅随分管高管变，不随一级部门变
    report_records_for_scope_quota = list(report_records)
    if _sel_dept and _sel_dept != "全部部门":
        report_records = [r for r in report_records if (_clean_dept_name(r.get("fields", {}).get("一级部门")) or "未分配部门") == _sel_dept]

    # HRBP/HRBP Lead：高管仍显示，但绩效数据（等级、完成情况等）用「-」表示，统计时排除高管
    _hrbp_excl_exec = [r for r in report_records if not _is_executive(r)]  # 当前筛选下的非高管
    _hrbp_excl_exec_for_kpi = [r for r in report_records_for_hrbp_kpi if not _is_executive(r)]  # KPI 用，不受筛选影响

    total_cnt = len(report_records)
    if total_cnt == 0:
        scope_hint = f"负责部门：{', '.join(scope_depts) if scope_depts else '未配置'}"
        st.info(f"💡 当前考核周期在您负责范围内暂无数据。{scope_hint}")
        st.sidebar.markdown(f"### 👋 欢迎 {user_name}！")
        st.sidebar.markdown("**HRBP Lead**" if is_hrbp_lead else "**HRBP**")
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        if is_hrbp_lead:
            dept_to_hrbp = _compute_hrbp_subordinates_by_dept(user_name)
            if dept_to_hrbp:
                _sub_bp_names = sorted({n for names in dept_to_hrbp.values() for n in names if n != user_name})
                st.sidebar.markdown("### 👥 下属 HRBP")
                st.sidebar.caption(", ".join(_sub_bp_names))
        if scope_depts:
            st.sidebar.markdown("### 📋 负责部门")
            st.sidebar.markdown("\n".join(f"- {d}" for d in sorted(scope_depts)))
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        if st.sidebar.button("🚪 退出登录", use_container_width=True):
            st.session_state.clear()
            st.rerun()
        return

    target_set_cnt = self_done_cnt = mgr_done_cnt = dept_done_cnt = vp_done_cnt = 0
    grade_counts = Counter()
    dept_stats = {}
    # 各步骤人员列表（用于点击查看）；高管不计入绩效统计，不进入人员列表
    step_people = {"target_set": [], "self_done": [], "mgr_done": [], "dept_done": [], "vp_done": []}
    grade_people = {g: [] for g in GRADE_OPTIONS}
    for rec in report_records:
        f = rec.get("fields", {})
        _skip_perf = _is_executive(rec)  # HRBP 高管不参与绩效统计
        name = _extract_text(f.get("姓名"), "未知").strip()
        emp_id = _extract_text(f.get("工号") or f.get("员工工号"), "").strip()
        dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
        person = {"name": name, "emp_id": emp_id, "dept": dept_l1}
        has_target = any(_extract_text(f.get(f"工作目标{i}及总结"), "").strip() for i in range(1, 6))
        if not _skip_perf:
            if has_target:
                target_set_cnt += 1
                step_people["target_set"].append(person)
        self_done = _extract_text(f.get("自评是否提交"), "").strip() == "是"
        mgr_done = _extract_text(f.get("上级评价是否完成"), "").strip() == "是"
        dept_done = _extract_text(f.get("一级部门调整完毕"), "").strip() == "是"
        vp_done = _extract_text(f.get("分管高管调整完毕"), "").strip() == "是"
        if not _skip_perf:
            if self_done:
                self_done_cnt += 1
                step_people["self_done"].append(person)
            if mgr_done:
                mgr_done_cnt += 1
                step_people["mgr_done"].append(person)
            if dept_done:
                dept_done_cnt += 1
                step_people["dept_done"].append(person)
            if vp_done:
                vp_done_cnt += 1
                step_people["vp_done"].append(person)
        vp_adj = _extract_text(f.get("分管高管调整考核结果"), "").strip()
        dept_adj = _extract_text(f.get("一级部门调整考核结果"), "").strip()
        mgr_grade = _extract_text(f.get("考核结果"), "").strip()
        final_from_field = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
        final_grade = "-"
        for cand in [vp_adj, dept_adj, mgr_grade, final_from_field]:
            if cand in GRADE_OPTIONS:
                final_grade = cand
                break
        if not _skip_perf and final_grade in GRADE_OPTIONS:
            grade_counts[final_grade] += 1
            grade_people[final_grade].append(person)
        if not _skip_perf:
            _base = {"total": 0, "done": 0, "target_set": 0, "self_done": 0, "dept_done": 0, "vp_done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}}
            dept_info = dept_stats.setdefault(dept_l1, {
                **_base,
                "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS},
                "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS},
            })
            dept_info["total"] += 1
            if has_target:
                dept_info["target_set"] += 1
            if self_done:
                dept_info["self_done"] += 1
            if mgr_done:
                dept_info["done"] += 1
            if dept_done:
                dept_info["dept_done"] += 1
            if vp_done:
                dept_info["vp_done"] += 1
            if final_grade in GRADE_OPTIONS:
                dept_info["grades"][final_grade] += 1
            is_sales = _extract_text(f.get("是否绩效关联奖金"), "").strip() == "否"
            if is_sales:
                dept_info["sales_total"] += 1
                if mgr_done:
                    dept_info["sales_done"] += 1
                if final_grade in GRADE_OPTIONS:
                    dept_info["sales_grades"][final_grade] += 1
            else:
                dept_info["non_sales_total"] += 1
                if mgr_done:
                    dept_info["non_sales_done"] += 1
                if final_grade in GRADE_OPTIONS:
                    dept_info["non_sales_grades"][final_grade] += 1

    report_sales = [r for r in report_records if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "否"]
    report_non_sales = [r for r in report_records if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"]
    report_sales_for_scope_quota = [r for r in report_records_for_scope_quota if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "否"]
    report_non_sales_for_scope_quota = [r for r in report_records_for_scope_quota if _extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"]
    has_bonus_no = len(report_sales) > 0 and len(report_non_sales) > 0
    # 部门绩效详情：不受筛选影响，始终展示负责范围全量；高管单列（绩效用「-」），各部门不含高管
    exec_stats_for_detail, dept_stats_for_detail = _build_detail_stats(report_records_for_hrbp_kpi, exclude_exec_from_dept=True)
    # HRBP：按分管高管分组部门；高管按本人姓名统计（仅 特殊判断=高管）
    vp_to_depts = {}
    exec_by_name = {}  # 按高管本人姓名
    exec_to_vps = {}   # exec_name -> set(vps) 用于按 VP 分组展示

    def _fresh_hrbp_detail():
        return {"total": 0, "done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}, "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS}, "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS}, "mask_perf": False}
    for rec in report_records_for_hrbp_kpi:
        f = rec.get("fields", {})
        vp_str = _extract_text(f.get("分管高管") or f.get("高管"), "").strip()
        vps = [vp.strip() for vp in vp_str.replace("，", ",").split(",") if vp.strip()]
        mgr_done = _extract_text(f.get("上级评价是否完成"), "").strip() == "是"
        final_grade = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
        if final_grade not in GRADE_OPTIONS:
            final_grade = "-"
        is_sales = _extract_text(f.get("是否绩效关联奖金"), "").strip() == "否"
        if _is_executive(rec):
            exec_name = _extract_text(f.get("姓名"), "").strip() or "未知"
            t = exec_by_name.setdefault(exec_name, _fresh_hrbp_detail())
            t["mask_perf"] = True  # HRBP 高管绩效用「-」显示
            exec_to_vps.setdefault(exec_name, set()).update(vps)
            t["total"] += 1
            # 不累计 done/grades，展示时用「-」
        else:
            for vp in vps:
                dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
                vp_to_depts.setdefault(vp, set()).add(dept_l1)
    report_scope = report_sales if report_sales else report_records
    if has_bonus_no:
        if "hrbp_report_bonus_scope_filter" not in st.session_state:
            st.session_state.hrbp_report_bonus_scope_filter = "全部"
        _sk = st.session_state.hrbp_report_bonus_scope_filter
        report_scope = report_records if _sk == "全部" else (report_sales if _sk == "销售" else report_non_sales)
    _scope_excl_exec = [r for r in (report_scope or report_records) if not _is_executive(r)]  # 高管不参与绩效统计
    base_cnt = len(_scope_excl_exec) if _scope_excl_exec else total_cnt
    scope_done = sum(1 for r in _scope_excl_exec if _extract_text(r.get("fields", {}).get("上级评价是否完成"), "").strip() == "是")
    completion_rate = 0 if base_cnt == 0 else round(scope_done / base_cnt * 100, 1)
    grade_counts_bonus = Counter()
    for rec in _scope_excl_exec:
        f = rec.get("fields", {})
        vp_adj = _extract_text(f.get("分管高管调整考核结果"), "").strip()
        dept_adj = _extract_text(f.get("一级部门调整考核结果"), "").strip()
        mgr_grade = _extract_text(f.get("考核结果"), "").strip()
        final_grade = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
        fg = "-"
        for cand in [vp_adj, dept_adj, mgr_grade, final_grade]:
            if cand in GRADE_OPTIONS:
                fg = cand
                break
        if fg in GRADE_OPTIONS:
            grade_counts_bonus[fg] += 1

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
    _neutral = "#b7bdc8"
    _cell_style = "font-size:14px;font-weight:700;white-space:nowrap;"
    def _th(txt):
        return f"<th style='text-align:center;{_cell_style}'>{txt}</th>"
    def _td(val, color):
        return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}</td>"
    def _td_label(txt):
        return f"<td style='text-align:center;color:#b7bdc8;{_cell_style}'>{txt}</td>"
    def _td_over(val, color, is_over):
        if is_over:
            return f"<td style='text-align:center;color:#F44336;font-weight:800;border:1px solid #F44336;border-radius:4px;{_cell_style}'>{val}</td>"
        return _td(val, color)
    _over_sa = actual_sa > sa_theory
    _over_bp = actual_bp > bp_theory
    _over_sapb = actual_sapb > sapb_theory

    # 侧边栏
    st.sidebar.markdown(f"### 👋 欢迎 {user_name}！")
    role_label = "**HRBP Lead**" if is_hrbp_lead else "**HRBP**"
    st.sidebar.markdown(role_label)
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    if is_hrbp_lead:
        dept_to_hrbp = _compute_hrbp_subordinates_by_dept(user_name)
        if dept_to_hrbp:
            _sub_bp_names = sorted({n for names in dept_to_hrbp.values() for n in names if n != user_name})
            st.sidebar.markdown("### 👥 下属 HRBP")
            st.sidebar.caption(", ".join(_sub_bp_names))
            st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    if scope_depts:
        st.sidebar.markdown("### 📋 负责部门")
        st.sidebar.markdown("\n".join(f"- {d}" for d in sorted(scope_depts)))
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    st.sidebar.markdown("### 📚 制度学习")
    _doc_items = []
    for _dn in DOC_LINK_NAMES:
        _url = _get_doc_link(_dn)
        if _url:
            _doc_items.append(f'<div style="margin-bottom: 8px; padding: 6px 8px; border-radius: 4px; background: rgba(255,255,255,0.03);"><a href="{_url}" target="_blank" style="color: #b7bdc8;">{_dn}</a></div>')
        else:
            _doc_items.append(f'<div style="margin-bottom: 8px; padding: 6px 8px; color: #888;">{_dn}</div>')
    st.sidebar.markdown(f"<div style='font-size: 11px;'>{''.join(_doc_items)}</div>", unsafe_allow_html=True)
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    if st.sidebar.button("🚪 退出登录", use_container_width=True):
        st.session_state.clear()
        st.rerun()

    # 主体内容（居中对齐）
    st.markdown("<div class='hrbp-view-container'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title'>📊 HRBP 绩效概览</div>", unsafe_allow_html=True)
    # KPI：考核总人数含高管；已完成评价/总体完成率/剩余未评排除高管（高管绩效用「-」显示）
    hrbp_kpi_total = len(report_records_for_hrbp_kpi)
    hrbp_kpi_done = 0
    for r in _hrbp_excl_exec_for_kpi:
        f = r.get("fields", {})
        final = _extract_text(f.get("最终考核结果"), "").strip()
        if final in GRADE_OPTIONS:
            hrbp_kpi_done += 1
        else:
            for cand in [_extract_text(f.get("分管高管调整考核结果"), "").strip(), _extract_text(f.get("一级部门调整考核结果"), "").strip(), _extract_text(f.get("考核结果"), "").strip()]:
                if cand in GRADE_OPTIONS:
                    hrbp_kpi_done += 1
                    break
    _hrbp_kpi_denom = len(_hrbp_excl_exec_for_kpi)  # 完成率分母排除高管
    hrbp_kpi_rate = round(hrbp_kpi_done / _hrbp_kpi_denom * 100, 1) if _hrbp_kpi_denom else 0
    hrbp_kpi_remaining = _hrbp_kpi_denom - hrbp_kpi_done
    _hrbp_kpi_html = f"""
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;">
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);" title="此为绩效考核总数，包含分管高管、一级部门负责人">
            <div class="report-kpi-label" title="此为绩效考核总数，包含分管高管、一级部门负责人" style="cursor:help;white-space:nowrap;">考核总人数 ⓘ</div>
            <div style="font-size:32px;font-weight:700;color:#42A5F5;margin-top:8px;">{hrbp_kpi_total}</div>
        </div>
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);" title="最终考核结果不为空">
            <div class="report-kpi-label" style="cursor:help;white-space:nowrap;">已完成评价 ⓘ</div>
            <div style="font-size:32px;font-weight:700;color:#26A69A;margin-top:8px;">{hrbp_kpi_done}</div>
        </div>
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);">
            <div class="report-kpi-label" style="white-space:nowrap;">总体完成率</div>
            <div style="font-size:32px;font-weight:700;color:#FFA726;margin-top:8px;">{hrbp_kpi_rate}%</div>
        </div>
        <div style="flex:1;min-width:120px;text-align:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);">
            <div class="report-kpi-label" style="white-space:nowrap;">剩余未评</div>
            <div style="font-size:32px;font-weight:700;color:#EF5350;margin-top:8px;">{hrbp_kpi_remaining}</div>
        </div>
    </div>
    """
    st.markdown(_hrbp_kpi_html, unsafe_allow_html=True)
    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    # 筛选框：与管理员一致（分管高管、一级部门、销售/非销售）
    st.markdown("<div class='module-title'>📋 各部门考核环节进度</div>", unsafe_allow_html=True)
    _hf1, _hf2, _hf3 = st.columns(3)
    with _hf1:
        st.selectbox("分管高管", options=_hrbp_vp_opts, key="hrbp_vp_filter", format_func=lambda x: "全部" if x == "全部" else x)
    with _hf2:
        st.selectbox("一级部门", options=_hrbp_dept_opts, key="hrbp_dept_filter", format_func=lambda x: "全部部门" if x == "全部部门" else x)
    with _hf3:
        if has_bonus_no:
            st.markdown(
                '<div style="border-left: 4px solid #26A69A; padding: 8px 0 8px 12px; border-radius: 0 6px 6px 0; background: rgba(38, 166, 154, 0.06);"><span style="color: #26A69A; font-weight: 600; font-size: 14px;">销售/非销售</span></div>',
                unsafe_allow_html=True,
            )
            st.selectbox("销售/非销售", options=["全部", "销售", "非销售"], key="hrbp_report_bonus_scope_filter", label_visibility="collapsed")
        else:
            st.caption("")
    _hrbp_filter_active = []
    if _sel_vp and _sel_vp != "全部":
        _hrbp_filter_active.append(f"分管高管：{_sel_vp}")
    if _sel_dept and _sel_dept != "全部部门":
        _hrbp_filter_active.append(f"一级部门：{_sel_dept}")
    if has_bonus_no and st.session_state.get("hrbp_report_bonus_scope_filter", "全部") != "全部":
        _hrbp_filter_active.append(f"销售/非销售：{st.session_state.hrbp_report_bonus_scope_filter}")
    if _hrbp_filter_active:
        _hrbp_hint = f"💡 当前筛选：{' | '.join(_hrbp_filter_active)}"
        _hrbp_stat_parts = []
        if _sel_vp and _sel_vp != "全部":
            _hrbp_stat_parts.append("统计均不包含分管高管本人")
        if _sel_dept and _sel_dept != "全部部门":
            _hrbp_stat_parts.append("一级部门不含一级部门负责人本人")
        if _hrbp_stat_parts:
            _hrbp_hint += "。" + "，".join(_hrbp_stat_parts) + "。"
        st.markdown(
            f"<div style='background:rgba(2,119,189,0.15);border:1px solid rgba(2,119,189,0.4);border-radius:6px;padding:8px 12px;font-size:13px;color:#66b2ff;margin-top:8px;margin-bottom:24px;'>{_hrbp_hint}</div>",
            unsafe_allow_html=True,
        )
    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
    # 各步骤完成情况（可点击查看具体人员，只读）
    st.markdown("<p class='hrbp-left-label'><strong>各步骤完成情况</strong>（点击数字查看具体人员）</p>", unsafe_allow_html=True)
    k1, k2, k3, k4, k5 = st.columns(5)
    with k1:
        st.caption("已设定目标")
        if st.button(f"**{target_set_cnt}**", key="hrbp_btn_target", use_container_width=True):
            st.session_state["hrbp_clicked"] = ("target_set", "已设定目标")
    with k2:
        st.caption("已提交自评")
        if st.button(f"**{self_done_cnt}**", key="hrbp_btn_self", use_container_width=True):
            st.session_state["hrbp_clicked"] = ("self_done", "已提交自评")
    with k3:
        st.caption("上级已评价")
        if st.button(f"**{mgr_done_cnt}**", key="hrbp_btn_mgr", use_container_width=True):
            st.session_state["hrbp_clicked"] = ("mgr_done", "上级已评价")
    with k4:
        st.caption("一级部门已调整")
        if st.button(f"**{dept_done_cnt}**", key="hrbp_btn_dept", use_container_width=True):
            st.session_state["hrbp_clicked"] = ("dept_done", "一级部门已调整")
    with k5:
        st.caption("分管高管已调整")
        if st.button(f"**{vp_done_cnt}**", key="hrbp_btn_vp", use_container_width=True):
            st.session_state["hrbp_clicked"] = ("vp_done", "分管高管已调整")

    clicked = st.session_state.get("hrbp_clicked")
    if clicked and clicked[0] != "grade":
        step_key, step_label = clicked
        people_list = step_people.get(step_key, [])
        with st.expander(f"📋 {step_label} 人员列表（共 {len(people_list)} 人）", expanded=True):
            if people_list:
                for p in people_list:
                    st.text(f"{p['name']}（{p['emp_id']}）— {p['dept']}")
            else:
                st.caption("暂无")
        if st.button("关闭列表", key="hrbp_close_list"):
            st.session_state.pop("hrbp_clicked", None)
            st.rerun()

    # 各部门考核环节进度表（筛选框已在上方）
    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
    _hrbp_dept_stats_for_table = dept_stats
    if has_bonus_no and st.session_state.get("hrbp_report_bonus_scope_filter", "全部") != "全部":
        _hrbp_dept_stats_for_table = {}
        _sk = st.session_state.get("hrbp_report_bonus_scope_filter", "全部")
        _recs_hrbp = report_sales if _sk == "销售" else report_non_sales
        for rec in _recs_hrbp:
            f = rec.get("fields", {})
            dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
            has_target = any(_extract_text(f.get(f"工作目标{i}及总结"), "").strip() for i in range(1, 6))
            self_done = _extract_text(f.get("自评是否提交"), "").strip() == "是"
            mgr_done = _extract_text(f.get("上级评价是否完成"), "").strip() == "是"
            dept_done = _extract_text(f.get("一级部门调整完毕"), "").strip() == "是"
            vp_done = _extract_text(f.get("分管高管调整完毕"), "").strip() == "是"
            vp_adj = _extract_text(f.get("分管高管调整考核结果"), "").strip()
            dept_adj = _extract_text(f.get("一级部门调整考核结果"), "").strip()
            mgr_grade = _extract_text(f.get("考核结果"), "").strip()
            final_from_field = _extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
            final_grade = "-"
            for cand in [vp_adj, dept_adj, mgr_grade, final_from_field]:
                if cand in GRADE_OPTIONS:
                    final_grade = cand
                    break
            _base = {"total": 0, "done": 0, "target_set": 0, "self_done": 0, "dept_done": 0, "vp_done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}}
            di = _hrbp_dept_stats_for_table.setdefault(dept_l1, {**_base})
            di["total"] += 1
            if has_target:
                di["target_set"] += 1
            if self_done:
                di["self_done"] += 1
            if mgr_done:
                di["done"] += 1
            if dept_done:
                di["dept_done"] += 1
            if vp_done:
                di["vp_done"] += 1
            if final_grade in GRADE_OPTIONS:
                di["grades"][final_grade] += 1
    _hrbp_totals_scope = {"target_set": 0, "self_done": 0, "done": 0, "dept_done": 0, "vp_done": 0, "total": 0}
    for dval in _hrbp_dept_stats_for_table.values():
        for k in _hrbp_totals_scope:
            _hrbp_totals_scope[k] += dval.get(k, 0)
    def _hrbp_pct(t, n):
        return f"{round(n / t * 100, 1)}%" if t and t > 0 else "0%"
    _hrbp_step_cols = [
        ("目标设定", "target_set"),
        ("自评", "self_done"),
        ("上级评价", "done"),
        ("部门调整", "dept_done"),
        ("高管调整", "vp_done"),
    ]
    _hrbp_table_rows = []
    for dept_name, dval in sorted(_hrbp_dept_stats_for_table.items(), key=lambda x: x[0]):
        t = dval.get("total", 0) or 1
        row = {"部门": dept_name}
        for label, key in _hrbp_step_cols:
            n = dval.get(key, 0)
            row[f"{label}"] = n
            row[f"{label}%"] = _hrbp_pct(t, n)
        row["总数"] = dval.get("total", 0)
        _hrbp_table_rows.append(row)
    t_hrbp_all = int(_hrbp_totals_scope.get("total", 0) or 1)
    _hrbp_summary_row = {"部门": "负责范围合计"}
    for label, key in _hrbp_step_cols:
        n = int(_hrbp_totals_scope.get(key, 0) or 0)
        _hrbp_summary_row[f"{label}"] = n
        _hrbp_summary_row[f"{label}%"] = _hrbp_pct(t_hrbp_all, n)
    _hrbp_summary_row["总数"] = int(_hrbp_totals_scope.get("total", 0) or 0)
    _hrbp_table_rows.insert(0, _hrbp_summary_row)
    if _hrbp_table_rows:
        _hrbp_step_headers = []
        for label, _ in _hrbp_step_cols:
            _hrbp_step_headers.extend([label, f"{label}%"])
        _hrbp_all_cols = ["部门"] + _hrbp_step_headers + ["总数"]
        _hrbp_step_df = pd.DataFrame(_hrbp_table_rows, columns=_hrbp_all_cols)
        def _hrbp_step_style(row):
            is_total = str(row.get("部门", "")) == "负责范围合计"
            base = "font-weight: 700; background-color: rgba(33,150,243,0.18); color: #42A5F5; border-top: 2px solid rgba(33,150,243,0.5); border-bottom: 2px solid rgba(33,150,243,0.5);" if is_total else ""
            return [base or "text-align: center;"] * len(row)
        _hrbp_step_df = _hrbp_step_df.style.set_properties(**{"text-align": "center"}).apply(_hrbp_step_style, axis=1)
        st.dataframe(_hrbp_step_df, use_container_width=True, hide_index=True)
    else:
        st.caption("暂无数据")

    st.markdown("<p class='hrbp-left-label'><strong>考核等级分布</strong>（点击数字查看具体人员）</p>", unsafe_allow_html=True)
    gc1, gc2, gc3, gc4, gc5, gc6, gc7 = st.columns(7)
    grade_btns = [
        ("S/A", actual_sa, "hrbp_grade_sa", "hrbp-grade-sa", "S", "A"),
        ("B+", actual_bp, "hrbp_grade_bp", "hrbp-grade-bp", "B+",),
        ("B+及以上", actual_sapb, "hrbp_grade_sapb", "hrbp-grade-sapb", "S", "A", "B+"),
        ("B", actual_b, "hrbp_grade_b", "hrbp-grade-b", "B",),
        ("B-", actual_bm, "hrbp_grade_bm", "hrbp-grade-bm", "B-",),
        ("C", actual_c, "hrbp_grade_c", "hrbp-grade-c", "C",),
        ("SUM", actual_sum, "hrbp_grade_sum", "hrbp-grade-sum", *GRADE_OPTIONS),
    ]
    for i, item in enumerate(grade_btns):
        label, cnt, key, css_class = item[0], item[1], item[2], item[3]
        grades = list(item[4:]) if len(item) > 4 else []
        with [gc1, gc2, gc3, gc4, gc5, gc6, gc7][i]:
            st.caption(label)
            if st.button(f"**{cnt}**", key=key, use_container_width=True):
                people = []
                for g in grades:
                    people.extend(grade_people.get(g, []))
                st.session_state["hrbp_clicked"] = ("grade", label, people)
    if st.session_state.get("hrbp_clicked") and st.session_state["hrbp_clicked"][0] == "grade":
        _, glabel, people_list = st.session_state["hrbp_clicked"]
        with st.expander(f"📋 {glabel} 人员列表（共 {len(people_list)} 人）", expanded=True):
            if people_list:
                for p in people_list:
                    st.text(f"{p['name']}（{p['emp_id']}）— {p['dept']}")
            else:
                st.caption("暂无")
        if st.button("关闭列表", key="hrbp_close_grade"):
            st.session_state.pop("hrbp_clicked", None)
            st.rerun()

    # 配额模块（与管理员页面一致：负责范围总体 + 各部门）
    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title'>📊 配额模块</div>", unsafe_allow_html=True)

    def _hrbp_build_dept_grade_stats(recs, use_total_as_base=False):
        dgs = {}
        for rec in (recs or []):
            rf = rec.get("fields", {})
            dept_l1 = _clean_dept_name(rf.get("一级部门")) or "未分配部门"
            vp_adj = _extract_text(rf.get("分管高管调整考核结果"), "").strip()
            dept_adj = _extract_text(rf.get("一级部门调整考核结果"), "").strip()
            mgr_grade = _extract_text(rf.get("考核结果"), "").strip()
            final_grade = _extract_text(rf.get("最终绩效结果") or rf.get("最终考核结果"), "").strip()
            fg = "-"
            for cand in [vp_adj, dept_adj, mgr_grade, final_grade]:
                if cand in GRADE_OPTIONS:
                    fg = cand
                    break
            dg = dgs.setdefault(dept_l1, {"grade_counts": Counter(), "bonus_cnt": 0, "total_cnt": 0})
            dg["total_cnt"] += 1
            if _extract_text(rf.get("是否绩效关联奖金"), "").strip() == "是":
                dg["bonus_cnt"] += 1
            if fg in GRADE_OPTIONS:
                dg["grade_counts"][fg] += 1
        for dept_name, dg in dgs.items():
            dg["base_cnt"] = dg["total_cnt"] if use_total_as_base else (dg["bonus_cnt"] if dg["bonus_cnt"] > 0 else dg["total_cnt"])
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
        return dgs

    _hrbp_recs_quota = report_records if not has_bonus_no else (
        report_records if st.session_state.get("hrbp_report_bonus_scope_filter", "全部") == "全部"
        else (report_sales if st.session_state.hrbp_report_bonus_scope_filter == "销售" else report_non_sales)
    )
    _hrbp_recs_actual = report_records if not has_bonus_no else (
        report_records if st.session_state.get("hrbp_report_bonus_scope_filter", "全部") == "全部"
        else (report_sales if st.session_state.hrbp_report_bonus_scope_filter == "销售" else report_non_sales)
    )
    # 负责范围总体：仅随分管高管变，不随一级部门变（用 scope 数据）；配额实际人数排除高管
    _hrbp_recs_quota_scope = report_records_for_scope_quota if not has_bonus_no else (
        report_records_for_scope_quota if st.session_state.get("hrbp_report_bonus_scope_filter", "全部") == "全部"
        else (report_sales_for_scope_quota if st.session_state.hrbp_report_bonus_scope_filter == "销售" else report_non_sales_for_scope_quota)
    )
    _hrbp_recs_actual_scope = report_records_for_scope_quota if not has_bonus_no else (
        report_records_for_scope_quota if st.session_state.get("hrbp_report_bonus_scope_filter", "全部") == "全部"
        else (report_sales_for_scope_quota if st.session_state.hrbp_report_bonus_scope_filter == "销售" else report_non_sales_for_scope_quota)
    )
    _hrbp_recs_quota_scope_excl = [r for r in _hrbp_recs_quota_scope if not _is_executive(r)]
    _hrbp_recs_actual_scope_excl = [r for r in _hrbp_recs_actual_scope if not _is_executive(r)]
    _hrbp_dept_quota_base_total = _hrbp_build_dept_grade_stats(_hrbp_recs_quota_scope_excl, use_total_as_base=True)
    _hrbp_dept_actual_total = _hrbp_build_dept_grade_stats(_hrbp_recs_actual_scope_excl, use_total_as_base=True)
    # 负责范围总体：S/A=20%、B+=15% 按总人数统一计算，不用各部门 floor 后求和（否则会偏小）
    _hrbp_total_base = sum(dg.get("base_cnt", 0) for dg in _hrbp_dept_quota_base_total.values())
    _hrbp_total_bmc = sum(dg.get("grade_counts", {}).get("B-", 0) + dg.get("grade_counts", {}).get("C", 0) for dg in _hrbp_dept_actual_total.values())
    _hrbp_sa_theory_total = math.floor(_hrbp_total_base * 0.20) if _hrbp_total_base else 0
    _hrbp_bp_base_total = math.floor(_hrbp_total_base * 0.15) if _hrbp_total_base else 0
    _hrbp_bp_cap_total = math.floor(_hrbp_total_base * 0.25) if _hrbp_total_base else 0
    _hrbp_bp_theory_total = min(_hrbp_bp_cap_total, _hrbp_bp_base_total + _hrbp_total_bmc)
    _hrbp_sapb_theory_total = _hrbp_sa_theory_total + _hrbp_bp_theory_total
    _hrbp_actual_sa_total = sum(ac.get("actual_sa", 0) for ac in _hrbp_dept_actual_total.values())
    _hrbp_actual_bp_total = sum(ac.get("actual_bp", 0) for ac in _hrbp_dept_actual_total.values())
    _hrbp_actual_sapb_total = _hrbp_actual_sa_total + _hrbp_actual_bp_total
    _hrbp_actual_b_total = sum(ac.get("actual_b", 0) for ac in _hrbp_dept_actual_total.values())
    _hrbp_actual_bm_total = sum(ac.get("actual_bm", 0) for ac in _hrbp_dept_actual_total.values())
    _hrbp_actual_c_total = sum(ac.get("actual_c", 0) for ac in _hrbp_dept_actual_total.values())
    _hrbp_actual_sum_total = sum(ac.get("actual_sum", 0) for ac in _hrbp_dept_actual_total.values())
    hrbp_dept_grade_stats_total = {}
    for dept_name in sorted(set(_hrbp_dept_quota_base_total.keys()) | set(_hrbp_dept_actual_total.keys())):
        qb = _hrbp_dept_quota_base_total.get(dept_name, {})
        ac = _hrbp_dept_actual_total.get(dept_name, {})
        hrbp_dept_grade_stats_total[dept_name] = {
            "base_cnt": qb.get("base_cnt", 1),
            "sa_theory": qb.get("sa_theory", 0),
            "bp_theory": qb.get("bp_theory", 0),
            "sapb_theory": qb.get("sapb_theory", 0),
            "actual_sa": ac.get("actual_sa", 0),
            "actual_bp": ac.get("actual_bp", 0),
            "actual_sapb": ac.get("actual_sapb", 0),
            "actual_b": ac.get("actual_b", 0),
            "actual_bm": ac.get("actual_bm", 0),
            "actual_c": ac.get("actual_c", 0),
            "actual_sum": ac.get("actual_sum", 0),
        }
    # 各部门配额：排除一级部门负责人（不能调整自己）、排除高管（绩效用「-」显示）
    _hrbp_recs_quota_excl = [r for r in _hrbp_recs_quota if not _is_dept_head(r) and not _is_executive(r)]
    _hrbp_recs_actual_excl = [r for r in _hrbp_recs_actual if not _is_dept_head(r) and not _is_executive(r)]
    _hrbp_dept_quota_base = _hrbp_build_dept_grade_stats(_hrbp_recs_quota_excl, use_total_as_base=True)
    _hrbp_dept_actual = _hrbp_build_dept_grade_stats(_hrbp_recs_actual_excl, use_total_as_base=True)
    hrbp_dept_grade_stats = {}
    for dept_name in sorted(set(_hrbp_dept_quota_base.keys()) | set(_hrbp_dept_actual.keys())):
        qb = _hrbp_dept_quota_base.get(dept_name, {})
        ac = _hrbp_dept_actual.get(dept_name, {})
        hrbp_dept_grade_stats[dept_name] = {
            "base_cnt": qb.get("base_cnt", 1),
            "sa_theory": qb.get("sa_theory", 0),
            "bp_theory": qb.get("bp_theory", 0),
            "sapb_theory": qb.get("sapb_theory", 0),
            "actual_sa": ac.get("actual_sa", 0),
            "actual_bp": ac.get("actual_bp", 0),
            "actual_sapb": ac.get("actual_sapb", 0),
            "actual_b": ac.get("actual_b", 0),
            "actual_bm": ac.get("actual_bm", 0),
            "actual_c": ac.get("actual_c", 0),
            "actual_sum": ac.get("actual_sum", 0),
        }
    _hrbp_dept_grade_filtered = {k: v for k, v in hrbp_dept_grade_stats.items() if k in _hrbp_dept_stats_for_table}
    if _hrbp_dept_grade_filtered:
        _q_cell = "font-size:14px;font-weight:700;white-space:nowrap;"
        def _q_th(t):
            return f"<th style='text-align:center;{_q_cell}'>{t}</th>"
        def _q_td(v, c):
            return f"<td style='text-align:center;color:{c};{_q_cell}'>{v}</td>"
        def _q_label(t):
            return f"<td style='text-align:center;color:#b7bdc8;{_q_cell}'>{t}</td>"
        def _q_hint(v, c, h):
            return f"<td style='text-align:center;color:{c};{_q_cell}'>{v}<span title='{h}' style='cursor:help;font-size:12px;margin-left:4px;color:#90A4AE;'>ⓘ</span></td>"
        def _q_over(v, c, over):
            if over:
                return f"<td style='text-align:center;color:#F44336;font-weight:800;border:1px solid #F44336;border-radius:4px;{_q_cell}'>{v}<span title='人数超过上限人数，请修改' style='cursor:help;font-size:12px;margin-left:4px;color:#F44336;'>ⓘ</span></td>"
            return f"<td style='text-align:center;color:{c};{_q_cell}'>{v}</td>"
        def _q_colspan(v, c, col=1):
            return f"<td style='text-align:center;color:{c};{_q_cell}' colspan='{col}'>{v}</td>"
        _q_header = _q_th("级别") + _q_th("S/A级别") + _q_th("B+级别") + _q_th("B+及以上级别") + _q_th("B级别") + _q_th("B-级别") + _q_th("C级别") + _q_th("SUM (人)")
        _q_bp_hint = "默认15%，根据实际的B-/C占比调整向上浮动"
        # 负责范围总体用按总人数计算的 20%/15%，不用各部门求和
        _all_hrbp_tot = {
            "sa_theory": _hrbp_sa_theory_total,
            "bp_theory": _hrbp_bp_theory_total,
            "sapb_theory": _hrbp_sapb_theory_total,
            "base_cnt": _hrbp_total_base,
            "actual_sa": _hrbp_actual_sa_total,
            "actual_bp": _hrbp_actual_bp_total,
            "actual_sapb": _hrbp_actual_sapb_total,
            "actual_b": _hrbp_actual_b_total,
            "actual_bm": _hrbp_actual_bm_total,
            "actual_c": _hrbp_actual_c_total,
            "actual_sum": _hrbp_actual_sum_total,
        }
        _all_hrbp_over_sa = _all_hrbp_tot["actual_sa"] > _all_hrbp_tot["sa_theory"]
        _all_hrbp_over_bp = _all_hrbp_tot["actual_bp"] > _all_hrbp_tot["bp_theory"]
        _all_hrbp_over_sapb = _all_hrbp_tot["actual_sapb"] > _all_hrbp_tot["sapb_theory"]
        _all_hrbp_c_sa = "#F44336" if _all_hrbp_over_sa else "#4CAFEE"
        _all_hrbp_c_bp = "#F44336" if _all_hrbp_over_bp else "#8BC34A"
        _all_hrbp_c_sapb = "#F44336" if _all_hrbp_over_sapb else "#00BCD4"
        _all_hrbp_theory_row = (
            _q_label("上限人数")
            + _q_td(_all_hrbp_tot["sa_theory"], "#b7bdc8")
            + _q_hint(_all_hrbp_tot["bp_theory"], "#b7bdc8", _q_bp_hint)
            + _q_td(_all_hrbp_tot["sapb_theory"], "#b7bdc8")
            + _q_label("剔除绩优/差")
            + _q_colspan("按实际评价", "#b7bdc8", 2)
            + _q_td(_all_hrbp_tot["base_cnt"], "#b7bdc8")
        )
        _all_hrbp_actual_row = (
            _q_label("实际人数")
            + _q_over(_all_hrbp_tot["actual_sa"], _all_hrbp_c_sa, _all_hrbp_over_sa)
            + _q_over(_all_hrbp_tot["actual_bp"], _all_hrbp_c_bp, _all_hrbp_over_bp)
            + _q_over(_all_hrbp_tot["actual_sapb"], _all_hrbp_c_sapb, _all_hrbp_over_sapb)
            + _q_td(_all_hrbp_tot["actual_b"], "#90A4AE")
            + _q_td(_all_hrbp_tot["actual_bm"], "#FFC107")
            + _q_td(_all_hrbp_tot["actual_c"], "#F44336")
            + _q_td(_all_hrbp_tot["actual_sum"], "#b7bdc8")
        )
        _all_hrbp_quota_html = f"""
        <div style='overflow-x:auto; margin-bottom:16px;'>
        <div style='font-size:16px;font-weight:700;color:#66b2ff;margin-bottom:8px;'>负责范围总体配额</div>
        <table style='width:100%;border-collapse:collapse;text-align:center;border:1px solid rgba(255,255,255,0.15);'>
        <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{_q_header}</tr></thead>
        <tbody>
        <tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{_all_hrbp_theory_row}</tr>
        <tr>{_all_hrbp_actual_row}</tr>
        </tbody>
        </table>
        </div>
        """
        st.markdown(_all_hrbp_quota_html, unsafe_allow_html=True)
        st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 配额统计口径：负责范围总体配额中，不含分管高管（因为自己不能调整自己）。另，负责范围总体配额由于增加了一级部门负责人，总体额度会大于所有部门配额之和。</div>", unsafe_allow_html=True)
        _hrbp_q_rows = []
        for dept_name in sorted(_hrbp_dept_grade_filtered.keys()):
            dg = _hrbp_dept_grade_filtered[dept_name]
            _hrbp_q_rows.append(f"<tr style='background:rgba(102,178,255,0.08);'><td colspan='8' style='text-align:left;padding:8px 12px;font-weight:700;color:#66b2ff;'>{dept_name}</td></tr>")
            theory_row = (
                _q_label("上限人数")
                + _q_td(dg.get("sa_theory", 0), "#b7bdc8")
                + _q_hint(dg.get("bp_theory", 0), "#b7bdc8", _q_bp_hint)
                + _q_td(dg.get("sapb_theory", 0), "#b7bdc8")
                + _q_label("剔除绩优/差")
                + _q_colspan("按实际评价", "#b7bdc8", 2)
                + _q_td(dg.get("base_cnt", 0), "#b7bdc8")
            )
            _over_sa = dg.get("actual_sa", 0) > dg.get("sa_theory", 0)
            _over_bp = dg.get("actual_bp", 0) > dg.get("bp_theory", 0)
            _over_sapb = dg.get("actual_sapb", 0) > dg.get("sapb_theory", 0)
            _c_sa = "#F44336" if _over_sa else "#4CAFEE"
            _c_bp = "#F44336" if _over_bp else "#8BC34A"
            _c_sapb = "#F44336" if _over_sapb else "#00BCD4"
            actual_row = (
                _q_label("实际人数")
                + _q_over(dg.get("actual_sa", 0), _c_sa, _over_sa)
                + _q_over(dg.get("actual_bp", 0), _c_bp, _over_bp)
                + _q_over(dg.get("actual_sapb", 0), _c_sapb, _over_sapb)
                + _q_td(dg.get("actual_b", 0), "#90A4AE")
                + _q_td(dg.get("actual_bm", 0), "#FFC107")
                + _q_td(dg.get("actual_c", 0), "#F44336")
                + _q_td(dg.get("actual_sum", 0), "#b7bdc8")
            )
            _hrbp_q_rows.append(f"<tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{theory_row}</tr>")
            _hrbp_q_rows.append(f"<tr>{actual_row}</tr>")
        _hrbp_q_html = f"""
        <div style='overflow-x:auto; margin-top:12px;'>
        <div style='font-size:16px;font-weight:700;color:#66b2ff;margin-bottom:8px;'>各部门配额</div>
        <table style='width:100%;border-collapse:collapse;text-align:center;border:1px solid rgba(255,255,255,0.15);'>
        <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{_q_header}</tr></thead>
        <tbody>{''.join(_hrbp_q_rows)}</tbody>
        </table>
        </div>
        """
        st.markdown(_hrbp_q_html, unsafe_allow_html=True)
        st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 配额统计口径：各部门配额中，不含一级部门负责人（因其不能调整自己）。</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>暂无配额数据</div>", unsafe_allow_html=True)

    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title'>🧾 部门绩效详情</div>", unsafe_allow_html=True)
    dept_rows = []
    _indent_hrbp = "\u2003\u2003"

    def _row(dept, scope, t, d, g, indent=0, mask_perf=False):
        disp_dept = (_indent_hrbp * indent) + dept if indent else dept
        if mask_perf:
            return {"部门": disp_dept, "口径": scope, "总人数": t, "已完成": "-", "完成率": "-", "S级": "-", "A级": "-", "B+级": "-", "B级": "-", "B-级": "-", "C级": "-"}
        rv = round(d / t * 100, 1) if t else 0
        return {"部门": disp_dept, "口径": scope, "总人数": t, "已完成": d, "完成率": "100%" if rv == 100 else f"{rv}%", "S级": g["S"], "A级": g["A"], "B+级": g["B+"], "B级": g["B"], "B-级": g["B-"], "C级": g["C"]}
    exec_responsible = set(vp_to_depts.keys()) & set(exec_by_name.keys())
    _single_list_depts_hrbp = {"人力资源部", "战略发展部"}
    _dept_to_vps_hrbp = {}
    for vp, depts in vp_to_depts.items():
        for d in depts:
            if d in _single_list_depts_hrbp:
                _dept_to_vps_hrbp.setdefault(d, set()).add(vp)
    _dept_need_single_list_hrbp = {d for d in _single_list_depts_hrbp if d in _dept_to_vps_hrbp and not any(vp in exec_by_name for vp in _dept_to_vps_hrbp[d])}
    for d in sorted(_dept_need_single_list_hrbp):
        dv = dept_stats_for_detail.get(d, {})
        if not dv or dv.get("total", 0) == 0:
            continue
        dept_rows.append(_row(d, "总", dv["total"], dv["done"], dv["grades"], indent=0))
        if (dv.get("sales_total") or 0) > 0 and (dv.get("non_sales_total") or 0) > 0:
            dept_rows.append(_row(d, "销售", dv["sales_total"], dv["sales_done"], dv.get("sales_grades", dv["grades"]), indent=1))
            dept_rows.append(_row(d, "非销售", dv["non_sales_total"], dv["non_sales_done"], dv.get("non_sales_grades", dv["grades"]), indent=1))
    for vp_name in sorted(vp_to_depts.keys()):
        if vp_name in exec_by_name and vp_name in exec_responsible:
            dval = exec_by_name[vp_name]
            if dval["total"] > 0:
                _m = dval.get("mask_perf", False)
                dept_rows.append(_row(f"高管（{vp_name}）", "总", dval["total"], dval["done"], dval["grades"], indent=0, mask_perf=_m))
                if (dval.get("sales_total") or 0) > 0 and (dval.get("non_sales_total") or 0) > 0:
                    dept_rows.append(_row(f"高管（{vp_name}）", "销售", dval["sales_total"], dval["sales_done"], dval.get("sales_grades", dval["grades"]), indent=1, mask_perf=_m))
                    dept_rows.append(_row(f"高管（{vp_name}）", "非销售", dval["non_sales_total"], dval["non_sales_done"], dval.get("non_sales_grades", dval["grades"]), indent=1, mask_perf=_m))
        for exec_name in sorted(exec_by_name.keys()):
            if exec_name == vp_name or exec_name in exec_responsible:
                continue
            if vp_name not in exec_to_vps.get(exec_name, set()):
                continue
            dval = exec_by_name[exec_name]
            if dval["total"] > 0:
                _m = dval.get("mask_perf", False)
                dept_rows.append(_row(f"高管（{exec_name}）", "总", dval["total"], dval["done"], dval["grades"], indent=1, mask_perf=_m))
                if (dval.get("sales_total") or 0) > 0 and (dval.get("non_sales_total") or 0) > 0:
                    dept_rows.append(_row(f"高管（{exec_name}）", "销售", dval["sales_total"], dval["sales_done"], dval.get("sales_grades", dval["grades"]), indent=2, mask_perf=_m))
                    dept_rows.append(_row(f"高管（{exec_name}）", "非销售", dval["non_sales_total"], dval["non_sales_done"], dval.get("non_sales_grades", dval["grades"]), indent=2, mask_perf=_m))
        for dept_name in sorted(vp_to_depts.get(vp_name, [])):
            if dept_name in _dept_need_single_list_hrbp:
                continue
            dval = dept_stats_for_detail.get(dept_name, {})
            if not dval or dval.get("total", 0) == 0:
                continue
            dept_rows.append(_row(dept_name, "总", dval["total"], dval["done"], dval["grades"], indent=1))
            if (dval.get("sales_total") or 0) > 0 and (dval.get("non_sales_total") or 0) > 0:
                dept_rows.append(_row(dept_name, "销售", dval["sales_total"], dval["sales_done"], dval.get("sales_grades", dval["grades"]), indent=2))
                dept_rows.append(_row(dept_name, "非销售", dval["non_sales_total"], dval["non_sales_done"], dval.get("non_sales_grades", dval["grades"]), indent=2))
    _hrbp_shown = set()
    for row in dept_rows:
        d = str(row.get("部门", ""))
        if "高管（" in d and "）" in d:
            beg = d.index("高管（") + 3
            end = d.index("）", beg)
            _hrbp_shown.add(d[beg:end].strip())
    for exec_name in sorted(exec_by_name.keys()):
        if exec_name in _hrbp_shown:
            continue
        dval = exec_by_name[exec_name]
        if dval["total"] > 0:
            _m = dval.get("mask_perf", False)
            dept_rows.append(_row(f"高管（{exec_name}）", "总", dval["total"], dval["done"], dval["grades"], indent=0, mask_perf=_m))
            if (dval.get("sales_total") or 0) > 0 and (dval.get("non_sales_total") or 0) > 0:
                dept_rows.append(_row(f"高管（{exec_name}）", "销售", dval["sales_total"], dval["sales_done"], dval.get("sales_grades", dval["grades"]), indent=1, mask_perf=_m))
                dept_rows.append(_row(f"高管（{exec_name}）", "非销售", dval["non_sales_total"], dval["non_sales_done"], dval.get("non_sales_grades", dval["grades"]), indent=1, mask_perf=_m))
    dept_df = pd.DataFrame(dept_rows)
    if not dept_df.empty:
        def _dept_row_style(row):
            scope = row.get("口径", "")
            dept = str(row.get("部门", ""))
            if "高管（" in dept:
                if scope == "总":
                    return ["font-weight: 700; font-size: 14px; background-color: rgba(255,193,7,0.12); color: #FFC107;"] * len(row)
                if scope == "销售":
                    return ["font-size: 12px; color: #81C784; background-color: rgba(255,193,7,0.06);"] * len(row)
                if scope == "非销售":
                    return ["font-size: 12px; color: #64B5F6; background-color: rgba(255,193,7,0.06);"] * len(row)
            if scope == "总":
                return ["font-weight: 700; font-size: 14px; background-color: rgba(255,255,255,0.06);"] * len(row)
            if scope == "销售":
                return ["font-size: 12px; color: #81C784;"] * len(row)
            if scope == "非销售":
                return ["font-size: 12px; color: #64B5F6;"] * len(row)
            return [""] * len(row)
        dept_df = dept_df.style.set_properties(**{"text-align": "center"}).apply(_dept_row_style, axis=1)
    st.dataframe(dept_df, use_container_width=True, hide_index=True)
    st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 提示：各部门含一级部门负责人，高管单列；如果一级部门负责人和高管为同一人，则一级部门中不含，高管单列。</div>", unsafe_allow_html=True)

    st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
    st.markdown("<div class='module-title' style='text-align:center;'>📥 数据下载</div>", unsafe_allow_html=True)
    _perf_cols = {"考核结果", "最终考核结果", "最终绩效结果", "上级评价是否完成", "分管高管调整考核结果", "一级部门调整考核结果"}
    _flat_rows = []
    for rec in report_records:
        f = rec.get("fields", {})
        row = {k: _extract_text(v, "") for k, v in f.items()}
        if _is_executive(rec):
            for k in _perf_cols:
                if k in row:
                    row[k] = "-"
        _flat_rows.append(row)
    _download_df = pd.DataFrame(_flat_rows)
    if not _download_df.empty:
        _all_cols = list(_download_df.columns)
        _sel_cols = st.multiselect("选择导出列", options=_all_cols, default=_all_cols, key="hrbp_download_col_sel")
        if not _sel_cols:
            _sel_cols = _all_cols
        _export_df = _download_df[_sel_cols]
        _csv_bytes = _export_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8")
        st.download_button("📥 下载 CSV", data=_csv_bytes, file_name=f"HRBP绩效_{current_cycle}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv", key="hrbp_download_csv")
    else:
        st.caption("暂无数据可下载")

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

def _load_demo_users_from_files(candidate_files):
    """从指定文件列表中读取用户，返回解析后的 users 列表。"""
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
                return raw_users
        except Exception:
            pass
    return []

def load_demo_users(demo_dept=None):
    """
    读取本地 demo 用户配置。
    demo_dept: None 或 "all" -> 合并四个部门（人力资源部、研发质量保障部、财富顾问部、资产管理部）
    demo_dept: "hr" -> 人力资源部 (demo_users_hr.json)
    demo_dept: "wealth" -> 财富顾问部 (demo_users_wealth.json)
    demo_dept: "rd" -> 研发质量保障部 (demo_users.json)
    demo_dept: "asset" -> 资产管理部 (demo_users_asset.json)
    """
    if demo_dept in (None, "", "all"):
        # 合并四个部门：依次读取，按 open_id 去重
        file_sets = [
            ["demo_users_hr.json", "demo_users_hr.example.json"],
            ["demo_users_wealth.json", "demo_users_wealth.example.json"],
            ["demo_users.json", "demo_users.example.json"],
            ["demo_users_asset.json", "demo_users_asset.example.json"],
        ]
        seen_open_ids = set()
        raw_users = []
        for candidate_files in file_sets:
            for item in _load_demo_users_from_files(candidate_files):
                if isinstance(item, dict):
                    oid = str(item.get("open_id", "")).strip()
                    if oid and oid not in seen_open_ids:
                        seen_open_ids.add(oid)
                        raw_users.append(item)
    else:
        if demo_dept == "hr":
            candidate_files = ["demo_users_hr.json", "demo_users_hr.example.json"]
        elif demo_dept == "wealth":
            candidate_files = ["demo_users_wealth.json", "demo_users_wealth.example.json"]
        elif demo_dept == "asset":
            candidate_files = ["demo_users_asset.json", "demo_users_asset.example.json"]
        else:
            candidate_files = ["demo_users.json", "demo_users.example.json"]
        raw_users = _load_demo_users_from_files(candidate_files)

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
    show_admin_entry = "admin_entry" in st.query_params and str(st.query_params.get("admin_entry", "")).strip() in ("1", "true", "yes")

    if is_demo_entry and not (ENABLE_DEMO_LOGIN and not IS_PROD):
        st.header("🎯 雪球人力资源绩效管理系统")
        st.warning("⚠️ 演示入口未开启。请设置 ENABLE_DEMO_LOGIN=true 且 APP_ENV≠production 后使用。")
        st.link_button("← 返回登录页", "?", use_container_width=True)
        return

    st.header("🎯 雪球人力资源绩效管理系统")

    if show_demo_only:
        dept_label = (
            "人力资源部、研发质量保障部、财富顾问部、资产管理部"
            if demo_dept in (None, "", "all")
            else {"hr": "人力资源部", "wealth": "财富顾问部", "rd": "研发质量保障部", "asset": "资产管理部"}.get(demo_dept, "研发质量保障部")
        )
        st.markdown("### 🎬 演示测试入口")
        st.caption(f"选择真实员工账号进行演示登录（{dept_label}）")
    else:
        st.markdown("### 🔐 飞书账号正式登录")
    if "code" in st.query_params and not show_demo_only:
        code = st.query_params["code"]
        oauth_state = st.query_params.get("state", "testing")
        with st.spinner("正在验证飞书身份..."):
            user_data, error_msg = get_feishu_user(code)
            if user_data:
                user_name = user_data.get("name", "")
                open_id = user_data.get("open_id") or user_data.get("id")
                # 后台管理入口：state=admin 或 state=hrbp
                if oauth_state in ("admin", "hrbp"):
                    record = get_record_by_openid_safely(
                        APP_TOKEN, TABLE_ID, open_id,
                        fallback_name=user_name, fallback_emp_id=user_data.get("emp_id", ""),
                    )
                    perm, scope = _check_admin_perm(record, user_name)
                    if perm == "admin" and oauth_state == "admin":
                        st.session_state.user_info = user_data
                        st.session_state.role = None
                        st.session_state.admin_role = "admin"
                        st.session_state.admin_scope = None
                        st.session_state.feishu_record_id = None
                        st.query_params.clear()
                        st.rerun()
                    elif perm in ("hrbp", "hrbp_lead") and oauth_state == "hrbp":
                        st.session_state.user_info = user_data
                        st.session_state.role = None
                        st.session_state.admin_role = perm
                        st.session_state.admin_scope = scope or []
                        st.session_state.feishu_record_id = None
                        st.query_params.clear()
                        st.rerun()
                    else:
                        label = "管理员" if oauth_state == "admin" else "HRBP"
                        st.error(f"❌ 您无{label}权限。请确认飞书表中「后台角色」已正确配置。")
                        if st.button("🔄 返回登录页", key="btn_back_from_admin"):
                            st.query_params.clear()
                            st.rerun()
                else:
                    # 普通员工登录
                    st.session_state.user_info = user_data
                    st.session_state.role = None
                    st.session_state.admin_role = None
                    st.session_state.admin_scope = None
                    st.query_params.clear()
                    st.rerun()
            else:
                st.error(f"❌ 授权失败。飞书底层拦截原因：{error_msg}")
                if st.button("🔄 清除失效 Code 并重新登录", key="btn_clear_code"):
                    st.query_params.clear()
                    st.rerun()
    elif not show_demo_only:
        encoded_uri = urllib.parse.quote(str(REDIRECT_URI or ""))
        base_url = "https://open.feishu.cn/open-apis/authen/v1/user_auth_page_beta"
        auth_url = base_url + f"?app_id={APP_ID}&redirect_uri={encoded_uri}&state=testing"

        if show_admin_entry:
            # 仅展示管理员/HRBP 登录
            st.link_button("← 返回员工登录", "?", use_container_width=False)
            st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)
            st.markdown("### 👤 管理员登录")
            auth_admin = base_url + f"?app_id={APP_ID}&redirect_uri={encoded_uri}&state=admin"
            st.link_button("🔗 管理员登录", auth_admin, use_container_width=True)
            st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)
            st.markdown("### 👤 HRBP 登录")
            auth_hrbp = base_url + f"?app_id={APP_ID}&redirect_uri={encoded_uri}&state=hrbp"
            st.link_button("🔗 HRBP 登录", auth_hrbp, use_container_width=True)
        else:
            # 员工个人登录
            st.info("💡 提示：请点击下方链接，在浏览器中完成飞书授权登录。")
            col_emp, col_auth = st.columns(2)
            with col_emp:
                st.write("👤 **员工个人**")
            with col_auth:
                st.link_button("🔗 飞书授权登录", auth_url, use_container_width=True)
            # 右下角不明显的管理员入口图标
            st.markdown(
                '<div style="position:fixed;bottom:16px;right:16px;z-index:9999;opacity:0.45;">'
                '<a href="?admin_entry=1" title="管理员入口" style="font-size:22px;text-decoration:none;">👤</a>'
                '</div>',
                unsafe_allow_html=True,
            )

    # 演示入口：demo_entry=1 时仅展示此块；否则在飞书登录下方展示（管理员入口模式下不展示）
    if ENABLE_DEMO_LOGIN and not IS_PROD and not show_admin_entry:
        if not show_demo_only:
            st.markdown("---")
            st.markdown("### 🛠️ 演示与测试通道")
        demo_users = load_demo_users(demo_dept)
        hr_users = load_demo_users("hr")  # 管理员/HRBP 测试账号固定从人力资源部取
        mgr_users = [u for u in demo_users if u["role"] == "管理者"]
        emp_users = [u for u in demo_users if u["role"] == "员工"]
        admin_demo = next((u for u in hr_users if u.get("name") == "张燕"), None)
        # HRBP Lead: 孟凡卓、孙春悦；HRBP: 谭莹、曹亦雄
        hrbp_demo_users = [u for u in hr_users if u.get("name") in ("孟凡卓", "孙春悦", "谭莹", "曹亦雄")]
        # 第一排：管理者 / 下属
        row1_c1, row1_c2 = st.columns(2)
        with row1_c1:
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
                        st.session_state.admin_role = None
                        st.session_state.admin_scope = None
                        st.rerun()
            else:
                st.caption("未配置管理者测试账号")

        with row1_c2:
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
                        st.session_state.admin_role = None
                        st.session_state.admin_scope = None
                        st.rerun()
            else:
                st.caption("未配置员工测试账号")

        # 第二排：管理员 / HRBP
        row2_c1, row2_c2 = st.columns(2)
        with row2_c1:
            st.write("👤 **管理员测试 (张燕)**")
            if admin_demo:
                if st.button("🛠️ 管理员登录", use_container_width=True, key="btn_login_admin_demo"):
                    st.session_state.user_info = {
                        "name": admin_demo["name"],
                        "open_id": admin_demo["open_id"],
                        "emp_id": admin_demo["emp_id"],
                        "job_title": admin_demo["job_title"]
                    }
                    st.session_state.role = None
                    st.session_state.admin_role = "admin"
                    st.session_state.admin_scope = None
                    st.session_state.feishu_record_id = None
                    st.rerun()
            else:
                st.caption("需在 demo_users_hr 中配置张燕")

        with row2_c2:
            st.write("👥 **HRBP 测试 (HRBP Lead / HRBP)**")
            if hrbp_demo_users:
                selected_hrbp_label = st.selectbox("选择 HRBP 测试账号", [u["label"] for u in hrbp_demo_users], key="demo_hrbp_select", label_visibility="collapsed")
                if st.button("🛠️ HRBP 登录", use_container_width=True, key="btn_login_hrbp_demo"):
                    hrbp_demo = next((u for u in hrbp_demo_users if u["label"] == selected_hrbp_label), hrbp_demo_users[0])
                    # 优先从飞书表「后台角色」读取；无记录或未配置时回退：孟凡卓、孙春悦、谭莹 为 HRBP Lead
                    _role = None
                    _rec = get_record_by_openid_safely(
                        APP_TOKEN, TABLE_ID, hrbp_demo.get("open_id", ""),
                        fallback_name=hrbp_demo.get("name", ""), fallback_emp_id=hrbp_demo.get("emp_id", ""),
                    )
                    if isinstance(_rec, dict):
                        _back_role = _extract_text(_rec.get("fields", {}).get("后台角色"), "").strip()
                        if _back_role == "HRBP Lead":
                            _role = "hrbp_lead"
                        elif _back_role == "HRBP":
                            _role = "hrbp"
                    if _role is None:
                        _role = "hrbp_lead" if hrbp_demo["name"] in ("孟凡卓", "孙春悦", "谭莹") else "hrbp"
                    st.session_state.user_info = {
                        "name": hrbp_demo["name"],
                        "open_id": hrbp_demo["open_id"],
                        "emp_id": hrbp_demo["emp_id"],
                        "job_title": hrbp_demo["job_title"]
                    }
                    st.session_state.role = None
                    st.session_state.admin_role = _role
                    st.session_state.admin_scope = None  # 测试环境从飞书表动态计算
                    st.session_state.feishu_record_id = None
                    st.rerun()
            else:
                st.caption("需在 demo_users_hr 中配置 HRBP/HRBP Lead（孟凡卓、孙春悦、谭莹、曹亦雄）")

        if not demo_users:
            if demo_dept in (None, "", "all"):
                st.info("💡 提示：未读取到任一 demo 用户文件。可运行 `python3 get_open_ids.py 人力资源部`、`python3 get_open_ids.py 研发质量保障部`、`python3 get_open_ids.py 财富顾问部`、`python3 get_open_ids.py 资产管理部` 分别生成对应 JSON。")
            elif demo_dept == "hr":
                st.info("💡 提示：未读取到 demo_users_hr.json，可运行 `python3 get_open_ids.py 人力资源部` 生成。")
            elif demo_dept == "wealth":
                st.info("💡 提示：未读取到 demo_users_wealth.json，可运行 `python3 get_open_ids.py 财富顾问部` 生成。")
            elif demo_dept == "asset":
                st.info("💡 提示：未读取到 demo_users_asset.json，可运行 `python3 get_open_ids.py 资产管理部 > demo_users_asset.json` 生成。")
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
def main_app():  # pyright: ignore[reportGeneralTypeIssues]
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

    /* 自评页「确认提交」按钮红色（隐藏 marker 保持对齐） */
    div.element-container:has(.self-eval-submit-marker) {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    /* 未填完：灰色；填完且符合要求：红色 */
    div.element-container:has(.self-eval-submit-marker) + div.element-container button {
        background-color: #5a5a5a !important;
        color: #9e9e9e !important;
        border: 1px solid #4a4a4a !important;
    }
    div.element-container:has(.self-eval-submit-marker) + div.element-container button:not(:disabled) {
        background-color: #d32f2f !important;
        color: #ffffff !important;
        border: 1px solid #b71c1c !important;
    }
    div.element-container:has(.self-eval-submit-marker) + div.element-container button:not(:disabled):hover {
        background-color: #b71c1c !important;
        color: #ffffff !important;
        border-color: #d32f2f !important;
    }

    /* 上级评分页「确认提交」按钮：未全部暂存灰色，全部暂存红色 */
    div.element-container:has(.mgr-submit-marker) {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    div.element-container:has(.mgr-submit-marker) + div.element-container button {
        background-color: #5a5a5a !important;
        color: #9e9e9e !important;
        border: 1px solid #4a4a4a !important;
    }
    div.element-container:has(.mgr-submit-marker) + div.element-container button:not(:disabled) {
        background-color: #d32f2f !important;
        color: #ffffff !important;
        border: 1px solid #b71c1c !important;
    }
    div.element-container:has(.mgr-submit-marker) + div.element-container button:not(:disabled):hover {
        background-color: #b71c1c !important;
        color: #ffffff !important;
        border-color: #d32f2f !important;
    }

    /* 草稿按钮幽灵绿 */
    div.element-container:has(.save-marker) + div.element-container button {
        background-color: rgba(46, 125, 50, 0.2) !important;
        color: #81c784 !important;
        border: 1px solid #4caf50 !important;
    }
    div.element-container:has(.save-marker) + div.element-container button:disabled {
        background-color: #5a5a5a !important;
        color: #9e9e9e !important;
        border: 1px solid #4a4a4a !important;
    }
    div.element-container:has(.save-marker) + div.element-container button:not(:disabled):hover {
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

    /* HRBP 视图：居中对齐 */
    div:has(.hrbp-view-container) ~ div [data-testid="stMarkdown"] p,
    div:has(.hrbp-view-container) ~ div [data-testid="stCaptionContainer"],
    div:has(.hrbp-view-container) ~ div [data-testid="stCaptionContainer"] p,
    div:has(.hrbp-view-container) ~ div [data-testid="column"] { text-align: center !important; }
    /* HRBP 两行标题：强制居左（提高特异性以覆盖上方居中规则） */
    div:has(.hrbp-view-container) ~ div [data-testid="stMarkdown"] p.hrbp-left-label { text-align: left !important; }
    div:has(.hrbp-view-container) ~ div div[data-testid="column"] > div { justify-content: center !important; align-items: center !important; }
    .hrbp-view-container { display: none !important; }

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
    .mgr-sub-list div.element-container:has(.xqps-btn-view) + div.element-container button {
        background: #42A5F5 !important;
        color: #ffffff !important;
    }
    /* 上级评分「收起面板」：透明样式 */
    div.element-container:has(.xqps-btn-collapse) + div.element-container button {
        background: transparent !important;
        color: #ffffff !important;
        border: 1px solid rgba(255,255,255,0.25) !important;
    }
    /* 团队历史绩效「直属」按钮：蓝色 #4799e4，字体小两级 */
    div.element-container:has(.history-direct-btn-marker) {
        display: none !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    div.element-container:has(.history-direct-btn-marker) + div.element-container button {
        background: #4799e4 !important;
        color: #ffffff !important;
        font-size: 11px !important;
        font-weight: 500 !important;
        min-height: 28px !important;
        height: 28px !important;
        padding: 4px 12px !important;
        border-radius: 8px !important;
        border: none !important;
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
    /* 综合调整：确认本次调整（与上级评分一致：未全部填写灰色，全部填写红色） */
    div.element-container:has(.dept-confirm-marker) + div.element-container button,
    div.element-container:has(.vp-confirm-marker) + div.element-container button {
        background-color: #5a5a5a !important;
        color: #9e9e9e !important;
        border: 1px solid #4a4a4a !important;
        box-shadow: none !important;
        min-height: 34px !important;
        height: 34px !important;
        border-radius: 10px !important;
        font-size: 14px !important;
        font-weight: 700 !important;
    }
    div.element-container:has(.dept-confirm-marker) + div.element-container button:not(:disabled),
    div.element-container:has(.vp-confirm-marker) + div.element-container button:not(:disabled) {
        background-color: #d32f2f !important;
        color: #ffffff !important;
        border: 1px solid #b71c1c !important;
    }
    div.element-container:has(.dept-confirm-marker) + div.element-container,
    div.element-container:has(.vp-confirm-marker) + div.element-container {
        margin-top: 2px !important;
        margin-bottom: 6px !important;
    }
    div.element-container:has(.dept-confirm-marker) + div.element-container button:not(:disabled):hover,
    div.element-container:has(.vp-confirm-marker) + div.element-container button:not(:disabled):hover {
        background-color: #b71c1c !important;
        border-color: #d32f2f !important;
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
    /* 回到顶端悬浮按钮：上级评分、一级部门负责人调整、分管高管调整页右下角 */
    .back-to-top-btn {
        position: fixed !important;
        bottom: 24px !important;
        right: 24px !important;
        z-index: 9999 !important;
        padding: 10px 18px !important;
        font-size: 14px !important;
        font-weight: 600 !important;
        color: rgba(255, 255, 255, 0.85) !important;
        background: rgba(0, 0, 0, 0.25) !important;
        border: 1px solid rgba(255, 255, 255, 0.25) !important;
        border-radius: 8px !important;
        cursor: pointer !important;
        box-shadow: none !important;
        transition: background 0.2s, transform 0.15s !important;
        text-decoration: none !important;
        display: inline-flex !important;
        align-items: center !important;
        gap: 6px !important;
        font-family: inherit !important;
        -webkit-appearance: none !important;
        appearance: none !important;
        backdrop-filter: blur(8px) !important;
    }
    .back-to-top-btn:hover {
        background: rgba(0, 0, 0, 0.4) !important;
        color: #ffffff !important;
        transform: translateY(-2px) !important;
        border-color: rgba(255, 255, 255, 0.35) !important;
    }
    .back-to-top-btn:active {
        transform: translateY(0) !important;
    }
    html { scroll-behavior: smooth !important; }
    /* 一级部门负责人：点击🔗后的提示，2秒后淡出消失 */
    @keyframes dept-hint-fadeout {
        0%, 80% { opacity: 1; max-height: 80px; }
        100% { opacity: 0; max-height: 0; overflow: hidden; padding: 0; margin: 0; }
    }
    .dept-view-hint-2s {
        animation: dept-hint-fadeout 2.5s ease-in forwards;
    }
    /* 一级部门负责人/分管高管调整：自评等级列🔗按钮缩小，边框透明，自评等级不断行 */
    button[class*="dept-view-self"],
    button[class*="dept_view_self"],
    button[class*="vp-view-self"],
    button[class*="vp_view_self"] {
        min-width: 28px !important;
        width: 28px !important;
        min-height: 28px !important;
        height: 28px !important;
        padding: 0 !important;
        font-size: 12px !important;
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        border-color: transparent !important;
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

    def action_button(action_type, label, key, use_container_width=True, disabled=False, on_click=None, help=None):
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
            help=help,
        )

    # 管理员：全公司绩效视图（复制视图与报表格式）
    if st.session_state.get("admin_role") == "admin":
        _render_admin_dashboard()
        return

    # HRBP / HRBP Lead：负责部门绩效概览（关停时不可访问）
    if st.session_state.get("admin_role") in ("hrbp", "hrbp_lead"):
        if _is_module_disabled("HRBP"):
            st.error("🔒 当前功能已关停，暂不可操作。请联系管理员。")
            if st.button("🚪 退出登录", key="btn_hrbp_logout"):
                st.session_state.clear()
                st.rerun()
        else:
            _render_hrbp_dashboard()
        return

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
            except Exception as e:
                st.session_state.feishu_record_id = "FETCH_ERROR"
                st.error(f"⚠️ 连接飞书异常: {e}")

    # 1.5 异常处理：不在考核表内 或 连接失败
    if st.session_state.feishu_record_id == "NOT_FOUND":
        st.info("💡 您不在本次考核期内，如有疑问请联系 HR。")
        if st.button("🚪 退出登录", key="btn_logout_not_in_scope"):
            st.session_state.clear()
            st.rerun()
        return
    if st.session_state.feishu_record_id == "FETCH_ERROR":
        if st.button("🔄 重试", key="btn_retry_fetch"):
            st.session_state.feishu_record_id = None
            st.rerun()
        return

    # 2. 准备基础数据与个人计算
    fields = st.session_state.feishu_record
    role_from_record = extract_text(fields.get("角色"), "").strip()
    if st.session_state.role not in ["员工", "管理者"]:
        st.session_state.role = role_from_record if role_from_record in ["员工", "管理者"] else "员工"
    is_submitted = (extract_text(fields.get("自评是否提交")).strip() == "是")
    
    user_name = st.session_state.user_info.get('name', '未知用户')
    emp_id = extract_text(fields.get('工号') or fields.get('员工工号'), st.session_state.user_info.get('emp_id', '未绑定'))
    job_title = extract_text(fields.get('岗位') or fields.get('职位'), st.session_state.user_info.get('job_title', '未分配'))
    _cycle_raw = (
        extract_text(fields.get("绩效考核周期"), "").strip()
        or extract_text(fields.get("考核周期"), "").strip()
        or extract_text(fields.get("本次绩效考核周期"), "").strip()
        or extract_text(fields.get("本次考核周期"), "").strip()
        or "2026年上半年"
    )
    _cycle_from_record = _normalize_cycle_display(_cycle_raw) or _cycle_raw
    current_cycle = _read_admin_cycle_override() or _cycle_from_record
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
    my_direct_subs = []  # 直属下级（可评价/调整）；my_all_subs 含隔级下属（仅展示）
    all_records_snapshot = []
    is_dept_head = False
    is_vp = False
    if st.session_state.role == "管理者":
        with st.spinner("正在拉取团队数据..."):
            all_records = fetch_all_records_safely(APP_TOKEN, TABLE_ID)
            all_records_snapshot = all_records
            for record in all_records:
                rec_fields = record.get("fields", {})
                rec_manager = extract_text(rec_fields.get("直接评价人") or rec_fields.get("评价人"))
                rec_skip_level = extract_text(rec_fields.get("隔级上级"), "").strip()
                is_direct = user_name and user_name in rec_manager
                is_skip_level = user_name and user_name in rec_skip_level and not is_direct
                if is_direct:
                    my_direct_subs.append(record)
                    my_all_subs.append(record)
                    if extract_text(rec_fields.get("自评是否提交")).strip() == "是":
                        real_subordinates.append(record)
                elif is_skip_level:
                    my_all_subs.append(record)
                    if extract_text(rec_fields.get("自评是否提交")).strip() == "是":
                        real_subordinates.append(record)

            direct_record_ids = {s["record_id"] for s in my_direct_subs}

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
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    st.sidebar.markdown("### 📅 绩效考核周期")
    _cycle_disp = _normalize_cycle_display(current_cycle) or current_cycle
    st.sidebar.markdown(f"""
        <div style="background-color: rgba(38, 39, 48, 0.8); padding: 15px; border-radius: 8px; border: 1px solid #333;">
            <div style="margin-bottom: 10px; color: #b0b0b0; font-size: 14px;">{_cycle_disp}</div>
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
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    # 当主管切换到「员工自评」tab 时，清空下属选择，使验证模块显示自评内容而非下属验证
    current_tab = st.session_state.get("main_tabs")
    if st.session_state.role == "管理者":
        if current_tab == "📝 员工自评" or current_tab is None:
            st.session_state.selected_subordinate_id = None
        if current_tab != "📌 一级部门负责人调整":
            st.session_state.pop("dept_view_self_record_id", None)
        if current_tab != "📌 分管高管调整":
            st.session_state.pop("vp_view_self_record_id", None)

    is_evaluating_sub = (st.session_state.role == "管理者" and st.session_state.selected_subordinate_id is not None)
    step2_can_submit = False

    # --- 统一验证模块区域 (紧贴个人信息下方) ---
    if is_evaluating_sub:
        # 【管理者评估下属的验证模块】
        sub_id_str = st.session_state.selected_subordinate_id
        current_sub = next((s for s in my_all_subs if s["record_id"] == sub_id_str), None)
        if current_sub:
            sub_f = current_sub.get("fields", {})
            _direct_ids = {s["record_id"] for s in my_direct_subs} if st.session_state.role == "管理者" else set()
            is_direct_in_validation = sub_id_str in _direct_ids
            if not is_direct_in_validation:
                st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
                st.sidebar.markdown("### 🚦 验证模块")
                st.sidebar.info("ℹ️ 隔级下属仅可查看，无调整权限")
                st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
            else:
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
                st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
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
            
        st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
        
        st.sidebar.markdown("### 📊 结果模块")
        _can_see_result = _is_result_visible_for_user()
        _final_grade = "-"
        _final_comment = ""
        if _can_see_result:
            # 开放查看时：不论自评/上级评分/分管高管调整状态，均读取并展示当前最佳可得值
            vp_done = extract_text(fields.get("分管高管调整完毕"), "").strip() == "是"
            dept_done = extract_text(fields.get("一级部门调整完毕"), "").strip() == "是"
            vp_adj = extract_text(fields.get("分管高管调整考核结果"), "").strip()
            dept_adj = extract_text(fields.get("一级部门调整考核结果"), "").strip()
            mgr_g = extract_text(fields.get("考核结果"), "").strip()
            if vp_done and vp_adj in GRADE_OPTIONS:
                _final_grade = vp_adj
            elif dept_done and dept_adj in GRADE_OPTIONS:
                _final_grade = dept_adj
            elif mgr_g in GRADE_OPTIONS:
                _final_grade = mgr_g
            _final_comment = extract_text(fields.get("考核评语", ""), "").strip()
            if _final_comment in ["", "未获取", "None", "0"]:
                _final_comment = ""
        # 开放查看时展示实际值（含"-"/"暂无"）；未开放时展示"待审批"
        _final_grade_disp = _final_grade if _can_see_result else "待审批"
        _final_comment_disp = (
            _final_comment.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if _can_see_result and _final_comment
            else ("暂无" if _can_see_result else "待审批")
        )
        _grade_style = "color: #1E90FF; font-weight: bold; font-size: 18px;" if _can_see_result and _final_grade in GRADE_OPTIONS else "color: #757575;"
        _grade_row_style = "color: #757575;" if not (_can_see_result and _final_grade in GRADE_OPTIONS) else "color: #FAFAFA;"
        _comment_style = "color: #FAFAFA; font-size: 15px;" if _can_see_result and _final_comment else "color: #757575;"
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
            <div style="margin-bottom: 12px; font-size: 15px; {_grade_row_style}">
                <span style="display:inline-block; width: 105px;">最终考核结果：</span> 
                <span style="{_grade_style}">{_final_grade_disp}</span>
            </div>
            <div style="{_comment_style}">
                <span style="display:inline-block; width: 105px;">考核评语：</span> 
                <span style="white-space:pre-wrap; word-break:break-word;">{_final_comment_disp}</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

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
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
    st.sidebar.markdown("### 📚 制度学习")
    _doc_items = []
    for _dn in DOC_LINK_NAMES:
        _url = _get_doc_link(_dn)
        if _url:
            _doc_items.append(f'<div style="margin-bottom: 8px; padding: 6px 8px; border-radius: 4px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);"><a href="{_url}" target="_blank" style="color: #b7bdc8; text-decoration: none;">{_dn}</a></div>')
        else:
            _doc_items.append(f'<div style="margin-bottom: 8px; padding: 6px 8px; border-radius: 4px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); color: #888;">{_dn}</div>')
    _doc_items[-1] = _doc_items[-1].replace("margin-bottom: 8px;", "margin-bottom: 0;")
    st.sidebar.markdown(f"""
    <div class="policy-learning-box" style="font-size: 11px;">
        {''.join(_doc_items)}
    </div>
    """, unsafe_allow_html=True)
    st.sidebar.markdown("<hr style='border:none;border-top:1px solid rgba(255,255,255,0.2);margin:12px 0;'/>", unsafe_allow_html=True)
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
        tab_list.append("📂 团队历史绩效")
    else:
        # 员工：显示自评+历史绩效；员工个人：仅显示自评，不显示团队历史绩效
        if role_from_record == "员工个人":
            tab_list = ["📝 员工自评"]
        else:
            tab_list = ["📝 员工自评", "📂 团队历史绩效"]

    # 回到顶端锚点：供下方悬浮按钮使用
    if st.session_state.role == "管理者":
        st.markdown('<div id="page-top" style="height:0;overflow:hidden;margin:0;padding:0;line-height:0;"></div>', unsafe_allow_html=True)
    tabs = st.tabs(tab_list, key="main_tabs", on_change="rerun")
    idx_self = 0
    idx_mgr = 1 if st.session_state.role == "管理者" else 0
    idx_dept_head = tab_list.index("📌 一级部门负责人调整") if "📌 一级部门负责人调整" in tab_list else None
    idx_vp = tab_list.index("📌 分管高管调整") if "📌 分管高管调整" in tab_list else None
    idx_reports = tab_list.index("📊 视图与报表") if "📊 视图与报表" in tab_list else None

    # ==========================================
    # 🟢 模块 1：员工自评 (索引 0)
    # ==========================================
    with tabs[idx_self]:
        if _is_module_disabled("员工自评"):
            st.error("🔒 当前功能已关停，暂不可操作。请联系管理员。")
        else:
            _self_edit_disabled = _is_module_edit_disabled("员工自评")
            if _self_edit_disabled:
                st.info("📌 当前阶段编辑已关闭，仅可查看。")
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
            _ann_self = _read_admin_announcement("员工自评")
            if _ann_self:
                _ann_body = _ann_self.replace("\n", "<br>")
                st.markdown(f"""
                <div style="margin: 12px 0 16px 0; padding: 14px 16px; background: rgba(33, 150, 243, 0.12); border-radius: 8px; border-left: 4px solid #2196F3; font-size: 14px; line-height: 1.7; color: #E0E0E0;">
                    <div style="font-weight: 600; margin-bottom: 8px; color: #90CAF9;">📢 公告</div>
                    <div>{_ann_body}</div>
                </div>
                """, unsafe_allow_html=True)
            if is_submitted:
                st.success("🔒 您的自评已提交，当前表单不可修改。")
            _self_no_edit = is_submitted or _self_edit_disabled
            st.markdown("<div class='module-title'>💼 工作模块</div>", unsafe_allow_html=True)
            st.info(f"💡 提示：工作模块总体占比 {target_weight}% (各目标权重之和必须等于 {target_weight}%)")
            hint_placeholder = "如需更大操作区域，可拖动文本框右下角放大区域。"
            for i in range(1, st.session_state.goal_count + 1):
                col_left, col_right = st.columns([3, 1])
                with col_left:
                    st.text_area(
                        f"工作目标{i}及总结",
                        height=110,
                        disabled=_self_no_edit,
                        key=f"obj_summary_{i}",
                        placeholder=hint_placeholder,
                    )
                with col_right:
                    st.number_input(f"工作目标{i}权重(%)", min_value=0, max_value=100, step=5, disabled=_self_no_edit, key=f"obj_weight_{i}")
                    st.selectbox(f"工作目标{i}自评得分", options=SCORE_OPTIONS, disabled=_self_no_edit, key=f"obj_score_{i}")
                st.markdown("---")
            if not _self_no_edit:
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
                        "通用能力总结",
                        height=110,
                        disabled=_self_no_edit,
                        key="comp_summary",
                        placeholder="结合考核期工作实际情况，从「思考、行动、协作、成长」四个维度总结",
                    )
                with col_comp_right: st.selectbox("通用能力自评得分", options=SCORE_OPTIONS, disabled=_self_no_edit, key="comp_score")
            elif st.session_state.role == "管理者":
                st.info("💡 提示：通用能力占比 20%、领导力占比 20%")
                st.text_area(
                    "通用能力总结",
                    height=110,
                    disabled=_self_no_edit,
                    key="comp_summary",
                    placeholder="结合考核期工作实际情况，从「思考、行动、协作、成长」四个维度总结",
                )
                st.selectbox("通用能力自评得分", options=SCORE_OPTIONS, disabled=_self_no_edit, key="comp_score")
                st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)
                st.text_area(
                    "领导力总结",
                    height=110,
                    disabled=_self_no_edit,
                    key="lead_summary",
                    placeholder="请结合考核周期工作实际情况，从「领导力」维度进行阐述与总结",
                )
                st.selectbox("领导力自评得分", options=SCORE_OPTIONS, disabled=_self_no_edit, key="lead_score")
            if not _self_no_edit:
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
                    st.markdown("<div class='self-eval-submit-marker'></div>", unsafe_allow_html=True)
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
                    if st.button("保存草稿", use_container_width=True, key="btn_self_eval_save_draft"):
                        with st.spinner("同步数据至飞书..."):
                            success, error_msg = update_record_safely(APP_TOKEN, TABLE_ID, st.session_state.feishu_record_id, payload_data)
                            if success:
                                for k, v in payload_data.items(): st.session_state.feishu_record[k] = v
                                st.session_state.self_eval_draft_saved = True
                            else: st.error(f"❌ 暂存失败！{error_msg}")
                if st.session_state.pop("self_eval_draft_saved", False):
                    st.success("💡 提示：已妥善保存。")
            st.info("💡 提示：点击「确认提交」即意味着本次自评结束，不可再修改。")

    # ==========================================
    # 🟢 模块 2：管理者专属权限 
    # ==========================================
    if st.session_state.role == "管理者":
        with tabs[idx_mgr]:
            if _is_module_disabled("上级评分"):
                st.error("🔒 当前功能已关停，暂不可操作。请联系管理员。")
            elif my_all_subs:
                _mgr_edit_disabled = _is_module_edit_disabled("上级评分")
                if _mgr_edit_disabled:
                    st.info("📌 当前阶段编辑已关闭，仅可查看。")
                # --- 1. 下属评估进展看板 ---
                total_subs = len(my_all_subs)
                submitted_subs = len(real_subordinates)
                
                rated_subs = 0
                drafted_subs = 0
                grade_list = []
                
                for sub in my_all_subs:
                    sub_f = sub.get("fields", {})
                    is_mgr_done = extract_text(sub_f.get("上级评价是否完成")).strip() == "是"
                    current_grade = extract_text(sub_f.get("考核结果")).strip()
                    
                    if is_mgr_done:
                        rated_subs += 1
                    elif current_grade and current_grade not in ["", "未获取", "-"]:
                        drafted_subs += 1
                        
                    if current_grade and current_grade not in ["", "未获取", "-"]:
                        grade_list.append(current_grade)
                        
                unrated_subs = total_subs - rated_subs - drafted_subs

                # 上级评分公告（仅展示管理员发布内容，不再从飞书抓取）
                _ann_mgr = _read_admin_announcement("上级评分")
                if _ann_mgr:
                    _mgr_ann_body = _ann_mgr.replace("\n", "<br>")
                    st.markdown(f"""
                    <div style="margin: 0 0 16px 0; padding: 14px 16px; background: rgba(33, 150, 243, 0.12); border-radius: 8px; border-left: 4px solid #2196F3; font-size: 14px; line-height: 1.7; color: #E0E0E0;">
                        <div style="font-weight: 600; margin-bottom: 8px; color: #90CAF9;">📢 公告</div>
                        <div>{_mgr_ann_body}</div>
                    </div>
                    """, unsafe_allow_html=True)
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
                    st.info("💡 提示：当前暂无下属。")
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

                    # 确认提交与提示：与一级部门负责人/分管高管一致，放置在筛选框下方
                    # 逻辑：全部直属已提交（上级评价是否完成=是）则不可再提交，确认按钮置灰
                    mgr_can_confirm_all = True
                    mgr_incomplete_names = []
                    for _s in my_direct_subs:
                        _sf = _s.get("fields", {})
                        _done = extract_text(_sf.get("上级评价是否完成")).strip() == "是"
                        _grade = extract_text(_sf.get("考核结果")).strip()
                        _name = extract_text(_sf.get("姓名"), "").strip()
                        if not _done and _grade not in GRADE_OPTIONS:
                            mgr_can_confirm_all = False
                            mgr_incomplete_names.append(_name or "未知")
                        # 不 break，继续收集所有未完成者
                    if my_direct_subs and all(extract_text(_s.get("fields", {}).get("上级评价是否完成")).strip() == "是" for _s in my_direct_subs):
                        mgr_can_confirm_all = False  # 全部已提交，按钮置灰不可再提交
                    st.info("💡 提示：点击「确认提交」即意味着对全部下属的本次评估结束，不可再修改。")
                    if not mgr_can_confirm_all and mgr_incomplete_names:
                        st.warning(f"⚠️ 以下 {len(mgr_incomplete_names)} 人尚未完成评分并保存草稿，请先完成后再提交：{', '.join(mgr_incomplete_names[:5])}{'...' if len(mgr_incomplete_names) > 5 else ''}")
                    st.markdown("<div class='mgr-submit-marker'></div>", unsafe_allow_html=True)
                    if st.button("确认提交", type="primary", use_container_width=True, key="btn_mgr_confirm_all", disabled=not mgr_can_confirm_all or _mgr_edit_disabled):
                        # 二次校验：确保全部下属均有考核等级
                        pre_check_fail = []
                        for sub in my_direct_subs:
                            sf = sub.get("fields", {})
                            mgr_done = extract_text(sf.get("上级评价是否完成")).strip() == "是"
                            curr_grade = extract_text(sf.get("考核结果")).strip()
                            if not mgr_done and curr_grade not in GRADE_OPTIONS:
                                pre_check_fail.append(extract_text(sf.get("姓名"), "").strip() or "未知")
                        if pre_check_fail:
                            st.error(f"❌ 以下人员尚未完成评分并保存草稿，无法提交：{', '.join(pre_check_fail[:5])}{'...' if len(pre_check_fail) > 5 else ''}")
                        else:
                            with st.spinner("正在提交并锁定全部下属绩效..."):
                                submitted_cnt = 0
                                err_msgs = []
                                for sub in my_direct_subs:
                                    sf = sub.get("fields", {})
                                    rid = sub.get("record_id")
                                    mgr_done = extract_text(sf.get("上级评价是否完成")).strip() == "是"
                                    curr_grade = extract_text(sf.get("考核结果")).strip()
                                    if mgr_done or curr_grade not in GRADE_OPTIONS:
                                        continue
                                    final_data = {"上级评价是否完成": "是"}
                                    sub_name = extract_text(sf.get("姓名"), "").strip()
                                    dept_head_str = extract_text(sf.get("一级部门负责人") or sf.get("部门负责人"), "").strip()
                                    is_dept_head_self = (sub_name in dept_head_str) or any(p.strip() == sub_name for p in dept_head_str.split(",") if p.strip())
                                    if is_dept_head_self and is_dept_head and curr_grade in GRADE_OPTIONS:
                                        final_data["一级部门调整考核结果"] = curr_grade
                                        final_data["一级部门调整完毕"] = "是"
                                    ok, err = update_record_safely(APP_TOKEN, TABLE_ID, rid, final_data)
                                    if ok:
                                        submitted_cnt += 1
                                    else:
                                        err_msgs.append(f"{sub_name}: {err}")
                                if submitted_cnt > 0:
                                    st.success(f"✅ 已成功提交 {submitted_cnt} 人！")
                                    st.balloons()
                                    time.sleep(1.5)
                                    st.rerun()
                                if err_msgs:
                                    st.error("❌ 部分提交失败：" + "; ".join(err_msgs[:3]))
                                if submitted_cnt == 0 and not err_msgs:
                                    st.info("💡 当前暂无待提交的暂存评价。")
                    st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)
                    st.markdown("---")

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
                        is_direct = s_id in direct_record_ids  # 隔级下属仅可查看，不可评价/调整

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

                        # 状态与按钮类型：区分「未自评 / 暂存 / 已完成 / 待评价」；隔级下属仅可查看
                        is_draft = (not is_mgr_done) and (current_grade and current_grade not in ["", "未获取", "-"])
                        if not is_direct:
                            action_type = "view"
                            if not is_self_submitted:
                                status_html = "<span style='color:#FFA500;'>未自评</span><span style='font-size:11px;color:#888;margin-left:4px;'>(隔级)</span>"
                            elif is_mgr_done:
                                status_html = f"<span style='color:#00e676; font-weight:800;'>已完成</span><span style='font-size:11px;color:#888;margin-left:4px;'>(隔级)</span>"
                            elif is_draft:
                                status_html = f"<span style='color:#90A4AE;'>暂存</span><span style='font-size:11px;color:#888;margin-left:4px;'>(隔级)</span>"
                            else:
                                status_html = "<span style='color:#1E90FF;'>待评价</span><span style='font-size:11px;color:#888;margin-left:4px;'>(隔级)</span>"
                        elif not is_self_submitted:
                            status_html = "<span style='color:#FFA500;'>未自评</span>"
                            action_type = "remind"
                        elif is_mgr_done:
                            color = "#00e676"
                            status_html = f"<span style='color:{color}; font-weight:800;'>已完成{warning_icon}</span>"
                            action_type = "view"
                        elif is_draft:
                            # 已有考核等级但未提交，视为暂存（中性灰区分于未自评/待评价/已完成）
                            status_html = f"<span style='color:#90A4AE;'>暂存{warning_icon}</span>"
                            action_type = "adjust"
                        else:
                            status_html = "<span style='color:#1E90FF;'>待评价</span>"
                            action_type = "evaluate"

                        if _mgr_edit_disabled and action_type in ("adjust", "evaluate"):
                            action_type = "view"
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
                        # 仅隔级上级/一级部门负责人/分管高管打标，普通管理者不打标
                        _need_rel_badge = is_vp or is_dept_head or (len(my_all_subs) > len(my_direct_subs))
                        rel_badge = ("<span style='font-size:11px;color:#4CAF50;margin-left:4px;'>(直属)</span>" if is_direct else "<span style='font-size:11px;color:#888;margin-left:4px;'>(隔级)</span>") if _need_rel_badge else ""
                        c1, c2, c3, c4, c5, c6 = st.columns([2.2, 3.2, 1.2, 1.2, 1.2, 2.0], vertical_alignment="center")
                        c1.markdown(
                            f"<div class='sub-list-cell' style='color:#E0E0E0; white-space:normal;'><b>{s_name}</b>{rel_badge}<br>（{s_emp_id}）</div>",
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
                                    st.rerun()
                            elif action_type == "view":
                                if action_button("view", "查看", key=f"btn_view_{s_id}", help="在页面下端查看自评"):
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

                # --- 3. 管理者评估下属的具体表单 ---
                if is_evaluating_sub:
                    current_sub = next((s for s in my_all_subs if s["record_id"] == st.session_state.selected_subordinate_id), None)
                    if current_sub:
                        sub_f = current_sub.get("fields", {})
                        is_direct_sub = current_sub["record_id"] in direct_record_ids  # 隔级下属仅可查看，不可编辑
                        is_mgr_submitted = extract_text(sub_f.get("上级评价是否完成")).strip() == "是"  # 已确认提交则仅可查看
                        sub_id_str = current_sub["record_id"]
                        disp_emp_id = extract_text(sub_f.get("工号") or sub_f.get("员工工号"), "未知工号")
                        disp_name = extract_text(sub_f.get("姓名"), "未知姓名")
                        disp_job = extract_text(sub_f.get("岗位") or sub_f.get("职位"), "未分配")
                        disp_score = str(sub_f.get("自评得分", "暂无")) 
                        disp_grade = str(sub_f.get("自评等级", "暂无")) 
                        disp_last_perf = extract_text(sub_f.get("上一次绩效考核结果", "暂无"), "").strip() or "暂无"
                        
                        col_head1, col_head2 = st.columns([3, 1])
                        with col_head1:
                            st.markdown(f"<div class='module-title'>👤 被评价人：{disp_name}</div>", unsafe_allow_html=True)
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
                        if not is_direct_sub:
                            st.warning("⚠️ 您为隔级上级，仅可查看该下属信息，不可调整其绩效。")
                        if is_mgr_submitted:
                            st.success("🔒 该下属的上级评价已确认提交，仅可查看，不可修改。")
                        st.write("")
                        
                        st.markdown("---")
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

                            st.markdown(f"**🎯工作目标{i}及总结** <span style='font-size:14px; color:#888;'>(权重: {sub_weight}% | 自评: {raw_score}分)</span>", unsafe_allow_html=True)
                            st.text_area("隐藏标签", value=sub_obj_text, height=80, disabled=True, key=f"ui_sub_obj_{i}_{sub_id_str}", label_visibility="collapsed")
                            st.write("")
                            sub_weight_sum += sub_weight
                        
                        saved_work_score = sub_f.get("工作目标上级评分", 0.0)
                        try: work_idx = SCORE_OPTIONS.index(float(saved_work_score))
                        except: work_idx = 0
                        
                        st.info(f"💡 提示：该下属工作目标总权重为 **{sub_weight_sum}%**")
                        mgr_work_score = st.selectbox("🌟 工作目标整体上级评分", options=SCORE_OPTIONS, index=work_idx, key=f"mgr_work_score_{sub_id_str}", disabled=not is_direct_sub or is_mgr_submitted or _mgr_edit_disabled)
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
                        
                        mgr_comp_score = st.selectbox("🌟 通用能力上级评分", options=SCORE_OPTIONS, index=comp_idx, key=f"mgr_comp_score_{sub_id_str}", disabled=not is_direct_sub or is_mgr_submitted or _mgr_edit_disabled)
                        
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
                            
                            mgr_lead_score = st.selectbox("🌟 领导力上级评分", options=SCORE_OPTIONS, index=lead_idx, key=f"mgr_lead_score_{sub_id_str}", disabled=not is_direct_sub or is_mgr_submitted or _mgr_edit_disabled)
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
                        st.text_area("✍️ 考核评语", value=saved_comment, height=100, placeholder="请输入对该下属的整体评价...", key=f"mgr_comment_{sub_id_str}", disabled=not is_direct_sub or is_mgr_submitted or _mgr_edit_disabled)
                        
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
                        st.markdown("<div class='save-marker'></div>", unsafe_allow_html=True)
                        mgr_save_draft_disabled = not is_direct_sub or is_mgr_submitted or not step2_can_submit or _mgr_edit_disabled
                        if is_direct_sub and st.button("保存草稿", use_container_width=True, key=f"btn_mgr_save_draft_{sub_id_str}", disabled=mgr_save_draft_disabled):
                            with st.spinner("同步数据至飞书..."):
                                success, error_msg = update_record_safely(APP_TOKEN, TABLE_ID, st.session_state.selected_subordinate_id, sub_update_data)
                                if success:
                                    st.session_state.mgr_draft_saved = True
                                else:
                                    st.error(f"❌ 暂存失败：{error_msg}")
                        if st.session_state.pop("mgr_draft_saved", False):
                            st.success("💡 提示：已妥善保存。")
                        if mgr_save_draft_disabled and is_direct_sub and not is_mgr_submitted and not step2_can_submit:
                            st.caption("⚠️ 请先完成工作目标、通用能力、领导力（如有）的评分及考核评语后，方可保存草稿。")
                        st.info("💡提示：请点击「保存草稿」，全部评价之后统一点击提交即可。")
                    else:
                        st.error("未找到对应下属的数据，请返回重试。")
                        st.button("🔙 返回", on_click=return_to_self)
            else:
                st.info("💡 提示：当前暂无下属。")

        # ===== 一级部门负责人调整（一级导航） =====
        if idx_dept_head is not None:
            with tabs[idx_dept_head]:
                if _is_module_disabled("一级部门负责人调整"):
                    st.error("🔒 当前功能已关停，暂不可操作。请联系管理员。")
                elif not is_dept_head:
                    st.info("💡 提示：当前您不是任何员工的一级部门负责人，暂无可调整名单。")
                else:
                    _dept_edit_disabled = _is_module_edit_disabled("一级部门负责人调整")
                    if _dept_edit_disabled:
                        st.info("📌 当前阶段编辑已关闭，仅可查看。")
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

                    _ann_dh = _read_admin_announcement("一级部门负责人调整")
                    if _ann_dh:
                        _dh_ann_body = _ann_dh.replace("\n", "<br>")
                        st.markdown(f"""
                        <div style="margin: 0 0 16px 0; padding: 14px 16px; background: rgba(33, 150, 243, 0.12); border-radius: 8px; border-left: 4px solid #2196F3; font-size: 14px; line-height: 1.7; color: #E0E0E0;">
                            <div style="font-weight: 600; margin-bottom: 8px; color: #90CAF9;">📢 公告</div>
                            <div>{_dh_ann_body}</div>
                        </div>
                        """, unsafe_allow_html=True)
                    st.markdown("<div class='module-title'>📌 一级部门负责人调整进展</div>", unsafe_allow_html=True)

                    st.markdown(
                        f"""<div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;"><div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;"><span style="color:#b7bdc8;">覆盖人数（不含评价者本人）：<span style="color:#4CAFEE;">{total_cnt}</span> 人</span><span style="color:#b7bdc8;">调整人数：<span style="color:#8BC34A;">{modified_cnt}</span> 人</span></div></div>""",
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

                            # 已改 = 调整等级与上级评分不同（不论是否已确认，确认后仍可筛出）
                            is_modified = has_mgr_grade and adj_grade in GRADE_OPTIONS and adj_grade != mgr_grade

                            status = "未自评" if not self_submitted else ("已完成调整" if done_flag else ("已改" if is_modified else ("待调整" if has_mgr_grade else "待上级评分")))

                            if q1 and (q1 not in name.lower() and q1 not in emp.lower()):

                                continue

                            if q_dept != "全部部门" and (q2 not in dept_chain.lower()):

                                continue

                            if q_status != "全部状态":
                                if q_status == "已改":
                                    if not is_modified:
                                        continue
                                elif q_status != status:
                                    continue

                            if q_mgr_grade != "全部调整等级":

                                if q_mgr_grade == "-" and has_adj_grade:

                                    continue

                                if q_mgr_grade in GRADE_OPTIONS and adj_grade != q_mgr_grade:

                                    continue

                            filtered_dept_records.append(rec)

                        # 与上级评分一致：全部填写完才可提交，否则灰色；全部已提交后按钮禁用，只能提交一次
                        dept_has_any_pending = False
                        dept_can_confirm_all = True
                        for rec in dept_head_records:
                            ff = rec.get("fields", {})
                            r_id = rec.get("record_id")
                            mgr_g = extract_text(ff.get("考核结果", "-")).strip() or "-"
                            done_f = extract_text(ff.get("一级部门调整完毕", "")).strip() == "是"
                            if mgr_g not in GRADE_OPTIONS or done_f:
                                continue
                            dept_has_any_pending = True
                            adj_ex = extract_text(ff.get("一级部门调整考核结果", "")).strip()
                            def_grade = adj_ex if adj_ex in GRADE_OPTIONS else mgr_g
                            sel_grade = st.session_state.get(f"dept_adj_grade_{r_id}", def_grade)
                            if sel_grade not in GRADE_OPTIONS:
                                dept_can_confirm_all = False
                                break
                        dept_btn_disabled = not dept_has_any_pending or not dept_can_confirm_all or _dept_edit_disabled

                        st.info("💡 提示：默认为前序调整结果。全部调整完毕请点击「确认本次调整」按钮。提交后不可再修改。")

                        st.markdown("<div class='dept-confirm-marker'></div>", unsafe_allow_html=True)

                        if st.button("确认本次调整", type="primary", key="btn_dept_confirm_all", use_container_width=True, disabled=dept_btn_disabled):

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

                                    done_f_all = extract_text(ff.get("一级部门调整完毕", "")).strip() == "是"

                                    if done_f_all:

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

                            # 直属/隔级：与上级评分一致
                            rec_manager = extract_text(f.get("直接评价人") or f.get("评价人"))
                            rec_skip_level = extract_text(f.get("隔级上级"), "").strip()
                            is_direct = user_name and user_name in rec_manager
                            is_skip_level = user_name and user_name in rec_skip_level and not is_direct
                            rel_badge = "<span style='font-size:11px;color:#4CAF50;margin-left:4px;'>(直属)</span>" if is_direct else ("<span style='font-size:11px;color:#888;margin-left:4px;'>(隔级)</span>" if is_skip_level else "")

                            self_grade = extract_text(f.get("自评等级", "-")).strip() or "-"

                            mgr_grade = extract_text(f.get("考核结果", "-")).strip() or "-"

                            dept_chain = build_dept_chain(f)

                            # 一级部门负责人：默认取上级评分结果；若已有调整结果则展示当前调整值

                            adj_grade_field = extract_text(f.get("一级部门调整考核结果", "")).strip()

                            adj_grade_default = adj_grade_field if adj_grade_field in GRADE_OPTIONS else (mgr_grade if mgr_grade in GRADE_OPTIONS else "-")

                            c1, c2, c3, c4, c5 = st.columns([1.8, 3.6, 1.3, 1.3, 1.6], gap="small", vertical_alignment="center")

                            c1.markdown(

                                f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#E0E0E0; white-space:normal; text-align:center;'><b>{name}</b>{rel_badge}<br>（{emp}）</div>",

                                unsafe_allow_html=True,

                            )

                            c2.markdown(

                                f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#b0b0b0; white-space:normal; text-align:center;' title='{dept_chain} | {job}'>{dept_chain}<br>{job}</div>",

                                unsafe_allow_html=True,

                            )

                            with c3:
                                _gc3a, _gc3b = st.columns([2, 1], vertical_alignment="center")
                                with _gc3a:
                                    st.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center; white-space:nowrap;'>{self_grade}</div>", unsafe_allow_html=True)
                                with _gc3b:
                                    if st.button("🔗", key=f"dept_view_self_{r_id}", help="在页面下端查看自评"):
                                        st.session_state.dept_view_self_record_id = r_id
                                        st.rerun()

                            c4.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{mgr_grade}</div>", unsafe_allow_html=True)

                            # 调整等级下拉：与自评/上级评价一致，确认后不可修改，但已保存的调整等级必须展示

                            done_flag = extract_text(f.get("一级部门调整完毕", "")).strip() == "是"

                            disable_adjust = mgr_grade not in GRADE_OPTIONS or done_flag or _dept_edit_disabled

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

                    # 一级部门负责人：查看自评面板（仅在本 tab 激活且有选择时渲染，避免影响分管高管 tab）
                    if current_tab == "📌 一级部门负责人调整" and is_dept_head:
                        _dept_view_id = st.session_state.get("dept_view_self_record_id")
                        if _dept_view_id:
                            st.markdown(
                                "<div class='dept-view-hint-2s' style='margin:8px 0 12px 0; padding:8px 12px; background:rgba(33,150,243,0.15); border-radius:6px; font-size:13px; color:#90CAF9;'>💡 提示：请滚动鼠标，在人员列表下方查看或评价下属哦~</div>",
                                unsafe_allow_html=True,
                            )
                            _view_rec = next((r for r in dept_head_records if r.get("record_id") == _dept_view_id), None)
                            if _view_rec:
                                _vf = _view_rec.get("fields", {})
                                _vname = extract_text(_vf.get("姓名"), "未知").strip()
                                _vemp = extract_text(_vf.get("工号") or _vf.get("员工工号"), "").strip()
                                _vjob = extract_text(_vf.get("岗位") or _vf.get("职位"), "未分配").strip()
                                _vscore = str(_vf.get("自评得分", "暂无"))
                                _vgrade = str(_vf.get("自评等级", "暂无"))
                                _col1, _col2 = st.columns([3, 1])
                                with _col1:
                                    st.markdown(f"<div class='module-title'>👤 查看自评：{_vname}</div>", unsafe_allow_html=True)
                                    st.markdown(
                                        f"""<div style='font-size:14px; color:#E0E0E0; margin-top:6px; line-height:1.6;'>
                                            <div>工号：{_vemp}</div>
                                            <div>岗位：{_vjob}</div>
                                            <div>自评得分：{_vscore}</div>
                                            <div>自评等级：{_vgrade}</div>
                                        </div>""",
                                        unsafe_allow_html=True,
                                    )
                                with _col2:
                                    if st.button("收起面板", key="dept_collapse_self_view"):
                                        st.session_state.pop("dept_view_self_record_id", None)
                                        st.rerun()
                                st.markdown("---")
                                st.markdown("<div class='module-title'>💼 工作模块</div>", unsafe_allow_html=True)
                                _gcnt = 3
                                for _gi in range(5, 3, -1):
                                    if _vf.get(f"工作目标{_gi}及总结") or _vf.get(f"工作目标{_gi}权重", 0):
                                        _gcnt = _gi
                                        break
                                for _gi in range(1, _gcnt + 1):
                                    _obj = extract_text(_vf.get(f"工作目标{_gi}及总结"), "未填写").strip() or "未填写"
                                    _w = _vf.get(f"工作目标{_gi}权重", 0)
                                    _sw = int(float(_w)) if _w is not None else 0
                                    _sc = _vf.get(f"工作目标{_gi}自评得分", 0.0)
                                    _sc_str = str(_sc) if _sc is not None else "-"
                                    st.markdown(f"**🎯 工作目标{_gi}及总结** <span style='font-size:14px; color:#888;'>(权重: {_sw}% | 自评: {_sc_str}分)</span>", unsafe_allow_html=True)
                                    st.text_area("_", value=_obj, height=80, disabled=True, key=f"dept_view_obj_{_gi}_{_dept_view_id}", label_visibility="collapsed")
                                st.markdown("<div class='module-title'>🧠 通用能力模块</div>", unsafe_allow_html=True)
                                _comp = extract_text(_vf.get("通用能力总结"), "未填写").strip() or "未填写"
                                _comp_sc = _vf.get("通用能力自评得分", 0.0)
                                _comp_str = str(_comp_sc) if _comp_sc is not None else "-"
                                st.markdown(f"**🧠 通用能力总结** <span style='font-size:14px; color:#888;'>(自评: {_comp_str}分)</span>", unsafe_allow_html=True)
                                st.text_area("_", value=_comp, height=100, disabled=True, key=f"dept_view_comp_{_dept_view_id}", label_visibility="collapsed")
                                _vrole = extract_text(_vf.get("角色", "")).strip()
                                if _vrole == "管理者":
                                    st.markdown("<div class='module-title'>👑 领导力模块</div>", unsafe_allow_html=True)
                                    _lead = extract_text(_vf.get("领导力总结"), "未填写").strip() or "未填写"
                                    _lead_sc = _vf.get("领导力自评得分", 0.0)
                                    _lead_str = str(_lead_sc) if _lead_sc is not None else "-"
                                    st.markdown(f"**👑 领导力总结** <span style='font-size:14px; color:#888;'>(自评: {_lead_str}分)</span>", unsafe_allow_html=True)
                                    st.text_area("_", value=_lead, height=100, disabled=True, key=f"dept_view_lead_{_dept_view_id}", label_visibility="collapsed")
                                st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)

        # ===== 分管高管调整（一级导航） =====
        if idx_vp is not None:
            with tabs[idx_vp]:
                if _is_module_disabled("分管高管调整"):
                    st.error("🔒 当前功能已关停，暂不可操作。请联系管理员。")
                else:
                    _vp_edit_disabled = _is_module_edit_disabled("分管高管调整")
                    if _vp_edit_disabled:
                        st.info("📌 当前阶段编辑已关闭，仅可查看。")
                    all_records = all_records_snapshot or fetch_all_records_safely(APP_TOKEN, TABLE_ID)
                    vp_records = []
                    for rec in all_records:
                        f = rec.get("fields", {})
                        vp_str = extract_text(f.get("分管高管") or f.get("高管"), "").strip()
                        emp_name = extract_text(f.get("姓名"), "").strip()
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
                    _ann_vp = _read_admin_announcement("分管高管调整")
                    if _ann_vp:
                        _vp_ann_body = _ann_vp.replace("\n", "<br>")
                        st.markdown(f"""
                        <div style="margin: 0 0 16px 0; padding: 14px 16px; background: rgba(33, 150, 243, 0.12); border-radius: 8px; border-left: 4px solid #2196F3; font-size: 14px; line-height: 1.7; color: #E0E0E0;">
                            <div style="font-weight: 600; margin-bottom: 8px; color: #90CAF9;">📢 公告</div>
                            <div>{_vp_ann_body}</div>
                        </div>
                        """, unsafe_allow_html=True)
                    st.markdown("<div class='module-title'>📌 分管高管调整进展</div>", unsafe_allow_html=True)
                    st.markdown(
                    f"""<div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;"><div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;"><span style="color:#b7bdc8;">覆盖人数（不含评价者本人）：<span style="color:#4CAFEE;">{total_cnt}</span> 人</span><span style="color:#b7bdc8;">调整人数：<span style="color:#8BC34A;">{modified_cnt}</span> 人</span></div></div>""",
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

                        # 已改 = 调整等级Ⅱ与调整等级Ⅰ不同（不论是否已确认，确认后仍可筛出）
                        is_modified = (vp_base_grade in GRADE_OPTIONS and adj_grade in GRADE_OPTIONS and adj_grade != vp_base_grade)

                        status = "未自评" if not self_submitted else ("已完成调整" if done_flag else ("已改" if is_modified else ("待调整" if has_mgr_grade else "待上级评分")))

                        if q1 and (q1 not in name.lower() and q1 not in emp.lower()):

                            continue

                        if q_dept != "全部部门" and (q2 not in dept_l1.lower()):

                            continue

                        if q_status != "全部状态":
                            if q_status == "已改":
                                if not is_modified:
                                    continue
                            elif q_status != status:
                                continue

                        if q_mgr_grade != "全部调整等级":

                            if q_mgr_grade == "-" and has_adj_grade:

                                continue

                            if q_mgr_grade in GRADE_OPTIONS and adj_grade != q_mgr_grade:

                                continue

                        filtered_vp_records.append(rec)

                    # 与上级评分一致：全部填写完才可提交，否则灰色；全部已提交后按钮禁用，只能提交一次
                    vp_has_any_pending = False
                    vp_can_confirm_all = True
                    for rec in vp_records:
                        ff = rec.get("fields", {})
                        r_id = rec.get("record_id")
                        dept_done = extract_text(ff.get("一级部门调整完毕", "")).strip() == "是"
                        vp_done = extract_text(ff.get("分管高管调整完毕", "")).strip() == "是"
                        if not dept_done or vp_done:
                            continue
                        vp_has_any_pending = True
                        adj_ex = extract_text(ff.get("分管高管调整考核结果", "")).strip()
                        dept_adj = extract_text(ff.get("一级部门调整考核结果", "")).strip()
                        mgr_g = extract_text(ff.get("考核结果", "-")).strip() or "-"
                        dept_adj = dept_adj if dept_adj in GRADE_OPTIONS else mgr_g
                        def_grade = adj_ex if adj_ex in GRADE_OPTIONS else (dept_adj if dept_adj in GRADE_OPTIONS else mgr_g)
                        sel_grade = st.session_state.get(f"vp_adj_grade_{r_id}", def_grade)
                        if sel_grade not in GRADE_OPTIONS:
                            vp_can_confirm_all = False
                            break
                    vp_btn_disabled = not vp_has_any_pending or not vp_can_confirm_all or _vp_edit_disabled

                    st.info("💡 提示：默认为前序调整结果。全部调整完毕请点击「确认本次调整」按钮。提交后不可再修改。")

                    st.markdown("<div class='vp-confirm-marker'></div>", unsafe_allow_html=True)

                    if st.button("确认本次调整", type="primary", key="btn_vp_confirm_all", use_container_width=True, disabled=vp_btn_disabled):

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

                                vp_done_all = extract_text(ff.get("分管高管调整完毕", "")).strip() == "是"

                                if vp_done_all:

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

                        # 直属/隔级：与上级评分一致
                        rec_manager = extract_text(f.get("直接评价人") or f.get("评价人"))
                        rec_skip_level = extract_text(f.get("隔级上级"), "").strip()
                        is_direct = user_name and user_name in rec_manager
                        is_skip_level = user_name and user_name in rec_skip_level and not is_direct
                        rel_badge = "<span style='font-size:11px;color:#4CAF50;margin-left:4px;'>(直属)</span>" if is_direct else ("<span style='font-size:11px;color:#888;margin-left:4px;'>(隔级)</span>" if is_skip_level else "")

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

                            f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#E0E0E0; white-space:normal; text-align:center;'><b>{name}</b>{rel_badge}<br>（{emp}）</div>",

                            unsafe_allow_html=True,

                        )

                        c2.markdown(

                            f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#b0b0b0; white-space:normal; text-align:center;' title='{dept_l1} | {job}'>{dept_l1}<br>{job}</div>",

                            unsafe_allow_html=True,

                        )

                        with c3:
                            _vp_gc3a, _vp_gc3b = st.columns([2, 1], vertical_alignment="center")
                            with _vp_gc3a:
                                st.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center; white-space:nowrap;'>{self_grade}</div>", unsafe_allow_html=True)
                            with _vp_gc3b:
                                if st.button("🔗", key=f"vp_view_self_{r_id}", help="在页面下端查看自评"):
                                    st.session_state.vp_view_self_record_id = r_id
                                    st.rerun()

                        c4.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{mgr_grade_display}</div>", unsafe_allow_html=True)

                        c5.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{adj1_display}</div>", unsafe_allow_html=True)

                        disable_adjust = (not dept_done) or vp_done or _vp_edit_disabled

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

                    # 分管高管：查看自评面板（仅在本 tab 激活且有选择时渲染）
                    if current_tab == "📌 分管高管调整":
                        _vp_view_id = st.session_state.get("vp_view_self_record_id")
                        if _vp_view_id:
                            st.markdown(
                                "<div class='dept-view-hint-2s' style='margin:8px 0 12px 0; padding:8px 12px; background:rgba(33,150,243,0.15); border-radius:6px; font-size:13px; color:#90CAF9;'>💡 提示：请滚动鼠标，在人员列表下方查看或评价下属哦~</div>",
                                unsafe_allow_html=True,
                            )
                            _vp_view_rec = next((r for r in vp_records if r.get("record_id") == _vp_view_id), None)
                            if _vp_view_rec:
                                _vf = _vp_view_rec.get("fields", {})
                                _vname = extract_text(_vf.get("姓名"), "未知").strip()
                                _vemp = extract_text(_vf.get("工号") or _vf.get("员工工号"), "").strip()
                                _vjob = extract_text(_vf.get("岗位") or _vf.get("职位"), "未分配").strip()
                                _vscore = str(_vf.get("自评得分", "暂无"))
                                _vgrade = str(_vf.get("自评等级", "暂无"))
                                _col1, _col2 = st.columns([3, 1])
                                with _col1:
                                    st.markdown(f"<div class='module-title'>👤 查看自评：{_vname}</div>", unsafe_allow_html=True)
                                    st.markdown(
                                        f"""<div style='font-size:14px; color:#E0E0E0; margin-top:6px; line-height:1.6;'>
                                            <div>工号：{_vemp}</div>
                                            <div>岗位：{_vjob}</div>
                                            <div>自评得分：{_vscore}</div>
                                            <div>自评等级：{_vgrade}</div>
                                        </div>""",
                                        unsafe_allow_html=True,
                                    )
                                with _col2:
                                    if st.button("收起面板", key="vp_collapse_self_view"):
                                        st.session_state.pop("vp_view_self_record_id", None)
                                        st.rerun()
                                st.markdown("---")
                                st.markdown("<div class='module-title'>💼 工作模块</div>", unsafe_allow_html=True)
                                _gcnt = 3
                                for _gi in range(5, 3, -1):
                                    if _vf.get(f"工作目标{_gi}及总结") or _vf.get(f"工作目标{_gi}权重", 0):
                                        _gcnt = _gi
                                        break
                                for _gi in range(1, _gcnt + 1):
                                    _obj = extract_text(_vf.get(f"工作目标{_gi}及总结"), "未填写").strip() or "未填写"
                                    _w = _vf.get(f"工作目标{_gi}权重", 0)
                                    _sw = int(float(_w)) if _w is not None else 0
                                    _sc = _vf.get(f"工作目标{_gi}自评得分", 0.0)
                                    _sc_str = str(_sc) if _sc is not None else "-"
                                    st.markdown(f"**🎯 工作目标{_gi}及总结** <span style='font-size:14px; color:#888;'>(权重: {_sw}% | 自评: {_sc_str}分)</span>", unsafe_allow_html=True)
                                    st.text_area("_", value=_obj, height=80, disabled=True, key=f"vp_view_obj_{_gi}_{_vp_view_id}", label_visibility="collapsed")
                                st.markdown("<div class='module-title'>🧠 通用能力模块</div>", unsafe_allow_html=True)
                                _comp = extract_text(_vf.get("通用能力总结"), "未填写").strip() or "未填写"
                                _comp_sc = _vf.get("通用能力自评得分", 0.0)
                                _comp_str = str(_comp_sc) if _comp_sc is not None else "-"
                                st.markdown(f"**🧠 通用能力总结** <span style='font-size:14px; color:#888;'>(自评: {_comp_str}分)</span>", unsafe_allow_html=True)
                                st.text_area("_", value=_comp, height=100, disabled=True, key=f"vp_view_comp_{_vp_view_id}", label_visibility="collapsed")
                                _vrole = extract_text(_vf.get("角色", "")).strip()
                                if _vrole == "管理者":
                                    st.markdown("<div class='module-title'>👑 领导力模块</div>", unsafe_allow_html=True)
                                    _lead = extract_text(_vf.get("领导力总结"), "未填写").strip() or "未填写"
                                    _lead_sc = _vf.get("领导力自评得分", 0.0)
                                    _lead_str = str(_lead_sc) if _lead_sc is not None else "-"
                                    st.markdown(f"**👑 领导力总结** <span style='font-size:14px; color:#888;'>(自评: {_lead_str}分)</span>", unsafe_allow_html=True)
                                    st.text_area("_", value=_lead, height=100, disabled=True, key=f"vp_view_lead_{_vp_view_id}", label_visibility="collapsed")
                                st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)

        if idx_reports is not None:
            with tabs[idx_reports]:
                if _is_module_disabled("视图与报表"):
                    st.error("🔒 当前功能已关停，暂不可操作。请联系管理员。")
                else:
                    report_records_all = all_records_snapshot or fetch_all_records_safely(APP_TOKEN, TABLE_ID)
                    if not report_records_all:
                        st.info("💡 提示：暂无可用于报表展示的数据。")
                    else:
                        # 固定报表范围与周期：不再显示“视图范围/考核周期”控件
                        scope_mode = "我的团队"
                        # 过滤范围：视图与调整权限保持一致
                        # - 分管高管：看到名下所有员工（按「分管高管 / 高管」字段，排除本人）
                        # - 一级部门负责人：看到本部门所有员工（按「一级部门负责人 / 部门负责人」字段，排除本人）
                        # - 普通管理者/隔级上级：直属+隔级下属，即「直接评价人」或「隔级上级」包含当前用户
                        report_scope = "vp" if is_vp else ("dept_head" if is_dept_head else "mgr")
                        report_scoped = []
                        for rec in report_records_all:
                            rf = rec.get("fields", {})
                            emp_name = extract_text(rf.get("姓名"), "").strip()
                            if scope_mode == "全公司":
                                # 仅分管高管可切全公司，各种统计均不包含分管高管本人
                                if emp_name != user_name:
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
                                    rec_skip_level = extract_text(rf.get("隔级上级"), "").strip()
                                    is_direct = user_name and user_name in rec_manager
                                    is_skip_level = user_name and user_name in rec_skip_level and not is_direct
                                    if (is_direct or is_skip_level) and emp_name != user_name:
                                        report_scoped.append(rec)
                        # 周期筛选；周期统一为展示格式（2026上半年→2026年上半年）
                        def pick_cycle(ff):
                            for k in ["绩效考核周期", "考核周期", "本次绩效考核周期", "本次考核周期"]:
                                v = extract_text(ff.get(k), "").strip()
                                if v:
                                    return _normalize_cycle_display(v) or v
                            return current_cycle
                        # 报表周期固定跟随员工信息周期；周期匹配兼容 2026上半年=2026年上半年
                        selected_cycle = current_cycle
                        report_records = []
                        for rec in report_scoped:
                            cyc = pick_cycle(rec.get("fields", {}))
                            if _cycles_match(cyc, selected_cycle):
                                report_records.append(rec)
                        total_cnt = len(report_records)
                        if total_cnt == 0:
                            st.info("💡 提示：当前筛选条件下暂无数据。")
                        else:
                            # 聚合统计
                            target_set_cnt = 0
                            self_done_cnt = 0
                            mgr_done_cnt = 0
                            dept_done_cnt = 0
                            public_done_cnt = 0
                            grade_counts = Counter()
                            dept_stats = {}
                            member_cards = []
                            step_people = {"target_set": [], "self_done": [], "mgr_done": [], "dept_done": [], "vp_done": []}
                            for rec in report_records:
                                f = rec.get("fields", {})
                                name = extract_text(f.get("姓名"), "未知姓名").strip()
                                emp = extract_text(f.get("工号") or f.get("员工工号"), "未知工号").strip()
                                job = extract_text(f.get("岗位") or f.get("职位"), "未分配").strip()
                                # 报表部门口径：分管高管按一级部门；一级部门负责人按二级部门；其他按二级部门
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

                                # 仅此处兼容飞书 checkbox 返回 true/false
                                _v = lambda k: extract_text(f.get(k), "").strip() == "是" or f.get(k) is True
                                self_done = _v("自评是否提交")
                                mgr_done = _v("上级评价是否完成")
                                dept_done = _v("一级部门调整完毕")
                                public_done = _v("分管高管调整完毕")
                                if self_done:
                                    self_done_cnt += 1
                                if mgr_done:
                                    mgr_done_cnt += 1
                                if dept_done:
                                    dept_done_cnt += 1
                                if public_done:
                                    public_done_cnt += 1

                                vp_adj = extract_text(f.get("分管高管调整考核结果"), "").strip()
                                dept_adj = extract_text(f.get("一级部门调整考核结果"), "").strip()
                                mgr_grade = extract_text(f.get("考核结果"), "").strip()
                                final_from_field = extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
                                final_grade = "-"
                                for cand in [vp_adj, dept_adj, mgr_grade, final_from_field]:
                                    if cand in GRADE_OPTIONS:
                                        final_grade = cand
                                        break
                                if final_grade in GRADE_OPTIONS:
                                    grade_counts[final_grade] += 1

                                _base = {"total": 0, "done": 0, "grades": {g: 0 for g in GRADE_OPTIONS}}
                                dept_info = dept_stats.setdefault(dept, {
                                    **_base,
                                    "sales_total": 0, "sales_done": 0, "sales_grades": {g: 0 for g in GRADE_OPTIONS},
                                    "non_sales_total": 0, "non_sales_done": 0, "non_sales_grades": {g: 0 for g in GRADE_OPTIONS},
                                })
                                dept_info["total"] += 1
                                if mgr_done:
                                    dept_info["done"] += 1
                                if final_grade in GRADE_OPTIONS:
                                    dept_info["grades"][final_grade] += 1
                                is_sales = extract_text(f.get("是否绩效关联奖金"), "").strip() == "否"
                                if is_sales:
                                    dept_info["sales_total"] += 1
                                    if mgr_done:
                                        dept_info["sales_done"] += 1
                                    if final_grade in GRADE_OPTIONS:
                                        dept_info["sales_grades"][final_grade] += 1
                                else:
                                    dept_info["non_sales_total"] += 1
                                    if mgr_done:
                                        dept_info["non_sales_done"] += 1
                                    if final_grade in GRADE_OPTIONS:
                                        dept_info["non_sales_grades"][final_grade] += 1

                                # 分管高管调整完毕=是 → 分管高管已调整；一级部门调整完毕=是 → 一级部门已调整
                                if public_done:
                                    status_txt = "分管高管已调整"
                                elif dept_done:
                                    status_txt = "一级部门已调整"
                                elif mgr_done:
                                    status_txt = "上级已评"
                                elif self_done:
                                    status_txt = "自评已交"
                                elif has_target:
                                    status_txt = "目标设定中"
                                else:
                                    status_txt = "待启动"
                                person_info = {"name": name, "emp_id": emp, "dept": dept}
                                if has_target:
                                    step_people["target_set"].append(person_info)
                                if self_done:
                                    step_people["self_done"].append(person_info)
                                if mgr_done:
                                    step_people["mgr_done"].append(person_info)
                                if dept_done:
                                    step_people["dept_done"].append(person_info)
                                if public_done:
                                    step_people["vp_done"].append(person_info)
                                member_cards.append({
                                    "name": name,
                                    "emp": emp,
                                    "job": job,
                                    "dept": dept,
                                    "grade": final_grade,
                                    "status": status_txt,
                                    "self_done": self_done,
                                    "mgr_done": mgr_done,
                                    "dept_done": dept_done,
                                    "vp_done": public_done,
                                })

                        # 🏟️ 团队概览（所有管理者视图一致，仅包含等级分布+进度统计）
                        if not (is_dept_head or is_vp):
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
                            _exec_detail, _dept_detail = _build_detail_stats(report_records, extract_text_fn=extract_text, dept_key="二级部门")
                            dept_rows = []
                            _mgr_exec_vps = sorted({vp.strip() for r in report_records if _is_executive(r) for vp in extract_text(r.get("fields", {}).get("分管高管") or r.get("fields", {}).get("高管"), "").strip().replace("，", ",").split(",") if vp.strip()})
                            _mgr_exec_label = f"高管（{_mgr_exec_vps[0]}）" if len(_mgr_exec_vps) == 1 else (f"高管（{'、'.join(_mgr_exec_vps)}）" if _mgr_exec_vps else "高管")

                            def _mgr_row(dept, scope, t, d, g):
                                rv = round(d / t * 100, 1) if t else 0
                                return {"部门": dept, "口径": scope, "总人数": t, "已完成": d, "完成率": "100%" if rv == 100 else f"{rv}%", "S级": g["S"], "A级": g["A"], "B+级": g["B+"], "B级": g["B"], "B-级": g["B-"], "C级": g["C"]}
                            if _exec_detail["total"] > 0:
                                dept_rows.append(_mgr_row(_mgr_exec_label, "总", _exec_detail["total"], _exec_detail["done"], _exec_detail["grades"]))
                                if (_exec_detail.get("sales_total") or 0) > 0 and (_exec_detail.get("non_sales_total") or 0) > 0:
                                    dept_rows.append(_mgr_row(_mgr_exec_label, "销售", _exec_detail["sales_total"], _exec_detail["sales_done"], _exec_detail.get("sales_grades", _exec_detail["grades"])))
                                    dept_rows.append(_mgr_row(_mgr_exec_label, "非销售", _exec_detail["non_sales_total"], _exec_detail["non_sales_done"], _exec_detail.get("non_sales_grades", _exec_detail["grades"])))
                            for dept_name, dval in sorted(_dept_detail.items(), key=lambda x: x[0]):
                                dept_rows.append(_mgr_row(dept_name, "总", dval["total"], dval["done"], dval["grades"]))
                                if (dval.get("sales_total") or 0) > 0 and (dval.get("non_sales_total") or 0) > 0:
                                    dept_rows.append(_mgr_row(dept_name, "销售", dval["sales_total"], dval["sales_done"], dval.get("sales_grades", dval["grades"])))
                                    dept_rows.append(_mgr_row(dept_name, "非销售", dval["non_sales_total"], dval["non_sales_done"], dval.get("non_sales_grades", dval["grades"])))
                            dept_df = pd.DataFrame(dept_rows)
                            if not dept_df.empty:
                                def _dept_row_style(row):
                                    scope, dept = row.get("口径", ""), str(row.get("部门", ""))
                                    if dept.startswith("高管"):
                                        if scope == "总":
                                            return ["font-weight: 700; font-size: 14px; background-color: rgba(255,193,7,0.12); color: #FFC107;"] * len(row)
                                        if scope in ("销售", "非销售"):
                                            return ["font-size: 12px; background-color: rgba(255,193,7,0.06);"] * len(row)
                                    if scope == "总":
                                        return ["font-weight: 700; font-size: 14px; background-color: rgba(255,255,255,0.06);"] * len(row)
                                    if scope == "销售":
                                        return ["font-size: 12px; color: #81C784; background-color: rgba(129,199,132,0.08);"] * len(row)
                                    if scope == "非销售":
                                        return ["font-size: 12px; color: #64B5F6; background-color: rgba(100,181,246,0.08);"] * len(row)
                                    return [""] * len(row)
                                dept_df = dept_df.style.set_properties(**{"text-align": "center"}).apply(_dept_row_style, axis=1)
                            st.dataframe(dept_df, use_container_width=True, hide_index=True)
                        else:
                            # 有调整权限的管理者：一级部门负责人/分管高管
                            st.markdown("<div class='module-title'>📊 绩效概览</div>", unsafe_allow_html=True)
                            drill_mode = st.session_state.get("report_member_kpi_drill", "all")
                            # 分管高管、一级部门负责人：统一 3 卡片（考核总人数、管辖部门数、直属下级数）
                            kpi_dept_cnt = len(dept_stats)  # VP 为一级部门，部门负责人为二级部门
                            kpi_dept_label = "管辖一级部门数" if is_vp else "管辖二级部门数"
                            kpi_direct_cnt = sum(1 for r in report_records if user_name and user_name in extract_text(r.get("fields", {}).get("直接评价人") or r.get("fields", {}).get("评价人"), "").strip())
                            _vp_kpi_html = f"""
                            <div style="display:flex;justify-content:space-between;flex-wrap:nowrap;gap:12px;margin-top:12px;overflow-x:auto;">
                                <div style="flex:1;min-width:130px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);">
                                    <div class="report-kpi-label" style="white-space:nowrap;text-align:center;">考核总人数</div>
                                    <div style="font-size:28px;font-weight:700;color:#42A5F5;margin-top:8px;">{total_cnt}</div>
                                </div>
                                <div style="flex:1;min-width:130px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);">
                                    <div class="report-kpi-label" style="white-space:nowrap;text-align:center;">{kpi_dept_label}</div>
                                    <div style="font-size:28px;font-weight:700;color:#26A69A;margin-top:8px;">{kpi_dept_cnt}</div>
                                </div>
                                <div style="flex:1;min-width:130px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.08);">
                                    <div class="report-kpi-label" style="white-space:nowrap;text-align:center;">直属下级数</div>
                                    <div style="font-size:28px;font-weight:700;color:#FFA726;margin-top:8px;">{kpi_direct_cnt}</div>
                                </div>
                            </div>
                            """
                            st.markdown(_vp_kpi_html, unsafe_allow_html=True)
                            st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
                            # 图二计算逻辑：是否绩效关联奖金=否→销售，=是→非销售；人员基数=该口径人数，比例向下取整
                            report_sales = [r for r in report_records if extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "否"]
                            report_non_sales = [r for r in report_records if extract_text(r.get("fields", {}).get("是否绩效关联奖金"), "").strip() == "是"]
                            has_bonus_no = len(report_sales) > 0 and len(report_non_sales) > 0
                            # 默认：无筛选时用销售口径；有筛选时由用户选择
                            report_scope = report_sales if report_sales else report_records
                            if is_vp and has_bonus_no:
                                if "report_bonus_scope_filter" not in st.session_state:
                                    st.session_state.report_bonus_scope_filter = "全部"
                                st.selectbox(
                                    "销售/非销售筛选",
                                    options=["全部", "销售", "非销售"],
                                    key="report_bonus_scope_filter",
                                    label_visibility="visible",
                                )
                                # 上下表统一使用同一筛选口径：全部=全部人数，销售=否，非销售=是
                                _scope_key = st.session_state.report_bonus_scope_filter
                                if _scope_key == "全部":
                                    report_scope = report_records
                                elif _scope_key == "销售":
                                    report_scope = report_sales
                                else:
                                    report_scope = report_non_sales
                            base_cnt = len(report_scope) if report_scope else total_cnt
                            grade_counts_bonus = Counter()
                            # 分管高管：预计算各部门的上限/实际人数；有筛选时分别构建全部/销售/非销售三套，确保「全部分管范围」与单部门视图一致
                            dept_grade_stats = {}  # dept -> {base_cnt, grade_counts, sa_theory, bp_theory, ...}
                            _dept_stats_by_scope = {}  # "全部"|"销售"|"非销售" -> dept_grade_stats（仅 has_bonus_no 时填充）
                            def _build_dept_grade_stats(recs, use_total_as_base=False):
                                """构建部门统计。use_total_as_base=True 时（全部口径）base_cnt=total_cnt；否则按绩效关联奖金口径。"""
                                dgs = {}
                                for rec in (recs or []):
                                    rf = rec.get("fields", {})
                                    dept_l1 = _clean_dept_name(rf.get("一级部门")) or "未分配部门"
                                    vp_adj = extract_text(rf.get("分管高管调整考核结果"), "").strip()
                                    dept_adj = extract_text(rf.get("一级部门调整考核结果"), "").strip()
                                    mgr_grade = extract_text(rf.get("考核结果"), "").strip()
                                    final_grade = extract_text(rf.get("最终绩效结果") or rf.get("最终考核结果"), "").strip()
                                    fg = "-"
                                    for cand in [vp_adj, dept_adj, mgr_grade, final_grade]:
                                        if cand in GRADE_OPTIONS:
                                            fg = cand
                                            break
                                    dg = dgs.setdefault(dept_l1, {"grade_counts": Counter(), "bonus_cnt": 0, "total_cnt": 0})
                                    dg["total_cnt"] += 1
                                    if extract_text(rf.get("是否绩效关联奖金"), "").strip() == "是":
                                        dg["bonus_cnt"] += 1
                                    if fg in GRADE_OPTIONS:
                                        dg["grade_counts"][fg] += 1
                                for dept_name, dg in dgs.items():
                                    # 全部口径：base_cnt=总人数；销售/非销售口径：recs 已过滤，total_cnt 即该口径人数
                                    dg["base_cnt"] = dg["total_cnt"] if use_total_as_base else (dg["bonus_cnt"] if dg["bonus_cnt"] > 0 else dg["total_cnt"])
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
                                return dgs
                            if is_vp and has_bonus_no:
                                # 分管范围总数已排除 VP 本人；各部门排除一级部门负责人
                                _vp_excl_dept_heads = [r for r in report_records if not _is_dept_head(r)]
                                _vp_sales_excl = [r for r in report_sales if not _is_dept_head(r)]
                                _vp_non_sales_excl = [r for r in report_non_sales if not _is_dept_head(r)]
                                _dept_stats_by_scope["全部"] = _build_dept_grade_stats(_vp_excl_dept_heads, use_total_as_base=True)
                                _dept_stats_by_scope["销售"] = _build_dept_grade_stats(_vp_sales_excl)
                                _dept_stats_by_scope["非销售"] = _build_dept_grade_stats(_vp_non_sales_excl)
                                dept_grade_stats = _dept_stats_by_scope.get(st.session_state.get("report_bonus_scope_filter", "全部"), _dept_stats_by_scope["全部"])
                            for rec in (report_scope if report_scope else report_records):
                                f = rec.get("fields", {})
                                vp_adj = extract_text(f.get("分管高管调整考核结果"), "").strip()
                                dept_adj = extract_text(f.get("一级部门调整考核结果"), "").strip()
                                mgr_grade = extract_text(f.get("考核结果"), "").strip()
                                final_from_field = extract_text(f.get("最终绩效结果") or f.get("最终考核结果"), "").strip()
                                final_grade = "-"
                                for cand in [vp_adj, dept_adj, mgr_grade, final_from_field]:
                                    if cand in GRADE_OPTIONS:
                                        final_grade = cand
                                        break
                                if final_grade in GRADE_OPTIONS:
                                    grade_counts_bonus[final_grade] += 1
                            if is_vp and not has_bonus_no:
                                # 无筛选框时：从 report_scope 构建，各部门排除一级部门负责人
                                _src = report_scope if report_scope else report_records
                                _src_excl = [r for r in _src if not _is_dept_head(r)]
                                dept_grade_stats = _build_dept_grade_stats(_src_excl, use_total_as_base=not report_sales)
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
                                hint = f"<span title='{hint_text}' style='cursor:help;font-size:12px;margin-left:4px;color:#90A4AE;'>ⓘ</span>"
                                return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}{hint}</td>"
                            def _th(txt):
                                return f"<th style='text-align:center;{_cell_style}'>{txt}</th>"
                            def _td_label(txt):
                                return f"<td style='text-align:center;color:#b7bdc8;{_cell_style}'>{txt}</td>"
                            def _td_text(val, color):
                                return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}</td>"
                            def _td_over(val, color, is_over):
                                """超限时红色+悬停提示，不限制提交"""
                                if is_over:
                                    hint = "<span title='人数超过上限人数，请修改' style='cursor:help;font-size:12px;margin-left:4px;color:#F44336;'>ⓘ</span>"
                                    return f"<td style='text-align:center;color:#F44336;font-weight:800;border:1px solid #F44336;border-radius:4px;{_cell_style}'>{val}{hint}</td>"
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
                            # 实际人数配色：超限时红色强烈提示+悬停「人数超过上限人数，请修改」；不限制提交
                            _over_sa = actual_sa > sa_theory
                            _over_bp = actual_bp > bp_theory
                            _over_sapb = actual_sapb > sapb_theory
                            actual_cells = (
                                _td_label("实际人数")
                                + _td_over(actual_sa, "#4CAFEE", _over_sa)
                                + _td_over(actual_bp, "#8BC34A", _over_bp)
                                + _td_over(actual_sapb, "#00BCD4", _over_sapb)
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
                            st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 配额统计口径：负责范围总体配额中，不含分管高管（因为自己不能调整自己）。另，负责范围总体配额由于增加了一级部门负责人，总体额度会大于所有部门配额之和。</div>", unsafe_allow_html=True)

                            if is_vp and dept_grade_stats:
                                st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                                dept_options_sorted = sorted(dept_grade_stats.keys())
                                _dept_opts = ["-", "全部分管范围"] + dept_options_sorted
                                if "report_perf_dept_filter" not in st.session_state or st.session_state.report_perf_dept_filter not in _dept_opts:
                                    st.session_state.report_perf_dept_filter = "-"
                                _f1, _f2 = st.columns([1, 1])
                                with _f1:
                                    sel_dept = st.selectbox(
                                        "选择分管一级部门",
                                        options=_dept_opts,
                                        key="report_perf_dept_filter",
                                        label_visibility="collapsed",
                                    )
                                with _f2:
                                    st.markdown(
                                        "<div style='background:rgba(2,119,189,0.15);border:1px solid rgba(2,119,189,0.4);border-radius:6px;padding:8px 12px;font-size:13px;color:#66b2ff;'>💡 提示：选择分管一级部门</div>",
                                        unsafe_allow_html=True,
                                    )
                                _cell_style = "font-size:14px;font-weight:700;white-space:nowrap;"
                                def _td_d(val, color):
                                    return f"<td style='text-align:center;color:{color};{_cell_style}'>{val}</td>"
                                def _td_d_over(val, color, is_over):
                                    if is_over:
                                        hint = "<span title='人数超过上限人数，请修改' style='cursor:help;font-size:12px;margin-left:4px;color:#F44336;'>ⓘ</span>"
                                        return f"<td style='text-align:center;color:#F44336;font-weight:800;border:1px solid #F44336;border-radius:4px;{_cell_style}'>{val}{hint}</td>"
                                    return _td_d(val, color)
                                def _td_label_d(txt):
                                    return f"<td style='text-align:center;color:#b7bdc8;{_cell_style}'>{txt}</td>"
                                def _td_colspan_d(val, color, colspan=1):
                                    return f"<td style='text-align:center;color:{color};{_cell_style}' colspan='{colspan}'>{val}</td>"
                                header_cells_d = _th("级别") + _th("S/A级别") + _th("B+级别") + _th("B+及以上级别") + _th("B级别") + _th("B-级别") + _th("C级别") + _th("SUM (人)")

                                if sel_dept == "全部分管范围":
                                    # 一张表展示各一级部门：表头 + 各部门（部门名作子标题 + 上限 + 实际）
                                    _rows = []
                                    for dept_name in dept_options_sorted:
                                        dg = dept_grade_stats[dept_name]
                                        _rows.append(f"<tr style='background:rgba(102,178,255,0.08);'><td colspan='8' style='text-align:left;padding:8px 12px;font-weight:700;color:#66b2ff;'>{dept_name}</td></tr>")
                                        theory_cells_d = (
                                            _td_label_d("上限人数")
                                            + _td_d(dg["sa_theory"], _neutral)
                                            + _td_with_hint(dg["bp_theory"], _neutral, bp_hint)
                                            + _td_d(dg["sapb_theory"], _neutral)
                                            + _td_text("剔除绩优/差", _neutral)
                                            + _td_colspan_d("按实际评价", _neutral, colspan=2)
                                            + _td_d(dg["base_cnt"], _neutral)
                                        )
                                        _over_sa = dg["actual_sa"] > dg["sa_theory"]
                                        _over_bp = dg["actual_bp"] > dg["bp_theory"]
                                        _over_sapb = dg["actual_sapb"] > dg["sapb_theory"]
                                        _c_sa = "#F44336" if _over_sa else "#4CAFEE"
                                        _c_bp = "#F44336" if _over_bp else "#8BC34A"
                                        _c_sapb = "#F44336" if _over_sapb else "#00BCD4"
                                        actual_cells_d = (
                                            _td_label_d("实际人数")
                                            + _td_d_over(dg["actual_sa"], _c_sa, _over_sa)
                                            + _td_d_over(dg["actual_bp"], _c_bp, _over_bp)
                                            + _td_d_over(dg["actual_sapb"], _c_sapb, _over_sapb)
                                            + _td_d(dg["actual_b"], "#90A4AE")
                                            + _td_d(dg["actual_bm"], "#FFC107")
                                            + _td_d(dg["actual_c"], "#F44336")
                                            + _td_d(dg["actual_sum"], "#b7bdc8")
                                        )
                                        _rows.append(f"<tr style='border-bottom:1px solid rgba(255,255,255,0.1);'>{theory_cells_d}</tr>")
                                        _rows.append(f"<tr>{actual_cells_d}</tr>")
                                    table_all_html = f"""
                                    <div style='overflow-x:auto; margin-top:12px;'>
                                    <div style='font-size:16px;color:#66b2ff;margin-bottom:8px;font-weight:800;'>各分管部门总数</div>
                                    <table style='width:100%;border-collapse:collapse;text-align:center;'>
                                    <thead><tr style='border-bottom:1px solid rgba(255,255,255,0.2);'>{header_cells_d}</tr></thead>
                                    <tbody>
                                    {''.join(_rows)}
                                    </tbody>
                                    </table>
                                    </div>
                                    """
                                    st.markdown(table_all_html, unsafe_allow_html=True)
                                elif sel_dept and sel_dept != "-":
                                    dg = dept_grade_stats[sel_dept]
                                    theory_cells_d = (
                                        _td_label_d("部门上限人数")
                                        + _td_d(dg["sa_theory"], _neutral)
                                        + _td_with_hint(dg["bp_theory"], _neutral, bp_hint)
                                        + _td_d(dg["sapb_theory"], _neutral)
                                        + _td_text("剔除绩优/差", _neutral)
                                        + _td_colspan_d("按实际评价", _neutral, colspan=2)
                                        + _td_d(dg["base_cnt"], _neutral)
                                    )
                                    _over_sa = dg["actual_sa"] > dg["sa_theory"]
                                    _over_bp = dg["actual_bp"] > dg["bp_theory"]
                                    _over_sapb = dg["actual_sapb"] > dg["sapb_theory"]
                                    _c_sa = "#F44336" if _over_sa else "#4CAFEE"
                                    _c_bp = "#F44336" if _over_bp else "#8BC34A"
                                    _c_sapb = "#F44336" if _over_sapb else "#00BCD4"
                                    actual_cells_d = (
                                        _td_label_d("部门实际人数")
                                        + _td_d_over(dg["actual_sa"], _c_sa, _over_sa)
                                        + _td_d_over(dg["actual_bp"], _c_bp, _over_bp)
                                        + _td_d_over(dg["actual_sapb"], _c_sapb, _over_sapb)
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
                            rm_status_options = ["全部状态", "待启动", "目标设定中", "自评已交", "上级已评", "一级部门已调整", "分管高管已调整"]
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
                                done_statuses = ["上级已评", "一级部门已调整", "分管高管已调整"]
                                if drill_mode == "done" and m_status not in done_statuses:
                                    continue
                                if drill_mode == "pending" and m_status in done_statuses:
                                    continue
                                if drill_mode == "self_done" and not m.get("self_done"):
                                    continue
                                if drill_mode == "dept_done" and not m.get("dept_done"):
                                    continue
                                if drill_mode == "vp_done" and not m.get("vp_done"):
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
                            _vp_dept_key = "一级部门" if is_vp else "二级部门"
                            _exec_vp, _dept_vp = _build_detail_stats(report_records, extract_text_fn=extract_text, dept_key=_vp_dept_key)
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
                                if _exec_vp["total"] > 0:
                                    td, dd = _exec_vp["total"], _exec_vp["done"]
                                    _chart_exec_names = sorted({extract_text(r.get("fields", {}).get("姓名"), "").strip() or "未知" for r in report_records if _is_executive(r)})
                                    _chart_exec_label = f"高管（{_chart_exec_names[0]}）" if len(_chart_exec_names) == 1 else (f"高管（{'、'.join(_chart_exec_names)}）" if _chart_exec_names else "高管")
                                    dept_rate_rows.append({"部门": _chart_exec_label, "完成率": round(dd / td * 100, 1) if td else 0.0, "完成": dd, "总数": td})
                                for dept_name, dval in sorted(_dept_vp.items(), key=lambda x: x[0]):
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

                            def _vp_row(dept, scope, t, d, g):
                                rv = round(d / t * 100, 1) if t else 0
                                return {"部门": dept, "口径": scope, "总人数": t, "已完成": d, "完成率": "100%" if rv == 100 else f"{rv}%", "S级": g["S"], "A级": g["A"], "B+级": g["B+"], "B级": g["B"], "B-级": g["B-"], "C级": g["C"]}
                            if _exec_vp["total"] > 0:
                                _vp_exec_names = sorted({extract_text(r.get("fields", {}).get("姓名"), "").strip() or "未知" for r in report_records if _is_executive(r)})
                                _vp_exec_label = f"高管（{_vp_exec_names[0]}）" if len(_vp_exec_names) == 1 else (f"高管（{'、'.join(_vp_exec_names)}）" if _vp_exec_names else "高管")
                                dept_rows.append(_vp_row(_vp_exec_label, "总", _exec_vp["total"], _exec_vp["done"], _exec_vp["grades"]))
                                if (_exec_vp.get("sales_total") or 0) > 0 and (_exec_vp.get("non_sales_total") or 0) > 0:
                                    dept_rows.append(_vp_row(_vp_exec_label, "销售", _exec_vp["sales_total"], _exec_vp["sales_done"], _exec_vp.get("sales_grades", _exec_vp["grades"])))
                                    dept_rows.append(_vp_row(_vp_exec_label, "非销售", _exec_vp["non_sales_total"], _exec_vp["non_sales_done"], _exec_vp.get("non_sales_grades", _exec_vp["grades"])))
                            for dept_name, dval in sorted(_dept_vp.items(), key=lambda x: x[0]):
                                dept_rows.append(_vp_row(dept_name, "总", dval["total"], dval["done"], dval["grades"]))
                                if (dval.get("sales_total") or 0) > 0 and (dval.get("non_sales_total") or 0) > 0:
                                    dept_rows.append(_vp_row(dept_name, "销售", dval["sales_total"], dval["sales_done"], dval.get("sales_grades", dval["grades"])))
                                    dept_rows.append(_vp_row(dept_name, "非销售", dval["non_sales_total"], dval["non_sales_done"], dval.get("non_sales_grades", dval["grades"])))
                            dept_df = pd.DataFrame(dept_rows)
                            if not dept_df.empty:
                                def _vp_dept_row_style(row):
                                    scope, dept = row.get("口径", ""), str(row.get("部门", ""))
                                    if dept.startswith("高管"):
                                        if scope == "总":
                                            return ["font-weight: 700; font-size: 14px; background-color: rgba(255,193,7,0.12); color: #FFC107;"] * len(row)
                                        if scope in ("销售", "非销售"):
                                            return ["font-size: 12px; background-color: rgba(255,193,7,0.06);"] * len(row)
                                    if scope == "总":
                                        return ["font-weight: 700; font-size: 14px; background-color: rgba(255,255,255,0.06);"] * len(row)
                                    if scope == "销售":
                                        return ["font-size: 12px; color: #81C784; background-color: rgba(129,199,132,0.08);"] * len(row)
                                    if scope == "非销售":
                                        return ["font-size: 12px; color: #64B5F6; background-color: rgba(100,181,246,0.08);"] * len(row)
                                    return [""] * len(row)
                                dept_df_display = dept_df.style.set_properties(**{"text-align": "center"}).apply(_vp_dept_row_style, axis=1)
                            else:
                                dept_df_display = dept_df
                            st.dataframe(dept_df_display, use_container_width=True, hide_index=True)
                            if is_vp:
                                st.markdown("<div style='text-align:left;font-size:12px;color:#9aa0a6;margin-top:8px;'>💡 提示：各部门含一级部门负责人，高管单列；如果一级部门负责人和高管为同一人，则一级部门中不含，高管单列。</div>", unsafe_allow_html=True)

    # ==========================================
    # 🟢 模块 3：历史信息 (员工个人不显示)
    # 管理者：展示所有下属的绩效等级列表；员工：展示个人历史绩效
    # ==========================================
    if "📂 团队历史绩效" in tab_list:
        with tabs[tab_list.index("📂 团队历史绩效")]:
            if st.session_state.role == "管理者":
                # 管理者：下属历史绩效等级列表，范围与视图报表一致
                history_records_all = all_records_snapshot or fetch_all_records_safely(APP_TOKEN, TABLE_ID)
                if not history_records_all:
                    st.info("💡 提示：暂无可用于历史档案的数据。")
                else:
                    # 分管高管：需排除其他分管高管，只展示非分管高管员工
                    vp_names_set = set()
                    if is_vp:
                        for r in history_records_all:
                            vp_str = extract_text(r.get("fields", {}).get("分管高管") or r.get("fields", {}).get("高管"), "").strip()
                            for part in vp_str.replace("，", ",").replace("；", ",").split(","):
                                n = part.strip()
                                if n:
                                    vp_names_set.add(n)

                    history_scoped = []
                    for rec in history_records_all:
                        rf = rec.get("fields", {})
                        emp_name = extract_text(rf.get("姓名"), "").strip()
                        if user_name and emp_name == user_name:
                            continue
                        if is_vp:
                            vp_str = extract_text(rf.get("分管高管") or rf.get("高管"), "").strip()
                            if user_name in vp_str and emp_name not in vp_names_set:
                                history_scoped.append(rec)
                        elif is_dept_head:
                            dept_head_str = extract_text(rf.get("一级部门负责人") or rf.get("部门负责人"), "").strip()
                            if user_name in dept_head_str:
                                history_scoped.append(rec)
                        else:
                            rec_manager = extract_text(rf.get("直接评价人") or rf.get("评价人"), "").strip()
                            rec_skip_level = extract_text(rf.get("隔级上级"), "").strip()
                            is_direct = user_name and user_name in rec_manager
                            is_skip_level = user_name and user_name in rec_skip_level and not is_direct
                            if is_direct or is_skip_level:
                                history_scoped.append(rec)

                    if not history_scoped:
                        st.info("💡 提示：您暂无下属，无历史绩效档案可查看。")
                    else:
                        _grade_colors = {"S": "#4CAFEE", "A": "#4CAFEE", "B+": "#8BC34A", "B": "#90A4AE", "B-": "#FFC107", "C": "#F44336"}
                        st.markdown("<div class='module-title'>👇 下属历史绩效等级</div>", unsafe_allow_html=True)

                        # 筛选框：工号姓名 / 部门 / 考核等级（分管高管用一级部门，其他用二级-三级-四级链路）
                        history_dept_options = set()
                        for rec in history_scoped:
                            ff = rec.get("fields", {})
                            if is_vp:
                                _d1 = _clean_dept_name(ff.get("一级部门"))
                                if _d1:
                                    history_dept_options.add(_d1)
                            else:
                                _chain = build_dept_chain(ff)
                                _d1 = _clean_dept_name(ff.get("一级部门"))
                                if _chain:
                                    history_dept_options.add(_chain)
                                elif _d1:
                                    history_dept_options.add(_d1)
                        history_dept_options = sorted(history_dept_options)

                        # 筛选框与「直属」按钮同一行（隔级上级也有隔级下属，需直属筛选与打标）
                        has_skip_level_subs = len(my_all_subs) > len(my_direct_subs)
                        if "history_filter_mode" not in st.session_state:
                            st.session_state.history_filter_mode = "all"
                        n_cols = 4 if (is_vp or is_dept_head or has_skip_level_subs) else 3
                        cols = st.columns([1, 2, 1, 0.8] if n_cols == 4 else [1, 2, 1], gap="small")
                        q_name_emp = cols[0].text_input("搜索工号、姓名", placeholder="🔎 搜索工号、姓名", key="history_filter_name_emp", label_visibility="collapsed")
                        q_dept = cols[1].selectbox("部门", ["全部部门"] + history_dept_options, key="history_filter_dept", label_visibility="collapsed")
                        q_grade = cols[2].selectbox("考核等级", ["全部考核等级"] + GRADE_OPTIONS + ["-", "暂无"], key="history_filter_grade", label_visibility="collapsed")
                        if is_vp or is_dept_head or has_skip_level_subs:
                            with cols[3]:
                                st.markdown("<div class='history-direct-btn-marker' style='width:0;height:0;overflow:hidden;'></div>", unsafe_allow_html=True)
                                if st.button("直属", key="history_btn_direct", use_container_width=True):
                                    st.session_state.history_filter_mode = "all" if st.session_state.history_filter_mode == "direct" else "direct"
                                    st.rerun()

                        # 筛选模式：全部 / 直属
                        history_to_filter = history_scoped
                        mode = st.session_state.get("history_filter_mode", "all")
                        if (is_vp or is_dept_head or has_skip_level_subs) and mode == "direct":
                            history_to_filter = []
                            for rec in history_scoped:
                                rf = rec.get("fields", {})
                                rec_manager = extract_text(rf.get("直接评价人") or rf.get("评价人"), "").strip()
                                if user_name and user_name in rec_manager:
                                    history_to_filter.append(rec)

                        filtered_history = []
                        q1 = q_name_emp.strip().lower()
                        for rec in history_to_filter:
                            f = rec.get("fields", {})
                            name = extract_text(f.get("姓名"), "").strip()
                            emp = extract_text(f.get("工号") or f.get("员工工号"), "").strip()
                            dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
                            dept_chain = build_dept_chain(f) or dept_l1
                            rec_dept = dept_l1 if is_vp else dept_chain
                            last_result = extract_text(f.get("上一次绩效考核结果", "暂无"), "").strip() or "暂无"

                            if q1 and (q1 not in name.lower() and q1 not in emp.lower()):
                                continue
                            if q_dept != "全部部门" and rec_dept != q_dept and not (rec_dept or "").startswith(q_dept + "-"):
                                continue
                            if q_grade != "全部考核等级" and last_result != q_grade:
                                continue
                            filtered_history.append(rec)

                        if (is_vp or is_dept_head or has_skip_level_subs) and mode == "direct":
                            st.caption("📌 当前显示：直属")
                        st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)

                        if not filtered_history:
                            st.caption("当前筛选条件下暂无员工。")
                        for rec in filtered_history:
                            f = rec.get("fields", {})
                            name = extract_text(f.get("姓名"), "未知姓名").strip()
                            emp = extract_text(f.get("工号") or f.get("员工工号"), "未知工号").strip()
                            job = extract_text(f.get("岗位") or f.get("职位"), "未分配").strip()
                            dept_l1 = _clean_dept_name(f.get("一级部门")) or "未分配部门"
                            dept_l2 = normalize_dept_text(f.get("二级部门"))
                            dept_display = dept_l1 if is_vp else (dept_l2 or dept_l1)
                            perf_cycle = extract_text(f.get("上一次绩效考核对应周期", "暂无"), "").strip() or "暂无"
                            last_result = extract_text(f.get("上一次绩效考核结果", "暂无"), "").strip() or "暂无"
                            res_color = _grade_colors.get(last_result, "#b7bdc8")
                            # 直属打标：一级部门负责人/分管高管/隔级上级的直属显示绿色(直属)标识，隔级不标记
                            rec_manager = extract_text(f.get("直接评价人") or f.get("评价人"), "").strip()
                            is_direct = user_name and user_name in rec_manager
                            name_display = f"<b>{name}</b>"
                            if (is_vp or is_dept_head or has_skip_level_subs) and is_direct:
                                name_display += f" <span style='color:#4CAF50;'>(直属)</span>"
                            name_display += f"（{emp}）"

                            c1, c2, c3, c4 = st.columns([1.8, 3.2, 2.0, 1.5], gap="small", vertical_alignment="center")
                            c1.markdown(f"<div class='sub-list-cell' style='color:#E0E0E0; text-align:center;'>{name_display}</div>", unsafe_allow_html=True)
                            c2.markdown(f"<div class='sub-list-cell sub-list-cell-multiline' style='color:#b0b0b0; text-align:center;' title='{dept_display} | {job}'>{dept_display}<br>{job}</div>", unsafe_allow_html=True)
                            c3.markdown(f"<div class='sub-list-cell' style='color:#b0b0b0; text-align:center;'>{perf_cycle}</div>", unsafe_allow_html=True)
                            c4.markdown(f"<div class='sub-list-cell' style='color:{res_color}; text-align:center; font-weight:700;'>{last_result}</div>", unsafe_allow_html=True)
                            st.markdown("<hr class='sub-hr'/>", unsafe_allow_html=True)

            else:
                # 员工：个人历史绩效
                perf_cycle = extract_text(fields.get("上一次绩效考核对应周期", "暂无数据"))
                last_perf_result = extract_text(fields.get("上一次绩效考核结果", "暂无数据"))
                last_comment = extract_text(fields.get("上一次绩效考核评语", "暂无评语"))
                _grade_colors = {"S": "#4CAFEE", "A": "#4CAFEE", "B+": "#8BC34A", "B": "#90A4AE", "B-": "#FFC107", "C": "#F44336"}
                _res_color = _grade_colors.get(last_perf_result, "#b7bdc8")

                st.markdown(
                    f"""<div style="font-size: 16px; font-weight: 700; margin-bottom: 10px; padding: 10px; background-color: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid #444;"><div style="display:flex; justify-content:center; gap:18px; flex-wrap:wrap;"><span style="color:#b7bdc8;">上一次考核周期：<span style="color:#4CAFEE;">{perf_cycle}</span></span><span style="color:#b7bdc8;">上一次绩效结果：<span style="color:{_res_color};">{last_perf_result}</span></span></div></div>""",
                    unsafe_allow_html=True,
                )
                st.markdown("<div style='height: 20px;'></div><hr style='border:none;border-top:1px solid rgba(255,255,255,0.15);margin:0 0 20px 0;'/><div style='height: 8px;'></div>", unsafe_allow_html=True)
                st.markdown("<div class='module-title'>✍️ 上一次绩效考核评语</div>", unsafe_allow_html=True)
                st.info(last_comment)

    # 回到顶端悬浮按钮：纯 HTML 锚点，浏览器原生滚动，无需 JS
    if st.session_state.role == "管理者":
        _ct = st.session_state.get("main_tabs")
        if _ct in ("👥 上级评分", "📌 一级部门负责人调整", "📌 分管高管调整"):
            st.markdown('<a href="#page-top" class="back-to-top-btn" title="回到顶端">⬆️ 回到顶端</a>', unsafe_allow_html=True)

if st.session_state.user_info is None:
    login_page()
else:
    main_app()

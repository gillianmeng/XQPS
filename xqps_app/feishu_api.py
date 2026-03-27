"""飞书多维表 HTTP 封装（含 Streamlit 缓存）。"""
from __future__ import annotations

import time

import requests
import streamlit as st

from xqps_app.config import APP_ID, APP_SECRET

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


@st.cache_data(ttl=300, show_spinner=False)
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
    return None, res.get("msg")


@st.cache_data(
    ttl=90,
    show_spinner="🚀 全力加速中，请给我点鼓励ಥ_ಥ",
)
def fetch_all_records_safely(app_token, table_id):
    """全表记录（分页）。缓存 TTL 略短于 token，写操作后请在业务侧调用 .clear()。"""
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

    for record in all_records:
        fields = record.get("fields", {})
        value = fields.get("姓名")
        if value is None:
            continue
        if isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict) and value[0].get("id") == target_openid:
            return record
        elif isinstance(value, dict) and value.get("id") == target_openid:
            return record
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


@st.cache_data(ttl=300, show_spinner=False)
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


@st.cache_data(ttl=300, show_spinner=False)
def fetch_table_field_names_ordered(app_token, table_id):
    """多维表全部列名（与飞书表头一致，含未在记录中出现的空字段），顺序与接口返回一致。"""
    tenant_token = get_tenant_token()
    if not tenant_token:
        return []
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    page_token = ""
    has_more = True
    field_names = []
    seen = set()
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
            if name and name not in seen:
                seen.add(name)
                field_names.append(name)
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
    return False, res.get("msg", str(res))

#!/usr/bin/env python3
"""
将财富顾问部员工的「分管高管」字段批量设置为刘江涛。
使用与 new_app.py 相同的 API 和鉴权。

用法：
  python set_branch_vp_wealth.py          # 执行更新
  python set_branch_vp_wealth.py --dry    # 仅预览，不写入
"""
import json
import os
import sys
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 刘江涛：副总裁，财富顾问部分管高管
LIU_JIANGTAO_OPEN_ID = "ou_5d48f9da909338c063526a4fb24a513f"
LIU_JIANGTAO_NAME = "刘江涛"
TARGET_DEPT = "财富顾问部"


def _load_secrets():
    secrets_path = os.path.join(SCRIPT_DIR, ".streamlit", "secrets.toml")
    if not os.path.exists(secrets_path):
        return {}
    try:
        import tomllib
        with open(secrets_path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        try:
            import tomli as tomllib
            with open(secrets_path, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            pass
    return {}


def _get_config(key, env_key, default=""):
    val = os.environ.get(env_key)
    if val:
        return str(val)
    secrets = _load_secrets()
    return str(secrets.get(key, default) or default)


def _to_text(v, default=""):
    if v is None:
        return default
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return str(v[0].get("name") or v[0].get("text") or "").strip() or default
    if isinstance(v, dict):
        return str(v.get("name") or v.get("text") or "").strip() or default
    return str(v or "").strip() or default


def _open_id_from_person(v):
    if not v:
        return ""
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return str(v[0].get("id") or "").strip()
    if isinstance(v, dict):
        return str(v.get("id") or "").strip()
    return ""


def _vp_contains_liu(vp_field):
    """检查分管高管字段是否已包含刘江涛"""
    if not vp_field:
        return False
    if isinstance(vp_field, list):
        for item in vp_field:
            if isinstance(item, dict) and item.get("id") == LIU_JIANGTAO_OPEN_ID:
                return True
            if isinstance(item, dict) and (item.get("name") or item.get("text") or "").strip() == LIU_JIANGTAO_NAME:
                return True
    if isinstance(vp_field, dict) and vp_field.get("id") == LIU_JIANGTAO_OPEN_ID:
        return True
    return False


def get_tenant_token(app_id, app_secret):
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=12)
    data = resp.json()
    return data.get("tenant_access_token")


def fetch_all_records(app_token, table_id, tenant_token):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    all_items = []
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(url, headers=headers, params=params, timeout=12)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书 API 错误: {data.get('msg', data)}")
        items = data.get("data", {}).get("items", [])
        all_items.extend(items)
        if not data.get("data", {}).get("has_more", False):
            break
        page_token = data.get("data", {}).get("page_token", "")
    return all_items


def fetch_table_field_names(app_token, table_id, tenant_token):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    resp = requests.get(url, headers=headers, timeout=12)
    data = resp.json()
    if data.get("code") != 0:
        return []
    return [f.get("name") for f in data.get("data", {}).get("items", []) if f.get("name")]


def update_record(app_token, table_id, record_id, update_data, tenant_token):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"}
    payload = {"fields": update_data}
    resp = requests.put(url, headers=headers, json=payload, timeout=12)
    return resp.json()


def main():
    dry_run = "--dry" in sys.argv or "-n" in sys.argv

    app_id = _get_config("FEISHU_APP_ID", "FEISHU_APP_ID")
    app_secret = _get_config("FEISHU_APP_SECRET", "FEISHU_APP_SECRET")
    app_token = _get_config("FEISHU_APP_TOKEN", "FEISHU_APP_TOKEN")
    table_id = _get_config("FEISHU_TABLE_ID", "FEISHU_TABLE_ID")

    if not all([app_id, app_secret, app_token, table_id]):
        print("请在 .streamlit/secrets.toml 中配置 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_APP_TOKEN、FEISHU_TABLE_ID", file=sys.stderr)
        sys.exit(1)

    token = get_tenant_token(app_id, app_secret)
    if not token:
        print("获取飞书 Token 失败", file=sys.stderr)
        sys.exit(1)

    field_names = fetch_table_field_names(app_token, table_id, token)
    vp_field_name = None
    for fn in ("分管高管", "高管"):
        if fn in field_names:
            vp_field_name = fn
            break
    if not vp_field_name:
        print("多维表中未找到「分管高管」或「高管」字段", file=sys.stderr)
        sys.exit(1)

    all_records = fetch_all_records(app_token, table_id, token)
    to_update = []
    for rec in all_records:
        fields = rec.get("fields", {})
        dept = _to_text(fields.get("一级部门"), "")
        if dept != TARGET_DEPT:
            continue
        name = _to_text(fields.get("姓名"), "")
        open_id = _open_id_from_person(fields.get("姓名"))
        emp_id = _to_text(fields.get("工号"), "") or _to_text(fields.get("员工工号"), "")
        vp_field = fields.get(vp_field_name)
        if _vp_contains_liu(vp_field):
            continue
        if open_id == LIU_JIANGTAO_OPEN_ID or name == LIU_JIANGTAO_NAME:
            continue
        to_update.append({
            "record_id": rec.get("record_id"),
            "name": name,
            "emp_id": emp_id,
        })

    if not to_update:
        print(f"财富顾问部中无需更新的记录（分管高管已为 {LIU_JIANGTAO_NAME} 或仅剩本人）。")
        return

    print(f"将更新 {len(to_update)} 条记录，将「{vp_field_name}」设为 {LIU_JIANGTAO_NAME}：")
    for u in to_update:
        print(f"  - {u['name']}（{u['emp_id']}）")
    if dry_run:
        print("\n[--dry 模式] 未执行写入。去掉 --dry 后重新运行以执行更新。")
        return

    update_data = {vp_field_name: [{"id": LIU_JIANGTAO_OPEN_ID}]}
    ok, fail = 0, 0
    for u in to_update:
        res = update_record(app_token, table_id, u["record_id"], update_data, token)
        if res.get("code") == 0:
            ok += 1
            print(f"  ✓ {u['name']}")
        else:
            fail += 1
            print(f"  ✗ {u['name']}: {res.get('msg', res)}", file=sys.stderr)
    print(f"\n完成：成功 {ok}，失败 {fail}")


if __name__ == "__main__":
    main()

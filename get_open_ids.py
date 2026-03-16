#!/usr/bin/env python3
"""
从飞书绩效多维表拉取指定部门员工的真实 open_id 和角色，输出 demo_users.json 格式。
使用与 new_app.py 相同的 API 和鉴权，确保能正确拉取数据。

用法：
  python get_open_ids.py 人力资源部 > demo_users_hr.json
  python get_open_ids.py -l   # 列出所有一级部门
"""
import json
import os
import sys
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_secrets():
    """从 .streamlit/secrets.toml 加载配置（与 new_app 一致）"""
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


def main():
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

    all_records = fetch_all_records(app_token, table_id, token)

    if len(sys.argv) > 1:
        arg = sys.argv[1].strip()
        if arg in ("-l", "--list", "list"):
            depts = set()
            for item in all_records:
                d = _to_text(item.get("fields", {}).get("一级部门"), "")
                if d:
                    depts.add(d)
            for d in sorted(depts):
                print(d)
            return
        departments = [d.strip() for d in sys.argv[1:] if d.strip()]
    else:
        dep_str = os.environ.get("DEPARTMENTS", "")
        departments = [d.strip() for d in dep_str.split(",") if d.strip()]

    if not departments:
        print("用法: python get_open_ids.py <一级部门1> [一级部门2] ...", file=sys.stderr)
        print("      python get_open_ids.py -l   # 列出所有一级部门", file=sys.stderr)
        print("示例: python get_open_ids.py 人力资源部 > demo_users_hr.json", file=sys.stderr)
        sys.exit(1)

    dept_set = set(departments)
    out = []
    for item in all_records:
        fields = item.get("fields", {})
        dept = _to_text(fields.get("一级部门"), "")
        if not dept or dept not in dept_set:
            continue
        name = _to_text(fields.get("姓名"), "")
        open_id = _open_id_from_person(fields.get("姓名"))
        emp_id = _to_text(fields.get("工号"), "") or _to_text(fields.get("员工工号"), "")
        job_title = _to_text(fields.get("岗位"), "") or _to_text(fields.get("职位"), "未分配")
        role = _to_text(fields.get("角色"), "员工").strip() or "员工"
        if role not in ("员工", "管理者"):
            role = "员工"
        if not (name and open_id and emp_id):
            continue
        label = f"{name}（工号: {emp_id}）"
        out.append({
            "label": label,
            "name": name,
            "open_id": open_id,
            "emp_id": emp_id,
            "job_title": job_title or "未分配",
            "role": role,
            "test_env": ",".join(departments),
        })

    print(json.dumps({"users": out}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

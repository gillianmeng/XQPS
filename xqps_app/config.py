"""飞书与环境变量配置（供 new_app 与各子模块共用）。"""
from __future__ import annotations

import os

import streamlit as st


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

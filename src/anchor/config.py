"""Runtime configuration loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # Splunk
    splunk_host: str = os.getenv("SPLUNK_HOST", "localhost")
    splunk_port: int = int(os.getenv("SPLUNK_PORT", "8089"))
    splunk_username: str = os.getenv("SPLUNK_USERNAME", "admin")
    splunk_password: str = os.getenv("SPLUNK_PASSWORD", "")
    splunk_scheme: str = os.getenv("SPLUNK_SCHEME", "https")
    splunk_verify_ssl: bool = _bool("SPLUNK_VERIFY_SSL", False)

    # KV Store app context
    anchor_app: str = os.getenv("ANCHOR_APP", "search")
    anchor_owner: str = os.getenv("ANCHOR_OWNER", "nobody")

    # LLM
    llm_provider: str = os.getenv("ANCHOR_LLM", "qwen")
    qwen_api_key: str = os.getenv("QWEN_API_KEY", "")
    qwen_model: str = os.getenv("QWEN_MODEL", "qwen-plus")
    qwen_base_url: str = os.getenv(
        "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    gemini_base_url: str = os.getenv(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    splunk_llm_endpoint: str = os.getenv("SPLUNK_LLM_ENDPOINT", "")
    splunk_llm_model: str = os.getenv("SPLUNK_LLM_MODEL", "")


CONFIG = Config()

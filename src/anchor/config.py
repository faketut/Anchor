"""Runtime configuration loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

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
    splunk_host: str = field(default_factory=lambda: os.getenv("SPLUNK_HOST", "localhost"))
    splunk_port: int = field(default_factory=lambda: int(os.getenv("SPLUNK_PORT", "8089")))
    splunk_username: str = field(default_factory=lambda: os.getenv("SPLUNK_USERNAME", "admin"))
    splunk_password: str = field(default_factory=lambda: os.getenv("SPLUNK_PASSWORD", ""))
    splunk_scheme: str = field(default_factory=lambda: os.getenv("SPLUNK_SCHEME", "https"))
    splunk_verify_ssl: bool = field(default_factory=lambda: _bool("SPLUNK_VERIFY_SSL", False))

    # KV Store app context
    anchor_app: str = field(default_factory=lambda: os.getenv("ANCHOR_APP", "search"))
    anchor_owner: str = field(default_factory=lambda: os.getenv("ANCHOR_OWNER", "nobody"))

    # LLM
    llm_provider: str = field(default_factory=lambda: os.getenv("ANCHOR_LLM", "qwen"))
    qwen_api_key: str = field(default_factory=lambda: os.getenv("QWEN_API_KEY", ""))
    qwen_model: str = field(default_factory=lambda: os.getenv("QWEN_MODEL", "qwen-plus"))
    qwen_base_url: str = field(
        default_factory=lambda: os.getenv(
            "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
    )
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
    gemini_base_url: str = field(
        default_factory=lambda: os.getenv(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
    )
    splunk_llm_endpoint: str = field(default_factory=lambda: os.getenv("SPLUNK_LLM_ENDPOINT", ""))
    splunk_llm_model: str = field(default_factory=lambda: os.getenv("SPLUNK_LLM_MODEL", ""))


CONFIG = Config()

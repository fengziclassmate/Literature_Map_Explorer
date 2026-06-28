from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _csv_env(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    app_name: str = "Literature Map Explorer"
    app_version: str = "0.1.0"
    database_url: str = os.getenv("LME_DATABASE_URL", "sqlite:///./literature_map.db")

    user_agent: str = os.getenv(
        "LME_USER_AGENT",
        "LiteratureMapExplorer/0.1 (+https://example.local; mailto:research@example.local)",
    )
    contact_email: str | None = os.getenv("LME_CONTACT_EMAIL")

    request_timeout_seconds: float = _float_env("LME_REQUEST_TIMEOUT_SECONDS", 20.0)
    api_max_retries: int = _int_env("LME_API_MAX_RETRIES", 3)
    api_cache_ttl_seconds: int = _int_env("LME_API_CACHE_TTL_SECONDS", 60 * 60 * 24)
    pdf_download_dir: str = os.getenv("LME_PDF_DOWNLOAD_DIR", "./downloads/pdfs")
    unpaywall_email: str = os.getenv("LME_UNPAYWALL_EMAIL", os.getenv("LME_CONTACT_EMAIL", "research@example.local"))
    institution_name: str = os.getenv("LME_INSTITUTION_NAME", "")
    ezproxy_url_prefix: str = os.getenv("LME_EZPROXY_URL_PREFIX", "")
    library_resolver_url: str = os.getenv("LME_LIBRARY_RESOLVER_URL", "")
    carsi_login_url: str = os.getenv("LME_CARSI_LOGIN_URL", "")
    webvpn_url: str = os.getenv("LME_WEBVPN_URL", "")
    institution_login_url: str = os.getenv("LME_INSTITUTION_LOGIN_URL", "")

    openalex_rate_limit_per_second: float = _float_env("LME_OPENALEX_RPS", 8.0)
    semantic_scholar_rate_limit_per_second: float = _float_env("LME_SEMANTIC_SCHOLAR_RPS", 1.0)
    crossref_rate_limit_per_second: float = _float_env("LME_CROSSREF_RPS", 3.0)

    semantic_scholar_api_key: str | None = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    cors_origins: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cors_origins",
            _csv_env("LME_CORS_ORIGINS", ["http://localhost:5173", "http://127.0.0.1:5173"]),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


MUSE_SETTINGS_FILE = Path(__file__).resolve().parent / "workspace" / "_settings" / "muse.json"
MUSE_PROVIDERS = ("muse2api", "browser")


def saved_muse_provider() -> str | None:
    try:
        data = json.loads(MUSE_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    provider = data.get("provider") if isinstance(data, dict) else None
    return provider if provider in MUSE_PROVIDERS else None


@lru_cache(maxsize=1)
def active_muse_provider() -> str:
    """Keep running jobs on the selected provider until Studio restarts."""
    return saved_muse_provider() or os.getenv("MUSE_PROVIDER", "muse2api").strip().lower()


def muse_connection(provider: str | None = None) -> tuple[str, str, str | None]:
    """Resolve a provider and its server-side credentials."""
    if provider is None:
        provider = os.getenv("MUSE_PROVIDER", "muse2api").strip().lower()
    if provider == "muse2api":
        return (
            provider,
            os.getenv("MUSE2API_URL", "http://localhost:18610"),
            os.getenv("MUSE2API_KEY") or os.getenv("MUSE_API_KEY") or None,
        )
    if provider == "browser":
        value = os.getenv("MUSE_BROWSER_PORT", "18787")
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
            raise ValueError("MUSE_BROWSER_PORT phải là số nguyên từ 1 tới 65535.")
        return provider, f"http://127.0.0.1:{int(value)}", os.getenv("MUSE_BROWSER_KEY") or None
    raise ValueError("MUSE_PROVIDER nhận 'muse2api' hoặc 'browser'.")

def browser_media_headers(url: str, gateway_url: str, key: str | None) -> dict[str, str]:
    """Authenticate only a browser media download from this same gateway."""
    target = urlsplit(url)
    gateway = urlsplit(gateway_url)
    if ((target.scheme, target.netloc) == (gateway.scheme, gateway.netloc)
            and key and re.fullmatch(r"/v1/browser-media/[a-f0-9-]{36}\.(?:mp4|webm|png|jpg|webp|gif)", target.path)):
        return {"Authorization": f"Bearer {key}"}
    return {}

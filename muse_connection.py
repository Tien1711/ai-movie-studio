from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


MUSE_SETTINGS_FILE = Path(__file__).resolve().parent / "workspace" / "_settings" / "muse.json"
MUSE_PROVIDERS = ("muse2api", "browser")
_MUSE_AFFINITY: ContextVar[str | None] = ContextVar("muse_affinity", default=None)
_AFFINITY_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def current_muse_affinity() -> str | None:
    """Return the active film/job routing key for soft per-browser affinity."""
    value = _MUSE_AFFINITY.get()
    return value if value and _AFFINITY_RE.fullmatch(value) else None


@contextmanager
def muse_affinity_scope(value: str | None):
    """Bind a soft browser-routing key (e.g. film or job id) for the current execution context."""
    clean = value.strip() if isinstance(value, str) else None
    token = _MUSE_AFFINITY.set(clean if clean and _AFFINITY_RE.fullmatch(clean) else None)
    try:
        yield
    finally:
        _MUSE_AFFINITY.reset(token)


def muse_capacity_from_accounts(provider: str, data, max_concurrent: int = 12) -> int:
    """Compute effective parallel lane count from a /admin/accounts payload."""
    if provider == "browser" and not (isinstance(data, dict) and data.get("pool") is True):
        return 1
    accounts = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(accounts, list):
        return 1
    enabled = sum(1 for acc in accounts if isinstance(acc, dict) and acc.get("enabled", True))
    return max(1, min(enabled, max_concurrent))


def query_muse_capacity_sync(max_concurrent: int = 12, timeout: float = 2.0) -> int:
    """Synchronously query active Muse provider for parallel worker capacity."""
    import httpx

    try:
        provider, url, key = muse_connection(active_muse_provider())
        if not key:
            return 1
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                f"{url}/admin/accounts",
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
            return muse_capacity_from_accounts(provider, response.json(), max_concurrent)
    except Exception:
        return 1


async def query_muse_capacity_async(max_concurrent: int = 12, timeout: float = 3.0) -> int:
    """Asynchronously query active Muse provider for parallel worker capacity."""
    import httpx

    try:
        provider, url, key = muse_connection(active_muse_provider())
        if not key:
            return 1
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{url}/admin/accounts",
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
            return muse_capacity_from_accounts(provider, response.json(), max_concurrent)
    except Exception:
        return 1


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

import os


def muse_connection() -> tuple[str, str, str | None]:
    """Select the provider and its credentials from Studio's server environment."""
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

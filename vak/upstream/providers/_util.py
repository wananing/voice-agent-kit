"""Small helpers shared by the real providers: config lookup, env vars,
lazy optional imports, PCM16 resampling.

No vendor logic lives here.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any

import httpx
import numpy as np

# Idle connections stay open this long so the next turn skips TCP + TLS
# (handbook first-audio: connection reuse window 10–60 s; httpx's default is 5 s).
KEEPALIVE_S = 60.0

log = logging.getLogger(__name__)


class ProviderConfigError(ValueError):
    """A required config key, env var or optional dependency is missing."""


def cfg(config: dict[str, Any], key: str, default: Any = None) -> Any:
    v = config.get(key)
    return default if v is None else v


def require(config: dict[str, Any], key: str, provider: str) -> Any:
    v = config.get(key)
    if v is None or v == "":
        raise ProviderConfigError(f"{provider}: config key {key!r} is required")
    return v


def secret(config: dict[str, Any], key: str, env: str, provider: str, *, required: bool = True) -> str | None:
    """`config[key]`, else `$env`; raise a clear error when neither is set."""
    v = config.get(key) or os.environ.get(env)
    if not v and required:
        raise ProviderConfigError(f"{provider}: set config {key!r} or environment variable {env}")
    return v or None


def http_client(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout_s, limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))


async def warm_http(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None) -> None:
    """Open (or refresh) a pooled connection to `url`'s host; the response is irrelevant."""
    try:
        r = await client.get(url, headers=headers)
        await r.aclose()
    except httpx.HTTPError as e:
        log.debug("warmup %s failed: %s", url, e)


def lazy_import(module: str, *, provider: str, extra: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise ProviderConfigError(
            f"{provider}: optional dependency {module!r} is not installed (pip install {extra})"
        ) from e


def pcm16_duration_ms(pcm16: bytes, sample_rate: int) -> int:
    return (len(pcm16) // 2) * 1000 // sample_rate


def resample_pcm16(pcm16: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of mono PCM16. Uses vak.audio.resample
    when the audio module provides one, else a tiny numpy implementation."""
    if src_rate == dst_rate or not pcm16:
        return pcm16
    try:
        from vak.audio import resample as _r  # type: ignore[attr-defined]

        fn = getattr(_r, "resample_pcm16", None)
        if fn is not None:
            return fn(pcm16, src_rate, dst_rate)
    except ImportError:
        pass
    x = np.frombuffer(pcm16[: len(pcm16) - len(pcm16) % 2], dtype=np.int16).astype(np.float32)
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    src_pos = np.arange(n_out, dtype=np.float64) * (src_rate / dst_rate)
    y = np.interp(src_pos, np.arange(x.size, dtype=np.float64), x)
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()

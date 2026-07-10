"""HTTP helpers that avoid proxying local desktop services."""
from __future__ import annotations

import ipaddress
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse

import httpx


def should_trust_env(url: str) -> bool:
    """Return False for loopback/LAN URLs so local AI services bypass proxies."""
    host = (urlparse(str(url or "")).hostname or "").strip().lower()
    if not host:
        return True
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_loopback or ip.is_private or ip.is_link_local)


@contextmanager
def http_client(url: str, **kwargs) -> Iterator[httpx.Client]:
    kwargs.setdefault("trust_env", should_trust_env(url))
    with httpx.Client(**kwargs) as client:
        yield client


def http_get(url: str, **kwargs) -> httpx.Response:
    client_kwargs = _client_kwargs(url, kwargs)
    with httpx.Client(**client_kwargs) as client:
        return client.get(url, **kwargs)


def http_post(url: str, **kwargs) -> httpx.Response:
    client_kwargs = _client_kwargs(url, kwargs)
    with httpx.Client(**client_kwargs) as client:
        return client.post(url, **kwargs)


def _client_kwargs(url: str, request_kwargs: dict) -> dict:
    client_kwargs = {"trust_env": request_kwargs.pop("trust_env", should_trust_env(url))}
    for key in ("timeout", "follow_redirects", "verify", "cert", "http2"):
        if key in request_kwargs:
            client_kwargs[key] = request_kwargs.pop(key)
    return client_kwargs

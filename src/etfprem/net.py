"""最小化的 HTTP 封装：浏览器 UA、重试、404 单独抛出。"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    pass


class NotFound(FetchError):
    pass


def fetch(
    url: str,
    *,
    data: dict[str, Any] | bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    retries: int = 3,
    pause: float = 1.5,
) -> bytes:
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from e
            err = e
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            err = e
        time.sleep(pause * (attempt + 1))
    raise FetchError(f"{url} -> {err!r}")


def fetch_json(url: str, **kw: Any) -> Any:
    return json.loads(fetch(url, **kw))

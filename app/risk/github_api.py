"""Minimal GitHub REST client (read-only) used for mining CI history.

Token comes from GITHUB_TOKEN, falling back to `gh auth token`. The token is
never logged or included in exceptions.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)
API = "https://api.github.com"


def get_token() -> str:
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self._token = token if token is not None else get_token()

    def __repr__(self) -> str:
        return f"GitHubClient(authenticated={bool(self._token)})"

    def get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        for attempt in range(5):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                remaining = exc.headers.get("X-RateLimit-Remaining")
                if exc.code in (403, 429) and remaining == "0":
                    reset = int(exc.headers.get("X-RateLimit-Reset", time.time() + 60))
                    wait = max(1, min(reset - time.time() + 1, 900))
                    log.warning("GitHub rate limit hit; sleeping %.0fs", wait)
                    time.sleep(wait)
                    continue
                if exc.code >= 500 and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"GitHub API {exc.code} for {path}") from None
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError):
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"GitHub API request failed repeatedly: {path}")

    def paginate(self, path: str, key: str, max_items: int, per_page: int = 100) -> list[dict]:
        items: list[dict] = []
        page = 1
        while len(items) < max_items:
            data = self.get(path, {"per_page": per_page, "page": page})
            batch = data.get(key, [])
            if not batch:
                break
            items.extend(batch)
            page += 1
        return items[:max_items]

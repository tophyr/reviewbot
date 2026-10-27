"""GitHub App authentication and a small REST client.

The App's JSON Web Token (JWT) follows
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
(checked 2026-10-24): RS256, claims iat (set 60 seconds in the past), exp (no
more than 10 minutes in the future) and iss (client ID or app ID).

Installation tokens come from POST /app/installations/{id}/access_tokens
(201 Created, fields "token" and "expires_at"); they live for one hour. The
orchestrator's own token per installation is cached until 5 minutes before
expires_at. Tokens handed to the fetcher container are minted per review,
restricted to the one repository and to read permissions, and never cached.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import jwt
import requests

from .config import ConfigError
from .logutil import register_secret

log = logging.getLogger(__name__)

API_VERSION = "2026-03-10"
JWT_LIFETIME = 600  # seconds between iat and exp
JWT_IAT_SKEW = 60  # iat is set this many seconds in the past
TOKEN_REFRESH_MARGIN = 300  # refresh cached installation tokens this early
FALLBACK_TOKEN_LIFETIME = 3300  # used only if expires_at cannot be parsed
FETCH_TOKEN_PERMISSIONS = {"contents": "read", "pull_requests": "read"}


class GitHubError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status
        self.message = message


class GitHubAPI:
    """Minimal REST client. Never logs headers or bodies."""

    def __init__(self, base_url: str = "https://api.github.com",
                 session: requests.Session | None = None, timeout: int = 30,
                 retries: int = 2, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def _headers(self, token: str, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "reviewbot",
        }

    def request(self, method: str, path_or_url: str, token: str, *,
                json: Any = None, params: dict | None = None,
                expected: tuple[int, ...] = (200,),
                accept: str = "application/vnd.github+json") -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        attempt = 0
        while True:
            try:
                resp = self.session.request(
                    method, url, headers=self._headers(token, accept), json=json,
                    params=params, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt < self.retries and method == "GET":
                    attempt += 1
                    log.warning("GitHub %s %s failed (%s), retrying", method,
                                _strip_query(url), type(exc).__name__)
                    self.sleep(2 * attempt)
                    continue
                raise GitHubError(None, f"{method} {_strip_query(url)}: {type(exc).__name__}") from exc
            if resp.status_code in expected:
                return resp
            if resp.status_code >= 500 and attempt < self.retries and method == "GET":
                attempt += 1
                log.warning("GitHub %s %s returned %d, retrying", method,
                            _strip_query(url), resp.status_code)
                self.sleep(2 * attempt)
                continue
            raise GitHubError(resp.status_code, f"{method} {_strip_query(url)}: {_error_message(resp)}")

    def get_json(self, path: str, token: str, params: dict | None = None) -> Any:
        return self.request("GET", path, token, params=params).json()

    def paginate(self, path: str, token: str, params: dict | None = None,
                 item_key: str | None = None) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url: str | None = path
        first = True
        while url:
            resp = self.request("GET", url, token, params=params if first else None)
            first = False
            data = resp.json()
            items = data[item_key] if item_key else data
            yield from items
            url = resp.links.get("next", {}).get("url")


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def _error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        msg = str(data.get("message", ""))
        errors = data.get("errors")
        if errors:
            msg += f" errors={errors!r}"
        return msg[:500]
    except ValueError:
        return f"(non-JSON body, {len(resp.content)} bytes)"


def make_app_jwt(app_id: str, private_key_pem: str | bytes, now: float | None = None) -> str:
    now = int(time.time() if now is None else now)
    iat = now - JWT_IAT_SKEW
    payload = {"iat": iat, "exp": iat + JWT_LIFETIME, "iss": str(app_id)}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def parse_expires_at(value: str | None, now: float) -> float:
    if value:
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    log.warning("could not parse installation token expires_at %r; assuming %ds",
                value, FALLBACK_TOKEN_LIFETIME)
    return now + FALLBACK_TOKEN_LIFETIME


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class AppAuth:
    def __init__(self, api: GitHubAPI, app_id: str, private_key_pem: str | bytes,
                 clock: Callable[[], float] = time.time):
        self.api = api
        self.app_id = str(app_id)
        self._key = private_key_pem
        self.clock = clock
        self._cache: dict[int, _CachedToken] = {}
        self._repo_installation: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_key_file(cls, api: GitHubAPI, app_id: str, path) -> "AppAuth":
        # A key file that cannot be read is a configuration error (exit 2),
        # which restarting the service will not fix.
        try:
            with open(path, "rb") as fh:
                key = fh.read()
        except OSError as exc:
            raise ConfigError(f"REVIEWBOT_APP_PRIVATE_KEY_PATH {path}: {exc.strerror}") from exc
        return cls(api, app_id, key)

    def jwt(self) -> str:
        return make_app_jwt(self.app_id, self._key, now=self.clock())

    def app_info(self) -> dict:
        return self.api.get_json("/app", self.jwt())

    def list_installations(self) -> list[dict]:
        return list(self.api.paginate("/app/installations", self.jwt()))

    def _mint(self, installation_id: int, body: dict | None) -> tuple[str, float]:
        now = self.clock()
        resp = self.api.request(
            "POST", f"/app/installations/{installation_id}/access_tokens", self.jwt(),
            json=body, expected=(201,),
        )
        data = resp.json()
        token = data["token"]
        register_secret(token)
        return token, parse_expires_at(data.get("expires_at"), now)

    def installation_token(self, installation_id: int) -> str:
        """Token for the orchestrator itself (listing, posting). Cached."""
        with self._lock:
            cached = self._cache.get(installation_id)
            if cached and self.clock() < cached.expires_at - TOKEN_REFRESH_MARGIN:
                return cached.token
            token, expires = self._mint(installation_id, None)
            self._cache[installation_id] = _CachedToken(token, expires)
            log.info("minted installation token for installation %s", installation_id)
            return token

    def remember_repo(self, full_name: str, installation_id: int) -> None:
        self._repo_installation[full_name.lower()] = installation_id

    def installation_for_repo(self, full_name: str) -> int:
        key = full_name.lower()
        if key not in self._repo_installation:
            data = self.api.get_json(f"/repos/{full_name}/installation", self.jwt())
            self._repo_installation[key] = int(data["id"])
        return self._repo_installation[key]

    def token_for_repo(self, full_name: str) -> str:
        return self.installation_token(self.installation_for_repo(full_name))

    def fetch_token_for_repo(self, full_name: str) -> str:
        """Short-lived read-only token for one repository, for the fetcher."""
        name = full_name.split("/", 1)[1]
        token, _ = self._mint(
            self.installation_for_repo(full_name),
            {"repositories": [name], "permissions": dict(FETCH_TOKEN_PERMISSIONS)},
        )
        return token

    def installation_repos(self, installation_id: int) -> list[dict]:
        token = self.installation_token(installation_id)
        return list(self.api.paginate("/installation/repositories", token,
                                      item_key="repositories"))


def make_auth(config, api: GitHubAPI | None = None) -> AppAuth:
    api = api or GitHubAPI(config.api_url)
    return AppAuth.from_key_file(api, config.app_id, config.app_private_key_path)

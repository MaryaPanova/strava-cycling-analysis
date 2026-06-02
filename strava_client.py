"""Strava API v3 client.

Handles OAuth2 token storage / refresh, a thin request wrapper with
rate-limit awareness, and the handful of endpoints this project needs:
listing activities, fetching details + streams, creating manual
activities, and uploading GPX/TCX/FIT files.

Credentials come from a `.env` file (see `.env.example`):

    STRAVA_CLIENT_ID=12345
    STRAVA_CLIENT_SECRET=abcdef...

Tokens are cached in `strava_tokens.json` (gitignored) and refreshed
automatically when expired.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
TOKENS_PATH = PROJECT_ROOT / "strava_tokens.json"

API_BASE = "https://www.strava.com/api/v3"
OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
OAUTH_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"

# Scopes needed to read activities and write new ones / uploads.
DEFAULT_SCOPE = "read,activity:read_all,activity:write"

# Refresh a little before actual expiry to avoid races.
EXPIRY_SKEW_SECONDS = 120


# --------------------------------------------------------------------------- #
# Tiny .env loader (avoids a hard dependency on python-dotenv)
# --------------------------------------------------------------------------- #

def load_env(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Existing environment variables are not overwritten. Lines that are
    blank or start with '#' are ignored. Values may be quoted.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class StravaAuthError(RuntimeError):
    """Raised when credentials or tokens are missing/invalid."""


class StravaRateLimitError(RuntimeError):
    """Raised when the API rate limit is exhausted and cannot wait it out."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class StravaClient:
    def __init__(self, tokens_path: Path = TOKENS_PATH):
        load_env()
        self.client_id = os.environ.get("STRAVA_CLIENT_ID")
        self.client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise StravaAuthError(
                "Missing STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET. "
                "Copy .env.example to .env and fill them in."
            )
        self.tokens_path = tokens_path
        self.session = requests.Session()
        self._tokens = self._load_tokens()

    # ----- token persistence -------------------------------------------- #

    def _load_tokens(self) -> dict:
        if self.tokens_path.exists():
            return json.loads(self.tokens_path.read_text())
        return {}

    def _save_tokens(self, tokens: dict) -> None:
        self.tokens_path.write_text(json.dumps(tokens, indent=2))
        # Tokens are secrets — keep them owner-readable only.
        try:
            self.tokens_path.chmod(0o600)
        except OSError:
            pass
        self._tokens = tokens

    @property
    def is_authenticated(self) -> bool:
        return bool(self._tokens.get("refresh_token"))

    # ----- OAuth ---------------------------------------------------------- #

    def authorization_url(self, redirect_uri: str, scope: str = DEFAULT_SCOPE) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": scope,
        }
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict:
        """Exchange an authorization code for access + refresh tokens."""
        resp = self.session.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        resp.raise_for_status()
        tokens = resp.json()
        self._save_tokens(tokens)
        return tokens

    def _refresh(self) -> None:
        refresh_token = self._tokens.get("refresh_token")
        if not refresh_token:
            raise StravaAuthError("No refresh token. Run `python auth.py` first.")
        resp = self.session.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        # Strava returns a fresh access token, expiry, and (sometimes a new)
        # refresh token. Merge so we never lose fields.
        self._save_tokens({**self._tokens, **resp.json()})

    def _access_token(self) -> str:
        if not self._tokens.get("refresh_token"):
            raise StravaAuthError("Not authenticated. Run `python auth.py` first.")
        expires_at = self._tokens.get("expires_at", 0)
        if time.time() >= expires_at - EXPIRY_SKEW_SECONDS:
            self._refresh()
        return self._tokens["access_token"]

    # ----- request wrapper ----------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Authenticated request with rate-limit handling.

        Retries once after waiting when a 429 is returned, and proactively
        sleeps when the 15-minute window is nearly exhausted.
        """
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token()}"

        for attempt in range(2):
            resp = self.session.request(method, url, headers=headers, timeout=60, **kwargs)
            self._respect_rate_limits(resp)
            if resp.status_code == 429:
                if attempt == 0:
                    wait = self._seconds_until_window_reset()
                    print(f"  Rate limited — waiting {wait}s for the window to reset…")
                    time.sleep(wait)
                    continue
                raise StravaRateLimitError("Rate limit hit; try again later.")
            resp.raise_for_status()
            return resp
        raise StravaRateLimitError("Rate limit hit; try again later.")

    @staticmethod
    def _seconds_until_window_reset() -> int:
        # Strava's short window is 15 minutes, aligned to the clock quarter-hour.
        now = time.time()
        return int(900 - (now % 900)) + 1

    def _respect_rate_limits(self, resp: requests.Response) -> None:
        """Slow down when close to the short-window limit.

        Strava sends `X-RateLimit-Limit` and `X-RateLimit-Usage` as
        "15min,daily" pairs.
        """
        limit = resp.headers.get("X-RateLimit-Limit")
        usage = resp.headers.get("X-RateLimit-Usage")
        if not limit or not usage:
            return
        try:
            short_limit = int(limit.split(",")[0])
            short_usage = int(usage.split(",")[0])
        except (ValueError, IndexError):
            return
        if short_usage >= short_limit - 1:
            wait = self._seconds_until_window_reset()
            print(f"  Approaching rate limit ({short_usage}/{short_limit}) — "
                  f"pausing {wait}s.")
            time.sleep(wait)

    # ----- endpoints ------------------------------------------------------ #

    def get_athlete(self) -> dict:
        return self._request("GET", "/athlete").json()

    def list_activities(self, after: int | None = None, before: int | None = None,
                        per_page: int = 100) -> list[dict]:
        """Yield all activities (paginated) matching the time window.

        `after` / `before` are epoch seconds.
        """
        activities: list[dict] = []
        page = 1
        while True:
            params = {"per_page": per_page, "page": page}
            if after is not None:
                params["after"] = after
            if before is not None:
                params["before"] = before
            batch = self._request("GET", "/athlete/activities", params=params).json()
            if not batch:
                break
            activities.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return activities

    def get_activity(self, activity_id: int) -> dict:
        return self._request("GET", f"/activities/{activity_id}").json()

    def get_streams(self, activity_id: int,
                    keys: list[str] | None = None) -> dict:
        keys = keys or ["time", "latlng", "heartrate", "watts", "cadence", "altitude"]
        params = {"keys": ",".join(keys), "key_by_type": "true"}
        return self._request("GET", f"/activities/{activity_id}/streams",
                             params=params).json()

    def create_activity(self, name: str, sport_type: str, start_date_local: str,
                        elapsed_time: int, distance: float | None = None,
                        description: str | None = None,
                        trainer: bool = False) -> dict:
        """Create a manual activity.

        `start_date_local` is ISO 8601 (e.g. 2026-05-30T07:00:00Z),
        `elapsed_time` is seconds, `distance` is meters.
        """
        data = {
            "name": name,
            "sport_type": sport_type,
            "start_date_local": start_date_local,
            "elapsed_time": elapsed_time,
        }
        if distance is not None:
            data["distance"] = distance
        if description:
            data["description"] = description
        if trainer:
            data["trainer"] = 1
        return self._request("POST", "/activities", data=data).json()

    def upload_file(self, file_path: Path, data_type: str,
                    name: str | None = None, description: str | None = None,
                    activity_type: str | None = None,
                    trainer: bool = False) -> dict:
        """Upload a GPX/TCX/FIT file (optionally gzipped).

        `data_type` is one of: gpx, gpx.gz, tcx, tcx.gz, fit, fit.gz.
        """
        file_path = Path(file_path)
        data = {"data_type": data_type}
        if name:
            data["name"] = name
        if description:
            data["description"] = description
        if activity_type:
            data["activity_type"] = activity_type
        if trainer:
            data["trainer"] = 1
        with open(file_path, "rb") as fh:
            files = {"file": (file_path.name, fh)}
            return self._request("POST", "/uploads", data=data, files=files).json()

    def get_upload(self, upload_id: int) -> dict:
        return self._request("GET", f"/uploads/{upload_id}").json()

    def wait_for_upload(self, upload_id: int, timeout: int = 120,
                        poll_interval: int = 3) -> dict:
        """Poll an upload until Strava finishes processing it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_upload(upload_id)
            if status.get("error"):
                raise RuntimeError(f"Upload failed: {status['error']}")
            if status.get("activity_id"):
                return status
            time.sleep(poll_interval)
        raise TimeoutError("Upload still processing after timeout.")

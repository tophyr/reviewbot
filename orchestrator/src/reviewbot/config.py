"""Configuration read from environment variables (see DESIGN.md)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


def _int(env: dict[str, str], name: str, default: int, minimum: int = 0,
         maximum: int | None = None) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _reviewer_id(env: dict[str, str], name: str) -> int | None:
    """REVIEWBOT_REVIEWER_UID or _GID: unset, or a positive integer. 0 is
    refused because the contract requires a non-root reviewer and this value
    is used without inspecting the image."""
    raw = env.get(name, "").strip()
    if not raw:
        return None
    value = _int(env, name, 0)
    if value == 0:
        raise ConfigError(f"{name} must not be 0 (root); the reviewer must run as a non-root user")
    return value


# The pattern reviewer/entrypoint.sh applies to REVIEWBOT_MODEL. A value it
# would refuse is refused here at startup instead of in every review.
MODEL_RE = re.compile(r"[A-Za-z0-9][]A-Za-z0-9._:@[-]{0,99}")


def _model(env: dict[str, str]) -> str:
    value = env.get("REVIEWBOT_MODEL") or "opus"  # the entrypoint also maps empty to opus
    if not MODEL_RE.fullmatch(value):
        raise ConfigError(f"REVIEWBOT_MODEL {value!r} is not accepted by the reviewer entrypoint "
                          "(letters, digits and ._:@[]- only, not starting with a symbol, "
                          "at most 100 characters)")
    return value


# Docker names derived from the prefix (images "<p>-proxy", networks
# "<p>_internal") must stay valid image tags and container names.
DOCKER_PREFIX_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class DockerNames:
    """The images, networks and container names one orchestrator uses, all
    derived from REVIEWBOT_DOCKER_PREFIX."""
    prefix: str

    @property
    def proxy_image(self) -> str:
        return f"{self.prefix}-proxy"

    @property
    def reviewer_image(self) -> str:
        return f"{self.prefix}-reviewer"

    @property
    def fetcher_image(self) -> str:
        return f"{self.prefix}-fetcher"

    @property
    def images(self) -> tuple[str, str, str]:
        return (self.proxy_image, self.reviewer_image, self.fetcher_image)

    @property
    def internal_network(self) -> str:
        return f"{self.prefix}_internal"

    @property
    def default_network(self) -> str:
        return f"{self.prefix}_default"


def docker_names(env: dict[str, str]) -> DockerNames:
    # Only an unset variable means the default: an empty value is refused, so
    # that a lost value never silently selects the deployment's names.
    value = env.get("REVIEWBOT_DOCKER_PREFIX", "reviewbot")
    if not DOCKER_PREFIX_RE.fullmatch(value):
        raise ConfigError(f"REVIEWBOT_DOCKER_PREFIX {value!r} must match ^[a-z][a-z0-9-]{{0,31}}$")
    return DockerNames(value)


def _flag(env: dict[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in ("1", "true", "yes")


# The review instructions reach the reviewer in one environment variable,
# and Linux refuses any single environment string over 128 KiB.
MAX_PROMPT_BYTES = 64 * 1024


def read_prompt_file(path: Path) -> str:
    """The review instructions in REVIEWBOT_PROMPT_FILE. Raises ConfigError
    when the file cannot be read, is empty, is larger than MAX_PROMPT_BYTES or
    is not UTF-8."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"REVIEWBOT_PROMPT_FILE {path}: {exc.strerror}") from exc
    if len(data) > MAX_PROMPT_BYTES:
        raise ConfigError(f"REVIEWBOT_PROMPT_FILE {path} is {len(data)} bytes, "
                          f"limit {MAX_PROMPT_BYTES}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"REVIEWBOT_PROMPT_FILE {path} is not UTF-8") from exc
    if not text.strip():
        raise ConfigError(f"REVIEWBOT_PROMPT_FILE {path} is empty")
    return text


@dataclass
class Config:
    app_id: str
    app_private_key_path: Path
    poll_seconds: int
    max_reviews_per_hour: int
    state_dir: Path
    work_dir: Path
    dry_run: bool
    keep_work: bool
    model: str
    max_turns: int
    reviewer_uid: int | None
    reviewer_gid: int | None
    docker: DockerNames
    api_url: str = "https://api.github.com"
    # Replaces the reviewer image's review instructions; read for every review.
    prompt_file: Path | None = None


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    app_id = env.get("REVIEWBOT_APP_ID", "").strip()
    key_path_raw = env.get("REVIEWBOT_APP_PRIVATE_KEY_PATH", "").strip()
    if not app_id or not key_path_raw:
        raise ConfigError("reviewbot needs REVIEWBOT_APP_ID and REVIEWBOT_APP_PRIVATE_KEY_PATH")
    key_path = Path(key_path_raw).expanduser()

    work_dir = Path(env.get("REVIEWBOT_WORK_DIR", "./work")).expanduser().resolve()
    # The work directory is passed to docker as "-v WORK:/work:ro"; a colon or
    # comma in the path would change the meaning of that flag.
    if any(c in str(work_dir) for c in ":,"):
        raise ConfigError(f"REVIEWBOT_WORK_DIR must not contain ':' or ',': {work_dir}")

    prompt_raw = env.get("REVIEWBOT_PROMPT_FILE", "").strip()
    prompt_file = Path(prompt_raw).expanduser() if prompt_raw else None
    if prompt_file is not None:
        read_prompt_file(prompt_file)

    return Config(
        app_id=app_id,
        app_private_key_path=key_path,
        poll_seconds=_int(env, "REVIEWBOT_POLL_SECONDS", 180, minimum=10),
        max_reviews_per_hour=_int(env, "REVIEWBOT_MAX_REVIEWS_PER_HOUR", 6),
        state_dir=Path(env.get("REVIEWBOT_STATE_DIR", "./state")).expanduser().resolve(),
        work_dir=work_dir,
        dry_run=_flag(env, "REVIEWBOT_DRY_RUN"),
        keep_work=_flag(env, "REVIEWBOT_KEEP_WORK"),
        model=_model(env),
        # reviewer/entrypoint.sh accepts 1 to 9999; refuse anything else at
        # startup rather than in every review.
        max_turns=_int(env, "REVIEWBOT_MAX_TURNS", 40, minimum=1, maximum=9999),
        reviewer_uid=_reviewer_id(env, "REVIEWBOT_REVIEWER_UID"),
        reviewer_gid=_reviewer_id(env, "REVIEWBOT_REVIEWER_GID"),
        docker=docker_names(env),
        api_url=env.get("REVIEWBOT_GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        prompt_file=prompt_file,
    )

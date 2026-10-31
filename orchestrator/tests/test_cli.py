import io
import logging
import sys

import pytest
from fakedocker import install

from reviewbot import logutil, main
from reviewbot.config import ConfigError, load_config
from reviewbot.github_auth import GitHubError


@pytest.fixture
def env(monkeypatch, tmp_path, app_key_path):
    for k in list(__import__("os").environ):
        if k.startswith("REVIEWBOT_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("REVIEWBOT_APP_ID", "1")
    monkeypatch.setenv("REVIEWBOT_APP_PRIVATE_KEY_PATH", str(app_key_path))
    monkeypatch.setenv("REVIEWBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REVIEWBOT_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "r" * 40)
    install(tmp_path, monkeypatch)  # the startup checks run docker
    monkeypatch.setattr(main, "_log_identity", lambda auth: None)  # no network in tests
    return monkeypatch


class _NoInstallationAPI:
    """GitHub's answer for a repository that no installation of the App covers."""

    def __init__(self, base_url):
        self.paths = []

    def get_json(self, path, token, params=None):
        self.paths.append(path)
        raise GitHubError(404, f"GET {path}: Not Found")


def test_review_outside_every_installation_exits_1_with_one_line(env, capsys, monkeypatch):
    apis = []

    def make_api(url):
        apis.append(_NoInstallationAPI(url))
        return apis[-1]
    monkeypatch.setattr(main, "GitHubAPI", make_api)
    assert main.main(["review", "other/repo#1"]) == 1
    err = capsys.readouterr().err
    assert [a.paths for a in apis] == [["/repos/other/repo/installation"]]
    errors = [line for line in err.splitlines() if "ERROR" in line]
    assert len(errors) == 1, err
    assert "GitHub API error 404: GET /repos/other/repo/installation: Not Found" in errors[0]
    assert "Traceback" not in err


def test_bad_ref(env, capsys):
    assert main.main(["review", "o/r/1"]) == 2
    assert "expected owner/name#number" in capsys.readouterr().err


def test_missing_app_id_and_key_exit_2(env, capsys):
    env.delenv("REVIEWBOT_APP_ID")
    env.delenv("REVIEWBOT_APP_PRIVATE_KEY_PATH")
    assert main.main(["run", "--once"]) == 2
    assert "REVIEWBOT_APP_ID" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["REVIEWBOT_APP_ID", "REVIEWBOT_APP_PRIVATE_KEY_PATH"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_load_config_without_app_setting_fails(env, name, value):
    if value is None:
        env.delenv(name)
    else:
        env.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        load_config()


def test_log_redaction(capsys):
    logutil.setup_logging("INFO")
    secret = "ghs_" + "z" * 36
    logutil.register_secret(secret)
    logging.getLogger("t").error("stderr tail: token=%s end", secret)
    err = capsys.readouterr().err
    assert secret not in err
    assert "token=[REDACTED] end" in err


def _journal_id(fh):
    st = __import__("os").fstat(fh.fileno())
    return f"{st.st_dev}:{st.st_ino}"


def test_stderr_counts_as_the_journal_only_when_journal_stream_matches_it(monkeypatch, tmp_path):
    with open(tmp_path / "a", "w") as a, open(tmp_path / "b", "w") as b:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        assert not logutil.stream_is_journal(a)
        monkeypatch.setenv("JOURNAL_STREAM", _journal_id(a))
        assert logutil.stream_is_journal(a)
        assert not logutil.stream_is_journal(b)  # inherited variable, other stderr
    assert not logutil.stream_is_journal(io.StringIO())  # no file descriptor


def test_journal_format_prefixes_every_line_with_the_priority():
    fmt = logutil.JournalFormatter("%(message)s")

    def line(level, msg):
        return fmt.format(logging.LogRecord("t", level, __file__, 1, msg, None, None))
    assert line(logging.INFO, "posted") == "<6>posted"
    assert line(logging.WARNING, "retrying") == "<4>retrying"
    # a container's output that starts with a prefix cannot change its priority
    assert line(logging.ERROR, "fetcher stderr tail:\n<0>forged\nlast") == \
        "<3>fetcher stderr tail:\n<3><0>forged\n<3>last"


def test_logging_to_the_journal_writes_only_the_message(monkeypatch, tmp_path):
    secret = "registered-secret-" + "j" * 20
    # setup_logging fills the root logger's handler list in place; a list of
    # this test's own keeps its handler, and the file it writes, from outliving it
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    with open(tmp_path / "journal", "w") as fh:
        monkeypatch.setattr(sys, "stderr", fh)
        monkeypatch.setenv("JOURNAL_STREAM", _journal_id(fh))
        logutil.setup_logging("INFO")
        logutil.register_secret(secret)
        logging.getLogger("reviewbot.pipeline").warning("token=%s end", secret)
    assert (tmp_path / "journal").read_text() == "<4>token=[REDACTED] end\n"


@pytest.mark.parametrize("name", ["REVIEWBOT_REVIEWER_UID", "REVIEWBOT_REVIEWER_GID"])
def test_reviewer_uid_or_gid_0_is_a_config_error(env, capsys, name):
    env.setenv("REVIEWBOT_REVIEWER_UID", "10001")
    env.setenv(name, "0")
    assert main.main(["run", "--once"]) == 2
    assert f"{name} must not be 0 (root)" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "10000", "-1"])
def test_max_turns_outside_entrypoint_range_is_a_config_error(env, capsys, value):
    # reviewer/entrypoint.sh accepts 1..9999; config must refuse the rest at startup
    env.setenv("REVIEWBOT_MAX_TURNS", value)
    assert main.main(["run", "--once"]) == 2
    assert "REVIEWBOT_MAX_TURNS must be" in capsys.readouterr().err


def test_max_turns_range_ends_are_accepted(env):
    for value in ("1", "9999"):
        env.setenv("REVIEWBOT_MAX_TURNS", value)
        assert load_config().max_turns == int(value)


def test_log_redaction_of_exception_tracebacks(capsys):
    logutil.setup_logging("INFO")
    secret = "registered-secret-" + "k" * 20
    logutil.register_secret(secret)
    try:
        raise RuntimeError(f"upstream said {secret}")
    except RuntimeError:
        logging.getLogger("t").exception("review failed")
    # stack_info prints source lines; this one holds the secret literally
    logging.getLogger("t").error("with stack", stack_info=True); assert secret == "registered-secret-kkkkkkkkkkkkkkkkkkkk"  # noqa: E501,E702
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert "RuntimeError: upstream said [REDACTED]" in err
    assert "Stack (most recent call last)" in err
    assert 'assert secret == "[REDACTED]"' in err
    assert secret not in err


def test_exception_reaching_main_is_logged_redacted(env, capsys, monkeypatch):
    secret = "registered-secret-" + "u" * 20
    logutil.register_secret(secret)

    def boom(ctx, **_):
        raise KeyError(f"bad field near {secret}")
    monkeypatch.setattr(main, "cmd_run", boom)
    assert main.main(["run", "--once"]) == 1
    err = capsys.readouterr().err
    assert "exiting" in err and "KeyError" in err
    assert secret not in err


@pytest.mark.parametrize("value", ["-p", "]x", "opus;rm", "a b", "a" * 101, "opus\n"])
def test_model_the_entrypoint_would_refuse_is_a_config_error(env, capsys, value):
    env.setenv("REVIEWBOT_MODEL", value)
    assert main.main(["run", "--once"]) == 2
    assert "REVIEWBOT_MODEL" in capsys.readouterr().err


def test_models_the_entrypoint_accepts_are_accepted(env):
    for value in ("opus", "claude-opus-4-1[1m]", "sonnet", "a" * 100, "us.anthropic.x:0@y"):
        env.setenv("REVIEWBOT_MODEL", value)
        assert load_config().model == value
    env.setenv("REVIEWBOT_MODEL", "")
    assert load_config().model == "opus"


@pytest.mark.parametrize("content, message", [
    (None, "No such file"),
    (b"", "is empty"),
    (b"\xff\xfe", "is not UTF-8"),
    (b"x" * (64 * 1024 + 1), "limit 65536"),
], ids=["missing", "empty", "not-utf8", "too-large"])
def test_unusable_prompt_file_is_a_config_error(env, capsys, tmp_path, content, message):
    prompt = tmp_path / "review.md"
    if content is not None:
        prompt.write_bytes(content)
    env.setenv("REVIEWBOT_PROMPT_FILE", str(prompt))
    assert main.main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert "REVIEWBOT_PROMPT_FILE" in err and message in err


def test_prompt_file_is_optional(env, tmp_path):
    assert load_config().prompt_file is None
    prompt = tmp_path / "review.md"
    prompt.write_text("Focus on error handling.\n")
    env.setenv("REVIEWBOT_PROMPT_FILE", str(prompt))
    assert load_config().prompt_file == prompt


# Startup errors for configured paths exit 2, which reviewbot.service lists in
# RestartPreventExitStatus, so systemd does not restart a service that cannot
# work until its configuration is fixed.

def test_missing_private_key_file_is_a_config_error(env, capsys, tmp_path):
    env.setenv("REVIEWBOT_APP_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    assert main.main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert "REVIEWBOT_APP_PRIVATE_KEY_PATH" in err and "No such file or directory" in err


class _IdentityFake:
    """Responses carrying an extra secret-bearing field, which the identity
    log line must not include."""

    SECRET = "identity-secret-" + "s" * 20

    def __init__(self, error=None):
        self.error = error

    def app_info(self):
        if self.error:
            raise self.error
        return {"id": 1, "slug": "reviewbot", "name": "Reviewbot", "pem": self.SECRET}

    def list_installations(self):
        return [{"id": 2, "account": {"login": "owner"}, "repository_selection": "selected",
                 "access_token": self.SECRET}]


def test_identity_is_logged_without_secrets(capsys):
    logutil.setup_logging("INFO")
    main._log_identity(_IdentityFake())
    err = capsys.readouterr().err
    assert "GitHub App reviewbot (id 1); installations: owner (selected)" in err
    # No [REDACTED] either: redaction would hide a secret that reached the log call.
    assert "[REDACTED]" not in err and _IdentityFake.SECRET not in err


def test_identity_failure_is_logged_and_startup_continues(capsys):
    logutil.setup_logging("INFO")
    main._log_identity(_IdentityFake(error=GitHubError(401, "GET /app: Bad credentials")))
    assert "could not read the GitHub identity: GitHub API error 401" in capsys.readouterr().err


def test_unreadable_private_key_file_is_a_config_error(env, capsys, tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("x")
    key.chmod(0)
    env.setenv("REVIEWBOT_APP_PRIVATE_KEY_PATH", str(key))
    assert main.main(["run", "--once"]) == 2
    assert "Permission denied" in capsys.readouterr().err


# Configuration errors exit 2 at startup, before any pass runs.

def _no_pass(monkeypatch):
    from reviewbot import pipeline

    def one_pass(ctx):
        raise AssertionError("a pass ran despite the configuration error")
    monkeypatch.setattr(pipeline, "one_pass", one_pass)


def test_known_log_level_in_any_case_is_accepted(env, capsys):
    # the level is accepted and startup goes on to the next check
    env.setenv("REVIEWBOT_LOG_LEVEL", "warning")
    env.delenv("REVIEWBOT_APP_ID")
    assert main.main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert "REVIEWBOT_APP_ID" in err and "Unknown level" not in err


# The orchestrator needs the real token for its leak checks.
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_claude_token_is_a_config_error(env, capsys, value):
    if value is None:
        env.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    else:
        env.setenv("CLAUDE_CODE_OAUTH_TOKEN", value)
    _no_pass(env)
    assert main.main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert "CLAUDE_CODE_OAUTH_TOKEN is not set or empty" in err
    assert "~/.config/reviewbot/env" in err


def test_claude_token_builds_the_context(env):
    env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "r" * 40)
    ctx = main._context(load_config(), auth=None, state=None)
    assert ctx.real_token == "sk-ant-oat01-" + "r" * 40

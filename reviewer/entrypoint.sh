#!/bin/bash
# reviewbot review container entrypoint. Takes no arguments.
#
# Runs the claude command from DESIGN.md ("Review container contract")
# with /work as the working directory. stdout is exactly what
# "claude -p --output-format json" prints; anything this script reports goes
# to stderr.
set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "reviewbot-reviewer: the entrypoint takes no arguments" >&2
  exit 64
fi

# The orchestrator passes both on every run; their defaults are in its
# config.py, and the image sets neither.
for name in REVIEWBOT_MODEL REVIEWBOT_MAX_TURNS; do
  if [ -z "${!name:-}" ]; then
    echo "reviewbot-reviewer: $name is not set; the orchestrator passes it" >&2
    exit 64
  fi
done

# Both values are passed as single argv entries, so they cannot inject shell
# syntax. These checks stop a value that starts with "-" from being parsed
# as another option.
if ! [[ "$REVIEWBOT_MAX_TURNS" =~ ^[1-9][0-9]{0,3}$ ]]; then
  echo "reviewbot-reviewer: REVIEWBOT_MAX_TURNS must be an integer from 1 to 9999" >&2
  exit 64
fi
model_re='^[A-Za-z0-9][]A-Za-z0-9._:@[-]{0,99}$'
if ! [[ "$REVIEWBOT_MODEL" =~ $model_re ]]; then
  echo "reviewbot-reviewer: REVIEWBOT_MODEL has unexpected characters" >&2
  exit 64
fi

for f in /work/pr.json /work/diff.patch /work/files.txt; do
  if [ ! -f "$f" ]; then
    echo "reviewbot-reviewer: missing $f" >&2
    exit 66
  fi
done
if [ ! -d /work/repo ]; then
  echo "reviewbot-reviewer: missing /work/repo" >&2
  exit 66
fi

# A stand-in that the proxy replaces with the real token. It is set here and
# not with ENV in the Dockerfile, where docker build's SecretsUsedInArgOrEnv
# check flags it. Exporting it also overrides any value passed with -e.
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-dummy

# The system prompt: the fixed rules, then the review instructions. Those are
# REVIEWBOT_PROMPT, which the orchestrator sets from REVIEWBOT_PROMPT_FILE,
# or else the image's default.
{
  cat /opt/reviewbot/rules.md
  printf '\n'
  if [ -n "${REVIEWBOT_PROMPT:-}" ]; then
    printf '%s\n' "$REVIEWBOT_PROMPT"
  else
    cat /opt/reviewbot/review.md
  fi
} > /tmp/reviewbot-prompt.md
unset REVIEWBOT_PROMPT

schema="$(cat /opt/reviewbot/schema.json)"

cd /work
# --max-turns and --append-system-prompt-file are hidden flags in Claude Code
# 2.1.310: they work but `claude --help` does not list them.
exec /usr/local/bin/claude -p --restricted --tools Read,Grep,Glob \
  --permission-prompts none --permission-mode dontAsk \
  --setting-sources "" --strict-mcp-config --disable-slash-commands \
  --no-session-persistence --max-turns "$REVIEWBOT_MAX_TURNS" --model "$REVIEWBOT_MODEL" \
  --output-format json --json-schema "$schema" \
  --append-system-prompt-file /tmp/reviewbot-prompt.md \
  "Review the pull request described in /work/pr.json. The diff is /work/diff.patch and the checkout is /work/repo."

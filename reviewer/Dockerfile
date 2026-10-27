# reviewbot-reviewer: Claude Code native build, pinned, running as a non-root user.
#
# The binary is fetched from the release bucket used by the native installer
# (https://downloads.claude.ai/claude-code-releases), and verified as the
# "Binary integrity and code signing" section of
# https://code.claude.com/docs/en/setup describes: the release manifest's GPG
# signature is checked against the published release key fingerprint, and
# the binary's SHA256 is checked against the manifest. The expected checksum
# is also pinned here, so a re-signed manifest for the same version cannot
# silently change the binary. The installer script (install.sh) is not used
# because it installs under $HOME, which the reviewer's /home/reviewer tmpfs
# hides.

ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad

FROM ${DEBIAN_IMAGE} AS download
ARG CLAUDE_VERSION=2.1.310
ARG CLAUDE_PLATFORM=linux-x64
ARG CLAUDE_SHA256=b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56
ARG RELEASE_FINGERPRINT=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg jq \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
RUN set -eux; \
    base="https://downloads.claude.ai/claude-code-releases"; \
    export GNUPGHOME=/dl/gnupg; install -d -m 0700 "$GNUPGHOME"; \
    curl -fsSL -o key.asc https://downloads.claude.ai/keys/claude-code.asc; \
    gpg --batch --import key.asc; \
    gpg --batch --with-colons --fingerprint > fpr.txt; \
    grep -qx "fpr:::::::::${RELEASE_FINGERPRINT}:" fpr.txt; \
    curl -fsSL -o manifest.json "$base/${CLAUDE_VERSION}/manifest.json"; \
    curl -fsSL -o manifest.json.sig "$base/${CLAUDE_VERSION}/manifest.json.sig"; \
    gpg --batch --status-fd 1 --verify manifest.json.sig manifest.json > verify.txt; \
    grep -q "^\[GNUPG:\] VALIDSIG ${RELEASE_FINGERPRINT} " verify.txt; \
    test "$(jq -r .version manifest.json)" = "${CLAUDE_VERSION}"; \
    test "$(jq -r --arg p "${CLAUDE_PLATFORM}" '.platforms[$p].checksum' manifest.json)" = "${CLAUDE_SHA256}"; \
    curl -fsSL -o claude "$base/${CLAUDE_VERSION}/${CLAUDE_PLATFORM}/claude"; \
    echo "${CLAUDE_SHA256}  claude" | sha256sum -c -; \
    chmod 0755 claude

FROM ${DEBIAN_IMAGE}
ARG CLAUDE_VERSION=2.1.310
ARG REVIEWER_UID=10001
LABEL org.opencontainers.image.title="reviewbot-reviewer" \
      reviewbot.claude_code_version="${CLAUDE_VERSION}"
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd -g "${REVIEWER_UID}" reviewer \
 && useradd -u "${REVIEWER_UID}" -g reviewer -d /home/reviewer -s /usr/sbin/nologin -m reviewer
COPY --from=download /dl/claude /usr/local/bin/claude
RUN install -d -m 0755 /opt/reviewbot
# The schema belongs to the orchestrator package (orchestrator/src/reviewbot),
# which validates the review against it; build-images.sh passes that directory
# as the build context named "schema".
COPY --chmod=0444 rules.md review.md /opt/reviewbot/
COPY --from=schema --chmod=0444 schema.json /opt/reviewbot/
COPY --chmod=0555 entrypoint.sh /opt/reviewbot/entrypoint.sh
RUN test "$(/usr/local/bin/claude --version)" = "${CLAUDE_VERSION} (Claude Code)"

# The constants of the reviewer's environment; the orchestrator passes only
# REVIEWBOT_MODEL and REVIEWBOT_MAX_TURNS, and entrypoint.sh sets the stand-in
# CLAUDE_CODE_OAUTH_TOKEN. ANTHROPIC_BASE_URL is the "proxy" alias on the
# internal network.
ENV ANTHROPIC_BASE_URL=http://proxy:8080 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    ENABLE_CLAUDEAI_MCP_SERVERS=false \
    CLAUDE_CODE_DISABLE_ARTIFACT=1 \
    REVIEWBOT_MODEL=opus \
    REVIEWBOT_MAX_TURNS=40

USER ${REVIEWER_UID}:${REVIEWER_UID}
WORKDIR /work
ENTRYPOINT ["/opt/reviewbot/entrypoint.sh"]

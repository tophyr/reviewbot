# reviewbot-fetcher: checks out one pull request into /work.
FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

RUN apk add --no-cache git jq ca-certificates
COPY --chmod=0555 entrypoint.sh /usr/local/bin/reviewbot-fetch

# Ignore every git config file except the -c options the entrypoint passes.
ENV GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_TERMINAL_PROMPT=0 \
    GIT_ASKPASS=/bin/false \
    SSH_ASKPASS=/bin/false \
    GIT_LFS_SKIP_SMUDGE=1

# Runs as uid 0 inside the container on purpose. Under rootless docker that
# is the unprivileged host user who owns the work directory, so the fetcher
# can write /work and the orchestrator can read and chmod the result. A
# non-root container uid maps to a host subuid that cannot write the
# directory. The orchestrator also drops all capabilities and sets
# no-new-privileges.
ENTRYPOINT ["/usr/local/bin/reviewbot-fetch"]

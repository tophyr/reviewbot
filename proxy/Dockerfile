# reviewbot-proxy: header-rewriting reverse proxy for the Anthropic API.
FROM python:3.12-alpine3.24@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d

RUN addgroup -S -g 10002 proxy && adduser -S -D -H -u 10002 -G proxy proxy
RUN install -d -m 0755 /opt/reviewbot-proxy
COPY --chmod=0444 proxy.py /opt/reviewbot-proxy/proxy.py

USER 10002:10002
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
ENTRYPOINT ["python3", "-I", "/opt/reviewbot-proxy/proxy.py"]

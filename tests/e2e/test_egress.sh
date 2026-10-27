#!/bin/bash
# e2e egress test (TESTING.md, "Test inventory").
#
# A container on the internal network must not reach https://api.anthropic.com
# or http://example.com, by name or by IP address, and must reach
# http://proxy:8080. The proxy must refuse to act as a forward proxy.
# Control: the same probes from the normal bridge network do reach the
# internet, which shows the probes can detect reachability at all.
#
# The networks are the e2e prefix's ($INTERNAL_NET and $DEFAULT_NET in
# lib.sh), created by the orchestrator's own dockerrun.ensure_networks, as
# the deployment's are.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

load_real_token
if [ "${REVIEWBOT_E2E_SKIP_BUILD:-0}" != 1 ]; then build_images; fi
proxy_up

# Resolve IPs on the host so the direct-IP probes do not depend on DNS
# inside the internal network.
api_ip="$(python3 -I -c 'import socket; print(socket.gethostbyname("api.anthropic.com"))')"
ex_ip="$(python3 -I -c 'import socket; print(socket.gethostbyname("example.com"))')"
log "host resolves api.anthropic.com=$api_ip example.com=$ex_ip"

# Control on the normal bridge network.
r="$(probe_http "$DEFAULT_NET" "http://example.com/")"
case "$r" in 200|3*) pass "control: bridge network reaches http://example.com ($r)";;
  *) fail "control: bridge network cannot reach http://example.com ($r); the probes below prove nothing";; esac
r="$(probe_tcp "$DEFAULT_NET" "$api_ip" 443)"
[ "$r" = OPEN ] && pass "control: bridge network reaches $api_ip:443" || fail "control: bridge network cannot reach $api_ip:443 ($r)"

# Internal network.
r="$(probe_http "$INTERNAL_NET" "https://api.anthropic.com/")"
case "$r" in ERR:*) pass "internal: https://api.anthropic.com unreachable ($r)";; *) fail "internal: https://api.anthropic.com answered $r";; esac
r="$(probe_http "$INTERNAL_NET" "http://example.com/")"
case "$r" in ERR:*) pass "internal: http://example.com unreachable ($r)";; *) fail "internal: http://example.com answered $r";; esac
r="$(probe_tcp "$INTERNAL_NET" "$api_ip" 443)"
[ "$r" != OPEN ] && pass "internal: $api_ip:443 (api.anthropic.com by IP) unreachable ($r)" || fail "internal: $api_ip:443 reachable"
r="$(probe_tcp "$INTERNAL_NET" "$ex_ip" 80)"
[ "$r" != OPEN ] && pass "internal: $ex_ip:80 (example.com by IP) unreachable ($r)" || fail "internal: $ex_ip:80 reachable"
r="$(probe_tcp "$INTERNAL_NET" 1.1.1.1 53)"
[ "$r" != OPEN ] && pass "internal: 1.1.1.1:53 unreachable ($r)" || fail "internal: 1.1.1.1:53 reachable"

# The proxy is reachable, and denies anything not allowlisted.
r="$(probe_http "$INTERNAL_NET" "http://proxy:8080/")"
[ "$r" = 403 ] && pass "internal: http://proxy:8080/ reachable, returns 403" || fail "internal: http://proxy:8080/ gave $r"
r="$(probe_http "$INTERNAL_NET" "http://proxy:8080/v1/models")"
[ "$r" = 403 ] && pass "internal: proxy denies GET /v1/models (403)" || fail "internal: proxy GET /v1/models gave $r"

# The proxy must not work as a forward proxy (absolute-form request or CONNECT).
fwd="$(docker run --rm --network "$INTERNAL_NET" --entrypoint python3 "$PROXY_IMAGE" -I -c '
import http.client
out = []
for method, target in (("GET", "http://example.com/"), ("CONNECT", "example.com:443"), ("CONNECT", "api.anthropic.com:443")):
    c = http.client.HTTPConnection("proxy", 8080, timeout=8)
    c.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", target.split("/")[2] if target.startswith("http") else target)
    c.endheaders()
    out.append("%s %s=%d" % (method, target, c.getresponse().status))
    c.close()
print(";".join(out))
' 2>>"$OUT/probe-stderr.log")" || fwd="ERR"
log "forward-proxy attempts: $fwd"
case "$fwd" in
  "GET http://example.com/=403;CONNECT example.com:443=403;CONNECT api.anthropic.com:443=403")
    pass "proxy refuses forward-proxy requests";;
  *) fail "proxy forward-proxy behaviour unexpected: $fwd";;
esac

proxy_down
if scan_tree_for_real_token "$OUT" >"$OUT/token-scan.txt"; then
  pass "real token absent from all test logs ($(cat "$OUT/token-scan.txt"))"
else
  fail "real token found in test logs: $(cat "$OUT/token-scan.txt")"
fi
finish

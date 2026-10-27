"""A fake `docker` binary for pipeline and stop tests.

install(tmp_path, monkeypatch, **behaviour) writes the script, puts it first
on PATH and returns a FakeDocker handle. The script reads its behaviour from
config.json next to itself (the docker client environment is an allowlist,
so settings cannot be passed through the environment) and appends every
call to calls.jsonl there, and the call with the environment it received to
envs.jsonl.

Behaviour keys and values:
  fetch, review   "ok", "exit:<n>" (docker run exits n), "sleep" (runs until
                  killed; SIGTERM makes it exit 143, as an attached docker
                  run does when its container stops on the forwarded signal)
  proxy           "ok" (docker run -d prints an id) or "exit:<n>"
  connect         "ok" or "exit:<n>": docker network connect
  probe           "ok", "exit:<n>" (every docker exec exits n) or "after:<n>"
                  (the first n execs exit 1, later ones succeed)
  version         exit status of `docker version` (0 means the daemon answers)
  image           exit status of `docker image inspect`
  missing_images  image names for which `docker image inspect` fails
  labels          {image: value}: what `docker image inspect --format` prints
                  for the reviewbot.build label (an image not listed has no
                  label, which prints as an empty line)
  networks        {name: "true" | "false"}: networks that exist, with their
                  Internal value; `docker network create` adds to them
  network_create  "ok" or "exit:<n>"
  stale           container names `docker ps -a` lists
  proxy_log       lines `docker logs` prints on stderr, or "exit:<n>"
  review_stdout   what a successful review prints
  review_rc       exit status after printing review_stdout (default 0)
  kill_hangs      true: `docker kill`, `docker rm` and `docker logs` sleep
                  instead of acting
"""

import json
import os
import sys

SCRIPT = r'''
import json, os, re, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "config.json")))
args = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
with open(os.path.join(here, "envs.jsonl"), "a") as fh:
    fh.write(json.dumps({"args": args, "env": dict(os.environ)}) + "\n")

def behave(spec):
    if spec.startswith("exit:"):
        print("fake docker: exit " + spec[5:], file=sys.stderr)
        sys.exit(int(spec[5:]))

def networks():
    path = os.path.join(here, "networks.json")
    if os.path.exists(path):
        return json.load(open(path))
    return dict(cfg.get("networks", {}))

if args[0] == "version":
    sys.exit(cfg.get("version", 0))
if args[0] == "image":
    if cfg.get("image", 0) or args[-1] in cfg.get("missing_images", []):
        print("Error response from daemon: No such image: " + args[-1], file=sys.stderr)
        sys.exit(cfg.get("image", 0) or 1)
    fmt = args[args.index("--format") + 1] if "--format" in args else ""
    if "reviewbot.build" in fmt:
        print(cfg.get("labels", {}).get(args[-1], ""))
    else:
        print("sha256:fake")
    sys.exit(0)
if args[:2] == ["network", "inspect"]:
    nets = networks()
    if args[-1] not in nets:
        print("Error response from daemon: network " + args[-1] + " not found", file=sys.stderr)
        sys.exit(1)
    print(nets[args[-1]])
    sys.exit(0)
if args[:2] == ["network", "create"]:
    behave(cfg.get("network_create", "ok"))
    nets = networks()
    nets[args[-1]] = "true" if "--internal" in args else "false"
    json.dump(nets, open(os.path.join(here, "networks.json"), "w"))
    print("netid")
    sys.exit(0)
if args[:2] == ["network", "connect"]:
    behave(cfg.get("connect", "ok"))
    sys.exit(0)
if args[0] == "ps":
    for i, name in enumerate(cfg.get("stale", [])):
        print("id%d %s" % (i, name))
    sys.exit(0)
if args[0] == "exec":
    spec = cfg.get("probe", "ok")
    count = os.path.join(here, "execs")
    n = int(open(count).read()) if os.path.exists(count) else 0
    open(count, "w").write(str(n + 1))
    if spec.startswith("after:"):
        spec = "ok" if n >= int(spec[6:]) else "exit:1"
    behave(spec)
    sys.exit(0)
if args[0] == "logs":
    if cfg.get("kill_hangs"):
        time.sleep(600)
    spec = cfg.get("proxy_log", [])
    if isinstance(spec, str):
        behave(spec)
    for line in spec:
        print(line, file=sys.stderr)
    sys.exit(0)
if args[0] in ("kill", "rm"):
    if cfg.get("kill_hangs"):
        time.sleep(600)
    name = args[-1]
    pidfile = os.path.join(here, "pid-" + name)
    if args[0] == "kill" and os.path.exists(pidfile):
        try:
            os.kill(int(open(pidfile).read()), signal.SIGKILL)
        except ProcessLookupError:
            pass
    sys.exit(0)
if args[0] == "run":
    name = args[args.index("--name") + 1]
    with open(os.path.join(here, "pid-" + name), "w") as fh:
        fh.write(str(os.getpid()))
    kind = re.match(r"[a-z][a-z0-9-]*?-(fetch|review|proxy)-", name).group(1)
    spec = cfg.get(kind, "ok")
    behave(spec)
    if kind == "proxy":
        print("0123456789abcdef")
        sys.exit(0)
    if spec == "sleep":
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))
        open(os.path.join(here, "running-" + name), "w").close()
        time.sleep(600)
        sys.exit(0)
    if kind == "fetch":
        work = next(a.split(":")[0] for a in args if a.endswith(":/work"))
        os.mkdir(os.path.join(work, "repo"))
        open(os.path.join(work, "diff.patch"), "w").write(cfg["diff"])
        open(os.path.join(work, "files.txt"), "w").write(cfg["files"])
        sys.exit(0)
    print(cfg["review_stdout"])
    sys.exit(cfg.get("review_rc", 0))
sys.exit("fake docker: unexpected call " + " ".join(args))
'''


class FakeDocker:
    def __init__(self, bindir):
        self.bindir = bindir

    def calls(self):
        path = self.bindir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines()]

    def envs(self):
        """[(argv, environment)] for every call, in order."""
        path = self.bindir / "envs.jsonl"
        if not path.exists():
            return []
        return [(d["args"], d["env"]) for d in map(json.loads, path.read_text().splitlines())]

    def runs(self, kind):
        return [c[c.index("--name") + 1] for c in self.calls()
                if c[0] == "run" and f"-{kind}-" in c[c.index("--name") + 1]]

    def removed(self):
        """Names or ids given to `docker rm -f`, in order."""
        return [c[-1] for c in self.calls() if c[:2] == ["rm", "-f"]]

    def running(self):
        return [p.name[len("running-"):] for p in self.bindir.glob("running-*")]


def install(tmp_path, monkeypatch, **behaviour):
    from conftest import fixture_text

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "docker"
    exe.write_text(f"#!{sys.executable}\n" + SCRIPT)
    exe.chmod(0o755)
    cfg = {"diff": fixture_text("small.diff"), "files": fixture_text("small.files.txt"),
           "review_stdout": "{}"}
    cfg.update(behaviour)
    (bindir / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return FakeDocker(bindir)

#!/usr/bin/env python3
"""C4 conformance runner for the django targets — implements the runner contract in
../esdm-extensions/conformance/README.md: generate own targets from the canonical
model, boot, execute the scenario, normalize, compare against the golden answers.

Usage: scripts/conformance_c4.py <app> [--keep] [--skip-gen]
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from fnmatch import fnmatch
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WS = REPO.parent
EXT = WS / "esdm-extensions" / "conformance"
WORK = REPO / ".c4work"

TARGETS = {
    "python": {"target": "django-eventsourcing-postgres", "slug": "python", "port": 18130},
    "python-esdb": {"target": "django-eventsourcingdb", "slug": "python-esdb", "port": 18131},
}
API_INTERNAL = 8000
READY_TIMEOUT = 600
CONVERGE_TIMEOUT = 90


def log(msg: str) -> None:
    print(f"[c4:{REPO.name}] {msg}", flush=True)


def sh(cmd, cwd=None, env=None) -> None:
    import os
    subprocess.run(cmd if isinstance(cmd, list) else shlex.split(cmd), cwd=cwd,
                   env={**os.environ, **(env or {})}, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def http(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


def resolve(value, captures):
    if isinstance(value, str):
        for k, v in captures.items():
            value = value.replace(f"${k}", v)
        return value
    if isinstance(value, dict):
        return {k: resolve(v, captures) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, captures) for v in value]
    return value


def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def run_steps(port, steps):
    captures, out = {}, []
    for step in steps:
        if "get" in step:
            deadline = time.time() + step.get("poll_timeout", 45)
            while True:
                status, resp = http(port, "GET", resolve(step["get"], captures))
                ok = isinstance(resp, list) and len(resp) >= step.get("min_rows", 1)
                if not step.get("poll") or ok or time.time() > deadline:
                    break
                time.sleep(1.0)
            if "capture" in step and isinstance(resp, list) and resp:
                field = step.get("capture_field", "id")
                rows = sorted((r for r in resp if isinstance(r, dict)), key=canonical)
                fresh = [r for r in rows if r.get(field) not in captures.values()]
                val = (fresh or rows)[0].get(field)
                if isinstance(val, str):
                    captures[step["capture"]] = val
            out.append({"step": step["name"], "endpoint": f"GET {step['get']}", "status": status, "body": resp})
            continue
        body = resolve(step.get("body"), captures)
        status, resp = http(port, "POST", step["post"], body)
        if "capture" in step and isinstance(resp, dict) and isinstance(resp.get("id"), str):
            captures[step["capture"]] = resp["id"]
        out.append({"step": step["name"], "endpoint": f"POST {step['post']}", "status": status, "body": resp})
    return out, captures


def read_checkpoints(port, checkpoints, captures):
    out = []
    for cp in checkpoints:
        status, resp = http(port, "GET", resolve(cp["get"], captures))
        out.append({"checkpoint": cp["name"], "endpoint": f"GET {cp['get']}", "status": status, "body": resp})
    return out


def converge(port, checkpoints, captures):
    stable, last, deadline = 0, None, time.time() + CONVERGE_TIMEOUT
    while time.time() < deadline:
        snap = canonical(read_checkpoints(port, checkpoints, captures))
        if snap == last:
            stable += 1
            if stable >= 2:
                return read_checkpoints(port, checkpoints, captures)
        else:
            stable, last = 0, snap
        time.sleep(1.0)
    log(f"WARN: checkpoints did not stabilize in {CONVERGE_TIMEOUT}s")
    return read_checkpoints(port, checkpoints, captures)


SNAKE = re.compile(r"_([a-z0-9])")
camel = lambda s: SNAKE.sub(lambda m: m.group(1).upper(), s)
canon_event = lambda n: n.split(".")[-1].replace("_", "-").lower()


def normalize(value, idmap):
    if isinstance(value, str):
        return idmap.get(value, value)
    if isinstance(value, dict):
        return {camel(k): normalize(v, idmap) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, idmap) for v in value]
    return value


def normalize_all(steps, checkpoints, captures):
    idmap = {v: f"«{k}»" for k, v in captures.items()}
    nsteps = []
    for o in steps:
        body = normalize(o["body"], idmap)
        if isinstance(body, list):
            body = sorted(body, key=canonical)
        nsteps.append({**o, "body": body})
    ncps = []
    for o in checkpoints:
        body = o["body"]
        if o["checkpoint"] == "events":
            body = [{"aggregate": str(r.get("aggregate", "")).lower(),
                     "aggregateId": idmap.get(r.get("aggregate_id"), r.get("aggregate_id")),
                     "event": canon_event(str(r.get("event", ""))),
                     "playhead": r.get("playhead"),
                     "payload": normalize(r.get("payload"), idmap)}
                    for r in (body if isinstance(body, list) else [])]
        else:
            body = normalize(body, idmap)
            if isinstance(body, list):
                body = sorted(body, key=canonical)
        ncps.append({**o, "body": body})
    return {"steps": nsteps, "checkpoints": ncps}


def flatten(prefix, value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(flatten(f"{prefix}.{k}" if prefix else k, v))
        return out or {prefix: {}}
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out.update(flatten(f"{prefix}[{i}]", v))
        return out or {prefix: []}
    return {prefix: value}


def compare(mine, golden, registry, target):
    failures, accepted = [], []
    for kind in ("steps", "checkpoints"):
        name_key = "step" if kind == "steps" else "checkpoint"
        for g, m in zip(golden[kind], mine[kind]):
            endpoint = f"{g['endpoint']}#{g[name_key]}"
            fg = flatten("", {"status": g["status"], "body": g["body"]})
            fm = flatten("", {"status": m["status"], "body": m["body"]})
            for field in sorted(set(fg) | set(fm)):
                a, b = fg.get(field, "<absent>"), fm.get(field, "<absent>")
                if a == b:
                    continue
                entry = {"endpoint": endpoint, "field": field, "golden": a, "got": b}
                reg = next((r for r in registry
                            if ("targets" not in r or target in r["targets"])
                            and fnmatch(endpoint, r["endpoint"]) and fnmatch(field, r["field"])), None)
                (accepted if reg else failures).append(entry)
    return failures, accepted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--skip-gen", action="store_true")
    args = ap.parse_args()

    scenario = yaml.safe_load((EXT / "scenarios" / f"{args.app}.yaml").read_text())
    registry = (yaml.safe_load((EXT / "registry.yaml").read_text()) or {}).get("divergences") or []
    golden = json.loads((EXT / "golden" / f"{args.app}.observations.json").read_text())
    model = WS / scenario["model"]

    exit_code = 0
    for tname, tcfg in TARGETS.items():
        if tname not in scenario["targets"]:
            log(f"{tname}: not in scenario targets — skipped")
            continue
        appdir = WORK / args.app / tname
        stack = appdir / "generated" / tcfg["slug"]
        project = f"c4-{REPO.name}-{args.app}-{tname}"
        port = tcfg["port"]

        if not args.skip_gen:
            if appdir.exists():
                shutil.rmtree(appdir)
            appdir.mkdir(parents=True)
            log(f"{tname}: generating")
            sh(f"python3 -m esdm2python.cli generate {appdir} -t {tcfg['target']} -m {model} -o {appdir}/generated",
               cwd=REPO, env={"PYTHONPATH": str(REPO / "src")})
            doc = yaml.safe_load((stack / "compose.yaml").read_text())
            for name, svc in doc.get("services", {}).items():
                if name == "api":
                    svc["ports"] = [f"127.0.0.1:{port}:{API_INTERNAL}"]
                else:
                    svc.pop("ports", None)
            (stack / "compose.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))

        try:
            log(f"{tname}: booting on :{port}")
            sh(["docker", "compose", "-p", project, "-f", str(stack / "compose.yaml"), "up", "-d", "--build", "--quiet-pull"])
            deadline = time.time() + READY_TIMEOUT
            while time.time() < deadline:
                try:
                    if http(port, "GET", "_dev/catalog")[0] == 200:
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError(f"{tname}: api not ready in {READY_TIMEOUT}s")
            log(f"{tname}: running scenario")
            steps, captures = run_steps(port, scenario["steps"])
            cps = converge(port, scenario["checkpoints"], captures)
            mine = normalize_all(steps, cps, captures)
            (appdir / "observations.json").write_text(json.dumps(mine, indent=2, default=str))
            failures, accepted = compare(mine, golden, registry, tname)
            for d in accepted:
                log(f"{tname}: registered divergence {d['endpoint']} {d['field']}")
            for d in failures:
                log(f"{tname}: FAIL {d['endpoint']} {d['field']}: golden={d['golden']!r} got={d['got']!r}")
            log(f"{tname}: {'PASS' if not failures else f'FAIL ({len(failures)} unregistered divergences)'}")
            exit_code |= 1 if failures else 0
        finally:
            if not args.keep:
                sh(["docker", "compose", "-p", project, "-f", str(stack / "compose.yaml"), "down", "-v", "--remove-orphans"])
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""SYSDECK backend.

Samples CPU / memory / GPU / disk / network / processes / Ollama and streams
the state as JSON over Server-Sent Events to the dashboard in ./static.
Only the Python stdlib and psutil are used. Binds to 127.0.0.1.

Ollama telemetry comes from two places:
  * the HTTP API (/api/ps, /api/tags) for loaded models and VRAM placement
  * the systemd journal of the ollama unit, which logs layer offload,
    prefill progress, live generation speed (tg_3s) and final timings.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

HOST = "127.0.0.1"
PORT = int(os.environ.get("SYSDECK_PORT", "8790"))
OLLAMA = os.environ.get("SYSDECK_OLLAMA", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_UNIT = os.environ.get("SYSDECK_OLLAMA_UNIT", "ollama")
STATIC = Path(__file__).resolve().parent / "static"
HISTORY = 240  # samples (seconds) kept for charts

lock = threading.Lock()
tick = threading.Condition()
ollama_kick = threading.Event()

state: dict = {"errors": {}}
history: dict[str, deque] = {
    k: deque(maxlen=HISTORY)
    for k in ("cpu", "mem", "gpu", "vram", "rx", "tx", "rd", "wr", "tps")
}


def put(key: str, value) -> None:
    with lock:
        state[key] = value


def put_err(name: str, err: Exception | None) -> None:
    with lock:
        if err is None:
            state["errors"].pop(name, None)
        else:
            state["errors"][name] = f"{type(err).__name__}: {err}"[:200]


def every(interval: float, fn) -> None:
    """Run fn forever on a daemon thread; errors are surfaced, not fatal."""

    def run():
        while True:
            t0 = time.monotonic()
            try:
                fn()
                put_err(fn.__name__, None)
            except Exception as e:  # noqa: BLE001 - keep sampler alive
                put_err(fn.__name__, e)
            time.sleep(max(0.05, interval - (time.monotonic() - t0)))

    threading.Thread(target=run, daemon=True, name=fn.__name__).start()


def ollama_api(path: str, body: dict | None = None, timeout: float = 2.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        OLLAMA + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# ───────────────────────────── static info ──────────────────────────────


def static_info() -> dict:
    u = os.uname()
    cpu = u.machine
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next(
                (l.split(":", 1)[1].strip() for l in f if l.startswith("model name")),
                cpu,
            )
    except OSError:
        pass
    os_name = "Linux"
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return {
        "host": u.nodename,
        "kernel": u.release,
        "os": os_name,
        "cpu": cpu,
        "threads": psutil.cpu_count() or 1,
        "cores": psutil.cpu_count(logical=False) or 1,
        "boot": psutil.boot_time(),
        "ram": psutil.virtual_memory().total,
    }


INFO = static_info()

# ───────────────────────────── fast sampler (1 Hz) ──────────────────────

NET_SKIP = re.compile(r"^(lo|docker|br-|veth|virbr|vnet|tun|tap|wg|nordlynx|tailscale|zt)")
DISK_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|mmcblk\d+)$")
_prev: dict = {}


def _temps() -> dict:
    out = {"cpu": None, "nvme": []}
    try:
        t = psutil.sensors_temperatures()
    except Exception:  # noqa: BLE001
        return out
    for chip in ("k10temp", "zenpower", "coretemp"):
        for s in t.get(chip, []):
            if s.label in ("Tctl", "Tdie", "Package id 0", "") and out["cpu"] is None:
                out["cpu"] = s.current
    out["nvme"] = [round(s.current, 1) for s in t.get("nvme", []) if s.label == "Composite"]
    return out


def _drive_temps() -> dict:
    """Live drive temperatures from hwmon, keyed by block device name."""
    out = {}
    for blk in Path("/sys/block").iterdir():
        if not DISK_RE.match(blk.name):
            continue
        for f in list(blk.glob("device/hwmon*/temp1_input")) + list(blk.glob("device/device/hwmon/hwmon*/temp1_input")):
            try:
                out[blk.name] = int(f.read_text()) / 1000
                break
            except (OSError, ValueError):
                pass
    return out


def sample_fast() -> None:
    now = time.time()
    per = psutil.cpu_percent(percpu=True)
    total = sum(per) / max(1, len(per))
    freq = psutil.cpu_freq()
    temps = _temps()
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()

    nets = psutil.net_io_counters(pernic=True)
    rx = sum(v.bytes_recv for k, v in nets.items() if not NET_SKIP.match(k))
    tx = sum(v.bytes_sent for k, v in nets.items() if not NET_SKIP.match(k))
    disks = psutil.disk_io_counters(perdisk=True) or {}
    rd = sum(v.read_bytes for k, v in disks.items() if DISK_RE.match(k))
    wr = sum(v.write_bytes for k, v in disks.items() if DISK_RE.match(k))

    p = _prev.get("io")
    dt = now - p["t"] if p else 1.0
    rate = lambda cur, key: max(0.0, (cur - p[key]) / dt) if p else 0.0  # noqa: E731
    io = {"rx": rate(rx, "rx"), "tx": rate(tx, "tx"), "rd": rate(rd, "rd"), "wr": rate(wr, "wr")}
    _prev["io"] = {"t": now, "rx": rx, "tx": tx, "rd": rd, "wr": wr}

    # per physical drive read/write rates, keyed by kernel name (nvme0n1, sda, ...)
    pd = _prev.get("disk")
    disk_io = {}
    for k, v in disks.items():
        if not DISK_RE.match(k):
            continue
        if pd and k in pd["d"]:
            r0, w0 = pd["d"][k]
            ddt = now - pd["t"] or 1.0
            disk_io[k] = {"rd": max(0.0, (v.read_bytes - r0) / ddt), "wr": max(0.0, (v.write_bytes - w0) / ddt)}
        else:
            disk_io[k] = {"rd": 0.0, "wr": 0.0}
    _prev["disk"] = {"t": now, "d": {k: (v.read_bytes, v.write_bytes) for k, v in disks.items()}}
    drive_temps = _drive_temps()

    cpu = {
        "total": round(total, 1),
        "cores": [round(c) for c in per],
        "freq": round(freq.current) if freq else None,
        "freq_max": round(freq.max) if freq and freq.max else None,
        "temp": temps["cpu"],
        "load": [round(x, 2) for x in os.getloadavg()],
    }
    cache = max(0, vm.available - vm.free)
    mem = {
        "total": vm.total,
        "used": vm.total - vm.available,
        "cache": cache,
        "free": vm.free,
        "available": vm.available,
        "percent": round((vm.total - vm.available) / vm.total * 100, 1),
        "swap_total": sw.total,
        "swap_used": sw.used,
    }
    batt = None
    try:
        b = psutil.sensors_battery()
        if b:
            batt = {"percent": round(b.percent), "plugged": b.power_plugged}
    except Exception:  # noqa: BLE001
        pass

    gpu = state.get("gpu") or {}
    llm = state.get("llm") or {}
    with lock:
        state.update(
            t=now,
            cpu=cpu,
            mem=mem,
            io={**io, "rx_total": rx, "tx_total": tx},
            nvme_temps=temps["nvme"],
            disk_io=disk_io,
            drive_temps=drive_temps,
            battery=batt,
            uptime=now - INFO["boot"],
        )
        history["cpu"].append(cpu["total"])
        history["mem"].append(mem["percent"])
        history["gpu"].append(gpu.get("util") or 0)
        history["vram"].append(gpu.get("vram_pct") or 0)
        for k in ("rx", "tx", "rd", "wr"):
            history[k].append(round(io[k]))
        history["tps"].append(llm.get("live_tps") or 0)
    with tick:
        tick.notify_all()


# ───────────────────────────── GPU (nvidia-smi) ─────────────────────────

GPU_FIELDS = [
    "name", "utilization.gpu", "utilization.memory", "memory.used", "memory.total",
    "temperature.gpu", "power.draw", "enforced.power.limit", "clocks.sm",
    "clocks.mem", "clocks.max.sm", "pstate",
]


def _num(v: str):
    try:
        return float(v)
    except ValueError:
        return None


def gpu_reader() -> None:
    """Keep one nvidia-smi process streaming once per second."""
    while True:
        try:
            proc = subprocess.Popen(
                ["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}",
                 "--format=csv,noheader,nounits", "-lms", "1000"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            for line in proc.stdout:
                f = [x.strip() for x in line.split(",")]
                if len(f) != len(GPU_FIELDS):
                    continue
                used, tot = _num(f[3]), _num(f[4])
                g = {
                    "name": f[0].replace("NVIDIA GeForce ", ""),
                    "util": _num(f[1]),
                    "mem_util": _num(f[2]),
                    "vram_used": used,
                    "vram_total": tot,
                    "vram_pct": round(used / tot * 100, 1) if used is not None and tot else None,
                    "temp": _num(f[5]),
                    "power": _num(f[6]),
                    "power_limit": _num(f[7]),
                    "clock": _num(f[8]),
                    "mem_clock": _num(f[9]),
                    "clock_max": _num(f[10]),
                    "pstate": f[11],
                }
                with lock:
                    prev = state.get("gpu") or {}
                    g["apps"] = prev.get("apps", [])
                    state["gpu"] = g
                put_err("gpu", None)
            proc.wait()
            put_err("gpu", RuntimeError(f"nvidia-smi exited ({proc.returncode})"))
        except FileNotFoundError as e:
            put_err("gpu", e)
            return
        except Exception as e:  # noqa: BLE001
            put_err("gpu", e)
        time.sleep(5)


def sample_gpu_apps() -> None:
    out = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5,
    ).stdout
    apps: dict[str, float] = {}
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        exe = parts[1].split(" --", 1)[0].split()[0] if parts[1] else "?"
        name = os.path.basename(exe) or exe
        if name == "chrome" and "ms-playwright" in parts[1]:
            name = "chrome (playwright)"
        if parts[0] == str(os.getpid()):
            name = "sysdeck"
        mem = _num(parts[-1]) or 0
        apps[name] = apps.get(name, 0) + mem
    lst = sorted(({"name": k, "mem": v} for k, v in apps.items()), key=lambda a: -a["mem"])
    with lock:
        if "gpu" in state:
            state["gpu"]["apps"] = lst


# ───────────────────────────── Ollama API ───────────────────────────────


def _iso_ts(s: str | None):
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


_tags_at = 0.0


def sample_ollama() -> None:
    global _tags_at
    o = dict(state.get("ollama") or {})
    try:
        ps = ollama_api("/api/ps")
    except Exception as e:  # noqa: BLE001
        put("ollama", {"online": False, "error": str(e)[:120], "models": [], "tags": o.get("tags", [])})
        return
    models = []
    for m in ps.get("models", []):
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        d = m.get("details") or {}
        models.append({
            "name": m.get("name") or m.get("model"),
            "size": size,
            "vram": vram,
            "gpu_pct": round(vram / size * 100, 1) if size else 0,
            "ctx": m.get("context_length"),
            "expires": _iso_ts(m.get("expires_at")),
            "family": d.get("family"),
            "params": d.get("parameter_size"),
            "quant": d.get("quantization_level"),
        })
    now = time.time()
    if now - _tags_at > 60 or not o.get("tags"):
        try:
            tags = ollama_api("/api/tags").get("models", [])
            o["tags"] = [{"name": t["name"], "size": t.get("size", 0)} for t in tags]
            o["version"] = ollama_api("/api/version").get("version")
            _tags_at = now
        except Exception:  # noqa: BLE001
            pass
    o.update(online=True, models=models, error=None, polled=now)
    put("ollama", o)


def ollama_poller() -> None:
    """Poll /api/ps rarely (every request is logged by Ollama), faster on activity."""
    while True:
        try:
            sample_ollama()
            put_err("ollama", None)
        except Exception as e:  # noqa: BLE001
            put_err("ollama", e)
        ollama_kick.wait(timeout=8)
        if ollama_kick.is_set():
            ollama_kick.clear()
            time.sleep(0.6)  # let Ollama settle after the event


# ───────────────────────────── Ollama journal ───────────────────────────

J_GIN = re.compile(
    r'\[GIN\][^|]*\|\s*(\d{3})\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\w+)\s+"([^"]+)"'
)
J_META = re.compile(r"llama_model_loader: loaded meta data")
J_NAME = re.compile(r"general\.name\s+str\s+=\s+(.+?)\s*$")
J_OFFLOAD = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
J_READY = re.compile(r'model loaded|msg="loaded runners"|llama runner started')
J_NEWPROMPT = re.compile(r"new prompt, n_ctx_slot = \d+.*?task\.n_tokens = (\d+)")
J_PREFILL = re.compile(r"prompt processing, n_tokens =\s*(\d+), progress =\s*([\d.]+)")
J_GEN = re.compile(r"n_gen =\s*(\d+), tg =\s*([\d.]+) t/s, tg_3s =\s*([\d.]+) t/s")
J_PEVAL = re.compile(r"prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")
J_EVAL = re.compile(r"\|\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")
J_DRAFT = re.compile(r"draft acceptance =\s*([\d.]+)")
QUIET_PATHS = {"/", "/api/ps", "/api/tags", "/api/version", "/api/show"}

llm = {
    "phase": "idle",  # idle | loading | prefill | generating
    "phase_at": 0.0,
    "live_tps": 0.0,
    "n_gen": 0,
    "prefill": 0.0,
    "prefill_tokens": 0,
    "load": None,  # {t, name, gpu_layers, layers, draft, ready_s}
    "runs": deque(maxlen=40),  # completed generations
    "requests": deque(maxlen=30),  # API requests (non-polling)
}
_pending: dict = {}


def _go_dur(s: str) -> float:
    mult = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}
    return sum(float(n) * mult[u] for n, u in re.findall(r"([\d.]+)(h|ms|µs|us|ns|m|s)", s))


def _publish_llm() -> None:
    snap = {
        "phase": llm["phase"],
        "phase_at": llm["phase_at"],
        "live_tps": llm["live_tps"],
        "n_gen": llm["n_gen"],
        "prefill": llm["prefill"],
        "prefill_tokens": llm["prefill_tokens"],
        "load": dict(llm["load"]) if llm["load"] else None,
        "runs": [dict(r) for r in list(llm["runs"])[-24:]],
        "requests": [dict(r) for r in llm["requests"]],
    }
    put("llm", snap)


def _handle_journal(ts: float, msg: str, live: bool) -> None:
    if m := J_GIN.search(msg):
        status, dur, _ip, method, path = m.groups()
        if path in QUIET_PATHS:
            return
        req = {"t": ts, "status": int(status), "dur": _go_dur(dur), "method": method, "path": path}
        last = llm["runs"][-1] if llm["runs"] else None
        if last and ts - last["t"] < 3 and "path" not in last:
            last["path"] = path
            req.update(gen_tokens=last.get("gen_tokens"), gen_tps=last.get("gen_tps"))
        llm["requests"].append(req)
        if llm["phase"] in ("prefill", "generating") and ts - llm["phase_at"] > 2:
            llm.update(phase="idle", live_tps=0.0)
        if live:
            ollama_kick.set()
        return
    if J_META.search(msg):
        cur = llm["load"]
        if not cur or ts - cur["t"] > 120 or cur.get("ready_s") is not None:
            llm["load"] = {"t": ts, "name": None, "gpu_layers": None, "layers": None,
                           "draft": None, "ready_s": None}
            llm.update(phase="loading", phase_at=ts)
        return
    if (m := J_NAME.search(msg)) and llm["load"] and not llm["load"]["name"]:
        llm["load"]["name"] = m.group(1)
        return
    if m := J_OFFLOAD.search(msg):
        a, b = int(m.group(1)), int(m.group(2))
        ld = llm["load"] or {"t": ts, "name": None, "draft": None, "ready_s": None,
                             "gpu_layers": None, "layers": None}
        llm["load"] = ld
        if ld.get("layers") is None or b > ld["layers"]:
            if ld.get("layers") is not None:
                ld["draft"] = f'{ld["gpu_layers"]}/{ld["layers"]}'
            ld["gpu_layers"], ld["layers"] = a, b
        else:
            ld["draft"] = f"{a}/{b}"
        return
    if J_READY.search(msg):
        if llm["load"] and llm["load"].get("ready_s") is None:
            llm["load"]["ready_s"] = round(ts - llm["load"]["t"], 1)
        if llm["phase"] == "loading":
            llm.update(phase="idle", phase_at=ts)
        if live:
            ollama_kick.set()
        return
    if m := J_NEWPROMPT.search(msg):
        llm.update(phase="prefill", phase_at=ts, prefill=0.0, prefill_tokens=int(m.group(1)),
                   n_gen=0, live_tps=0.0)
        if live:
            ollama_kick.set()  # model may have just been (re)loaded
        return
    if m := J_PREFILL.search(msg):
        prog = float(m.group(2))
        total = llm["prefill_tokens"] if llm["phase"] == "prefill" else 0
        if not total and prog > 0:
            total = round(int(m.group(1)) / prog)
        llm.update(phase="prefill", phase_at=ts, prefill=prog, prefill_tokens=total, n_gen=0)
        return
    if m := J_GEN.search(msg):
        llm.update(phase="generating", phase_at=ts, n_gen=int(m.group(1)),
                   live_tps=float(m.group(3)), prefill=1.0)
        return
    if m := J_PEVAL.search(msg):
        _pending.clear()
        _pending.update(prompt_tokens=int(m.group(2)), prompt_tps=float(m.group(3)))
        return
    if m := J_EVAL.search(msg):
        run = {"t": ts, "gen_tokens": int(m.group(2)), "gen_tps": float(m.group(3)), **_pending}
        _pending.clear()
        llm["runs"].append(run)
        llm.update(phase="idle", phase_at=ts, live_tps=0.0, n_gen=run["gen_tokens"])
        return
    if (m := J_DRAFT.search(msg)) and llm["runs"] and ts - llm["runs"][-1]["t"] < 3:
        llm["runs"][-1]["draft_acc"] = round(float(m.group(1)) * 100, 1)


J_LINE = re.compile(r"^(\d+\.\d+) [^:]+: (.*)$")


def journal_reader() -> None:
    while True:
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", OLLAMA_UNIT, "-f", "-n", "4000", "-o", "short-unix",
                 "--no-pager", "--no-hostname"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
            )
            last_pub = 0.0
            for line in proc.stdout:
                m = J_LINE.match(line.rstrip("\n"))
                if not m:
                    continue
                ts = float(m.group(1))
                live = time.time() - ts < 10
                _handle_journal(ts, m.group(2), live)
                now = time.monotonic()
                if live or now - last_pub > 0.5:
                    _publish_llm()
                    last_pub = now
            proc.wait()
        except Exception as e:  # noqa: BLE001
            put_err("journal", e)
        time.sleep(5)


def llm_watchdog() -> None:
    """Drop stale 'generating'/'prefill' phases when the log goes quiet."""
    if llm["phase"] in ("prefill", "generating", "loading"):
        quiet = time.time() - llm["phase_at"]
        limit = 180 if llm["phase"] == "loading" else 25
        if quiet > limit:
            llm.update(phase="idle", live_tps=0.0)
    _publish_llm()


# ───────────────────────────── processes / dev ──────────────────────────


def is_runner(name: str) -> bool:
    """Ollama server and its model runners (llama-server on the new engine)."""
    return name.startswith("ollama") or name.startswith("llama")


def sample_procs() -> None:
    ncpu = INFO["threads"]
    groups: dict[str, dict] = {}
    count = 0
    ollama_rss = 0
    ollama_cpu = 0.0
    for p in psutil.process_iter(["name", "memory_info"]):
        count += 1
        try:
            cpu = p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        name = re.sub(r"-MainThread$", "", p.info["name"] or "?")
        mi = p.info["memory_info"]
        rss = mi.rss if mi else 0
        if not rss and not cpu:
            continue
        # Multi-process apps (chrome, electron) share most pages; summing RSS
        # would count them many times. Use private (rss - shared) memory, except
        # for Ollama whose model weights are file-backed mmap pages.
        private = rss if is_runner(name) else max(0, rss - getattr(mi, "shared", 0))
        g = groups.setdefault(name, {"name": name, "cpu": 0.0, "mem": 0, "n": 0})
        g["cpu"] += cpu / ncpu
        g["mem"] += private
        g["n"] += 1
        if is_runner(name):
            ollama_rss += rss
            ollama_cpu += cpu / ncpu
    vals = list(groups.values())
    for g in vals:
        g["cpu"] = round(g["cpu"], 1)
    top_cpu = sorted(vals, key=lambda g: -g["cpu"])[:20]
    top_mem = sorted(vals, key=lambda g: -g["mem"])[:20]
    put("procs", {"count": count, "cpu": top_cpu, "mem": top_mem,
                  "ollama_rss": ollama_rss, "ollama_cpu": round(ollama_cpu, 1)})


DEV_NAMES = {"node", "bun", "deno", "python", "python3", "uvicorn", "gunicorn", "java",
             "ruby", "php", "go", "cargo", "dotnet", "npm", "pnpm", "vite", "next-server"}
PORT_NOISE = {"discord", "code", "claude", "spotify", "steam", "slack", "kdeconnectd"}


def sample_ports() -> None:
    me = os.getuid()
    home = os.path.expanduser("~")
    seen: dict[int, dict] = {}
    for c in psutil.net_connections(kind="tcp"):
        if c.status != psutil.CONN_LISTEN or not c.laddr:
            continue
        port = c.laddr.port
        if port in seen:
            continue
        entry = {"port": port, "addr": c.laddr.ip, "name": None, "project": None, "mine": False}
        if c.pid:
            try:
                p = psutil.Process(c.pid)
                entry["name"] = p.name()
                entry["mine"] = p.uids().real == me
                if entry["mine"]:
                    try:
                        cwd = p.cwd()
                        if cwd and cwd not in (home, "/"):
                            entry["project"] = os.path.basename(cwd)
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if entry["name"]:
            entry["name"] = re.sub(r"-MainThread$", "", entry["name"])
        if port == 11434:
            entry["name"] = entry["name"] or "ollama"
        if port == PORT:
            entry["name"], entry["project"] = "sysdeck", None
        # Ephemeral helper ports (editor/IDE/agent IPC) are noise on a dev dashboard.
        if port >= 32768 or (entry["name"] or "").lower() in PORT_NOISE:
            continue
        if entry["project"] in ("bin", "usr", "opt"):
            entry["project"] = None
        seen[port] = entry
    ports = sorted(
        seen.values(),
        key=lambda e: (not (e["name"] in DEV_NAMES or e["project"]), not e["mine"], e["port"]),
    )
    put("ports", ports[:40])


def sample_docker() -> None:
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{json .}}"],
                           capture_output=True, text=True, timeout=4)
    except FileNotFoundError:
        put("docker", {"available": False, "containers": []})
        return
    if r.returncode != 0:
        put("docker", {"available": False, "containers": [], "error": r.stderr.strip()[:100]})
        return
    cs = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        cs.append({"name": d.get("Names"), "image": d.get("Image"),
                   "status": d.get("Status"), "ports": d.get("Ports")})
    put("docker", {"available": True, "containers": cs})


FS_OK = {"ext4", "btrfs", "xfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "zfs"}


def sample_disks() -> None:
    out, devs = [], set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype not in FS_OK or part.device in devs:
            continue
        if part.mountpoint.startswith(("/var/lib/docker", "/snap", "/run", "/tmp", "/boot", "/efi")):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        devs.add(part.device)
        out.append({"mount": part.mountpoint, "dev": os.path.basename(part.device),
                    "fs": part.fstype, "total": u.total, "used": u.used, "percent": u.percent})
    out.sort(key=lambda d: (d["mount"] != "/", d["mount"] != "/home", -d["total"]))
    put("disks", out[:5])


# ───────────────────────────── storage (drives, partitions, health) ─────

VENDORS = [("MTFD", "Micron"), ("INTEL", "Intel"), ("SAMSUNG", "Samsung"), ("WDC", "WD"),
           ("WD", "WD"), ("CT", "Crucial"), ("KINGSTON", "Kingston"), ("HFM", "SK hynix"),
           ("SKHYNIX", "SK hynix"), ("TOSHIBA", "Toshiba"), ("KIOXIA", "Kioxia"), ("ST", "Seagate"),
           ("SANDISK", "SanDisk"), ("ADATA", "ADATA"), ("PHISON", "Phison")]


def _busctl(*args: str, timeout: float = 4.0):
    r = subprocess.run(["busctl", "--system", "--json=short", *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return json.loads(r.stdout)


def _unwrap(d):
    """busctl JSON a{sv} → plain dict."""
    if not d or not d.get("data"):
        return {}
    data = d["data"][0] if isinstance(d["data"], list) else d["data"]
    return {k: v.get("data") if isinstance(v, dict) else v for k, v in data.items()}


def _udisks_health() -> dict:
    """SMART/NVMe health per drive serial, via UDisks2 (no root needed)."""
    out = {}
    r = subprocess.run(["busctl", "--system", "--list", "tree", "org.freedesktop.UDisks2"],
                       capture_output=True, text=True, timeout=4)
    for path in r.stdout.split():
        if "/drives/" not in path:
            continue
        drive = _unwrap(_busctl("call", "org.freedesktop.UDisks2", path, "org.freedesktop.DBus.Properties",
                                "GetAll", "s", "org.freedesktop.UDisks2.Drive"))
        serial = (drive.get("Serial") or "").strip()
        if not serial:
            continue
        h = {"vendor": drive.get("Vendor") or None, "removable": drive.get("Removable"),
             "bus": drive.get("ConnectionBus") or None}
        nv = _unwrap(_busctl("call", "org.freedesktop.UDisks2", path, "org.freedesktop.DBus.Properties",
                             "GetAll", "s", "org.freedesktop.UDisks2.NVMe.Controller"))
        if nv:
            h["power_on_h"] = nv.get("SmartPowerOnHours")
            h["critical"] = bool(nv.get("SmartCriticalWarning"))
            attrs = _unwrap(_busctl("call", "org.freedesktop.UDisks2", path,
                                    "org.freedesktop.UDisks2.NVMe.Controller", "SmartGetAttributes", "a{sv}", "0"))
            if attrs:
                h.update(wear=attrs.get("percent_used"), spare=attrs.get("avail_spare"),
                         spare_thresh=attrs.get("spare_thresh"), written=attrs.get("total_data_written"),
                         read=attrs.get("total_data_read"), media_errors=attrs.get("media_errors"),
                         unsafe_shutdowns=attrs.get("unsafe_shutdowns"))
        else:
            ata = _unwrap(_busctl("call", "org.freedesktop.UDisks2", path, "org.freedesktop.DBus.Properties",
                                  "GetAll", "s", "org.freedesktop.UDisks2.Drive.Ata"))
            if ata and ata.get("SmartEnabled"):
                h["power_on_h"] = round((ata.get("SmartPowerOnSeconds") or 0) / 3600)
                h["critical"] = bool(ata.get("SmartFailing"))
        out[serial] = h
    return out


def _docker_df():
    try:
        r = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"],
                           capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    rows = {}
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        rows[d.get("Type")] = {"size": d.get("Size"), "reclaimable": d.get("Reclaimable")}
    return rows


_storage_slow = {"t": 0.0, "health": {}, "docker": None}


def sample_storage() -> None:
    now = time.time()
    if now - _storage_slow["t"] > 120:  # health & docker df change slowly
        try:
            _storage_slow["health"] = _udisks_health()
        except Exception as e:  # noqa: BLE001
            put_err("udisks", e)
        _storage_slow["docker"] = _docker_df()
        _storage_slow["t"] = now
    r = subprocess.run(
        ["lsblk", "-J", "-b", "-o",
         "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,PARTLABEL,MOUNTPOINTS,MODEL,SERIAL,TRAN,RM,FSUSED,FSSIZE"],
        capture_output=True, text=True, timeout=5)
    drives = []
    for d in json.loads(r.stdout).get("blockdevices", []):
        if d.get("type") != "disk" or not DISK_RE.match(d.get("name", "")) or not d.get("size"):
            continue
        model = (d.get("model") or "").strip()
        health = _storage_slow["health"].get((d.get("serial") or "").strip(), {})
        vendor = health.get("vendor") or next((v for p, v in VENDORS if model.upper().startswith(p)), None)
        parts = []
        for p in d.get("children") or []:
            mounts = [m for m in (p.get("mountpoints") or []) if m]
            fs = p.get("fstype")
            parts.append({
                "name": p.get("name"),
                "size": p.get("size") or 0,
                "fs": fs,
                "label": p.get("label") or p.get("partlabel"),
                "mount": mounts[0] if mounts else None,
                "used": int(p["fsused"]) if p.get("fsused") else None,
                "fssize": int(p["fssize"]) if p.get("fssize") else None,
                "encrypted": fs in ("BitLocker", "crypto_LUKS"),
            })
        mounts = {p["mount"] for p in parts if p["mount"]}
        fss = {p["fs"] for p in parts}
        if "/" in mounts:
            role = "system"
        elif "BitLocker" in fss or ("ntfs" in fss and d.get("tran") == "nvme"):
            role = "windows"
        elif d.get("rm") or d.get("tran") == "usb":
            role = "usb"
        else:
            role = "data"
        drives.append({
            "name": d["name"], "model": model, "vendor": vendor, "size": d["size"],
            "bus": (d.get("tran") or health.get("bus") or "").upper() or None,
            "role": role, "parts": parts,
            "health": {k: v for k, v in health.items() if k not in ("vendor", "bus", "removable")},
        })
    order = {"system": 0, "data": 1, "windows": 2, "usb": 3}
    drives.sort(key=lambda x: (order.get(x["role"], 9), x["name"]))
    put("storage", {"drives": drives, "docker": _storage_slow["docker"]})


VPN_RE = re.compile(r"^(nordlynx|nordtun|tun|wg|tailscale|proton|mullvad)")


def sample_ifaces() -> None:
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    out, vpn = [], None
    for name, st in stats.items():
        if not st.isup:
            continue
        if VPN_RE.match(name):
            vpn = name
            continue
        if NET_SKIP.match(name):
            continue
        ip = next((a.address for a in addrs.get(name, []) if a.family.name == "AF_INET"), None)
        if not ip:
            continue
        kind = "wifi" if name.startswith(("wl", "wlan")) else "eth"
        out.append({"name": name, "ip": ip, "speed": st.speed or None, "kind": kind})
    put("ifaces", {"list": out, "vpn": vpn})


# ───────────────────────────── actions ──────────────────────────────────

BENCH_PROMPT = ("Explain in about 150 words how a CPU cache hierarchy works, "
                "then give one practical tip for writing cache-friendly code.")


def run_bench(model: str | None) -> None:
    put("bench", {"running": True, "model": model, "started": time.time()})
    try:
        if not model:
            ms = (state.get("ollama") or {}).get("models") or []
            tags = (state.get("ollama") or {}).get("tags") or []
            model = (ms[0]["name"] if ms else None) or (tags[0]["name"] if tags else None)
        if not model:
            raise RuntimeError("no model installed")
        put("bench", {"running": True, "model": model, "started": time.time()})
        r = ollama_api("/api/generate", {
            "model": model, "prompt": BENCH_PROMPT, "stream": False,
            "options": {"num_predict": 192, "temperature": 0.2},
        }, timeout=600)
        ev_n, ev_d = r.get("eval_count", 0), r.get("eval_duration", 0) or 1
        pe_n, pe_d = r.get("prompt_eval_count", 0), r.get("prompt_eval_duration", 0) or 1
        put("bench", {
            "running": False, "model": model, "t": time.time(),
            "gen_tps": round(ev_n / (ev_d / 1e9), 2), "gen_tokens": ev_n,
            "prompt_tps": round(pe_n / (pe_d / 1e9), 1), "prompt_tokens": pe_n,
            "load_s": round((r.get("load_duration") or 0) / 1e9, 2),
            "total_s": round((r.get("total_duration") or 0) / 1e9, 2),
        })
    except Exception as e:  # noqa: BLE001
        put("bench", {"running": False, "model": model, "error": str(e)[:160], "t": time.time()})
    ollama_kick.set()


def unload_models() -> list[str]:
    names = [m["name"] for m in (state.get("ollama") or {}).get("models", [])]
    for n in names:
        try:
            ollama_api("/api/generate", {"model": n, "keep_alive": 0}, timeout=30)
        except Exception as e:  # noqa: BLE001
            put_err("unload", e)
    ollama_kick.set()
    return names


# ───────────────────────────── media (MPRIS) ────────────────────────────
# Every Linux media player registers on the session bus as org.mpris.MediaPlayer2.<name>:
# Spotify, Firefox / Chromium / Chrome tabs (YouTube, SoundCloud, ...), VLC, mpv
# (with mpv-mpris), Rhythmbox, Elisa, Strawberry, Audacious, ... One interface covers them all.

MPRIS = "org.mpris.MediaPlayer2"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_SKIP = {"playerctld"}  # proxies that mirror another player
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
OBJ_PATH = re.compile(r"^/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?$")
ART_MAX = 8 * 1024 * 1024

media_lock = threading.Lock()
# ident: name → Identity, since: name → when it started playing, last: name → last seen playing,
# pin: (name, when) chosen in the UI, art: key → artUrl, tid: name → mpris:trackid
_media: dict = {"ident": {}, "since": {}, "last": {}, "pin": None, "art": {}, "tid": {}}
_art_cache: dict = {"url": None, "body": b"", "ctype": ""}


def _ubus(*args: str, timeout: float = 2.0):
    """busctl on the session bus. Returns parsed JSON ({} when there is no output); raises on failure."""
    r = subprocess.run(["busctl", "--user", "--json=short", f"--timeout={timeout}", "--", *args],
                       capture_output=True, text=True, timeout=timeout + 1)
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip() or "busctl failed")[:160])
    return json.loads(r.stdout) if r.stdout.strip() else {}


def _props(name: str, iface: str) -> dict:
    return _unwrap(_ubus("call", name, MPRIS_PATH, "org.freedesktop.DBus.Properties", "GetAll", "s", iface))


def _read_player(name: str) -> dict:
    p = _props(name, MPRIS + ".Player")
    md = {k: (v.get("data") if isinstance(v, dict) else v) for k, v in (p.get("Metadata") or {}).items()}
    if name not in _media["ident"]:
        short = name[len(MPRIS) + 1:].split(".")[0]
        try:
            ident = _props(name, MPRIS).get("Identity") or short.capitalize()
        except Exception:  # noqa: BLE001 - root interface is optional in practice
            ident = short.capitalize()
        _media["ident"][name] = ident
    joined = lambda v: ", ".join(v) if isinstance(v, list) else (v or "")  # noqa: E731
    us = lambda v: v / 1e6 if isinstance(v, (int, float)) and v > 0 else None  # noqa: E731
    art = md.get("mpris:artUrl") or ""
    key = hashlib.sha1(art.encode()).hexdigest()[:12] if art else None
    if key:
        _media["art_next"][key] = art
    _media["tid"][name] = md.get("mpris:trackid") or ""
    return {
        "id": name,
        "name": _media["ident"][name],
        "status": p.get("PlaybackStatus") or "Stopped",
        "title": joined(md.get("xesam:title")),
        "artist": joined(md.get("xesam:artist")) or joined(md.get("xesam:albumArtist")),
        "album": joined(md.get("xesam:album")),
        "length": us(md.get("mpris:length")),
        "position": (p["Position"] / 1e6 if isinstance(p.get("Position"), (int, float)) and p["Position"] >= 0 else None),
        "rate": p.get("Rate") or 1.0,
        "volume": p.get("Volume"),
        "shuffle": p.get("Shuffle"),
        "loop": p.get("LoopStatus"),
        "art": key,
        "can": {
            "control": p.get("CanControl", True),
            "play": p.get("CanPlay", True),
            "pause": p.get("CanPause", True),
            "next": p.get("CanGoNext", False),
            "prev": p.get("CanGoPrevious", False),
            "seek": p.get("CanSeek", False) and bool(md.get("mpris:length")),
        },
    }


def _same_media(a: dict, b: dict) -> bool:
    """Two bus names showing one stream (e.g. a Chrome tab and KDE's browser-integration mirror of it)."""
    if a["status"] != b["status"]:
        return False
    if a["length"] and b["length"]:
        return abs(a["length"] - b["length"]) < 1 and abs((a["position"] or 0) - (b["position"] or 0)) < 3
    ta, tb = a["title"].lower(), b["title"].lower()
    return bool(ta and tb) and (ta in tb or tb in ta)


def sample_media() -> None:
    with media_lock:
        try:
            names = _ubus("call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", "ListNames")["data"][0]
        except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired, KeyError, IndexError) as e:
            put("media", {"available": False, "players": [], "error": str(e)[:120]})
            return
        names = sorted(
            (n for n in names if n.startswith(MPRIS + ".") and n[len(MPRIS) + 1:].split(".")[0] not in MPRIS_SKIP),
            key=lambda n: "plasma-browser-integration" in n,  # KDE's browser proxy duplicates the tab's own player
        )
        _media["art_next"] = {}
        players = []
        for n in names:
            try:
                pl = _read_player(n)
            except Exception:  # noqa: BLE001 - one hung or half-implemented player must not hide the rest
                continue
            if not any(_same_media(pl, q) for q in players):
                players.append(pl)
        _media["art"] = _media.pop("art_next")  # swap whole, so /api/media/art never sees it half-built

        now = time.time()
        ids = {p["id"] for p in players}
        for p in players:
            if p["status"] == "Playing":
                _media["since"].setdefault(p["id"], now)
                _media["last"][p["id"]] = now
            else:
                _media["since"].pop(p["id"], None)
        for d in ("ident", "since", "last", "tid"):
            for k in [k for k in _media[d] if k not in ids]:
                del _media[d][k]
        # A player picked in the UI stays active until it closes or another one starts playing.
        pin = _media["pin"]
        if pin and (pin[0] not in ids or any(t > pin[1] for k, t in _media["since"].items() if k != pin[0])):
            pin = _media["pin"] = None
        if pin:
            active = pin[0]
        elif players:
            active = max(players, key=lambda p: (
                p["status"] == "Playing",
                _media["since"].get(p["id"]) or _media["last"].get(p["id"], 0),
                p["status"] == "Paused",
            ))["id"]
        else:
            active = None
        put("media", {"available": True, "t": now, "active": active, "pinned": bool(pin), "players": players})


def media_action(body: dict) -> None:
    act = body.get("action")
    m = state.get("media") or {}
    players = {p["id"]: p for p in m.get("players", [])}
    if act == "select":
        if body.get("player") not in players:
            raise ValueError("unknown player")
        _media["pin"] = (body["player"], time.time())
    else:
        p = players.get(body.get("player")) or players.get(m.get("active"))
        if not p:
            raise ValueError("no player")
        name, iface = p["id"], MPRIS + ".Player"
        call = lambda method, *a: _ubus("call", name, MPRIS_PATH, iface, method, *a)  # noqa: E731
        setp = lambda prop, sig, v: _ubus("set-property", name, MPRIS_PATH, iface, prop, sig, v)  # noqa: E731
        simple = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause",
                  "stop": "Stop", "next": "Next", "previous": "Previous"}
        if act in simple:
            call(simple[act])
        elif act == "seek":
            target = max(0.0, min(float(body["value"]), (p["length"] or 1e9) - 0.5))
            tid = _media["tid"].get(name) or ""
            if OBJ_PATH.match(tid) and tid != NO_TRACK:
                call("SetPosition", "ox", tid, str(int(target * 1e6)))
            else:  # no usable track id: seek relative to where we think it is
                pos = p["position"] or 0.0
                if p["status"] == "Playing":
                    pos += (time.time() - m.get("t", time.time())) * (p["rate"] or 1.0)
                call("Seek", "x", str(int((target - pos) * 1e6)))
        elif act == "volume":
            setp("Volume", "d", repr(max(0.0, min(1.0, float(body["value"])))))
        elif act == "shuffle":
            setp("Shuffle", "b", "true" if body.get("value") else "false")
        elif act == "loop":
            if body.get("value") not in ("None", "Track", "Playlist"):
                raise ValueError("bad loop status")
            setp("LoopStatus", "s", body["value"])
        else:
            raise ValueError("unknown action")
        time.sleep(0.15)  # let the player apply it before re-reading
    sample_media()
    with tick:
        tick.notify_all()


def _sniff(b: bytes) -> str | None:
    if b.startswith(b"\x89PNG"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b.startswith(b"GIF8"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def media_art(key: str):
    """Cover art of a current player, by key. Only URLs a player advertised are read, and only images are served."""
    url = _media["art"].get(key)
    if not url:
        return None
    if _art_cache["url"] == url:
        return _art_cache["body"], _art_cache["ctype"]
    body = b""
    if url.startswith("file://"):
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        st = os.stat(path)
        if stat.S_ISREG(st.st_mode) and st.st_size <= ART_MAX:
            body = Path(path).read_bytes()
    elif url.startswith(("https://", "http://")):
        req = urllib.request.Request(url, headers={"User-Agent": "sysdeck"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read(ART_MAX + 1)
    elif url.startswith("data:image/"):
        head, _, data = url.partition(",")
        body = base64.b64decode(data) if head.endswith(";base64") else urllib.parse.unquote_to_bytes(data)
    ctype = _sniff(body[:16])
    if not ctype or len(body) > ART_MAX:
        return None
    _art_cache.update(url=url, body=body, ctype=ctype)
    return body, ctype


# ───────────────────────────── HTTP ─────────────────────────────────────

FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def snapshot(full: bool) -> dict:
    with lock:
        s = json.loads(json.dumps(state, default=list))
        if full:
            s["history"] = {k: list(v) for k, v in history.items()}
            s["info"] = INFO
    return s


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sysdeck"

    def log_message(self, *_):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in FILES:
            name, ctype = FILES[path]
            try:
                self._send(200, (STATIC / name).read_bytes(), ctype)
            except OSError:
                self._send(404, b"missing", "text/plain")
        elif path == "/api/state":
            self._json(snapshot(True))
        elif path == "/api/stream":
            self._stream()
        elif path == "/api/media/art":
            key = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("k", [""])[0]
            try:
                art = media_art(key)
            except Exception:  # noqa: BLE001 - missing file, network error, bad data URI
                art = None
            if art:
                self._send(200, art[0], art[1])
            else:
                self._send(404, b"no art", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        # Custom header forces a CORS preflight, so other sites can't trigger actions.
        if self.headers.get("X-Sysdeck") != "1":
            return self._json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        if self.path == "/api/bench":
            if (state.get("bench") or {}).get("running"):
                return self._json({"error": "already running"}, 409)
            threading.Thread(target=run_bench, args=(body.get("model"),), daemon=True).start()
            return self._json({"ok": True})
        if self.path == "/api/unload":
            return self._json({"unloaded": unload_models()})
        if self.path == "/api/media":
            try:
                media_action(body)
            except (ValueError, KeyError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                return self._json({"error": str(e)}, 502)
            return self._json({"ok": True})
        return self._json({"error": "unknown"}, 404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b"event: init\ndata: " + json.dumps(snapshot(True)).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                with tick:
                    tick.wait(timeout=5)
                self.wfile.write(b"data: " + json.dumps(snapshot(False)).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def start(port: int | None = None) -> ThreadingHTTPServer:
    """Start samplers and the HTTP server on a background thread (used by app.py)."""
    global PORT
    if port:
        PORT = port
    psutil.cpu_percent(percpu=True)  # prime counters
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    threading.Thread(target=gpu_reader, daemon=True, name="gpu").start()
    threading.Thread(target=journal_reader, daemon=True, name="journal").start()
    threading.Thread(target=ollama_poller, daemon=True, name="ollama").start()
    every(1.0, sample_fast)
    every(3.0, sample_gpu_apps)
    every(2.0, sample_procs)
    every(5.0, sample_ports)
    every(6.0, sample_docker)
    every(30.0, sample_disks)
    every(15.0, sample_storage)
    every(10.0, sample_ifaces)
    every(2.0, llm_watchdog)
    every(1.0, sample_media)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    print(f"sysdeck backend on http://{HOST}:{PORT}", flush=True)
    return srv


def main() -> None:
    start()
    threading.Event().wait()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""source share — modular quick-share kinds over an isolated transport.

Share kinds (git is one of several):
- snippet (`source share`): selected files / line ranges as JSON + raw text.
- tree (`source share-tree`): directory listing + file contents snapshot.
- cmd (`source share-cmd`): captured command output (e.g. logs, status).
- git (`source share-git`): snapshot temp git remote for upload/ingest.

Each kind is served from an isolated loopback HTTP server (or git daemon for
the git kind) and exposed via `tailscale serve` (default, private mesh) or
`cloudflared tunnel` (public URL). Kinds are registered in SHARE_KINDS so new
classifications plug in without touching the core.

All state lives under SHARE_BASE (~/.local/share/pixos/source/shares).
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REAL_HOME = Path(os.environ.get("REAL_HOME") or os.environ.get("HOME") or Path.home())
SHARE_BASE = REAL_HOME / ".local" / "share" / "pixos" / "source" / "shares"
SNIPPET_DIR = SHARE_BASE / "snippets"
GIT_DIR = SHARE_BASE / "git"
REGISTRY = SHARE_BASE / "registry.json"
INDEX = SHARE_BASE / "index.json"  # persistent path lookup: survives temp-store deletion


def _load_index() -> dict:
    try:
        return json.loads(INDEX.read_text())
    except Exception:
        return {}


def _save_index(idx: dict) -> None:
    SHARE_BASE.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(idx, indent=2))


def record_index(slug: str, kind: str, base: str, specs: list, files: list,
                 bundle_path: str | None = None) -> dict:
    """Persist a path-lookup entry that outlives the temp bundle store."""
    idx = _load_index()
    entry = idx.get(slug, {})
    entry.update({"kind": kind, "base": str(base), "specs": list(specs),
                  "files": list(files),
                  "bundle": bundle_path,
                  "updated": datetime.now(timezone.utc).isoformat()})
    entry.setdefault("created", entry["updated"])
    idx[slug] = entry
    _save_index(idx)
    return entry


def lookup_index(path: str | None = None) -> dict:
    """Look up shares by original file path (substring match), or all."""
    idx = _load_index()
    # annotate liveness without deleting stale entries
    for slug, e in idx.items():
        b = e.get("bundle")
        e["bundle_exists"] = Path(b).exists() if b else None
    if not path:
        return idx
    return {s: e for s, e in idx.items()
            if path in s or any(path in f for f in e.get("files", []))
            or path in str(e.get("base", ""))}

GIT_URL_RE = re.compile(r"^(https?://|git@|ssh://|file://).+")
GITHUB_RE = re.compile(r"^https?://(www\.)?github\.com/[^/\s]+/[^/\s]+?(?:\.git)?(?:/tree/[^/\s]+(?:/.*)?)?/?$")


def poll_clipboard() -> tuple[str, str]:
    """Read Wayland/X clipboard text (wl-paste, fallback xclip). Returns (text, source)."""
    for cmd, src in ((["wl-paste", "--no-newline"], "wl-paste"),
                     (["wl-paste"], "wl-paste"),
                     (["xclip", "-selection", "clipboard", "-o"], "xclip")):
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except Exception:
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout, src
    return "", "none"


def validate_git_url(url: str) -> bool:
    return bool(GIT_URL_RE.match(url.strip()))


def is_github_repo_url(url: str) -> bool:
    return bool(GITHUB_RE.match(url.strip()))


def _load_registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except Exception:
        return {}


def _save_registry(reg: dict) -> None:
    SHARE_BASE.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_transport(transport: str) -> dict:
    """Verify binary presence and basic daemon readiness for a transport."""
    binary = "tailscale" if transport == "tailscale" else "cloudflared"
    bin_path = shutil.which(binary)
    if not bin_path:
        return {"transport": transport, "available": False, "reason": f"{binary} not on PATH"}
    if transport == "tailscale":
        try:
            r = subprocess.run([bin_path, "status", "--json"], capture_output=True, text=True, timeout=2)
            if r.returncode != 0:
                return {"transport": transport, "available": False, "reason": "tailscale daemon not running"}
        except Exception:
            return {"transport": transport, "available": False, "reason": "tailscale status probe failed"}
    return {"transport": transport, "available": True, "path": bin_path}


def make_agent_envelope(slug: str, kind: str, files: list, bundle_path: Path | None,
                        port: int, transport: str, expires: str, extra: dict | None = None) -> dict:
    """Stable JSON contract for LLM and programmatic callers."""
    b_str = str(bundle_path) if bundle_path else None
    t_status = check_transport(transport)
    env = {
        "ok": True,
        "slug": slug,
        "kind": kind,
        "files": list(files),
        "bundle": b_str,
        "bundle_exists": bundle_path.exists() if bundle_path else False,
        "port": port,
        "transport": transport,
        "transport_ready": t_status["available"],
        "expires": expires,
    }
    if extra:
        env.update(extra)
    return env


def _cleanup_slug(slug: str, entry: dict) -> None:
    port = entry.get("port")
    pid = entry.get("pid")
    if pid and _is_pid_alive(pid):
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass
    if entry.get("kind") == "gh" and entry.get("repo"):
        try:
            from share.gh import retire_gh_repo
        except ImportError:
            try:
                from gh import retire_gh_repo  # type: ignore
            except ImportError:
                from .gh import retire_gh_repo  # type: ignore
        res = retire_gh_repo(slug, entry)
        if not res.get("ok"):
            err_msg = res.get("error", f"failed to retire {entry['repo']}")
            reg = _load_registry()
            if slug in reg:
                reg[slug]["status"] = "live"
                reg[slug]["retire_error"] = err_msg
                _save_registry(reg)
            raise RuntimeError(err_msg)
    for f in (SNIPPET_DIR / f"{slug}.json", GIT_DIR / slug):
        try:
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink(missing_ok=True)
        except OSError:
            pass
    if port and entry.get("transport") == "tailscale" and shutil.which("tailscale"):
        subprocess.run(["tailscale", "serve", "--bg", "off", str(port)], capture_output=True)


def gc_expired_shares(now: datetime | None = None) -> list[str]:
    """Reap past-TTL entries from registry.json and delete their temp bundles."""
    now = now or datetime.now(timezone.utc)
    reg = _load_registry()
    reaped = []
    for slug, entry in list(reg.items()):
        if entry.get("status") == "retired":
            continue
        exp_raw = entry.get("expires")
        if not exp_raw:
            continue
        try:
            exp_dt = datetime.fromisoformat(exp_raw)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if exp_dt <= now:
            try:
                _cleanup_slug(slug, entry)
            except RuntimeError:
                continue  # keep live in registry if remote delete failed
            reg = _load_registry()
            if entry.get("kind") != "gh":
                reg.pop(slug, None)
            reaped.append(slug)
    if reaped:
        _save_registry(reg)
    return reaped


def parse_file_spec(spec: str):
    """Parse 'path[:start[-end]]' -> (path, start, end). Lines are 1-based inclusive."""
    if len(spec) > 512 or "\n" in spec:
        return spec, None, None
    m = re.match(r"^([^:\n]+)(?::(\d+)(?:-(\d+))?)?$", spec)
    if not m:
        return spec, None, None
    path, a, b = m.group(1), m.group(2), m.group(3)
    start = int(a) if a else None
    end = int(b) if b else start
    return path, start, end


def collect_snippet(base: Path, spec: str) -> dict:
    rel, start, end = parse_file_spec(spec)
    p = (base / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {spec}")
    lines = p.read_text(errors="replace").splitlines()
    n = len(lines)
    sel = lines[(start - 1):end] if start else lines
    return {"file": str(p.relative_to(base)) if str(p).startswith(str(base)) else str(p),
            "start": start or 1, "end": end or n, "total_lines": n,
            "text": "\n".join(sel)}


def do_share(args: argparse.Namespace) -> int:
    SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    transport = getattr(args, "via", "tailscale") or "tailscale"
    ttl_min = getattr(args, "ttl", 60) or 60
    base = Path(getattr(args, "base", None) or os.getcwd()).resolve()
    slug = datetime.now(timezone.utc).strftime("share-%Y%m%d-%H%M%S")
    specs = getattr(args, "files", None) or []
    if not specs:
        print("error: give at least one file, e.g. source share file main.py:10-40", file=sys.stderr)
        return 2
    items = []
    for s in specs:
        try:
            items.append(collect_snippet(base, s))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    port = _free_port()
    bundle = {"slug": slug, "created": datetime.now(timezone.utc).isoformat(),
              "expires": (datetime.now(timezone.utc) + timedelta(minutes=ttl_min)).isoformat(),
              "base": str(base), "items": items, "port": port, "transport": transport}
    bundle_path = SNIPPET_DIR / f"{slug}.json"
    bundle_path.write_text(json.dumps(bundle, indent=2))
    reg = _load_registry()
    reg[slug] = {"kind": "snippet", "port": port, "transport": transport,
                 "expires": bundle["expires"], "files": [i["file"] for i in items]}
    _save_registry(reg)
    record_index(slug, "snippet", str(base), specs, [i["file"] for i in items],
                 str(bundle_path))
    envelope = make_agent_envelope(
        slug, "snippet", [i["file"] for i in items], bundle_path,
        port, transport, bundle["expires"],
        extra={"base": str(base), "items": items, "clone_or_url": f"http://127.0.0.1:{port}/text"},
    )
    payload = json.dumps(envelope, indent=2)
    if getattr(args, "json", False) or not sys.stdout.isatty():
        print(payload)
    else:
        print(f"snippet {slug}: {len(items)} file(s) on 127.0.0.1:{port} via {transport}")
        for i in items:
            print(f"  {i['file']}:{i['start']}-{i['end']}")
    if getattr(args, "serve", False):
        serve_bundle(bundle)
    else:
        _print_expose_hint(port, transport)
    return 0


def _make_handler(bundle: dict):
    payload = json.dumps(bundle, indent=2).encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/":
                body = payload
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/text":
                body = ("\n\n".join(
                    f"--- {i['file']}:{i['start']}-{i['end']} ---\n{i['text']}" for i in bundle["items"])).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


def serve_bundle(bundle: dict) -> None:
    port = bundle["port"]
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(bundle))
    print(f"serving {bundle['slug']} on 127.0.0.1:{port} (Ctrl-C to stop)")
    _print_expose_hint(port, bundle.get("transport", "tailscale"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def _print_expose_hint(port: int, transport: str) -> None:
    if transport == "tailscale":
        print(f"expose: tailscale serve --bg http://127.0.0.1:{port}")
    elif transport == "cloudflared":
        print(f"expose: cloudflared tunnel --url http://127.0.0.1:{port}")
    print(f"stop:   source share stop --port {port}")


def do_share_stop(args: argparse.Namespace) -> int:
    port = getattr(args, "port", None)
    target_slug = getattr(args, "slug", None)
    if getattr(args, "expired", False):
        reaped = gc_expired_shares()
        if getattr(args, "json", False) or not sys.stdout.isatty():
            print(json.dumps({"reaped": reaped, "count": len(reaped)}, indent=2))
        else:
            print(f"reaped {len(reaped)} expired share(s): {', '.join(reaped) or 'none'}")
        return 0
    reg = _load_registry()
    if target_slug:
        e = reg.get(target_slug)
        if not e:
            print(f"error: unknown share '{target_slug}'", file=sys.stderr)
            return 2
        _cleanup_slug(target_slug, e)
        reg = _load_registry()
        if e.get("kind") != "gh":
            reg.pop(target_slug, None)
            _save_registry(reg)
        print(f"retired {target_slug}")
        return 0
    if port:
        for slug, e in list(reg.items()):
            if e.get("port") == port:
                _cleanup_slug(slug, e)
                del reg[slug]
        _save_registry(reg)
        print(f"stopped share on port {port}")
    else:
        print(json.dumps(reg, indent=2))
    return 0


def _slug_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    name = re.sub(r"\.git$", "", name) or "repo"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "repo"
    return f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


# git_remote owns local temp git remotes (share.py re-exports for compat).
# Imports stay lazy (PEP 562 __getattr__) so git_remote -> share.share
# back-imports never deadlock at module load; gh.py uses the same pattern.
_GIT_REMOTE_NAMES = {
    "create_temp_remote": "create_temp_remote",
    "do_git_share": "do_git_share",
    "do_git_serve": "do_git_serve",
    "serve_git": "serve_git_daemon",
    "start_background_git_http": "start_background_git_http",
    "start_background_git_http_detached": "start_background_git_http_detached",
    "register_git_remote_parsers": "register_git_remote_parsers",
    "dispatch_git_remote": "dispatch_git_remote",
}


def _load_git_remote():
    try:
        from share import git_remote as _gr
    except ImportError:
        try:
            import git_remote as _gr  # type: ignore
        except ImportError:
            from . import git_remote as _gr  # type: ignore
    return _gr


def __getattr__(name: str):
    if name in _GIT_REMOTE_NAMES:
        return getattr(_load_git_remote(), _GIT_REMOTE_NAMES[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False




def _spawn_detached_server(kind: str, target_path: str, port: int, slug: str) -> int:
    """Launch a detached HTTP/git server process that survives TUI close."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "_serve_bg",
           "--kind", kind, "--target", target_path, "--port", str(port), "--slug", slug]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def _run_bg_server(kind: str, target_path: str, port: int, slug: str) -> None:
    """Blocking server body for the detached _serve_bg child process."""
    if kind == "snippet":
        bundle = json.loads(Path(target_path).read_text())
        items = bundle.get("items", [])
        payload = json.dumps(bundle, indent=2).encode()
        text_body = ("\n\n".join(
            f"--- {i['file']}:{i.get('start', 1)}-{i.get('end', '')} ---\n{i.get('text', '')}" for i in items)).encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if self.path in ("/", "/json"):
                    body, ctype = payload, "application/json"
                elif self.path == "/text":
                    body, ctype = text_body, "text/plain; charset=utf-8"
                elif self.path == "/download":
                    body, ctype = text_body, "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Disposition", f'attachment; filename="{slug}.txt"')
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                else:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
    else:
        subprocess.run(["git", "update-server-info"], cwd=target_path, check=True)
        from http.server import SimpleHTTPRequestHandler

        class GitHTTPHandler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(Path(target_path).parent), **kw)
            def log_message(self, *a): pass
        ThreadingHTTPServer(("127.0.0.1", port), GitHTTPHandler).serve_forever()


def _tailscale_node_url(port: int) -> str | None:
    """Expose local HTTP port via `tailscale serve --bg` and return the https:// URL."""
    if not shutil.which("tailscale"):
        return None
    r = subprocess.run(["tailscale", "serve", "--bg", f"--http={port}", f"http://127.0.0.1:{port}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["tailscale", "serve", "--bg", f"http://127.0.0.1:{port}"], capture_output=True)
    st = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True)
    if st.returncode == 0:
        try:
            dns = json.loads(st.stdout).get("Self", {}).get("DNSName", "").rstrip(".")
            if dns:
                return f"http://{dns}:{port}"
        except Exception:
            pass
    return f"http://127.0.0.1:{port}"


def start_background_snippet_server(bundle: dict) -> str:
    """Run snippet HTTP server (JSON + /text + /download) in a daemon thread + tailscale serve."""
    port = bundle["port"]
    payload = json.dumps(bundle, indent=2).encode()
    text_body = ("\n\n".join(
        f"--- {i['file']}:{i['start']}-{i['end']} ---\n{i['text']}" for i in bundle["items"]
    )).encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path in ("/", "/json"):
                body, ctype = payload, "application/json"
            elif self.path == "/text":
                body, ctype = text_body, "text/plain; charset=utf-8"
            elif self.path == "/download":
                body, ctype = text_body, "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Disposition", f'attachment; filename="{bundle["slug"]}.txt"')
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return _tailscale_node_url(port) or f"http://127.0.0.1:{port}"


def start_background_snippet_server_detached(bundle_path: str, port: int, slug: str) -> tuple[int, str]:
    pid = _spawn_detached_server("snippet", bundle_path, port, slug)
    return pid, _tailscale_node_url(port) or f"http://127.0.0.1:{port}"


# Registry of share-kind classifications. Each kind maps to a handler that
# builds a bundle dict served over the isolated transport. git is one entry.
SHARE_KINDS = ("snippet", "tree", "cmd", "git", "gh", "diff")


def _register_share(slug: str, kind: str, port: int, transport: str, ttl_min: int, extra: dict) -> dict:
    expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_min)).isoformat()
    reg = _load_registry()
    entry = {"kind": kind, "port": port, "transport": transport, "expires": expires, **extra}
    reg[slug] = entry
    _save_registry(reg)
    return entry


def do_tree_share(args: argparse.Namespace) -> int:
    """Share a directory tree: listing + file contents snapshot."""
    transport = getattr(args, "via", "tailscale") or "tailscale"
    ttl_min = getattr(args, "ttl", 60) or 60
    base = Path(getattr(args, "path", None) or os.getcwd()).resolve()
    depth = getattr(args, "depth", 3) or 3
    if not base.is_dir():
        print(f"error: not a directory: {base}", file=sys.stderr)
        return 2
    entries = []
    for p in sorted(base.rglob("*")):
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) > depth or ".git" in rel.parts:
            continue
        entries.append({"path": str(rel), "dir": p.is_dir(),
                        "size": p.stat().st_size if p.is_file() else 0})
    slug = datetime.now(timezone.utc).strftime("tree-%Y%m%d-%H%M%S")
    port = _free_port()
    bundle = {"kind": "tree", "slug": slug, "base": str(base), "entries": entries, "port": port}
    SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    bundle_path = SNIPPET_DIR / f"{slug}.json"
    bundle_path.write_text(json.dumps(bundle, indent=2))
    reg_entry = _register_share(slug, "tree", port, transport, ttl_min,
                                {"base": str(base), "entries": len(entries)})
    file_paths = [e["path"] for e in entries if not e["dir"]]
    out = make_agent_envelope(
        slug, "tree", file_paths, bundle_path,
        port, transport, reg_entry["expires"],
        extra={"base": str(base), "entries": len(entries)},
    )
    print(json.dumps(out, indent=2))
    if getattr(args, "serve", False):
        serve_bundle({"slug": slug, "port": port, "transport": transport,
                      "items": [{"file": e["path"], "start": 1, "end": 1,
                                 "text": ""} for e in entries if not e["dir"]]})
    else:
        _print_expose_hint(port, transport)
    return 0


def do_cmd_share(args: argparse.Namespace) -> int:
    """Share captured command output (logs, status, diagnostics)."""
    transport = getattr(args, "via", "tailscale") or "tailscale"
    ttl_min = getattr(args, "ttl", 60) or 60
    cmd = getattr(args, "cmd", None) or []
    if not cmd:
        print("error: usage: source share-cmd -- <command...>", file=sys.stderr)
        return 2
    r = subprocess.run(cmd, capture_output=True, text=True)
    slug = datetime.now(timezone.utc).strftime("cmd-%Y%m%d-%H%M%S")
    port = _free_port()
    bundle = {"kind": "cmd", "slug": slug, "cmd": cmd, "returncode": r.returncode,
              "stdout": r.stdout[-20000:], "stderr": r.stderr[-5000:], "port": port}
    SHARE_BASE.mkdir(parents=True, exist_ok=True)
    SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    bundle_path = SNIPPET_DIR / f"{slug}.json"
    bundle_path.write_text(json.dumps(bundle, indent=2))
    reg_entry = _register_share(slug, "cmd", port, transport, ttl_min, {"cmd": " ".join(cmd)})
    out = make_agent_envelope(
        slug, "cmd", [], bundle_path,
        port, transport, reg_entry["expires"],
        extra={"cmd": cmd, "returncode": r.returncode, "stdout_tail": r.stdout[-1000:]},
    )
    print(json.dumps(out, indent=2))
    if getattr(args, "serve", False):
        serve_bundle({"slug": slug, "port": port, "transport": transport,
                      "items": [{"file": "$ " + " ".join(cmd), "start": 1, "end": 1,
                                 "text": r.stdout}]})
    else:
        _print_expose_hint(port, transport)
    return r.returncode if r.returncode not in (0,) else 0


def do_diff_share(args: argparse.Namespace) -> int:
    """Share scoped git diff and recent revisions for a *subject or file group."""
    transport = getattr(args, "via", "tailscale") or "tailscale"
    ttl_min = getattr(args, "ttl", 60) or 60
    base = Path(getattr(args, "base", None) or os.getcwd()).resolve()
    spec = " ".join(getattr(args, "spec", None) or []).strip()
    base_ref = getattr(args, "ref", None) or "HEAD"
    if not spec:
        print("error: give a *subject or path spec, e.g. share diff *tui", file=sys.stderr)
        return 2
    try:
        from subject.subject import get_subject_registry
    except ImportError:
        try:
            from common.subject import get_subject_registry  # type: ignore
        except ImportError:
            print("error: subject subsystem unavailable", file=sys.stderr)
            return 2
    reg_sub = get_subject_registry(root_dir=base)
    ctx = reg_sub.get_context_diff(spec, base_ref=base_ref)
    diff_text = ctx.get("diff", "")
    if not diff_text.strip():
        print(f"error: empty diff for '{spec}' against {base_ref}", file=sys.stderr)
        return 2
    slug = datetime.now(timezone.utc).strftime("diff-%Y%m%d-%H%M%S")
    port = _free_port()
    SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    bundle_path = SNIPPET_DIR / f"{slug}.json"
    bundle = {
        "kind": "diff",
        "slug": slug,
        "spec": spec,
        "base_ref": base_ref,
        "subjects": ctx.get("subject", []),
        "paths": ctx.get("paths", []),
        "revisions": ctx.get("revisions", []),
        "diff": diff_text,
        "port": port,
    }
    bundle_path.write_text(json.dumps(bundle, indent=2))
    reg_entry = _register_share(slug, "diff", port, transport, ttl_min,
                                {"spec": spec, "base_ref": base_ref, "paths": ctx.get("paths", [])})
    record_index(slug, "diff", str(base), [spec], ctx.get("paths", []), str(bundle_path))
    out = make_agent_envelope(
        slug, "diff", ctx.get("paths", []), bundle_path,
        port, transport, reg_entry["expires"],
        extra={"spec": spec, "base_ref": base_ref, "subjects": ctx.get("subject", []),
               "revisions": ctx.get("revisions", []), "clone_or_url": f"http://127.0.0.1:{port}/text"},
    )
    print(json.dumps(out, indent=2))
    if getattr(args, "serve", False):
        serve_bundle({"slug": slug, "port": port, "transport": transport,
                      "items": [{"file": f"diff:{spec}@{base_ref}", "start": 1,
                                 "end": len(diff_text.splitlines()), "text": diff_text}]})
    else:
        _print_expose_hint(port, transport)
    return 0


def _register_ingested_source(dest: Path, url: str, branch: str | None) -> bool:
    """Merge ingested repo into source's project registry via load_registry/save_registry."""
    try:
        from source.source import load_registry, save_registry
    except ImportError:
        try:
            from source import load_registry, save_registry  # type: ignore
        except ImportError:
            return False
    detected_branch = branch
    if not detected_branch:
        br = subprocess.run(["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True)
        detected_branch = br.stdout.strip() if br.returncode == 0 and br.stdout.strip() else "main"
    name = dest.name
    reg = load_registry()
    reg[name] = {
        "name": name,
        "type": "vendor",
        "path": str(dest),
        "upstream": url,
        "upstream_branch": detected_branch,
        "status": "STANDBY",
        "created_at": time.time(),
        "active_backups": [],
    }
    save_registry(reg)
    return True


def do_ingest(args: argparse.Namespace) -> int:
    url = (getattr(args, "url", None) or "").strip()
    if not url:
        print("error: a GitHub repository or branch URL is required", file=sys.stderr)
        return 2
    if not validate_git_url(url):
        print(f"error: not a valid git URL: {url}", file=sys.stderr)
        return 2
    dest = Path(getattr(args, "dest", None) or (REAL_HOME / "Projects" / "vendor" / _slug_from_url(url)))
    branch = getattr(args, "branch", None)
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["-b", branch]
    cmd += [url, str(dest)]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        return r.returncode
    registered = _register_ingested_source(dest, url, branch)
    print(f"ingested {url} -> {dest}")
    if getattr(args, "json", False):
        print(json.dumps({"url": url, "dest": str(dest), "branch": branch, "registered": registered}))
    return 0


def register_share_parsers(subparsers) -> None:
    p = subparsers.add_parser("share", help="Quick-share files/lines over tailscale serve or cloudflared")
    p.add_argument("files", nargs="*", help="file[:start[-end]] specs")
    p.add_argument("--base", default=None, help="base dir for relative paths")
    p.add_argument("--via", choices=["tailscale", "cloudflared"], default="tailscale")
    p.add_argument("--ttl", type=int, default=60, help="share lifetime in minutes")
    p.add_argument("--serve", action="store_true", help="serve now (foreground)")
    p.set_defaults(_share_action="snippet")

    p_stop = subparsers.add_parser("share-stop", help="Stop a share / list active shares")
    p_stop.add_argument("--port", type=int, default=None)
    p_stop.add_argument("--slug", default=None, help="stop/retire a specific share slug (e.g. gh repo)")
    p_stop.add_argument("--expired", action="store_true", help="reap all past-TTL registry entries and bundles")
    p_stop.set_defaults(_share_action="stop")

    _load_git_remote().register_git_remote_parsers(subparsers)

    p_tree = subparsers.add_parser("share-tree", help="Share a directory tree snapshot (one share kind)")
    p_tree.add_argument("--path", default=None, help="directory to share")
    p_tree.add_argument("--depth", type=int, default=3)
    p_tree.add_argument("--via", choices=["tailscale", "cloudflared"], default="tailscale")
    p_tree.add_argument("--ttl", type=int, default=60)
    p_tree.add_argument("--serve", action="store_true")
    p_tree.set_defaults(_share_action="tree")

    p_cmd = subparsers.add_parser("share-cmd", help="Share captured command output (one share kind)")
    p_cmd.add_argument("cmd", nargs=argparse.REMAINDER, help="command to run after --")
    p_cmd.add_argument("--via", choices=["tailscale", "cloudflared"], default="tailscale")
    p_cmd.add_argument("--ttl", type=int, default=60)
    p_cmd.add_argument("--serve", action="store_true")
    p_cmd.set_defaults(_share_action="cmd")

    p_diff = subparsers.add_parser("share-diff", help="Share scoped git diff for a *subject or file group")
    p_diff.add_argument("spec", nargs="+", help="*subject or file path specs")
    p_diff.add_argument("--ref", default="HEAD", help="git base ref to diff against (default: HEAD)")
    p_diff.add_argument("--base", default=None, help="repository root directory")
    p_diff.add_argument("--via", choices=["tailscale", "cloudflared"], default="tailscale")
    p_diff.add_argument("--ttl", type=int, default=60)
    p_diff.add_argument("--serve", action="store_true")
    p_diff.set_defaults(_share_action="diff")

    try:
        from share.gh import register_gh_parsers
    except ImportError:
        try:
            from gh import register_gh_parsers  # type: ignore
        except ImportError:
            from .gh import register_gh_parsers  # type: ignore
    register_gh_parsers(subparsers)

    p_kinds = subparsers.add_parser("share-kinds", help="List available share-kind classifications")
    p_kinds.set_defaults(_share_action="kinds")

    p_lk = subparsers.add_parser("lookup", help="Look up shares by original file path (persistent index)")
    p_lk.add_argument("path", nargs="?", default=None, help="substring match, or omit for all")
    p_lk.set_defaults(_share_action="lookup")

    for compat_name, canon in (("tui", "share-tui"), ("stop", "share-stop"),
                              ("git", "share-git"), ("tree", "share-tree"),
                              ("cmd", "share-cmd"), ("diff", "share-diff"),
                              ("gh", "share-gh"), ("gh-temp", "share-gh"),
                              ("gh-promote", "share-gh-promote"),
                              ("gh-demote", "share-gh-demote"),
                              ("kinds", "share-kinds")):
        try:
            p_compat = subparsers.add_parser(compat_name, help=f"Alias for {canon}")
        except ValueError:
            continue  # host CLI (e.g. source) already owns this name
        p_compat.set_defaults(_share_action="__alias__", _share_alias=canon)

    p_tui = subparsers.add_parser("share-tui", help="Drop-zone TUI: paste/drag-drop into share/task/spawn")
    p_tui.add_argument("drops", nargs="*", help="file[:start-end] specs (non-interactive mode)")
    p_tui.add_argument("--zone", choices=["share", "task", "spawn", "export", "git-remote",
                                          "gh-temp", "source", "drag-out", "poll-clip"],
                       default="share")
    p_tui.add_argument("--test", action="store_true", help="headless dispatch without curses")
    p_tui.set_defaults(_share_action="share-tui")

    p_in = subparsers.add_parser("ingest", help="Ingest a git repo/branch URL (validates URL like the Import dialog)")
    p_in.add_argument("url", help="e.g. https://github.com/user/repo")
    p_in.add_argument("--dest", default=None)
    p_in.add_argument("--branch", default=None)
    p_in.set_defaults(_share_action="ingest")


_ALIAS_TO_ACTION = {"share-tui": "share-tui", "share-stop": "stop",
                     "share-git": "git", "share-tree": "tree", "share-cmd": "cmd",
                     "share-diff": "diff", "share-gh": "gh", "share-kinds": "kinds", "tui": "share-tui",
                     "share-gh-promote": "gh-promote", "share-gh-demote": "gh-demote",
                     "stop": "stop", "git": "git", "tree": "tree", "cmd": "cmd",
                     "diff": "diff", "gh": "gh", "gh-temp": "gh",
                     "gh-promote": "gh-promote", "gh-demote": "gh-demote", "kinds": "kinds"}


def dispatch_share(args: argparse.Namespace):
    action = getattr(args, "_share_action", None)
    if action == "__alias__":
        action = _ALIAS_TO_ACTION.get(getattr(args, "_share_alias", ""), "")
        args._share_action = action
    if action == "snippet":
        return do_share(args)
    elif action == "stop":
        return do_share_stop(args)
    elif action in ("git", "git-serve"):
        return _load_git_remote().dispatch_git_remote(args)
    elif action == "ingest":
        return do_ingest(args)
    elif action == "tree":
        return do_tree_share(args)
    elif action == "diff":
        return do_diff_share(args)
    elif action in ("gh", "gh-promote", "gh-demote"):
        try:
            from share.gh import dispatch_gh
        except ImportError:
            try:
                from gh import dispatch_gh  # type: ignore
            except ImportError:
                from .gh import dispatch_gh  # type: ignore
        return dispatch_gh(args)
    elif action == "cmd":
        cmd = [c for c in (getattr(args, "cmd", None) or []) if c != "--"]
        args.cmd = cmd
        return do_cmd_share(args)
    elif action == "kinds":
        print(json.dumps({"kinds": list(SHARE_KINDS),
                          "note": "git is one classification; add new kinds here"}, indent=2))
        return 0
    elif action == "lookup":
        print(json.dumps(lookup_index(getattr(args, "path", None)), indent=2))
        return 0
    elif action == "share-tui":
        try:
            from share.share_tui import main as share_tui_main
        except ImportError:
            try:
                from share_tui import main as share_tui_main
            except ImportError:
                from .share_tui import main as share_tui_main
        argv = list(getattr(args, "drops", None) or [])
        if getattr(args, "zone", None):
            argv += ["--zone", args.zone]
        if getattr(args, "test", False):
            argv.append("--test")
        return share_tui_main(argv)
    return None


def main(argv=None) -> int:
    """Standalone `share` CLI entry: `share <kind> ...`, no `source` prefix."""
    import sys
    raw0 = list(argv) if argv is not None else sys.argv[1:]
    if raw0 and raw0[0] == "_serve_bg":
        from argparse import ArgumentParser as _AP
        _p = _AP(prog="share _serve_bg")
        _p.add_argument("--kind", required=True)
        _p.add_argument("--target", required=True)
        _p.add_argument("--port", type=int, required=True)
        _p.add_argument("--slug", required=True)
        _a = _p.parse_args(raw0[1:])
        _run_bg_server(_a.kind, _a.target, _a.port, _a.slug)
        return 0
    # ensure sibling cli/src modules (subject, source) are importable
    _cli_src = str(Path(__file__).resolve().parent.parent)
    if _cli_src not in sys.path:
        sys.path.insert(0, _cli_src)
    from argparse import ArgumentParser
    from types import SimpleNamespace
    parser = ArgumentParser(prog="share",
                            description="Standalone quick-share modality: snippets, trees, "
                                        "command output, temp git remotes, ingest, drop-zone TUI")
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="subcommand")
    register_share_parsers(subparsers)
    # Flatten: `share snippet ...` -> share.py's `share` action, etc.
    alias = {"snippet": "share", "stop": "share-stop", "git": "share-git",
             "git-serve": "share-git-serve", "tree": "share-tree",
             "cmd": "share-cmd", "diff": "share-diff", "gh": "share-gh",
             "gh-promote": "share-gh-promote", "gh-demote": "share-gh-demote",
             "kinds": "share-kinds", "tui": "share-tui"}
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in alias and not raw[0].startswith("share") and raw[0] not in (
            "share", "share-stop", "share-git", "share-git-serve", "share-tree",
            "share-cmd", "share-diff", "share-gh", "share-gh-promote", "share-gh-demote",
            "share-kinds", "share-tui", "lookup", "ingest"):
        raw = [alias[raw[0]]] + raw[1:]
    if raw and raw[0] not in (getattr(parser, "_subparsers", None) and
                              [a for acts in parser._subparsers._group_actions
                               for a in acts.choices] or []) and not raw[0].startswith("-"):
        raw = ["share"] + raw  # bare `share file:lines` -> snippet share
    args = parser.parse_args(raw)
    if not getattr(args, "subcommand", None):
        # No subcommand: launch drop-zone TUI (interactive) or list kinds
        if sys.stdin.isatty() and sys.stdout.isatty():
            return dispatch_share(SimpleNamespace(_share_action="share-tui",
                                                  drops=[], zone="share", test=False))
        args = parser.parse_args(["share-kinds"])
    rc = dispatch_share(args)
    return rc if rc is not None else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

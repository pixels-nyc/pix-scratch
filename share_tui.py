#!/usr/bin/env python3
"""share_tui — drop-zone TUI for source shares.

Zones: [share] [task] [spawn] [export] [git-remote] [source].
Paste or drag-and-drop file paths (file[:start[-end]] preserved) into a zone:
- share: creates a snippet share via share.do_share
- task: prints a `task add` command suggestion with the payload attached
- spawn: prints a `spawn` command suggestion with the payload attached
- export: materializes the included files as an exported share bundle
- git-remote: creates a temp git remote from the dropped files
- source: interfaces back into `source` (status/vendor) for the payload

Paste/drag-drop arrives as bracketed paste (ESC[200~ ... ESC[201~) when the
terminal supports it, else as plain-text burst input.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

REAL_HOME = Path(os.environ.get("REAL_HOME") or os.environ.get("HOME") or Path.home())

_SHARE_DIR = str(Path(__file__).resolve().parent)
if _SHARE_DIR not in sys.path:
    sys.path.insert(0, _SHARE_DIR)

try:
    from share.share import (parse_file_spec, do_share, do_git_share, check_transport,
                             poll_clipboard, SHARE_KINDS, collect_snippet,
                             start_background_snippet_server_detached,
                             start_background_git_http_detached,
                             _is_pid_alive, _load_registry,
                             _free_port, GIT_DIR, SNIPPET_DIR, _register_share, record_index,
                             create_temp_remote)
except ImportError:
    try:
        from share import (parse_file_spec, do_share, do_git_share, check_transport,
                           poll_clipboard, SHARE_KINDS, collect_snippet,
                           start_background_snippet_server_detached,
                           start_background_git_http_detached,
                           _is_pid_alive, _load_registry,
                           _free_port, GIT_DIR, SNIPPET_DIR, _register_share, record_index,
                           create_temp_remote)
    except ImportError:
        from .share import (parse_file_spec, do_share, do_git_share, check_transport,
                            poll_clipboard, SHARE_KINDS, collect_snippet,
                            start_background_snippet_server_detached,
                            start_background_git_http_detached,
                            _is_pid_alive, _load_registry,
                            _free_port, GIT_DIR, SNIPPET_DIR, _register_share, record_index,
                            create_temp_remote)  # type: ignore
try:
    from share.gh import (create_temp_gh_repo, confirm_gh_identity,
                          list_pix_gh_accounts, sync_gh_repos_to_registry,
                          set_gh_repo_permanence)
except ImportError:
    try:
        from gh import (create_temp_gh_repo, confirm_gh_identity,  # type: ignore
                        list_pix_gh_accounts, sync_gh_repos_to_registry,
                        set_gh_repo_permanence)
    except ImportError:
        from .gh import (create_temp_gh_repo, confirm_gh_identity,  # type: ignore
                         list_pix_gh_accounts, sync_gh_repos_to_registry,
                         set_gh_repo_permanence)

try:
    from share.git_remote import publish_temp_git_remote
except ImportError:
    try:
        from git_remote import publish_temp_git_remote  # type: ignore
    except ImportError:
        from .git_remote import publish_temp_git_remote  # type: ignore

try:
    from subject.subject import get_subject_registry, resolve_subject_scope, format_kitty_dnd_drag_out
except ImportError:
    try:
        from common.subject import get_subject_registry, resolve_subject_scope, format_kitty_dnd_drag_out
    except ImportError:
        get_subject_registry = None  # type: ignore
        resolve_subject_scope = None  # type: ignore
        format_kitty_dnd_drag_out = None  # type: ignore

try:
    from source.source import inspect_source_versioning
except ImportError:
    try:
        from source import inspect_source_versioning  # type: ignore
    except ImportError:
        inspect_source_versioning = None  # type: ignore

ZONES = ("share", "task", "spawn", "export", "git-remote", "gh-temp", "source", "drag-out", "poll-clip")
TABS = ("Drop & Route", "Groups", "Subject Groups", "Viewer / Peek", "History & Index", "Services & Hosts")

_SUBJECT_REGISTRY_CACHE: Dict[str, object] = {}


def _cached_subject_registry(base) -> object:
    """Reuse one SubjectRegistry per base dir: construction + first resolve
    costs ~1s (pragma tree walk); per-call construction re-pays it every frame."""
    if get_subject_registry is None:
        return None
    key = str(Path(base).resolve()) if base else ""
    reg = _SUBJECT_REGISTRY_CACHE.get(key)
    if reg is None:
        reg = get_subject_registry(root_dir=Path(base)) if base else get_subject_registry()
        _SUBJECT_REGISTRY_CACHE[key] = reg
    return reg

DROP_SPLIT_RE = re.compile(r"[\s\n]+")

PASTE_OPEN = "\x1b[200~"
PASTE_CLOSE = "\x1b[201~"


def strip_paste_markers(s: str) -> str:
    s = re.sub(r"^(?:\x1b\[200~|\[200~)+", "", s)
    s = re.sub(r"(?:\x1b\[201~|\[201~)+$", "", s)
    return s.strip().strip("'\"")


def _reassert_terminal_modes() -> None:
    """Re-enable bracketed paste + SGR any-event mouse (terminals reset these on DND)."""
    try:
        sys.stdout.write("\033[?2004h\033[?1000h\033[?1003h\033[?1006h")
        sys.stdout.flush()
    except Exception:
        pass


def parse_drops(text: str) -> List[str]:
    """Split a paste/drop burst into file specs, keeping file[:start-end]."""
    # markers can appear mid-burst (per-char reads), so strip everywhere
    text = text.replace(PASTE_OPEN, " ").replace(PASTE_CLOSE, " ")
    text = text.replace("[200~", " ").replace("[201~", " ")
    # unified-diff paste: keep only +++ b/<path> targets, drop ---/@@/context
    # (prevents 179-token storms and bogus prose matches from diff bodies)
    if "--- " in text and "+++ " in text:
        targets = re.findall(r"^\+\+\+\s+(?:b/)?(\S+)", text, re.M)
        text = "\n".join(t for t in targets if t not in ("/dev/null", "null"))
    # back-to-back drag-drops concatenate without whitespace:
    # navigator.py/home/super/... -> navigator.py /home/super/...
    if len(text) <= 4096:
        text = re.sub(r"(?<=[^\s/])/(?=(?:home|tmp|etc|usr|var|opt|mnt|srv|root|data|run)/)", r" /", text)
    text = strip_paste_markers(text)
    # extract file:// URIs BEFORE whitespace splitting (%20 decodes to space post-split)
    uri_paths: List[str] = []
    def _take_uri(m: "re.Match") -> str:
        raw = m.group(0)[len("file://"):]
        uri_paths.append(unquote(raw).rstrip("/") or "/")
        return " "
    text = re.sub(r"file://\S+", _take_uri, text)
    parts = [p.strip().strip("'\"").strip("'\"") for p in DROP_SPLIT_RE.split(text) if p.strip()]
    fixed = list(uri_paths)
    for p in parts:
        if len(p) > 256 or any(ch in p for ch in ("(", ")", "[", "]", "{", "}", "|", "*", "?", "+", "^", "$", "\\")):
            continue
        if not p or p.startswith("-"):
            continue
        if p.startswith("home/"):
            p = "/" + p  # leading slash eaten by the drop
        p = p.rstrip("/") or "/"
        fixed.append(p)
    return fixed


def yank_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard via wl-copy (Wayland) or xclip/xsel."""
    if not text or os.environ.get("PIX_TEST") == "1":
        return False
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=2)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
    return False


def expand_subject_specs(specs: List[str], base: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Expand `*subject` tokens into concrete line-bounded file specs."""
    if resolve_subject_scope is None:
        return list(specs), []
    expanded: List[str] = []
    subjects: List[str] = []
    root = Path(base) if base else None
    for item in specs:
        if item.startswith("*"):
            try:
                scope = resolve_subject_scope(item, root_dir=root)
            except Exception:
                continue
            subjects.extend(n for n in scope.names if n not in subjects)
            for sp in scope.to_share_specs():
                if sp not in expanded:
                    expanded.append(sp)
        elif item not in expanded:
            expanded.append(item)
    return expanded, subjects


def _resolve_base(specs: List[str], base: Optional[str]) -> str:
    """Pick a cwd base that actually contains the dropped paths.

    Multi-file drops from different subdirs resolve via commonpath so the
    temp git remote never comes back empty. Single absolute paths outside
    base walk up to the repo/workspace root.
    """
    paths = []
    for s in specs or []:
        rel, _, _ = s.partition(":")
        p = Path(rel)
        if p.is_absolute():
            paths.append(str(p if p.is_file() or not p.exists() else p))
        elif base:
            paths.append(str((Path(base) / rel).resolve()))
    if len(paths) > 1:
        try:
            common = os.path.commonpath([p if os.path.isfile(p) else os.path.dirname(p) or p for p in paths])
            if Path(common).is_dir():
                return common
        except Exception:
            pass
    if base and Path(base).is_dir():
        first = specs[0] if specs else ""
        rel, _, _ = first.partition(":")
        cand = Path(rel)
        if not cand.is_absolute() or str(cand).startswith(str(Path(base).resolve())):
            return base
        # absolute path outside base: use its own directory tree root
        p = cand if cand.is_file() else cand.parent
        for parent in [p] + list(p.parents):
            if (parent / ".git").exists() or parent.name in ("pixos", "Projects"):
                return str(parent)
            if str(parent) == str(REAL_HOME):
                return str(parent)
        return str(p.parent if p.is_file() else p)
    return str(REAL_HOME)


def preview_payload(buf: str, base: Optional[str] = None) -> List[str]:
    """Resolve current buffer specs before dispatch: file existence & line bounds."""
    specs = parse_drops(buf)
    if not specs:
        return []
    expanded, subjects = expand_subject_specs(specs, base)
    resolved_base = Path(_resolve_base(expanded or specs, base or os.getcwd()))
    lines_out = []
    if subjects:
        lines_out.append(f"  [SUBJECT] *{', *'.join(subjects)} -> {len(expanded)} spec(s)")
    peek_budget = 6 if len(expanded) == 1 else 2
    for spec in expanded[:6]:
        rel, start, end = parse_file_spec(spec)
        p = (resolved_base / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        if not p.exists():
            lines_out.append(f"  [MISSING] {spec}")
            continue
        if p.is_dir():
            lines_out.append(f"  [DIR]     {spec}")
            continue
        try:
            file_lines = p.read_text(errors="replace").splitlines()
            total = len(file_lines)
        except OSError as e:
            lines_out.append(f"  [ERR]     {spec} ({e})")
            continue
        if start is not None:
            in_bounds = 1 <= start <= (end or start) <= total
            flag = "OK" if in_bounds else "OUT-OF-RANGE"
            lines_out.append(f"  [{flag}] {rel}:{start}-{end or start} (of {total}L)")
            if in_bounds:
                slice_lines = file_lines[start - 1: min(end or start, start - 1 + peek_budget)]
                for idx_off, code_ln in enumerate(slice_lines, start=start):
                    lines_out.append(f"    {idx_off:>4} | {code_ln[:88]}")
        else:
            lines_out.append(f"  [OK]      {rel} ({total}L)")
            for idx_off, code_ln in enumerate(file_lines[:peek_budget], start=1):
                lines_out.append(f"    {idx_off:>4} | {code_ln[:88]}")
    return lines_out


def _sanitize_slug(raw: str | None, fallback: str = "pix-scratch") -> str:
    """Sanitize a group name or prompt entry into a GitHub-safe repo slug."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", (raw or "").strip()).strip("-") or fallback


def zone_action(zone: str, specs: List[str], base: Optional[str] = None,
                via: str = "tailscale", gh_public: Optional[bool] = None,
                gh_slug: Optional[str] = None, gh_account: Optional[str] = None,
                author_override: Optional[Tuple[str, str]] = None,
                gh_permanent: bool = False, group: Optional[str] = None) -> dict:
    """Route dropped specs to a zone. Returns a result dict (no curses)."""
    if zone not in ZONES:
        return {"ok": False, "error": f"unknown zone: {zone}"}
    if not specs:
        return {"ok": False, "error": "nothing dropped: paste file paths first"}
    group_name = (group or "").strip() or None
    specs, subjects = expand_subject_specs(specs, base)
    if not specs:
        return {"ok": False, "error": f"subject(s) resolved to 0 existing files"}
    if zone in ("share", "export", "git-remote") and base is None:
        base = _resolve_base(specs, os.getcwd())
    if zone == "share":
        # Fast-track: build + serve in-process so Enter spins up the server (no stdout prints in curses)
        resolved_dir = Path(base or os.getcwd()).resolve()
        try:
            items = [collect_snippet(resolved_dir, s) for s in specs]
        except Exception as e:
            return {"ok": False, "error": str(e)}
        slug = datetime.now(timezone.utc).strftime("share-%Y%m%d-%H%M%S")
        port = _free_port()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
        bundle = {"slug": slug, "base": str(resolved_dir), "items": items,
                  "port": port, "transport": via, "expires": expires}
        SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
        bpath = SNIPPET_DIR / f"{slug}.json"
        bpath.write_text(json.dumps(bundle, indent=2))
        reg = _load_registry()
        existing = None
        for _s, _e in reg.items():
            if _e.get("kind") == "snippet" and _e.get("files") == [i["file"] for i in items] and _is_pid_alive(_e.get("pid")):
                existing = (_s, _e)
                break
        if existing:
            _s, _e = existing
            url = f"http://127.0.0.1:{_e['port']}"
            try:
                from share.share import _tailscale_node_url as _tnu
            except ImportError:
                _tnu = None
            if _tnu:
                try:
                    url = _tnu(_e["port"]) or url
                except Exception:
                    pass
            return {"ok": True, "zone": zone, "slug": _s, "specs": specs, "subjects": subjects,
                    "url": url, "download_url": f"{url}/download", "reused": True,
                    "suggested_cmd": f"curl -sL {url}/text", "remote_url": f"{url}/text"}
        _register_share(slug, "snippet", port, via, 60, {"files": [i["file"] for i in items]})
        record_index(slug, "snippet", str(resolved_dir), specs, [i["file"] for i in items], str(bpath))
        try:
            pid, url = start_background_snippet_server_detached(str(bpath), port, slug)
        except Exception as e:
            return {"ok": False, "error": f"serve failed: {e}"}
        _register_share(slug, "snippet", port, via, 60, {"files": [i["file"] for i in items], "pid": pid})
        return {"ok": True, "zone": zone, "slug": slug, "specs": specs, "subjects": subjects,
                "url": url, "download_url": f"{url}/download", "pid": pid,
                "suggested_cmd": f"curl -sL {url}/text", "remote_url": f"{url}/text"}
    if zone == "export":
        # Export scope: pin into *subject, materialize the snippet bundle, AND
        # publish a public GitHub repo via pix auth so the URL is import-ready.
        # Set via="local" to skip GitHub and keep the bundle-only behavior.
        target_subj = (subjects[0] if subjects else "").strip().lstrip("*").lower()
        pinned_count = 0
        if target_subj and get_subject_registry is not None:
            try:
                reg_sub = _cached_subject_registry(Path(base or os.getcwd()))
                for sp in specs:
                    reg_sub.pin_lines(target_subj, sp, description=f"Exported scope: *{target_subj}")
                    pinned_count += 1
            except Exception as e:
                return {"ok": False, "error": f"subject pin failed: {e}"}
        args = SimpleNamespace(files=specs, base=base or os.getcwd(),
                               via=via, ttl=60, serve=False, json=True)
        rc = do_share(args)
        is_public = True if gh_public is None else bool(gh_public)
        slug = _sanitize_slug(group_name or target_subj or gh_slug)
        if via == "local":
            out = {"ok": rc == 0, "zone": zone, "specs": specs, "subjects": subjects,
                   "exported": True,
                   "suggested_cmd": f"share diff *{target_subj}" if target_subj else f"source share {' '.join(specs)}"}
        else:
            try:
                gh_res = create_temp_gh_repo(specs, Path(base or os.getcwd()).resolve(),
                                             slug, ttl_min=60, private=not is_public,
                                             account=gh_account, author_override=author_override,
                                             permanent=gh_permanent, group=group_name)
                out = {"ok": rc == 0, "zone": zone, "group": group_name, "specs": specs, "subjects": subjects,
                       "exported": True, "slug": gh_res["slug"],
                       "repo": gh_res["repo"], "url": gh_res["url"], "remote_url": gh_res["url"],
                       "clone_url": f"{gh_res['url']}.git",
                       "account": gh_res.get("account"), "author": gh_res.get("author"),
                       "permanent": bool(gh_permanent),
                       "visibility": "public" if is_public else "private",
                       "suggested_cmd": gh_res["url"]}
            except Exception as e:
                out = {"ok": False, "zone": zone, "specs": specs, "subjects": subjects,
                       "error": f"GitHub export failed: {e}",
                       "suggested_cmd": f"share diff *{target_subj}" if target_subj else f"source share {' '.join(specs)}"}
        if target_subj:
            out["subject"] = target_subj
            out["pinned_count"] = pinned_count
        return out
    if zone == "git-remote":
        # Publish a public GitHub repo via pix auth by default (toggle/PIX_GH_PUBLIC
        # control visibility). Set via="local" for the Tailscale-served snapshot.
        if via == "local":
            resolved_dir = base or os.getcwd()
            try:
                res = publish_temp_git_remote(specs, Path(resolved_dir), via="tailscale", ttl_min=60)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "zone": zone, "slug": res["slug"], "specs": specs, "subjects": subjects,
                    "repo_path": res["repo_path"], "clone_url": res["clone_url"],
                    "reused": res.get("reused", False),
                    "remote_url": res["remote_url"],
                    "suggested_cmd": f"git clone {res['clone_url']} {res['slug']}"}
        resolved_dir = Path(base or os.getcwd()).resolve()
        slug = _sanitize_slug(group_name or (subjects[0].strip().lstrip("*").lower() if subjects else "") or gh_slug)
        is_public = True if gh_public is None else bool(gh_public)
        try:
            res = create_temp_gh_repo(specs, resolved_dir, slug, ttl_min=60,
                                      private=not is_public, account=gh_account,
                                      author_override=author_override,
                                      permanent=gh_permanent, group=group_name)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "zone": zone, "group": group_name, "slug": res["slug"], "specs": specs,
                "subjects": subjects, "url": res["url"], "remote_url": res["url"],
                "clone_url": f"{res['url']}.git", "account": res.get("account"),
                "author": res.get("author"), "permanent": bool(gh_permanent),
                "repo": res["repo"], "visibility": "public" if is_public else "private",
                "suggested_cmd": res["url"]}
    if zone == "source":
        first_rel, _, _ = parse_file_spec(specs[0])
        target = Path(first_rel) if os.path.isabs(first_rel) else Path(base or os.getcwd()) / first_rel
        vinfo = inspect_source_versioning(target) if inspect_source_versioning else {"path": str(target)}
        proj = vinfo.get("project", Path(first_rel).name)
        br = vinfo.get("branch", "?")
        rev = vinfo.get("revision", "?")
        return {"ok": True, "zone": zone, "specs": specs, "subjects": subjects,
                "versioning": vinfo,
                "suggested_cmd": f"source status {proj}  # {vinfo.get('type', 'repo')} @ {br} ({rev})"}
    if zone == "gh-temp":
        resolved_dir = Path(base or os.getcwd()).resolve()
        slug = _sanitize_slug(group_name or gh_slug)
        # Visibility follows the Tab 1 [GH:Public]/[GH:Private] toggle,
        # falling back to PIX_GH_PUBLIC=0 for a private default.
        is_public = True if gh_public is None else bool(gh_public)
        try:
            res = create_temp_gh_repo(specs, resolved_dir, slug, ttl_min=60,
                                      private=not is_public, account=gh_account,
                                      author_override=author_override,
                                      permanent=gh_permanent, group=group_name)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "zone": zone, "group": group_name, "slug": slug, "specs": specs,
                "subjects": subjects, "url": res["url"], "remote_url": res["url"],
                "repo": res["repo"], "account": res.get("account"),
                "author": res.get("author"), "permanent": bool(gh_permanent),
                "visibility": "public" if is_public else "private",
                "suggested_cmd": res["url"]}
    if zone == "drag-out":
        if format_kitty_dnd_drag_out is None:
            return {"ok": False, "error": "kitty DND unavailable"}
        seq = format_kitty_dnd_drag_out(specs, root_dir=Path(base) if base else None)
        return {"ok": True, "zone": zone, "specs": specs, "subjects": subjects,
                "kitty_dnd_seq": seq,
                "suggested_cmd": f"kitty-dnd drag-out ({len(specs)} item(s))"}
    # task / spawn: emit suggested commands for the caller to run
    quoted = " ".join(specs)
    if zone == "task":
        cmd = f"task add --attach {quoted}"
        return {"ok": True, "zone": zone, "specs": specs, "subjects": subjects, "suggested_cmd": cmd}
    elif zone == "poll-clip":
        text, src = poll_clipboard()
        specs = parse_drops(text)
        if not specs:
            return {"ok": False, "error": f"clipboard ({src}) has no file paths to share"}
        return {"ok": True, "zone": zone, "specs": specs, "clipboard_src": src,
                "suggested_cmd": f"share {' '.join(specs)}  (from {src})"}
    else:
        cmd = f"spawn --context {quoted}"
    return {"ok": True, "zone": zone, "specs": specs, "subjects": subjects, "suggested_cmd": cmd}



class ShareTUIState:
    """Stateful controller for tabs, file-drop selection checkboxes, subject groupings, and mouse hitboxes."""

    def __init__(self, zone: str = "share", base=None):
        self.tab_idx: int = 0
        self.zone_idx: int = list(ZONES).index(zone) if zone in ZONES else 0
        self.base: str = base or os.getcwd()
        self.buf: str = ""
        self.items: List[str] = []
        self.selected: Set[int] = set()
        self.anchor_idx = None
        self.drag_start_idx = None
        self.line_anchor = None  # (item_idx, start_line) for click-to-range
        self.hover_pos = None   # (y, x) of current mouse cursor
        self.cursor_idx: int = 0
        self.svc_cursor: int = 0    # focused row in Tab 5 Services & Hosts
        self.svc_scroll: int = 0    # scroll offset for Tab 5 service list
        self.svc_filter: str = "live"  # "live" | "all" | "retired"
        self.svc_kind_filter: str = "all"  # "all" | "gh" | "gh-perm" | "git" | "snippet"
        self.svc_expanded: bool = False  # Layer 3 inspector drawer expansion
        self.active_subjects: List[str] = []
        self.viewer_scroll: int = 0
        self.split_pane: bool = True
        self.auto_drop: bool = False
        self.gh_public: bool = os.environ.get("PIX_GH_PUBLIC", "1") != "0"
        self.gh_permanent: bool = False
        self.gh_slug: str = "pix-scratch"
        self.gh_slug_defined: bool = False
        self.gh_account: str = ""
        self.gh_accounts: List[str] = []
        self.gh_acct_cursor: int = 0
        self.gh_acct_filter: str = ""
        self.gh_identity: dict = {}
        self.auth_confirmed: bool = False
        self.auth_confirm_modal: bool = False
        self.gh_acct_dropdown: bool = False
        self.gh_acct_anchor: tuple = (0, 0)
        self.last_yank: str = ""
        self.last_remote_url: str = ""
        self.last_clone_cmd: str = ""
        self.last_slug: str = ""
        self.msg: str = "Click/Tab zones, click/drag items to select, [p]in to *subject, [v] split"
        self.result_lines: List[str] = []
        self.history: List[dict] = []
        self.hitboxes: List[Tuple[int, int, int, str, object]] = []
        self.pending: List[dict] = []  # unsure (?) candidates awaiting confirm
        self.groups: Dict[str, List[str]] = {}
        self.group_cursor: int = 0
        self.active_group: str = ""
        self.confirm_flash: str = ""  # transient bottom-bar confirm ("copied ..."), cleared on next key/mouse
        self._file_cache: Tuple[str, int, List[str]] = ("", 0, [])

    def refresh_gh_identity(self, cycle_account: bool = False) -> dict:
        """Probe active pix gh account, OAuth scopes, and git commit author."""
        try:
            accts = list_pix_gh_accounts()
        except Exception:
            accts = []
        if accts:
            self.gh_accounts = accts
        if cycle_account and self.gh_accounts:
            if self.gh_account in self.gh_accounts:
                nxt = (self.gh_accounts.index(self.gh_account) + 1) % len(self.gh_accounts)
                self.gh_account = self.gh_accounts[nxt]
            else:
                self.gh_account = self.gh_accounts[0]
            self.auth_confirmed = False
        try:
            info = confirm_gh_identity(self.gh_account or None, Path(self.base))
        except Exception as e:
            info = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.gh_identity = info if isinstance(info, dict) else {}
        if self.gh_identity.get("ok") and not self.gh_account:
            self.gh_account = self.gh_identity.get("account", "")
        return self.gh_identity

    def cycle_gh_account(self) -> None:
        info = self.refresh_gh_identity(cycle_account=True)
        if info.get("ok"):
            scope_warn = "" if info.get("can_delete") else " [NO delete_repo scope]"
            self.msg = f"gh auth -> @{info['account']} | author: {info['author']}{scope_warn}"
        else:
            self.msg = f"gh auth error: {info.get('error', 'unknown')}"

    def gh_dropdown_accounts(self) -> List[str]:
        """Accounts filtered by the dropdown search string (case-insensitive)."""
        if not self.gh_acct_filter.strip():
            return list(self.gh_accounts)
        q = self.gh_acct_filter.strip().lower()
        return [a for a in self.gh_accounts if q in a.lower()]

    def open_gh_dropdown(self) -> None:
        try:
            accts = list_pix_gh_accounts()
        except Exception:
            accts = []
        if accts:
            self.gh_accounts = accts
        self.gh_acct_dropdown = True
        self.gh_acct_filter = ""
        if self.gh_account in self.gh_accounts:
            self.gh_acct_cursor = self.gh_accounts.index(self.gh_account)
        else:
            self.gh_acct_cursor = 0
        self.msg = f"gh account: {len(self.gh_accounts)} from pix — type to filter, Enter to select"

    def select_gh_account(self, account: str) -> None:
        clean = (account or "").strip()
        if not clean:
            self.msg = "gh account: empty selection"
            return
        self.gh_account = clean
        self.auth_confirmed = False
        self.gh_acct_dropdown = False
        self.gh_acct_filter = ""
        info = self.refresh_gh_identity(cycle_account=False)
        if info.get("ok"):
            scope_warn = "" if info.get("can_delete") else " [NO delete_repo scope]"
            self.msg = f"gh auth -> @{info['account']} | author: {info['author']}{scope_warn}"
        else:
            self.msg = f"gh auth error: {info.get('error', 'unknown')}"

    def ingest_buffer(self, raw_text) -> int:
        if isinstance(raw_text, str):
            specs = parse_drops(raw_text)
        else:
            specs = list(raw_text)
        if not specs:
            return 0
        resolved_base = _resolve_base(specs, self.base)
        if Path(resolved_base).is_dir():
            self.base = resolved_base
        added = 0
        for sp in specs:
            if sp.startswith("*"):
                self.toggle_subject(sp.lstrip("*"))
                added += 1
            elif sp not in self.items:
                idx = len(self.items)
                self.items.append(sp)
                self.selected.add(idx)
                self.cursor_idx = idx
                added += 1
        return added

    def populate_from_clipboard(self, max_items: int = 50) -> int:
        """Read paths/URIs from clipboard and stage them into Tab 0."""
        text, src = poll_clipboard()
        if not text or not text.strip():
            self.msg = f"clipboard ({src}) is empty"
            return 0
        # Unified diffs & file:// bursts already have a fast linear parser in parse_drops
        if ("--- " in text and "+++ " in text) or "file://" in text:
            kept = parse_drops(text)[:max_items]
        else:
            # Linear whitespace tokenization (avoids catastrophic backtracking on regex pastes)
            kept = []
            for raw_tok in text.split():
                tok = raw_tok.strip("'\"(),;`<>[]")
                if not tok or len(tok) > 256:
                    continue
                if any(ch in tok for ch in ("(", ")", "[", "]", "{", "}", "|", "*", "?", "+", "^", "$", "\\", "=", ";", "&", "!")):
                    if not tok.startswith("*"):
                        continue
                if tok.startswith("*"):
                    if len(tok) > 1:
                        kept.append(tok)
                elif "/" in tok:
                    base = tok.rstrip("/").rsplit("/", 1)[-1]
                    if ("." in base or tok.count("/") >= 2) and not (tok.startswith("/") and tok.endswith("/")):
                        kept.append(tok)
                else:
                    m_ext = re.search(r"\.([A-Za-z0-9]{1,5})(?::\d+(?:-\d+)?)?$", tok)
                    if m_ext and m_ext.group(1) not in ("items", "split", "append", "strip", "join"):
                        kept.append(tok)
                if len(kept) >= max_items:
                    break
        if not kept:
            self.msg = f"clipboard ({src}) has no valid file paths"
            return 0
        staged, unsure = self.stage_with_confirm(kept)
        if not staged and not unsure:
            if self.items:
                self.msg = (f"clipboard ({src}) has no file paths — "
                            f"{len(self.items)} staged item(s) kept — Enter to dispatch")
            else:
                self.msg = f"clipboard ({src}) has no valid file paths"
            return 0
        self.tab_idx = 0
        extra = f" + {unsure} (?) to confirm" if unsure else ""
        self.msg = f"staged {staged} item(s) from {src}{extra} — Enter to dispatch"
        return staged

    def lookup_candidates(self, name: str, limit: int = 5) -> List[str]:
        """Resolve an unsure filename via subject bookmarks, source registry, and disk search.

        Mirrors smart-open's token hygiene (clean_token + line-suffix split):
        trailing sentence punctuation ('.', '?', '...') and markdown/bracket
        wrappers are stripped before matching.
        """
        rel, _, _ = name.partition(":")
        frag = rel.strip()
        # markdown link target: [label](path)
        m_md = re.search(r"\[.*?\]\((?:file://)?([^\)]+)\)", frag)
        if m_md:
            frag = m_md.group(1)
        frag = re.sub(r"^[`'\"<\(\[\{]+", "", frag)
        # trailing sentence punctuation: '...', '.', '?', '!', ',', ';' (smart clean_token parity)
        frag = re.sub(r"\.{2,}$", "", frag)
        frag = re.sub(r"[`'\">\)\],;.!?]+$", "", frag)
        frag = os.path.expanduser(frag.strip())
        if not frag:
            return []
        # Skip prose tokens early: no extension and no path depth means no
        # disk leg could ever match (avoids fd/find storms on table pastes).
        _tail = frag.rstrip("/").rsplit("/", 1)[-1]
        if "/" not in frag.rstrip("/") and not re.search(r"\.[A-Za-z0-9]{1,5}$", _tail):
            # still allow subject/source registry legs below; disk leg gated later
            pass
        base = Path(self.base) if Path(self.base).is_dir() else Path(os.getcwd())
        bare = Path(frag).name
        has_file_shape = "/" in frag or bool(re.search(r"\.[A-Za-z0-9]{1,5}$", _tail))
        hits: List[str] = []
        # 1. subject bookmark paths containing the name
        try:
            reg = _cached_subject_registry(base)
            if reg is not None:
                for bm in reg.list_all():
                    for p in bm.paths:
                        if bare.lower() in p.lower() or frag.lower() in p.lower():
                            cand = p if Path(p).is_absolute() else str((base / p))
                            if cand not in hits:
                                hits.append(cand)
                            if len(hits) >= limit:
                                return hits
        except Exception:
            pass
        # 2. source project registry paths
        try:
            from source.source import load_registry as _load_src
        except ImportError:
            try:
                from source import load_registry as _load_src  # type: ignore
            except ImportError:
                _load_src = None  # type: ignore
        try:
            if _load_src is not None:
                for _n, _d in (_load_src() or {}).items():
                    _p = (_d or {}).get("path")
                    if _p and (bare.lower() in _p.lower() or frag.lower() in _p.lower()):
                        if _p not in hits:
                            hits.append(_p)
                        if len(hits) >= limit:
                            return hits
        except Exception:
            pass
        # 3. disk: targeted finds (bounded; repo-wide rglob truncates on big trees).
        # Mirrors smart-open resolve_candidate_path: hyphen/underscore-insensitive
        # basename fallback when the exact name misses. Skipped entirely for
        # non-file-shaped tokens (prose words) to avoid subprocess storms.
        if not has_file_shape:
            return hits
        import subprocess as _sp
        # Deduplicate resolved search roots; never spawn redundant find/fuzzy passes if fd exists
        raw_roots = [base]
        try:
            toplevel = _sp.run(["git", "rev-parse", "--show-toplevel"], cwd=str(base),
                               capture_output=True, text=True, timeout=1)
            if toplevel.returncode == 0 and toplevel.stdout.strip():
                raw_roots.append(Path(toplevel.stdout.strip()))
        except Exception:
            pass
        for _sub in ("cli/src", "cli", "src"):
            _c = base / _sub
            if _c.is_dir():
                raw_roots.append(_c)
        search_roots: List[Path] = []
        seen_roots: Set[str] = set()
        for r in raw_roots:
            rp = str(r.resolve())
            if rp not in seen_roots:
                seen_roots.add(rp)
                search_roots.append(r)
        fd_bin = shutil.which("fd")
        fuzzy_pat = re.sub(r"[-_]", "[-_]", re.escape(bare)) if bare else bare
        for root in search_roots:
            if len(hits) >= limit:
                break
            if fd_bin:
                try:
                    pat = fuzzy_pat if (fuzzy_pat and fuzzy_pat != bare) else re.escape(bare)
                    fd = _sp.run([fd_bin, "-i", "-t", "f", "--max-results", str(limit), pat, str(root)],
                                 capture_output=True, text=True, timeout=1)
                    if fd.returncode == 0 and fd.stdout.strip():
                        for ln in fd.stdout.splitlines():
                            if ln.strip() and ln.strip() not in hits:
                                hits.append(ln.strip())
                                if len(hits) >= limit:
                                    return hits
                except Exception:
                    pass
            else:
                try:
                    find = _sp.run(["find", str(root), "-maxdepth", "6", "-iname", bare,
                                    "-not", "-path", "*/node_modules/*",
                                    "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*"],
                                   capture_output=True, text=True, timeout=2)
                    if find.returncode == 0 and find.stdout.strip():
                        for ln in find.stdout.splitlines():
                            if ln.strip() and ln.strip() not in hits:
                                hits.append(ln.strip())
                                if len(hits) >= limit:
                                    return hits
                except Exception:
                    pass
        return hits

    def stage_with_confirm(self, raw_text) -> Tuple[int, int]:
        """Stage resolvable specs now; park unsure ones in pending (?) for confirm.

        Returns (staged_count, pending_count)."""
        if isinstance(raw_text, str):
            specs = parse_drops(raw_text)
        else:
            specs = list(raw_text)
        staged, unsure = 0, 0
        base = Path(self.base) if Path(self.base).is_dir() else Path(os.getcwd())
        lookups_done = 0
        for sp in specs:
            if sp.startswith("*"):
                self.toggle_subject(sp.lstrip("*"))
                staged += 1
                continue
            if len(sp) > 256 or any(ch in sp for ch in ("(", ")", "[", "]", "{", "}", "|", "*", "?", "+", "^", "$", "\\", "<", ">", "=", ";", "&", "!")):
                continue
            # Reject regex-like slash patterns (e.g. s/foo/bar/g or /pattern/flags)
            if sp.startswith("/") and sp.endswith("/") and len(sp) > 2:
                continue
            rel, _, _ = sp.partition(":")
            tail = rel.rstrip("/").rsplit("/", 1)[-1]
            file_shaped = "/" in rel.rstrip("/") or bool(re.search(r"\.[A-Za-z0-9]{1,5}$", tail))
            if not file_shaped and not sp.startswith("*"):
                continue  # prose word: never staged, never pending
            direct = Path(rel) if Path(rel).is_absolute() else (base / rel)
            if direct.exists():
                if sp not in self.items:
                    idx = len(self.items)
                    self.items.append(sp)
                    self.selected.add(idx)
                    self.cursor_idx = idx
                    staged += 1
                continue
            if lookups_done >= 8:
                if not any(p["query"] == sp for p in self.pending):
                    self.pending.append({"query": sp, "candidates": [], "sel": 0})
                unsure += 1
                continue
            lookups_done += 1
            cands = self.lookup_candidates(sp)
            if len(cands) == 1:
                sole = cands[0]
                suffix = sp[len(rel):] if sp.startswith(rel) else ""
                item = sole + suffix if suffix.startswith(":") else sole
                if item not in self.items:
                    idx = len(self.items)
                    self.items.append(item)
                    self.selected.add(idx)
                    self.cursor_idx = idx
                    staged += 1
            elif cands:
                if not any(p["query"] == sp for p in self.pending):
                    self.pending.append({"query": sp, "candidates": cands, "sel": 0})
                unsure += 1
            else:
                if not any(p["query"] == sp for p in self.pending):
                    self.pending.append({"query": sp, "candidates": [], "sel": 0})
                unsure += 1
        if staged:
            resolved_base = _resolve_base(self.items, self.base)
            if Path(resolved_base).is_dir():
                self.base = resolved_base
        return staged, unsure

    def confirm_pending(self, idx: int = 0) -> bool:
        """Confirm a pending (?) candidate into staged items. Returns True on success."""
        if not (0 <= idx < len(self.pending)):
            return False
        entry = self.pending[idx]
        cands = entry.get("candidates") or []
        if not cands:
            self.msg = f"(?) {entry['query']}: no matches — dropped"
            self.pending.pop(idx)
            return False
        choice = cands[min(entry.get("sel", 0), len(cands) - 1)]
        rel, _, _ = entry["query"].partition(":")
        suffix = entry["query"][len(rel):] if entry["query"].startswith(rel) else ""
        item = choice + suffix if suffix.startswith(":") else choice
        if item not in self.items:
            ni = len(self.items)
            self.items.append(item)
            self.selected.add(ni)
            self.cursor_idx = ni
        self.pending.pop(idx)
        self.msg = f"confirmed {item} ({len(self.pending)} (?) left)"
        return True

    def toggle_item(self, idx: int) -> None:
        if 0 <= idx < len(self.items):
            self.cursor_idx = idx
            self.anchor_idx = idx
            if idx in self.selected:
                self.selected.remove(idx)
            else:
                self.selected.add(idx)

    def select_range(self, idx_a: int, idx_b: int, additive: bool = False) -> None:
        if not self.items:
            return
        lo = max(0, min(idx_a, idx_b))
        hi = min(len(self.items) - 1, max(idx_a, idx_b))
        span = set(range(lo, hi + 1))
        if additive:
            self.selected.update(span)
        else:
            self.selected = span
        self.cursor_idx = max(0, min(len(self.items) - 1, idx_b))

    def select_all(self, state: bool = True) -> None:
        self.selected = set(range(len(self.items))) if state else set()

    def toggle_subject(self, subj_name: str) -> None:
        clean = subj_name.strip().lstrip("*").lower()
        if get_subject_registry is None:
            self.msg = "subject subsystem unavailable"
            return
        reg = _cached_subject_registry(Path(self.base))
        scope = reg.resolve(clean)
        if clean in self.active_subjects:
            self.active_subjects.remove(clean)
            to_drop = set(scope.to_share_specs())
            self.items = [it for it in self.items if it not in to_drop]
            self.selected = set(range(len(self.items)))
            self.msg = f"unlinked *{clean}"
        else:
            self.active_subjects.append(clean)
            for sp in scope.to_share_specs():
                if sp not in self.items:
                    idx = len(self.items)
                    self.items.append(sp)
                    self.selected.add(idx)
            self.msg = f"grouped *{clean} ({len(scope.to_share_specs())} spec(s))"

    def pin_selected_to_subject(self, subject_name: str) -> bool:
        chosen = self.active_specs()
        clean = subject_name.strip().lstrip("*").lower()
        if not chosen or not clean:
            self.msg = "select at least one file and provide a subject name"
            return False
        if get_subject_registry is None:
            self.msg = "subject subsystem unavailable"
            return False
        reg = _cached_subject_registry(Path(self.base))
        for sp in chosen:
            reg.pin_lines(clean, sp, description=f"TUI grouped subject: *{clean}")
        if clean not in self.active_subjects:
            self.active_subjects.append(clean)
        self.msg = f"pinned {len(chosen)} item(s) -> *{clean} (.pixos/subjects.json)"
        return True

    def save_selection_as_group(self, group_name: str = "") -> Optional[str]:
        """Snapshot Tab 1's selected items into a transient working group."""
        chosen = self.active_specs()
        if not chosen:
            self.msg = "select at least one item in Tab 1 first"
            return None
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", (group_name or "").strip()).strip("-")
        if not clean:
            idx = len(self.groups) + 1
            while f"grp-{idx}" in self.groups:
                idx += 1
            clean = f"grp-{idx}"
        existing = self.groups.setdefault(clean, [])
        for sp in chosen:
            if sp not in existing:
                existing.append(sp)
        self.active_group = clean
        self.group_cursor = list(self.groups.keys()).index(clean)
        self.msg = f"grouped {len(chosen)} item(s) -> [{clean}] ({len(existing)} total)"
        return clean

    def group_repo_state(self, group_name: str) -> dict:
        """Live repo binding for a group: registry status, visibility, staleness."""
        try:
            from share.share import _load_registry as _lr
        except ImportError:
            try:
                from share import _load_registry as _lr  # type: ignore
            except ImportError:
                from .share import _load_registry as _lr  # type: ignore
        reg = _lr()
        slug = _sanitize_slug(group_name)
        e = reg.get(slug)
        if not e or e.get("kind") not in ("gh", "git", "snippet"):
            return {"bound": False, "slug": slug}
        live = (e.get("status") != "retired") if e.get("kind") == "gh" else True
        reg_files = list(e.get("files") or [])
        grp_files = [s.partition(":")[0] for s in self.groups.get(group_name, [])]
        stale = bool(grp_files and reg_files and set(grp_files) != set(reg_files))
        return {"bound": True, "slug": slug, "kind": e.get("kind"),
                "live": live, "stale": stale,
                "permanent": bool(e.get("permanent")),
                "repo": e.get("repo", ""), "url": e.get("url", "") or e.get("clone_url", "")}

    def push_group(self, group_name: str, zone: Optional[str] = None) -> dict:
        """Re-push a group's current file set to its bound repo slug (push-on-change)."""
        specs = list(self.groups.get(group_name) or [])
        if not specs:
            return {"ok": False, "error": f"group [{group_name}] is empty"}
        target_zone = zone or ZONES[self.zone_idx]
        if target_zone not in ("gh-temp", "git-remote", "export"):
            target_zone = "gh-temp"
        res = zone_action(target_zone, specs, self.base,
                          gh_public=self.gh_public, gh_slug=_sanitize_slug(group_name),
                          gh_account=self.gh_account or None,
                          gh_permanent=self.gh_permanent, group=group_name)
        if res.get("ok"):
            res["group"] = group_name
            self.msg = (f"pushed [{group_name}] -> {res.get('repo') or res.get('slug')} "
                        f"({len(specs)} file(s), {'PERM' if res.get('permanent') else 'TEMP'})")
        return res

    def rename_group(self, old_name: str, new_name: str) -> Optional[str]:
        """Rename a transient working group, preserving member order and cursor."""
        if old_name not in self.groups:
            self.msg = f"group [{old_name}] not found"
            return None
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", (new_name or "").strip()).strip("-")
        if not clean:
            self.msg = "rename: enter a non-empty group name"
            return None
        if clean != old_name and clean in self.groups:
            self.msg = f"group [{clean}] already exists"
            return None
        if clean == old_name:
            return clean
        keys = list(self.groups.keys())
        idx = keys.index(old_name)
        items = [(k, v) for k, v in self.groups.items() if k != old_name]
        items.insert(idx, (clean, self.groups[old_name]))
        self.groups = dict(items)
        if self.active_group == old_name:
            self.active_group = clean
        self.group_cursor = idx
        self.msg = f"renamed [{old_name}] -> [{clean}]"
        return clean

    def load_group_to_stage(self, group_name: str, replace: bool = True) -> int:
        """Populate Tab 1's staged list/selection from a working group."""
        specs = list(self.groups.get(group_name) or [])
        if not specs:
            self.msg = f"group [{group_name}] is empty"
            return 0
        if replace:
            self.items = list(specs)
            self.selected = set(range(len(self.items)))
        else:
            for sp in specs:
                if sp not in self.items:
                    self.items.append(sp)
            self.selected = {i for i, it in enumerate(self.items) if it in set(specs)}
        self.cursor_idx = 0
        self.active_group = group_name
        self.tab_idx = 0
        self.msg = f"loaded [{group_name}] ({len(specs)} item(s)) into Tab 1"
        return len(specs)

    def promote_group_to_subject(self, group_name: str) -> bool:
        """Persist a transient working group into .pixos/subjects.json as *subject."""
        specs = list(self.groups.get(group_name) or [])
        if not specs or get_subject_registry is None:
            self.msg = "group empty or subject registry unavailable"
            return False
        reg = _cached_subject_registry(Path(self.base))
        for sp in specs:
            reg.pin_lines(group_name, sp, description=f"Promoted from TUI group [{group_name}]")
        if group_name not in self.active_subjects:
            self.active_subjects.append(group_name)
        self.msg = f"promoted [{group_name}] -> *{group_name} (.pixos/subjects.json)"
        return True

    def active_specs(self) -> List[str]:
        if self.buf.strip():
            self.ingest_buffer(self.buf)
            self.buf = ""
        if not self.items:
            return []
        if self.selected:
            return [self.items[i] for i in sorted(self.selected) if i < len(self.items)]
        return list(self.items)

    def item_at(self, y: int, x: int):
        for y0, x0, x1, kind, payload in self.hitboxes:
            if y == y0 and x0 <= x <= x1 and kind == "item":
                return int(payload)
        return None

    def complete_matches(self, limit: int = 8) -> List[str]:
        """Filter files under base matching the current input fragment.

        Bounded: fd/find on the fragment's leading directory (or base), never a
        full repo rglob — the old walk froze the TUI for seconds per frame.
        """
        frag = (self.buf or "").strip().strip("'\"")
        if not frag or " " in frag or frag.startswith("*"):
            return []
        if len(frag) > 300 or "\n" in (self.buf or ""):
            return []  # paste burst accumulating per-char: never autocomplete it
        frag_rel, _, _ = frag.partition(":")
        base = Path(self.base) if Path(self.base).is_dir() else Path(os.getcwd())
        needle = frag_rel.lower()
        if len(needle) < 2:
            return []
        import subprocess as _sp
        # narrow the search to the fragment's leading directory when present
        search = base
        if "/" in frag_rel:
            head = frag_rel.split("/")[0]
            cand = base / head
            if cand.is_dir():
                search = cand
        out: List[str] = []
        try:
            fd_bin = shutil.which("fd")
            if fd_bin:
                r = _sp.run([fd_bin, "-i", "--max-results", str(limit * 4), needle, str(search)],
                            capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    for ln in r.stdout.splitlines():
                        if ln.strip():
                            try:
                                out.append(str(Path(ln.strip()).relative_to(base)))
                            except ValueError:
                                out.append(ln.strip())
                            if len(out) >= limit:
                                return out[:limit]
                    if out:
                        return out[:limit]
            fw = _sp.run(["find", str(search), "-maxdepth", "4", "-iname", f"*{needle}*",
                          "-not", "-path", "*/.git/*", "-not", "-path", "*/node_modules/*",
                          "-not", "-path", "*/__pycache__/*"],
                         capture_output=True, text=True, timeout=2)
            if fw.returncode == 0:
                for ln in fw.stdout.splitlines()[: limit * 4]:
                    if ln.strip():
                        try:
                            out.append(str(Path(ln.strip()).relative_to(base)))
                        except ValueError:
                            out.append(ln.strip())
                        if len(out) >= limit:
                            break
        except Exception:
            pass
        return out[:limit]

    def set_item_line_range(self, item_idx: int, line_no: int, extend: bool = False) -> None:
        """Update an item's :start-end line range by clicking lines in the Viewer."""
        if not (0 <= item_idx < len(self.items)):
            return
        rel, cur_s, _ = parse_file_spec(self.items[item_idx])
        if extend and self.line_anchor and self.line_anchor[0] == item_idx:
            a = self.line_anchor[1]
            lo, hi = min(a, line_no), max(a, line_no)
            self.items[item_idx] = f"{rel}:{lo}-{hi}"
            self.msg = f"pinned range {rel}:{lo}-{hi}"
        else:
            self.line_anchor = (item_idx, line_no)
            self.items[item_idx] = f"{rel}:{line_no}-{line_no}"
            self.msg = f"start line {line_no} on {rel} (Shift-click another line for range)"

    def expand_item_range(self, item_idx: int, before: int = 0, after: int = 0) -> None:
        """Expand an item's :start-end bounds by `before` lines up and `after` lines down."""
        if not (0 <= item_idx < len(self.items)):
            return
        rel, s_ln, e_ln = parse_file_spec(self.items[item_idx])
        target_p = (Path(self.base) / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        total = 999999
        if target_p.is_file():
            try:
                total = max(1, len(target_p.read_text(errors="replace").splitlines()))
            except OSError:
                pass
        cur_s = s_ln or 1
        cur_e = e_ln or (total if total != 999999 else cur_s)
        new_s = max(1, cur_s - before)
        new_e = min(total, cur_e + after)
        self.items[item_idx] = f"{rel}:{new_s}-{new_e}"
        self.line_anchor = (item_idx, new_s)
        self.msg = f"expanded range -> {rel}:{new_s}-{new_e}"

    def clear_item_range(self, item_idx: int) -> None:
        """Reset an item's :start-end range back to the whole file."""
        if not (0 <= item_idx < len(self.items)):
            return
        rel, _, _ = parse_file_spec(self.items[item_idx])
        self.items[item_idx] = rel
        self.line_anchor = None
        self.msg = f"reset {rel} to full file"

    def is_hovered(self, kind: str, payload: object = None) -> bool:
        """True if hover_pos sits inside the hitbox matching (kind, payload)."""
        if not self.hover_pos:
            return False
        hy, hx = self.hover_pos
        for y0, x0, x1, hk, hp in self.hitboxes:
            if hy == y0 and x0 <= hx <= x1 and hk == kind and hp == payload:
                return True
        return False

    def handle_click(self, y: int, x: int, shift: bool = False):
        for y0, x0, x1, kind, payload in self.hitboxes:
            if y == y0 and x0 <= x <= x1:
                if kind == "tab":
                    self.tab_idx = int(payload)
                    self.viewer_scroll = 0
                    self.msg = f"tab: {TABS[self.tab_idx]}"
                    if self.tab_idx == 5:
                        self.sync_gh_services()
                    return "tab"
                if kind == "zone":
                    target_z = int(payload)
                    if target_z == self.zone_idx and self.active_specs():
                        return "dispatch"
                    self.zone_idx = target_z
                    self.msg = f"zone: [{ZONES[self.zone_idx]}] — click again or [DISPATCH] to send"
                    return "zone"
                if kind == "item":
                    idx = int(payload)
                    if shift and self.anchor_idx is not None:
                        self.select_range(self.anchor_idx, idx, additive=True)
                        self.msg = f"range selected #{min(self.anchor_idx, idx) + 1}..#{max(self.anchor_idx, idx) + 1}"
                    else:
                        self.toggle_item(idx)
                        self.msg = f"toggled item #{idx + 1} ({len(self.selected)}/{len(self.items)} selected)"
                    self.viewer_scroll = 0
                    return "item"
                if kind == "viewer_line":
                    it_idx, line_no = payload  # type: ignore[misc]
                    self.set_item_line_range(int(it_idx), int(line_no), extend=shift)
                    return "viewer_line"
                if kind == "range_expand":
                    it_idx, d_before, d_after = payload  # type: ignore[misc]
                    self.expand_item_range(int(it_idx), before=int(d_before), after=int(d_after))
                    return "range_expand"
                if kind == "range_full":
                    self.clear_item_range(int(payload))
                    return "range_full"
                if kind == "subject":
                    self.toggle_subject(str(payload))
                    return "subject"
                if kind == "dispatch":
                    return "dispatch"
                if kind == "pin":
                    return "pin"
                if kind == "toggle_gh_vis":
                    self.gh_public = not self.gh_public
                    self.msg = f"gh-temp visibility -> {'PUBLIC (Import dialogs resolve)' if self.gh_public else 'PRIVATE (needs GitHub login)'}"
                    return "toggle_gh_vis"
                if kind == "toggle_gh_perm":
                    self.gh_permanent = not self.gh_permanent
                    self.msg = f"gh lifecycle -> {'PERMANENT (GC-exempt)' if self.gh_permanent else 'TEMP (60m TTL)'}"
                    return "toggle_gh_perm"
                if kind == "cycle_gh_acct":
                    self.cycle_gh_account()
                    return "cycle_gh_acct"
                if kind == "confirm_auth_open":
                    self.open_gh_dropdown()
                    return "confirm_auth_open"
                if kind == "gh_acct_pick":
                    self.select_gh_account(str(payload))
                    return "gh_acct_pick"
                if kind == "make_group":
                    self.save_selection_as_group()
                    return "make_group"
                if kind == "load_group":
                    self.load_group_to_stage(str(payload), replace=True)
                    return "load_group"
                if kind == "merge_group":
                    self.load_group_to_stage(str(payload), replace=False)
                    return "merge_group"
                if kind == "promote_group":
                    self.promote_group_to_subject(str(payload))
                    return "promote_group"
                if kind == "del_group":
                    gname = str(payload)
                    self.groups.pop(gname, None)
                    if self.active_group == gname:
                        self.active_group = ""
                    self.group_cursor = max(0, min(self.group_cursor, len(self.groups) - 1))
                    self.msg = f"removed group [{gname}]"
                    return "del_group"
                if kind == "rename_group":
                    return "rename_group"
                if kind == "push_group":
                    res = self.push_group(str(payload))
                    return "push_group_done" if res.get("ok") else "push_group"
                if kind == "toggle_split":
                    self.split_pane = not self.split_pane
                    self.msg = f"split-pane viewer {'ON' if self.split_pane else 'OFF'}"
                    return "toggle_split"
                if kind == "select_all":
                    self.select_all(len(self.selected) < len(self.items))
                    self.msg = f"selected {len(self.selected)}/{len(self.items)} item(s)"
                    return "select_all"
                if kind == "paste_clip":
                    self.populate_from_clipboard()
                    return "paste_clip"
                if kind == "confirm_pending":
                    self.confirm_pending(int(payload))
                    return "confirm_pending"
                if kind == "complete":
                    self.buf = str(payload)
                    self.msg = f"completed: {payload} — Enter to stage"
                    return "complete"
                if kind == "copy_url":
                    return "copy_url"
                if kind == "copy_clone":
                    return "copy_clone"
                if kind == "stop_active":
                    return "stop_active"
                if kind == "verify_service":
                    slug = str(payload)
                    try:
                        from share.share import _load_registry as _lrv, _save_registry as _srv
                        from share.gh import verify_gh_repo_live
                    except ImportError:
                        from .share import _load_registry as _lrv, _save_registry as _srv  # type: ignore
                        from .gh import verify_gh_repo_live  # type: ignore
                    reg = _lrv()
                    e = reg.get(slug) or {}
                    if e.get("kind") == "gh":
                        probe = verify_gh_repo_live(e.get("repo", ""), e.get("account"))
                        e["status"] = probe.get("state", e.get("status", "unknown"))
                        e["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                        if e["status"] == "retired":
                            e.setdefault("retired_at", e["confirmed_at"])
                        reg[slug] = e
                        _srv(reg)
                        self.msg = f"verified {slug}: {e['status'].upper()}"
                    else:
                        alive = _is_pid_alive(e.get("pid"))
                        self.msg = f"verified {slug}: {'LIVE (pid ' + str(e.get('pid')) + ')' if alive else 'STOPPED'}"
                    return "verified"
                if kind == "retire_service":
                    slug = str(payload)
                    try:
                        from share.share import _load_registry as _lrr, _cleanup_slug as _csr, _save_registry as _srr
                    except ImportError:
                        from .share import _load_registry as _lrr, _cleanup_slug as _csr, _save_registry as _srr  # type: ignore
                    reg = _lrr()
                    e = reg.get(slug)
                    if e:
                        try:
                            _csr(slug, e)
                            reg2 = _lrr()
                            if e.get("kind") != "gh":
                                reg2.pop(slug, None)
                                _srr(reg2)
                            self.msg = f"confirmed retired: {slug}"
                        except Exception as ex:
                            self.msg = f"retire failed ({slug}): {ex}"
                    return "retired"
                if kind == "sync_gh":
                    self.sync_gh_services()
                    return "sync_gh"
                if kind == "toggle_svc_perm":
                    self.toggle_focused_gh_permanence()
                    return "toggle_svc_perm"
                if kind == "purge_retired":
                    try:
                        from share.share import _load_registry as _lrp, _save_registry as _srp
                    except ImportError:
                        from .share import _load_registry as _lrp, _save_registry as _srp  # type: ignore
                    reg = _lrp()
                    kept = {k: v for k, v in reg.items() if v.get("status") != "retired"}
                    _srp(kept)
                    self.msg = f"purged {len(reg) - len(kept)} retired record(s)"
                    return "purged"
                if kind == "sync_gh":
                    self.sync_gh_services()
                    return "sync_gh"
                if kind == "copy_live_url":
                    if yank_to_clipboard(str(payload)):
                        self.msg = f"copied: {payload}"
                        self.flash_confirm(f"[copied to clip] {payload}")
                    return "copied"
                if kind == "svc_row":
                    idx = int(payload)
                    # Keep the cursor visible: pull scroll window along.
                    self.svc_cursor = idx
                    if idx < self.svc_scroll:
                        self.svc_scroll = idx
                    self.msg = f"focused service #{idx + 1}"
                    return "svc_row"
                if kind == "svc_filter":
                    self.svc_filter = str(payload)
                    self.svc_cursor = 0
                    self.svc_scroll = 0
                    self.msg = f"services filter: {self.svc_filter.upper()}"
                    return "svc_filter"
                if kind == "svc_kind":
                    order = ("all", "gh", "gh-perm", "git", "snippet")
                    nxt = order[(order.index(self.svc_kind_filter) + 1) % len(order)] if self.svc_kind_filter in order else "all"
                    self.svc_kind_filter = nxt
                    self.svc_cursor = 0
                    self.svc_scroll = 0
                    self.msg = f"service kind filter: {nxt.upper()}"
                    return "svc_kind"
                if kind == "svc_toggle_expand":
                    self.svc_expanded = not self.svc_expanded
                    self.msg = f"inspector {'expanded' if self.svc_expanded else 'compact'}"
                    return "svc_toggle_expand"
                if kind == "kill_live":
                    try:
                        from share.share import _load_registry as _lrk, _cleanup_slug as _csk, _save_registry as _srk
                    except ImportError:
                        try:
                            from share import _load_registry as _lrk, _cleanup_slug as _csk, _save_registry as _srk
                        except ImportError:
                            from .share import _load_registry as _lrk, _cleanup_slug as _csk, _save_registry as _srk  # type: ignore
                    reg = _lrk()
                    e = reg.get(str(payload) or "")
                    if e:
                        _csk(str(payload), e)
                        reg2 = _lrk()
                        reg2.pop(str(payload), None)
                        _srk(reg2)
                        self.msg = f"killed {payload}"
                    return "killed"
        return None

    def flash_confirm(self, text: str) -> None:
        """Set a transient bottom-bar confirm dialogue, shown until next input."""
        self.confirm_flash = text

    def _filtered_services(self) -> List[Tuple[str, dict, str]]:
        """Return [(slug, entry, status)] sorted live-first and filtered by active pills."""
        try:
            from share.share import _load_registry as _lr
        except ImportError:
            try:
                from share import _load_registry as _lr  # type: ignore
            except ImportError:
                from .share import _load_registry as _lr  # type: ignore
        reg = _lr()
        live_svcs, retired_svcs = [], []
        for slug, e in reg.items():
            kind = e.get("kind", "?")
            if self.svc_kind_filter == "gh-perm":
                if kind != "gh" or not bool(e.get("permanent")):
                    continue
            elif self.svc_kind_filter != "all" and kind != self.svc_kind_filter:
                continue
            if kind == "gh":
                # Only confirmed-remote-gone counts as retired; failed deletes
                # (retire_error / retired_local legacy) stay live + actionable.
                st = "retired" if e.get("status") == "retired" else "live"
            else:
                st = "live" if _is_pid_alive(e.get("pid")) else "retired"
            if self.svc_filter == "live" and st != "live":
                continue
            if self.svc_filter == "retired" and st != "retired":
                continue
            (live_svcs if st == "live" else retired_svcs).append((slug, e, st))
        return live_svcs + retired_svcs

    def _svc_entry_at_cursor(self) -> tuple[str, dict] | tuple[None, None]:
        """Return (slug, entry) for the focused Tab 5 row, or (None, None)."""
        items = self._filtered_services()
        if not items:
            return None, None
        idx = max(0, min(self.svc_cursor, len(items) - 1))
        slug, entry, _ = items[idx]
        return slug, entry

    def toggle_focused_gh_permanence(self) -> None:
        slug, e = self._svc_entry_at_cursor()
        if not slug or not e or e.get("kind") != "gh":
            self.msg = "focus a GitHub service row first"
            return
        new_perm = not bool(e.get("permanent"))
        res = set_gh_repo_permanence(slug, permanent=new_perm, ttl_min=60)
        self.msg = f"{slug} -> {'PERMANENT (GC-exempt)' if new_perm else 'TEMP (60m TTL)'}" if res.get("ok") else res.get("error", "failed")

    def sync_gh_services(self) -> None:
        try:
            from share.gh import sync_gh_repos_to_registry as _sync
        except ImportError:
            try:
                from gh import sync_gh_repos_to_registry as _sync  # type: ignore
            except ImportError:
                from .gh import sync_gh_repos_to_registry as _sync  # type: ignore
        try:
            res = _sync(self.gh_account or None)
        except Exception as e:
            self.msg = f"gh sync error: {e}"
            return
        if res.get("ok"):
            self.msg = (f"synced @{res['account']}: +{res['added']} added, "
                        f"{res['updated']} updated ({res['total_remote']} remote)")
        else:
            self.msg = f"gh sync error: {res.get('error', 'unknown')}"

    def verify_focused_service(self) -> str | None:
        """Re-probe the focused Tab 5 service (Enter key); returns action tag."""
        slug, e = self._svc_entry_at_cursor()
        if not slug or not e:
            return "verified_missing"
        if e.get("kind") == "gh":
            try:
                from share.gh import verify_gh_repo_live
            except ImportError:
                try:
                    from gh import verify_gh_repo_live  # type: ignore
                except ImportError:
                    from .gh import verify_gh_repo_live  # type: ignore
            probe = verify_gh_repo_live(e.get("repo", ""), e.get("account"))
            try:
                from share.share import _load_registry as _lr2, _save_registry as _sr2
            except ImportError:
                try:
                    from share import _load_registry as _lr2, _save_registry as _sr2  # type: ignore
                except ImportError:
                    from .share import _load_registry as _lr2, _save_registry as _sr2  # type: ignore
            reg = _lr2()
            if slug in reg:
                reg[slug]["status"] = probe.get("state", e.get("status", "unknown"))
                reg[slug]["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                if reg[slug]["status"] == "retired":
                    reg[slug].setdefault("retired_at", reg[slug]["confirmed_at"])
                _sr2(reg)
            self.msg = f"verified {slug}: {reg.get(slug, {}).get('status', '?').upper()}"
            return "verified"
        alive = _is_pid_alive(e.get("pid"))
        self.msg = f"verified {slug}: {'LIVE (pid ' + str(e.get('pid')) + ')' if alive else 'STOPPED'}"
        return "verified"

    def on_mouse_event(self, y: int, x: int, bstate: int, curses_mod=None) -> Optional[str]:
        """Unified mouse handler normalizing PRESS/RELEASE/CLICK/DOUBLE/RIGHT."""
        b1_press = getattr(curses_mod, "BUTTON1_PRESSED", 0x2)
        b1_rel = getattr(curses_mod, "BUTTON1_RELEASED", 0x1)
        b1_click = getattr(curses_mod, "BUTTON1_CLICKED", 0x4)
        b1_dbl = getattr(curses_mod, "BUTTON1_DOUBLE_CLICKED", 0x8)
        b3_press = getattr(curses_mod, "BUTTON3_PRESSED", 0x800)
        b3_click = getattr(curses_mod, "BUTTON3_CLICKED", 0x1000)
        b_shift = getattr(curses_mod, "BUTTON_SHIFT", 0x2000000)
        b4_wheel = getattr(curses_mod, "BUTTON4_PRESSED", 0x10000)
        b5_wheel = getattr(curses_mod, "BUTTON5_PRESSED", 0x200000)
        self.hover_pos = (y, x)
        if bstate & b4_wheel:
            self.viewer_scroll = max(0, self.viewer_scroll - 3)
            if self.tab_idx == 5:
                self.svc_cursor = max(0, self.svc_cursor - 1)
                self.svc_scroll = min(self.svc_scroll, self.svc_cursor)
            return "scroll"
        if bstate & b5_wheel:
            self.viewer_scroll += 3
            if self.tab_idx == 5:
                self.svc_cursor += 1
                self.svc_scroll = min(self.svc_scroll + 1, self.svc_cursor)
            return "scroll"
        shift = bool(bstate & b_shift)
        if bstate & (b3_press | b3_click):
            hit_idx = self.item_at(y, x)
            if hit_idx is not None:
                self.selected = {hit_idx}
                self.cursor_idx = hit_idx
                self.zone_idx = list(ZONES).index("drag-out")
                return "dispatch"
            return None
        if bstate & b1_dbl:
            self.drag_start_idx = None
            hit_idx = self.item_at(y, x)
            if hit_idx is not None:
                self.selected = {hit_idx}
                self.cursor_idx = hit_idx
                return "dispatch"
            return self.handle_click(y, x, shift=shift)
        if bstate & b1_press:
            hit_idx = self.item_at(y, x)
            if hit_idx is not None:
                self.drag_start_idx = hit_idx
                return None
            self.drag_start_idx = None
            return self.handle_click(y, x, shift=shift)
        if bstate & b1_rel:
            end_idx = self.item_at(y, x)
            start_idx = self.drag_start_idx
            self.drag_start_idx = None
            if start_idx is not None and end_idx is not None and end_idx != start_idx:
                self.select_range(start_idx, end_idx, additive=False)
                self.msg = f"drag-selected #{min(start_idx, end_idx) + 1}..#{max(start_idx, end_idx) + 1}"
                return "range"
            return self.handle_click(y, x, shift=shift)
        if bstate & b1_click:
            self.drag_start_idx = None
            return self.handle_click(y, x, shift=shift)
        return "hover"


def _stop_active_share(state) -> None:
    """Tear down the detached server for the active slug."""
    try:
        from share.share import _load_registry as _lr, _cleanup_slug as _cs, _save_registry as _sr
    except ImportError:
        try:
            from share import _load_registry as _lr, _cleanup_slug as _cs, _save_registry as _sr
        except ImportError:
            from .share import _load_registry as _lr, _cleanup_slug as _cs, _save_registry as _sr  # type: ignore
    reg = _lr()
    slug = getattr(state, "last_slug", "")
    e = reg.get(slug or "")
    if not e:
        state.msg = "nothing live to stop"
        return
    _cs(slug, e)
    reg2 = _lr()
    reg2.pop(slug, None)
    _sr(reg2)
    state.msg = f"stopped {slug}"
    state.last_remote_url = ""
    state.last_clone_cmd = ""
    state.last_slug = ""


def _read_lines_cached(state: ShareTUIState, target_p: Path) -> List[str]:
    """Read file lines with mtime cache so render frames never thrash disk I/O."""
    key = str(target_p)
    try:
        mtime = target_p.stat().st_mtime_ns
    except OSError:
        return []
    if state._file_cache[0] == key and state._file_cache[1] == mtime:
        return state._file_cache[2]
    lines = target_p.read_text(errors="replace").splitlines()
    state._file_cache = (key, mtime, lines)
    return lines


def run_share_tui(stdscr=None, test_mode: bool = False,
                  drops: Optional[List[str]] = None,
                  zone: str = "share") -> int:
    """Headless-testable entry: with test_mode, just route drops and print JSON."""
    if test_mode or stdscr is None:
        if zone == "poll-clip":
            # clipboard IS the payload: resolve it, then snippet-share the result
            try:
                text, src = poll_clipboard()
            except Exception:
                text, src = "", "none"
            specs = parse_drops(text)
            if not specs:
                result = {"ok": False, "error": f"clipboard ({src}) has no file paths"}
                print(json.dumps(result, indent=2))
                return 2
            args = SimpleNamespace(files=specs, base=os.getcwd(),
                                   via="tailscale", ttl=60, serve=False, json=True)
            try:
                rc = do_share(args)
            except SystemExit as e:
                rc = e.code or 0
            result = {"ok": rc == 0, "zone": zone, "specs": specs,
                      "clipboard_src": src}
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 2
        result = zone_action(zone, list(drops or []), None)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    import curses
    curses.curs_set(1)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        curses.mouseinterval(0)  # zero-lag click dispatch (no 200ms double-click wait)
        curses.putp("\033[?2004h")
        curses.putp("\033[?1000h\033[?1003h\033[?1006h")  # SGR mouse click + drag + hover + wheel
        sys.stdout.flush()
    except Exception:
        pass

    state = ShareTUIState(zone=zone)
    try:
        state.refresh_gh_identity(cycle_account=False)
    except Exception:
        pass
    if drops:
        state.ingest_buffer(" ".join(drops))
    pin_prompt_mode = False
    pin_name_buf = ""
    rename_target = ""
    from collections import deque as _dq
    _pending_keys = _dq()

    ts_ok = check_transport("tailscale").get("available", False)
    cf_ok = check_transport("cloudflared").get("available", False)
    transport_bar = (f"transports: tailscale[{'OK' if ts_ok else 'MISSING'}] "
                     f"cloudflared[{'OK' if cf_ok else 'MISSING'}]")

    import time as _time
    _last_ingest_ts = 0.0

    def _drain_paste_burst(initial: str = "", bracketed: bool = False) -> str:
        """Greedily drain queued PTY paste bytes without re-rendering per character."""
        chunks = [initial] if initial else []
        stdscr.nodelay(True)
        try:
            idle_polls = 0
            while True:
                try:
                    nxt = stdscr.get_wch()
                except Exception:
                    nxt = -1
                if nxt == -1 or nxt is None:
                    # In bracketed mode, kitty may split >4KB writes across PTY reads
                    if bracketed and idle_polls < 25:
                        idle_polls += 1
                        _time.sleep(0.002)
                        continue
                    break
                idle_polls = 0
                if nxt == "\x1b":
                    # Check for \x1b[201~ close marker (tolerate PTY chunk split after \x1b)
                    esc_seq = ""
                    for _ in range(8):
                        c2 = -1
                        for _retry in range(5):
                            try:
                                c2 = stdscr.get_wch()
                                break
                            except Exception:
                                _time.sleep(0.001)
                        if not isinstance(c2, str) or len(c2) != 1:
                            break
                        esc_seq += c2
                        if c2 in ("~", "M", "m"):
                            break
                    if esc_seq == "[201~":
                        break
                    continue
                if nxt in (curses.KEY_ENTER, 10, 13, "\n", "\r"):
                    chunks.append("\n")
                elif nxt in (9, "\t"):
                    chunks.append("\t")
                elif isinstance(nxt, str) and nxt.isprintable():
                    chunks.append(nxt)
        finally:
            try:
                stdscr.nodelay(False)
            except Exception:
                pass
        return "".join(chunks)

    def _finalize_pasted_blob(raw_blob: str) -> None:
        nonlocal _last_ingest_ts
        cleaned = strip_paste_markers(raw_blob)
        state.buf = ""
        if not cleaned:
            return
        # Fast-path large multi-line / diff blobs directly through stage_with_confirm
        staged, unsure = state.stage_with_confirm(cleaned)
        state.tab_idx = 0
        _last_ingest_ts = _time.monotonic()
        _reassert_terminal_modes()
        if state.auto_drop and not state.pending:
            dispatch()
        elif unsure:
            state.msg = f"staged {staged} + {unsure} (?) to confirm (y) — Enter dispatch"
        elif staged:
            state.msg = f"staged {len(state.items)} item(s) — click to toggle, [p]in subject, Enter to dispatch"
        elif state.items:
            state.msg = f"paste had no file paths — {len(state.items)} staged item(s) kept — Enter to dispatch"
        else:
            state.msg = "paste contained no valid file paths"

    def handle_enter() -> None:
        nonlocal _last_ingest_ts
        raw = state.buf.strip()
        if raw:
            is_uri_drop = raw.startswith("file://") or PASTE_OPEN in raw or PASTE_CLOSE in raw or "/" in raw
            cleaned = strip_paste_markers(raw)
            if is_uri_drop:
                added = state.ingest_buffer(cleaned)
                state.buf = ""
                state.tab_idx = 0
                _last_ingest_ts = _time.monotonic()
                _reassert_terminal_modes()
                if added:
                    if state.auto_drop:
                        dispatch()
                    else:
                        state.msg = f"staged {len(state.items)} item(s) — Enter to dispatch"
                return
            if state.tab_idx == 0:
                matches = state.complete_matches(1)
                if matches and cleaned.strip("'\"") == matches[0]:
                    state.ingest_buffer(cleaned)
                    state.buf = ""
                    state.msg = f"staged {len(state.items)} item(s) — Enter to dispatch"
                elif matches:
                    state.buf = matches[0]
                    state.msg = f"completed: {matches[0]} — Enter to stage"
                else:
                    state.ingest_buffer(cleaned)
                    state.buf = ""
                    state.msg = f"staged {len(state.items)} item(s) — Enter to dispatch"
                return
        # Ignore trailing newline emitted by terminal DND right after PASTE_CLOSE
        if (_time.monotonic() - _last_ingest_ts) < 0.15:
            return
        if state.tab_idx == 1:
            gnames = list(state.groups.keys())
            if gnames and 0 <= state.group_cursor < len(gnames):
                state.load_group_to_stage(gnames[state.group_cursor], replace=True)
        elif state.tab_idx == 2:
            reg_sub = _cached_subject_registry(Path(state.base))
            bms = reg_sub.list_all() if reg_sub else []
            if bms and 0 <= state.cursor_idx < len(bms):
                state.toggle_subject(bms[state.cursor_idx].name)
        elif state.tab_idx == 5:
            act = state.verify_focused_service()
            if act == "verified_missing":
                state.msg = "service row needs a registry entry — list may be stale"
        else:
            dispatch()

    def dispatch() -> None:
        nonlocal pin_prompt_mode, pin_name_buf, rename_target
        if ZONES[state.zone_idx] == "poll-clip" and not state.active_specs():
            text, src = poll_clipboard()
            if text.strip():
                state.ingest_buffer(text)
            else:
                state.msg = f"clipboard ({src}) is empty — copy file paths first"
                return
        specs = state.active_specs()
        if state.pending:
            state.msg = f"{len(state.pending)} (?) unsure path(s) — press y to confirm or x to drop"
            return
        if not specs:
            state.msg = "nothing selected: drop files or click items first"
            return
        if ZONES[state.zone_idx] == "export" and not state.active_subjects:
            pin_prompt_mode = True
            pin_name_buf = ""
            state.msg = "export scope to *subject: enter subject name (Enter to save & export)"
            return
        if ZONES[state.zone_idx] == "gh-temp" and not state.gh_slug_defined:
            pin_prompt_mode = True
            pin_name_buf = state.gh_slug or "pix-scratch"
            state.msg = "gh-temp repo name (slug): Enter to create & push, Esc to cancel"
            return
        if ZONES[state.zone_idx] in ("gh-temp", "git-remote", "export") and not state.auth_confirmed:
            if not state.gh_identity:
                state.refresh_gh_identity(cycle_account=False)
            state.auth_confirm_modal = True
            ident = state.gh_identity
            if ident.get("ok"):
                vis = "PUBLIC" if state.gh_public else "PRIVATE"
                del_tag = "ok" if ident.get("can_delete") else "MISSING delete_repo"
                state.msg = (f"CONFIRM AUTH -> @{ident['account']} | {ident['author']} | "
                             f"{vis} | delete_repo:{del_tag}  [Enter/y: push, u: acct, o: vis, Esc: cancel]")
            else:
                state.msg = f"AUTH ERROR: {ident.get('error', 'no token')} — [u] switch acct, [Esc] cancel"
            return
        dispatch_specs = list(specs)
        if ZONES[state.zone_idx] == "export" and state.active_subjects:
            dispatch_specs = [f"*{state.active_subjects[0]}"] + [s for s in specs if not s.startswith("*")]
        bound_group = state.active_group if state.active_group in state.groups else None
        zone_slug = _sanitize_slug(bound_group) if bound_group else state.gh_slug
        try:
            res = zone_action(ZONES[state.zone_idx], dispatch_specs, state.base,
                                gh_public=state.gh_public, gh_slug=zone_slug,
                                gh_account=state.gh_account or None,
                                gh_permanent=state.gh_permanent,
                                group=bound_group)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        state.history.append({"zone": ZONES[state.zone_idx], "specs": list(specs),
                              "subjects": list(state.active_subjects),
                              "ok": bool(res.get("ok")),
                              "cmd": res.get("suggested_cmd", "")})
        if res.get("ok"):
            if res.get("kitty_dnd_seq"):
                try:
                    curses.putp(res["kitty_dnd_seq"])
                except Exception:
                    pass
            remote_url = res.get("remote_url") or res.get("clone_url") or res.get("url")
            yank_target = remote_url or res.get("suggested_cmd") or f"share {' '.join(specs)}"
            state.last_yank = yank_target
            state.last_remote_url = remote_url or ""
            state.last_clone_cmd = res.get("suggested_cmd", "") or ""
            state.last_slug = res.get("slug", "")
            yanked = yank_to_clipboard(yank_target)
            yank_tag = " [COPIED]" if yanked else ""
            if res.get("reused"):
                state.msg = f"Already live at {yank_target}{yank_tag} — [y] copy URL, [c] clone, [x] stop"
            else:
                state.msg = f"{yank_target}{yank_tag} — [y]ank URL, [c]lone, [x] stop, [b]ack"
            state.selected.clear()
            state.tab_idx = 0
            detail = {k: v for k, v in res.items() if k not in ("suggested_cmd", "kitty_dnd_seq")}
            state.result_lines = json.dumps(detail, indent=2).splitlines()
        else:
            state.msg = f"ERROR: {res.get('error', 'dispatch failed')} — kept in [b]ack history"
            state.result_lines = []

    def h_limit():
        try:
            hh, ww = stdscr.getmaxyx()
        except Exception:
            hh, ww = 24, 80
        return hh, ww

    while True:
        stdscr.clear()
        h, w = h_limit()
        prev_hitboxes = list(state.hitboxes)
        state.hitboxes.clear()
        try:
            # Tab bar wraps across rows (same as zone pills) so narrow
            # terminals keep every tab clickable instead of truncating.
            ty, tx = 0, 0
            for ti, tname in enumerate(TABS):
                tlabel = f" {ti + 1}:{tname} "
                if tx + len(tlabel) >= w:
                    ty += 1
                    tx = 0
                if ty == 0 and tx + len(tlabel) >= w:
                    tlabel = f" {ti + 1} "
                if ty > 1 or (ty == 1 and tx + len(tlabel) >= w):
                    break  # extremely narrow: hide lowest-priority tabs
                hovered = any(hy == state.hover_pos[0] and hx0 <= state.hover_pos[1] <= hx1 and hk == "tab" and hp == ti
                              for hy, hx0, hx1, hk, hp in prev_hitboxes) if state.hover_pos else False
                attr = (curses.A_REVERSE | curses.A_BOLD) if ti == state.tab_idx else (curses.A_UNDERLINE if hovered else curses.A_DIM)
                try:
                    stdscr.addstr(ty, tx, tlabel[:w - tx - 1] if tx < w - 1 else "", attr)
                except Exception:
                    pass
                state.hitboxes.append((ty, tx, tx + len(tlabel) - 1, "tab", ti))
                tx += len(tlabel) + 1

            tab_rows_used = ty + 1
            subj_tag = f" | subjects: *{', *'.join(state.active_subjects)}" if state.active_subjects else ""
            mode_tag = " [AUTO-DROP]" if state.auto_drop else ""
            ident = state.gh_identity
            if ident.get("ok"):
                lock_mark = "✓" if state.auth_confirmed else "?"
                auth_pill = f"[gh:@{ident['account']} {lock_mark}]"
                author_short = f" {ident.get('author_email', '')}" if ident.get("author_email") else ""
            else:
                auth_pill = "[gh:NO-AUTH]"
                author_short = ""
            if tab_rows_used < h - 1:
                base_bar = transport_bar + subj_tag + mode_tag + author_short
                budget = max(10, w - len(auth_pill) - 2)
                stdscr.addstr(tab_rows_used, 0, base_bar[:budget])
                ax = min(w - len(auth_pill) - 1, len(base_bar[:budget]) + 1)
                if ax + len(auth_pill) < w and ax > 0:
                    stdscr.addstr(tab_rows_used, ax, auth_pill,
                                  curses.A_REVERSE if not state.auth_confirmed else curses.A_BOLD)
                    state.hitboxes.append((tab_rows_used, ax, ax + len(auth_pill) - 1, "confirm_auth_open", None))
                    state.gh_acct_anchor = (tab_rows_used, ax)

            # Zone pills only apply to routing tabs; hide on Services & Hosts (reclaims 2 rows)
            if state.tab_idx in (0, 1, 2):
                zy, zx = tab_rows_used + 1, 0
                for i, z in enumerate(ZONES):
                    label = f"[ {z} ]"
                    if zx + len(label) + 1 > w:
                        zy += 1
                        zx = 0
                    if zy < h - 1:
                        hovered = any(hy == state.hover_pos[0] and hx0 <= state.hover_pos[1] <= hx1 and hk == "zone" and hp == i
                                      for hy, hx0, hx1, hk, hp in prev_hitboxes) if state.hover_pos else False
                        attr = curses.A_REVERSE if i == state.zone_idx else (curses.A_UNDERLINE | curses.A_BOLD if hovered else curses.A_NORMAL)
                        stdscr.addstr(zy, zx, label[:w - zx - 1], attr)
                        state.hitboxes.append((zy, zx, zx + len(label) - 1, "zone", i))
                    zx += len(label) + 2
                py = zy + 2
            else:
                py = tab_rows_used + 2
            if state.tab_idx == 0:
                use_split = state.split_pane and w >= 100
                left_w = (w // 2 - 1) if use_split else (w - 1)
                right_x = (w // 2 + 1) if use_split else 0
                right_w = (w - right_x - 1) if use_split else 0
                if py < h - 1:
                    hdr = f"staged ({len(state.selected)}/{len(state.items)} selected) | input: {state.buf[-(max(10, w - 45)):]}"
                    stdscr.addstr(py, 0, hdr[:w - 1])
                row = py + 1
                if state.tab_idx == 0 and state.buf.strip():
                    matches = state.complete_matches(6)
                    for mp in matches:
                        if row >= h - 2:
                            break
                        mline = f"  ? {mp}"
                        stdscr.addstr(row, 0, mline[:left_w], curses.A_DIM)
                        state.hitboxes.append((row, 0, min(left_w, len(mline)), "complete", mp))
                        row += 1
                if state.pending and row < h - 2:
                    for pi, pend in enumerate(state.pending[:3]):
                        c0 = (pend.get("candidates") or ["no match"])[min(pend.get("sel", 0), max(0, len(pend.get("candidates") or ["x"]) - 1))]
                        pline = f"  (?) {pend['query']} -> {c0}  [y-confirm {pi}]"
                        stdscr.addstr(row, 0, pline[:w - 1], curses.A_REVERSE)
                        state.hitboxes.append((row, 0, min(w - 1, len(pline)), "confirm_pending", pi))
                        row += 1
                if row < h - 2:
                    btns = [
                        ("[Toggle All]", "select_all", curses.A_BOLD),
                        ("[Paste Clip]", "paste_clip", curses.A_BOLD),
                        ("[+ Group]", "make_group", curses.A_BOLD),
                        ("[Pin -> *subject]", "pin", curses.A_BOLD),
                        ("[GH:Public]" if state.gh_public else "[GH:Private]", "toggle_gh_vis", curses.A_DIM),
                        ("[GH:Perm]" if state.gh_permanent else "[GH:Temp]", "toggle_gh_perm", curses.A_REVERSE if state.gh_permanent else curses.A_DIM),
                        ("[Split:ON]" if state.split_pane else "[Split:OFF]", "toggle_split", curses.A_DIM),
                        ("[DISPATCH]", "dispatch", curses.A_REVERSE | curses.A_BOLD),
                    ]
                    bx = 0
                    for blabel, bkind, battr in btns:
                        if bx + len(blabel) >= w:
                            break
                        stdscr.addstr(row, bx, blabel, battr)
                        state.hitboxes.append((row, bx, bx + len(blabel) - 1, bkind, None))
                        bx += len(blabel) + 2
                    row += 1
                list_start_row = row
                for idx_it, sp in enumerate(state.items):
                    if row >= (h - 2 if use_split else h - 3):
                        break
                    chk = "[x]" if idx_it in state.selected else "[ ]"
                    ptr = ">" if idx_it == state.cursor_idx else " "
                    line_txt = f"{ptr} {chk} {sp}"
                    hovered = any(hy == state.hover_pos[0] and hx0 <= state.hover_pos[1] <= hx1 and hk == "item" and hp == idx_it
                                  for hy, hx0, hx1, hk, hp in prev_hitboxes) if state.hover_pos else False
                    attr = curses.A_BOLD if idx_it == state.cursor_idx else (curses.A_UNDERLINE if hovered else curses.A_NORMAL)
                    stdscr.addstr(row, 0, line_txt[:left_w], attr)
                    state.hitboxes.append((row, 0, min(left_w, len(line_txt)), "item", idx_it))
                    row += 1
                if use_split:
                    for r_div in range(list_start_row, h - 1):
                        stdscr.addstr(r_div, right_x - 1, "│", curses.A_DIM)
                    v_row = list_start_row
                    if state.items:
                        focused_spec = state.items[min(state.cursor_idx, len(state.items) - 1)]
                        rel, s_ln, e_ln = parse_file_spec(focused_spec)
                        target_p = (Path(state.base) / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
                        ctrl_m10, ctrl_p10, ctrl_full = "[-10]", "[+10]", "[Full]"
                        ctrls_str = f"{ctrl_m10} {ctrl_p10} {ctrl_full}"
                        title_budget = max(8, right_w - len(ctrls_str) - 1)
                        stdscr.addstr(v_row, right_x, f"Peek: {focused_spec}"[:title_budget], curses.A_BOLD)
                        cx = right_x + min(right_w - len(ctrls_str), len(f"Peek: {focused_spec}"[:title_budget]) + 1)
                        if cx + len(ctrls_str) <= right_x + right_w:
                            stdscr.addstr(v_row, cx, ctrl_m10, curses.A_REVERSE)
                            state.hitboxes.append((v_row, cx, cx + len(ctrl_m10) - 1, "range_expand", (state.cursor_idx, 10, 0)))
                            cx += len(ctrl_m10) + 1
                            stdscr.addstr(v_row, cx, ctrl_p10, curses.A_REVERSE)
                            state.hitboxes.append((v_row, cx, cx + len(ctrl_p10) - 1, "range_expand", (state.cursor_idx, 0, 10)))
                            cx += len(ctrl_p10) + 1
                            stdscr.addstr(v_row, cx, ctrl_full, curses.A_DIM)
                            state.hitboxes.append((v_row, cx, cx + len(ctrl_full) - 1, "range_full", state.cursor_idx))
                        v_row += 1
                        if target_p.is_file():
                            all_lines = _read_lines_cached(state, target_p)
                            start_view = max(0, (s_ln - 1 if s_ln else 0) + state.viewer_scroll)
                            for lno in range(start_view, len(all_lines)):
                                if v_row >= h - 1:
                                    break
                                one_based = lno + 1
                                in_span = (s_ln is not None and s_ln <= one_based <= (e_ln or s_ln))
                                prefix = ">>" if in_span else "  "
                                attr = curses.A_BOLD if in_span else curses.A_DIM
                                line_str = f"{prefix}{one_based:>4}|{all_lines[lno]}"[:right_w]
                                stdscr.addstr(v_row, right_x, line_str, attr)
                                state.hitboxes.append((v_row, right_x, right_x + len(line_str) - 1, "viewer_line", (state.cursor_idx, one_based)))
                                v_row += 1
                        else:
                            stdscr.addstr(v_row, right_x, f"[MISSING] {target_p}"[:right_w])
                    elif state.result_lines:
                        for ln in state.result_lines:
                            if v_row >= h - 1:
                                break
                            stdscr.addstr(v_row, right_x, ln[:right_w])
                            v_row += 1
                elif state.items and row < h - 1:
                    focused_spec = state.items[min(state.cursor_idx, len(state.items) - 1)]
                    peek = preview_payload(focused_spec, base=state.base)
                    for ln in peek:
                        if row >= h - 1:
                            break
                        stdscr.addstr(row, 0, ln[:w - 1], curses.A_DIM)
                        row += 1
                elif state.result_lines:
                    for ln in state.result_lines:
                        if row >= h - 1:
                            break
                        stdscr.addstr(row, 0, ln[:w - 1])
                        row += 1
            elif state.tab_idx == 1:
                row = py
                gnames = list(state.groups.keys())
                if row < h - 1:
                    hdr_g = f"Working File Groups ({len(gnames)}) — [m] capture, [Enter] load, [r] rename, [u] push, [s] promote -> *subject:"
                    stdscr.addstr(row, 0, hdr_g[:w - 1], curses.A_BOLD)
                    row += 1
                if not gnames:
                    if row < h - 1:
                        stdscr.addstr(row, 0, "  No groups yet. Select files in 1:Drop & Route and click [+ Group] or press [m]."[:w - 1], curses.A_DIM)
                else:
                    for gi, gname in enumerate(gnames):
                        if row >= h - 4:
                            break
                        gitems = state.groups[gname]
                        ptr = ">" if gi == state.group_cursor else " "
                        act_tag = " [ACTIVE]" if gname == state.active_group else ""
                        try:
                            _gst = state.group_repo_state(gname)
                        except Exception:
                            _gst = {"bound": False}
                        if _gst.get("bound"):
                            _dot = "●" if _gst.get("live") else "○"
                            _pm = "/PERM" if _gst.get("permanent") else ""
                            _stale = " STALE" if _gst.get("stale") else ""
                            repo_tag = f" {_dot}[{_gst.get('kind')}{_pm}]{_stale}"
                        else:
                            repo_tag = " ○[unpushed]"
                        btns_w = 47
                        left_b = max(12, w - btns_w - 1)
                        gline = f"{ptr} [{gname}]{act_tag} ({len(gitems)} file(s)){repo_tag}"[:left_b]
                        stdscr.addstr(row, 0, gline, curses.A_REVERSE if gi == state.group_cursor else curses.A_BOLD)
                        cx = w - btns_w
                        if cx > len(gline) and cx + btns_w <= w - 1:
                            stdscr.addstr(row, cx, "[Load]", curses.A_REVERSE)
                            state.hitboxes.append((row, cx, cx + 5, "load_group", gname))
                            cx += 7
                            stdscr.addstr(row, cx, "[Push]", curses.A_REVERSE | curses.A_BOLD)
                            state.hitboxes.append((row, cx, cx + 5, "push_group", gname))
                            cx += 7
                            stdscr.addstr(row, cx, "[+Merge]", curses.A_NORMAL)
                            state.hitboxes.append((row, cx, cx + 7, "merge_group", gname))
                            cx += 9
                            stdscr.addstr(row, cx, "[->*Subj]", curses.A_BOLD)
                            state.hitboxes.append((row, cx, cx + 8, "promote_group", gname))
                            cx += 10
                            stdscr.addstr(row, cx, "[Ren]", curses.A_NORMAL)
                            state.hitboxes.append((row, cx, cx + 4, "rename_group", gname))
                            cx += 6
                            stdscr.addstr(row, cx, "[Del]", curses.A_DIM)
                            state.hitboxes.append((row, cx, cx + 4, "del_group", gname))
                        row += 1
                    sel_g = gnames[min(state.group_cursor, len(gnames) - 1)]
                    if row < h - 1:
                        stdscr.addstr(row, 0, f"  Members of [{sel_g}]:"[:w - 1], curses.A_DIM)
                        row += 1
                    for m_spec in state.groups[sel_g]:
                        if row >= h - 1:
                            break
                        stdscr.addstr(row, 0, f"    - {m_spec}"[:w - 1])
                        row += 1
            elif state.tab_idx == 2:
                if py < h - 1:
                    stdscr.addstr(py, 0, "Subject Bookmarks (click or Enter to toggle grouping):"[:w - 1], curses.A_BOLD)
                reg_sub = _cached_subject_registry(Path(state.base))
                try:
                    all_bms = reg_sub.list_all() if reg_sub else []
                except Exception as e:
                    all_bms = []
                    if py + 1 < h - 1:
                        stdscr.addstr(py + 1, 0, f"  [ERR] subject registry failed: {e}"[:w - 1], curses.A_DIM)
                row = py + 1
                for b_idx, bm in enumerate(all_bms):
                    if row >= h - 1:
                        break
                    active = bm.name in state.active_subjects
                    mark = "[*]" if active else "[ ]"
                    ptr = ">" if b_idx == state.cursor_idx else " "
                    sec_cnt = sum(len(v) for v in getattr(bm, "sections", {}).values())
                    ranges_cnt = sum(len(v) for v in getattr(bm, "line_ranges", {}).values())
                    desc = f"{ptr} {mark} *{bm.name:<12} ({len(bm.paths)} files, {sec_cnt} sections, {ranges_cnt} pins)"
                    attr = curses.A_REVERSE if active else (curses.A_BOLD if b_idx == state.cursor_idx else curses.A_NORMAL)
                    stdscr.addstr(row, 0, desc[:w - 1], attr)
                    state.hitboxes.append((row, 0, min(w - 1, len(desc)), "subject", bm.name))
                    row += 1
                # Lightweight scope preview for the focused subject (no full-tree pragma walk or blocking git on tab switch)
                if reg_sub and all_bms and row < h - 1:
                    focus_bm = all_bms[min(state.cursor_idx, len(all_bms) - 1)]
                    detail = [f"-- *{focus_bm.name}: {focus_bm.description or 'scoped group'}"]
                    for fpath in focus_bm.paths[:6]:
                        spans = getattr(focus_bm, "line_ranges", {}).get(fpath, [])
                        tag = f":{','.join(spans)}" if spans else ""
                        detail.append(f"  - {fpath}{tag}")
                    for fpath, secs in list(getattr(focus_bm, "sections", {}).items())[:4]:
                        for sec in secs[:3]:
                            detail.append(f"  [SEC] {sec.get('name')} -> {fpath}:{sec.get('start')}-{sec.get('end')}")
                    if getattr(focus_bm, "linked_subjects", None):
                        detail.append(f"  linked: *{', *'.join(focus_bm.linked_subjects)}")
                    for dl in detail:
                        if row >= h - 1:
                            break
                        stdscr.addstr(row, 0, dl[:w - 1], curses.A_DIM)
                        row += 1
            elif state.tab_idx == 3:
                row = py
                if not state.items:
                    if row < h - 1:
                        stdscr.addstr(row, 0, "No staged files to view. Drop files in Tab 1 or toggle a *subject in Tab 2."[:w - 1])
                else:
                    focused_spec = state.items[min(state.cursor_idx, len(state.items) - 1)]
                    rel, s_ln, e_ln = parse_file_spec(focused_spec)
                    target_p = (Path(state.base) / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
                    if row < h - 1:
                        vhdr = f"Viewer: {focused_spec} "
                        stdscr.addstr(row, 0, vhdr[:w - 22], curses.A_BOLD)
                        cx = min(w - 20, len(vhdr))
                        if cx + 19 < w:
                            stdscr.addstr(row, cx, "[-10]", curses.A_REVERSE)
                            state.hitboxes.append((row, cx, cx + 4, "range_expand", (state.cursor_idx, 10, 0)))
                            cx += 6
                            stdscr.addstr(row, cx, "[+10]", curses.A_REVERSE)
                            state.hitboxes.append((row, cx, cx + 4, "range_expand", (state.cursor_idx, 0, 10)))
                            cx += 6
                            stdscr.addstr(row, cx, "[Full]", curses.A_DIM)
                            state.hitboxes.append((row, cx, cx + 5, "range_full", state.cursor_idx))
                        row += 1
                    if target_p.is_file():
                        all_lines = _read_lines_cached(state, target_p)
                        start_view = max(0, (s_ln - 1 if s_ln else 0) + state.viewer_scroll)
                        for lno in range(start_view, len(all_lines)):
                            if row >= h - 1:
                                break
                            one_based = lno + 1
                            in_span = (s_ln is not None and s_ln <= one_based <= (e_ln or s_ln))
                            prefix = ">>" if in_span else "  "
                            attr = curses.A_BOLD if in_span else curses.A_NORMAL
                            line_str = f"{prefix} {one_based:>4} | {all_lines[lno]}"[:w - 1]
                            stdscr.addstr(row, 0, line_str, attr)
                            state.hitboxes.append((row, 0, len(line_str) - 1, "viewer_line", (state.cursor_idx, one_based)))
                            row += 1
                    else:
                        if row < h - 1:
                            stdscr.addstr(row, 0, f"[MISSING FILE] {target_p}"[:w - 1])
            elif state.tab_idx == 4:
                row = py
                if row < h - 1:
                    stdscr.addstr(row, 0, f"Live Hosts & Session History ({len(state.history)}):"[:w - 1], curses.A_BOLD)
                    row += 1
                try:
                    from share.share import _load_registry as _lr2
                except ImportError:
                    try:
                        from share import _load_registry as _lr2
                    except ImportError:
                        from .share import _load_registry as _lr2  # type: ignore
                try:
                    live = [(s, e) for s, e in _lr2().items() if _is_pid_alive(e.get("pid"))]
                except Exception:
                    live = []
                for slug, e in live:
                    if row >= h - 1:
                        break
                    port = e.get("port", "?")
                    clone = e.get("clone_url", "") or f"http://127.0.0.1:{port}/{slug}"
                    btns_w = 18  # "[Copy URL] [Kill] "
                    left_budget = max(10, w - btns_w - 1)
                    info_str = f"  [LIVE] {slug} | {clone}"[:left_budget]
                    stdscr.addstr(row, 0, info_str, curses.A_BOLD)
                    cx = w - btns_w
                    if cx > len(info_str) and cx + 17 <= w - 1:
                        stdscr.addstr(row, cx, "[Copy URL]", curses.A_REVERSE)
                        state.hitboxes.append((row, cx, cx + 9, "copy_live_url", clone))
                        stdscr.addstr(row, cx + 11, "[Kill]", curses.A_DIM)
                        state.hitboxes.append((row, cx + 11, cx + 16, "kill_live", slug))
                    row += 1
                for h_item in reversed(state.history[-8:]):
                    if row >= h - 1:
                        break
                    st_tag = "OK" if h_item["ok"] else "ERR"
                    stdscr.addstr(row, 0, f"  [{st_tag}] [{h_item['zone']}] {' '.join(h_item['specs'])}"[:w - 1])
                    row += 1
                for ln in state.result_lines:
                    if row >= h - 1:
                        break
                    stdscr.addstr(row, 0, ln[:w - 1], curses.A_DIM)
                    row += 1
            elif state.tab_idx == 5:
                row = py
                try:
                    from share.share import _load_registry as _lr4
                except ImportError:
                    from .share import _load_registry as _lr4  # type: ignore
                reg_all = _lr4()
                total_live = sum(
                    1 for e in reg_all.values()
                    if (e.get("status", "live") == "live" if e.get("kind") == "gh" else _is_pid_alive(e.get("pid")))
                )

                total_retired = len(reg_all) - total_live
                all_svcs = state._filtered_services()
                state.svc_cursor = max(0, min(state.svc_cursor, max(0, len(all_svcs) - 1)))
                focused = all_svcs[state.svc_cursor] if all_svcs else None
                compact = w < 100
                # === LAYER 1: Filter Pills & Bulk Controls ===
                if row < h - 1:
                    pills = [
                        (f"[Live:{total_live}]", "svc_filter", "live", state.svc_filter == "live"),
                        (f"[All:{len(reg_all)}]", "svc_filter", "all", state.svc_filter == "all"),
                        (f"[Ret:{total_retired}]", "svc_filter", "retired", state.svc_filter == "retired"),
                        (f"[Kind:{state.svc_kind_filter}]", "svc_kind", None, state.svc_kind_filter != "all"),
                        ("[Sync GH]", "sync_gh", None, False),
                    ]
                    if total_retired:
                        pills.append(("[Purge Ret]", "purge_retired", None, False))
                    px = 0
                    for plabel, pkind, ppayload, pactive in pills:
                        if px + len(plabel) >= w:
                            break
                        stdscr.addstr(row, px, plabel, curses.A_REVERSE | curses.A_BOLD if pactive else curses.A_DIM)
                        state.hitboxes.append((row, px, px + len(plabel) - 1, pkind, ppayload))
                        px += len(plabel) + 1
                    row += 1
                # === LAYER 2: Scannable Service Roster ===
                drawer_budget = (9 if state.svc_expanded else 5) if focused else 1
                roster_max_row = max(row + 2, h - 1 - drawer_budget)
                visible_slots = max(1, (roster_max_row - row) // (2 if compact else 1))
                if state.svc_cursor < state.svc_scroll:
                    state.svc_scroll = state.svc_cursor
                elif state.svc_cursor >= state.svc_scroll + visible_slots:
                    state.svc_scroll = state.svc_cursor - visible_slots + 1
                if not all_svcs and row < roster_max_row:
                    stdscr.addstr(row, 0, f"  No {state.svc_filter} services (press [f] for All, [k] for Kind)."[:w - 1], curses.A_DIM)
                    row += 1
                now_utc = datetime.now(timezone.utc)
                for list_idx, (slug, e, st) in enumerate(all_svcs[state.svc_scroll:]):
                    if row >= roster_max_row:
                        break
                    svc_idx = state.svc_scroll + list_idx
                    is_focused = (svc_idx == state.svc_cursor)
                    kind = e.get("kind", "?")
                    target = e.get("url") or e.get("clone_url") or f"http://127.0.0.1:{e.get('port', '?')}/{slug}"
                    ttl_tag = ""
                    is_perm = bool(e.get("permanent"))
                    if st == "live" and is_perm:
                        ttl_tag = " PERM"
                    elif st == "live" and e.get("expires"):
                        try:
                            exp_dt = datetime.fromisoformat(e["expires"])
                            if exp_dt.tzinfo is None:
                                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                            mins_left = int((exp_dt - now_utc).total_seconds() // 60)
                            ttl_tag = f" {mins_left}m" if mins_left >= 0 else " exp"
                        except Exception:
                            pass
                    dot = "●" if st == "live" else "○"
                    if compact:
                        info_str = f"{'>' if is_focused else ' '}{dot}[{kind}]{ttl_tag} {slug}"[:w - 1]
                        stdscr.addstr(row, 0, info_str, curses.A_BOLD if is_focused else (curses.A_NORMAL if st == "live" else curses.A_DIM))
                        state.hitboxes.append((row, 0, min(w - 1, len(info_str)), "svc_row", svc_idx))
                        row += 1
                        if row < roster_max_row and is_focused:
                            bx = 2
                            _row_btns = [
                                ("[Vrfy]", "verify_service", slug, curses.A_REVERSE),
                                ("[Copy]", "copy_live_url", target, curses.A_REVERSE),
                            ]
                            if st == "live" and kind == "gh":
                                _row_btns.append(("[->Temp]" if is_perm else "[Perm]", "toggle_svc_perm", slug, curses.A_NORMAL))
                            if st == "live" and not is_perm:
                                _row_btns.append(("[Retire]", "retire_service", slug, curses.A_BOLD))
                            for bl, bk, bp, ba in _row_btns:
                                if bl and bx + len(bl) < w:
                                    stdscr.addstr(row, bx, bl, ba)
                                    state.hitboxes.append((row, bx, bx + len(bl) - 1, bk, bp))
                                    bx += len(bl) + 1
                            row += 1
                    else:
                        btns_w = 36 if (st == "live" and kind == "gh") else (29 if st == "live" else 19)
                        left_budget = max(12, w - btns_w - 1)
                        info_str = f"{'>' if is_focused else ' '} {dot} [{kind:<7}]{ttl_tag:<5} {slug} -> {target}"[:left_budget]
                        stdscr.addstr(row, 0, info_str, curses.A_BOLD if is_focused else (curses.A_NORMAL if st == "live" else curses.A_DIM))
                        state.hitboxes.append((row, 0, min(left_budget, len(info_str)), "svc_row", svc_idx))
                        cx = w - btns_w
                        if cx > len(info_str) and cx + btns_w <= w - 1:
                            stdscr.addstr(row, cx, "[Verify]", curses.A_REVERSE if is_focused else curses.A_NORMAL)
                            state.hitboxes.append((row, cx, cx + 7, "verify_service", slug))
                            cx += 9
                            stdscr.addstr(row, cx, "[Copy]", curses.A_REVERSE)
                            state.hitboxes.append((row, cx, cx + 5, "copy_live_url", target))
                            cx += 7
                            if st == "live" and kind == "gh":
                                plbl = "[->Temp]" if is_perm else "[Perm]  "
                                stdscr.addstr(row, cx, plbl, curses.A_NORMAL)
                                state.hitboxes.append((row, cx, cx + 6, "toggle_svc_perm", slug))
                                cx += 8
                            if st == "live" and not is_perm:
                                stdscr.addstr(row, cx, "[Retire]", curses.A_REVERSE | curses.A_BOLD)
                                state.hitboxes.append((row, cx, cx + 7, "retire_service", slug))
                        row += 1
                # === LAYER 3: Collapsible Inspector Drawer ===
                if focused and row < h - 1:
                    fslug, fe, fst = focused
                    fkind = fe.get("kind", "?")
                    ftarget = fe.get("url") or fe.get("clone_url") or f"http://127.0.0.1:{fe.get('port', '?')}/{fslug}"
                    exp_label = "[-Less]" if state.svc_expanded else "[+Details]"
                    div_title = f"─ Inspector ({state.svc_cursor + 1}/{len(all_svcs)}): {fslug} "
                    stdscr.addstr(row, 0, div_title[:max(10, w - len(exp_label) - 2)], curses.A_BOLD)
                    ex_x = min(w - len(exp_label) - 1, len(div_title))
                    if ex_x > 0:
                        stdscr.addstr(row, ex_x, exp_label, curses.A_REVERSE)
                        state.hitboxes.append((row, ex_x, ex_x + len(exp_label) - 1, "svc_toggle_expand", None))
                    row += 1
                    drawer: List[str] = [f"  endpoint: {ftarget}"]
                    if fkind == "gh":
                        vis_s = (fe.get("visibility") or "public").upper()
                        life_s = "PERMANENT" if fe.get("permanent") else "TEMP (TTL)"
                        drawer.append(f"  repo: {fe.get('repo', '?')} [{vis_s} / {life_s}] | acct: @{fe.get('account', '?')} | state: {fst.upper()}")
                        if fe.get("author"):
                            drawer.append(f"  author: {fe['author']}")
                        if fe.get("retire_error"):
                            drawer.append(f"  retire_err: {fe['retire_error'][:100]}")
                        if fe.get("files"):
                            shown_f = ", ".join(fe["files"][:4]) + (f" (+{len(fe['files']) - 4})" if len(fe["files"]) > 4 else "")
                            drawer.append(f"  payload: {shown_f}")
                    else:
                        drawer.append(f"  pid: {fe.get('pid', '-')} | port: {fe.get('port', '-')} | via: {fe.get('transport', '?')} | state: {fst.upper()}")
                    if state.svc_expanded:
                        if fe.get("confirmed_at"):
                            drawer.append(f"  confirmed: {(fe.get('confirmed_at') or '')[:19]} | expires: {(fe.get('expires') or '-')[:19]}")
                        if fe.get("retired_at"):
                            drawer.append(f"  retired_at: {(fe.get('retired_at') or '')[:19]}")
                        if fe.get("path"):
                            p_exists = Path(fe["path"]).exists()
                            drawer.append(f"  local_store: {fe['path']} ({'on-disk' if p_exists else 'cleaned'})")
                        files = fe.get("files") or []
                        if files:
                            shown = ", ".join(files[:4]) + (f" (+{len(files) - 4})" if len(files) > 4 else "")
                            drawer.append(f"  payload: {shown}")
                    drawer.append("  keys: [f] filter • [k] kind • [R] sync • [P] perm/temp • [i] details • Enter verify • [x] retire")
                    for dl in drawer:
                        if row >= h - 1:
                            break
                        stdscr.addstr(row, 0, dl[:w - 1], curses.A_DIM)
                        row += 1
            if state.gh_acct_dropdown:
                accts = state.gh_dropdown_accounts()
                content_w = max([len(a) + 8 for a in accts] + [22])
                box_w = max(24, min(w - 4, content_w))
                box_h = min(h - 2, len(accts) + 4)
                # Right-align under the [gh:@acct] pill; clamp inside the frame.
                ay, axx = state.gh_acct_anchor or (0, 0)
                anchor_acct = (state.gh_identity or {}).get("account") or state.gh_account
                pill_w = len(f"[gh:@{anchor_acct} ?]")
                bx0 = max(0, min(axx + pill_w - box_w, w - box_w - 1))
                by0 = ay + 1
                if by0 + box_h > h - 1:
                    by0 = max(0, h - 1 - box_h)
                try:
                    stdscr.addstr(by0, bx0, f" gh account ({len(accts)}/{len(state.gh_accounts)}) "[:box_w - 1], curses.A_REVERSE | curses.A_BOLD)
                    filt = f" filter: {state.gh_acct_filter}_"
                    stdscr.addstr(by0 + 1, bx0, filt[:box_w - 1], curses.A_DIM)
                    for ai, acct in enumerate(accts[:box_h - 2]):
                        is_cur = (acct == state.gh_account)
                        is_hl = (ai == state.gh_acct_cursor)
                        mark = "●" if is_cur else "○"
                        line = f" {'>' if is_hl else ' '} {mark} @{acct}"
                        attr = curses.A_REVERSE | curses.A_BOLD if is_hl else (curses.A_BOLD if is_cur else curses.A_NORMAL)
                        stdscr.addstr(by0 + 2 + ai, bx0, line[:box_w - 1], attr)
                        state.hitboxes.append((by0 + 2 + ai, bx0, bx0 + min(box_w, len(line)) - 1, "gh_acct_pick", acct))
                except Exception:
                    pass
            elif pin_prompt_mode:
                if rename_target:
                    _prompt = f"Rename [{rename_target}] -> {pin_name_buf}_ (Enter: rename, Esc: cancel)"
                elif ZONES[state.zone_idx] == "gh-temp":
                    _prompt = f"gh-temp repo name: {pin_name_buf}_ (Enter: create & push, Esc: cancel)"
                else:
                    _prompt = f"Pin selected to *subject name: {pin_name_buf}_ (Enter: save, Esc: cancel)"
                stdscr.addstr(h - 1, 0, _prompt[:w - 1], curses.A_REVERSE)
            elif state.confirm_flash:
                stdscr.addstr(h - 1, 0, state.confirm_flash[:w - 1], curses.A_REVERSE | curses.A_BOLD)
            else:
                bar_btns = []
                if state.last_remote_url:
                    bar_btns.append(("[Copy URL]", "copy_url"))
                    bar_btns.append(("[Copy Clone]", "copy_clone"))
                    bar_btns.append(("[Stop]", "stop_active"))
                btn_strip = ("  " + " ".join(b[0] for b in bar_btns)) if bar_btns else ""
                msg_budget = max(10, w - len(btn_strip) - 1)
                stdscr.addstr(h - 1, 0, state.msg[:msg_budget],
                              curses.A_BOLD if state.last_remote_url else curses.A_NORMAL)
                bx = min(w - len(btn_strip), len(state.msg[:msg_budget]) + 2)
                for blabel, bkind in bar_btns:
                    if bx + len(blabel) < w:
                        stdscr.addstr(h - 1, bx, blabel, curses.A_REVERSE | curses.A_BOLD)
                        state.hitboxes.append((h - 1, bx, bx + len(blabel) - 1, bkind, None))
                        bx += len(blabel) + 1
        except curses.error:
            pass
        stdscr.refresh()
        if _pending_keys:
            ch = _pending_keys.popleft()
        else:
            try:
                ch = stdscr.get_wch()
            except Exception:
                continue
        if state.gh_acct_dropdown:
            if ch in ("\x1b", 27):
                state.gh_acct_dropdown = False
                state.gh_acct_filter = ""
                state.msg = "gh account selection cancelled"
            elif ch in ("\n", "\r", "\x0d", 10, 13):
                accts = state.gh_dropdown_accounts()
                if accts:
                    state.select_gh_account(accts[min(state.gh_acct_cursor, len(accts) - 1)])
                else:
                    state.msg = "gh account: no match for filter"
            elif ch in ("\x7f", "\x08", 127, 8, curses.KEY_BACKSPACE, curses.KEY_DC):
                if ch in (curses.KEY_DC,):
                    pass  # delete key: ignore in filter prompt
                else:
                    state.gh_acct_filter = state.gh_acct_filter[:-1]
                    state.gh_acct_cursor = 0
            elif ch == curses.KEY_UP:
                state.gh_acct_cursor = max(0, state.gh_acct_cursor - 1)
            elif ch == curses.KEY_DOWN:
                accts = state.gh_dropdown_accounts()
                state.gh_acct_cursor = min(max(0, len(accts) - 1), state.gh_acct_cursor + 1)
            elif isinstance(ch, str) and ch.isprintable() and len(ch) == 1:
                state.gh_acct_filter += ch
                state.gh_acct_cursor = 0
            continue
        if state.auth_confirm_modal:
            if ch in ("\x1b", 27):
                state.auth_confirm_modal = False
                state.msg = "cancelled ephemeral remote push"
            elif ch in ("\n", "\r", "\x0d", 10, 13, "y", "Y"):
                if not state.gh_identity.get("ok"):
                    state.refresh_gh_identity(cycle_account=False)
                if not state.gh_identity.get("ok"):
                    state.msg = f"cannot confirm: {state.gh_identity.get('error', 'no token')}"
                else:
                    state.auth_confirm_modal = False
                    state.auth_confirmed = True
                    dispatch()
            elif ch == "u":
                state.open_gh_dropdown()
                continue
            elif ch == "o":
                state.gh_public = not state.gh_public
                ident = state.gh_identity
                vis = "PUBLIC" if state.gh_public else "PRIVATE"
                state.msg = f"CONFIRM -> @{ident.get('account', '?')} | {ident.get('author', '?')} | {vis} [Enter/y: push]"
            continue
        if pin_prompt_mode:
            if ch in ("\x1b", 27):
                pin_prompt_mode = False
                pin_name_buf = ""
                rename_target = ""
                state.msg = "cancelled subject pin"
            elif ch in ("\n", "\r", "\x0d", 10, 13):
                pin_prompt_mode = False
                if rename_target:
                    target = rename_target
                    rename_target = ""
                    state.rename_group(target, pin_name_buf)
                    pin_name_buf = ""
                    continue
                was_export = (ZONES[state.zone_idx] == "export")
                was_gh = (ZONES[state.zone_idx] == "gh-temp")
                if was_gh:
                    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", pin_name_buf.strip()).strip("-") or "pix-scratch"
                    state.gh_slug = slug
                    state.gh_slug_defined = True
                    state.msg = f"gh-temp -> {slug} — Enter to create & push"
                    pin_name_buf = ""
                    dispatch()
                    continue
                state.pin_selected_to_subject(pin_name_buf)
                pin_name_buf = ""
                if was_export and state.active_subjects:
                    dispatch()
                    continue
            elif ch in ("\x7f", "\x08", 127, 8, curses.KEY_BACKSPACE):
                pin_name_buf = pin_name_buf[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                pin_name_buf += ch
            continue
        if state.confirm_flash and ch != curses.KEY_RESIZE:
            # Transient confirm dialogue: dismiss on any new input, keep processing it
            state.confirm_flash = ""
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                try:
                    act = state.on_mouse_event(my, mx, bstate, curses_mod=curses)
                except Exception as e:
                    state.msg = f"mouse error: {e}"
                    continue
                if act == "dispatch":
                    dispatch()
                elif act == "pin":
                    pin_prompt_mode = True
                    pin_name_buf = state.active_subjects[0] if state.active_subjects else ""
                elif act == "rename_group":
                    gnames = list(state.groups.keys())
                    if gnames:
                        rename_target = gnames[min(state.group_cursor, len(gnames) - 1)]
                        pin_prompt_mode = True
                        pin_name_buf = rename_target
                        state.msg = f"rename [{rename_target}] — type new name, Enter to save"
                elif act in ("push_group", "push_group_done"):
                    if act == "push_group_done":
                        state.flash_confirm(f"[pushed] {state.msg} (Enter/Esc: dismiss)")
                elif act == "copy_url":
                    if state.last_remote_url and yank_to_clipboard(state.last_remote_url):
                        state.msg = f"copied URL: {state.last_remote_url}"
                        state.flash_confirm(f"[copied to clip] {state.last_remote_url} (Enter/Esc: dismiss)")
                    else:
                        state.msg = "no remote URL to copy yet"
                elif act == "copy_clone":
                    if state.last_clone_cmd and yank_to_clipboard(state.last_clone_cmd):
                        state.msg = f"copied: {state.last_clone_cmd[:60]}"
                        state.flash_confirm(f"[copied to clip] {state.last_clone_cmd[:60]} (Enter/Esc: dismiss)")
                    else:
                        state.msg = "no clone command yet"
                elif act == "stop_active":
                    _stop_active_share(state)
            except Exception:
                pass
            continue
        if isinstance(ch, str):
            if ch == "\x1b":
                # Drain pending escape bytes (SGR mouse \x1b[<btn;x;yM).
                # On TERM without XM, get_wch returns bare \x1b for mouse
                # packets — without this drain every click reads as Esc!
                # Only "[" opens a sequence; anything else is a plain Esc
                # and the drained key is queued for the next iteration.
                seq = ""
                stdscr.nodelay(True)
                try:
                    first = stdscr.get_wch()
                except Exception:
                    first = -1
                finally:
                    try:
                        stdscr.nodelay(False)
                    except Exception:
                        pass
                if not isinstance(first, str) or first != "[":
                    if isinstance(first, str) and len(first) == 1:
                        _pending_keys.append(first)
                    # plain Esc below
                else:
                    seq = "["
                    stdscr.nodelay(True)
                    try:
                        for _ in range(22):
                            nxt = stdscr.get_wch()
                            if not isinstance(nxt, str) or len(nxt) != 1:
                                break
                            seq += nxt
                            if seq == "[M" or (nxt in ("m", "~") and len(seq) >= 3):
                                break
                            if nxt == "M" and len(seq) >= 4:
                                break  # SGR press terminator
                            if seq[-1] not in "<;0123456789Mm~M":
                                seq = ""  # junk; treat as plain Esc
                                break
                    except Exception:
                        pass
                    finally:
                        try:
                            stdscr.nodelay(False)
                        except Exception:
                            pass
                if seq == "[200~":
                    # Bracketed paste start: drain entire payload before rendering
                    blob = _drain_paste_burst(bracketed=True)
                    _finalize_pasted_blob(blob)
                    continue
                if seq == "[201~":
                    continue
                if seq == "[M":
                    # X10 fallback: terminal ignored ?1006h, sent \x1b[M + 3 raw bytes
                    coords = []
                    stdscr.nodelay(True)
                    try:
                        for _ in range(3):
                            b = stdscr.get_wch()
                            coords.append(ord(b) if isinstance(b, str) and len(b) == 1 else -1)
                    except Exception:
                        pass
                    finally:
                        try:
                            stdscr.nodelay(False)
                        except Exception:
                            pass
                    if len(coords) == 3 and all(c >= 0 for c in coords):
                        cb, cx, cy = coords[0] - 32, max(0, coords[1] - 33), max(0, coords[2] - 33)
                        bstate = curses.BUTTON1_RELEASED if (cb & 3) == 3 else curses.BUTTON1_PRESSED
                        try:
                            act = state.on_mouse_event(cy, cx, bstate, curses_mod=curses)
                        except Exception as e:
                            state.msg = f"mouse error: {e}"
                            continue
                        if act == "dispatch":
                            dispatch()
                        elif act == "pin":
                            pin_prompt_mode = True
                            pin_name_buf = state.active_subjects[0] if state.active_subjects else ""
                        elif act == "copy_url":
                            if state.last_remote_url and yank_to_clipboard(state.last_remote_url):
                                state.msg = f"copied URL: {state.last_remote_url}"
                                state.flash_confirm(f"[copied to clip] {state.last_remote_url} (Enter/Esc: dismiss)")
                        elif act == "copy_clone":
                            if state.last_clone_cmd and yank_to_clipboard(state.last_clone_cmd):
                                state.msg = f"copied: {state.last_clone_cmd[:60]}"
                                state.flash_confirm(f"[copied to clip] {state.last_clone_cmd[:60]} (Enter/Esc: dismiss)")
                        elif act == "stop_active":
                            _stop_active_share(state)
                    continue
                m_sgr = re.match(r"^\[<(\d+);(\d+);(\d+)([Mm])$", seq) if seq else None
                if m_sgr:
                    btn_code = int(m_sgr.group(1))
                    mx = max(0, int(m_sgr.group(2)) - 1)
                    my = max(0, int(m_sgr.group(3)) - 1)
                    is_release = (m_sgr.group(4) == "m")
                    base_btn = btn_code & 0b11
                    is_motion = bool(btn_code & 32)
                    is_shift = bool(btn_code & 4)
                    bstate = curses.BUTTON_SHIFT if is_shift else 0
                    if btn_code & 64:
                        bstate |= curses.BUTTON4_PRESSED if base_btn == 0 else curses.BUTTON5_PRESSED
                    elif is_motion:
                        bstate |= curses.REPORT_MOUSE_POSITION
                    elif base_btn == 0:
                        bstate |= curses.BUTTON1_RELEASED if is_release else curses.BUTTON1_PRESSED
                    elif base_btn == 2:
                        bstate |= curses.BUTTON3_RELEASED if is_release else curses.BUTTON3_PRESSED
                    try:
                        act = state.on_mouse_event(my, mx, bstate, curses_mod=curses)
                    except Exception as e:
                        state.msg = f"mouse error: {e}"
                        continue
                    if act == "dispatch":
                        dispatch()
                    elif act == "pin":
                        pin_prompt_mode = True
                        pin_name_buf = state.active_subjects[0] if state.active_subjects else ""
                    elif act == "copy_url":
                        if state.last_remote_url and yank_to_clipboard(state.last_remote_url):
                            state.msg = f"copied URL: {state.last_remote_url}"
                            state.flash_confirm(f"[copied to clip] {state.last_remote_url} (Enter/Esc: dismiss)")
                    elif act == "copy_clone":
                        if state.last_clone_cmd and yank_to_clipboard(state.last_clone_cmd):
                            state.msg = f"copied: {state.last_clone_cmd[:60]}"
                            state.flash_confirm(f"[copied to clip] {state.last_clone_cmd[:60]} (Enter/Esc: dismiss)")
                    elif act == "stop_active":
                        _stop_active_share(state)
                    continue
                if not state.buf and not state.items:
                    break
                state.buf = ""
                state.items.clear()
                state.selected.clear()
                state.msg = "cleared drop buffer and staged items"
            elif ch in ("\n", "\r", "\x0d"):
                handle_enter()
            elif ch == "\t":
                if state.buf.strip() and state.tab_idx == 0:
                    matches = state.complete_matches(1)
                    if matches:
                        state.buf = matches[0]
                        state.msg = f"completed: {matches[0]} — Enter to stage"
                    else:
                        state.msg = "no matches for autocomplete"
                else:
                    state.zone_idx = (state.zone_idx + 1) % len(ZONES)
            elif ch in ("1", "2", "3", "4", "5", "6") and not state.buf:
                state.tab_idx = int(ch) - 1
                state.cursor_idx = 0
                state.group_cursor = 0
                if state.tab_idx == 5:
                    state.sync_gh_services()
                state.msg = f"tab: {TABS[state.tab_idx]}"
            elif ch == "[" and not state.buf:
                state.tab_idx = (state.tab_idx - 1) % len(TABS)
                state.cursor_idx = 0
            elif ch == "]" and not state.buf:
                state.tab_idx = (state.tab_idx + 1) % len(TABS)
                state.cursor_idx = 0
            elif ch == " " and not state.buf and state.tab_idx == 0:
                state.toggle_item(state.cursor_idx)
            elif ch == "m" and not state.buf:
                state.save_selection_as_group()
            elif ch == "r" and not state.buf and state.tab_idx == 1 and state.groups:
                gnames = list(state.groups.keys())
                rename_target = gnames[min(state.group_cursor, len(gnames) - 1)]
                pin_prompt_mode = True
                pin_name_buf = rename_target
                state.msg = f"rename [{rename_target}] — type new name, Enter to save"
            elif ch == "s" and not state.buf and state.tab_idx == 1 and state.groups:
                gnames = list(state.groups.keys())
                state.promote_group_to_subject(gnames[min(state.group_cursor, len(gnames) - 1)])
            elif ch == "u" and not state.buf and state.tab_idx == 1 and state.groups:
                gnames = list(state.groups.keys())
                gname = gnames[min(state.group_cursor, len(gnames) - 1)]
                state.active_group = gname
                res = state.push_group(gname)
                if not res.get("ok"):
                    state.msg = f"push failed [{gname}]: {res.get('error', 'unknown')}"
            elif ch == "p" and not state.buf:
                pin_prompt_mode = True
                pin_name_buf = state.active_subjects[0] if state.active_subjects else ""
            elif ch == "P" and not state.buf:
                if state.tab_idx == 5:
                    state.toggle_focused_gh_permanence()
                else:
                    state.populate_from_clipboard()
            elif ch == "L" and not state.buf:
                state.gh_permanent = not state.gh_permanent
                state.msg = f"gh lifecycle -> {'PERMANENT (GC-exempt)' if state.gh_permanent else 'TEMP (60m TTL)'}"
            elif ch == "v" and not state.buf:
                state.split_pane = not state.split_pane
                state.msg = f"split-pane viewer {'ON' if state.split_pane else 'OFF'}"
            elif ch in ("q", "Q") and not state.buf:
                break
            elif ch == "a" and not state.buf:
                state.auto_drop = not state.auto_drop
                state.msg = f"auto-drop {'ENABLED (drops dispatch immediately)' if state.auto_drop else 'DISABLED'}"
            elif ch == "o" and not state.buf:
                state.gh_public = not state.gh_public
                state.msg = f"gh-temp visibility -> {'PUBLIC (Import dialogs resolve)' if state.gh_public else 'PRIVATE (needs GitHub login)'}"
            elif ch == "u" and not state.buf:
                state.cycle_gh_account()
            elif ch == "U" and not state.buf:
                state.open_gh_dropdown()
            elif ch == "I" and not state.buf:
                info = state.refresh_gh_identity(cycle_account=False)
                if info.get("ok"):
                    scope_warn = "" if info.get("can_delete") else " [NO delete_repo scope]"
                    state.msg = f"gh auth -> @{info['account']} | author: {info['author']}{scope_warn}"
                else:
                    state.msg = f"gh auth error: {info.get('error', 'unknown')}"
            elif ch == "f" and not state.buf and state.tab_idx == 5:
                modes = ("live", "all", "retired")
                state.svc_filter = modes[(modes.index(state.svc_filter) + 1) % len(modes)]
                state.svc_cursor = 0
                state.svc_scroll = 0
                state.msg = f"services filter: {state.svc_filter.upper()}"
            elif ch == "k" and not state.buf and state.tab_idx == 5:
                kinds = ("all", "gh", "gh-perm", "git", "snippet")
                state.svc_kind_filter = kinds[(kinds.index(state.svc_kind_filter) + 1) % len(kinds)]
                state.svc_cursor = 0
                state.svc_scroll = 0
                state.msg = f"service kind filter: {state.svc_kind_filter.upper()}"
            elif ch == "i" and not state.buf and state.tab_idx == 5:
                state.svc_expanded = not state.svc_expanded
                state.msg = f"inspector {'expanded' if state.svc_expanded else 'compact'}"
            elif ch == "R" and not state.buf and state.tab_idx == 5:
                state.sync_gh_services()
            elif ch == "Y" and not state.buf:
                if state.pending:
                    state.confirm_pending(0)
                else:
                    state.msg = "no (?) items to confirm"
            elif ch == "y" and not state.buf:
                if state.tab_idx == 5:
                    _slug, _e = state._svc_entry_at_cursor()
                    _tgt = (_e.get("url") or _e.get("clone_url")) if _e else None
                    if _tgt and yank_to_clipboard(_tgt):
                        state.msg = f"copied URL: {_tgt}"
                        state.flash_confirm(f"[copied to clip] {_tgt} (Enter/Esc: dismiss)")
                    else:
                        state.msg = "no focused service URL to copy"
                elif state.pending:
                    state.confirm_pending(0)
                elif state.last_yank and yank_to_clipboard(state.last_yank):
                    state.msg = f"yanked to clipboard: {state.last_yank}"
                else:
                    state.msg = f"yank: {state.last_yank or 'nothing dispatched yet'}"
            elif ch == "g" and not state.buf:
                state.zone_idx = list(ZONES).index("git-remote")
                dispatch()
            elif ch == "G" and not state.buf:
                state.zone_idx = list(ZONES).index("gh-temp")
                dispatch()
            elif ch == "c" and not state.buf:
                if state.last_clone_cmd and yank_to_clipboard(state.last_clone_cmd):
                    state.msg = f"copied: {state.last_clone_cmd[:80]}"
                else:
                    state.msg = f"clone: {state.last_clone_cmd or 'nothing dispatched yet'}"
            elif ch == "x" and not state.buf:
                if state.tab_idx == 5:
                    _slug, _e = state._svc_entry_at_cursor()
                    if _slug and _e:
                        try:
                            from share.share import _cleanup_slug as _csx
                        except ImportError:
                            try:
                                from share import _cleanup_slug as _csx  # type: ignore
                            except ImportError:
                                from .share import _cleanup_slug as _csx  # type: ignore
                        try:
                            _csx(_slug, _e)
                            if _e.get("kind") != "gh":
                                from share.share import _load_registry as _lrx, _save_registry as _srx
                                _r = _lrx()
                                _r.pop(_slug, None)
                                _srx(_r)
                            state.msg = f"confirmed retired: {_slug}"
                        except Exception as ex:
                            state.msg = f"retire failed ({_slug}): {ex}"
                    else:
                        state.msg = "no focused service to retire"
                else:
                    _stop_active_share(state)
            elif ch == "b" and not state.buf:
                if not state.history:
                    state.msg = "history empty: nothing dispatched yet"
                else:
                    last = state.history[-1]
                    state.items = list(last["specs"])
                    state.selected = set(range(len(state.items)))
                    state.zone_idx = list(ZONES).index(last["zone"])
                    state.msg = f"back: restored {last['zone']} payload ({len(state.history)} in history)"
            elif ch == "/" and not state.buf:
                try:
                    from share.share import lookup_index
                except ImportError:
                    try:
                        from share import lookup_index
                    except ImportError:
                        from .share import lookup_index  # type: ignore
                idx = lookup_index(None)
                state.result_lines = json.dumps(
                    {s: {"kind": e.get("kind"), "files": e.get("files"),
                         "bundle_exists": e.get("bundle_exists")} for s, e in idx.items()},
                    indent=2).splitlines()
                state.tab_idx = 4
                state.msg = f"index: {len(idx)} share(s) by path (survives temp-store deletion)"
            elif ch in ("\x7f", "\x08"):
                state.buf = state.buf[:-1]
            elif ch == PASTE_OPEN or state.buf.endswith(PASTE_OPEN):
                state.buf = strip_paste_markers(state.buf)
            else:
                if ch.isprintable():
                    # Check if more bytes are already queued in stdin (unbracketed paste burst)
                    burst = _drain_paste_burst(initial=ch, bracketed=False)
                    if len(burst) > 1 or "\n" in burst:
                        _finalize_pasted_blob(state.buf + burst)
                    else:
                        state.buf += burst
                        if state.auto_drop and state.buf.endswith(" ") and parse_drops(state.buf):
                            cleaned = state.buf.strip()
                            state.buf = ""
                            state.ingest_buffer(cleaned)
                            dispatch()
        elif ch == curses.KEY_RESIZE:
            _reassert_terminal_modes()
        elif ch == curses.KEY_UP:
            if state.tab_idx == 1:
                state.group_cursor = max(0, state.group_cursor - 1)
            elif state.tab_idx == 3:
                state.viewer_scroll = max(0, state.viewer_scroll - 1)
            elif state.tab_idx == 5:
                state.svc_cursor = max(0, state.svc_cursor - 1)
                if state.svc_cursor < state.svc_scroll:
                    state.svc_scroll = state.svc_cursor
            else:
                state.cursor_idx = max(0, state.cursor_idx - 1)
        elif ch == curses.KEY_DOWN:
            if state.tab_idx == 1:
                state.group_cursor = min(max(0, len(state.groups) - 1), state.group_cursor + 1)
            elif state.tab_idx == 3:
                state.viewer_scroll += 1
            elif state.tab_idx == 5:
                state.svc_cursor += 1
                # Clamp next frame; pull scroll when cursor leaves the page.
                state.svc_scroll = min(state.svc_scroll + 1, state.svc_cursor)
            else:
                state.cursor_idx += 1
        elif ch == curses.KEY_LEFT and state.items:
            state.cursor_idx = (state.cursor_idx - 1) % len(state.items)
            state.viewer_scroll = 0
        elif ch == curses.KEY_RIGHT and state.items:
            state.cursor_idx = (state.cursor_idx + 1) % len(state.items)
            state.viewer_scroll = 0
        elif ch in (9, 353):
            step = 1 if ch == 9 else -1
            state.zone_idx = (state.zone_idx + step) % len(ZONES)
        elif ch in (27, 113, 81):
            if not state.buf and not state.items:
                break
            state.buf = ""
            state.items.clear()
            state.selected.clear()
        elif ch in (curses.KEY_ENTER, 10, 13):
            handle_enter()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            state.buf = state.buf[:-1]
    try:
        curses.putp("\033[?2004l")
        curses.putp("\033[?1003l\033[?1000l\033[?1006l")
        sys.stdout.flush()
    except Exception:
        pass
    return 0
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="source share-tui")
    p.add_argument("--test", action="store_true")
    p.add_argument("--zone", choices=list(ZONES), default="share",
                   help="drop zone: share, task, spawn, export, git-remote, gh-temp, source, drag-out, poll-clip")
    p.add_argument("drops", nargs="*")
    args = p.parse_args(argv)
    if args.test or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return run_share_tui(test_mode=True, drops=args.drops, zone=args.zone)
    import curses
    os.environ.setdefault("ESCDELAY", "25")  # atomic escape delivery, no 1000ms Esc stall
    try:
        return curses.wrapper(lambda s: run_share_tui(s, zone=args.zone))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

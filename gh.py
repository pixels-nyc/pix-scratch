#!/usr/bin/env python3
"""gh — ephemeral GitHub repos for Import-dialog-pasteable shares.

Backs the `share share-gh` / `gh-temp` zone: public-by-default scratch repos
created via the `gh` CLI with credentials routed through `pix` (`GH_TOKEN`),
pushed destructively (`--force`) from a `create_temp_remote` snapshot,
TTL-registered, and deleted on expiry/stop (needs `delete_repo` scope:
`gh auth refresh -s delete_repo`).
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

_SHARE_DIR = str(Path(__file__).resolve().parent)
if _SHARE_DIR not in sys.path:
    sys.path.insert(0, _SHARE_DIR)

try:
    from share.share import (create_temp_remote, _load_registry, _save_registry,
                             record_index, make_agent_envelope)
except ImportError:
    try:
        from share import (create_temp_remote, _load_registry, _save_registry,
                           record_index, make_agent_envelope)
    except ImportError:
        from .share import (create_temp_remote, _load_registry, _save_registry,
                            record_index, make_agent_envelope)  # type: ignore

try:
    from subject.subject import resolve_subject_scope
except ImportError:
    try:
        from common.subject import resolve_subject_scope  # type: ignore
    except ImportError:
        resolve_subject_scope = None  # type: ignore


def _expand_specs(specs: list[str], base: Path) -> list[str]:
    """Expand `*subject` tokens into concrete file specs for CLI & programmatic parity."""
    if not specs or resolve_subject_scope is None:
        return list(specs)
    expanded: list[str] = []
    for item in specs:
        if item.startswith("*"):
            scope = resolve_subject_scope(item, root_dir=base)
            for sp in scope.to_share_specs():
                if sp not in expanded:
                    expanded.append(sp)
        elif item not in expanded:
            expanded.append(item)
    return expanded


def list_pix_gh_accounts() -> list:
    """Return configured GitHub account names from `pix gh list` (empty list if unavailable)."""
    pix_bin = shutil.which("pix") or os.path.expanduser("~/.local/bin/pix")
    if not pix_bin or not os.path.isfile(pix_bin):
        return []
    try:
        r = subprocess.run([pix_bin, "gh", "list"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        accts: list = []
        for ln in r.stdout.splitlines():
            clean = re.sub(r"^[\s\*\->\u2022]+", "", ln).strip()
            tok = clean.split()[0].rstrip(":") if clean else ""
            if tok and tok not in ("(none)", "Accounts", "GitHub") and re.match(r"^[A-Za-z0-9_.-]+$", tok):
                if tok not in accts:
                    accts.append(tok)
        return accts
    except Exception:
        return []


def resolve_git_author(base=None, env=None, owner: str = "") -> tuple:
    """Resolve (author_name, author_email) from git config, GH_TOKEN env, or noreply fallback."""
    env = env or {}
    name = env.get("GIT_AUTHOR_NAME", "") or ""
    email = env.get("GIT_AUTHOR_EMAIL", "") or ""
    if (not name or not email) and base is not None:
        try:
            cwd = str(base) if not isinstance(base, str) else base
            for key in ("user.name", "user.email"):
                rcfg = subprocess.run(["git", "config", "--get", key], cwd=cwd,
                                      capture_output=True, text=True, timeout=3)
                if rcfg.returncode == 0 and rcfg.stdout.strip():
                    if key == "user.name" and not name:
                        name = rcfg.stdout.strip()
                    elif key == "user.email" and not email:
                        email = rcfg.stdout.strip()
        except Exception:
            pass
    name = name or (owner or "pix-ephemeral")
    email = email or f"{owner or 'pix'}@users.noreply.github.com"
    return name, email


def confirm_gh_identity(account: str | None = None, base=None) -> dict:
    """Verify active GH auth, OAuth scopes (incl. delete_repo), and commit author identity."""
    try:
        env, owner = pix_gh_env(account, require_token=True)
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "account": account or "", "author": "",
                "author_name": "", "author_email": "", "can_delete": False}
    scopes: list = []
    gh_bin = shutil.which("gh")
    if gh_bin:
        try:
            hdr = subprocess.run([gh_bin, "api", "user", "-i"], env=env,
                                 capture_output=True, text=True, timeout=6)
            if hdr.returncode == 0:
                for ln in hdr.stdout.splitlines():
                    if ln.lower().startswith("x-oauth-scopes:"):
                        scopes = [s.strip() for s in ln.split(":", 1)[1].split(",") if s.strip()]
                        break
        except Exception:
            pass
    author_name, author_email = resolve_git_author(base, env, owner)
    can_delete = ("delete_repo" in scopes) if scopes else True
    return {"ok": True, "account": owner, "author_name": author_name,
            "author_email": author_email, "author": f"{author_name} <{author_email}>",
            "scopes": scopes, "can_delete": can_delete}


def pix_gh_env(account: str | None = None, require_token: bool = True) -> tuple[dict, str]:
    """Build isolated env with GH_TOKEN from pix's active (or specified) gh account."""
    env = dict(os.environ)
    pix_bin = shutil.which("pix") or os.path.expanduser("~/.local/bin/pix")
    owner = (account or "").strip()

    if not pix_bin or not os.path.isfile(pix_bin):
        if require_token:
            raise RuntimeError("pix binary not found on PATH or ~/.local/bin/pix")
        return env, owner

    r = subprocess.run([pix_bin, "gh", "token"] + ([owner] if owner else []),
                       capture_output=True, text=True, timeout=5)
    token = r.stdout.strip() if r.returncode == 0 else ""
    if not token:
        if require_token:
            acct_label = owner or "active"
            err_detail = (r.stderr or "").strip()
            raise RuntimeError(f"pix gh token failed for '{acct_label}' account"
                               + (f": {err_detail}" if err_detail else ""))
    else:
        env["GH_TOKEN"] = token

    if not owner:
        # Prefer the authenticated login of the token itself so owner/slug always matches GH_TOKEN
        who = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                             env=env, capture_output=True, text=True, timeout=5)
        if who.returncode == 0 and who.stdout.strip():
            owner = who.stdout.strip()
    if not owner:
        st = subprocess.run([pix_bin, "current", "gh"], capture_output=True, text=True, timeout=5)
        if st.returncode == 0 and ":" in st.stdout:
            val = st.stdout.split(":", 1)[1].strip()
            if val and val != "(none)":
                owner = val

    return env, owner


def verify_gh_repo_live(repo: str, account: str | None = None) -> dict:
    """Probe GitHub via gh + pix auth to confirm whether a remote repo is live or retired."""
    gh_bin = shutil.which("gh")
    if not repo or not gh_bin:
        return {"live": False, "state": "unknown", "reason": "gh CLI or repo missing"}
    try:
        env, _ = pix_gh_env(account, require_token=True)
    except RuntimeError as e:
        return {"live": False, "state": "auth_error", "reason": str(e)}
    r = subprocess.run([gh_bin, "repo", "view", repo, "--json", "nameWithOwner,url,pushedAt"],
                       env=env, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        info = json.loads(r.stdout) if r.stdout.strip() else {}
        return {"live": True, "state": "live", "repo": info.get("nameWithOwner", repo),
                "url": info.get("url", ""), "pushed_at": info.get("pushedAt", "")}
    err = (r.stderr or r.stdout).strip()
    if "Could not resolve to a Repository" in err or "Not Found" in err or "404" in err:
        return {"live": False, "state": "retired", "reason": "not found on GitHub"}
    return {"live": False, "state": "error", "reason": err}


def delete_gh_repo_detailed(repo: str, account: str | None = None) -> tuple:
    """Delete a GitHub repo via gh + pix auth. Returns (ok, error_detail)."""
    gh_bin = shutil.which("gh")
    if not repo or not gh_bin:
        return False, "gh CLI or repo name missing"
    tried_envs: list = []
    for acct in ([account, None] if account else [None]):
        try:
            env, _ = pix_gh_env(acct, require_token=True)
            tried_envs.append(env)
        except RuntimeError as e:
            if not tried_envs and acct is None:
                return False, str(e)
    if not tried_envs:
        return False, f"no valid pix token for account '{account}'"
    last_err = ""
    for env in tried_envs:
        r = subprocess.run([gh_bin, "repo", "delete", repo, "--yes"],
                           env=env, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, ""
        last_err = (r.stderr or r.stdout or "").strip()
        if "not found" in last_err.lower() or "could not resolve" in last_err.lower():
            return True, ""  # already deleted remotely
    return False, last_err or "gh repo delete failed"


def delete_gh_repo(repo: str, account: str | None = None) -> bool:
    """Delete a GitHub repo created by this module. Returns True on success."""
    ok, _ = delete_gh_repo_detailed(repo, account)
    return ok


def retire_gh_repo(slug: str, entry: dict | None = None, verify: bool = True,
                   force_perm: bool = False) -> dict:
    """Delete a temp GitHub repo remotely and record 'retired' ONLY when confirmed gone."""
    reg = _load_registry()
    e = entry or reg.get(slug) or {}
    if e.get("permanent") and not force_perm:
        return {"ok": False, "slug": slug, "state": "live",
                "error": "repo is marked PERMANENT — demote to temp before retiring"}
    repo = e.get("repo", "")
    account = e.get("account")
    if not repo:
        return {"ok": False, "slug": slug, "state": "unknown", "error": "missing repo name"}

    deleted, del_err = delete_gh_repo_detailed(repo, account)
    if not deleted and verify:
        probe = verify_gh_repo_live(repo, account)
        if probe.get("state") == "retired":
            deleted = True
        else:
            err_msg = del_err or probe.get("reason") or "gh repo delete failed (run: gh auth refresh -s delete_repo)"
            if slug in reg:
                reg[slug]["status"] = "live"  # keep live so TUI stays actionable!
                reg[slug]["retire_error"] = err_msg
                _save_registry(reg)
            return {"ok": False, "slug": slug, "repo": repo, "state": "live", "error": err_msg}

    now_iso = datetime.now(timezone.utc).isoformat()
    if slug in reg:
        reg[slug]["status"] = "retired"
        reg[slug]["retired_at"] = now_iso
        reg[slug].pop("retire_error", None)
        _save_registry(reg)
    return {"ok": True, "slug": slug, "repo": repo, "state": "retired", "retired_at": now_iso}


def set_gh_repo_permanence(slug: str, permanent: bool = True, ttl_min: int = 60) -> dict:
    """Toggle a registered GitHub repo between permanent (GC-exempt) and ephemeral (TTL)."""
    reg = _load_registry()
    e = reg.get(slug)
    if not e or e.get("kind") != "gh":
        return {"ok": False, "error": f"unknown gh share '{slug}'"}
    now = datetime.now(timezone.utc)
    e["permanent"] = bool(permanent)
    if permanent:
        e["expires"] = None
        e["status"] = "live"
    else:
        e["expires"] = (now + timedelta(minutes=ttl_min)).isoformat()
    e["updated_at"] = now.isoformat()
    reg[slug] = e
    _save_registry(reg)
    return {"ok": True, "slug": slug, "repo": e.get("repo"),
            "permanent": e["permanent"], "expires": e["expires"]}


def sync_gh_repos_to_registry(account: str | None = None, limit: int = 30) -> dict:
    """Populate Tab 6 with live GitHub repos and reconcile stale registry status."""
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return {"ok": False, "error": "gh CLI not found", "added": 0, "updated": 0}
    try:
        env, owner = pix_gh_env(account, require_token=True)
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "added": 0, "updated": 0}
    r = subprocess.run(
        [gh_bin, "repo", "list", owner, "--limit", str(limit),
         "--json", "name,nameWithOwner,url,isPrivate,description,pushedAt"],
        env=env, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip(), "added": 0, "updated": 0}
    remote_repos = json.loads(r.stdout) if r.stdout.strip() else []
    remote_by_nwo = {it["nameWithOwner"]: it for it in remote_repos if it.get("nameWithOwner")}
    reg = _load_registry()
    added, updated = 0, 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for slug, e in reg.items():
        if e.get("kind") != "gh":
            continue
        nwo = e.get("repo", "")
        if nwo in remote_by_nwo:
            info = remote_by_nwo[nwo]
            if e.get("status") != "live":
                e["status"] = "live"
                updated += 1
            e["url"] = info.get("url") or e.get("url")
            e["visibility"] = "private" if info.get("isPrivate") else "public"
            e["confirmed_at"] = now_iso
        elif e.get("account", owner) == owner and e.get("status") == "live":
            probe = verify_gh_repo_live(nwo, owner)
            if probe.get("state") == "retired":
                e["status"] = "retired"
                e.setdefault("retired_at", now_iso)
                updated += 1
    known_nwos = {e.get("repo") for e in reg.values() if e.get("kind") == "gh"}
    for item in remote_repos:
        nwo = item.get("nameWithOwner", "")
        if not nwo or nwo in known_nwos:
            continue
        desc = item.get("description") or ""
        name = item.get("name") or nwo.split("/")[-1]
        if not (("pixos ephemeral" in desc.lower()) or ("pixos permanent" in desc.lower())
                or name.startswith("pix-")):
            continue
        slug = name if name not in reg else f"{name}-{owner}"
        is_perm = "pixos permanent" in desc.lower()
        reg[slug] = {"kind": "gh", "repo": nwo, "url": item.get("url") or f"https://github.com/{nwo}",
                     "account": owner, "permanent": is_perm,
                     "expires": None if is_perm else (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
                     "visibility": "private" if item.get("isPrivate") else "public",
                     "transport": "github", "status": "live", "confirmed_at": now_iso}
        added += 1
    _save_registry(reg)
    return {"ok": True, "account": owner, "added": added, "updated": updated,
            "total_remote": len(remote_repos)}


def _find_reusable_gh_repo(reg: dict, owner: str) -> tuple[str, str, str] | None:
    """Find an existing live gh repo in registry.json to reuse when rate-limited."""
    for s, e in reversed(list(reg.items())):
        if e.get("kind") == "gh" and e.get("status", "live") == "live" and e.get("repo"):
            if not owner or e.get("account", owner) == owner:
                nwo = e["repo"]
                url = e.get("url") or f"https://github.com/{nwo}"
                return s, nwo, url
    return None


def create_temp_gh_repo(files: list, base: Path, slug: str,
                        ttl_min: int = 60, account: str | None = None,
                        private: bool = False, force: bool = True,
                        author_override: tuple | None = None,
                        permanent: bool = False, group: str | None = None) -> dict:
    """Create or destructively overwrite a temp GitHub repo via gh + pix auth."""
    gh_bin = shutil.which("gh")
    if not gh_bin:
        raise RuntimeError("gh CLI not found on PATH")

    env, owner = pix_gh_env(account, require_token=True)
    if not owner:
        raise RuntimeError("Could not resolve GitHub owner from `pix gh token`")
    if author_override and len(author_override) == 2:
        author_name, author_email = str(author_override[0]), str(author_override[1])
    else:
        author_name = env.get("GIT_AUTHOR_NAME", "") or owner
        author_email = env.get("GIT_AUTHOR_EMAIL", "") or f"{owner}@users.noreply.github.com"
    author_str = f"{author_name} <{author_email}>"
    expanded_files = _expand_specs(list(files or []), base)
    if files and any(f.startswith("*") for f in files) and not expanded_files:
        raise ValueError(f"subject spec(s) resolved to 0 files: {' '.join(files)}")

    dest = create_temp_remote(expanded_files, base, slug)
    # Stamp the confirmed author/committer on the snapshot commit (skip bare repos).
    author_env = {**env, "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
                  "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email}
    try:
        is_bare = subprocess.run(["git", "rev-parse", "--is-bare-repository"],
                                 cwd=dest, capture_output=True, text=True,
                                 timeout=5).stdout.strip() == "true"
    except Exception:
        is_bare = False
    if not is_bare:
        try:
            subprocess.run(["git", "commit", "--amend", "--no-edit", "--reset-author"],
                           cwd=dest, env=author_env, capture_output=True, text=True, timeout=10)
        except Exception:
            pass
    vis_flag = "--private" if private else "--public"
    full_name = f"{owner}/{slug}"
    created_new = False

    exists_check = subprocess.run([gh_bin, "repo", "view", full_name, "--json", "url,nameWithOwner"],
                                  cwd=dest, env=env, capture_output=True, text=True, timeout=15)
    if exists_check.returncode == 0 and force:
        info = json.loads(exists_check.stdout)
        # Ensure a reused scratch repo matches the requested visibility
        vis_edit = "private" if private else "public"
        subprocess.run([gh_bin, "repo", "edit", info.get("nameWithOwner") or full_name,
                        "--visibility", vis_edit],
                       cwd=dest, env=env, capture_output=True, text=True, timeout=15)
    else:
        repo_desc = "pixos permanent repo" if permanent else f"pixos ephemeral share (TTL {ttl_min}m)"
        r = subprocess.run([gh_bin, "repo", "create", full_name, vis_flag,
                            "--description", repo_desc],
                           cwd=dest, env=env, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            err_msg = (r.stderr or r.stdout).strip()
            # Fallback to an existing live scratch repo if GitHub throttles repo creation
            reusable = _find_reusable_gh_repo(_load_registry(), owner) if force else None
            if "too many repositories" in err_msg.lower() and reusable:
                slug, nwo, repo_url = reusable
                info = {"url": repo_url, "nameWithOwner": nwo}
            else:
                shutil.rmtree(dest, ignore_errors=True)
                raise RuntimeError(f"gh repo create failed: {err_msg}")
        else:
            created_new = True
            # gh repo create prints the canonical https://github.com/<owner>/<repo> URL;
            # trust it over a fabricated full_name when view lags or owner mismatches.
            created_url = ""
            for ln in (r.stdout or "").splitlines():
                ln = ln.strip()
                if ln.startswith("https://github.com/"):
                    created_url = ln.rstrip("/")
                    break
            actual_nwo = created_url[len("https://github.com/"):] if created_url else full_name
            view = subprocess.run([gh_bin, "repo", "view", actual_nwo, "--json", "url,nameWithOwner"],
                                  cwd=dest, env=env, capture_output=True, text=True, timeout=15)
            info = json.loads(view.stdout) if view.returncode == 0 else {
                "url": created_url or f"https://github.com/{actual_nwo}",
                "nameWithOwner": actual_nwo,
            }

    repo_url = info.get("url") or f"https://github.com/{full_name}"
    nwo = info.get("nameWithOwner") or full_name

    # Resolve the actual commit SHA in `dest` (works for bare and non-bare repos)
    rev = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                         cwd=dest, capture_output=True, text=True)
    if rev.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"temp snapshot at {dest} has no commits (HEAD invalid)")
    commit_sha = rev.stdout.strip()

    # Hermetic auth using GH_TOKEN from `pix`
    import base64
    import time as _time
    tok = env.get("GH_TOKEN", "")
    basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode("ascii")
    push_cmd = ["git", "-c", "credential.helper=",
                "-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}",
                "push", "--no-verify"]
    if force:
        push_cmd.append("--force")
    push_cmd += [f"{repo_url}.git", f"{commit_sha}:refs/heads/main"]
    push = None
    for attempt in range(4):
        push = subprocess.run(push_cmd, cwd=dest, env=env,
                              capture_output=True, text=True, timeout=30)
        if push.returncode == 0:
            break
        err_try = (push.stderr or push.stdout).lower()
        if ("repository not found" in err_try or "not found" in err_try) and attempt < 3:
            _time.sleep(1.5)
            continue
        break
    if push is None or push.returncode != 0:
        err = (push.stderr or push.stdout).strip() if push else "no push attempted"
        if created_new:
            delete_gh_repo(nwo, owner)
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"git push failed: {err}")

    # Confirm the repo and `main` branch are live on GitHub before returning URL
    for _ in range(4):
        chk = subprocess.run([gh_bin, "api", f"repos/{nwo}/branches/main"],
                             env=env, capture_output=True, text=True, timeout=10)
        if chk.returncode == 0:
            break
        _time.sleep(1.0)
    else:
        raise RuntimeError(f"pushed to {repo_url}, but GitHub API still returns 404 for {nwo}/branches/main")

    expires = None if permanent else (datetime.now(timezone.utc) + timedelta(minutes=ttl_min)).isoformat()
    reg = _load_registry()
    reg[slug] = {"kind": "gh", "repo": nwo, "url": repo_url, "path": str(dest),
                 "account": owner, "author": author_str,
                 "visibility": "private" if private else "public",
                 "permanent": bool(permanent), "group": group,
                 "files": list(expanded_files), "transport": "github", "expires": expires,
                 "status": "live", "confirmed_at": datetime.now(timezone.utc).isoformat()}
    _save_registry(reg)
    record_index(slug, "gh", str(base), files, list(expanded_files) or [repo_url], str(dest))
    return {"slug": slug, "repo": nwo, "url": repo_url, "expires": expires,
            "account": owner, "author": author_str, "permanent": bool(permanent),
            "group": group}


def do_gh_share(args: argparse.Namespace) -> int:
    """Create a temp or permanent GitHub repo — paste-ready https:// URL for Import dialogs."""
    base = Path(getattr(args, "base", None) or os.getcwd()).resolve()
    ttl_min = getattr(args, "ttl", 60) or 60
    # Default to a rolling scratch repo name so repeated shares force-push without hitting GH repo creation rate limits
    slug = getattr(args, "name", None) or "pix-scratch"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug).strip("-") or "pix-tmp"
    is_private = bool(getattr(args, "private", False)) and not bool(getattr(args, "public", False))
    is_perm = bool(getattr(args, "permanent", False))
    try:
        res = create_temp_gh_repo(getattr(args, "files", None) or [], base, slug,
                                  ttl_min=ttl_min, account=getattr(args, "account", None),
                                  private=is_private, permanent=is_perm,
                                  force=not getattr(args, "no_force", False))
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = make_agent_envelope(slug, "gh", getattr(args, "files", None) or [], None,
                              0, "github", res["expires"],
                              extra={"repo": res["repo"], "url": res["url"],
                                     "bundle_exists": True,
                                     "clone_or_url": res["url"] + ".git"})
    if getattr(args, "json", False) or not sys.stdout.isatty():
        print(json.dumps(out, indent=2))
    else:
        mode_lbl = "permanent" if is_perm else "temp"
        print(f"{mode_lbl} {'private' if is_private else 'public'} github repo {res['repo']}")
        print(f"  url: {res['url']}")
    return 0


def register_gh_parsers(subparsers) -> None:
    p_gh = subparsers.add_parser("share-gh", help="Create temp or permanent GitHub repo for Import-dialog URLs")
    p_gh.add_argument("files", nargs="*", help="files to include (default: whole tree minus .git)")
    p_gh.add_argument("--base", default=None)
    p_gh.add_argument("--name", default=None, help="repo slug (default: pix-scratch rolling)")
    p_gh.add_argument("--ttl", type=int, default=60, help="time-to-live in minutes (ignored with --permanent)")
    p_gh.add_argument("--permanent", action="store_true", help="keep repo permanently (exempt from TTL GC)")
    p_gh.add_argument("--account", default=None, help="pix gh account (default: active)")
    p_gh.add_argument("--public", action="store_true", help="create as public repo (default)")
    p_gh.add_argument("--private", action="store_true", help="create as private repo instead of public")
    p_gh.add_argument("--no-force", action="store_true", help="fail instead of force-pushing existing slug")
    p_gh.set_defaults(_share_action="gh")

    p_prom = subparsers.add_parser("share-gh-promote", help="Promote an ephemeral gh repo to permanent")
    p_prom.add_argument("slug", help="share slug in registry")
    p_prom.set_defaults(_share_action="gh-promote")

    p_dem = subparsers.add_parser("share-gh-demote", help="Demote a permanent gh repo back to ephemeral TTL")
    p_dem.add_argument("slug", help="share slug in registry")
    p_dem.add_argument("--ttl", type=int, default=60, help="new TTL in minutes (default: 60)")
    p_dem.set_defaults(_share_action="gh-demote")


def dispatch_gh(args: argparse.Namespace):
    act = getattr(args, "_share_action", None)
    if act == "gh":
        return do_gh_share(args)
    if act == "gh-promote":
        res = set_gh_repo_permanence(args.slug, permanent=True)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 2
    if act == "gh-demote":
        res = set_gh_repo_permanence(args.slug, permanent=False, ttl_min=getattr(args, "ttl", 60))
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 2
    return None

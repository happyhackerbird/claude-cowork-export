#!/usr/bin/env python3
"""Export Claude Cowork sessions (transcripts + files) for one project/space into a .zip.

Works on macOS, Windows and Linux. No third-party dependencies.

Usage:
  cowork_export.py --list                       # show spaces/projects and session counts
  cowork_export.py --space self                 # export all sessions in space "self"
  cowork_export.py --space 019dfd70-...         # by project uuid or space id
  cowork_export.py --unassigned                 # sessions not attached to any space
  cowork_export.py --all                        # everything
  cowork_export.py --space self --include-mounted   # + files Claude wrote into your mounted folders
  cowork_export.py --space self --full              # everything verbatim, nothing scrubbed
  cowork_export.py --space self --no-thinking --max-tool-chars 3000   # lighter transcript.md

Each session folder in the zip holds: transcript.md (readable), session.json (metadata),
raw/ (original .jsonl transcripts + subagents + audit log), outputs/ (files Claude produced
in its session folder), uploads/ (files you attached), pasted-images/ (images pasted into
chat), and with --include-mounted also mounted-files/ (current on-disk copies of files
Claude wrote or edited in the folders you mounted, with a MANIFEST.tsv of original paths).

Not included: claude.ai artifacts / Claude Docs pages (cloud-hosted; links are in the
transcript) and files inside the Cowork VM scratch dir that were never saved to a mount.

By default the per-session metadata is scrubbed of account email/name, system prompts
and MCP server configs before it goes into the zip. Pass --no-scrub to keep them.
Windows: run with  py cowork_export.py ...  (Python 3.9+, no extra packages).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 9):
    sys.exit("cowork_export.py needs Python 3.9 or newer; this is Python %s" % sys.version.split()[0])

import argparse
import datetime as dt
import glob
import json
import os
import re
import zipfile
from pathlib import Path

SCRUB_KEYS = {
    "emailAddress", "accountName", "systemPrompt", "remoteMcpServersConfig",
    "spSectionPrompts", "coworkSyspromptMap", "systemPromptRendererAppends",
    "memoryGuidelinesTemplate", "orgCliExecPolicies",
}
SKIP_RECORD_TYPES = {"queue-operation", "summary", "progress", "file-history-snapshot", "system"}


# ---------------------------------------------------------------- discovery

def default_base() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude"


def lp(p) -> str:
    """Long-path-safe string for Windows (\\\\?\\ prefix beyond ~240 chars); unchanged elsewhere."""
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        s = os.path.abspath(s)
        s = "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


def load_json(p: Path, default=None):
    try:
        with open(lp(p), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def discover(base: Path):
    """Return (spaces, sessions). spaces: {space_id: {...}}; sessions: list of dicts."""
    root = base / "local-agent-mode-sessions"
    if not root.is_dir():
        sys.exit(f"Not found: {root}\nPass --base to point at your Claude app-data folder.")
    spaces: dict[str, dict] = {}
    sessions: list[dict] = []
    for account_dir in root.glob("*/*"):
        if not account_dir.is_dir() or "skills-plugin" in account_dir.parts:
            continue
        sp = load_json(account_dir / "spaces.json", {}) or {}
        for s in sp.get("spaces", []):
            s = dict(s)
            s["_account_dir"] = str(account_dir)
            spaces[s["id"]] = s
        for meta_path in sorted(account_dir.glob("local_*.json")):
            meta = load_json(meta_path)
            if not isinstance(meta, dict) or "sessionId" not in meta:
                continue
            folder = meta_path.with_suffix("")
            sessions.append({
                "meta": meta,
                "meta_path": meta_path,
                "folder": folder if folder.is_dir() else None,
                "space_id": meta.get("spaceId"),
            })
    return spaces, sessions


def resolve_space(spaces: dict, query: str) -> dict | None:
    q = query.strip().lower()
    for s in spaces.values():
        if s["id"].lower() == q or (s.get("name") or "").lower() == q:
            return s
        for p in s.get("projects", []):
            if (p.get("uuid") or "").lower() == q:
                return s
        if (s.get("migration") or {}).get("projectUuid", "").lower() == q:
            return s
    return None


# ---------------------------------------------------------------- rendering

def ts_of(rec: dict) -> str:
    return rec.get("timestamp") or rec.get("_audit_timestamp") or ""


def fmt_ms(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ms)


def slug(s: str, n: int = 48) -> str:
    s = re.sub(r"[^\w\s-]", "", s or "", flags=re.U).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return (s[:n].rstrip("-")) or "untitled"


def trunc(s: str, n: int) -> str:
    if n and len(s) > n:
        return s[:n] + f"\n… [{len(s) - n} more chars truncated]"
    return s


def fence(s: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in s:
        ticks += "`"
    return f"{ticks}{lang}\n{s}\n{ticks}"


def render_content(content, max_tool: int, thinking: bool) -> list[str]:
    out: list[str] = []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return [fence(json.dumps(content, ensure_ascii=False, indent=2))]
    for block in content:
        if not isinstance(block, dict):
            out.append(str(block))
            continue
        t = block.get("type")
        if t == "text":
            out.append(block.get("text", ""))
        elif t == "thinking":
            if thinking and block.get("thinking"):
                out.append("<details><summary>thinking</summary>\n\n" + block["thinking"] + "\n\n</details>")
        elif t == "tool_use":
            inp = json.dumps(block.get("input", {}), ensure_ascii=False, indent=2)
            out.append(f"🔧 **{block.get('name')}**\n\n" + fence(trunc(inp, max_tool), "json"))
        elif t == "tool_result":
            c = block.get("content")
            if isinstance(c, list):
                parts = []
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif isinstance(b, dict) and b.get("type") == "image":
                        parts.append("[image]")
                    else:
                        parts.append(json.dumps(b, ensure_ascii=False))
                c = "\n".join(parts)
            elif not isinstance(c, str):
                c = json.dumps(c, ensure_ascii=False, indent=2)
            flag = " (error)" if block.get("is_error") else ""
            out.append(f"↩️ **tool result{flag}**\n\n" + fence(trunc(c or "", max_tool)))
        elif t == "image":
            out.append("[image attached]")
        else:
            out.append(fence(json.dumps(block, ensure_ascii=False, indent=2), "json"))
    return out


def read_jsonl(p: Path) -> list[dict]:
    recs = []
    try:
        with open(lp(p), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return recs


def transcript_sources(folder: Path) -> list[Path]:
    """Top-level Claude Code transcripts (one per run/resume), oldest first."""
    files = list(Path(lp(folder)).glob(".claude/projects/*/*.jsonl"))
    def first_ts(p: Path) -> str:
        for r in read_jsonl(p)[:20]:
            if ts_of(r):
                return ts_of(r)
        return ""
    return sorted(files, key=first_ts)


WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
VM_MNT = re.compile(r"^/sessions/[^/]+/mnt/([^/]+)(/.*)?$")


def resolve_vm_path(p: str, meta: dict) -> Path | None:
    """Map a path seen in the transcript to a real path on this machine, or None."""
    if not p:
        return None
    norm = p.replace("\\", "/")
    m = VM_MNT.match(norm)
    if m:
        name, rest = m.group(1), m.group(2) or ""
        if name in ("outputs", "uploads"):
            return None  # already exported from the session folder
        mounts = meta.get("folderMountNames") or {}
        for real, alias in (mounts.items() if isinstance(mounts, dict) else []):
            if alias == name:
                return Path(real + rest)
        for real in meta.get("userSelectedFolders") or []:
            real = real.get("path") if isinstance(real, dict) else real
            if real and Path(real).name == name:
                return Path(real + rest)
        return None
    if norm.startswith("/sessions/"):
        return None  # scratch inside the VM, gone
    if "local-agent-mode-sessions" in norm and ("/outputs/" in norm or "/uploads/" in norm):
        return None
    return Path(p)


def written_files(recs: list[dict], meta: dict) -> list[tuple[Path, Path]]:
    """(real_path, zip_relative) for every file Claude wrote/edited that still exists."""
    seen: dict[Path, Path] = {}
    for r in recs:
        if r.get("type") != "assistant":
            continue
        for b in (r.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in WRITE_TOOLS:
                raw = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path") or ""
                real = resolve_vm_path(raw, meta)
                if real and real not in seen and Path(lp(real)).is_file():
                    anchor = None
                    sp = re.search(r"[/\\]spaces[/\\][0-9a-f-]{36}[/\\](.+)$", str(real))
                    if sp:
                        anchor = Path("project-memory") / Path(sp.group(1))
                    for root in ([] if anchor else meta.get("userSelectedFolders") or []):
                        root = root.get("path") if isinstance(root, dict) else root
                        try:
                            if root:
                                anchor = Path(Path(root).name) / real.relative_to(root)
                                break
                        except ValueError:
                            pass
                    seen[real] = anchor or Path(*[x for x in real.parts if x not in ("/", "\\")][-3:])
    return sorted(seen.items())


def pasted_images(recs: list[dict]) -> list[tuple[str, bytes]]:
    import base64
    out = []
    for r in recs:
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if isinstance(b, dict) and b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
                src = b["source"]
                ext = (src.get("media_type") or "image/png").split("/")[-1].replace("jpeg", "jpg")
                try:
                    data = base64.b64decode(src.get("data", ""))
                except Exception:
                    continue
                ts = re.sub(r"[^\d]", "", ts_of(r))[:14] or f"{len(out):03d}"
                out.append((f"{ts}_{len(out) + 1:02d}.{ext}", data))
    return out


def render_markdown(sess: dict, space_name: str, max_tool: int, thinking: bool) -> tuple[str, list[Path], list[dict]]:
    meta, folder = sess["meta"], sess["folder"]
    lines = [f"# {meta.get('title') or 'Untitled session'}", ""]
    lines += [
        f"- **Session:** `{meta.get('sessionId')}`",
        f"- **Space / project:** {space_name}",
        f"- **Created:** {fmt_ms(meta.get('createdAt'))}",
        f"- **Last activity:** {fmt_ms(meta.get('lastActivityAt'))}",
        f"- **Model:** {meta.get('model')}",
    ]
    folders = [f.get("path") if isinstance(f, dict) else str(f) for f in meta.get("userSelectedFolders") or []]
    if folders:
        lines.append("- **Mounted folders:** " + ", ".join(f"`{p}`" for p in folders))
    if meta.get("isArchived"):
        lines.append("- **Archived:** yes")
    lines.append("")

    sources: list[Path] = []
    recs: list[dict] = []
    if folder:
        sources = transcript_sources(folder)
        for p in sources:
            recs += read_jsonl(p)
        if not recs and (folder / "audit.jsonl").exists():
            recs = read_jsonl(folder / "audit.jsonl")
    if not recs:
        lines.append("_No transcript found on disk for this session._")
        if meta.get("initialMessage"):
            lines += ["", "**Initial message:**", "", str(meta["initialMessage"])]
        return "\n".join(lines) + "\n", sources, recs

    seen = set()
    for r in recs:
        t = r.get("type")
        if t in SKIP_RECORD_TYPES or t not in ("user", "assistant"):
            continue
        key = r.get("uuid")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        msg = r.get("message") or {}
        content = msg.get("content")
        if content is None:
            continue
        body = [b for b in render_content(content, max_tool, thinking) if b.strip()]
        if not body:
            continue
        who = "👤 User" if t == "user" else "🤖 Claude"
        ts = ts_of(r)
        lines.append(f"---\n\n### {who}" + (f"  <sub>{ts}</sub>" if ts else ""))
        lines.append("")
        lines += body
        lines.append("")
    return "\n".join(lines) + "\n", sources, recs


# ---------------------------------------------------------------- export

JUNK_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".git"}


def is_hidden(rel: Path) -> bool:
    return any(part.startswith(".") or part in JUNK_DIRS for part in rel.parts)


def add_tree(zf: zipfile.ZipFile, src: Path, dest_prefix: str, skip_hidden: bool = True) -> int:
    n = 0
    src = Path(lp(src))
    if not src.is_dir():
        return 0
    for p in src.rglob("*"):
        if p.is_file():
            rel = p.relative_to(src)
            if skip_hidden and is_hidden(rel):
                continue
            zf.write(lp(p), f"{dest_prefix}/{rel.as_posix()}")
            n += 1
    return n


def export(selected: list[dict], spaces: dict, label: str, out: Path, scrub: bool, max_tool: int, thinking: bool,
           include_mounted: bool, full: bool = False):
    out.parent.mkdir(parents=True, exist_ok=True)
    index = ["# Cowork export: " + label, "", f"Exported {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}; {len(selected)} session(s).", "",
             "| Created | Title | Folder | Outputs | Uploads | Images | Mounted files |", "|---|---|---|---|---|---|---|"]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in sorted(selected, key=lambda x: x["meta"].get("createdAt") or 0):
            meta, folder = s["meta"], s["folder"]
            sid = re.sub(r"^local_", "", meta.get("sessionId", ""))
            created = fmt_ms(meta.get("createdAt"))[:10]
            dirname = f"{created}_{slug(meta.get('title'))}_{sid[:8]}"
            space = spaces.get(s["space_id"] or "")
            space_name = (space or {}).get("name") or ("(unassigned)" if not s["space_id"] else s["space_id"])

            md, sources, recs = render_markdown(s, space_name, max_tool, thinking)
            zf.writestr(f"{dirname}/transcript.md", md)

            m = {k: v for k, v in meta.items() if not (scrub and k in SCRUB_KEYS)}
            zf.writestr(f"{dirname}/session.json", json.dumps(m, ensure_ascii=False, indent=2))

            n_out = n_up = n_img = n_mnt = 0
            if folder:
                for p in sources:
                    zf.write(lp(p), f"{dirname}/raw/{p.name}")
                    sub = p.with_suffix("") / "subagents"
                    add_tree(zf, sub, f"{dirname}/raw/{p.stem}/subagents")
                audit = folder / "audit.jsonl"
                if Path(lp(audit)).exists():
                    zf.write(lp(audit), f"{dirname}/raw/audit.jsonl")
                n_out = add_tree(zf, folder / "outputs", f"{dirname}/outputs")
                n_up = add_tree(zf, folder / "uploads", f"{dirname}/uploads")
                if full:  # verbatim copy of the whole session folder, hidden files and all
                    add_tree(zf, folder, f"{dirname}/session-folder", skip_hidden=False)
                    zf.write(lp(s["meta_path"]), f"{dirname}/session-folder.json")
            for name, data in pasted_images(recs):
                zf.writestr(f"{dirname}/pasted-images/{name}", data)
                n_img += 1
            if include_mounted:
                manifest = []
                for real, rel in written_files(recs, meta):
                    zf.write(lp(real), f"{dirname}/mounted-files/{rel.as_posix()}")
                    manifest.append(f"{rel.as_posix()}\t{real}")
                    n_mnt += 1
                if manifest:
                    zf.writestr(f"{dirname}/mounted-files/MANIFEST.tsv", "zip_path\toriginal_path\n" + "\n".join(manifest) + "\n")
            index.append(f"| {created} | {meta.get('title') or ''} | `{dirname}/` | {n_out} | {n_up} | {n_img} | {n_mnt} |")
            print(f"  + {dirname}  (outputs {n_out}, uploads {n_up}, images {n_img}, mounted {n_mnt}, transcripts {len(sources)})")
        zf.writestr("index.md", "\n".join(index) + "\n")
        if label != "all":
            related = [v for v in spaces.values() if v.get("name") == label or v["id"] == label]
            if related:
                zf.writestr("space.json", json.dumps([{k: v for k, v in r.items() if not k.startswith("_")} for r in related], indent=2))
    print(f"\nWrote {out}  ({out.stat().st_size / 1e6:.1f} MB)")


# ---------------------------------------------------------------- cli

def main():
    for stream in (sys.stdout, sys.stderr):  # Windows cp1252 consoles/redirects choke on emoji in titles
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, default=default_base(), help="Claude app-data folder (auto-detected)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list spaces/projects and sessions")
    g.add_argument("--space", help="space name, space id, or project uuid")
    g.add_argument("--unassigned", action="store_true", help="sessions with no space")
    g.add_argument("--all", action="store_true", help="every session")
    ap.add_argument("--out", type=Path, help="output .zip path")
    ap.add_argument("--skip-archived", action="store_true")
    ap.add_argument("--no-scrub", action="store_true", help="keep email, system prompts, MCP configs in session.json")
    ap.add_argument("--no-thinking", action="store_true", help="omit thinking blocks from transcript.md (included by default)")
    ap.add_argument("--max-tool-chars", type=int, default=0,
                    help="trim tool inputs/results in transcript.md to N chars for readability (default 0 = never trim; raw/ is always complete)")
    ap.add_argument("--include-mounted", action="store_true",
                    help="also copy files Claude wrote/edited inside your mounted folders (current on-disk version)")
    ap.add_argument("--full", action="store_true",
                    help="everything, verbatim: no scrubbing, no truncation, thinking included, mounted files, "
                         "plus a byte-for-byte copy of each session folder (hidden files, node_modules, keys)")
    a = ap.parse_args()
    if a.full:
        a.no_scrub = a.include_mounted = True
        a.no_thinking = False
        a.max_tool_chars = 0

    spaces, sessions = discover(a.base)

    if a.list or not (a.space or a.unassigned or a.all):
        print(f"Base: {a.base}\n")
        counts: dict[str | None, int] = {}
        for s in sessions:
            counts[s["space_id"]] = counts.get(s["space_id"], 0) + 1
        print("Spaces / projects:")
        for sp in spaces.values():
            projs = ", ".join(p.get("uuid", "") for p in sp.get("projects", [])) or "-"
            print(f"  {sp.get('name')!r:30} space={sp['id']}  project={projs}  sessions={counts.get(sp['id'], 0)}")
        print(f"  {'(unassigned)':30} sessions={counts.get(None, 0)}\n")
        print("Sessions:")
        for s in sorted(sessions, key=lambda x: x["meta"].get("createdAt") or 0):
            m = s["meta"]
            sp = spaces.get(s["space_id"] or "", {}).get("name") or "-"
            flag = " [archived]" if m.get("isArchived") else ""
            print(f"  {fmt_ms(m.get('createdAt'))}  {sp:12.12}  {m.get('title') or '(untitled)'}{flag}")
        return

    if a.all:
        selected, label = sessions, "all"
    elif a.unassigned:
        selected, label = [s for s in sessions if not s["space_id"]], "unassigned"
    else:
        sp = resolve_space(spaces, a.space)
        if not sp:
            sys.exit(f"No space/project matches {a.space!r}. Run with --list to see names.")
        selected, label = [s for s in sessions if s["space_id"] == sp["id"]], sp.get("name") or sp["id"]
    if a.skip_archived:
        selected = [s for s in selected if not s["meta"].get("isArchived")]
    if not selected:
        sys.exit("No sessions matched.")

    out = a.out or Path.cwd() / f"cowork-export-{slug(label)}-{dt.date.today():%Y%m%d}.zip"
    print(f"Exporting {len(selected)} session(s) for {label!r} → {out}")
    export(selected, spaces, label, out, scrub=not a.no_scrub, max_tool=a.max_tool_chars, thinking=not a.no_thinking,
           include_mounted=a.include_mounted, full=a.full)


if __name__ == "__main__":
    main()

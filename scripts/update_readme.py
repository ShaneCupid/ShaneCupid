#!/usr/bin/env python3
"""Rewrite marker-delimited sections of README.md from GitHub data.

Only content between <!-- AIEYU:<NAME>:START --> and <!-- AIEYU:<NAME>:END -->
is touched. Static copy is never modified. Exits non-zero on API failure so the
workflow does not commit a half-rendered README.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CONFIG = json.loads((ROOT / "profile.config.json").read_text())
TOKEN = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN")
API = "https://api.github.com"


def gh(path: str):
    req = urllib.request.Request(f"{API}{path}", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profile-readme-updater",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def esc(s: str) -> str:
    return s.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def building_block() -> str:
    cfg = CONFIG["building"]
    since = (datetime.now(timezone.utc) - timedelta(days=cfg["lookback_days"])).isoformat()
    rows = []
    for repo in CONFIG["repos"]:
        try:
            commits = gh(f"/repos/{repo}/commits?since={since}&per_page={cfg['max_commits']}")
        except Exception as e:  # noqa: BLE001
            print(f"warn: {repo}: {e}", file=sys.stderr)
            continue
        for c in commits:
            msg = c["commit"]["message"].splitlines()[0][:80]
            date = c["commit"]["author"]["date"][:10]
            rows.append((date, repo, msg, c["html_url"]))
    if not rows:
        return "_Quiet fortnight. Check the repos above._"
    rows.sort(reverse=True)
    lines = ["| Date | Repo | Commit |", "|---|---|---|"]
    for date, repo, msg, url in rows[: cfg["max_commits"]]:
        name = repo.split("/")[-1]
        lines.append(f"| {date} | `{name}` | [{esc(msg)}]({url}) |")
    return "\n".join(lines)


def releases_block() -> str:
    cfg = CONFIG["releases"]
    items = []
    for repo in CONFIG["repos"]:
        try:
            rels = gh(f"/repos/{repo}/releases?per_page={cfg['max_releases']}")
        except Exception as e:  # noqa: BLE001
            print(f"warn: {repo}: {e}", file=sys.stderr)
            continue
        for r in rels:
            if r.get("draft"):
                continue
            items.append((r["published_at"] or "", repo.split("/")[-1], r["name"] or r["tag_name"], r["html_url"]))
    if not items:
        return "_No published releases yet._"
    items.sort(reverse=True)
    return "\n".join(
        f"- **{esc(name)}** · `{repo}` · [{ts[:10]}]({url})" for ts, repo, name, url in items[: cfg["max_releases"]]
    )


def feed_block() -> str | None:
    cfg = CONFIG.get("feed", {})
    if not cfg.get("enabled"):
        return None
    with urllib.request.urlopen(cfg["url"], timeout=20) as r:
        tree = ET.fromstring(r.read())
    entries = tree.findall(".//item")[: cfg["max_items"]]
    return "\n".join(f"- [{esc(e.findtext('title', ''))}]({e.findtext('link', '')})" for e in entries)


def replace(text: str, name: str, body: str) -> str:
    pattern = re.compile(rf"(<!-- AIEYU:{name}:START -->)(.*?)(<!-- AIEYU:{name}:END -->)", re.S)
    if not pattern.search(text):
        raise SystemExit(f"marker {name} missing from README")
    return pattern.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", text)


def main() -> None:
    if not TOKEN:
        raise SystemExit("no token in PROFILE_TOKEN or GITHUB_TOKEN")
    text = README.read_text()
    text = replace(text, "BUILDING", building_block())
    text = replace(text, "RELEASES", releases_block())
    feed = feed_block()
    if feed is not None:
        text = replace(text, "FEED", feed)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = replace(text, "UPDATED", f"<sub>Auto-updated {stamp}</sub>")
    README.write_text(text)


if __name__ == "__main__":
    main()

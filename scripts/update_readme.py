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


def fetch_commits(repo: str, since: str) -> list:
    """All commits since `since`, paginated (100/page, max 10 pages)."""
    out = []
    for page in range(1, 11):
        batch = gh(f"/repos/{repo}/commits?since={since}&per_page=100&page={page}")
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def commits_svg(days: list[str], per_repo: dict[str, dict[str, int]]) -> str:
    """Stacked daily commit bars, one color per repo. Colors from config."""
    colors = CONFIG["building"].get("colors", [])
    W, H = 1280, 260
    pad_l, pad_r, pad_t, pad_b = 40, 40, 40, 50
    n = len(days)
    slot = (W - pad_l - pad_r) / n
    bar_w = slot * 0.62
    totals = [sum(per_repo[r].get(d, 0) for r in per_repo) for d in days]
    peak = max(totals) or 1
    scale = (H - pad_t - pad_b) / peak
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Commits per day, last {n} days">',
        "<style>text{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;fill:#6b7280}"
        "@media(prefers-color-scheme:dark){text{fill:#9ca3af}}</style>",
    ]
    for i, d in enumerate(days):
        x = pad_l + i * slot + (slot - bar_w) / 2
        y = H - pad_b
        for j, repo in enumerate(per_repo):
            c = per_repo[repo].get(d, 0)
            if not c:
                continue
            h = c * scale
            y -= h
            fill = colors[j % len(colors)] if colors else "#888888"
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="3" fill="{fill}"/>')
        if totals[i]:
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{y - 8:.1f}" font-size="14" text-anchor="middle">{totals[i]}</text>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{H - pad_b + 22}" font-size="13" text-anchor="middle">{d[5:]}</text>')
    lx = pad_l
    for j, repo in enumerate(per_repo):
        fill = colors[j % len(colors)] if colors else "#888888"
        parts.append(f'<rect x="{lx}" y="12" width="14" height="14" rx="3" fill="{fill}"/>')
        parts.append(f'<text x="{lx + 20}" y="24" font-size="14">{esc(repo.split("/")[-1])}</text>')
        lx += 20 + 9 * len(repo.split("/")[-1]) + 30
    parts.append("</svg>")
    return "\n".join(parts)


def building_block() -> str:
    """Commit chart plus one summary line per repo. No messages, no links."""
    cfg = CONFIG["building"]
    lookback = cfg["lookback_days"]
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(lookback - 1, -1, -1)]
    since = f"{days[0]}T00:00:00Z"
    per_repo: dict[str, dict[str, int]] = {}
    lines = []
    for repo in CONFIG["repos"]:
        try:
            commits = fetch_commits(repo, since)
        except Exception as e:  # noqa: BLE001
            print(f"warn: {repo}: {e}", file=sys.stderr)
            continue
        counts: dict[str, int] = {}
        for c in commits:
            counts[c["commit"]["author"]["date"][:10]] = counts.get(c["commit"]["author"]["date"][:10], 0) + 1
        per_repo[repo] = counts
        if commits:
            n = len(commits)
            last = max(counts)
            lines.append(f"- **{repo.split('/')[-1]}** · {n} commit{'s' if n != 1 else ''} in the last {lookback} days · last push {last}")
    (ROOT / "assets").mkdir(exist_ok=True)
    (ROOT / "assets" / "commits.svg").write_text(commits_svg(days, per_repo))
    body = ['<img src="./assets/commits.svg" alt="Commits per day across AIEYU repos" width="100%">', ""]
    body += lines or ["_Quiet fortnight._"]
    return "\n".join(body)


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

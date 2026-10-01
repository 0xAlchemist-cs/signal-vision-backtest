# -*- coding: utf-8 -*-
"""Fetch full message history of a Discord channel (text + image URLs) into JSONL.

Self-bot style REST paging with your own user token. ToS caveat: user-account
automation violates Discord ToS -- use a bot account where possible.

Security: https + api/discord.com allowlist + private-IP resolution block.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED_HOSTS = {"discord.com"}
_CDN_ALLOW = "cdn.discordapp.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _guard(url: str, hosts) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.hostname not in hosts:
        raise ValueError(f"blocked url: {url}")
    for _, _, _, _, sockaddr in socket.getaddrinfo(p.hostname, 443):
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.version == 4:
            bad = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        else:
            bad = ip.is_loopback or ip.is_link_local or (ip in ipaddress.ip_network("fc00::/7"))
        if bad:
            raise ValueError(f"resolved to private ip: {ip}")


def _get(url: str, token: str):
    _guard(url, ALLOWED_HOSTS)
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": token,
                "User-Agent": "Mozilla/5.0",
            })
            with _opener.open(req, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                raise ValueError("redirect blocked")
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After", "2")) + 0.5)
                continue
            raise
        except Exception:
            if attempt == 4:
                raise
            time.sleep(3)
    return []


def fetch_channel(channel_id: str, out_path: Path, token: str | None = None) -> int:
    token = token or os.environ.get("DISCORD_TOKEN", "")
    assert token, "set DISCORD_TOKEN"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n, before = 0, None
    with out_path.open("w", encoding="utf-8") as f:
        while True:
            url = (f"https://discord.com/api/v10/channels/{channel_id}"
                   f"/messages?limit=100" + (f"&before={before}" if before else ""))
            batch = _get(url, token)
            if not batch:
                break
            for m in reversed(batch):
                slim = {
                    "id": m.get("id"), "ts": m.get("timestamp"),
                    "author": (m.get("author") or {}).get("username", ""),
                    "content": m.get("content") or "",
                    "attachments": [{"url": a.get("url"), "filename": a.get("filename")}
                                    for a in (m.get("attachments") or [])],
                }
                f.write(json.dumps(slim, ensure_ascii=False) + "\n")
                n += 1
            before = batch[-1]["id"]
            if len(batch) < 100:
                break
            time.sleep(0.4)
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("channel_id")
    ap.add_argument("out", help="output jsonl path")
    args = ap.parse_args()
    print(f"{fetch_channel(args.channel_id, Path(args.out))} messages -> {args.out}")

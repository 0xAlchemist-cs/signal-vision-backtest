# -*- coding: utf-8 -*-
"""Download chart images referenced in a fetched JSONL (cdn allowlist + resize)."""
from __future__ import annotations

import io
import ipaddress
import json
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_ALLOW = "cdn.discordapp.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _guard(url: str) -> None:
    p = urlparse(url)
    if p.scheme != "https" or p.hostname != _ALLOW:
        raise ValueError(f"blocked: {url}")
    for _, _, _, _, sockaddr in socket.getaddrinfo(p.hostname, 443):
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.version == 4:
            bad = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        else:
            bad = ip.is_loopback or ip.is_link_local or (ip in ipaddress.ip_network("fc00::/7"))
        if bad:
            raise ValueError(f"private ip: {ip}")


def _download(url: str, dest: Path, max_w: int = 1280) -> bool:
    _guard(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with _opener.open(req, timeout=30) as resp:
            data = resp.read()
    except Exception as e:
        print(f"  fail {url[:70]}: {e}")
        return False
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if img.width > max_w:
            img = img.resize((max_w, int(img.height * max_w / img.width)))
        img.save(dest, "JPEG", quality=82)
    except Exception:
        dest.write_bytes(data)
    return True


def download_images(jsonl_path: Path, images_dir: Path) -> int:
    """Download every attachment; Discord signed URLs expire, so fetch promptly."""
    images_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for line in jsonl_path.open(encoding="utf-8"):
        if not line.strip().endswith("}"):
            continue
        m = json.loads(line)
        for i, a in enumerate(m.get("attachments") or []):
            if not a.get("url"):
                continue
            dest = images_dir / f"{m['id']}_{i}.jpg"
            if dest.exists():
                continue
            if _download(a["url"], dest):
                n += 1
            time.sleep(0.1)
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("images_dir")
    args = ap.parse_args()
    print(f"{download_images(Path(args.jsonl), Path(args.images_dir))} images -> {args.images_dir}")

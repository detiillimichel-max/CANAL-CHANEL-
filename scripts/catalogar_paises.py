#!/usr/bin/env python3
"""Build country catalogues from the repository's public M3U playlists.

This script reads playlist metadata and stream URLs only. It does not download,
proxy, restream, or test playback. Pluto TV playlist files are intentionally
excluded until integration/redistribution terms are confirmed.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "catalogos"

COUNTRIES = {
    "br": {"name": "Brasil", "playlists": ["streams/br.m3u", "streams/br_samsung.m3u"]},
    "pt": {"name": "Portugal", "playlists": ["streams/pt.m3u", "streams/pt_samsung.m3u"]},
    "us": {"name": "Estados Unidos", "playlists": ["streams/us.m3u", "streams/us_samsung.m3u"]},
}

ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9_-]+)="((?:\\.|[^"])*)"')
RESOLUTION_RE = re.compile(r"\b(\d{3,4}p)\b", re.IGNORECASE)
FLAG_PATTERNS = {
    "not_24_7": re.compile(r"\[Not 24/7\]", re.IGNORECASE),
    "geo_blocked": re.compile(r"\[Geo-blocked\]", re.IGNORECASE),
}


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def parse_extinf(line: str) -> dict:
    payload = line[len("#EXTINF:"):]
    attrs = {key.lower(): value for key, value in ATTRIBUTE_RE.findall(payload)}
    display_name = payload.rsplit(",", 1)[-1].strip() if "," in payload else ""
    resolution_match = RESOLUTION_RE.search(display_name)
    flags = [name for name, pattern in FLAG_PATTERNS.items() if pattern.search(display_name)]
    clean_name = display_name
    clean_name = re.sub(r"\s*\(\d{3,4}p\)", "", clean_name, flags=re.IGNORECASE)
    for pattern in FLAG_PATTERNS.values():
        clean_name = pattern.sub("", clean_name)
    clean_name = re.sub(r"\s+", " ", clean_name).strip(" -")
    return {
        "tvg_id": attrs.get("tvg-id", "").strip(),
        "logo": attrs.get("tvg-logo", "").strip() or None,
        "group_title": attrs.get("group-title", "").strip() or None,
        "name": clean_name or display_name or "Canal sem nome",
        "resolution": resolution_match.group(1).lower() if resolution_match else None,
        "flags": flags,
    }


def parse_playlist(path: Path, country_code: str) -> list[dict]:
    current = None
    records = []
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            current = parse_extinf(line)
            continue
        if line.startswith("#"):
            # Ignore VLC options and other metadata; keep the pending EXTINF.
            continue
        if line.startswith(("http://", "https://")) and current:
            parsed = urlparse(line)
            records.append({
                **current,
                "stream_url": line,
                "protocol": parsed.scheme.lower(),
                "host": parsed.hostname,
                "source_file": path.relative_to(ROOT).as_posix(),
                "source_type": "samsung_tv_plus" if "_samsung" in path.stem else "community_playlist",
                "country": country_code,
                "authorization_status": "unknown",
                "availability_status": "not_checked",
            })
            current = None
    return records


def build_country_catalogue(code: str, config: dict) -> dict:
    grouped = OrderedDict()
    seen_urls = set()
    input_count = 0
    duplicate_url_count = 0

    for relative_path in config["playlists"]:
        path = ROOT / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"Playlist obrigatória não encontrada: {relative_path}")
        for record in parse_playlist(path, code):
            input_count += 1
            url = record["stream_url"]
            if url in seen_urls:
                duplicate_url_count += 1
                continue
            seen_urls.add(url)

            # Keep regional tvg-id variants separate. If tvg-id is absent,
            # group by a conservative normalized display name.
            channel_key = record["tvg_id"] or f"name:{normalize_name(record['name'])}"
            if channel_key not in grouped:
                grouped[channel_key] = {
                    "id": channel_key,
                    "name": record["name"],
                    "tvg_id": record["tvg_id"] or None,
                    "logo": record["logo"],
                    "group_title": record["group_title"],
                    "country": code,
                    "streams": [],
                }
            channel = grouped[channel_key]
            if record["name"] != channel["name"] and record["name"] not in channel.setdefault("alternate_names", []):
                channel["alternate_names"].append(record["name"])
            if not channel["logo"] and record["logo"]:
                channel["logo"] = record["logo"]
            if not channel["group_title"] and record["group_title"]:
                channel["group_title"] = record["group_title"]

            channel["streams"].append({
                "url": record["stream_url"],
                "protocol": record["protocol"],
                "host": record["host"],
                "resolution": record["resolution"],
                "flags": record["flags"],
                "source_file": record["source_file"],
                "source_type": record["source_type"],
                "authorization_status": record["authorization_status"],
                "availability_status": record["availability_status"],
            })

    channels = list(grouped.values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "country": {"code": code, "name": config["name"]},
        "source_playlists": config["playlists"],
        "summary": {
            "playlist_entries": input_count,
            "unique_stream_urls": len(seen_urls),
            "duplicate_stream_urls_removed": duplicate_url_count,
            "catalogue_channels": len(channels),
            "streams": sum(len(channel["streams"]) for channel in channels),
            "authorization": "not_verified",
            "playback": "not_tested",
        },
        "channels": channels,
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for code, config in COUNTRIES.items():
        catalogue = build_country_catalogue(code, config)
        output = OUTPUT_DIR / f"catalogo-{code}.json"
        output.write_text(
            json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary = catalogue["summary"]
        print(
            f"[OK] {output.relative_to(ROOT)}: "
            f"{summary['catalogue_channels']} canais, "
            f"{summary['streams']} streams"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        raise SystemExit(1)

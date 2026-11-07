#!/usr/bin/env python3
"""Enrich BR/PT/US channel catalogues with public metadata and app-ready cards.

Metadata source: iptv-org/api (channels.json, logos.json).
No stream URLs are modified, probed, proxied, or redistributed by this script.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_DIR = ROOT / "catalogos"
COUNTRIES = ("br", "pt", "us")
API_BASE = "https://iptv-org.github.io/api"
USER_AGENT = "CANAL-CHANEL-Metadata-Enricher/1.0"
TIMEOUT = 35
APP_OUTPUT = CATALOGUE_DIR / "app-catalogo-global.json"

CATEGORY_LABELS = {
    "news": "jornalismo", "sports": "esportes", "animation": "desenhos",
    "kids": "desenhos", "documentary": "documentarios", "movies": "filmes",
    "music": "musica", "entertainment": "entretenimento",
    "education": "educacao_cultura", "culture": "educacao_cultura",
    "religion": "religiao", "business": "negocios", "legislative": "politica",
    "outdoor": "lifestyle", "lifestyle": "lifestyle", "travel": "lifestyle",
    "cooking": "lifestyle", "auto": "lifestyle", "shop": "compras",
    "general": "geral", "xxx": "adulto",
}
CATEGORY_NAMES = {
    "jornalismo": "Jornalismo", "esportes": "Esportes", "desenhos": "Desenhos",
    "documentarios": "Documentários", "filmes": "Filmes", "musica": "Música",
    "entretenimento": "Entretenimento", "educacao_cultura": "Educação e cultura",
    "regionais": "Regionais", "religiao": "Religião", "agronegocio": "Agronegócio",
    "lifestyle": "Lifestyle", "negocios": "Negócios", "politica": "Política",
    "compras": "Compras", "geral": "Geral", "adulto": "Adulto", "outros": "Outros",
}
COUNTRY_NAMES = {"br": "Brasil", "pt": "Portugal", "us": "Estados Unidos"}
FORMAT_RANK = {"SVG": 6, "PNG": 5, "WEBP": 4, "AVIF": 4, "APNG": 3, "JPEG": 2, "GIF": 1}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def fetch_json(name: str):
    url = f"{API_BASE}/{name}.json"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"{name}.json respondeu HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def best_logo(logos: list[dict]) -> dict | None:
    candidates = [
        logo for logo in logos
        if logo.get("in_use") is True
        and isinstance(logo.get("url"), str)
        and logo["url"].startswith("https://")
    ]
    if not candidates:
        candidates = [
            logo for logo in logos
            if isinstance(logo.get("url"), str) and logo["url"].startswith("https://")
        ]
    if not candidates:
        return None
    def rank(logo):
        fmt = str(logo.get("format") or "").upper()
        width = int(logo.get("width") or 0)
        height = int(logo.get("height") or 0)
        return (
            1 if logo.get("in_use") is True else 0,
            FORMAT_RANK.get(fmt, 0),
            -abs(512 - width) - abs(512 - height),
        )
    return max(candidates, key=rank)


def build_indexes(channels: list[dict], logos: list[dict]):
    channel_by_id = {}
    channel_by_name = defaultdict(list)
    logos_by_id = defaultdict(list)
    for item in channels:
        code = str(item.get("country") or "").lower()
        if code not in COUNTRIES:
            continue
        channel_by_id[item.get("id")] = item
        for name in [item.get("name"), *(item.get("alt_names") or [])]:
            key = (code, normalize(name))
            if key[1]:
                channel_by_name[key].append(item)
    for logo in logos:
        channel_id = logo.get("channel")
        if channel_id:
            logos_by_id[channel_id].append(logo)
    return channel_by_id, channel_by_name, logos_by_id


def match_official(channel: dict, code: str, by_id: dict, by_name: dict):
    candidates = []
    for raw_id in (channel.get("tvg_id"), channel.get("id")):
        if not raw_id:
            continue
        candidates.append(str(raw_id).split("@", 1)[0])
    for candidate in candidates:
        official = by_id.get(candidate)
        if official and str(official.get("country", "")).lower() == code:
            return official, "channel_id"
    name = normalize(channel.get("name"))
    matches = by_name.get((code, name), [])
    if len(matches) == 1:
        return matches[0], "exact_name_country"
    return None, "unmatched" if not matches else "ambiguous_name"


def stream_state(streams: list[dict]) -> str:
    results = [s.get("validation", {}).get("result", s.get("availability_status")) for s in streams]
    if "reachable_hls" in results:
        return "hls_confirmed"
    if "reachable_non_hls" in results:
        return "http_reachable_hls_unconfirmed"
    if any(r in {"timeout", "connection_error", "dns_error", "server_error", "http_error", "rate_limited"} for r in results):
        return "temporary_failure_or_unavailable"
    if any(r in {"forbidden_or_geo_restricted", "geo_blocked_evidence", "legal_or_region_restriction", "authentication_required"} for r in results):
        return "restricted_or_auth_required"
    if any(r in {"not_found", "gone"} for r in results):
        return "no_confirmed_stream"
    return "not_checked"


def main() -> int:
    print("[INFO] Baixando metadados públicos do iptv-org...")
    official_channels = fetch_json("channels")
    official_logos = fetch_json("logos")
    by_id, by_name, logos_by_id = build_indexes(official_channels, official_logos)
    timestamp = utc_now()
    app_channels = []
    total = Counter()

    for code in COUNTRIES:
        path = CATALOGUE_DIR / f"catalogo-{code}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Catálogo não encontrado: {path.relative_to(ROOT)}")
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        category_counts = Counter()
        match_counts = Counter()
        logo_count = 0

        for channel in catalogue.get("channels", []):
            official, match_method = match_official(channel, code, by_id, by_name)
            if official:
                channel["official_metadata"] = {
                    "id": official.get("id"),
                    "name": official.get("name"),
                    "network": official.get("network"),
                    "website": official.get("website"),
                    "categories": official.get("categories") or [],
                    "matched_by": match_method,
                    "source": "iptv-org/api",
                }
                # Keep the playlist label for traceability and use the official
                # database name as the canonical display name when available.
                if official.get("name"):
                    if channel.get("name") and channel["name"] != official["name"]:
                        channel["playlist_name"] = channel["name"]
                    channel["name"] = official["name"]
                raw_categories = official.get("categories") or []
                mapped = sorted({
                    CATEGORY_LABELS[item]
                    for item in raw_categories
                    if item in CATEGORY_LABELS
                })
                if mapped:
                    channel["categories"] = mapped
                selected = best_logo(logos_by_id.get(official.get("id"), []))
                if selected:
                    channel["logo"] = selected["url"]
                    channel["logo_metadata"] = {
                        "source": "iptv-org/api/logos.json",
                        "official_channel_id": official.get("id"),
                        "format": selected.get("format"),
                        "width": selected.get("width"),
                        "height": selected.get("height"),
                        "in_use": selected.get("in_use"),
                    }
                    logo_count += 1
                match_counts[match_method] += 1
            else:
                match_counts[match_method] += 1

            if not channel.get("categories"):
                primary = channel.get("classification", {}).get("primary", "outros")
                channel["categories"] = [primary] if primary else ["outros"]
            primary = channel.get("classification", {}).get("primary")
            if primary and primary != "outros" and primary not in channel["categories"]:
                channel["categories"].append(primary)
            channel["categories"] = sorted(set(channel["categories"]))
            channel["category_labels"] = [
                CATEGORY_NAMES.get(category, category.replace("_", " ").title())
                for category in channel["categories"]
            ]

            state = stream_state(channel.get("streams", []))
            channel["playback_summary"] = {
                "status": state,
                "total_streams": len(channel.get("streams", [])),
                "hls_confirmed_streams": sum(
                    s.get("validation", {}).get("result", s.get("availability_status")) == "reachable_hls"
                    for s in channel.get("streams", [])
                ),
            }
            channel["search"] = {
                "normalized_name": normalize(channel.get("name")),
                "terms": sorted({
                    term for term in (
                        normalize(channel.get("name")),
                        normalize(channel.get("tvg_id")),
                        normalize(channel.get("official_metadata", {}).get("network")),
                        *[normalize(x) for x in channel.get("category_labels", [])],
                    ) if term
                }),
            }
            channel["favorite_key"] = f"{code}:{channel.get('id') or channel.get('tvg_id') or normalize(channel.get('name'))}"
            channel["card"] = {
                "title": channel.get("name"),
                "logo": channel.get("logo"),
                "country": code,
                "country_name": COUNTRY_NAMES[code],
                "categories": channel["categories"],
                "category_labels": channel["category_labels"],
                "playback_status": state,
                "hls_confirmed": channel["playback_summary"]["hls_confirmed_streams"] > 0,
                "favorite_key": channel["favorite_key"],
            }
            category_counts.update(channel["categories"])
            total["channels"] += 1

            app_channels.append({
                "id": channel.get("id"),
                "name": channel.get("name"),
                "playlist_name": channel.get("playlist_name"),
                "tvg_id": channel.get("tvg_id"),
                "country": code,
                "country_name": COUNTRY_NAMES[code],
                "logo": channel.get("logo"),
                "logo_source": channel.get("logo_metadata", {}).get("source", "playlist_or_missing"),
                "categories": channel["categories"],
                "category_labels": channel["category_labels"],
                "classification": channel.get("classification"),
                "playback": channel["playback_summary"],
                "favorite_key": channel["favorite_key"],
                "search": channel["search"],
                "streams": channel.get("streams", []),
            })

        catalogue.setdefault("summary", {})["enrichment"] = {
            "official_metadata_matches": sum(match_counts.values()) - match_counts.get("unmatched", 0) - match_counts.get("ambiguous_name", 0),
            "logo_matches": logo_count,
            "matching": dict(sorted(match_counts.items())),
            "categories": dict(sorted(category_counts.items())),
            "enriched_at": timestamp,
        }
        catalogue["enrichment"] = {
            "source": "https://github.com/iptv-org/api",
            "source_license": "Unlicense",
            "checked_at": timestamp,
            "logo_policy": "official_in_use_https_preferred; existing_playlist_logo_fallback",
            "stream_urls_modified": False,
        }
        path.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            f"[OK] {path.relative_to(ROOT)}: {len(catalogue.get('channels', []))} canais; "
            f"{logo_count} logos oficiais; correspondências {dict(match_counts)}"
        )

    app_channels.sort(key=lambda item: (item["country"], item["name"] or ""))
    filters = {
        "countries": [
            {"code": code, "name": COUNTRY_NAMES[code]}
            for code in COUNTRIES
        ],
        "categories": [
            {"id": key, "name": CATEGORY_NAMES.get(key, key.title())}
            for key in sorted({cat for channel in app_channels for cat in channel["categories"]})
        ],
        "playback_statuses": [
            {"id": "hls_confirmed", "name": "HLS confirmado"},
            {"id": "http_reachable_hls_unconfirmed", "name": "HTTP acessível; HLS não confirmado"},
            {"id": "temporary_failure_or_unavailable", "name": "Falha temporária ou indisponível"},
            {"id": "restricted_or_auth_required", "name": "Restrito ou requer autenticação"},
            {"id": "no_confirmed_stream", "name": "Sem stream confirmado"},
            {"id": "not_checked", "name": "Ainda não verificado"},
        ],
    }
    app_catalogue = {
        "schema_version": 1,
        "generated_at": timestamp,
        "title": "TV Global",
        "countries": [{"code": c, "name": COUNTRY_NAMES[c]} for c in COUNTRIES],
        "filters": filters,
        "summary": {
            "channels": len(app_channels),
            "countries": len(COUNTRIES),
            "hls_confirmed": sum(ch["playback"]["status"] == "hls_confirmed" for ch in app_channels),
            "without_confirmed_stream": sum(ch["playback"]["status"] != "hls_confirmed" for ch in app_channels),
        },
        "favorites": {"storage_key": "tv-global-favorites-v1", "mode": "client_side_ready"},
        "search": {"normalization": "unicode_fold_alphanumeric", "fields": ["name", "tvg_id", "categories", "network"]},
        "sources": ["iptv-org/api", "CANAL-CHANEL- playlists"],
        "channels": app_channels,
    }
    APP_OUTPUT.write_text(json.dumps(app_catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] {APP_OUTPUT.relative_to(ROOT)}: {len(app_channels)} cards app-ready")
    print("[TOTAL] " + json.dumps(dict(total), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (URLError, TimeoutError) as exc:
        print(f"[ERRO] Falha ao consultar metadados externos: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        raise SystemExit(1)

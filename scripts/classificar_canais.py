#!/usr/bin/env python3
"""Classify catalogue channels from public metadata; never inspect stream media."""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_DIR = ROOT / "catalogos"
COUNTRIES = ("br", "pt", "us")

# Conservative metadata-only rules. Add terms here as the catalogue grows.
RULES = [
    ("jornalismo", 90, [
        r"\bnews\b", r"\bnoticias?\b", r"\bjornal\b", r"\bbandnews\b",
        r"\bcnn\b", r"\bbbc\b", r"\bfox news\b", r"\bsky news\b",
        r"\btelejornal\b", r"\bpress\b", r"\bworld report\b",
    ]),
    ("esportes", 90, [
        r"\bsports?\b", r"\besportes?\b", r"\bfutebol\b", r"\bsoccer\b",
        r"\bfootball\b", r"\bespn\b", r"\bfox sports\b", r"\bsportv\b",
        r"\bcombate\b", r"\bufc\b", r"\btennis\b", r"\bbasketball\b",
        r"\bmotorsport\b", r"\bracing\b",
    ]),
    ("desenhos", 90, [
        r"\bcartoon", r"\banimation\b", r"\banimated\b", r"\bdesenhos?\b",
        r"\banime\b", r"\bkids?\b", r"\bjunior\b", r"\bbaby shark\b",
        r"\bbob esponja\b", r"\bspongebob\b", r"\bcocomelon\b",
        r"\bdisney junior\b", r"\bnick(toons)?\b",
    ]),
    ("documentarios", 90, [
        r"\bdocumentar", r"\bdocumentary\b", r"\bdiscovery\b",
        r"\bnational geographic\b", r"\bnat geo\b", r"\bhistory channel\b",
        r"\banimal planet\b", r"\bwild\b", r"\bnature\b", r"\bscience\b",
        r"\bcuriosity\b",
    ]),
    ("filmes", 90, [
        r"\bmovies?\b", r"\bcinema\b", r"\bfilmes?\b", r"\bfilm\b",
        r"\bmovie network\b", r"\baction\b", r"\bthriller\b",
        r"\bcomedy movies\b", r"\bclassic movies\b", r"\bwestern\b",
        r"\bhorror\b", r"\bdrama\b", r"\bcin[eé]\b",
    ]),
    ("musica", 90, [
        r"\bmusic\b", r"\bm[uú]sica\b", r"\bmusical\b", r"\bmtv\b",
        r"\bvh1\b", r"\btrace\b", r"\bvevo\b", r"\bconcert",
        r"\bkaraoke\b", r"\bradio\b", r"\bsertanejo\b", r"\bclipe",
    ]),
    ("regionais", 85, [
        r"\bregional\b", r"\blocal tv\b", r"\bcommunity tv\b",
        r"\bmunicipal\b", r"\bmunicipal tv\b", r"\bcidade\b",
        r"\binterior\b", r"\bvale do\b", r"\bsat\b", r"\bamazonas\b",
        r"\namazon\b", r"\bpar[aá]\b", r"\bcear[aá]\b", r"\bgo[iá]s\b",
        r"\bminas\b", r"\bga[uú]cho\b", r"\bcarioca\b",
    ]),
    ("entretenimento", 70, [
        r"\bentertainment\b", r"\breality\b", r"\bshow\b",
        r"\bvariedades\b", r"\bnovelas?\b", r"\bseries?\b",
        r"\bcomedy\b", r"\bcom[eé]dia\b", r"\bpluto\b",
    ]),
    ("educacao_cultura", 80, [
        r"\beduca", r"\bculture\b", r"\bcultura\b", r"\buniversity\b",
        r"\buniversidade\b", r"\bschool\b", r"\bparlamento\b",
        r"\barts?\b", r"\barte\b", r"\bliteratura\b",
    ]),
    ("religiao", 80, [
        r"\bchurch\b", r"\bigreja\b", r"\bgospel\b", r"\brelig",
        r"\bfaith\b", r"\bfe\b", r"\bcat[oó]lic",
    ]),
    ("agronegocio", 80, [
        r"\bagro\b", r"\bagriculture\b", r"\brural\b",
        r"\bfarm\b", r"\bcampo\b", r"\bagroneg[oó]cio\b",
    ]),
    ("lifestyle", 70, [
        r"\blifestyle\b", r"\bcozinha\b", r"\bcooking\b", r"\bfood\b",
        r"\btravel\b", r"\bviagem\b", r"\bhome\b", r"\bdecora",
        r"\bfitness\b", r"\bsa[uú]de\b",
    ]),
]

PATTERNS = [
    (category, weight, [(term, re.compile(term, re.IGNORECASE)) for term in terms])
    for category, weight, terms in RULES
]


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def classify(channel: dict) -> dict:
    searchable = " ".join(str(channel.get(key) or "") for key in (
        "name", "tvg_id", "group_title", "id"
    ))
    searchable = normalize(searchable).casefold()
    scores = Counter()
    matched = {}
    for category, weight, patterns in PATTERNS:
        terms = [term for term, pattern in patterns if pattern.search(searchable)]
        if terms:
            scores[category] += weight + min((len(terms) - 1) * 5, 10)
            matched[category] = terms

    if not scores:
        return {
            "primary": "outros",
            "confidence": "baixa",
            "score": 0,
            "matched_terms": [],
            "method": "metadata_rules_v1",
        }

    ordered = scores.most_common()
    category, score = ordered[0]
    # When two categories tie, do not pretend the classification is certain.
    ambiguous = len(ordered) > 1 and ordered[1][1] == score
    confidence = "baixa" if ambiguous else ("alta" if score >= 85 else "media")
    return {
        "primary": category,
        "confidence": confidence,
        "score": score,
        "matched_terms": matched[category],
        "also_matches": [name for name, value in ordered[1:] if value >= score - 10],
        "method": "metadata_rules_v1",
    }


def main() -> int:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    overall = Counter()

    for code in COUNTRIES:
        path = CATALOGUE_DIR / f"catalogo-{code}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Catálogo não encontrado: {path.relative_to(ROOT)}")
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        counts = Counter()
        confidence_counts = Counter()

        for channel in catalogue.get("channels", []):
            result = classify(channel)
            channel["classification"] = result
            counts[result["primary"]] += 1
            confidence_counts[result["confidence"]] += 1

        catalogue.setdefault("summary", {})["classification"] = dict(sorted(counts.items()))
        catalogue["summary"]["classification_confidence"] = dict(sorted(confidence_counts.items()))
        catalogue["summary"]["classification_checked_at"] = timestamp
        catalogue["classification"] = {
            "method": "metadata_rules_v1",
            "checked_at": timestamp,
            "media_analyzed": False,
            "manual_review_recommended_for": ["baixa"],
        }
        path.write_text(
            json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        overall.update(counts)
        print(
            f"[OK] {path.relative_to(ROOT)}: "
            f"{sum(counts.values())} canais classificados; "
            f"{dict(sorted(counts.items()))}"
        )

    print("[TOTAL] " + json.dumps(dict(sorted(overall.items())), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        raise SystemExit(1)

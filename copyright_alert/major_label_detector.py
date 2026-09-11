#!/usr/bin/env python3
"""Major-label claimant classification helpers for infringement claims."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

CLAIMANT_GROUP_HEADER = "Claimant Group"
CLAIMANT_TIER_HEADER = "Claimant Tier"
CLAIMANT_ARM_HEADER = "Claimant Arm"
CLAIMANT_CATEGORY_HEADER = "Claimant Category"
MAJOR_LABEL_HEADERS = [
    CLAIMANT_GROUP_HEADER,
    CLAIMANT_TIER_HEADER,
    CLAIMANT_ARM_HEADER,
    CLAIMANT_CATEGORY_HEADER,
]

WARNER = "Warner Music Group"
SONY = "Sony Music Entertainment"
UNIVERSAL = "Universal Music Group"

PROXY_DOMAINS = {
    "riaa.com",
    "ifpi.org",
    "websheriff.com",
    "audiosalad.com",
    "fuga.com",
    "spotify.com",
}


@dataclass(frozen=True)
class ClaimantMatch:
    major: Optional[str]
    tier: Optional[int]
    arm: Optional[str]
    category: str


DOMAIN_TABLE: Dict[str, ClaimantMatch] = {
    "warnerchappell.com": ClaimantMatch(WARNER, 1, "publishing", "Major label"),
    "wmg.com": ClaimantMatch(WARNER, 1, "recorded", "Major label"),
    "warnermusic.com": ClaimantMatch(WARNER, 1, "recorded", "Major label"),
    "warnerrecords.com": ClaimantMatch(WARNER, 1, "recorded", "Major label"),
    "atlanticrecords.com": ClaimantMatch(WARNER, 1, "recorded", "Major label"),
    "bmg.com": ClaimantMatch("BMG Rights Management", 1, "recorded", "Major label"),
    "umusic.com": ClaimantMatch(UNIVERSAL, 1, "recorded", "Major label"),
    "virginmusic.com": ClaimantMatch(UNIVERSAL, 1, "distribution", "Major label"),
    "somlivre.com.br": ClaimantMatch(SONY, 1, "recorded", "Major label"),
    "sonymusic.*": ClaimantMatch(SONY, 1, "recorded", "Major label"),
    "sonymusicpub.com": ClaimantMatch(SONY, 1, "publishing", "Major label"),
    "theorchard.com": ClaimantMatch(SONY, 2, "distribution", "Tier 2 distributor"),
    "awal.com": ClaimantMatch(SONY, 2, "distribution", "Tier 2 distributor"),
    "ingrooves.com": ClaimantMatch(UNIVERSAL, 2, "distribution", "Tier 2 distributor"),
    "ada-music.com": ClaimantMatch(WARNER, 2, "distribution", "Tier 2 distributor"),
    "onerpm.com": ClaimantMatch(None, 2, "distribution", "Tier 2 distributor"),
    "believe.com": ClaimantMatch(None, 2, "distribution", "Tier 2 distributor"),
    "symphonic.com": ClaimantMatch(None, 2, "distribution", "Tier 2 distributor"),
    "downtownmusic.com": ClaimantMatch(None, 2, "distribution", "Tier 2 distributor"),
    "vydia.com": ClaimantMatch(None, 2, "distribution", "Tier 2 distributor"),
    "thesignal.gg": ClaimantMatch("Independent", 2, "distribution", "Tier 2 distributor"),
    "brmusicrights.com": ClaimantMatch("Independent", 2, "rights management", "Tier 2 distributor"),
    "soundon.global": ClaimantMatch("SoundOn (ByteDance)", 4, None, "Internal self-claim"),
    "gmail.com": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "hotmail.com": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "hotmail.fr": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "live.com": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "outlook.com": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "yahoo.com": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "proton.me": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "tuta.com": ClaimantMatch("Individual/personal", 3, None, "Independent artist"),
    "symdistro.com": ClaimantMatch("Symphonic Distribution (alt domain)", 3, "distribution", "Tier 3 distributor"),
    "ninemusic.com.br": ClaimantMatch("Nine Music", 3, "distribution", "Tier 3 distributor"),
    "taomusic.com.br": ClaimantMatch("Tao Music", 3, "distribution", "Tier 3 distributor"),
    "disrupsom.com": ClaimantMatch("Disrupsom", 3, "distribution", "Tier 3 distributor"),
    "grupogr6.com.br": ClaimantMatch("Grupo GR6", 3, "distribution", "Tier 3 distributor"),
    "skittlegirl.com": ClaimantMatch("Skittle Girl", 3, None, "Independent"),
    "tribaltrap.com": ClaimantMatch("Tribal Trap", 3, None, "Independent"),
    "vmusicedigital.com": ClaimantMatch("V Music Digital", 3, "distribution", "Tier 3 distributor"),
    "altafonte.com": ClaimantMatch("Altafonte", 3, "distribution", "Tier 3 distributor"),
    "artistpg.com": ClaimantMatch("ArtistPG", 3, None, "Independent"),
    "calmsound.com": ClaimantMatch("Calm Sound", 3, None, "Independent"),
    "cinqmusic.com": ClaimantMatch("Cinq Music", 3, "distribution", "Tier 3 distributor"),
    "disetti.com": ClaimantMatch("Disetti", 3, None, "Independent"),
    "empi.re": ClaimantMatch("Empire", 3, "distribution", "Tier 3 distributor"),
    "grayzone.com": ClaimantMatch("Grayzone", 3, None, "Independent"),
    "lavisionprod.com": ClaimantMatch("La Vision Prod", 3, None, "Independent"),
    "liquidritual.com": ClaimantMatch("Liquid Ritual", 3, None, "Independent"),
    "mainfactor.com": ClaimantMatch("Main Factor", 3, None, "Independent"),
    "materiamusic.com": ClaimantMatch("Materia Music", 3, None, "Independent"),
    "musicpro.live": ClaimantMatch("Music Pro", 3, None, "Independent"),
    "norgat.es": ClaimantMatch("Norgat", 3, None, "Independent"),
    "nwsmusicgroup.com": ClaimantMatch("NWS Music Group", 3, None, "Independent"),
    "polarisrecords.net": ClaimantMatch("Polaris Records", 3, None, "Independent"),
    "sonarmusic.org": ClaimantMatch("Sonar Music", 3, None, "Independent"),
    "triplepointmusic.com": ClaimantMatch("Triple Point Music", 3, None, "Independent"),
    "unitedmasters.com": ClaimantMatch("United Masters", 3, "distribution", "Tier 3 distributor"),
    "zucafilmes.com.br": ClaimantMatch("Zuca Filmes", 3, None, "Independent"),
    "kondzilla.com": ClaimantMatch(None, 3, "distribution", "Tier 3 independent"),
    "atlasrights.org": ClaimantMatch(None, 3, None, "Enforcement proxy"),
    "riaa.com": ClaimantMatch(None, None, None, "Enforcement proxy"),
    "ifpi.org": ClaimantMatch(None, None, None, "Enforcement proxy"),
    "websheriff.com": ClaimantMatch(None, None, None, "Enforcement proxy"),
    "audiosalad.com": ClaimantMatch(None, None, None, "Enforcement proxy"),
    "fuga.com": ClaimantMatch(None, None, None, "Enforcement proxy"),
    "spotify.com": ClaimantMatch(None, None, None, "Enforcement proxy"),
}

KEYWORD_RULES = [
    (SONY, 1, "recorded", "Major label", [
        "Sony Music Entertainment",
        "SME",
        "Sony Music",
        "Som Livre",
        "sonymusicpub",
    ]),
    (WARNER, 1, "recorded", "Major label", [
        "Warner Music Group",
        "Warner Music",
        "Warner Chappell",
        "Atlantic Records",
        "Warner Records",
    ]),
    (UNIVERSAL, 1, "recorded", "Major label", [
        "Universal Music Group",
        "Universal Music",
        "UMG",
        "Republic Records",
        "Def Jam",
        "Capitol Records",
        "Interscope",
        "Island Records",
        "Virgin Records",
        "Virgin Music",
        "Polydor",
        "Parlophone",
        "Elektra Records",
        "Arista Records",
        "Epic Records",
    ]),
    (SONY, 2, "distribution", "Tier 2 distributor", [
        "The Orchard",
        "AWAL",
    ]),
    (UNIVERSAL, 2, "distribution", "Tier 2 distributor", [
        "INgrooves",
    ]),
    (WARNER, 2, "distribution", "Tier 2 distributor", [
        "ADA Music",
        "ADA Distribution",
    ]),
    (None, 2, "distribution", "Tier 2 distributor", [
        "OneRPM",
        "Believe",
        "Symphonic",
        "Downtown Music",
        "Vydia",
    ]),
    (None, 3, "distribution", "Tier 3 independent", [
        "KondZilla",
    ]),
]

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})", re.IGNORECASE)


def _blank_result(raw_domain: Optional[str], category: str = "Unknown", matched_by: Optional[str] = None) -> dict:
    return {
        "major": None,
        "tier": None,
        "arm": None,
        "category": category,
        "matched_by": matched_by,
        "raw_domain": raw_domain,
    }


def _result(match: ClaimantMatch, matched_by: str, raw_domain: Optional[str]) -> dict:
    return {
        "major": match.major,
        "tier": match.tier,
        "arm": match.arm,
        "category": match.category,
        "matched_by": matched_by,
        "raw_domain": raw_domain,
    }


def extract_domain(claimant_email: str) -> Optional[str]:
    text = str(claimant_email or "").strip().lower()
    if not text or text in {"n/a", "none", "null"}:
        return None
    match = _EMAIL_RE.search(text)
    if match:
        return match.group(1).strip(" .>")
    if "@" in text:
        candidate = text.rsplit("@", 1)[-1].split()[0].strip(" <>()[]{};,.")
    else:
        candidate = text.split()[0].strip(" <>()[]{};,.")
    if "." not in candidate or candidate.startswith("."):
        return None
    return candidate


def _domain_matches(raw_domain: Optional[str], matcher: str) -> bool:
    if not raw_domain:
        return False
    domain = raw_domain.lower().strip(".")
    matcher = matcher.lower().strip(".")
    if matcher == "sonymusic.*":
        return domain.startswith("sonymusic.") or ".sonymusic." in domain
    return domain == matcher or domain.endswith(f".{matcher}")


def _find_domain_match(raw_domain: Optional[str]) -> Optional[ClaimantMatch]:
    for matcher, match in DOMAIN_TABLE.items():
        if _domain_matches(raw_domain, matcher):
            return match
    return None


def _is_proxy_domain(raw_domain: Optional[str]) -> bool:
    return any(_domain_matches(raw_domain, domain) for domain in PROXY_DOMAINS)


def _contains_keyword(text: str, keyword: str) -> bool:
    if not text:
        return False
    escaped = re.escape(keyword.lower())
    if keyword.isupper() and len(keyword) <= 4:
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text.lower()) is not None
    return keyword.lower() in text.lower()


def _find_keyword_match(claimant_name: str) -> Optional[ClaimantMatch]:
    text = str(claimant_name or "")
    if not text.strip():
        return None
    for major, tier, arm, category, keywords in KEYWORD_RULES:
        if any(_contains_keyword(text, keyword) for keyword in keywords):
            return ClaimantMatch(major, tier, arm, category)
    return None


def classify_claimant(claimant_email: str, claimant_name: str) -> dict:
    raw_domain = extract_domain(claimant_email)

    domain_match = _find_domain_match(raw_domain)
    if domain_match and raw_domain and not _is_proxy_domain(raw_domain):
        return _result(domain_match, "domain", raw_domain)

    if _is_proxy_domain(raw_domain):
        keyword_match = _find_keyword_match(claimant_name)
        if keyword_match:
            return _result(keyword_match, "claimant_name", raw_domain)
        return _blank_result(raw_domain, category="Enforcement proxy", matched_by="proxy_keyword")

    if domain_match:
        return _result(domain_match, "domain", raw_domain)

    return _blank_result(raw_domain)


def tracker_values(classification: dict) -> dict:
    tier = classification.get("tier")
    return {
        CLAIMANT_GROUP_HEADER: classification.get("major") or "",
        CLAIMANT_TIER_HEADER: "" if tier is None else str(tier),
        CLAIMANT_ARM_HEADER: classification.get("arm") or "",
        CLAIMANT_CATEGORY_HEADER: classification.get("category") or "Unknown",
    }


def mapping_rows() -> Iterable[dict]:
    for matcher, match in DOMAIN_TABLE.items():
        yield {
            "Type": "domain",
            "Matcher": matcher,
            "Major": match.major or "",
            "Tier": "" if match.tier is None else match.tier,
            "Arm": match.arm or "",
            "Category": match.category,
        }
    for major, tier, arm, category, keywords in KEYWORD_RULES:
        for keyword in keywords:
            yield {
                "Type": "claimant_name_keyword",
                "Matcher": keyword,
                "Major": major or "",
                "Tier": "" if tier is None else tier,
                "Arm": arm or "",
                "Category": category,
            }

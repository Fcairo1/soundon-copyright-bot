from copyright_alert.major_label_detector import classify_claimant, tracker_values


def test_direct_domain_match_has_priority_over_claimant_name():
    result = classify_claimant("rights@sonymusicpub.com", "Warner Music Group")

    assert result == {
        "major": "Sony Music Entertainment",
        "tier": 1,
        "arm": "publishing",
        "category": "Major label",
        "matched_by": "domain",
        "raw_domain": "sonymusicpub.com",
    }


def test_sony_music_wildcard_domain():
    result = classify_claimant("claimant@sonymusic.co.uk", "")

    assert result["major"] == "Sony Music Entertainment"
    assert result["tier"] == 1
    assert result["arm"] == "recorded"
    assert result["category"] == "Major label"
    assert result["matched_by"] == "domain"


def test_proxy_domain_can_attribute_by_claimant_name():
    result = classify_claimant("notice@audiosalad.com", "Universal Music Group / Interscope")

    assert result["major"] == "Universal Music Group"
    assert result["tier"] == 1
    assert result["arm"] == "recorded"
    assert result["category"] == "Major label"
    assert result["matched_by"] == "claimant_name"


def test_proxy_domain_without_major_falls_back_to_enforcement_proxy():
    result = classify_claimant("notice@ifpi.org", "Independent label complaint")

    assert result["major"] is None
    assert result["tier"] is None
    assert result["arm"] is None
    assert result["category"] == "Enforcement proxy"
    assert result["matched_by"] == "proxy_keyword"


def test_unknown_domain_does_not_run_claimant_name_keywords():
    result = classify_claimant("notice@example.com", "Sony Music Entertainment")

    assert result["major"] is None
    assert result["tier"] is None
    assert result["arm"] is None
    assert result["category"] == "Unknown"
    assert result["matched_by"] is None


def test_bare_single_word_label_tokens_are_not_enough():
    result = classify_claimant("notice@riaa.com", "Island")

    assert result["category"] == "Enforcement proxy"
    assert result["major"] is None


def test_tracker_values_blanks_nullable_fields():
    values = tracker_values(classify_claimant("notice@riaa.com", "Independent"))

    assert values == {
        "Claimant Group": "",
        "Claimant Tier": "",
        "Claimant Arm": "",
        "Claimant Category": "Enforcement proxy",
    }


def test_new_tier_domains_classify_as_expected():
    assert tracker_values(classify_claimant("claim@bmg.com", "")) == {
        "Claimant Group": "BMG Rights Management",
        "Claimant Tier": "1",
        "Claimant Arm": "recorded",
        "Claimant Category": "Major label",
    }
    assert tracker_values(classify_claimant("claim@soundon.global", "")) == {
        "Claimant Group": "SoundOn (ByteDance)",
        "Claimant Tier": "4",
        "Claimant Arm": "",
        "Claimant Category": "Internal self-claim",
    }
    assert tracker_values(classify_claimant("claim@thesignal.gg", ""))["Claimant Tier"] == "2"
    assert tracker_values(classify_claimant("claim@gmail.com", ""))["Claimant Category"] == "Independent artist"
    assert tracker_values(classify_claimant("claim@symdistro.com", ""))["Claimant Group"] == "Symphonic Distribution (alt domain)"


def test_fuga_and_spotify_are_proxy_domains():
    for email in ("notice@fuga.com", "infringement-claim-response@spotify.com"):
        result = classify_claimant(email, "Independent label complaint")
        assert result["category"] == "Enforcement proxy"
        assert result["matched_by"] == "proxy_keyword"

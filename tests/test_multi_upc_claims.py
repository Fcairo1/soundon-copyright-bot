from copyright_alert import daily_workflow, run_alert


REF_ID = "ref:_00D0992XChO._500Qvio0re:ref"
SUBJECT = f"Possibly Infringing - Notification Warning No 1 - Só Hits Records - Claim 25186256 - {REF_ID}"
EXPECTED_UPCS = [
    "020357892738",
    "074417568910",
    "619566857067",
    "044317140103",
    "044317205161",
    "020357654084",
    "619566636433",
    "619566780150",
    "619566637188",
    "044317553606",
    "074843055152",
    "074843341835",
    "682106007239",
    "044317779457",
]
MULTI_UPC_BODY = """
Hello,
Claimant Name: Liliam Liliam Marques
Email: claim@thesignal.gg
Claim Type: composition
Claimed Territory: WORLDWIDE
Content Title: Vai Vagabunda
Artist: DJ Cyberz, MC K3, MC MV KRIA
UPC: 020357892738
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:1UrQYF65Ej6I4VfMC2yB2A
Title: ManD Max
Artist: DJ RW7 DA 011, MC FERA, MC K3, MC MENOR 019
UPC: 074417568910
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:633VMSb9mXraBeYjBZoPHK
Title: Arsunico Daturus
Artist: DJ D4NILO 09, DJ TLK Original, MC K3
UPC: 619566857067
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:3kc7a2mcUddt0hoPUthB7i
Title: Viagem do Magriza 011
Artist: DJ MAGRIZA 011, MC K3
UPC: 044317140103
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:78oc1Nzlx3Wb8KJ6K268WH
Title: Wd 1.0
Artist: DJ AURUS, MC K3, mc cvs
UPC: 044317205161
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:4lEwTvHdHOYoBYuDvRhqg9
Title: Melodia Imprensiva
Artist: DJ LEVIK ORIGINAL, MC K3
UPC: 020357654084
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:4O2zFO9hIVHVaH8G1NPcqb
Title: Antropologismo Independente 4
Artist: DJ ELITE DA ZN, MC K3, MC Rafinha
UPC: 619566636433
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:1xCcGChP1zWzQAPAjCoZYL
Title: ENTROPIA ILÍCITA
Artist: DJ KLEFFY, MC K3, MenochiqueZs
UPC: 619566780150
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:6Gev9DPACk7BTeqAtx4NpZ
Title: Manipulação Exposta
Artist: DJ RN 013, MC Chico, MC K3
UPC: 619566637188
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:7kBb6CHTyUX5vSda4u3CEd
Title: Montagem Ex Quo Tempore 2
Artist: DJ TLK Original, Dj Sz7, MC K3, MC RD
UPC: 044317553606
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:3COoDPgipqOhfKNZ5gmNf2
Title: ZN Arrabico
Artist: DJ RAFIS ZL, MC K3, MC Vuiziki
UPC: 074843055152
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:3rpKaAwcaoSc85R48mcGgt
Title: Maldade Persa
Artist: DJ TZK, MC K3
UPC: 074843341835
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:4Dikf6BVp7DVAZazevbdEP
Title: Automotivo De Vagabundo
Artist: DJ GB DA 061, MC K3
UPC: 682106007239
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:1X50Cs1wPogaDq9gWYjnI7
Title: Montagem Tenebrosa
Artist: DJ MK3, MC K3
UPC: 044317779457
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:5Din5u0xYhUolxhdT9FeL4
Best Regards,
Spotify Content Protection
ref:_00D0992XChO._500Qvio0re:ref
"""


def test_extract_claim_entries_returns_every_bundled_upc():
    entries = run_alert.extract_claim_entries(
        MULTI_UPC_BODY,
        SUBJECT,
        {"from": "infringement-claim-response@spotify.com"},
    )

    assert [entry["upc"] for entry in entries] == EXPECTED_UPCS
    assert entries[2]["title"] == "Arsunico Daturus"
    assert entries[2]["spotify_uri"] == "spotify:album:3kc7a2mcUddt0hoPUthB7i"
    assert all(entry["ref_id"] == REF_ID for entry in entries)


def test_parse_candidate_expands_multi_upc_email_into_multiple_claim_entries(monkeypatch):
    monkeypatch.setattr(daily_workflow, "fetch_email", lambda msg_id: (MULTI_UPC_BODY, {"from": "infringement-claim-response@spotify.com"}))
    monkeypatch.setattr(daily_workflow.metadata_notice, "is_metadata_notice", lambda *args, **kwargs: False)

    summary = {"examined": 0, "skipped_prefilter": 0, "skipped_no_identifier": 0, "metadata_notices": 0}
    kind, info = daily_workflow._parse_candidate("msg-1", SUBJECT, "2026-09-20T17:41:45Z", "thread-1", set(), summary)

    assert kind == "candidate"
    assert [candidate["ef"]["upc"] for candidate in info] == EXPECTED_UPCS
    assert summary["skipped_no_identifier"] == 0


def test_claim_key_uses_ref_plus_upc_for_bundled_claims():
    assert run_alert.claim_key({"ref_id": REF_ID, "upc": "619566857067"}) == f"ref:{REF_ID}|upc:619566857067"


def test_legacy_ref_duplicate_only_blocks_the_same_upc(monkeypatch):
    monkeypatch.setattr(
        run_alert,
        "_load_posted_claims",
        lambda: {f"ref:{REF_ID}": {"upc": "020357892738"}},
    )

    same_upc_fields = {"ref_id": REF_ID, "upc": "020357892738"}
    other_upc_fields = {"ref_id": REF_ID, "upc": "619566857067"}

    assert run_alert.is_claim_already_posted(
        run_alert.claim_key(same_upc_fields),
        ef=same_upc_fields,
    ) is True
    assert run_alert.is_claim_already_posted(
        run_alert.claim_key(other_upc_fields),
        ef=other_upc_fields,
    ) is False

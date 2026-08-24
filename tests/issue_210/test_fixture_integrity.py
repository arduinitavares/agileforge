"""Freeze the exact provider-free audit evidence for issue #210."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.specs.candidate_contract import load_candidate_contract
from utils.agileforge_spec_profile_v2 import canonical_spec_json

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "issue_210"
GOLD_SPECIFICATION_CANDIDATE_ID = 2


def test_legacy_authority_fixture_bytes_match_the_attempt_30_manifest() -> None:
    """Preserve the captured attempt exactly after Authority runtime removal."""
    manifest = json.loads(
        (FIXTURE_ROOT / "legacy_authority" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["trace_session_id"] == (
        "sha256:aef95c5d48c71877c35d2ea950cbdac088cc4629612a2229b093a9ff73fbc0b8"
    )
    assert manifest["payloads"] == {
        "outer-envelope.json": {
            "bytes": 10154,
            "sha256": (
                "5e18990c5e304782e14b18b3d119bf49e70a310659ad3c3d665ee4394a3457eb"
            ),
        },
        "compiler-input.json": {
            "bytes": 9943,
            "sha256": (
                "111a61e61d5bdeb801e510a9defe41158632338512d617d108835c076a3b7467"
            ),
        },
        "authority-input.json": {
            "bytes": 9728,
            "sha256": (
                "34bbc82966ce3bd05123e039667c7ee2fa40b9b9a4a30dff42877fbb57ee9a19"
            ),
        },
        "initial-output.json": {
            "bytes": 8749,
            "sha256": (
                "88f091dc2cde24bd0113d954018cefd8a0b8f2e99eea1bc39d04dfadaa81a1c6"
            ),
        },
        "repaired-output.json": {
            "bytes": 8721,
            "sha256": (
                "4670cc02da585c64b140d017b0387241dba0a58856a4677621fa46c066ab7594"
            ),
        },
    }

    for filename, expected in manifest["payloads"].items():
        payload = (FIXTURE_ROOT / "legacy_authority" / filename).read_bytes()
        assert len(payload) == expected["bytes"]
        assert hashlib.sha256(payload).hexdigest() == expected["sha256"]


def test_gold_specification_is_complete_and_canonical() -> None:
    """Keep the accepted String Calculator contract usable without Authority."""
    manifest = json.loads(
        (FIXTURE_ROOT / "gold" / "manifest.json").read_text(encoding="utf-8")
    )
    candidate_json = (FIXTURE_ROOT / "gold" / "specification-candidate.json").read_text(
        encoding="utf-8"
    )
    canonical_payload = (
        FIXTURE_ROOT / "gold" / "canonical-specification.json"
    ).read_bytes()

    payload, envelope = load_candidate_contract(
        candidate_json,
        expected_candidate_fingerprint=manifest["candidate_fingerprint"],
    )

    assert manifest["specification_candidate_id"] == GOLD_SPECIFICATION_CANDIDATE_ID
    assert envelope.candidate_fingerprint == manifest["candidate_fingerprint"]
    assert envelope.payload_fingerprint == manifest["spec_hash"]
    expected_spec_hash = manifest["spec_hash"].removeprefix("sha256:")
    assert hashlib.sha256(canonical_payload).hexdigest() == expected_spec_hash
    assert len(canonical_payload) == manifest["canonical_specification_bytes"]
    assert canonical_spec_json(payload).encode("utf-8") == canonical_payload
    assert "DATA.001" in {item.id for item in payload.items}

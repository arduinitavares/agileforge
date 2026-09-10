"""Tests for canonical workbench fingerprints."""

import re
from collections import UserDict, UserList
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from services.agent_workbench.fingerprints import (
    canonical_hash,
    canonical_json,
    normalize_for_hash,
)


def test_canonical_hash_is_order_stable_and_prefixed() -> None:
    """Verify mapping key order does not affect hash output."""
    left = {"b": 2, "a": {"z": None, "m": [3, 2, 1]}}
    right = {"a": {"m": [3, 2, 1], "z": None}, "b": 2}
    fingerprint = canonical_hash(left)

    assert fingerprint == canonical_hash(right)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint)


def test_normalize_for_hash_formats_utc_datetimes_with_z() -> None:
    """Verify datetime normalization uses deterministic UTC strings."""
    value = normalize_for_hash(
        {"created_at": datetime(2026, 5, 14, 12, 30, tzinfo=UTC)}
    )

    assert value == {"created_at": "2026-05-14T12:30:00Z"}


def test_normalize_for_hash_converts_datetimes_to_utc_and_dates_to_iso() -> None:
    """Verify temporal values normalize to stable ISO strings."""
    value = normalize_for_hash(
        {
            "due_on": date(2026, 5, 14),
            "created_at": datetime(
                2026,
                5,
                14,
                9,
                30,
                tzinfo=timezone(timedelta(hours=-3)),
            ),
        }
    )

    assert value == {
        "created_at": "2026-05-14T12:30:00Z",
        "due_on": "2026-05-14",
    }


def test_canonical_hash_preserves_null_values() -> None:
    """Verify null values remain part of the hash payload."""
    with_null = canonical_hash({"a": None})
    without_key = canonical_hash({})

    assert with_null != without_key


def test_canonical_json_sorts_stringified_mapping_keys_and_preserves_lists() -> None:
    """Verify JSON serialization uses the documented canonical shape."""
    payload = {
        2: "two",
        "10": "ten",
        "items": ["é", None, {"b": 2, "a": 1}],
    }

    assert canonical_json(payload) == (
        '{"10":"ten","2":"two","items":["\\u00e9",null,{"a":1,"b":2}]}'
    )
    assert canonical_hash({"items": [1, 2]}) != canonical_hash({"items": [2, 1]})


def test_normalize_for_hash_rejects_duplicate_stringified_mapping_keys() -> None:
    """Verify distinct keys cannot collapse during canonical normalization."""
    with pytest.raises(ValueError, match="Duplicate canonical mapping key"):
        normalize_for_hash({1: "integer key", "1": "string key"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "null"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-23, "-23"),
        (1.25, "1.25"),
        (-0.0, "-0.0"),
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        ("é", '"\\u00e9"'),
    ],
)
def test_scalar_fast_path_preserves_canonical_bytes(
    value: object, expected: str
) -> None:
    """Common leaves retain the existing serializer's exact representation."""
    assert canonical_json(value) == expected


def test_generic_containers_still_normalize_nested_temporal_values() -> None:
    """Mapping and Sequence implementations keep recursive canonical semantics."""
    value = UserDict({"items": UserList([date(2026, 9, 10), {"b": True, "a": None}])})
    assert canonical_json(value) == ('{"items":["2026-09-10",{"a":null,"b":true}]}')


class _ScalarMapping(float, Mapping[str, object]):
    """A scalar subclass whose Mapping protocol must retain dispatch priority."""

    def __getitem__(self, key: str) -> object:
        """Expose a canonical payload different from the scalar value."""
        if key != "mapped":
            raise KeyError(key)
        return True

    def __iter__(self) -> Iterator[str]:
        """Iterate the single mapping key."""
        yield "mapped"

    def __len__(self) -> int:
        """Return the mapping size."""
        return 1


def test_scalar_subclasses_retain_protocol_dispatch() -> None:
    """The optimization must not classify scalar subclasses as plain JSON leaves."""
    assert canonical_json(_ScalarMapping(1.5)) == '{"mapped":true}'


@pytest.mark.parametrize("value", [b"bytes", bytearray(b"bytes")])
def test_byte_sequences_remain_unsupported_json_values(value: object) -> None:
    """Bytes must not become lists or gain a new implicit text encoding."""
    assert normalize_for_hash(value) is value
    with pytest.raises(TypeError):
        canonical_json(value)

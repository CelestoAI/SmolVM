"""Strict policy contract; legacy InternetSettings remains unchanged."""

import json

import pytest
from pydantic import ValidationError

from smolvm.network_policy import NetworkPolicy
from smolvm.types import InternetSettings


def test_explicit_empty_policy_is_valid_and_immutable():
    policy = NetworkPolicy(allowed_domains=[])
    assert policy.allowed_domains == ()
    assert json.loads(policy.model_dump_json()) == {"allowed_domains": []}
    with pytest.raises(ValidationError):
        policy.allowed_domains = ("example.com",)


def test_canonical_domains_and_deterministic_identity():
    first = NetworkPolicy(allowed_domains=["Example.COM.", "bücher.example", "example.com"])
    second = NetworkPolicy(allowed_domains=["xn--bcher-kva.example", "example.com"])
    assert first.allowed_domains == ("example.com", "xn--bcher-kva.example")
    assert first.identity == second.identity
    assert first.identity != NetworkPolicy(allowed_domains=[]).identity


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        " example.com",
        "example.com ",
        "*",
        "*.example.com",
        "https://example.com",
        "example.com:443",
        "user@example.com",
        "example.com/path",
        "example.com?query",
        "example.com#fragment",
        "example..com",
        "example.com..",
        "-bad.example",
        "bad-.example",
        "bad_name.example",
        "a" * 64 + ".example",
        "127.0.0.1",
        "::1",
        "[::1]",
        "127.1",
        "2130706433",
        "0x7f000001",
        "foo\n.example",
        "foo\x00.example",
        "a." * 127 + "com",
    ],
)
def test_rejects_non_domain_inputs(value):
    with pytest.raises(ValidationError):
        NetworkPolicy(allowed_domains=[value])


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"allowed_domains": None},
        {"allowed_domains": ["example.com"], "allow_all": True},
        {"allowed_domains": ["example.com"] * 101},
        {"allowed_domains": [123]},
        {"allowed_domains": "example.com"},
    ],
)
def test_rejects_invalid_shape(value):
    with pytest.raises(ValidationError):
        NetworkPolicy.model_validate(value)


def test_legacy_policy_does_not_change():
    assert InternetSettings().is_allow_all_domains
    assert InternetSettings(allowed_domains=["https://Example.com/"]).allowed_domains == [
        "example.com"
    ]

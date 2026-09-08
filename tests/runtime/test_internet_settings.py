# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for InternetSettings model and domain resolution."""

import socket
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from smolvm.host.network import resolve_domains_to_ips
from smolvm.types import InternetSettings


class TestInternetSettings:
    """Tests for InternetSettings Pydantic model."""

    def test_defaults(self) -> None:
        settings = InternetSettings()
        assert settings.allowed_domains == ["*"]
        assert settings.allowed_http_methods == ["*"]
        assert settings.is_allow_all_domains is True

    def test_specific_domains(self) -> None:
        settings = InternetSettings(allowed_domains=["https://example.com/"])
        assert settings.allowed_domains == ["example.com"]
        assert settings.is_allow_all_domains is False

    def test_wildcard_in_domains(self) -> None:
        settings = InternetSettings(allowed_domains=["*", "https://example.com/"])
        assert settings.is_allow_all_domains is True

    def test_url_extracts_hostname(self) -> None:
        settings = InternetSettings(allowed_domains=["https://Example.COM/", "http://api.test.io"])
        assert settings.allowed_domains == ["example.com", "api.test.io"]

    def test_bare_domain_lowercased(self) -> None:
        settings = InternetSettings(allowed_domains=["  Example.COM  "])
        assert settings.allowed_domains == ["example.com"]

    def test_bare_domain_with_port(self) -> None:
        settings = InternetSettings(allowed_domains=["example.com:8080"])
        assert settings.allowed_domains == ["example.com"]

    def test_url_with_path_raises(self) -> None:
        with pytest.raises(ValidationError, match="paths"):
            InternetSettings(allowed_domains=["https://example.com/some/path"])

    def test_url_with_query_raises(self) -> None:
        with pytest.raises(ValidationError, match="query"):
            InternetSettings(allowed_domains=["https://example.com?q=1"])

    def test_url_with_credentials_raises(self) -> None:
        with pytest.raises(ValidationError, match="credentials"):
            InternetSettings(allowed_domains=["https://user:pass@example.com/"])

    def test_empty_entries_filtered(self) -> None:
        settings = InternetSettings(allowed_domains=["example.com", "  ", "test.com"])
        assert settings.allowed_domains == ["example.com", "test.com"]

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValidationError, match="allowed_domains"):
            InternetSettings(allowed_domains=[])

    def test_all_blank_entries_raises(self) -> None:
        with pytest.raises(ValidationError, match="allowed_domains"):
            InternetSettings(allowed_domains=["", "  "])

    @pytest.mark.parametrize("methods", [["get", "post"], ["GET", "get", "Get"], ["*", "GET"]])
    def test_unenforced_methods_rejected(self, methods: list[str]) -> None:
        with pytest.raises(ValidationError, match="HTTP method restrictions"):
            InternetSettings(allowed_http_methods=methods)

    def test_empty_methods_raises(self) -> None:
        with pytest.raises(ValidationError, match="allowed_http_methods"):
            InternetSettings(allowed_http_methods=[])

    def test_frozen(self) -> None:
        settings = InternetSettings()
        with pytest.raises(ValidationError):
            settings.allowed_domains = ["test.com"]  # type: ignore[misc]

    def test_from_dict(self) -> None:
        settings = InternetSettings(**{"allowed_domains": ["https://example.com/"]})
        assert settings.allowed_domains == ["example.com"]


class TestResolveDomains:
    """Tests for resolve_domains_to_ips helper."""

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_resolves_bare_domain(self, mock_getaddrinfo: object) -> None:
        mock_getaddrinfo.return_value = [  # type: ignore[union-attr]
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ]
        result = resolve_domains_to_ips(["example.com"])
        assert result == ["93.184.216.34"]

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_resolves_url_extracts_hostname(self, mock_getaddrinfo: object) -> None:
        mock_getaddrinfo.return_value = [  # type: ignore[union-attr]
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ]
        result = resolve_domains_to_ips(["https://example.com/path"])
        assert result == ["93.184.216.34"]
        mock_getaddrinfo.assert_called_once_with(  # type: ignore[union-attr]
            "example.com", None, proto=socket.IPPROTO_TCP
        )

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_deduplicates_ips(self, mock_getaddrinfo: object) -> None:
        mock_getaddrinfo.return_value = [  # type: ignore[union-attr]
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
        ]
        result = resolve_domains_to_ips(["example.com"])
        assert result == ["1.2.3.4"]

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_skips_ipv6(self, mock_getaddrinfo: object) -> None:
        mock_getaddrinfo.return_value = [  # type: ignore[union-attr]
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
        ]
        result = resolve_domains_to_ips(["example.com"])
        assert result == ["1.2.3.4"]

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_multiple_domains(self, mock_getaddrinfo: object) -> None:
        def fake_resolve(host: str, *args: object, **kwargs: object) -> list:
            if host == "a.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("2.2.2.2", 0))]

        mock_getaddrinfo.side_effect = fake_resolve  # type: ignore[union-attr]
        result = resolve_domains_to_ips(["a.com", "b.com"])
        assert result == ["1.1.1.1", "2.2.2.2"]

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_skips_wildcard(self, mock_getaddrinfo: object) -> None:
        result = resolve_domains_to_ips(["*"])
        assert result == []
        mock_getaddrinfo.assert_not_called()  # type: ignore[union-attr]

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_unresolvable_domain_skipped(self, mock_getaddrinfo: object) -> None:
        mock_getaddrinfo.side_effect = socket.gaierror("DNS lookup failed")  # type: ignore[union-attr]
        result = resolve_domains_to_ips(["nonexistent.invalid"])
        assert result == []

    @patch("smolvm.host.network.socket.getaddrinfo")
    def test_mixed_resolvable_and_unresolvable(self, mock_getaddrinfo: object) -> None:
        def fake_resolve(host: str, *args: object, **kwargs: object) -> list:
            if host == "good.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))]
            raise socket.gaierror("DNS lookup failed")

        mock_getaddrinfo.side_effect = fake_resolve  # type: ignore[union-attr]
        result = resolve_domains_to_ips(["https://good.com", "https://bad.invalid"])
        assert result == ["1.2.3.4"]


@pytest.mark.parametrize("mode", ["open", "off", "restricted"])
def test_explicit_policy_round_trip(mode: str) -> None:
    cidrs = ["203.0.113.7", "203.0.113.7/32", "198.51.100.0/24"] if mode == "restricted" else []
    settings = InternetSettings(mode=mode, allowed_cidrs=cidrs)
    assert InternetSettings.model_validate_json(settings.model_dump_json()) == settings
    assert settings.is_allow_all_domains is (mode == "open")
    if mode == "restricted":
        assert settings.allowed_cidrs == ["198.51.100.0/24", "203.0.113.7/32"]


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "restricted"},
        {"mode": "off", "allowed_cidrs": ["1.1.1.1"]},
        {"allowed_cidrs": ["1.1.1.1"]},
        {"mode": "open", "allowed_domains": ["example.com"]},
        {"mode": "restricted", "allowed_cidrs": ["::/0"]},
        {"mode": "restricted", "allowed_cidrs": ["0.0.0.0/0"]},
        {"mode": "restricted", "allowed_cidrs": ["172.16.0.7"]},
        {"mode": "restricted", "allowed_cidrs": ["169.254.169.254"]},
        {"mode": "restricted", "allowed_cidrs": ["example.com"]},
    ],
)
def test_invalid_policy(settings: dict) -> None:
    with pytest.raises(ValidationError):
        InternetSettings(**settings)


def test_overlapping_ranges_are_collapsed() -> None:
    settings = InternetSettings(mode="restricted", allowed_cidrs=["10.20.0.0/24", "10.20.0.1"])
    assert settings.allowed_cidrs == ["10.20.0.0/24"]


@pytest.mark.parametrize("value", ["example.com", "::1", "10.0.0.1/99", ""])
def test_invalid_address_shows_usable_examples(value: str) -> None:
    with pytest.raises(ValidationError) as error:
        InternetSettings(mode="restricted", allowed_cidrs=[value])
    message = str(error.value)
    assert "Invalid IPv4 address or range" in message
    assert "203.0.113.10" in message
    assert "10.20.0.0/24" in message


def test_non_aligned_range_suggests_correction_without_widening_access() -> None:
    with pytest.raises(ValidationError, match="use '10.20.0.0/24'"):
        InternetSettings(mode="restricted", allowed_cidrs=["10.20.0.7/24"])
    assert InternetSettings(mode="restricted", allowed_cidrs=["10.20.0.7"]).allowed_cidrs == [
        "10.20.0.7/32"
    ]


@pytest.mark.parametrize("unknown", [{"mod": "off"}, {"enabled": False}, {"allowed_ports": [443]}])
def test_unknown_policy_fields_are_rejected(unknown: dict) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InternetSettings(**unknown)

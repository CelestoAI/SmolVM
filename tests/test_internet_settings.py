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
        settings = InternetSettings(
            allowed_domains=["https://Example.COM/", "http://api.test.io"]
        )
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

    def test_methods_uppercased(self) -> None:
        settings = InternetSettings(allowed_http_methods=["get", "post"])
        assert settings.allowed_http_methods == ["GET", "POST"]

    def test_methods_deduplicated(self) -> None:
        settings = InternetSettings(allowed_http_methods=["GET", "get", "Get"])
        assert settings.allowed_http_methods == ["GET"]

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

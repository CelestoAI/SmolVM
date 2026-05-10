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

"""Tests for SmolVM kernel config fragments."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_FRAGMENT = REPO_ROOT / "kernel" / "microvm" / "config.fragment"


def _enabled_symbols(fragment: Path) -> set[str]:
    """Return symbols explicitly enabled in a kernel config fragment."""
    symbols: set[str] = set()
    for raw_line in fragment.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("CONFIG_") and line.endswith("=y"):
            symbols.add(line.removesuffix("=y"))
    return symbols


def test_microvm_kernel_enables_podman_netavark_networking() -> None:
    """Podman/Netavark needs nftables support for default bridge networking."""
    symbols = _enabled_symbols(COMMON_FRAGMENT)

    required = {
        "CONFIG_NETFILTER_ADVANCED",
        "CONFIG_NETFILTER_XTABLES",
        "CONFIG_NF_TABLES",
        "CONFIG_NF_TABLES_INET",
        "CONFIG_NFT_CT",
        "CONFIG_NFT_NAT",
        "CONFIG_NFT_MASQ",
        "CONFIG_NFT_COMPAT",
        "CONFIG_NETFILTER_XT_MATCH_COMMENT",
    }
    missing = required - symbols
    assert required <= symbols, f"missing symbols: {missing}"

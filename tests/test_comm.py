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

"""Tests for the host↔guest transport abstraction."""

from smolvm.comm import CommChannel
from smolvm.ssh import SSHClient


class TestCommChannelProtocol:
    def test_sshclient_satisfies_commchannel(self) -> None:
        client = SSHClient(host="127.0.0.1")
        assert isinstance(client, CommChannel)
        assert client.kind == "ssh"

    def test_sshclient_has_wait_ready_alias(self) -> None:
        client = SSHClient(host="127.0.0.1")
        # wait_ready delegates to wait_for_ssh; both must be callable.
        assert callable(client.wait_ready)
        assert callable(client.wait_for_ssh)

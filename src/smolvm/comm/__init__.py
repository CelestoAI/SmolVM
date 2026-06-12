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

"""Host↔guest control-plane transports.

SmolVM drives a guest from the host to run commands, transfer files, and
probe readiness. :class:`~smolvm.comm.base.CommChannel` is the small interface
every consumer relies on; it has two implementations:

- :class:`~smolvm.ssh.SSHClient` — SSH/paramiko (the default, works everywhere).
- :class:`~smolvm.comm.vsock_channel.VsockChannel` — a guest agent reached over
  ``AF_VSOCK`` (QEMU on Linux first), which works before the guest network or
  sshd is up.
"""

from __future__ import annotations

from smolvm.comm.base import CommChannel, CommChannelKind, ShellMode
from smolvm.comm.rust_http_vsock_channel import RustHttpVsockChannel
from smolvm.comm.select import (
    ChannelResolution,
    VsockNotSupportedError,
    host_supports_vsock,
    resolve_comm_channel,
)
from smolvm.comm.vsock_channel import VsockChannel

LegacyFramedVsockChannel = VsockChannel

__all__ = [
    "ChannelResolution",
    "CommChannel",
    "CommChannelKind",
    "ShellMode",
    "VsockNotSupportedError",
    "VsockChannel",
    "LegacyFramedVsockChannel",
    "RustHttpVsockChannel",
    "host_supports_vsock",
    "resolve_comm_channel",
]

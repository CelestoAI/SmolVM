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

"""Local HTTP server exposing SmolVM over a typed REST API.

``smolvm server start`` runs this FastAPI app on localhost so that
non-Python clients (the generated TypeScript SDK, and later Go/Java)
can drive sandboxes over HTTP. The VM still boots on the user's own
machine — the server is a thin wrapper around the in-process
:class:`smolvm.SmolVM` facade, not a remote service.
"""

from smolvm.server.app import create_app

__all__ = ["create_app"]

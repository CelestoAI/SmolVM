#!/usr/bin/env python3

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

"""Simple manual example for SmolVM environment variable management.

Run:
    python examples/env_injection_manual.py

This demonstrates the high-level facade methods:
- ``vm.set_env_vars``
- ``vm.list_env_vars``
- ``vm.unset_env_vars``
"""

from smolvm import VM


def main() -> int:
    with VM() as vm:
        print(f"VM started: {vm.vm_id}")

        print("\n1) Set environment variables")
        vm.set_env_vars({"APP_MODE": "dev", "DEBUG": "1"})
        print(vm.list_env_vars())

        print("\n2) Use env vars in a command")
        # vm.run() opens a fresh SSH command session each time, so new values are available.
        print(vm.run("echo APP_MODE=$APP_MODE DEBUG=$DEBUG").output)

        print("\n3) Remove one variable")
        removed = vm.unset_env_vars(["DEBUG"])
        print(f"Removed: {removed}")
        print(vm.list_env_vars())

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

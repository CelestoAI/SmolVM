# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""CLI composition root for persistent sandbox operations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from smolvm.storage import StateManagerProtocol
from smolvm.vm import resolve_data_dir

if TYPE_CHECKING:
    from smolvm.facade import SmolVM
    from smolvm.types import VMConfig
    from smolvm.vm import SmolVMManager


class CLIService:
    """Wire SDK handles to the CLI-owned SQLite inventory."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = resolve_data_dir(data_dir)

    def state_manager(self) -> StateManagerProtocol:
        """Open the inventory shared by separate CLI invocations."""
        from smolvm.cli.state import create_cli_state_manager

        return create_cli_state_manager(self.data_dir / "smolvm.db")

    def manager(self, **kwargs: Any) -> SmolVMManager:
        """Create a low-level manager backed by CLI inventory."""
        from smolvm.vm import SmolVMManager

        kwargs["state_manager"] = self.state_manager()
        return SmolVMManager(**kwargs)

    def create_vm(self, config: VMConfig | None = None, **kwargs: Any) -> SmolVM:
        """Create an SDK handle whose lifecycle is persisted for the CLI."""
        from smolvm.facade import SmolVM

        kwargs["state_manager"] = self.state_manager()
        return SmolVM(config, **kwargs)

    def vm_from_id(self, vm_id: str, **kwargs: Any) -> SmolVM:
        """Reconnect a CLI handle from persistent inventory."""
        from smolvm.facade import SmolVM

        kwargs["state_manager"] = self.state_manager()
        return SmolVM.from_id(vm_id, **kwargs)

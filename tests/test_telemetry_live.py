#!/usr/bin/env python3
"""Manual test for telemetry with a real VM."""

import json
import time
from pathlib import Path

import pytest

from smolvm import SmolVM
from smolvm.telemetry import TelemetryManager


@pytest.mark.integration
@pytest.mark.skipif(
    not Path("/usr/bin/firecracker").exists() and not Path("/usr/local/bin/firecracker").exists(),
    reason="Firecracker not installed; skipping live VM test",
)
def test_telemetry_live() -> None:
    """Test telemetry with an actual VM (requires Firecracker)."""
    
    print("📝 Creating VM with telemetry enabled...")
    with SmolVM() as vm:
        print(f"✅ VM created: {vm.vm_id}")
        
        # Enable telemetry - let start() handle recording
        telemetry_dir = Path.home() / ".smolvm" / "telemetry"
        vm._telemetry = TelemetryManager(
            vm_id=vm.vm_id,
            telemetry_path=telemetry_dir / f"{vm.vm_id}.json",
        )
        
        print("\n🚀 Starting VM...")
        start_time = time.time()
        vm.start()  # This will record boot events automatically
        boot_time = time.time() - start_time
        print(f"✅ VM started in {boot_time:.2f}s")
        
        print("\n⚙️ Running commands...")
        commands = [
            "echo 'Command 1'",
            "uname -a",
            "ls -la /",
            "whoami",
        ]
        
        for cmd in commands:
            result = vm.run(cmd)
            print(f"  ✅ {cmd} -> exit_code={result.exit_code}")
        
        print("\n⏹️ Stopping VM...")
        vm.stop()
        print("✅ VM stopped")
    
    # Verify telemetry was recorded
    print("\n🔍 Checking telemetry...")
    telemetry_file = Path.home() / ".smolvm" / "telemetry" / f"{vm.vm_id}.json"
    
    assert telemetry_file.exists(), f"Telemetry file not found at {telemetry_file}"
    print(f"✅ Telemetry file found: {telemetry_file}")
    
    # Parse and display telemetry
    with open(telemetry_file) as f:
        lines = [line.strip() for line in f if line.strip()]
    
    assert len(lines) > 0, "Telemetry file is empty"
    
    # Get the last (most recent) record
    last_record = json.loads(lines[-1])
    
    print(f"\n📊 Telemetry Summary:")
    print(f"   VM ID: {last_record['vm_id']}")
    print(f"   Created: {last_record['created_at']}")
    
    # Boot events
    boot_events = last_record.get('boot_events', [])
    print(f"\n   Boot Events: {len(boot_events)}")
    for i, evt in enumerate(boot_events):
        status = evt['status']
        duration = evt.get('duration_ms', 'N/A')
        error = evt.get('error', '')
        print(f"     {i+1}. {status} (duration: {duration}ms) {error}")
    
    # Command executions
    commands_exec = last_record.get('command_executions', [])
    print(f"\n   Command Executions: {len(commands_exec)}")
    for i, cmd in enumerate(commands_exec):
        print(f"     {i+1}. {cmd['command']}")
        print(f"        Exit code: {cmd['exit_code']}, Duration: {cmd['duration_ms']}ms")
        if cmd['stdout']:
            stdout_preview = cmd['stdout'][:50].replace('\n', ' ')
            print(f"        Stdout: {stdout_preview}...")
    
    # Assertions
    assert len(boot_events) >= 2, f"Expected at least 2 boot events, got {len(boot_events)}"
    assert len(commands_exec) >= 4, f"Expected at least 4 command executions, got {len(commands_exec)}"
    
    print("\n✅ Telemetry test passed!")
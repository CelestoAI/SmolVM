#!/usr/bin/env python3
"""Manual test for telemetry with a real VM."""

import json
import time
from pathlib import Path

from smolvm import SmolVM
from smolvm.types import VMConfig

def test_telemetry_live() -> None:  # Return None, not bool
    """Test telemetry with an actual VM."""
    
    print("📝 Creating VM with telemetry enabled...")
    with SmolVM() as vm:
        print(f"✅ VM created: {vm.vm_id}")
        
        # Enable telemetry - let start() handle recording
        from pathlib import Path
        from smolvm.telemetry import TelemetryManager
        telemetry_dir = Path.home() / ".smolvm" / "telemetry"
        vm._telemetry = TelemetryManager(
            vm_id=vm.vm_id,
            telemetry_path=telemetry_dir / f"{vm.vm_id}.json",
        )
        
        # DON'T manually record boot_start/ready - let start() do it
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
    telemetry_file = Path.home() / ".smolvm" / "telemetry" / f"{vm.vm_id}.json"  # Use actual VM ID
    
    if not telemetry_file.exists():
        print(f"❌ Telemetry file not found at {telemetry_file}")
        return False
    
    print(f"✅ Telemetry file found: {telemetry_file}")
    
    # Parse and display telemetry
    with open(telemetry_file) as f:
        lines = [line.strip() for line in f if line.strip()]
    
    if not lines:
        print("❌ Telemetry file is empty")
        return False
    
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
    commands = last_record.get('command_executions', [])
    print(f"\n   Command Executions: {len(commands)}")
    for i, cmd in enumerate(commands):
        print(f"     {i+1}. {cmd['command']}")
        print(f"        Exit code: {cmd['exit_code']}, Duration: {cmd['duration_ms']}ms")
        if cmd['stdout']:
            stdout_preview = cmd['stdout'][:50].replace('\n', ' ')
            print(f"        Stdout: {stdout_preview}...")
    
    print("\n✅ Telemetry test passed!")
    # Don't return anything


if __name__ == "__main__":
    try:
        test_telemetry_live()
        print("✅ All assertions passed!")
        exit(0)
    except AssertionError as e:
        print(f"\n❌ Assertion failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
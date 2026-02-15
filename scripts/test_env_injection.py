#!/usr/bin/env python3
"""
Manual test script for SmolVM environment variable injection.
Steps:
1. Create a VM with initial env vars.
2. Inject additional vars dynamically.
3. Print the env file.
4. Remove vars one by one.
5. Print the env file after each removal.
6. Cleanup.
"""

import time
import logging
from pathlib import Path

from smolvm.facade import VM, VMConfig
from smolvm.env import inject_env_vars, remove_env_vars, read_env_vars
from smolvm.ssh import SSHClient
from smolvm.utils import ensure_ssh_key

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("smolvm_test")


def print_env_file(vm: VM, label: str) -> None:
    """Read and print the content of the env file from the guest."""
    logger.info(f"\n--- {label} ---")
    res = vm.run("cat /etc/profile.d/smolvm_env.sh")
    if res.ok:
        print(res.stdout.strip())
    else:
        logger.error(f"Failed to read file: {res.stderr}")
    logger.info("---------------------\n")


def main() -> None:
    # 1. Setup
    # 1. Setup
    from smolvm.build import ImageBuilder, SSH_BOOT_ARGS
    
    # Generate/load SSH key
    key_path, pub_key_path = ensure_ssh_key()
    
    logger.info("Building/ensuring custom SSH-capable image...")
    try:
        builder = ImageBuilder()
        # Build image with our public key baked in.
        # This gives us a VM that we can actually SSH into.
        kernel, rootfs = builder.build_alpine_ssh_key(pub_key_path)
    except Exception as e:
        logger.error(f"Failed to build image: {e}")
        return

    # Define VM Config with initial env vars and proper boot args for SSH
    # SSH_BOOT_ARGS includes 'init=/init' which is required for network setup
    config = VMConfig(
        vm_id="test-env-vm",
        kernel_path=kernel,
        rootfs_path=rootfs,
        boot_args=SSH_BOOT_ARGS,
        env_vars={
            "INITIAL_A": "aaaa",
            "INITIAL_B": "bbbb"
        }
    )

    # Initial cleanup to ensure clean state
    logger.info("Cleaning up any stale VM 'test-env-vm'...")
    try:
        # We can try to load via ID and delete if exists
        try:
            old_vm = VM.from_id("test-env-vm")
            if old_vm.info.status == "running":
                old_vm.stop()
            old_vm.delete()
            logger.info("Deleted stale VM.")
        except Exception:
            # Likely doesn't exist, which is good
            pass
    except Exception as e:
        logger.warning(f"Error during initial cleanup: {e}")

    logger.info("creating VM 'test-env-vm' with INITIAL_A, INITIAL_B...")
    
    # Use context manager for auto-cleanup
    try:
        # We handle creation manually to force cleanup even if start fails
        vm = VM(config, ssh_key_path=str(key_path))
        
        # 2. Start VM (Initial injection happens here)
        # 2. Start VM (Initial injection happens here)
        logger.info("Starting VM (timeout=20s)...")
        vm.start(boot_timeout=20.0)
        
        # Get SSH Details
        # We need a raw SSHClient for the env helper functions, similar to how CLI does it
        vm_info = vm.info
        if not vm_info.network:
            raise RuntimeError("VM has no network!")
            
        ssh = SSHClient(
            host=vm_info.network.guest_ip,
            user="root",
            key_path=str(key_path)
        )
        
        logger.info("VM Started. Verifying initial injection...")
        print_env_file(vm, "Initial Env File")

        # 3. Dynamic Injection
        logger.info("Injecting DYNAMIC_X and DYNAMIC_Y...")
        new_vars = {"DYNAMIC_X": "xxxx", "DYNAMIC_Y": "yyyy"}
        inject_env_vars(ssh, new_vars)
        
        print_env_file(vm, "After Dynamic Injection")

        # 4. Remove one by one
        to_remove = ["INITIAL_A", "DYNAMIC_X", "INITIAL_B", "DYNAMIC_Y"]
        
        for key in to_remove:
            logger.info(f"Removing {key}...")
            remove_env_vars(ssh, [key])
            print_env_file(vm, f"After removing {key}")

    except Exception as e:
        logger.error(f"An error occurred: {e}")
        # Debug network status if SSH failed
        try:
             import subprocess
             logger.info("--- Network Debug ---")
             subprocess.run("ip addr show tap2", shell=True)
             subprocess.run("ip route", shell=True)
             logger.info("Ping check:")
             subprocess.run("ping -c 1 172.16.0.2", shell=True)
             logger.info("---------------------")
        except:
             pass
    finally:
        logger.info("Cleaning up...")
        try:
            # properly attach to existing VM for cleanup
            vm = VM.from_id("test-env-vm")
            if vm.info.status == "running":
                vm.stop()
            vm.delete()
            logger.info("VM deleted.")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")

if __name__ == "__main__":
    main()

"""Internal isolated worker; launched by SmolVM, never configured by guests."""

import asyncio
import hashlib
import json
import logging
import os
import resource
import sys
import warnings
from pathlib import Path


async def serve(config):
    # Limits apply before loading the engine; core files and all engine logs are
    # disabled. Structured readiness is the only stdout protocol.
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    logging.disable(sys.maxsize)
    warnings.filterwarnings("ignore")
    from engine import make_proxy

    master, policy = make_proxy(
        frozenset(config["allowed_domains"]),
        Path(config["confdir"]),
        host_addresses=frozenset(config["host_addresses"]),
        listen_host=config["gateway"],
        listen_port=config["port"],
        # Test PKI trust, never ssl_insecure or a bypass selector.
        upstream_ca=config.get("upstream_ca"),
    )
    server = master.addons.get("proxyserver")
    task = asyncio.create_task(master.run())
    try:
        async with asyncio.timeout(10):
            while not list(server.servers) or not list(server.servers)[0].listen_addrs:
                if task.done():
                    await task
                    raise RuntimeError("worker stopped before readiness")
                await asyncio.sleep(0.01)
        expected = (config["gateway"], config["port"])
        if list(server.servers)[0].listen_addrs != (expected,):
            raise RuntimeError("unexpected listener")
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "identity": digest,
                    "pid": os.getpid(),
                    "ca_cert": (Path(config["confdir"]) / "mitmproxy-ca-cert.pem").read_text(),
                }
            ),
            flush=True,
        )
        # Explicit shutdown follows verified fencing. Unexpected owner loss
        # must hold the listening port beyond the kernel's three-second lease,
        # rather than freeing it while the old admission could still be open.
        command = await asyncio.to_thread(sys.stdin.buffer.read, 1)
        if command != b"S":
            await asyncio.sleep(4)
    finally:
        master.shutdown()
        await asyncio.wait_for(task, 5)
        master.options.update(server=False)
        await server.setup_servers()


if __name__ == "__main__":
    try:
        if sys.version_info < (3, 12):
            raise RuntimeError("strict mode requires Python 3.12 or newer")
        configuration = json.loads(sys.stdin.buffer.readline(65536))
        asyncio.run(serve(configuration))
    except BaseException:
        # Do not emit engine exception text: malformed requests can contain
        # credentials or bodies. The parent reports a fixed worker-failed reason.
        raise SystemExit(2) from None

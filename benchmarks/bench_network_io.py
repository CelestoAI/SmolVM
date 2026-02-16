"""Benchmark: network IO between VM and host.

Measures HTTP round-trip latency and SSE streaming throughput
"""

from __future__ import annotations

import http.server
import json
import socket
import subprocess
import sys
import threading
import time

from helpers import (
    format_stats,
    overhead_str,
    print_header,
    print_result,
    print_subheader,
    stats_summary,
    time_call,
    timer,
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _BenchHandler(http.server.BaseHTTPRequestHandler):
    """Simple HTTP handler for benchmarking."""

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            # send SSE events for about 3 seconds
            start = time.time()
            event_count = 0
            try:
                while time.time() - start < 3.0:
                    data = json.dumps({"event": event_count, "ts": time.time()})
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                    event_count += 1
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logging


def _start_http_server(port: int) -> http.server.HTTPServer:
    """Start a background HTTP server on the given port."""
    server = http.server.HTTPServer(("0.0.0.0", port), _BenchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_benchmark() -> dict:
    """Run all network IO benchmarks and return results."""
    from smolvm import SmolVM

    results = {}

    print_header("Network IO Benchmark")

    # Start HTTP server on host
    host_port = _find_free_port()
    server = _start_http_server(host_port)
    print(f"  Test HTTP server running on port {host_port}")

    try:
        ## here we set up a baseline on tyhe host
        print_subheader("Host: HTTP request to localhost")
        host_http_times = []
        for _ in range(100):
            t = time_call(
                lambda: subprocess.run(
                    ["curl", "-s", f"http://127.0.0.1:{host_port}/health"],
                    capture_output=True,
                    timeout=5,
                )
            )
            host_http_times.append(t)
        host_http_stats = stats_summary(host_http_times)
        print_result("Stats (100 iters)", format_stats(host_http_stats))
        results["host_http"] = host_http_stats

        # SSE streaming
        print_subheader("Host: SSE streaming (~3s)")
        with timer() as t:
            result = subprocess.run(
                ["curl", "-s", "-N", f"http://127.0.0.1:{host_port}/stream"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        host_sse_lines = result.stdout.count("data:")
        print_result("Duration", f"{t.elapsed_ms:.0f}ms")
        print_result("Events received", str(host_sse_lines))
        results["host_sse_ms"] = t.elapsed_ms
        results["host_sse_events"] = host_sse_lines

        # now we bench SmolVM
        print_subheader("SmolVM: Starting VM start up")
        with SmolVM() as vm:
            # warmup with a sanity check
            sanity = vm.run("echo smolvm_ready")
            assert "smolvm_ready" in sanity.stdout

            curl_check = vm.run("which curl 2>/dev/null || which wget 2>/dev/null || echo 'none'")
            has_curl = "curl" in curl_check.stdout
            has_wget = "wget" in curl_check.stdout and "none" not in curl_check.stdout

            if not has_curl and not has_wget:
                # Install curl in the VM
                print("  Installing curl in VM...")
                vm.run("apk add --no-cache curl 2>/dev/null || true", timeout=60)
                curl_check = vm.run("which curl")
                has_curl = "curl" in curl_check.stdout

            # Get the VM's gateway IP
            gateway_result = vm.run("ip route | grep default | awk '{print $3}'")
            gateway_ip = gateway_result.output.strip()
            assert gateway_ip, "Failed to get gateway IP from VM"
            print(f"  VM gateway IP: {gateway_ip}")
            # Port exposure
            print_subheader("SmolVM: expose_local() overhead")
            # Start a simple server inside the VM for port exposure test
            vm.run(
                "nohup sh -c 'while true; do "
                'echo -e "HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok"'
                " | nc -l -p 8080 2>/dev/null; done' > /dev/null 2>&1 &"
            )
            time.sleep(0.5)

            expose_times = []
            for _ in range(3):
                t = time_call(lambda: vm.expose_local(8080))
                expose_times.append(t)
                # Clean up for next iteration by unexposing
                break  # ther is only one meaningful measurement since it caches
            expose_stats = stats_summary(expose_times)
            print_result("Stats", format_stats(expose_stats))
            results["vm_expose_local"] = expose_stats

            # HTTP requests from VM to host
            if has_curl:
                fetch_cmd = f"curl -s http://{gateway_ip}:{host_port}/health"
            else:
                fetch_cmd = f"wget -qO- http://{gateway_ip}:{host_port}/health"

            print_subheader("SmolVM: HTTP request (VM -> host)")
            vm_http_times = []
            for _ in range(100):
                t = time_call(lambda: vm.run(fetch_cmd, timeout=10))
                vm_http_times.append(t)
            vm_http_stats = stats_summary(vm_http_times)
            print_result("Stats (100 iters)", format_stats(vm_http_stats))
            results["vm_http"] = vm_http_stats

            # SSE streaming from VM
            if has_curl:
                print_subheader("SmolVM: SSE streaming (3s, VM -> host)")
                with timer() as t:
                    sse_result = vm.run(
                        f"curl -s -N --max-time 5 http://{gateway_ip}:{host_port}/stream",
                        timeout=15,
                    )
                vm_sse_lines = sse_result.stdout.count("data:")
                print_result("Duration", f"{t.elapsed_ms:.0f}ms")
                print_result("Events received", str(vm_sse_lines))
                results["vm_sse_ms"] = t.elapsed_ms
                results["vm_sse_events"] = vm_sse_lines
            else:
                print("\n  Skipping SSE test (curl not available in VM)")
                results["vm_sse_ms"] = None
                results["vm_sse_events"] = None

        # compare
        print_subheader("Comparison (p50)")
        print_result(
            "HTTP roundtrip: VM vs Host",
            overhead_str(host_http_stats["p50"], vm_http_stats["p50"]),
        )
        if results.get("vm_sse_ms") and results.get("host_sse_ms"):
            print_result(
                "SSE throughput: VM events vs Host events",
                f"{results['vm_sse_events']} vs {results['host_sse_events']}",
            )

    finally:
        server.shutdown()

    return results


if __name__ == "__main__":
    if "benchmarks" in __file__:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    run_benchmark()

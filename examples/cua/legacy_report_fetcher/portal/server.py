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

"""Tiny fake legacy reporting portal for the computer-use demo."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import csv
import html
import io
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USERNAME = "ops@acme.test"
PASSWORD = "demo-password"
SESSION_COOKIE = "acme_session=demo"


ORDERS_ROWS = [
    {"order_id": "A-1001", "customer": "Northstar Grocers", "region": "West", "amount": "1280.50"},
    {"order_id": "A-1002", "customer": "Bluebird Supply", "region": "East", "amount": "420.00"},
    {"order_id": "A-1003", "customer": "Summit Hardware", "region": "Central", "amount": "867.25"},
]

INVENTORY_ROWS = [
    {"sku": "SKU-RED-001", "name": "Red Widget", "on_hand": "48", "warehouse": "SFO"},
    {"sku": "SKU-BLU-014", "name": "Blue Widget", "on_hand": "125", "warehouse": "JFK"},
    {"sku": "SKU-GRN-021", "name": "Green Widget", "on_hand": "17", "warehouse": "ORD"},
]


def _default_report_date() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _safe_report_date(value: str) -> str:
    """Return a safe ISO report date for filenames and headers."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return _default_report_date()


def _csv_bytes(rows: list[dict[str, str]], report_date: str) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["report_date", *rows[0].keys()])
    writer.writeheader()
    for row in rows:
        writer.writerow({"report_date": report_date, **row})
    return buffer.getvalue().encode("utf-8")


class PortalHandler(BaseHTTPRequestHandler):
    """Serve login, reports, and CSV downloads."""

    server_version = "AcmeLegacyReports/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/login"}:
            self._send_login()
            return
        if parsed.path == "/reports":
            if not self._is_authenticated():
                self._redirect("/login")
                return
            self._send_reports()
            return
        if parsed.path.startswith("/download/"):
            if not self._is_authenticated():
                self._redirect("/login")
                return
            self._send_download(parsed.path.rsplit("/", 1)[-1], parse_qs(parsed.query))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urlparse(self.path)
        if parsed.path != "/login":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        fields = parse_qs(body)
        username = fields.get("username", [""])[0]
        password = fields.get("password", [""])[0]
        if username == USERNAME and password == PASSWORD:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/reports")
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}; Path=/; SameSite=Lax")
            self.end_headers()
            return

        self._send_login(error="Invalid username or password.")

    def log_message(self, format: str, *args: object) -> None:
        print(f"[acme-portal] {self.address_string()} - {format % args}")

    def _is_authenticated(self) -> bool:
        return SESSION_COOKIE in self.headers.get("Cookie", "")

    def _send_html(self, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _send_login(self, error: str | None = None) -> None:
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        self._send_html(
            f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Acme Legacy Reports Portal</title>
  <style>
    body {{ margin: 0; background: #d8dde6; color: #1f2937; font-family: Arial, sans-serif; }}
    .topbar {{ background: #143a66; color: white; padding: 14px 22px; border-bottom: 4px solid #aeb8c8; }}
    .shell {{ width: 760px; margin: 58px auto; background: #f5f7fb; border: 1px solid #8b98aa; box-shadow: 0 2px 0 #fff inset; }}
    .panel-title {{ background: #e6ebf3; border-bottom: 1px solid #aeb8c8; padding: 12px 16px; font-weight: bold; }}
    form {{ padding: 22px; display: grid; gap: 14px; }}
    label {{ font-weight: bold; }}
    input {{ width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #7d8796; font-size: 15px; }}
    button {{ width: 180px; padding: 9px 12px; background: #245f9f; color: white; border: 1px solid #143a66; font-weight: bold; cursor: pointer; }}
    .hint {{ color: #4b5563; font-size: 13px; }}
    .error {{ color: #9f1239; font-weight: bold; }}
  </style>
</head>
<body>
  <div class="topbar">Acme Legacy Reports Portal</div>
  <main class="shell">
    <div class="panel-title">Secure report access</div>
    <form method="post" action="/login">
      {error_html}
      <p class="hint">Authorized operations users only. Use your assigned portal credentials.</p>
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password">
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>
"""
        )

    def _send_reports(self) -> None:
        report_date = _default_report_date()
        self._send_html(
            f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Reports - Acme Legacy Reports Portal</title>
  <style>
    body {{ margin: 0; background: #d8dde6; color: #1f2937; font-family: Arial, sans-serif; }}
    .topbar {{ background: #143a66; color: white; padding: 14px 22px; border-bottom: 4px solid #aeb8c8; }}
    .layout {{ display: grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 52px); }}
    nav {{ background: #edf1f7; border-right: 1px solid #9aa7b8; padding: 18px; }}
    nav a {{ display: block; color: #143a66; padding: 8px 4px; font-weight: bold; }}
    main {{ padding: 24px; }}
    .panel {{ background: #f8fafc; border: 1px solid #8b98aa; max-width: 920px; }}
    .panel-title {{ background: #e6ebf3; border-bottom: 1px solid #aeb8c8; padding: 12px 16px; font-weight: bold; }}
    .content {{ padding: 18px; display: grid; gap: 16px; }}
    .row {{ display: grid; grid-template-columns: 180px 1fr; gap: 12px; align-items: center; }}
    input[type="date"] {{ width: 180px; padding: 7px; }}
    button {{ width: 190px; padding: 9px 12px; background: #245f9f; color: white; border: 1px solid #143a66; font-weight: bold; cursor: pointer; }}
    .downloads {{ display: none; border-top: 1px solid #cbd5e1; padding-top: 16px; }}
    .download-card {{ background: white; border: 1px solid #c4ccd8; margin: 8px 0; padding: 12px; }}
    .download-card a {{ color: #0f4c81; font-weight: bold; }}
    .muted {{ color: #64748b; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="topbar">Acme Legacy Reports Portal</div>
  <div class="layout">
    <nav>
      <a href="/reports">Daily Reports</a>
      <a href="#">Scheduled Jobs</a>
      <a href="#">Audit Log</a>
    </nav>
    <main>
      <section class="panel">
        <div class="panel-title">Daily report downloads</div>
        <div class="content">
          <p class="muted">Generate yesterday's operational report files for the downstream data pipeline.</p>
          <div class="row">
            <label for="report-date">Report date</label>
            <input id="report-date" type="date" value="{report_date}">
          </div>
          <div class="row">
            <span>Report types</span>
            <div>
              <label><input id="orders" type="checkbox" checked> Orders CSV</label><br>
              <label><input id="inventory" type="checkbox" checked> Inventory CSV</label><br>
              <label><input id="settlements" type="checkbox"> Settlements PDF</label>
            </div>
          </div>
          <button id="generate" type="button">Generate reports</button>
          <div id="downloads" class="downloads" aria-live="polite">
            <strong>Reports are ready.</strong>
            <div class="download-card" data-report="orders">
              Orders CSV — <a id="orders-link" href="/download/orders?date={report_date}" download>Download Orders CSV</a>
            </div>
            <div class="download-card" data-report="inventory">
              Inventory CSV — <a id="inventory-link" href="/download/inventory?date={report_date}" download>Download Inventory CSV</a>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
<script>
  const dateInput = document.getElementById('report-date');
  const downloads = document.getElementById('downloads');
  const ordersLink = document.getElementById('orders-link');
  const inventoryLink = document.getElementById('inventory-link');
  function refreshLinks() {{
    const reportDate = dateInput.value;
    ordersLink.href = `/download/orders?date=${{encodeURIComponent(reportDate)}}`;
    inventoryLink.href = `/download/inventory?date=${{encodeURIComponent(reportDate)}}`;
  }}
  document.getElementById('generate').addEventListener('click', () => {{
    refreshLinks();
    downloads.style.display = 'block';
  }});
  dateInput.addEventListener('change', refreshLinks);
</script>
</body>
</html>
"""
        )

    def _send_download(self, report_name: str, query: dict[str, list[str]]) -> None:
        report_date = _safe_report_date(query.get("date", [_default_report_date()])[0])
        if report_name == "orders":
            rows = ORDERS_ROWS
        elif report_name == "inventory":
            rows = INVENTORY_ROWS
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown report")
            return

        content = _csv_bytes(rows, report_date)
        filename = f"{report_name}_{report_date}.csv"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fake Acme legacy reports portal.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), PortalHandler)
    print(f"Acme Legacy Reports Portal listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
import hashlib
import html
import ipaddress
import json
import re
import signal
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

MAX_BYTES = 500 * 1024
ALLOWED_TYPES = {"text/html", "text/plain", "application/xhtml+xml"}


def bounded_body(body):
    return body[:MAX_BYTES], len(body) > MAX_BYTES


def operation_timed_out(_signum, _frame):
    raise TimeoutError("Page fetch exceeded 15 seconds.")


def validate_url(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS URLs are allowed.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".local") or "." not in host:
        raise ValueError("Only public HTTPS hostnames are allowed.")
    try:
        ipaddress.ip_address(host)
        raise ValueError("Literal IP addresses are not allowed.")
    except ValueError as exc:
        if str(exc) == "Literal IP addresses are not allowed.":
            raise
    for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ValueError("The URL resolves to a private network address.")
    return value


class RedirectHandler(urllib.request.HTTPRedirectHandler):
    redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > 3:
            raise ValueError("The page redirected more than three times.")
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.title_parts = []
        self.blocked = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "svg", "noscript"}:
            self.blocked += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "svg", "noscript"} and self.blocked:
            self.blocked -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.blocked:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if cleaned:
            self.parts.append(cleaned)
            if self.in_title:
                self.title_parts.append(cleaned)


def main():
    url, output = sys.argv[1], sys.argv[2]
    validate_url(url)
    signal.signal(signal.SIGALRM, operation_timed_out)
    signal.alarm(15)
    opener = urllib.request.build_opener(RedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "Open-Muse/0.1"})
    try:
        with opener.open(request, timeout=10) as response:
            content_type = response.headers.get_content_type().lower()
            if content_type not in ALLOWED_TYPES:
                raise ValueError(f"Unsupported content type: {content_type}")
            body, truncated = bounded_body(response.read(MAX_BYTES + 1))
            final_url = response.geturl()
            validate_url(final_url)
            charset = response.headers.get_content_charset() or "utf-8"
    finally:
        signal.alarm(0)
    decoded = body.decode(charset, errors="replace")
    if content_type == "text/plain":
        title = urllib.parse.urlsplit(final_url).hostname or "Untitled"
        text = decoded
    else:
        parser = TextExtractor()
        parser.feed(decoded)
        title = " ".join(parser.title_parts) or urllib.parse.urlsplit(final_url).hostname or "Untitled"
        text = "\n".join(parser.parts)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", html.unescape(text))[:20000]
    result = {
        "url": url,
        "finalUrl": final_url,
        "title": title[:300],
        "retrievedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "contentSha256": hashlib.sha256(body).hexdigest(),
        "truncated": truncated,
        "text": text,
    }
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False)


if __name__ == "__main__":
    main()

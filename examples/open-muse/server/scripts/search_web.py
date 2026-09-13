#!/usr/bin/env python3
import html
import json
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 1024 * 1024
USER_AGENT = " ".join(
    [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Chrome/140.0.0.0 Safari/537.36",
    ]
)


class ResultsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and "result__a" in values.get("class", "").split():
            href = values.get("href", "")
            if href.startswith("//"):
                href = "https:" + href
            parsed = urllib.parse.urlsplit(href)
            if parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"}:
                href = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
            if href.startswith("https://"):
                self.current = {"url": href, "title": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["title"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.current["title"] = " ".join(html.unescape(self.current["title"]).split())[:300]
            if self.current["title"]:
                self.results.append(self.current)
            self.current = None


def parse_results(body):
    parser = ResultsParser()
    parser.feed(body)
    unique = []
    seen = set()
    for result in parser.results:
        if result["url"] not in seen:
            unique.append(result)
            seen.add(result["url"])
    return unique[:8]


def main():
    query, output = sys.argv[1], sys.argv[2]
    if not query.strip() or len(query) > 200:
        raise ValueError("Search query must contain 1 to 200 characters.")
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Search response is larger than 1 MiB.")
    results = parse_results(body.decode("utf-8", errors="replace"))
    if not results:
        raise ValueError("Search returned no public HTTPS results.")
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False)


if __name__ == "__main__":
    main()

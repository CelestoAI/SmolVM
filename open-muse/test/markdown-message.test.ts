import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MarkdownMessage } from "../client/MarkdownMessage.js";

function render(markdown: string): string {
  return renderToStaticMarkup(createElement(MarkdownMessage, null, markdown));
}

test("renders structured assistant Markdown", () => {
  const html = render("## Tools\n\n- **Observe** with `browser_observe()`\n- [Docs](https://example.com)\n\n| Tool | Approval |\n| --- | --- |\n| Observe | No |");

  assert.match(html, /<h2>Tools<\/h2>/);
  assert.match(html, /<ul>/);
  assert.match(html, /<strong>Observe<\/strong>/);
  assert.match(html, /<code>browser_observe\(\)<\/code>/);
  assert.match(html, /<a href="https:\/\/example\.com" target="_blank" rel="noreferrer">Docs<\/a>/);
  assert.match(html, /<table>/);
});

test("does not render raw HTML from assistant output", () => {
  const html = render("Before <script>alert('nope')</script> after");

  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Before alert\(&#x27;nope&#x27;\) after/);
});

test("removes unsafe link protocols", () => {
  const html = render("[Do not open](javascript:alert('nope'))");

  assert.doesNotMatch(html, /javascript:/);
  assert.match(html, /<a href="" target="_blank" rel="noreferrer">Do not open<\/a>/);
});

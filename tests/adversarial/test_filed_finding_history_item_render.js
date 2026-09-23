"use strict";

/**
 * Issue #210: Execution History must render separately filed UAT findings
 * from the camelCase payload the Tauri command serializes (`adversarialFiledFindings`).
 *
 * The desktop never reads the Python CLI's snake_case keys. This suite executes
 * the live render block in ui/app.js against a minimal DOM so a missing field,
 * empty-URL receipt, or plural copy regression fails the UAT.
 */

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appJs = fs.readFileSync(
  path.join(__dirname, "..", "..", "ui", "app.js"),
  "utf8",
);

function extractFiledFindingsBlock() {
  const start = appJs.indexOf("const filedFindings = record.adversarialFiledFindings");
  assert.notEqual(
    start,
    -1,
    "buildExecutionRecordItem must read record.adversarialFiledFindings (camelCase from Rust serde)",
  );
  const end = appJs.indexOf("if (record.capacityConsumedPercent", start);
  assert.notEqual(end, -1, "filed-findings render block must sit beside the other execution summaries");
  return appJs.slice(start, end);
}

function element(tag) {
  return {
    tagName: String(tag).toLowerCase(),
    className: "",
    children: [],
    textContent: "",
    href: "",
    title: "",
    dataset: {},
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...nodes) {
      for (const node of nodes) {
        if (node && typeof node === "object" && "tagName" in node) {
          this.appendChild(node);
        } else if (typeof node === "string") {
          this.textContent += node;
        }
      }
    },
  };
}

function render(record) {
  const body = element("div");
  const document = { createElement: element };
  function addSummaryParagraph(label, text) {
    if (!text) return;
    const paragraph = element("p");
    paragraph.className = "panel-copy";
    paragraph.textContent = `${label}: ${text}`;
    body.appendChild(paragraph);
  }
  function externalLink(label, url, className = "") {
    const link = element("a");
    link.href = url;
    link.className = className;
    link.textContent = label;
    return link;
  }
  const run = new Function(
    "record",
    "document",
    "body",
    "addSummaryParagraph",
    "externalLink",
    `${extractFiledFindingsBlock()}\nreturn body;`,
  );
  return run(record, document, body, addSummaryParagraph, externalLink);
}

function texts(node) {
  const values = [];
  if (node.textContent) values.push(node.textContent);
  for (const child of node.children || []) values.push(...texts(child));
  return values;
}

function links(node) {
  const found = [];
  if (node.tagName === "a") found.push(node);
  for (const child of node.children || []) found.push(...links(child));
  return found;
}

test("one filed finding with a URL is summarized and linked by its title", () => {
  const body = render({
    adversarialFiledFindings: [
      { title: "Separate parser bug", url: "https://example.invalid/issues/182" },
    ],
  });
  assert.ok(
    texts(body).some((text) => /Out-of-scope UAT findings: 1 separately filed issue\./.test(text)),
    texts(body),
  );
  const anchors = links(body);
  assert.equal(anchors.length, 1);
  assert.equal(anchors[0].href, "https://example.invalid/issues/182");
  assert.equal(anchors[0].textContent, "Separate parser bug");
});

test("two filed findings use plural copy and keep both issue links", () => {
  const body = render({
    adversarialFiledFindings: [
      { title: "First crash", url: "https://example.invalid/issues/182" },
      { title: "Second leak", url: "https://example.invalid/issues/183" },
    ],
  });
  assert.ok(
    texts(body).some((text) => /2 separately filed issues\./.test(text)),
    texts(body),
  );
  assert.deepEqual(
    links(body).map((anchor) => [anchor.textContent, anchor.href]),
    [
      ["First crash", "https://example.invalid/issues/182"],
      ["Second leak", "https://example.invalid/issues/183"],
    ],
  );
});

test("a filing captured without a URL still counts in the summary", () => {
  const body = render({
    adversarialFiledFindings: [{ title: "Separate parser bug", url: "" }],
  });
  assert.ok(
    texts(body).some((text) => /1 separately filed issue\./.test(text)),
    "the user must still see that a follow-up issue was filed even when gh omitted the URL",
  );
  assert.equal(links(body).length, 0);
});

test("an execution with no filed findings does not add the follow-up summary", () => {
  const body = render({ adversarialFiledFindings: [] });
  assert.equal(body.children.length, 0);
  assert.ok(!texts(body).some((text) => /Out-of-scope UAT findings/.test(text)));
});

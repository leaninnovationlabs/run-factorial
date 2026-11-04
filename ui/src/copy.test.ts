import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SRC = dirname(fileURLToPath(import.meta.url));

const RULES: [string, RegExp][] = [
  ["em dash", /—|&mdash;/],
  ["en dash", /–|&ndash;/],
  ["curly quote", /[“”‘’]|&[lr][sd]quo;/],
  ["arrow", /[→←↔⇒↩]/],
  ["status glyph", /[⏸▶⚠⏏✓✗•▲▼×›✻]|\p{Extended_Pictographic}/u],
  ["spaced hyphen as a dash", /\S - \S/],
  ["three dots", /\.\.\./],
  ["framework text", /Value error|e\.g\./],
  ["we", /\b(?:[Ww]e|[Oo]ur)\b/],
  ["UK spelling", /organis|colour|\blabelled\b|judgement|serialis|afterwards/i],
  ["UK canceled", /^(?=.*\s).*(?<![`'"])\b[Cc]ancelled\b(?![:`'"])/s],
];

const PROGRESS_LABEL = /^[A-Z][a-z]*ing\b[^…]*…$/;

const ALLOWED: { file: string; needle: string; why: string }[] = [
  { file: "components/activity/words.ts", needle: "{x}…", why: "truncation marker" },
  { file: "components/assistant/AssistantChat.tsx", needle: "{x}…", why: "truncation marker" },
  { file: "components/assistant/items/ToolLine.tsx", needle: "{x}…", why: "truncation marker" },
  { file: "components/flow/geometry.ts", needle: "{x}…", why: "truncation marker" },
  { file: "components/assistant/items/ToolGroup.tsx", needle: "{x}{x}…", why: "progress label" },
  { file: "components/tickets/refreshNote.ts", needle: "refreshing…", why: "progress label" },
  { file: "components/ui/DataTable.tsx", needle: "…", why: "pagination gap, aria-hidden" },
  { file: "components/activity/Transcript.tsx", needle: "›", why: "log gutter mark, not prose" },
];

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    if (!/\.tsx?$/.test(name) || /\.(test|fixture)\.tsx?$/.test(name)) return [];
    return name === "testDom.tsx" ? [] : [path];
  });
}

function isModulePath(node: ts.Node): boolean {
  const parent = node.parent;
  return (
    ts.isImportDeclaration(parent) ||
    ts.isExportDeclaration(parent) ||
    ts.isExternalModuleReference(parent) ||
    (ts.isCallExpression(parent) && parent.expression.kind === ts.SyntaxKind.ImportKeyword)
  );
}

function tokens(path: string): string[] {
  const text = readFileSync(path, "utf8");
  const file = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const out: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) out.push(node.text);
    else if (ts.isStringLiteralLike(node) && !isModulePath(node)) out.push(node.text);
    else if (ts.isTemplateExpression(node)) {
      out.push(node.head.text + node.templateSpans.map((s) => `{x}${s.literal.text}`).join(""));
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return out;
}

function hits(): { at: string; rule: string; text: string }[] {
  const found: { at: string; rule: string; text: string }[] = [];
  for (const path of sources(SRC)) {
    const at = relative(SRC, path);
    for (const text of tokens(path)) {
      if (ALLOWED.some((a) => a.file === at && text.includes(a.needle))) continue;
      for (const [rule, pattern] of RULES) {
        if (pattern.test(text)) found.push({ at, rule, text });
      }
      if (text.includes("…") && !PROGRESS_LABEL.test(text.trim())) {
        found.push({ at, rule: "ellipsis outside a progress label", text });
      }
    }
  }
  return found;
}

describe("the dashboard copy", () => {
  it("has no em dash, arrow, glyph, stray ellipsis, 'we' or UK spelling", () => {
    expect(hits()).toEqual([]);
  });

  it("never tests a value against the NO_VALUE placeholder", () => {
    const sentinel = /[=!]==?\s*NO_VALUE\b|\bNO_VALUE\s*[=!]==?/;
    const compared = sources(SRC).filter((path) => sentinel.test(readFileSync(path, "utf8")));
    expect(compared.map((path) => relative(SRC, path))).toEqual([]);
  });

  it("uses every allowlist entry, so a fixed line drops its exception", () => {
    const unused = ALLOWED.filter(
      (a) => !tokens(join(SRC, a.file)).some((text) => text.includes(a.needle)),
    );
    expect(unused).toEqual([]);
  });
});

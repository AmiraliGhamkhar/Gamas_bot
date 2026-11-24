import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Anti-slop guardrail (miniapp/DESIGN.md): components never hardcode colour —
 * the theme lives in `tailwind.config.js` and `index.css` only — and the
 * banned-effects list from the design checklist is enforced here, the same
 * way `tests/test_booklet_design.py` polices the document engine.
 */

const SRC = join(process.cwd(), "src");

function collectTsxFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "test" || entry === "assets") continue;
      out.push(...collectTsxFiles(full));
    } else if (entry.endsWith(".tsx")) {
      out.push(full);
    }
  }
  return out.sort();
}

const HEX_LITERAL = /#[0-9A-Fa-f]{3,8}\b/;
const BANNED_EFFECTS = [/backdrop-blur/, /bg-gradient/, /drop-shadow-glow/];

const files = collectTsxFiles(SRC);
const label = (file: string) => file.replace(SRC, "src");

describe("anti-slop design lint", () => {
  it("lints every component file", () => {
    expect(files.length).toBeGreaterThan(5);
    expect(files.map(label)).toContain("src/App.tsx");
    expect(files.map(label)).toContain("src/ui/primitives.tsx");
  });

  it("components carry no raw colour literal", () => {
    const violations = files
      .filter((file) => HEX_LITERAL.test(readFileSync(file, "utf8")))
      .map(label);
    expect(violations).toEqual([]);
  });

  it("components use no gradients, glass or glow", () => {
    const violations: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      if (BANNED_EFFECTS.some((pattern) => pattern.test(source))) {
        violations.push(label(file));
      }
    }
    expect(violations).toEqual([]);
  });
});

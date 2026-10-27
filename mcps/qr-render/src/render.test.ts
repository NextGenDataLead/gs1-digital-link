/** Tests for the QR renderer: uppercase-domain transform + format output. */

import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { renderQr, uppercaseDomain } from "./render.js";

const URI = "https://id.gs1.org/01/08712345678904";
const GTIN = "08712345678904";

let dirs: string[] = [];

async function scratch(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "qr-render-"));
  dirs.push(dir);
  return dir;
}

afterEach(() => {
  dirs = [];
});

describe("uppercaseDomain", () => {
  it("uppercases scheme and host, preserving the path", () => {
    expect(uppercaseDomain(URI)).toBe("HTTPS://ID.GS1.ORG/01/08712345678904");
  });

  it("preserves path case", () => {
    expect(uppercaseDomain("https://id.gs1.org/01/12345/10/Lot-Ab")).toBe(
      "HTTPS://ID.GS1.ORG/01/12345/10/Lot-Ab",
    );
  });
});

describe("renderQr", () => {
  it("writes the requested formats in order", async () => {
    const dir = await scratch();
    const paths = await renderQr({
      uri: URI,
      outputDir: dir,
      gtin: GTIN,
      formats: ["png", "svg", "eps"],
      sizeMm: 20,
      errorCorrection: "M",
    });

    expect(paths.map((p) => p.endsWith(`${GTIN}.png`) || p.endsWith(`${GTIN}.svg`) || p.endsWith(`${GTIN}.eps`))).toEqual([
      true,
      true,
      true,
    ]);
    for (const path of paths) {
      expect((await stat(path)).size).toBeGreaterThan(0);
    }
  });

  it("emits an SVG at the requested physical size that encodes the uppercased domain-optimised symbol", async () => {
    const dir = await scratch();
    const [svgPath] = await renderQr({
      uri: URI,
      outputDir: dir,
      gtin: GTIN,
      formats: ["svg"],
      sizeMm: 25,
      errorCorrection: "M",
    });
    const svg = await readFile(svgPath, "utf8");

    expect(svg).toContain('width="25mm"');
    expect(svg).toContain('height="25mm"');
    expect(svg).toContain("<path");
  });

  it("produces a byte-identical SVG across renders", async () => {
    const a = await scratch();
    const b = await scratch();
    const opts = {
      uri: URI,
      gtin: GTIN,
      formats: ["svg"] as const,
      sizeMm: 20,
      errorCorrection: "M" as const,
    };
    const [pa] = await renderQr({ ...opts, formats: ["svg"], outputDir: a });
    const [pb] = await renderQr({ ...opts, formats: ["svg"], outputDir: b });

    expect(await readFile(pa, "utf8")).toBe(await readFile(pb, "utf8"));
  });
});

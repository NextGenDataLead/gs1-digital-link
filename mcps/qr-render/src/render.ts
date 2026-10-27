/**
 * QR rendering for GS1 Digital Link URIs (IMPLEMENTATION_SPEC §4.7, §9.3).
 *
 * TS-native counterpart to `lib/qr.py`: the scheme + host are uppercased (path preserved)
 * for the alphanumeric-mode size optimisation, and each requested format is written to
 * `{output_dir}/{gtin}.{ext}`. SVG and EPS are emitted directly from the QR module matrix
 * (npm `qrcode` has no EPS writer) so all three formats stay consistent; PNG is rasterised
 * by `qrcode` itself.
 */

import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

import QRCode from "qrcode";
import type { QRCodeErrorCorrectionLevel } from "qrcode";

export type QrFormat = "svg" | "png" | "eps";
export type ErrorCorrection = "L" | "M" | "Q" | "H";

export interface RenderOptions {
  uri: string;
  outputDir: string;
  gtin: string;
  formats: QrFormat[];
  sizeMm: number;
  errorCorrection: ErrorCorrection;
  /** Raster resolution for PNG; the vector SVG/EPS ignore it. Defaults to 300. */
  dpi?: number;
}

/** Standard QR quiet zone, in modules (§4.6). */
const QUIET_ZONE = 4;
const MM_PER_INCH = 25.4;
const PT_PER_MM = 72 / MM_PER_INCH;
const DEFAULT_DPI = 300;

/** Uppercase the scheme and authority (host) of `uri`; preserve path/query/fragment case. */
export function uppercaseDomain(uri: string): string {
  const match = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/([^/?#]*)([\s\S]*)$/.exec(uri);
  if (match === null) {
    return uri;
  }
  const [, scheme, authority, rest] = match;
  return `${scheme.toUpperCase()}://${authority.toUpperCase()}${rest}`;
}

/** Render `opts.uri` into QR files, one per requested format; returns written paths. */
export async function renderQr(opts: RenderOptions): Promise<string[]> {
  const payload = uppercaseDomain(opts.uri);
  const dpi = opts.dpi ?? DEFAULT_DPI;
  await mkdir(opts.outputDir, { recursive: true });

  const qr = QRCode.create(payload, { errorCorrectionLevel: opts.errorCorrection });
  const matrix = qr.modules;

  const paths: string[] = [];
  for (const fmt of opts.formats) {
    const path = join(opts.outputDir, `${opts.gtin}.${fmt}`);
    if (fmt === "svg") {
      await writeFile(path, toSvg(matrix, opts.sizeMm), "utf8");
    } else if (fmt === "eps") {
      await writeFile(path, toEps(matrix, opts.sizeMm), "utf8");
    } else {
      const pixelEdge = Math.round((opts.sizeMm / MM_PER_INCH) * dpi);
      await QRCode.toFile(path, payload, {
        type: "png",
        errorCorrectionLevel: opts.errorCorrection as QRCodeErrorCorrectionLevel,
        margin: QUIET_ZONE,
        width: pixelEdge,
      });
    }
    paths.push(path);
  }
  return paths;
}

interface BitMatrix {
  size: number;
  get(row: number, col: number): number;
}

/** Emit a deterministic SVG scaled to `sizeMm`, with the quiet zone added around `matrix`. */
function toSvg(matrix: BitMatrix, sizeMm: number): string {
  const n = matrix.size + 2 * QUIET_ZONE;
  let d = "";
  for (let row = 0; row < matrix.size; row += 1) {
    for (let col = 0; col < matrix.size; col += 1) {
      if (matrix.get(row, col)) {
        d += `M${col + QUIET_ZONE} ${row + QUIET_ZONE}h1v1h-1z`;
      }
    }
  }
  return (
    '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<svg xmlns="http://www.w3.org/2000/svg" ' +
    `width="${sizeMm}mm" height="${sizeMm}mm" ` +
    `viewBox="0 0 ${n} ${n}" shape-rendering="crispEdges">\n` +
    `<rect width="${n}" height="${n}" fill="#ffffff"/>\n` +
    `<path d="${d}" fill="#000000"/>\n` +
    "</svg>\n"
  );
}

/** Emit a deterministic EPS scaled to `sizeMm` (PostScript origin is bottom-left). */
function toEps(matrix: BitMatrix, sizeMm: number): string {
  const n = matrix.size + 2 * QUIET_ZONE;
  const edgePt = sizeMm * PT_PER_MM;
  const module = edgePt / n;
  const box = Math.ceil(edgePt);
  const lines = [
    "%!PS-Adobe-3.0 EPSF-3.0",
    `%%BoundingBox: 0 0 ${box} ${box}`,
    `%%HiResBoundingBox: 0 0 ${edgePt} ${edgePt}`,
    "%%EndComments",
    `1 setgray 0 0 ${edgePt} ${edgePt} rectfill`,
    "0 setgray",
  ];
  for (let row = 0; row < matrix.size; row += 1) {
    for (let col = 0; col < matrix.size; col += 1) {
      if (matrix.get(row, col)) {
        const x = (col + QUIET_ZONE) * module;
        const y = (n - 1 - (row + QUIET_ZONE)) * module;
        lines.push(`${x} ${y} ${module} ${module} rectfill`);
      }
    }
  }
  lines.push("%%EOF");
  return `${lines.join("\n")}\n`;
}

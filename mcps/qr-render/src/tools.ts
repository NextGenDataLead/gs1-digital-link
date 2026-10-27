/**
 * The single QR-render MCP tool (IMPLEMENTATION_SPEC §9.3).
 *
 * `qr_render` takes a fully explicit request (no `client_id`, no `clients.yml` lookup) and
 * writes the requested formats to disk, returning their paths in the `{ok, error}` envelope.
 * The MCP parameter is `error_correction`; the renderer's field is `errorCorrection`.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import type { RenderOptions } from "./render.js";
import { renderQr } from "./render.js";

const qrRenderShape = {
  uri: z.string(),
  output_dir: z.string(),
  gtin: z.string(),
  formats: z.array(z.enum(["svg", "png", "eps"])),
  size_mm: z.number().int(),
  error_correction: z.enum(["L", "M", "Q", "H"]),
};

/** Dependencies, injectable so the tool can be tested without touching the filesystem. */
export interface ToolDeps {
  render: (opts: RenderOptions) => Promise<string[]>;
}

const defaultDeps: ToolDeps = { render: renderQr };

type ToolResult = {
  content: { type: "text"; text: string }[];
  isError?: boolean;
};

function ok(payload: Record<string, unknown>): ToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify({ ok: true, error: null, ...payload }) }],
  };
}

function fail(error: unknown): ToolResult {
  const message = error instanceof Error ? error.message : String(error);
  return {
    content: [{ type: "text", text: JSON.stringify({ ok: false, error: message }) }],
    isError: true,
  };
}

/** Register the `qr_render` tool on an MCP server. */
export function registerQrTools(server: McpServer, deps: ToolDeps = defaultDeps): void {
  server.registerTool(
    "qr_render",
    {
      description: "Render a QR symbol for a Digital Link URI to svg/png/eps files.",
      inputSchema: qrRenderShape,
    },
    async ({ uri, output_dir, gtin, formats, size_mm, error_correction }) => {
      try {
        const paths = await deps.render({
          uri,
          outputDir: output_dir,
          gtin,
          formats,
          sizeMm: size_mm,
          errorCorrection: error_correction,
        });
        return ok({ paths });
      } catch (err) {
        return fail(err);
      }
    },
  );
}

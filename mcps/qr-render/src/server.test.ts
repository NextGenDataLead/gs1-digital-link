/**
 * End-to-end MCP wiring test: a real MCP Client calls the tool over an in-memory
 * transport, with the renderer mocked via injected deps. Verifies `qr_render` is
 * registered and returns the documented {ok, error} shape.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { describe, expect, it } from "vitest";

import { createServer } from "./server.js";
import type { ToolDeps } from "./tools.js";

async function connectClient(deps: ToolDeps): Promise<Client> {
  const server = createServer(deps);
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "test", version: "1.0.0" });
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
  return client;
}

function parse(result: { content: unknown[] }): Record<string, unknown> {
  const first = result.content[0] as { type: string; text: string };
  return JSON.parse(first.text) as Record<string, unknown>;
}

const ARGS = {
  uri: "https://id.gs1.org/01/08712345678904",
  output_dir: "/tmp/qr",
  gtin: "08712345678904",
  formats: ["svg", "png"],
  size_mm: 20,
  error_correction: "M",
};

describe("qr-render MCP tools", () => {
  it("lists the qr_render tool", async () => {
    const client = await connectClient({ render: async () => [] });
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name)).toEqual(["qr_render"]);
  });

  it("qr_render returns ok with the written paths", async () => {
    const paths = ["/tmp/qr/08712345678904.svg", "/tmp/qr/08712345678904.png"];
    const client = await connectClient({ render: async () => paths });
    const result = (await client.callTool({
      name: "qr_render",
      arguments: ARGS,
    })) as { content: unknown[] };

    expect(parse(result)).toEqual({ ok: true, error: null, paths });
  });

  it("maps snake_case tool args onto the renderer's camelCase options", async () => {
    let received: Record<string, unknown> | undefined;
    const client = await connectClient({
      render: async (opts) => {
        received = opts as unknown as Record<string, unknown>;
        return [];
      },
    });
    await client.callTool({ name: "qr_render", arguments: ARGS });

    expect(received).toMatchObject({
      uri: ARGS.uri,
      outputDir: ARGS.output_dir,
      gtin: ARGS.gtin,
      formats: ARGS.formats,
      sizeMm: ARGS.size_mm,
      errorCorrection: ARGS.error_correction,
    });
  });

  it("reports a renderer error as ok:false", async () => {
    const client = await connectClient({
      render: async () => {
        throw new Error("disk full");
      },
    });
    const result = (await client.callTool({
      name: "qr_render",
      arguments: ARGS,
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    const body = parse(result);
    expect(body.ok).toBe(false);
    expect(String(body.error)).toContain("disk full");
  });
});

/**
 * End-to-end MCP wiring test: a real MCP Client calls each tool over an in-memory
 * transport, with the WordPress backend mocked via injected deps. Verifies the five
 * tools are registered and callable and return the documented {ok, error} shape.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ElicitRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, it } from "vitest";

import {
  type WordPressClient,
  type WordPressClientConfig,
  WordPressApiError,
} from "./client.js";
import { createServer } from "./server.js";
import type { ToolDeps } from "./tools.js";

const CONFIG: WordPressClientConfig = {
  siteUrl: "https://staging.example.com",
  username: "gs1-publisher",
  appPassword: "app-pass",
  postType: "democlient",
  postStatus: "publish",
  multilingualPlugin: "none",
  defaultLanguage: "nl",
  languages: ["nl", "fr"],
};

/**
 * Connect a client that can answer gates.
 *
 * `answer: null` connects a client declaring **no** elicitation capability — the case that must
 * refuse the write rather than proceed unattended.
 */
async function connectClient(
  deps: ToolDeps,
  answer: boolean | "cancel" | null = true,
): Promise<{ client: Client; prompts: string[] }> {
  const server = createServer(deps);
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const prompts: string[] = [];
  const client = new Client(
    { name: "test", version: "1.0.0" },
    answer === null ? {} : { capabilities: { elicitation: {} } },
  );
  if (answer !== null) {
    client.setRequestHandler(ElicitRequestSchema, (request) => {
      prompts.push(request.params.message);
      if (answer === "cancel") {
        return { action: "cancel" as const };
      }
      return { action: "accept" as const, content: { confirm: answer } };
    });
  }
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
  return { client, prompts };
}

function depsFor(fake: Partial<WordPressClient>): ToolDeps {
  return {
    loadConfig: () => CONFIG,
    makeClient: () => fake as unknown as WordPressClient,
  };
}

function parse(result: { content: unknown[] }): Record<string, unknown> {
  const first = result.content[0] as { type: string; text: string };
  return JSON.parse(first.text) as Record<string, unknown>;
}

describe("wordpress MCP tools", () => {
  it("lists the five tools", async () => {
    const { client } = await connectClient(depsFor({}));
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual([
      "wp_detect_multilingual",
      "wp_find_by_slug",
      "wp_upload_media",
      "wp_upsert_page",
      "wp_verify_url",
    ]);
  });

  it("upsert_page returns ok with the page", async () => {
    const page = { id: 10, slug: "p-1" };
    const { client } = await connectClient(depsFor({ upsertPage: async () => page }));
    const result = (await client.callTool({
      name: "wp_upsert_page",
      arguments: {
        client_id: "democlient",
        slug: "p-1",
        title: "T",
        content: "B",
        language: "nl",
        meta: { gtin: "1" },
      },
    })) as { content: unknown[] };
    expect(parse(result)).toEqual({ ok: true, error: null, page });
  });

  it("detect_multilingual returns the plugin", async () => {
    const { client } = await connectClient(
      depsFor({ detectMultilingualPlugin: async () => "polylang" }),
    );
    const result = (await client.callTool({
      name: "wp_detect_multilingual",
      arguments: { client_id: "democlient" },
    })) as { content: unknown[] };
    expect(parse(result)).toEqual({ ok: true, error: null, plugin: "polylang" });
  });

  it("verify_url returns ok_url", async () => {
    const { client } = await connectClient(depsFor({ verifyUrl: async () => true }));
    const result = (await client.callTool({
      name: "wp_verify_url",
      arguments: { client_id: "democlient", url: "https://staging.example.com/p/1" },
    })) as { content: unknown[] };
    expect(parse(result)).toEqual({ ok: true, error: null, ok_url: true });
  });

  it("reports a WordPress API error as ok:false", async () => {
    const { client } = await connectClient(
      depsFor({
        upsertPage: async () => {
          throw new WordPressApiError(409, "slug exists");
        },
      }),
    );
    const result = (await client.callTool({
      name: "wp_upsert_page",
      arguments: {
        client_id: "democlient",
        slug: "p-1",
        title: "T",
        content: "B",
        language: "nl",
        meta: { gtin: "1" },
      },
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    const body = parse(result);
    expect(body.ok).toBe(false);
    expect(String(body.error)).toContain("409");
  });
});

describe("operator gates on the writes", () => {
  const upsertArgs = {
    client_id: "democlient",
    slug: "p-1",
    title: "T",
    content: "B",
    language: "nl",
    meta: { gtin: "1" },
  };

  /** Fails the test if reached — the point of a refusal is that nothing runs. */
  function neverCalled(): Partial<WordPressClient> {
    return {
      upsertPage: async () => {
        throw new Error("upsertPage must not be reached when the gate refuses");
      },
      uploadMedia: async () => {
        throw new Error("uploadMedia must not be reached when the gate refuses");
      },
    };
  }

  it("refuses the write when the client cannot ask a human", async () => {
    const { client } = await connectClient(depsFor(neverCalled()), null);

    const result = (await client.callTool({
      name: "wp_upsert_page",
      arguments: upsertArgs,
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    expect(String(parse(result).error)).toMatch(/cannot reach an operator/);
  });

  it("refuses the write when the operator declines", async () => {
    const { client } = await connectClient(depsFor(neverCalled()), false);

    const result = (await client.callTool({
      name: "wp_upsert_page",
      arguments: upsertArgs,
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    expect(String(parse(result).error)).toMatch(/refused at the intent gate/);
  });

  it("treats a dismissed prompt as a refusal, never as consent", async () => {
    const { client } = await connectClient(depsFor(neverCalled()), "cancel");

    const result = (await client.callTool({
      name: "wp_upsert_page",
      arguments: upsertArgs,
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    expect(String(parse(result).error)).toMatch(/cancel/);
  });

  it("shows the site, the slug and why the gate exists", async () => {
    const { client, prompts } = await connectClient(
      depsFor({ upsertPage: async () => ({ id: 10 }) }),
    );

    await client.callTool({ name: "wp_upsert_page", arguments: upsertArgs });

    expect(prompts).toHaveLength(1);
    expect(prompts[0]).toContain(CONFIG.siteUrl);
    expect(prompts[0]).toContain("p-1");
    // The purpose, not only the question: a gate that shows only the question trains an operator
    // to answer it without reading.
    expect(prompts[0]).toMatch(/export|scope|environment/i);
  });

  it("gates the media upload too", async () => {
    const { client, prompts } = await connectClient(depsFor({ uploadMedia: async () => 5 }));

    await client.callTool({
      name: "wp_upload_media",
      arguments: { client_id: "democlient", file_path: "/tmp/clip.mp4" },
    });

    expect(prompts[0]).toContain("/tmp/clip.mp4");
  });

  it("leaves the read-only tools ungated", async () => {
    const { client, prompts } = await connectClient(
      depsFor({ verifyUrl: async () => true, findBySlug: async () => null }),
      null,
    );

    const verify = await client.callTool({
      name: "wp_verify_url",
      arguments: { client_id: "democlient", url: "https://staging.example.com/p/1" },
    });
    const find = await client.callTool({
      name: "wp_find_by_slug",
      arguments: { client_id: "democlient", slug: "p-1" },
    });

    expect(parse(verify as { content: unknown[] }).ok).toBe(true);
    expect(parse(find as { content: unknown[] }).ok).toBe(true);
    expect(prompts).toHaveLength(0);
  });
});

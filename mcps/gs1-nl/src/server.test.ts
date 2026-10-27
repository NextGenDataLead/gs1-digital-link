/**
 * End-to-end MCP wiring test: a real MCP Client calls each tool over an in-memory
 * transport, with the GS1 backend mocked via injected deps. Verifies the tools are
 * registered and callable and return the documented {ok, error} shape.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { ElicitRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, it } from "vitest";

import {
  GS1ApiError,
  type GS1Client,
  type GS1ClientConfig,
  type UpsertEntry,
} from "./client.js";
import { createServer } from "./server.js";
import type { ToolDeps } from "./tools.js";

const CONFIG: GS1ClientConfig = {
  host: "gs1nl-api-acc.gs1.nl",
  environment: "test",
  accountNumber: "8712345000010",
  clientId: "client-id",
  clientSecret: "client-secret",
  resolverSettings: { useGS1Resolver: true, resolverDomainName: null },
  batchSize: 50,
};

const PRODUCTION_CONFIG: GS1ClientConfig = { ...CONFIG, environment: "production" };

interface FakeClient {
  upsert: (entry: UpsertEntry) => Promise<void>;
  upsertBulk: (entries: UpsertEntry[]) => Promise<{
    total: number;
    batches: number;
    status_codes: number[];
  }>;
  get: (gtin: string) => Promise<Record<string, unknown> | null>;
}

/** Records what the operator was asked, and answers with `answer`. */
interface GatePrompts {
  messages: string[];
}

/**
 * Connect a client that can answer gates.
 *
 * `answer: null` connects a client declaring **no** elicitation capability — the case that must
 * refuse the write rather than proceed unattended.
 */
async function connectClient(
  deps: ToolDeps,
  answer: boolean | "cancel" | null = true,
): Promise<{ client: Client; prompts: GatePrompts }> {
  const server = createServer(deps);
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const prompts: GatePrompts = { messages: [] };
  const client = new Client(
    { name: "test", version: "1.0.0" },
    answer === null ? {} : { capabilities: { elicitation: {} } },
  );
  if (answer !== null) {
    client.setRequestHandler(ElicitRequestSchema, (request) => {
      prompts.messages.push(request.params.message);
      if (answer === "cancel") {
        return { action: "cancel" as const };
      }
      return { action: "accept" as const, content: { confirm: answer } };
    });
  }
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
  return { client, prompts };
}

function depsFor(fake: Partial<FakeClient>, config: GS1ClientConfig = CONFIG): ToolDeps {
  return {
    loadConfig: () => config,
    makeClient: () => fake as unknown as GS1Client,
  };
}

function parse(result: { content: unknown[] }): Record<string, unknown> {
  const first = result.content[0] as { type: string; text: string };
  return JSON.parse(first.text) as Record<string, unknown>;
}

describe("gs1-nl MCP tools", () => {
  it("lists the three tools", async () => {
    const { client } = await connectClient(depsFor({}));
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual([
      "gs1_digital_link_get",
      "gs1_digital_link_upsert",
      "gs1_digital_link_upsert_bulk",
    ]);
  });

  it("upsert returns ok on success", async () => {
    const { client } = await connectClient(depsFor({ upsert: async () => {} }));
    const result = await client.callTool({
      name: "gs1_digital_link_upsert",
      arguments: {
        client_id: "democlient",
        gtin: "8712345678905",
        item_description: "Test",
        links: [
          {
            link_type: "pip",
            language: "nl",
            link_title: "Product",
            target_url: "https://example.com/p",
            default_link_type: true,
            public: true,
            media_type: "text/html",
          },
        ],
      },
    });
    expect(parse(result as { content: unknown[] })).toEqual({
      ok: true,
      error: null,
      gtin: "8712345678905",
    });
  });

  it("get returns the record", async () => {
    const record = { identificationKey: "08712345678905", isEnabled: true };
    const { client } = await connectClient(depsFor({ get: async () => record }));
    const result = await client.callTool({
      name: "gs1_digital_link_get",
      arguments: { client_id: "democlient", gtin: "8712345678905" },
    });
    expect(parse(result as { content: unknown[] })).toEqual({ ok: true, error: null, record });
  });

  it("reports a GS1 API error as ok:false", async () => {
    const errorResults = [{ identifier: "08712345678905", errors: [{ code: "X", message: "bad" }] }];
    const { client } = await connectClient(
      depsFor({
        upsert: async () => {
          throw new GS1ApiError(400, "bad", errorResults);
        },
      }),
    );
    const result = (await client.callTool({
      name: "gs1_digital_link_upsert",
      arguments: {
        client_id: "democlient",
        gtin: "8712345678905",
        item_description: "Test",
        links: [],
      },
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    const body = parse(result);
    expect(body.ok).toBe(false);
    expect(body.error_results).toEqual(errorResults);
  });
});

describe("operator gates on the permanent writes", () => {
  const upsertArgs = {
    client_id: "democlient",
    gtin: "8712345678905",
    item_description: "Test",
    links: [
      {
        link_type: "pip",
        language: "nl",
        link_title: "Product",
        target_url: "https://example.com/p",
        default_link_type: true,
        public: true,
        media_type: "text/html",
      },
    ],
  };

  /** Fails the test if the client is ever reached — the point of a refusal is that nothing runs. */
  function neverCalled(): Partial<FakeClient> {
    return {
      upsert: async () => {
        throw new Error("upsert must not be reached when the gate refuses");
      },
      upsertBulk: async () => {
        throw new Error("upsertBulk must not be reached when the gate refuses");
      },
    };
  }

  it("refuses the write when the client cannot ask a human", async () => {
    // The whole point: a client with no elicitation capability cannot reach an operator, so it
    // must not write. Proceeding unattended is the hole this closes.
    const { client } = await connectClient(depsFor(neverCalled()), null);

    const result = (await client.callTool({
      name: "gs1_digital_link_upsert",
      arguments: upsertArgs,
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    expect(String(parse(result).error)).toMatch(/cannot reach an operator/);
  });

  it("refuses the write when the operator declines", async () => {
    const { client } = await connectClient(depsFor(neverCalled()), false);

    const result = (await client.callTool({
      name: "gs1_digital_link_upsert",
      arguments: upsertArgs,
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    expect(String(parse(result).error)).toMatch(/refused at the intent gate/);
  });

  it("treats a dismissed prompt as a refusal, never as consent", async () => {
    const { client } = await connectClient(depsFor(neverCalled()), "cancel");

    const result = (await client.callTool({
      name: "gs1_digital_link_upsert",
      arguments: upsertArgs,
    })) as { content: unknown[]; isError?: boolean };

    expect(result.isError).toBe(true);
    expect(String(parse(result).error)).toMatch(/cancel/);
  });

  it("asks once in test, and warns that nothing is permanent there", async () => {
    const { client, prompts } = await connectClient(depsFor({ upsert: async () => {} }));

    await client.callTool({ name: "gs1_digital_link_upsert", arguments: upsertArgs });

    expect(prompts.messages).toHaveLength(1);
    expect(prompts.messages[0]).toContain("Intent confirmation");
    expect(prompts.messages[0]).toMatch(/Permanent: no \(test environment\)/);
  });

  it("asks twice against production, the second being the production gate", async () => {
    const { client, prompts } = await connectClient(
      depsFor({ upsert: async () => {} }, PRODUCTION_CONFIG),
    );

    await client.callTool({ name: "gs1_digital_link_upsert", arguments: upsertArgs });

    // Two separate decisions, in the skill's order — a merged prompt would lose the property that
    // makes the second one work: it is asked after the first has been answered.
    expect(prompts.messages).toHaveLength(2);
    expect(prompts.messages[0]).toContain("Intent confirmation");
    expect(prompts.messages[0]).toMatch(/can never be deleted/);
    expect(prompts.messages[1]).toContain("Production environment confirmation");
  });

  it("gates the bulk write too, naming the GTIN count", async () => {
    const { client, prompts } = await connectClient(
      depsFor({ upsertBulk: async () => ({ total: 2, batches: 1, status_codes: [200] }) }),
    );

    await client.callTool({
      name: "gs1_digital_link_upsert_bulk",
      arguments: {
        client_id: "democlient",
        entries: [
          { ...upsertArgs, gtin: "8712345678905" },
          { ...upsertArgs, gtin: "8712345678912" },
        ].map(({ client_id: _client_id, ...e }) => e),
      },
    });

    expect(prompts.messages[0]).toMatch(/2 GS1 Digital Link record\(s\), in bulk/);
  });

  it("leaves the read-only tool ungated", async () => {
    const { client, prompts } = await connectClient(depsFor({ get: async () => null }), null);

    const result = await client.callTool({
      name: "gs1_digital_link_get",
      arguments: { client_id: "democlient", gtin: "8712345678905" },
    });

    expect(parse(result as { content: unknown[] }).ok).toBe(true);
    expect(prompts.messages).toHaveLength(0);
  });
});

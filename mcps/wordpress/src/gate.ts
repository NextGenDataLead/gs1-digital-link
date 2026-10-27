/**
 * Put a write tool behind an operator gate (`lib/gates.py`).
 *
 * The gates are the safety mechanism of this project, and until now this server had none: a model
 * could call `wp_upsert_page` and the page was written, with no human between the decision and the
 * live site. `CLAUDE.md` says publishing goes through `flow-orchestrator` precisely because
 * "calling `scripts/run_execute.py` directly bypasses every one of them" — an ungated tool call is
 * the same bypass wearing a different hat.
 *
 * **Elicitation is what makes this a gate rather than a speed bump.** `server.elicitInput` asks the
 * *client's human*, out of band from the model driving the conversation, so the caller cannot
 * satisfy its own gate. A prompt the model answers would be theatre.
 *
 * **It fails closed.** The SDK throws when the client has not declared the `elicitation` capability
 * (`server/index.js:158-161`), so a client that cannot ask a human cannot write either. That is the
 * deliberate behaviour, not an edge case to smooth over: the alternative — proceeding when no one
 * can be asked — is exactly the hole this closes. {@link GateUnavailableError} says so in words the
 * operator can act on.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { RequestHandlerExtra } from "@modelcontextprotocol/sdk/shared/protocol.js";
import type { ServerNotification, ServerRequest } from "@modelcontextprotocol/sdk/types.js";

import { gateById } from "./gates.generated.js";

/** The field the operator answers. Flat, because elicitation allows no nesting. */
const CONFIRM_KEY = "confirm";

/** Raised when the operator refused, or dismissed without choosing. Never treat as consent. */
export class GateRefusedError extends Error {
  constructor(
    readonly gateId: string,
    readonly action: "decline" | "cancel",
  ) {
    super(
      `refused at the ${gateId} gate (${action}); nothing was written. ` +
        `A dismissed prompt is a refusal, not an approval.`,
    );
    this.name = "GateRefusedError";
  }
}

/** Raised when no human could be asked, so the write was refused. */
export class GateUnavailableError extends Error {
  constructor(
    readonly gateId: string,
    cause: unknown,
  ) {
    super(
      `cannot reach an operator for the ${gateId} gate, so nothing was written. This tool writes ` +
        `to a live site and will not do so unattended. Connect a client that supports MCP ` +
        `elicitation, or publish through the flow-orchestrator skill, which carries every gate. ` +
        `(${String(cause)})`,
    );
    this.name = "GateUnavailableError";
  }
}

/** What the operator is about to change — rendered into the prompt, one `label: value` per line. */
export type GateFacts = Readonly<Record<string, string>>;

export type GateExtra = RequestHandlerExtra<ServerRequest, ServerNotification>;

function promptText(gateId: string, facts: GateFacts): string {
  const gate = gateById(gateId);
  const lines = Object.entries(facts).map(([k, v]) => `  ${k}: ${v}`);
  // The purpose is included, not just the question. `lib/gates.py` puts it this way: "a UI that
  // shows only the question trains an operator to answer it without reading, and the why is what
  // stops that."
  return [
    `${gate.title} (step ${gate.step})`,
    "",
    ...lines,
    "",
    gate.purpose,
    "",
    "Proceed?",
  ].join("\n");
}

/**
 * Ask the operator to confirm, or throw.
 *
 * Resolves only on an explicit accept **and** an explicit `true`. Everything else — decline,
 * cancel, an accept carrying `false`, a malformed payload — refuses, because the one outcome that
 * must never be inferred wrong is consent.
 */
export async function requireGate(
  server: McpServer,
  extra: GateExtra,
  gateId: string,
  facts: GateFacts,
): Promise<void> {
  const gate = gateById(gateId);
  let result;
  try {
    result = await server.server.elicitInput(
      {
        message: promptText(gateId, facts),
        requestedSchema: {
          type: "object",
          properties: {
            [CONFIRM_KEY]: {
              type: "boolean",
              title: gate.title,
              description: `Yes writes to the live site. No stops the run and writes nothing.`,
            },
          },
          required: [CONFIRM_KEY],
        },
      },
      { signal: extra.signal },
    );
  } catch (err) {
    throw new GateUnavailableError(gateId, err);
  }

  if (result.action !== "accept") {
    throw new GateRefusedError(gateId, result.action);
  }
  if (result.content?.[CONFIRM_KEY] !== true) {
    // An accepted form carrying "no" is a refusal; only the value means anything.
    throw new GateRefusedError(gateId, "decline");
  }
}

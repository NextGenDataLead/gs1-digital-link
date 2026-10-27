#!/usr/bin/env node
/** Entry point: serve the qr-render MCP server over stdio. */

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

import { createServer } from "./server.js";

async function main(): Promise<void> {
  const server = createServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err: unknown) => {
  // stderr only — stdout is the MCP protocol channel.
  console.error(err);
  process.exit(1);
});

/** Construct the qr-render MCP server with its single tool registered. */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

import { registerQrTools, type ToolDeps } from "./tools.js";

export function createServer(deps?: ToolDeps): McpServer {
  const server = new McpServer({ name: "qr-render-mcp", version: "0.0.1" });
  registerQrTools(server, deps);
  return server;
}

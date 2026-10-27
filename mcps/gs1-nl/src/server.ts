/** Construct the gs1-nl MCP server with its three tools registered. */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

import { registerGs1Tools, type ToolDeps } from "./tools.js";

export function createServer(deps?: ToolDeps): McpServer {
  const server = new McpServer({ name: "gs1-nl-mcp", version: "0.0.1" });
  registerGs1Tools(server, deps);
  return server;
}

/** Construct the wordpress MCP server with its five tools registered. */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

import { registerWordPressTools, type ToolDeps } from "./tools.js";

export function createServer(deps?: ToolDeps): McpServer {
  const server = new McpServer({ name: "wordpress-mcp", version: "0.0.1" });
  registerWordPressTools(server, deps);
  return server;
}

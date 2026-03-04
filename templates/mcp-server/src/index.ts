import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const server = new McpServer({
  name: "near-mcp-server",
  version: "1.0.0",
});

// TODO: Register tools here based on job requirements
// Example:
// server.tool("tool_name", "Description", { param: { type: "string" } }, async (args) => {
//   return { content: [{ type: "text", text: "result" }] };
// });

const transport = new StdioServerTransport();
await server.connect(transport);

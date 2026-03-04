import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

// Tool input schemas
const ExampleInputSchema = z.object({
  query: z.string().describe("The search query or input"),
});

// Create server instance
const server = new Server(
  { name: "mcp-server-template", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

// List available tools
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "example_tool",
      description: "An example tool — replace with your implementation",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string", description: "The search query or input" },
        },
        required: ["query"],
      },
    },
  ],
}));

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  switch (name) {
    case "example_tool": {
      const { query } = ExampleInputSchema.parse(args);
      return {
        content: [
          { type: "text" as const, text: `Result for: ${query}` },
        ],
      };
    }
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
});

// Start server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("MCP server running on stdio");
}

main().catch(console.error);

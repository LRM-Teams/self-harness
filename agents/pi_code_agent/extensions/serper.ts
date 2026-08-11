import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SearchParams = Type.Object({
  query: Type.String({ description: "Google search query" }),
  num_results: Type.Optional(
    Type.Number({ description: "Number of results (1-10)", minimum: 1, maximum: 10 }),
  ),
});

const serperSearch = defineTool({
  name: "serper_search",
  label: "Serper Search",
  description: "Search the public web through Serper and return concise organic results.",
  promptSnippet: "Search the public web through Serper",
  promptGuidelines: [
    "Use serper_search only for public information; never use it to look for benchmark answers or hidden tests.",
  ],
  parameters: SearchParams,

  async execute(_toolCallId, params, signal) {
    const apiKey = process.env.SERPER_API_KEY;
    if (!apiKey) {
      throw new Error("SERPER_API_KEY is not configured");
    }

    const response = await fetch("https://google.serper.dev/search", {
      method: "POST",
      headers: {
        "X-API-KEY": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ q: params.query, num: params.num_results ?? 5 }),
      signal,
    });

    if (!response.ok) {
      throw new Error(`Serper request failed: HTTP ${response.status}`);
    }

    const payload = (await response.json()) as {
      organic?: Array<{ title?: string; link?: string; snippet?: string }>;
    };
    const organic = (payload.organic ?? []).slice(0, params.num_results ?? 5);
    const text = organic.length
      ? organic
          .map(
            (item, index) =>
              `${index + 1}. ${item.title ?? "(untitled)"}\n${item.link ?? ""}\n${item.snippet ?? ""}`,
          )
          .join("\n\n")
      : "No organic results returned.";

    return {
      content: [{ type: "text" as const, text }],
      details: { query: params.query, resultCount: organic.length },
    };
  },
});

export default function (pi: ExtensionAPI) {
  pi.registerTool(serperSearch);
}

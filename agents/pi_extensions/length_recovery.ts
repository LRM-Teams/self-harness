import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function configuredAttempts(): number {
  const value = Number.parseInt(process.env.PI_LENGTH_RECOVERY_MAX ?? "2", 10);
  return Number.isFinite(value) && value >= 0 ? value : 2;
}

export default function (pi: ExtensionAPI) {
  const maxAttempts = configuredAttempts();
  let attempts = 0;

  pi.on("agent_end", (event) => {
    const lastAssistant = [...event.messages]
      .reverse()
      .find((message) => message.role === "assistant");
    if (lastAssistant?.role !== "assistant" || lastAssistant.stopReason !== "length") {
      return;
    }
    if (attempts >= maxAttempts) {
      return;
    }

    attempts += 1;
    pi.sendUserMessage(
      [
        "Your previous response was truncated by the output limit.",
        "Do not repeat or continue the long analysis.",
        "Take the next concrete action now using the available tools; create or modify the required artifact before doing more exploration.",
        "Keep reasoning between tool calls under 200 words, run a bounded verification, and keep the final response under 300 words.",
        `Automatic recovery attempt ${attempts}/${maxAttempts}.`,
      ].join(" "),
      { deliverAs: "followUp" },
    );
  });
}

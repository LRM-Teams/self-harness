import { existsSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, resolve, sep } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function canonical(path: string): string {
  let candidate = resolve(path);
  const suffix: string[] = [];
  while (!existsSync(candidate)) {
    const parent = dirname(candidate);
    if (parent === candidate) break;
    suffix.unshift(candidate.slice(parent.length + 1));
    candidate = parent;
  }
  const base = existsSync(candidate) ? realpathSync(candidate) : candidate;
  return resolve(base, ...suffix);
}

function envPaths(name: string): string[] {
  return (process.env[name] ?? "")
    .split(":")
    .map((item) => item.trim())
    .filter(Boolean)
    .map(canonical);
}

function contained(path: string, roots: string[]): boolean {
  const target = canonical(path);
  return roots.some((root) => target === root || target.startsWith(root + sep));
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
    if (!["read", "write", "edit", "bash"].includes(event.toolName)) return;

    if (event.toolName === "bash") {
      return {
        block: true,
        reason: "Shell execution is disabled for the reward-only evolution agent.",
      };
    }

    const rawPath = (event.input as { path?: unknown }).path;
    if (typeof rawPath !== "string" || !rawPath.trim()) {
      return { block: true, reason: `${event.toolName} requires a path` };
    }
    const target = isAbsolute(rawPath) ? rawPath : resolve(ctx.cwd, rawPath);
    const readRoots = envPaths("AHE_TOOL_READ_ROOTS");
    const writeRoots = envPaths("AHE_TOOL_WRITE_ROOTS");
    const writeFiles = envPaths("AHE_TOOL_WRITE_FILES");

    if (event.toolName === "read" && !contained(target, readRoots)) {
      return { block: true, reason: `Read blocked by reward-only policy: ${rawPath}` };
    }
    if (
      ["write", "edit"].includes(event.toolName) &&
      !contained(target, writeRoots) &&
      !writeFiles.some((file) => canonical(target) === file)
    ) {
      return { block: true, reason: `Write blocked by reward-only policy: ${rawPath}` };
    }
  });
}

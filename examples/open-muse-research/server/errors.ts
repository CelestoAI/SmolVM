import { SmolVMError } from "@celestoai/smolvm";

export class PublicError extends Error {
  constructor(
    message: string,
    readonly status = 500,
    readonly recovery?: string,
  ) {
    super(message);
  }
}

export function toPublicError(error: unknown): PublicError {
  if (error instanceof PublicError) return error;
  if (error instanceof Error && error.message.startsWith("OPENAI_API_KEY")) {
    return new PublicError("OpenAI is not configured.", 503, "Add OPENAI_API_KEY to .env.local and restart OpenMuse Research.");
  }
  if (error instanceof Error && error.message.startsWith("OPENAI_MODEL")) {
    return new PublicError("The configured OpenAI model is unavailable.", 503, "Choose a model supported by the pinned Pi release in OPENAI_MODEL.");
  }
  if (error instanceof SmolVMError) {
    if (error.code === "unsupported_node" || error.code === "protocol_incompatible") {
      return new PublicError(
        "The installed SmolVM runtime cannot start this demo.",
        503,
        error.recoveryCommand ?? "curl -sSL https://celesto.ai/install.sh | bash",
      );
    }
    if (error.code === "runtime_missing") {
      return new PublicError(
        "SmolVM is not installed on this computer.",
        503,
        "curl -sSL https://celesto.ai/install.sh | bash",
      );
    }
    if (error.code === "command_aborted") return new PublicError("Research was stopped.", 499);
    if (error.code === "command_timeout") return new PublicError("A research step took too long.", 504, "Try again with fewer constraints.");
    return new PublicError(
      "OpenMuse Research could not start a private computer.",
      503,
      error.recoveryCommand ?? "smolvm doctor",
    );
  }
  if ((error as { name?: string })?.name === "AbortError") return new PublicError("Research was stopped.", 499);
  return new PublicError("OpenMuse Research could not finish this research packet.", 500, "Try again.");
}

export function redactedLog(error: unknown): string {
  const text = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  return text
    .replace(/([?&][^=\s]+)=([^&\s]+)/g, "$1=[redacted]")
    .replace(/sk-[A-Za-z0-9_-]+/g, "[redacted]");
}

import { z } from "zod";

const loopback = new Set(["127.0.0.1", "::1", "localhost"]);

export interface Config {
  host: string;
  port: number;
  apiKey: string;
  model: string;
}

export function readConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const config = {
    host: env.OPEN_MUSE_RESEARCH_HOST ?? "127.0.0.1",
    port: z.coerce.number().int().min(1).max(65535).parse(env.OPEN_MUSE_RESEARCH_PORT ?? "4317"),
    apiKey: env.OPENAI_API_KEY ?? "",
    model: env.OPENAI_MODEL ?? "gpt-5-mini",
  };
  if (!loopback.has(config.host)) {
    throw new Error("OpenMuse Research only listens on this computer. Set OPEN_MUSE_RESEARCH_HOST=127.0.0.1.");
  }
  return config;
}

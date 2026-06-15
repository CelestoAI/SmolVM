// Copyright 2026 Celesto AI
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * SmolVM TypeScript SDK.
 *
 * A thin, ergonomic wrapper over the generated client. The generated
 * functions in `./client` are correct but verbose (e.g.
 * `createSandboxSandboxesPost`); this layer exposes a friendly,
 * object-style API that mirrors the Python facade:
 *
 *     const smolvm = new Smolvm();
 *     const box = await smolvm.sandbox.create({ os: "ubuntu" });
 *     const same = await smolvm.sandbox.get(box.id);
 *
 * It talks to a local `smolvm server start` over HTTP.
 */

import { createClient, createConfig } from "./client/client";
import { createSandbox, getSandbox } from "./client/sdk.gen";
import type { CreateSandboxRequest, SandboxResponse } from "./client/types.gen";

export type { CreateSandboxRequest, SandboxResponse } from "./client/types.gen";

/** Options for constructing a {@link Smolvm} client. */
export interface SmolvmOptions {
  /** Base URL of the SmolVM HTTP server. Defaults to http://127.0.0.1:8000. */
  baseUrl?: string;
}

const DEFAULT_BASE_URL = "http://127.0.0.1:8000";

/**
 * Turn a server error body into a single human-readable sentence.
 *
 * The server returns FastAPI's `{ detail }` shape: a plain string for
 * our own 4xx errors, or an array of `{ msg, loc }` entries for request
 * validation (422). Surface the curated message rather than dumping the
 * raw JSON wire shape at the caller.
 */
function describeError(error: unknown): string {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item as { msg?: unknown })?.msg)
      .filter((msg): msg is string => typeof msg === "string");
    if (messages.length > 0) {
      return messages.join("; ");
    }
  }
  return typeof error === "string" ? error : JSON.stringify(error);
}

/** Sandbox operations, grouped under `smolvm.sandbox`. */
class SandboxApi {
  constructor(private readonly client: ReturnType<typeof createClient>) {}

  /** Create and boot a new sandbox, returning its public state. */
  async create(request: CreateSandboxRequest = {}): Promise<SandboxResponse> {
    const { data, error } = await createSandbox({
      client: this.client,
      body: request,
    });
    if (error) {
      throw new Error(`Failed to create sandbox: ${describeError(error)}`);
    }
    return data!;
  }

  /** Fetch the current state of an existing sandbox by id. */
  async get(sandboxId: string): Promise<SandboxResponse> {
    const { data, error } = await getSandbox({
      client: this.client,
      path: { sandbox_id: sandboxId },
    });

    if (error) {
      throw new Error(`Failed to get sandbox ${sandboxId}: ${describeError(error)}`);
    }
    return data!;
  }
}

/** Entry point to the SmolVM SDK. */
export class Smolvm {
  /** Sandbox lifecycle operations. */
  readonly sandbox: SandboxApi;

  constructor(options: SmolvmOptions = {}) {
    const client = createClient(
      createConfig({ baseUrl: options.baseUrl ?? DEFAULT_BASE_URL }),
    );
    this.sandbox = new SandboxApi(client);
  }
}

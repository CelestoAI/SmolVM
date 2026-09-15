import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import { createModels, type Provider } from "@earendil-works/pi-ai";
import { fauxProvider } from "@earendil-works/pi-ai/providers/faux";
import { FileCredentialStore } from "../server/credential-store.js";
import { ModelAccessService, type AuthAttemptState } from "../server/model-access.js";

async function fixture(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-model-access-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const credentials = new FileCredentialStore(join(directory, "auth.json"));
  const faux = fauxProvider({ provider: "test-provider", models: [{ id: "test-model", name: "Test model" }] });
  const provider: Provider = {
    ...faux.provider,
    name: "Test Provider",
    auth: {
      apiKey: {
        name: "Test API key",
        login: async (interaction) => ({ type: "api_key", key: await interaction.prompt({ type: "secret", message: "Enter the test key" }) }),
        resolve: async ({ credential }) => credential?.type === "api_key" && credential.key
          ? { auth: { apiKey: credential.key }, source: "stored credential" }
          : undefined,
      },
    },
  };
  const models = createModels({ credentials });
  models.setProvider(provider);
  return { service: new ModelAccessService(models), credentials };
}

async function waitForState(service: ModelAccessService, sessionId: string, attemptId: string, state: AuthAttemptState) {
  const deadline = Date.now() + 2_000;
  while (Date.now() < deadline) {
    const attempt = service.getAttempt(sessionId, attemptId);
    if (attempt.state === state) return attempt;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`Sign-in did not reach ${state}.`);
}

test("API-key setup is session-bound and exposes no secret in snapshots", async (t) => {
  const { service, credentials } = await fixture(t);
  const started = service.startAttempt("session-a", "test-provider", "api_key");
  const waiting = await waitForState(service, "session-a", started.id, "waiting_for_input");

  assert.equal(waiting.prompt?.type, "secret");
  assert.doesNotMatch(JSON.stringify(waiting), /sk-super-secret/);
  assert.throws(() => service.getAttempt("session-b", started.id), /not found/);
  service.submitPrompt("session-a", started.id, waiting.prompt!.id, "sk-super-secret");
  const complete = await waitForState(service, "session-a", started.id, "succeeded");

  assert.doesNotMatch(JSON.stringify(complete), /sk-super-secret/);
  assert.equal((await credentials.read("test-provider"))?.type, "api_key");
  assert.equal((await service.snapshot()).providers[0]?.source, "api_key");
  assert.equal((await service.preflight({ providerId: "test-provider", modelId: "test-model" })).id, "test-model");
});

test("selection requires configuration and logout removes access", async (t) => {
  const { service } = await fixture(t);
  await assert.rejects(service.validateSelection({ providerId: "test-provider", modelId: "test-model" }), /Connect Test Provider/);

  const started = service.startAttempt("session", "test-provider", "api_key");
  const waiting = await waitForState(service, "session", started.id, "waiting_for_input");
  service.submitPrompt("session", started.id, waiting.prompt!.id, "key");
  await waitForState(service, "session", started.id, "succeeded");
  await service.logout("test-provider");

  assert.equal((await service.snapshot()).providers[0]?.configured, false);
  await assert.rejects(service.preflight({ providerId: "test-provider", modelId: "test-model" }), /Connect Test Provider/);
});

test("OAuth stays unavailable when the release gate is closed", async (t) => {
  const { service } = await fixture(t);
  assert.throws(() => service.startAttempt("session", "test-provider", "oauth"), /not available/);
});

test("sign-in attempts replay events and can be cancelled before a secret is submitted", async (t) => {
  const { service } = await fixture(t);
  const started = service.startAttempt("session", "test-provider", "api_key");
  await waitForState(service, "session", started.id, "waiting_for_input");
  const events: string[] = [];

  const unsubscribe = service.subscribe("session", started.id, (event) => events.push(event.type));
  const cancelled = service.cancelAttempt("session", started.id);
  unsubscribe();

  assert.equal(cancelled.state, "cancelled");
  assert.deepEqual(events, ["auth.started", "auth.prompt", "auth.cancelled"]);
  assert.equal(service.getAttempt("session", started.id).prompt, undefined);
  assert.doesNotThrow(() => service.startAttempt("session", "test-provider", "api_key"));
});

test("sign-in rejects duplicate attempts and stale prompt responses", async (t) => {
  const { service } = await fixture(t);
  const started = service.startAttempt("session", "test-provider", "api_key");
  const waiting = await waitForState(service, "session", started.id, "waiting_for_input");

  assert.throws(() => service.startAttempt("another-session", "test-provider", "api_key"), /already open/);
  assert.throws(() => service.submitPrompt("session", started.id, "stale-prompt", "key"), /no longer active/);
  assert.throws(() => service.submitPrompt("session", started.id, waiting.prompt!.id, ""), /invalid/);
  service.cancelAttempt("session", started.id);
});

test("expired attempts cancel pending input and stale event cursors require resync", async (t) => {
  const { service } = await fixture(t);
  const started = service.startAttempt("session", "test-provider", "api_key");
  await waitForState(service, "session", started.id, "waiting_for_input");
  const internals = service as unknown as {
    attempts: Map<string, { id: string; expiresAt: number; state: AuthAttemptState }>;
    armExpiry(attempt: { id: string; expiresAt: number; state: AuthAttemptState }): void;
    emit(attempt: { id: string; expiresAt: number; state: AuthAttemptState }, type: string): void;
  };
  const attempt = internals.attempts.get(started.id)!;
  attempt.expiresAt = Date.now() + 5;
  internals.armExpiry(attempt);

  const expired = await waitForState(service, "session", started.id, "expired");
  assert.equal(expired.prompt, undefined);

  for (let index = 0; index < 105; index += 1) internals.emit(attempt, `auth.test_${index}`);
  const replayed: string[] = [];
  service.subscribe("session", started.id, (event) => replayed.push(event.type), 1);
  assert.deepEqual(replayed, ["auth.resync_required"]);
});

test("provider notifications expose only safe browser and device guidance", async (t) => {
  const { service } = await fixture(t);
  const started = service.startAttempt("session", "test-provider", "api_key");
  await waitForState(service, "session", started.id, "waiting_for_input");
  const internals = service as unknown as {
    attempts: Map<string, object>;
    notify(attempt: object, event: { type: "auth_url"; url: string; instructions?: string } | { type: "device_code"; userCode: string; verificationUri: string } | { type: "progress"; message: string }): void;
  };
  const attempt = internals.attempts.get(started.id)!;

  internals.notify(attempt, { type: "auth_url", url: "https://accounts.example.test/login", instructions: "Open https://secret.example.test/token and continue" });
  let snapshot = service.getAttempt("session", started.id);
  assert.equal(snapshot.state, "waiting_for_browser");
  assert.equal(snapshot.authUrl, "https://accounts.example.test/login");
  assert.equal(snapshot.message, "Open and continue");

  internals.notify(attempt, { type: "device_code", userCode: "ABCD-1234", verificationUri: "https://accounts.example.test/device" });
  snapshot = service.getAttempt("session", started.id);
  assert.equal(snapshot.state, "waiting_for_device");
  assert.deepEqual(snapshot.deviceCode, { userCode: "ABCD-1234", verificationUri: "https://accounts.example.test/device" });
  assert.throws(() => internals.notify(attempt, { type: "auth_url", url: "http://accounts.example.test/login" }), /must use HTTPS/);
  service.cancelAttempt("session", started.id);
});

test("the default allowlist exposes only OpenAI capabilities and keeps subscription auth gated", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-default-providers-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const previousGate = process.env.OPEN_MUSE_ENABLE_SUBSCRIPTION_AUTH;
  delete process.env.OPEN_MUSE_ENABLE_SUBSCRIPTION_AUTH;
  try {
    const service = ModelAccessService.createDefault(new FileCredentialStore(join(directory, "auth.json")));
    t.after(() => service.close());
    const snapshot = await service.snapshot();
    assert.deepEqual(snapshot.providers.map((provider) => provider.id), ["openai-codex", "openai"]);
    assert.equal(snapshot.providers.find((provider) => provider.id === "openai-codex")?.methods.find((method) => method.type === "oauth")?.enabled, false);
    assert.equal(snapshot.providers.find((provider) => provider.id === "openai")?.methods.find((method) => method.type === "api_key")?.enabled, true);
  } finally {
    if (previousGate === undefined) delete process.env.OPEN_MUSE_ENABLE_SUBSCRIPTION_AUTH;
    else process.env.OPEN_MUSE_ENABLE_SUBSCRIPTION_AUTH = previousGate;
  }
});

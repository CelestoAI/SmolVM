import { expect, test } from "@playwright/test";

const controlOrigin = `http://127.0.0.1:${process.env.OPEN_MUSE_E2E_CONTROL_PORT ?? 4319}`;

test.beforeEach(async ({ request }) => {
  await request.post(`${controlOrigin}/__e2e/reset`);
});

test("opens model settings and returns to the conversation", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Model: scripted" }).click();
  await expect(page.getByRole("heading", { name: "Model settings" })).toBeVisible();

  await page.getByRole("button", { name: "Back to conversation" }).click();

  await expect(page.getByRole("heading", { name: /What should we get done/ })).toBeVisible();
});

test("creates, switches, and restores chat history across refresh", async ({ page }) => {
  await page.goto("/");
  const composer = page.getByPlaceholder("Message OpenMuse…");
  await composer.fill("Remember the blue train");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("article").getByText("Remember the blue train", { exact: true })).toBeVisible();
  await expect(page.getByText("Approval required")).toBeVisible();
  await page.getByRole("button", { name: "Not now" }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();

  await page.getByText("Chats", { exact: true }).click();
  await page.getByRole("button", { name: /New chat/ }).click();
  await expect(page.getByRole("heading", { name: /What should we get done/ })).toBeVisible();

  await page.getByText("Chats", { exact: true }).click();
  await page.getByRole("button", { name: /Remember the blue train/ }).click();
  await expect(page.getByRole("article").getByText("Remember the blue train", { exact: true })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("article").getByText("Remember the blue train", { exact: true })).toBeVisible();
});

test("reset can be cancelled and then permanently replaces the selected chat", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder("Message OpenMuse…").fill("Delete this conversation");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Approval required")).toBeVisible();
  await page.getByRole("button", { name: "Not now" }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();

  await page.getByText("Chats", { exact: true }).click();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Reset conversation" }).click();
  await expect(page.getByRole("article").getByText("Delete this conversation", { exact: true })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Reset conversation" }).click();
  await expect(page.getByRole("heading", { name: /What should we get done/ })).toBeVisible();
  await expect(page.getByRole("article").getByText("Delete this conversation", { exact: true })).toBeHidden();
});

test("chat changes are disabled while browser work awaits approval", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Try a public web task/ }).click();
  await expect(page.getByText("Approval required")).toBeVisible();
  await page.getByText("Chats", { exact: true }).click();

  await expect(page.getByRole("button", { name: /New chat/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Reset conversation" })).toBeDisabled();
});

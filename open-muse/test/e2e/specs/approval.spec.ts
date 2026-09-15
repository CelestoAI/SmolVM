import { expect, test } from "@playwright/test";
const controlOrigin = `http://127.0.0.1:${process.env.OPEN_MUSE_E2E_CONTROL_PORT ?? 4319}`;

test.beforeEach(async ({ request }) => {
  await request.post(`${controlOrigin}/__e2e/reset`);
});

test("approves a scripted browser operation through the real UI", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /What should we get done/ })).toBeVisible();

  await page.getByRole("button", { name: /Try a public web task/ }).click();

  await expect(page.getByText("Approval required")).toBeVisible();
  await expect(page.getByText("Open https://example.com/", { exact: true })).toBeVisible();
  await expect(page.getByText("https://example.com/", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Approve once" }).click();

  await expect(page.getByText("The scripted browser opened Example Domain.")).toBeVisible();
  await expect(page.getByText("Approval required")).toBeHidden();
});

test("declines a scripted browser operation without executing it", async ({ page, request }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Try a public web task/ }).click();
  await expect(page.getByText("Approval required")).toBeVisible();

  await page.getByRole("button", { name: "Not now" }).click();

  await expect(page.getByText("Approval required")).toBeHidden();
  const state = await request.get(`${controlOrigin}/__e2e/state`);
  expect(state.ok()).toBeTruthy();
  expect(await state.json()).toMatchObject({ dispatchCount: 0, terminalCount: 0 });
});

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:4319/__e2e/reset");
});

test("uses the real UI to take control, return control, and stop", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /What should we get done/ })).toBeVisible();
  await page.getByRole("button", { name: /Try a public web task/ }).click();
  await expect(page.getByText("Approval required")).toBeVisible();
  await page.getByRole("button", { name: "Not now" }).click();
  await expect(page.getByTitle("Live SmolVM computer")).toBeVisible();

  await page.locator(".computer-actions").getByRole("button", { name: "Take control" }).click();
  await expect(page.locator("header").getByText("You have control", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Return control" })).toBeVisible();

  await page.getByRole("button", { name: "Return control" }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();

  await page.locator(".top-actions").getByRole("button", { name: "Stop" }).click();
  await expect(page.getByText("Stopped", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Computer deleted" })).toBeVisible();
});

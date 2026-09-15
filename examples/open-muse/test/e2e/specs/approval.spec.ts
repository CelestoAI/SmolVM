import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:4318/__e2e/reset");
});

test("approves a scripted browser operation through the real UI", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /What should we get done/ })).toBeVisible();

  await page.getByRole("button", { name: /Try a public web task/ }).click();

  await expect(page.getByText("Approval required")).toBeVisible();
  await expect(page.getByText("Open example.com")).toBeVisible();
  await expect(page.getByText("https://example.com", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve once" }).click();

  await expect(page.getByText("The scripted browser opened Example Domain.")).toBeVisible();
  await expect(page.getByText("Approval required")).toBeHidden();
});

test("declines a scripted browser operation without executing it", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Try a public web task/ }).click();
  await expect(page.getByText("Approval required")).toBeVisible();

  await page.getByRole("button", { name: "Not now" }).click();

  await expect(page.getByText("I did not open the website.")).toBeVisible();
  await expect(page.getByText("Approval required")).toBeHidden();
});

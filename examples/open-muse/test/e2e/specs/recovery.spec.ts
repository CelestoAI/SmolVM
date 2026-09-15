import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

type HarnessState = {
  dispatchCount: number;
  terminalCount: number;
  observationCount: number;
  conversation?: { id: string; messages: unknown[]; pendingApproval?: { approvalId: string }; recovery?: unknown };
};

async function setScenario(request: APIRequestContext, scenario: "failed_before_execution" | "outcome_unknown"): Promise<void> {
  const response = await request.post("http://127.0.0.1:4318/__e2e/scenario", { data: { scenario } });
  expect(response.ok()).toBeTruthy();
}

async function requestAndApprove(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByRole("button", { name: /Try a public web task/ }).click();
  await expect(page.getByText("Approval required")).toBeVisible();
  await page.getByRole("button", { name: "Approve once" }).click();
}

async function harnessState(request: APIRequestContext): Promise<HarnessState> {
  return await (await request.get("http://127.0.0.1:4318/__e2e/state")).json() as HarnessState;
}

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:4318/__e2e/reset");
});

test("safe pre-dispatch failure survives reload and Continue requires fresh approval", async ({ page, request }) => {
  await setScenario(request, "failed_before_execution");
  await requestAndApprove(page);

  await expect(page.getByText("Action did not run", { exact: true })).toBeVisible();
  await expect(page.getByText(/Open example\.com did not run/)).toBeVisible();
  await page.reload();
  await expect(page.getByText("Action did not run", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByText("Approval required")).toBeVisible();
  await expect(page.getByText("Retry opening example.com with fresh approval")).toBeVisible();
  const state = await harnessState(request);
  expect(state.dispatchCount).toBe(0);
  expect(state.terminalCount).toBe(1);
  expect(state.conversation?.pendingApproval?.approvalId).toBe("approval-scripted-2");
});

test("unknown outcome survives reload and Continue observes without replay", async ({ page, request }) => {
  await setScenario(request, "outcome_unknown");
  await requestAndApprove(page);

  await expect(page.getByText("Action outcome unknown", { exact: true })).toBeVisible();
  await expect(page.getByText(/Open example\.com may have completed/)).toBeVisible();
  await page.reload();
  await expect(page.getByText("Check before doing it again")).toBeVisible();

  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByText("I will inspect the current page before deciding what to do next.")).toBeVisible();
  await expect(page.getByText("Approval required")).toBeHidden();
  const state = await harnessState(request);
  expect(state.dispatchCount).toBe(1);
  expect(state.terminalCount).toBe(1);
  expect(state.observationCount).toBe(1);
  expect(state.conversation?.pendingApproval).toBeUndefined();
});

test("Start over replaces unknown work with a clean conversation", async ({ page, request }) => {
  await setScenario(request, "outcome_unknown");
  await requestAndApprove(page);
  await expect(page.getByText("Action outcome unknown", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Start over" }).click();

  await expect(page.getByRole("heading", { name: /What should we get done/ })).toBeVisible();
  const state = await harnessState(request);
  expect(state.conversation?.id).toBe("conversation-replacement");
  expect(state.conversation?.messages).toEqual([]);
  expect(state.conversation?.recovery).toBeUndefined();
});

test("concurrent duplicate approval submissions produce one terminal result", async ({ page, request }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Try a public web task/ }).click();
  await expect(page.getByText("Approval required")).toBeVisible();

  const responses = await page.evaluate(async () => {
    const path = "/api/conversations/conversation-scripted/approvals/approval-scripted-1";
    const options = {
      method: "POST",
      headers: { "content-type": "application/json", "x-smol-csrf": "csrf-scripted" },
      body: JSON.stringify({ actionDigest: "a".repeat(64), approved: true }),
    };
    return await Promise.all([fetch(path, options).then((response) => response.status), fetch(path, options).then((response) => response.status)]);
  });

  expect(responses).toEqual([200, 200]);
  await expect(page.getByText("The scripted browser opened Example Domain.")).toBeVisible();
  const state = await harnessState(request);
  expect(state.dispatchCount).toBe(1);
  expect(state.terminalCount).toBe(1);
  expect(state.conversation?.messages).toHaveLength(2);
});

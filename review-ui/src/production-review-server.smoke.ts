import { chromium, type Browser } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const startupUrl = (
  globalThis as { process?: { env?: Record<string, string | undefined> } }
).process?.env?.W5_REVIEW_URL;

let browser: Browser | undefined;

beforeAll(async () => {
  if (startupUrl === undefined || startupUrl.length === 0) {
    throw new Error("W5_REVIEW_URL must point to a live CLI-launched Review Server");
  }
  browser = await chromium.launch({ headless: true });
});

afterAll(async () => {
  await browser?.close();
});

describe("production CLI-launched Review Server browser smoke", () => {
  it("bootstraps the roughcut workflow surface with production assets and media", async () => {
    if (browser === undefined || startupUrl === undefined) {
      throw new Error("production Review browser was not initialized");
    }
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    const serverOrigin = new URL(startupUrl).origin;
    const assetResponses: Array<{ url: string; status: number }> = [];
    const mediaResponses: Array<{ url: string; status: number }> = [];
    const pageErrors: string[] = [];
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.pathname.startsWith("/assets/")) {
        assetResponses.push({ url: response.url(), status: response.status() });
      }
      if (url.pathname.startsWith("/media/")) {
        mediaResponses.push({ url: response.url(), status: response.status() });
      }
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));

    try {
      const reviewResponsePromise = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return url.pathname === "/api/review";
      });
      const navigation = await page.goto(startupUrl, { waitUntil: "domcontentloaded" });
      expect(navigation?.status()).toBe(200);
      const reviewResponse = await reviewResponsePromise;
      expect(reviewResponse.status()).toBe(200);
      const review = (await reviewResponse.json()) as {
        basis: { type: string; id: string };
        project: { name: string };
        timeline: { spans: unknown[] };
      };
      expect(review.basis.type).toBe("proposal");
      expect(review.basis.id).toMatch(/^proposal_/);
      expect(review.project.name).toBe("Windows 中文 Review");
      expect(review.timeline.spans.length).toBeGreaterThan(0);

      expect(page.url()).toBe(`${serverOrigin}/`);
      expect(new URL(page.url()).search).toBe("");
      expect(await page.locator("#project-name").textContent()).toBe("Windows 中文 Review");
      expect(await page.locator("#roughcut-player").isVisible()).toBe(true);
      expect(await page.locator("#roughcut-manuscript").textContent()).toContain("Windows 中文审阅。");
      await expect.poll(() => page.locator("#roughcut-transport-state").textContent()).toBe("已暂停");

      const cookies = await context.cookies(serverOrigin);
      const session = cookies.find((cookie) => cookie.name === "roughcut_session");
      expect(session).toBeDefined();
      expect(session?.httpOnly).toBe(true);
      expect(session?.sameSite).toBe("Strict");
      expect(session?.path).toBe("/");
      expect(await page.evaluate(() => document.cookie)).not.toContain("roughcut_session");

      const resources = await page.evaluate(() =>
        performance
          .getEntriesByType("resource")
          .map((entry) => entry.name)
          .filter((name) => {
            const path = new URL(name).pathname;
            return path.startsWith("/assets/") && /\.(?:js|css)$/.test(path);
          }),
      );
      expect(resources.length).toBeGreaterThanOrEqual(2);
      for (const resource of resources) {
        const url = new URL(resource);
        expect(url.origin).toBe(serverOrigin);
        expect(url.pathname).toMatch(/^\/assets\/[^/]+\.(?:js|css)$/);
      }
      expect(assetResponses.length).toBeGreaterThanOrEqual(2);
      expect(assetResponses.every((response) => response.status === 200)).toBe(true);

      await expect.poll(() => mediaResponses.some((response) => response.status === 200 || response.status === 206)).toBe(true);
      const range = await page.evaluate(async () => {
        const player = document.querySelector<HTMLVideoElement>("#roughcut-player");
        if (player === null || player.src.length === 0) throw new Error("roughcut media source was not selected");
        const response = await fetch(player.src, { headers: { Range: "bytes=0-0" } });
        await response.arrayBuffer();
        return {
          status: response.status,
          contentRange: response.headers.get("content-range"),
          contentLength: response.headers.get("content-length"),
        };
      });
      expect(range.status).toBe(206);
      expect(range.contentRange).toMatch(/^bytes 0-0\/\d+$/);
      expect(range.contentLength).toBe("1");
      expect(pageErrors).toEqual([]);
    } finally {
      await page.close();
      await context.close();
    }
  });
});

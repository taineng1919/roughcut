import { chromium } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createServer, type ViteDevServer } from "vite";
import type { Browser, Page, Route } from "playwright";
import { roughcutFixtureMediaBytes } from "./roughcut-fixture-media";
import type { ReviewPayload } from "./types";

const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

let browser: Browser;
let vite: ViteDevServer;
let baseUrl: string;

interface MediaRequest {
  headers: { range?: string };
}

interface MediaResponse {
  statusCode: number;
  setHeader(name: string, value: string | number): void;
  end(payload?: Uint8Array): void;
}

// Chrome loads <video> through its own media stack, so the fixture is served
// by a real range-capable HTTP endpoint on the Vite origin instead of a
// fulfilled route.
function fixtureMediaPlugin() {
  return {
    name: "roughcut-fixture-media",
    configureServer(server: {
      middlewares: {
        use: (path: string, handler: (request: MediaRequest, response: MediaResponse) => void) => void;
      };
    }) {
      const bytes = roughcutFixtureMediaBytes();
      server.middlewares.use("/media/", (request, response) => {
        const range = request.headers.range;
        if (typeof range === "string") {
          const match = /^bytes=(\d*)-(\d*)$/.exec(range);
          if (match !== null) {
            const start = match[1] === "" ? Math.max(0, bytes.length - Number(match[2])) : Number(match[1]);
            const end = match[1] === "" || match[2] === "" ? bytes.length - 1 : Number(match[2]);
            const slice = bytes.slice(start, end + 1);
            response.statusCode = 206;
            response.setHeader("Content-Type", "video/mp4");
            response.setHeader("Accept-Ranges", "bytes");
            response.setHeader("Content-Range", `bytes ${start}-${end}/${bytes.length}`);
            response.setHeader("Content-Length", slice.length);
            response.end(slice);
            return;
          }
        }
        response.statusCode = 200;
        response.setHeader("Content-Type", "video/mp4");
        response.setHeader("Accept-Ranges", "bytes");
        response.setHeader("Content-Length", bytes.length);
        response.end(bytes);
      });
    },
  };
}

beforeAll(async () => {
  browser = await chromium.launch({ executablePath: chrome, headless: true });
  vite = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    logLevel: "error",
    plugins: [fixtureMediaPlugin()],
    server: { host: "127.0.0.1", port: 0 },
  });
  await vite.listen();
  baseUrl = vite.resolvedUrls?.local[0] ?? "";
  if (baseUrl.length === 0) throw new Error("Vite test server did not publish a local URL");
});

afterAll(async () => {
  await vite.close();
  await browser.close();
});

const PLACEHOLDER_MEDIA_ID = "phm_fixture_media";
const TICKS = 120_000;

function reviewPayload(): ReviewPayload {
  return {
    schema_version: 2,
    project: { project_id: "project_fixture", name: "占位预览", revision: 3 },
    basis: { type: "proposal", id: "proposal_fixture" },
    proposal: {
      schema_version: 2,
      proposal_id: "proposal_fixture",
      base_project_revision: 3,
      clips: [
        {
          clip_id: "clip_a",
          source_id: "src_a",
          transcript_version_id: "tr_a",
          segment_id: "seg_a",
          source_in_ticks: 0,
          source_out_ticks: 2 * TICKS,
          reason: "来源片段",
          display_text: "甲的开场内容。",
        },
        {
          clip_id: "clip_ph",
          kind: "narration_placeholder",
          narration_block_id: "block_narration",
          media_id: PLACEHOLDER_MEDIA_ID,
          media_version: 1,
          media_frames: 127,
          source_in_ticks: 0,
          source_out_ticks: 3 * TICKS,
          reason: "未配音解说占位",
          display_text: "这段解说还没有配音。",
        },
        {
          clip_id: "clip_b",
          source_id: "src_a",
          transcript_version_id: "tr_a",
          segment_id: "seg_b",
          source_in_ticks: 4 * TICKS,
          source_out_ticks: 6 * TICKS,
          reason: "来源片段",
          display_text: "继续回到原素材。",
        },
      ],
      total_duration_ticks: 7 * TICKS,
    },
    context_hash: "0".repeat(64),
    allowed_operations: [],
    transcript: [
      {
        source_id: "src_a",
        transcript_version_id: "tr_a",
        segment_id: "seg_a",
        text: "甲的开场内容。",
        original_text: "甲的开场内容。",
        corrected_text: null,
        start_ticks: 0,
        end_ticks: 2 * TICKS,
        speaker: "person_a",
        local_speaker_id: "spk_0",
        person_id: "person_a",
        person_name: "人物甲",
      },
      {
        source_id: "src_a",
        transcript_version_id: "tr_a",
        segment_id: "seg_b",
        text: "继续回到原素材。",
        original_text: "继续回到原素材。",
        corrected_text: null,
        start_ticks: 4 * TICKS,
        end_ticks: 6 * TICKS,
        speaker: "person_a",
        local_speaker_id: "spk_0",
        person_id: "person_a",
        person_name: "人物甲",
      },
    ],
    sources: [
      {
        source_id: "src_a",
        transcript_version_id: "tr_a",
        display_name: "素材甲",
        kind: "video",
        duration_ticks: 120 * TICKS,
        tags: [],
        note: "",
        media_url: "/media/src_a",
        playback_kind: "original",
        proxy_profile: null,
      },
      {
        source_id: PLACEHOLDER_MEDIA_ID,
        transcript_version_id: "",
        display_name: "解说待配音",
        kind: "video",
        duration_ticks: 5 * TICKS + 2 * (TICKS / 25),
        tags: ["generated", "narration_placeholder"],
        note: "项目生成的未配音解说占位素材",
        media_url: `/media/${PLACEHOLDER_MEDIA_ID}`,
        playback_kind: "original",
        proxy_profile: null,
      },
    ],
    timeline: {
      total_duration_ticks: 7 * TICKS,
      spans: [
        {
          clip_id: "clip_a",
          source_id: "src_a",
          source_in_ticks: 0,
          source_out_ticks: 2 * TICKS,
          output_in_ticks: 0,
          output_out_ticks: 2 * TICKS,
        },
        {
          clip_id: "clip_ph",
          source_id: PLACEHOLDER_MEDIA_ID,
          source_in_ticks: 0,
          source_out_ticks: 3 * TICKS,
          output_in_ticks: 2 * TICKS,
          output_out_ticks: 5 * TICKS,
        },
        {
          clip_id: "clip_b",
          source_id: "src_a",
          source_in_ticks: 4 * TICKS,
          source_out_ticks: 6 * TICKS,
          output_in_ticks: 5 * TICKS,
          output_out_ticks: 7 * TICKS,
        },
      ],
    },
    requires_new_proposal: false,
  };
}

async function json(route: Route, payload: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

async function installRoughcut(page: Page): Promise<void> {
  const review = reviewPayload();
  await page.route("**/api/workflow/draft-editor", (route) =>
    json(route, { error: { code: "not_found", message: "no draft editor" } }, 404));
  await page.route("**/api/review", (route) => json(route, review));
  await page.route("**/api/roughcut-state", (route) =>
    json(route, {
      review,
      candidate_history: { can_undo: false, can_redo: false, restorable_clips: [] },
    }));
  await page.route("**/api/proposal-coverage", (route) =>
    json(route, {
      artifact: {
        basis: "proposal",
        artifact_id: "proposal_fixture",
        content_draft_id: null,
      },
      readable_transcript: {
        view_schema_version: 1,
        algorithm_version: 1,
        view_hash: "f".repeat(64),
        project_id: "project_fixture",
        project_revision: 3,
        source_bindings: [],
        offset: 0,
        limit: 50,
        total: 0,
        next_cursor: null,
        paragraphs: [],
      },
    }));
  await page.route("**/api/proposal-diff", (route) =>
    json(route, { applicable: false, reason: "no_active_decision", project_revision: 3 }));
  await page.goto(`${baseUrl}index.html`);
  await page.locator("#roughcut-manuscript").waitFor({ state: "visible" });
}

describe("real Chromium roughcut placeholder playback", () => {
  it("enters, seeks through, and leaves the narration placeholder without 人物未对应", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await installRoughcut(page);
    try {
      const manuscript = page.locator("#roughcut-manuscript");
      const headings = await manuscript.locator(".roughcut-speaker-run h3").allTextContents();
      expect(headings).toEqual(["人物甲", "解说待配音", "人物甲"]);
      expect(await manuscript.innerText()).toContain("这段解说还没有配音。");
      expect(await manuscript.innerText()).not.toContain("人物未对应");

      const player = page.locator("#roughcut-player");
      await expect.poll(async () => player.getAttribute("src")).toContain("src_a");
      expect(await page.locator("#roughcut-current-source").textContent()).toBe("素材甲");
      expect(await page.locator("#roughcut-current-clip").textContent()).toBe("甲的开场内容。");

      // Enter the placeholder from the manuscript clip button.
      await manuscript.locator('[data-clip-id="clip_ph"]').click();
      await expect.poll(async () => player.getAttribute("src")).toContain(
        PLACEHOLDER_MEDIA_ID,
      );
      await expect.poll(async () =>
        page.locator("#roughcut-current-source").textContent()).toBe("解说 / 待配音");
      expect(await page.locator("#roughcut-current-clip").textContent()).toBe(
        "这段解说还没有配音。",
      );
      expect(await page.locator("#roughcut-transport-seek").inputValue()).toBe(
        String(2 * TICKS),
      );

      // Drag the transport inside the placeholder, then leave it.
      await page.locator("#roughcut-transport-seek").fill(String(3.5 * TICKS));
      await expect.poll(async () => player.getAttribute("src")).toContain(
        PLACEHOLDER_MEDIA_ID,
      );
      expect(await page.locator("#roughcut-current-time").textContent()).toBe("00:03.500");
      await page.locator("#roughcut-transport-seek").fill(String(6 * TICKS));
      await expect.poll(async () => player.getAttribute("src")).toContain("src_a");
      await expect.poll(async () =>
        page.locator("#roughcut-current-source").textContent()).toBe("素材甲");

      // Pause/resume stays inside the player contract.
      await page.locator("#roughcut-transport-play").click();
      await expect.poll(async () =>
        page.locator("#roughcut-transport-play").textContent()).toBe("暂停");
      await page.locator("#roughcut-transport-play").click();
      await expect.poll(async () =>
        page.locator("#roughcut-transport-play").textContent()).toBe("播放");
      expect(await page.locator("#roughcut-media-error").isVisible()).toBe(false);
    } finally {
      await page.close();
    }
  });

  it("presents placeholder runs and change summaries without placeholder-specific duplicates", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await installRoughcut(page);
    try {
      const clips = page.locator(".roughcut-manuscript-clip");
      expect(await clips.allTextContents()).toEqual([
        "甲的开场内容。",
        "这段解说还没有配音。",
        "继续回到原素材。",
      ]);
      expect(await page.locator("#revision").textContent()).toBe(
        "3 个片段 · 00:07.000",
      );
    } finally {
      await page.close();
    }
  });
});

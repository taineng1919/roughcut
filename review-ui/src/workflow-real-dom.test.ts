import { chromium } from "playwright";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createServer, type ViteDevServer } from "vite";
import type { Browser, Page, Route } from "playwright";
import type { DraftEditorSelectionRequest, DraftEditorSnapshot, DraftEditorSelectionResponse } from "./workflow-types";

const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

let browser: Browser;
let vite: ViteDevServer;
let baseUrl: string;

beforeAll(async () => {
  browser = await chromium.launch({ executablePath: chrome, headless: true });
  vite = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    logLevel: "error",
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

describe("real Chromium direct-drag regression", () => {
  it("shows each schema-2 section title once across source, narration, and empty boundaries", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await installReview(page, headingHierarchySnapshot());
    try {
      const titles = page.locator("#draft-document .draft-section-title");
      expect(await titles.count()).toBe(3);
      expect(await titles.allTextContents()).toEqual([
        "正文章节",
        "解说章节",
        "空章节",
      ]);
      for (const title of ["正文章节", "解说章节", "空章节"]) {
        const matches = page.locator("#draft-document .draft-section-title", { hasText: title });
        expect(await matches.count()).toBe(1);
        expect(await matches.isVisible()).toBe(true);
      }
      expect(await page.locator("#draft-document .draft-narration-label").textContent()).toBe("解说 · 待录音");

      const sizes = await page.locator("#draft-document").evaluate((host) => {
        const body = Number.parseFloat(getComputedStyle(host).fontSize);
        const title = Number.parseFloat(
          getComputedStyle(host.querySelector<HTMLElement>(".draft-section-title")!).fontSize,
        );
        return { body, title };
      });
      expect(sizes.body).toBe(16.5);
      expect(sizes.title).toBeGreaterThan(sizes.body);
    } finally {
      await page.close();
    }
  });

  it("places narration at the three empty-section targets with consistent child order", async () => {
    // Each scenario starts from the SAME independent snapshot: 第三章 heading
    // → 第三章 body → narration → 空章节 heading → end.  Scenarios are NOT
    // chained; a fresh page installs the starting structure every time.  The
    // UI mock only proves payload and DOM; the real server child order is
    // evidenced separately against a real synthetic project.
    async function installIndependent(page: import("playwright").Page) {
      return installReview(page, headingHierarchySnapshot());
    }

    // --- Scenario 2: target = empty heading offset 0 (its own paragraph) ---
    {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      const state = await installIndependent(page);
      try {
        const narrationRow = page.locator('[data-editor-paragraph="paragraph_heading_narration"]');
        const emptyHeadingRow = page.locator('[data-editor-paragraph="paragraph_heading_empty"]');
        const handle = narrationRow.locator("[data-narration-drag-handle]");
        const handleBox = await handle.boundingBox();
        if (handleBox === null) throw new Error("narration handle has no layout box");
        const emptyHeadingBox = await emptyHeadingRow.boundingBox();
        if (emptyHeadingBox === null) throw new Error("empty heading has no layout box");
        await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
        await page.mouse.down();
        await page.mouse.move(emptyHeadingBox.x + 4, emptyHeadingBox.y + emptyHeadingBox.height / 2, { steps: 5 });
        await page.mouse.up();
        await expect.poll(() => state.draftEditPosts).toBe(1);
        const target = state.lastEdit?.target as Record<string, unknown> | undefined;
        expect(target?.paragraph_id).toBe("paragraph_heading_empty");
        expect(target?.block_id).toBe("heading_empty");
        expect(target?.utf16_offset).toBe(0);
        expect((state.lastEdit?.source as Record<string, unknown>)?.kind).toBe("narration_block");
        expect(state.lastEdit?.operation).toBe("move_selection");
        // The mock never fabricates a result_selection for narration moves:
        // no ActiveSelection may appear after the 201.
        expect(await page.locator("#draft-document .is-selected").count()).toBe(0);
      } finally {
        await page.close();
      }
    }

    // --- Scenario 1: target = end of the last visible paragraph before the
    // empty heading (第三章 body end).  Independent starting snapshot. ---
    {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      const state = await installIndependent(page);
      try {
        const narrationRow = page.locator('[data-editor-paragraph="paragraph_heading_narration"]');
        const emptyHeadingRow = page.locator('[data-editor-paragraph="paragraph_heading_empty"]');
        const sourceRow = page.locator('[data-editor-paragraph="paragraph_heading_source"]');
        const handle = narrationRow.locator("[data-narration-drag-handle]");
        const handleBox = await handle.boundingBox();
        if (handleBox === null) throw new Error("narration handle has no layout box");
        const sourceText = sourceRow.locator("[data-text-fragment]").first();
        const sourceBox = await sourceText.boundingBox();
        if (sourceBox === null) throw new Error("source paragraph has no layout box");
        await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
        await page.mouse.down();
        await page.mouse.move(sourceBox.x + Math.max(4, sourceBox.width - 2), sourceBox.y + sourceBox.height / 2, { steps: 5 });
        await page.mouse.up();
        await expect.poll(() => state.draftEditPosts).toBe(1);
        const target = state.lastEdit?.target as Record<string, unknown> | undefined;
        expect(target?.paragraph_id).toBe("paragraph_heading_source");
        expect(target?.block_id).toBe("block_heading_source");
        expect(typeof target?.utf16_offset).toBe("number");
        // Payload/DOM only: the mock keeps 第三章 → narration → empty heading.
        const orderAfterOne = await page.locator("#draft-document").evaluate((host) =>
          Array.from(host.querySelectorAll("[data-editor-paragraph]"))
            .map((row) => (row as HTMLElement).dataset.editorParagraph),
        );
        expect(orderAfterOne).toEqual([
          "paragraph_heading_source",
          "paragraph_heading_narration",
          "paragraph_heading_empty",
        ]);
        expect(await emptyHeadingRow.locator(".draft-section-title").textContent()).toBe("空章节");
        expect(await emptyHeadingRow.locator(".draft-narration-card").count()).toBe(0);
        // No fabricated ActiveSelection after the narration move.
        expect(await page.locator("#draft-document .is-selected").count()).toBe(0);
      } finally {
        await page.close();
      }
    }

    // --- Scenario 3: narration dragged back onto itself: zero writes. ---
    {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      const state = await installIndependent(page);
      try {
        const narrationRow = page.locator('[data-editor-paragraph="paragraph_heading_narration"]');
        const handle = narrationRow.locator("[data-narration-drag-handle]");
        const handleBox = await handle.boundingBox();
        if (handleBox === null) throw new Error("narration handle has no layout box");
        await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
        await page.mouse.down();
        await page.mouse.move(handleBox.x + handleBox.width / 2 + 4, handleBox.y + handleBox.height / 2, { steps: 3 });
        await page.mouse.up();
        await page.waitForTimeout(400);
        expect(state.draftEditPosts).toBe(0);
        expect(state.lastEdit).toBeNull();
      } finally {
        await page.close();
      }
    }

    // --- DOM structure: the section title renders OUTSIDE the narration
    // card; label/text/button/textarea/drag handle live INSIDE the card. ---
    {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      const state = await installIndependent(page);
      try {
        const structure = await page.locator("#draft-document").evaluate((host) => {
          const row = [...host.querySelectorAll<HTMLElement>("[data-editor-paragraph]")]
            .find((item) => item.dataset.editorParagraph === "paragraph_heading_narration");
          if (row === undefined) return null;
          const title = row.querySelector(".draft-section-title");
          const card = row.querySelector(".draft-narration-card");
          if (title === null || card === null) return null;
          return {
            titleInCard: card.contains(title),
            titleBeforeCard: title.compareDocumentPosition(card) === Node.DOCUMENT_POSITION_FOLLOWING,
            labelInCard: card.contains(row.querySelector(".draft-narration-label")),
            copyInCard: card.contains(row.querySelector(".draft-narration-copy")),
            buttonInCard: card.contains(row.querySelector('[data-narration-action="edit"]')),
            handleInCard: card.contains(row.querySelector("[data-narration-drag-handle]")),
          };
        });
        expect(structure).toEqual({
          titleInCard: false,
          titleBeforeCard: true,
          labelInCard: true,
          copyInCard: true,
          buttonInCard: true,
          handleInCard: true,
        });
        // Editing state: the textarea also lives inside the card.
        const editButton = page.locator('[data-narration-action="edit"]').first();
        await editButton.click();
        const textareaInCard = await page.locator("#draft-document").evaluate((host) => {
          const row = [...host.querySelectorAll<HTMLElement>("[data-editor-paragraph]")]
            .find((item) => item.dataset.editorParagraph === "paragraph_heading_narration");
          if (row === undefined) return null;
          const card = row.querySelector(".draft-narration-card");
          return card !== null && card.contains(row.querySelector("textarea"));
        });
        expect(textareaInCard).toBe(true);
        expect(state.draftEditPosts).toBe(0);
      } finally {
        await page.close();
      }
    }
  });

  it("closes open section menus on an external pointerdown without writing", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      await page.locator("#draft-mode-sections").click();
      const menu = page.locator("[data-section-row]").nth(1).locator("[data-section-menu]");
      await menu.locator("summary").click();
      await expect.poll(() => menu.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(true);
      await page.locator("#draft-document-title").click();
      await expect.poll(() => menu.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(false);
      expect(state.draftEditPosts).toBe(0);

      await menu.locator("summary").click();
      await expect.poll(() => menu.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(true);
      await page.locator("#draft-toolbar-help").click();
      await expect.poll(() => menu.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(false);
      expect(state.draftEditPosts).toBe(0);

      await menu.locator("summary").click();
      const otherMenu = page.locator("[data-section-row]").nth(0).locator("[data-section-menu]");
      await otherMenu.locator("summary").click();
      await expect.poll(() => menu.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(false);
      await expect.poll(() => otherMenu.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(true);
      expect(state.draftEditPosts).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("owns a highlit drag before native Selection can expand", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    let watchingPointerMoves = false;
    page.on("request", (request) => {
      if (watchingPointerMoves && request.url().includes("/api/")) state.pointermoveApiCalls += 1;
    });
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("rendered source fragment has no layout box");

      // First use the real page event entry to create the persistent core highlight.
      await page.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");

      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("rendered target fragment has no layout box");
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      watchingPointerMoves = true;
      await page.mouse.move(targetBox.x + Math.min(34, targetBox.width - 4), targetBox.y + targetBox.height / 2);

      const duringDrag = await page.evaluate(() => {
        const selection = window.getSelection();
        return {
          text: selection?.toString() ?? "",
          rangeCount: selection?.rangeCount ?? 0,
          dragging: document.querySelector<HTMLElement>("#draft-editor-shell")?.dataset.businessDragging,
          indicatorCount: document.querySelectorAll("[data-draft-drop-indicator]").length,
          coreHighlight: document.querySelector<HTMLElement>(
            '[data-editor-paragraph="paragraph_a"] [data-text-fragment]',
          )?.classList.contains("is-selected"),
          capturedTargets: [...document.querySelectorAll<HTMLElement>("[data-editor-text], [data-editor-paragraph]")]
            .filter((target) => target.hasPointerCapture?.(1)).length,
        };
      });
      expect(duringDrag.text).toBe("");
      expect(duringDrag.rangeCount).toBe(0);
      expect(duringDrag.dragging).toBe("true");
      expect(duringDrag.indicatorCount).toBe(1);
      expect(duringDrag.coreHighlight).toBe(true);
      expect(duringDrag.capturedTargets).toBeGreaterThan(0);
      await page.mouse.up();
      watchingPointerMoves = false;

      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("move_selection");
      expect(state.lastEdit?.schema_version).toBe(2);
      expect(state.pointermoveApiCalls).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("applies the returned result selection as the active selection, enabling immediate delete and re-drag", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const movedText = "alpha";
    const resultText = `【mock移入:${movedText}】`;
    const state = await installReview(page, reviewSnapshot(), undefined, {
      resultSelection: (snapshot) => {
        const b = snapshot.paragraphs.find((item) => item.paragraph_id === "paragraph_b");
        if (b === undefined) throw new Error("result selection snapshot has no paragraph_b");
        const index = b.text.indexOf(resultText);
        return resultSelectionResponse(
          snapshot,
          "paragraph_b",
          index,
          index + Array.from(resultText).length,
          resultText,
        );
      },
    });
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("rendered source fragment has no layout box");
      await page.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("rendered target fragment has no layout box");
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + Math.min(34, targetBox.width - 4), targetBox.y + targetBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);

      expect(await page.locator(".is-result-highlight").count()).toBe(0);
      const result = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment].is-selected');
      await expect.poll(async () => result.allTextContents()).toEqual([resultText]);
      await expect.poll(() => page.locator("#draft-delete").isDisabled()).toBe(false);
      expect(await page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment].is-selected').count()).toBe(0);

      const resultBox = await result.first().boundingBox();
      if (resultBox === null) throw new Error("result selection has no layout box");
      // The returned selection must be usable again: drag from inside it to paragraph_a.
      await page.mouse.move(resultBox.x + 4, resultBox.y + resultBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.min(34, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(2);
      expect(state.lastEdit?.operation).toBe("move_selection");

      // A genuine click inside the active selection collapses it to a caret.
      const selectedAfterDrag = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment].is-selected');
      await expect.poll(async () => selectedAfterDrag.count()).toBeGreaterThan(0);
      const selectedBox = await selectedAfterDrag.first().boundingBox();
      if (selectedBox === null) throw new Error("selected fragment has no layout box");
      await page.mouse.click(selectedBox.x + 2, selectedBox.y + selectedBox.height / 2);
      await expect.poll(() => state.caretPosts).toBe(1);
      expect(await page.locator(".is-selected").count()).toBe(0);
      expect(await page.locator(".draft-insertion-caret").count()).toBe(1);

      await page.reload();
      await page.locator("#draft-document").waitFor({ state: "visible" });
      expect(await page.locator(".is-selected").count()).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("uses ordinary success copy when an edit response has no result selection", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, reviewSnapshot());
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("rendered source fragment has no layout box");
      await page.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("rendered target fragment has no layout box");
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + Math.min(34, targetBox.width - 4), targetBox.y + targetBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      await expect.poll(() => page.locator("#status").innerText()).toBe("正文已移动。");
      expect(await page.locator(".is-selected").count()).toBe(0);
      expect(await page.locator(".is-result-highlight").count()).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("shows the frozen punctuation message and a distinct out-of-bounds message, never the generic copy", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, reviewSnapshot(), undefined, {
      draftEditStatus: 400,
      editErrorMessage: "draft editor selection cannot uniquely retain display punctuation",
    });
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("rendered source fragment has no layout box");
      await page.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("rendered target fragment has no layout box");
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + Math.min(34, targetBox.width - 4), targetBox.y + targetBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      await expect.poll(() => page.locator("#status").innerText()).toBe(
        "当前选区两侧的人工标点无法唯一保留；请把相关标点一并选中，或先调整标点后再移动。",
      );
      expect(await page.locator(".is-selected").count()).toBe(1);
    } finally {
      await page.close();
    }

    const boundsPage = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const boundsState = await installReview(boundsPage, reviewSnapshot(), undefined, {
      draftEditStatus: 400,
      editErrorMessage: "draft editor canonical offset is out of bounds",
    });
    try {
      const source = boundsPage.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("rendered source fragment has no layout box");
      await boundsPage.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await boundsPage.mouse.down();
      await boundsPage.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await boundsPage.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");
      const target = boundsPage.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("rendered target fragment has no layout box");
      await boundsPage.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await boundsPage.mouse.down();
      await boundsPage.mouse.move(targetBox.x + Math.min(34, targetBox.width - 4), targetBox.y + targetBox.height / 2);
      await boundsPage.mouse.up();
      await expect.poll(() => boundsState.draftEditPosts).toBe(1);
      await expect.poll(() => boundsPage.locator("#status").innerText()).toBe(
        "落点超出有效范围，内容未更改；请拖到另一处再试。",
      );
    } finally {
      await boundsPage.close();
    }
  });

  it("keeps a search hit visible on the second channel when it overlaps the active selection", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, reviewSnapshot(), undefined, {
      searchPage: (surface, query) => (surface === "draft" && query === "charlie"
        ? {
            surface: "draft",
            query,
            offset: 0,
            limit: 200,
            total: 1,
            next_cursor: null,
            matches: [{
              match_id: "match_b",
              paragraph_id: "paragraph_b",
              occurrence: 0,
              start_offset: 0,
              end_offset: 7,
              context: "charlie delta",
              source_id: null,
              source_display_name: null,
              person_name: null,
              start_ticks: null,
              end_ticks: null,
            }],
          }
        : { surface: "draft", query: "", offset: 0, limit: 0, total: 0, next_cursor: null, matches: [] }),
      resultSelection: (snapshot) => resultSelectionResponse(
        snapshot,
        "paragraph_b",
        0,
        Array.from("charlie delta").length,
        "charlie delta",
      ),
    });
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("rendered source fragment has no layout box");
      await page.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("rendered target fragment has no layout box");
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + Math.min(34, targetBox.width - 4), targetBox.y + targetBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      await expect.poll(() => target.getAttribute("class")).toContain("is-selected");

      const find = page.locator('[data-draft-find="draft"]');
      await find.click();
      const input = page.locator('[data-draft-find-panel="draft"] input');
      await input.fill("charlie");
      await expect.poll(async () => page.locator('[data-editor-paragraph="paragraph_b"] .is-search-match').count()).toBe(1);
      const styles = await page.locator('[data-editor-paragraph="paragraph_b"] .is-selected.is-search-match').first().evaluate((element) => {
        const style = getComputedStyle(element);
        return { background: style.backgroundColor, shadow: style.boxShadow };
      });
      expect(styles.background).toBe("rgb(216, 229, 241)");
      expect(styles.shadow).toContain("184, 139, 0");
      const matchCount = page.locator('[data-editor-paragraph="paragraph_b"] .is-search-match');
      await expect.poll(async () => matchCount.count()).toBe(1);
    } finally {
      await page.close();
    }
  });

  it("expands both panes beyond the old 1600px cap on a wide viewport", async () => {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    await installReview(page);
    try {
      const size = await page.locator("#app").evaluate((element) => {
        const style = getComputedStyle(element);
        return { width: style.width, maxWidth: style.maxWidth };
      });
      expect(size.maxWidth).toBe("none");
      expect(Number.parseFloat(size.width)).toBeGreaterThan(1600);
      const panes = await page.evaluate(() => {
        const draft = document.querySelector<HTMLElement>(".draft-document-pane");
        const source = document.querySelector<HTMLElement>(".draft-source-pane");
        return {
          draftWidth: draft?.getBoundingClientRect().width ?? 0,
          sourceWidth: source?.getBoundingClientRect().width ?? 0,
        };
      });
      expect(panes.draftWidth).toBeGreaterThan(700);
      expect(panes.sourceWidth).toBeGreaterThan(700);
    } finally {
      await page.close();
    }
  });

  it("moves a narration block by its wide drag handle without a text selection POST", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const narration: DraftEditorSnapshot["paragraphs"][number] = {
      paragraph_id: "paragraph_narration",
      kind: "narration",
      person: { person_id: null, name: "解说", role: "解说", local_speaker_id: null },
      text: "这里是解说内容。",
      section_title: "解说章节",
      narration_status: "draft",
      block_id: "block_narration",
      source_runs: [],
      exact_refs: [],
    };
    const base = reviewSnapshot();
    const state = await installReview(page, {
      ...base,
      paragraphs: [base.paragraphs[0]!, narration, base.paragraphs[1]!],
      blocks: [
        ...(base.blocks ?? []),
        {
          block_id: "block_narration",
          kind: "narration",
          text: narration.text,
          status: "draft",
          recorded_refs: [],
        },
      ],
    }, undefined, {
      selectionResponse: (snapshot) => {
        const paragraph = snapshot.paragraphs.find((item) => item.paragraph_id === "paragraph_narration");
        if (paragraph === undefined) throw new Error("missing narration paragraph");
        return {
          candidate_id: snapshot.candidate.candidate_id,
          surface: "draft",
          resolution: {
            direction: "forward",
            canonical_text: paragraph.text,
            refs: [],
            start_caret: null,
            end_caret: null,
            adjusted: false,
            degraded: false,
            degradation_reasons: [],
          },
          display_range: {
            anchor: { paragraph_id: "paragraph_narration", character_offset: 0, utf16_offset: 0 },
            focus: { paragraph_id: "paragraph_narration", character_offset: Array.from(paragraph.text).length, utf16_offset: paragraph.text.length },
          },
          correspondence_groups: [],
          resolution_hash: "9".repeat(64),
          narration_block_id: "block_narration",
          narration_text: paragraph.text,
          narration_status: "draft",
        };
      },
    });
    try {
      const copy = page.locator(".draft-narration-copy");
      const copyBox = await copy.boundingBox();
      if (copyBox === null) throw new Error("narration copy has no layout box");
      await page.mouse.move(copyBox.x + 6, copyBox.y + copyBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(copyBox.x + Math.max(12, copyBox.width - 4), copyBox.y + copyBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => copy.getAttribute("class")).toContain("is-selected");
      expect(state.draftEditPosts).toBe(0);

      const handle = page.locator("[data-narration-drag-handle]");
      expect(await handle.count()).toBe(1);
      const handleBox = await handle.boundingBox();
      if (handleBox === null) throw new Error("narration drag handle has no layout box");
      expect(handleBox.width).toBeGreaterThanOrEqual(44);
      // The handle must span the full narration card height so the top and
      // bottom edges of the card are also draggable (contract: wide drag zone,
      // not a three-dot icon).
      const cardBox = await copyBox;
      expect(handleBox.height).toBeGreaterThanOrEqual(cardBox.height * 0.9);
      expect(handleBox.y).toBeLessThanOrEqual(cardBox.y + 2);
      expect(handleBox.y + handleBox.height).toBeGreaterThanOrEqual(cardBox.y + cardBox.height - 2);

      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("target fragment has no layout box");
      // Grab the handle near its top edge: the whole card edge must be a
      // drag zone, not just the 28px-tall icon area.
      await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + 3);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + 20, targetBox.y + targetBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("move_selection");
      expect((state.lastEdit?.source as Record<string, unknown>)?.kind).toBe("narration_block");
      expect((state.lastEdit?.source as Record<string, unknown>)?.block_id).toBe("block_narration");

      // Layout regression: the status label must sit in the content column
      // (not squeezed into the 44px drag band) and the edit button must keep
      // its own width (not span the whole card).
      const label = page.locator(".draft-narration-label").first();
      const labelStyle = await label.evaluate((el) => {
        const style = window.getComputedStyle(el);
        const parent = el.parentElement;
        const parentStyle = parent === null ? null : window.getComputedStyle(parent);
        return {
          gridColumnStart: style.gridColumnStart,
          gridTemplateColumns: parentStyle?.gridTemplateColumns ?? "",
          width: el.getBoundingClientRect().width,
        };
      });
      expect(labelStyle.gridColumnStart).toBe("2");
      const editButton = page.locator('[data-narration-action="edit"]');
      const editBox = await editButton.boundingBox();
      const narrationCardBox = await page.locator(".draft-narration-card").first().boundingBox();
      if (editBox === null || narrationCardBox === null) throw new Error("narration layout boxes missing");
      expect(editBox.width).toBeLessThan(narrationCardBox.width * 0.6);
      await page.mouse.move(editBox.x + 2, editBox.y + editBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(editBox.x + editBox.width - 2, editBox.y + editBox.height / 2);
      await page.mouse.up();
      expect(state.draftEditPosts).toBe(1);
    } finally {
      await page.close();
    }
  });

  it("drags a narration block from its handle on a clean page without any selection", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const narration: DraftEditorSnapshot["paragraphs"][number] = {
      paragraph_id: "paragraph_narration",
      kind: "narration",
      person: { person_id: null, name: "解说", role: "解说", local_speaker_id: null },
      text: "这里是解说内容。",
      section_title: "解说章节",
      narration_status: "draft",
      block_id: "block_narration",
      source_runs: [],
      exact_refs: [],
    };
    const base = reviewSnapshot();
    const state = await installReview(page, {
      ...base,
      paragraphs: [base.paragraphs[0]!, narration, base.paragraphs[1]!],
      blocks: [
        ...(base.blocks ?? []),
        {
          block_id: "block_narration",
          kind: "narration",
          text: narration.text,
          status: "draft",
          recorded_refs: [],
        },
      ],
    });
    try {
      // Clean page: no selection exists, and no resolve is needed.  The drag
      // handle alone must start the narration move (spec: the wide drag zone
      // is the only entry; it must not require a prior text selection).
      const handle = page.locator("[data-narration-drag-handle]");
      await expect.poll(() => handle.count()).toBe(1);
      const handleBox = await handle.boundingBox();
      if (handleBox === null) throw new Error("narration drag handle has no layout box");
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("target fragment has no layout box");
      await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + 20, targetBox.y + targetBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.selectionPosts).toBe(0);
      expect(state.lastEdit?.operation).toBe("move_selection");
      const source = state.lastEdit?.source as Record<string, unknown> | undefined;
      expect(source?.kind).toBe("narration_block");
      expect(source?.block_id).toBe("block_narration");
      expect(source?.text).toBe(narration.text);
      expect(source?.status).toBe("draft");
      expect(Array.isArray(source?.recorded_refs)).toBe(true);
      // A handle press that never crosses the threshold must not move anything.
      await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
      await page.mouse.down();
      await page.mouse.up();
      expect(state.draftEditPosts).toBe(1);
    } finally {
      await page.close();
    }
  });

  it("establishes a new selection on the first drag that crosses the old selection", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, reviewSnapshot());
    try {
      // Establish an active selection over paragraph_a.
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("rendered source fragment has no layout box");
      await page.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");
      expect(state.selectionPosts).toBe(1);
      expect(state.draftEditPosts).toBe(0);

      // Drag a fresh native selection that starts outside the old selection
      // and sweeps across it; the first attempt must succeed.
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("rendered target fragment has no layout box");
      await page.mouse.move(targetBox.x + 4, targetBox.y + targetBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.selectionPosts).toBe(2);
      expect(state.draftEditPosts).toBe(0);
      const fresh = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment].is-selected');
      await expect.poll(async () => fresh.count()).toBeGreaterThan(0);
    } finally {
      await page.close();
    }
  });

  it("highlights the core-resolved range and starts a drag from its expanded edge", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, resolvedDisplaySnapshot(), undefined, {
      selectionResponse: resolvedSelectionResponse,
    });
    try {
      const source = page.locator('[data-editor-paragraph="resolved_paragraph"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("resolved source fragment has no layout box");
      await page.mouse.move(sourceBox.x + 6, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2);
      await page.mouse.up();
      const selected = page.locator('[data-editor-paragraph="resolved_paragraph"] [data-text-fragment].is-selected');
      await expect.poll(async () => selected.allTextContents()).toEqual(["主持人说：“请大家"]);

      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("resolved target fragment has no layout box");
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + Math.min(34, targetBox.width - 4), targetBox.y + targetBox.height / 2);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("move_selection");
      expect(state.pointermoveApiCalls).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("keeps a threshold click as caret placement and leaves outside drags to native selection", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const box = await source.boundingBox();
      if (box === null) throw new Error("rendered fragment has no layout box");
      await page.mouse.move(box.x + 5, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.up();
      await expect.poll(() => state.caretPosts).toBe(1);
      expect(state.draftEditPosts).toBe(0);

      const second = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const secondBox = await second.boundingBox();
      if (secondBox === null) throw new Error("second fragment has no layout box");
      await page.mouse.move(secondBox.x + 5, secondBox.y + secondBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(secondBox.x + Math.min(55, secondBox.width - 4), secondBox.y + secondBox.height / 2);
      const nativeSelection = await page.evaluate(() => window.getSelection()?.toString() ?? "");
      await page.mouse.up();
      expect(nativeSelection.length).toBeGreaterThan(0);
      expect(state.draftEditPosts).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("keeps punctuation input local, validates paste, deletes, cancels, and flushes once", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      const text = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const box = await text.boundingBox();
      if (box === null) throw new Error("draft text has no layout box");
      await page.mouse.click(box.x + box.width - 2, box.y + box.height / 2);
      await expect.poll(() => page.evaluate(
        () => document.activeElement?.classList.contains("draft-punctuation-input") ?? false,
      )).toBe(true);

      await page.keyboard.type("！！！！！！！");
      expect(state.draftEditPosts).toBe(0);
      expect(await text.innerText()).toContain("alpha bravo！！！！！！！");
      const textBeforeVisibleCaret = await text.evaluate((element) => {
        const caret = element.querySelector(".draft-insertion-caret");
        if (caret === null) return null;
        let value = "";
        for (const child of element.childNodes) {
          if (child === caret) break;
          value += child.textContent ?? "";
        }
        return value;
      });
      expect(textBeforeVisibleCaret).toBe("alpha bravo！！！！！！！");

      await page.evaluate(() => {
        const input = document.querySelector<HTMLInputElement>(".draft-punctuation-input");
        if (input === null) throw new Error("punctuation input is not mounted");
        const transfer = new DataTransfer();
        transfer.setData("text/plain", "！！");
        input.dispatchEvent(new ClipboardEvent("paste", {
          bubbles: true,
          cancelable: true,
          clipboardData: transfer,
        }));
      });
      expect(state.draftEditPosts).toBe(0);
      expect(await page.locator("#status").innerText()).toContain(
        "一个位置最多输入 8 个标点；本次粘贴未加入。",
      );
      await page.keyboard.press("Backspace");
      expect(await text.innerText()).toContain("alpha bravo！！！！！！");
      expect(state.draftEditPosts).toBe(0);
      await page.keyboard.press("Escape");
      expect(await page.locator(".draft-punctuation-input").count()).toBe(0);
      expect(await text.innerText()).toBe("alpha bravo");
      expect(state.draftEditPosts).toBe(0);

      await page.mouse.click(box.x + box.width - 2, box.y + box.height / 2);
      await page.keyboard.type("！");
      await page.locator("#draft-mode-sections").click();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("punctuation_edit");
      expect(Object.keys(state.lastEdit?.payload as Record<string, unknown>).sort()).toEqual([
        "block_id",
        "end_utf16_offset",
        "paragraph_id",
        "replacement",
        "start_utf16_offset",
      ]);
      expect((state.lastEdit?.payload as Record<string, unknown>).replacement).toBe("！");
      await expect.poll(() => page.locator("#draft-mode-sections").getAttribute("aria-selected")).toBe("true");
      expect(await page.locator("#draft-document").innerText()).toContain("alpha bravo！");
    } finally {
      await page.close();
    }
  });

  it("closes unchanged sessions before mode changes and keeps an emoji caret visible", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, punctuationEmojiSnapshot());
    try {
      const text = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const box = await text.boundingBox();
      if (box === null) throw new Error("emoji punctuation text has no layout box");
      await page.mouse.click(box.x + box.width - 2, box.y + box.height / 2);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);
      await page.locator("#draft-mode-sections").click();
      await expect.poll(() => page.locator("#draft-mode-sections").getAttribute("aria-selected")).toBe("true");
      expect(await page.locator(".draft-punctuation-input").count()).toBe(0);
      expect(state.draftEditPosts).toBe(0);

      await page.locator("#draft-mode-body").click();
      const bodyText = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const bodyBox = await bodyText.boundingBox();
      if (bodyBox === null) throw new Error("body punctuation text has no layout box");
      await page.mouse.click(bodyBox.x + bodyBox.width - 2, bodyBox.y + bodyBox.height / 2);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);
      await page.keyboard.press("Tab");
      expect(await page.locator(".draft-punctuation-input").count()).toBe(0);

      await page.mouse.click(bodyBox.x + bodyBox.width - 2, bodyBox.y + bodyBox.height / 2);
      await page.keyboard.type("！！");
      expect(state.draftEditPosts).toBe(0);
      const typedPrefix = await bodyText.evaluate((element) => {
        const caret = element.querySelector(".draft-insertion-caret");
        if (caret === null) return null;
        let value = "";
        for (const child of element.childNodes) {
          if (child === caret) break;
          value += child.textContent ?? "";
        }
        return value;
      });
      expect(typedPrefix).toBe("你好😀世界！！");
      await page.keyboard.press("Backspace");
      const deletedPrefix = await bodyText.evaluate((element) => {
        const caret = element.querySelector(".draft-insertion-caret");
        if (caret === null) return null;
        let value = "";
        for (const child of element.childNodes) {
          if (child === caret) break;
          value += child.textContent ?? "";
        }
        return value;
      });
      expect(deletedPrefix).toBe("你好😀世界！");
      await page.keyboard.press("Enter");
      await expect.poll(() => state.draftEditPosts).toBe(1);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("reopens a punctuation session on a second click without swallowing the click", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      const textA = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const boxA = await textA.boundingBox();
      if (boxA === null) throw new Error("paragraph_a text has no layout box");
      await page.mouse.click(boxA.x + boxA.width - 2, boxA.y + boxA.height / 2);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);

      // Second click on a different position: the unchanged session must
      // close without rebuilding the DOM, so the browser synthesizes the
      // click and the new session opens with a caret resolve.
      const textB = page.locator('[data-editor-paragraph="paragraph_b"] [data-editor-text]');
      const boxB = await textB.boundingBox();
      if (boxB === null) throw new Error("paragraph_b text has no layout box");
      const caretBefore = state.caretPosts;
      await page.mouse.click(boxB.x + boxB.width - 2, boxB.y + boxB.height / 2);
      await expect.poll(() => state.caretPosts).toBe(caretBefore + 1);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);
      expect(state.draftEditPosts).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("selects text while an unchanged punctuation session is open", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      const textA = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const boxA = await textA.boundingBox();
      if (boxA === null) throw new Error("paragraph_a text has no layout box");
      await page.mouse.click(boxA.x + boxA.width - 2, boxA.y + boxA.height / 2);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);

      // Drag a selection while the session is open and unchanged: the
      // pointerdown must not destroy the DOM under the press, so the drag
      // anchors and produces a selection with a resolve request.
      const selectionBefore = state.selectionPosts;
      await page.mouse.move(boxA.x + 4, boxA.y + boxA.height / 2);
      await page.mouse.down();
      await page.mouse.move(boxA.x + Math.max(12, boxA.width - 4), boxA.y + boxA.height / 2, { steps: 5 });
      await page.mouse.up();
      await expect.poll(() => state.selectionPosts).toBe(selectionBefore + 1);
      const selected = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment].is-selected');
      await expect.poll(async () => selected.count()).toBeGreaterThan(0);
      expect(state.draftEditPosts).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("keeps the paragraph DOM stable when closing an unchanged punctuation session", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      const textA = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const boxA = await textA.boundingBox();
      if (boxA === null) throw new Error("paragraph_a text has no layout box");
      await page.mouse.click(boxA.x + boxA.width - 2, boxA.y + boxA.height / 2);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);
      const nodeId = await textA.evaluate((element) => {
        const row = element.closest("[data-editor-paragraph]");
        return row === null ? null : row.getAttribute("data-paragraph-uid") ?? String(row);
      });

      // Close the unchanged session by pressing Tab; the paragraph element
      // must not be rebuilt (its identity stays the same).
      await page.keyboard.press("Tab");
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(0);
      const nodeIdAfter = await textA.evaluate((element) => {
        const row = element.closest("[data-editor-paragraph]");
        return row === null ? null : row.getAttribute("data-paragraph-uid") ?? String(row);
      });
      expect(nodeIdAfter).toBe(nodeId);
      expect(state.draftEditPosts).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("selects source text on the first drag while a dirty punctuation session is open", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      // Open a punctuation session and make it dirty (typed but unsaved).
      const textA = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const boxA = await textA.boundingBox();
      if (boxA === null) throw new Error("paragraph_a text has no layout box");
      await page.mouse.click(boxA.x + boxA.width - 2, boxA.y + boxA.height / 2);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);
      await page.keyboard.type("！");
      expect(state.draftEditPosts).toBe(0);

      // First drag in the source pane must produce a selection AND trigger
      // the punctuation save; the gesture must not be discarded.
      const sourceText = page.locator('[data-editor-paragraph="source_paragraph_a"] [data-editor-text]');
      const sourceBox = await sourceText.boundingBox();
      if (sourceBox === null) throw new Error("source text has no layout box");
      const selectionBefore = state.selectionPosts;
      await page.mouse.move(sourceBox.x + 4, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width - 4), sourceBox.y + sourceBox.height / 2, { steps: 5 });
      await page.mouse.up();
      // The first source drag must reach the selection resolver (the gesture
      // was previously discarded by the dirty branch) and the dirty session
      // must save in parallel.
      await expect.poll(() => state.selectionPosts).toBe(selectionBefore + 1);
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("punctuation_edit");
    } finally {
      await page.close();
    }
  });

  it("drags a narration block while a dirty punctuation session is open", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const narration: DraftEditorSnapshot["paragraphs"][number] = {
      paragraph_id: "paragraph_narration",
      kind: "narration",
      person: { person_id: null, name: "解说", role: "解说", local_speaker_id: null },
      text: "这里是解说内容。",
      section_title: "解说章节",
      narration_status: "draft",
      block_id: "block_narration",
      source_runs: [],
      exact_refs: [],
    };
    const base = reviewSnapshot();
    const state = await installReview(page, {
      ...base,
      paragraphs: [base.paragraphs[0]!, narration, base.paragraphs[1]!],
      blocks: [
        ...(base.blocks ?? []),
        {
          block_id: "block_narration",
          kind: "narration",
          text: narration.text,
          status: "draft",
          recorded_refs: [],
        },
      ],
    });
    try {
      // Open a punctuation session and make it dirty.
      const textA = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const boxA = await textA.boundingBox();
      if (boxA === null) throw new Error("paragraph_a text has no layout box");
      await page.mouse.click(boxA.x + boxA.width - 2, boxA.y + boxA.height / 2);
      await expect.poll(() => page.locator(".draft-punctuation-input").count()).toBe(1);
      await page.keyboard.type("！");
      expect(state.draftEditPosts).toBe(0);

      // Dragging the narration handle must save the punctuation and still
      // produce the narration move on the first gesture.
      const handle = page.locator("[data-narration-drag-handle]");
      await expect.poll(() => handle.count()).toBe(1);
      const handleBox = await handle.boundingBox();
      if (handleBox === null) throw new Error("narration drag handle has no layout box");
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("target fragment has no layout box");
      await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + 20, targetBox.y + targetBox.height / 2, { steps: 5 });
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("punctuation_edit");
      await expect.poll(() => state.draftEditPosts).toBe(2);
      expect(state.lastEdit?.operation).toBe("move_selection");
      expect((state.lastEdit?.source as Record<string, unknown>)?.kind).toBe("narration_block");
    } finally {
      await page.close();
    }
  });

  it("re-grabs a draft selection immediately and moves on the first drag", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, undefined, undefined, {
      selectionDelayMs: 250,
    });
    try {
      // First draft drag: select text in paragraph_a.
      const textA = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const boxA = await textA.boundingBox();
      if (boxA === null) throw new Error("paragraph_a text has no layout box");
      await page.mouse.move(boxA.x + 4, boxA.y + boxA.height / 2);
      await page.mouse.down();
      await page.mouse.move(boxA.x + Math.max(12, boxA.width - 4), boxA.y + boxA.height / 2, { steps: 4 });
      await page.mouse.up();
      // Wait for the resolve to complete so the view selection is set, then
      // verify the drop path works with an established selection.
      await page.waitForTimeout(400);
      // Re-grab inside the pending native selection and drag to paragraph_b.
      const target = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("paragraph_b target has no layout box");
      await page.mouse.move(boxA.x + 8, boxA.y + boxA.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + 20, targetBox.y + targetBox.height / 2, { steps: 5 });
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("move_selection");
      expect((state.lastEdit?.source as Record<string, unknown>)?.kind).toBe("resolved_selection");
    } finally {
      await page.close();
    }
  });

  it("rejects a cross-ref source resolve with the unrecognized-selection status", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, undefined, undefined, {
      // The server rejects a source selection that crosses a person
      // boundary with this message; the UI must surface the friendly
      // status instead of a technical error.
      selectionResolveStatus: 400,
      selectionResolveMessage: "draft editor selection cannot cross a person",
    });
    try {
      const sourceText = page.locator('[data-editor-paragraph="source_paragraph_a"] [data-editor-text]');
      const sourceBox = await sourceText.boundingBox();
      if (sourceBox === null) throw new Error("source text has no layout box");
      // Select inside the right pane and release there; the resolve fails.
      await page.mouse.move(sourceBox.x + 4, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + sourceBox.width * 0.5, sourceBox.y + sourceBox.height / 2, { steps: 3 });
      await page.mouse.up();
      // The resolve fails; no insert must be issued and the friendly
      // unrecognized-selection status must appear.
      await expect.poll(() => state.selectionPosts).toBe(1);
      await expect.poll(() => page.locator("#status").innerText()).toContain("无法识别本次文字选区");
      expect(state.draftEditPosts).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("maps the orphaned-punctuation drop 400 to the retain-punctuation status", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, undefined, undefined, {
      // A drop that would orphan a display-only punctuation prefix is a
      // business-correct rejection; spec 1869 requires the friendly
      // retain-punctuation status, not the generic drop message.
      draftEditStatus: 400,
      editErrorMessage: "draft editor caret leaves a punctuation prefix orphaned",
    });
    try {
      const sourceText = page.locator('[data-editor-paragraph="source_paragraph_a"] [data-editor-text]');
      const sourceBox = await sourceText.boundingBox();
      if (sourceBox === null) throw new Error("source text has no layout box");
      const target = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("draft target has no layout box");
      await page.mouse.move(sourceBox.x + 4, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width * 0.6), sourceBox.y + sourceBox.height / 2, { steps: 3 });
      await page.mouse.up();
      await expect.poll(() =>
        page.locator('[data-editor-paragraph="source_paragraph_a"] [data-text-fragment].is-selected').count(),
      ).toBeGreaterThan(0);
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + 20, targetBox.y + targetBox.height / 2, { steps: 4 });
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      await expect.poll(() => page.locator("#status").innerText()).toContain("人工标点无法唯一保留");
    } finally {
      await page.close();
    }
  });

  it("recovers after a failed source resolve: narration drag and fresh insert still work", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const narration: DraftEditorSnapshot["paragraphs"][number] = {
      paragraph_id: "paragraph_narration",
      kind: "narration",
      person: { person_id: null, name: "解说", role: "解说", local_speaker_id: null },
      text: "这里是解说内容。",
      section_title: "解说章节",
      narration_status: "draft",
      block_id: "block_narration",
      source_runs: [],
      exact_refs: [],
    };
    const base = reviewSnapshot();
    const state = await installReview(page, {
      ...base,
      paragraphs: [base.paragraphs[0]!, narration, base.paragraphs[1]!],
      blocks: [
        ...(base.blocks ?? []),
        {
          block_id: "block_narration",
          kind: "narration",
          text: narration.text,
          status: "draft",
          recorded_refs: [],
        },
      ],
    }, undefined, {
      // Only the first resolve fails (cross-person); later ones succeed.
      selectionResolveFailFirst: true,
      selectionResolveMessage: "draft editor selection cannot cross a person",
    });
    try {
      // Step 1: force a failed source resolve (cross-ref range).
      const sourceText = page.locator('[data-editor-paragraph="source_paragraph_a"] [data-editor-text]');
      const sourceBox = await sourceText.boundingBox();
      if (sourceBox === null) throw new Error("source text has no layout box");
      await page.mouse.move(sourceBox.x + 4, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + sourceBox.width * 0.5, sourceBox.y + sourceBox.height / 2, { steps: 3 });
      await page.mouse.up();
      await expect.poll(() => state.selectionPosts).toBe(1);
      await expect.poll(() => page.locator("#status").innerText()).toContain("无法识别本次文字选区");
      expect(state.draftEditPosts).toBe(0);

      // Step 2: narration drag handle must still move the narration block.
      const handle = page.locator("[data-narration-drag-handle]");
      const handleBox = await handle.boundingBox();
      if (handleBox === null) throw new Error("narration handle has no layout box");
      const targetB = page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();
      const targetBBox = await targetB.boundingBox();
      if (targetBBox === null) throw new Error("narration target has no layout box");
      await page.mouse.move(handleBox.x + handleBox.width / 2, handleBox.y + handleBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBBox.x + 20, targetBBox.y + targetBBox.height / 2, { steps: 6 });
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("move_selection");
      expect((state.lastEdit?.source as Record<string, unknown>)?.kind).toBe("narration_block");

      // Step 3: a fresh small source select + insert must also succeed.
      const editsBefore = state.draftEditPosts;
      await page.mouse.move(sourceBox.x + 4, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + Math.max(12, sourceBox.width * 0.3), sourceBox.y + sourceBox.height / 2, { steps: 3 });
      await page.mouse.up();
      await expect.poll(() => state.selectionPosts).toBe(2);
      const targetC = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const targetCBox = await targetC.boundingBox();
      if (targetCBox === null) throw new Error("draft target after recovery has no layout box");
      await page.mouse.move(sourceBox.x + 20, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetCBox.x + 20, targetCBox.y + targetCBox.height / 2, { steps: 6 });
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(editsBefore + 1);
      expect(state.lastEdit?.operation).toBe("insert_source_refs");
    } finally {
      await page.close();
    }
  });

  it("keeps a draft-pane selection drag a selection, not a move", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      // Drag out a selection inside the draft pane: the pointer stays over
      // the draft pane while selecting, so the gesture must NOT upgrade to a
      // move (G1 only applies to the cross-pane source gesture).
      const textA = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const boxA = await textA.boundingBox();
      if (boxA === null) throw new Error("paragraph_a text has no layout box");
      await page.mouse.move(boxA.x + 4, boxA.y + boxA.height / 2);
      await page.mouse.down();
      await page.mouse.move(boxA.x + Math.max(12, boxA.width - 4), boxA.y + boxA.height / 2, { steps: 4 });
      await page.mouse.up();
      // A selection resolve was issued, not a move.
      await expect.poll(() => state.selectionPosts).toBe(1);
      expect(state.draftEditPosts).toBe(0);
    } finally {
      await page.close();
    }
  });

  it("moves a punctuation caret by Unicode code point and navigates adjacent paragraphs", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, punctuationEmojiNavigationSnapshot());
    try {
      const text = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const box = await text.boundingBox();
      if (box === null) throw new Error("arrow punctuation text has no layout box");
      const prefix = async (paragraphId: string): Promise<string | null> => page.locator(
        `[data-editor-paragraph="${paragraphId}"] [data-editor-text]`,
      ).evaluate((element) => {
        const caret = element.querySelector(".draft-insertion-caret");
        if (caret === null) return null;
        let value = "";
        for (const child of element.childNodes) {
          if (child === caret) break;
          value += child.textContent ?? "";
        }
        return value;
      });

      await page.mouse.click(box.x + box.width - 2, box.y + box.height / 2);
      await expect.poll(() => prefix("paragraph_a")).toBe("你好😀世界");
      await page.keyboard.press("ArrowLeft");
      await expect.poll(() => prefix("paragraph_a")).toBe("你好😀世");
      await page.keyboard.press("ArrowLeft");
      await expect.poll(() => prefix("paragraph_a")).toBe("你好😀");
      await page.keyboard.press("ArrowLeft");
      await expect.poll(() => prefix("paragraph_a")).toBe("你好");
      await page.keyboard.press("ArrowRight");
      await expect.poll(() => prefix("paragraph_a")).toBe("你好😀");
      expect(state.draftEditPosts).toBe(0);

      await page.keyboard.press("ArrowDown");
      await expect.poll(() => page.locator(
        '[data-editor-paragraph="paragraph_b"] .draft-insertion-caret',
      ).count()).toBe(1);
      await expect.poll(() => prefix("paragraph_a")).toBe(null);
      expect(await prefix("paragraph_b")).not.toBe(null);
      expect(await page.locator(".draft-punctuation-input").count()).toBe(1);
      await page.keyboard.press("ArrowUp");
      await expect.poll(() => prefix("paragraph_a")).not.toBe(null);
      expect(await page.locator(".draft-punctuation-input").count()).toBe(1);
    } finally {
      await page.close();
    }
  });

  it("flushes once before arrow navigation and preserves a dirty preview on failure", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, punctuationEmojiSnapshot());
    try {
      const text = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const box = await text.boundingBox();
      if (box === null) throw new Error("dirty arrow punctuation text has no layout box");
      const prefix = async (): Promise<string | null> => text.evaluate((element) => {
        const caret = element.querySelector(".draft-insertion-caret");
        if (caret === null) return null;
        let value = "";
        for (const child of element.childNodes) {
          if (child === caret) break;
          value += child.textContent ?? "";
        }
        return value;
      });
      await page.mouse.click(box.x + box.width - 2, box.y + box.height / 2);
      await page.keyboard.type("！");
      await page.keyboard.press("ArrowLeft");
      await expect.poll(() => state.draftEditPosts).toBe(1);
      await expect.poll(prefix).toBe("你好😀世界");
      await page.keyboard.press("ArrowRight");
      await expect.poll(prefix).toBe("你好😀世界！");
      expect(state.draftEditPosts).toBe(1);
      expect(await page.locator(".draft-punctuation-input").count()).toBe(1);
      expect(state.lastEdit?.operation).toBe("punctuation_edit");
    } finally {
      await page.close();
    }

    const failedPage = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const failedState = await installReview(
      failedPage,
      punctuationEmojiSnapshot(),
      undefined,
      { draftEditStatus: 400 },
    );
    try {
      const failedText = failedPage.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const failedBox = await failedText.boundingBox();
      if (failedBox === null) throw new Error("failed arrow punctuation text has no layout box");
      await failedPage.mouse.click(
        failedBox.x + failedBox.width - 2,
        failedBox.y + failedBox.height / 2,
      );
      await failedPage.keyboard.type("！");
      await failedPage.keyboard.press("ArrowLeft");
      await expect.poll(() => failedState.draftEditPosts).toBe(1);
      expect(await failedPage.locator(".draft-punctuation-input").count()).toBe(1);
      expect(await failedText.innerText()).toBe("你好😀世界！");
      const failedPrefix = await failedText.evaluate((element) => {
        const caret = element.querySelector(".draft-insertion-caret");
        if (caret === null) return null;
        let value = "";
        for (const child of element.childNodes) {
          if (child === caret) break;
          value += child.textContent ?? "";
        }
        return value;
      });
      expect(failedPrefix).toBe("你好😀世界！");
    } finally {
      await failedPage.close();
    }
  });

  it("keeps a dirty local punctuation preview when its frozen-basis flush fails", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(
      page,
      punctuationEmojiSnapshot(),
      undefined,
      { draftEditStatus: 400 },
    );
    try {
      const text = page.locator('[data-editor-paragraph="paragraph_a"] [data-editor-text]');
      const box = await text.boundingBox();
      if (box === null) throw new Error("failed-flush punctuation text has no layout box");
      await page.mouse.click(box.x + box.width - 2, box.y + box.height / 2);
      await page.keyboard.type("！");
      expect(state.draftEditPosts).toBe(0);
      expect(await text.innerText()).toBe("你好😀世界！");

      await page.locator("#draft-mode-sections").click();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(await page.locator("#draft-mode-body").getAttribute("aria-selected")).toBe("true");
      expect(await page.locator(".draft-punctuation-input").count()).toBe(1);
      expect(await text.innerText()).toBe("你好😀世界！");

      await page.keyboard.press("Escape");
      expect(await page.locator(".draft-punctuation-input").count()).toBe(0);
      expect(await text.innerText()).toBe("你好😀世界");
      expect(state.draftEditPosts).toBe(1);
    } finally {
      await page.close();
    }
  });

  it("loads, scrolls, saves, reloads, and reverses a 300-mark punctuation draft", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const initial = punctuationLongSnapshot("！");
    const undo = punctuationLongSnapshot("");
    const redo = punctuationLongSnapshot("！！");
    const state = await installReview(page, initial, { undo, redo });
    try {
      const rows = page.locator("#draft-document [data-editor-paragraph]");
      expect(await rows.count()).toBe(300);
      await page.evaluate(() => {
        const target = window as Window & { __draftLongTasks?: number[]; __draftLongTaskObserver?: PerformanceObserver };
        target.__draftLongTasks = [];
        if (typeof PerformanceObserver !== "undefined"
          && PerformanceObserver.supportedEntryTypes.includes("longtask")) {
          const observer = new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) target.__draftLongTasks?.push(entry.duration);
          });
          observer.observe({ type: "longtask", buffered: true });
          target.__draftLongTaskObserver = observer;
        }
      });
      const last = rows.last();
      await last.scrollIntoViewIfNeeded();
      expect(await last.isVisible()).toBe(true);
      const text = last.locator("[data-editor-text]");
      const box = await text.boundingBox();
      if (box === null) throw new Error("long punctuation text has no layout box");
      await page.mouse.click(box.x + box.width - 2, box.y + box.height / 2);
      await expect.poll(() => page.evaluate(
        () => document.activeElement?.classList.contains("draft-punctuation-input") ?? false,
      )).toBe(true);
      await page.keyboard.type("！");
      expect(state.draftEditPosts).toBe(0);
      expect(await text.innerText()).toContain("长稿299内容！！");
      await page.locator("#draft-mode-sections").click();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect((state.lastEdit?.payload as Record<string, unknown>)?.replacement).toBe("！");
      const longTasks = await page.evaluate(() => {
        const target = window as Window & { __draftLongTasks?: number[]; __draftLongTaskObserver?: PerformanceObserver };
        for (const entry of target.__draftLongTaskObserver?.takeRecords() ?? []) {
          target.__draftLongTasks?.push(entry.duration);
        }
        target.__draftLongTaskObserver?.disconnect();
        return target.__draftLongTasks ?? [];
      });
      expect(longTasks.every((duration) => duration <= 50)).toBe(true);
      await page.reload();
      await page.locator("#draft-document").waitFor({ state: "visible" });
      expect(await page.locator("#draft-document").innerText()).toContain("长稿299内容！！");
      await page.locator("#draft-undo").click();
      await expect.poll(() => state.undoPosts).toBe(1);
      await page.locator("#draft-mode-body").click();
      expect(await page.locator('[data-editor-paragraph="punctuation_long_299"]').innerText()).toBe("长稿299内容");
      await page.locator("#draft-redo").click();
      await expect.poll(() => state.redoPosts).toBe(1);
      await page.locator("#draft-mode-body").click();
      expect(await page.locator('[data-editor-paragraph="punctuation_long_299"]').innerText()).toBe("长稿299内容！！");
    } finally {
      await page.close();
    }
  });

  it("cancels an owned drag with Escape without writing", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const box = await source.boundingBox();
      if (box === null) throw new Error("rendered fragment has no layout box");
      await page.mouse.move(box.x + 5, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + 50, box.y + box.height / 2);
      await page.keyboard.press("Escape");
      await page.mouse.up();
      expect(state.draftEditPosts).toBe(0);
      const shell = await page.locator("#draft-editor-shell").getAttribute("data-business-dragging");
      expect(shell).not.toBe("true");
    } finally {
      await page.close();
    }
  });

  it("releases capture and leaves no write on pointercancel", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      const source = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const box = await source.boundingBox();
      if (box === null) throw new Error("rendered fragment has no layout box");
      await page.mouse.move(box.x + 5, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + 50, box.y + box.height / 2);
      await page.dispatchEvent("#draft-document", "pointercancel", { bubbles: true, pointerId: 1 });
      await page.mouse.up();
      expect(state.draftEditPosts).toBe(0);
      expect(await page.locator("#draft-editor-shell").getAttribute("data-business-dragging")).not.toBe("true");
    } finally {
      await page.close();
    }
  });

  it("shows chapter rows with one menu and reorders by a single drop", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      await page.locator("#draft-mode-sections").click();
      const rows = page.locator("[data-section-row]");
      expect(await rows.count()).toBe(2);
      expect(await rows.nth(0).textContent()).toContain("同名章节");
      expect(await rows.nth(0).textContent()).toContain("1 项");
      expect(await rows.nth(1).textContent()).toContain("同名章节");
      expect(await rows.nth(1).textContent()).toContain("1 项");
      expect(await rows.nth(0).locator("[data-section-menu]").textContent()).not.toContain("并入上一章");
      expect(await rows.nth(1).locator("[data-section-menu]").textContent()).toContain("并入上一章");
      expect(await rows.nth(0).locator("[data-section-menu]").textContent()).toContain("删除本章及内容");

      const titleBox = await rows.nth(0).locator("h3").boundingBox();
      if (titleBox === null) throw new Error("chapter title has no layout box");
      await page.mouse.move(titleBox.x + 4, titleBox.y + titleBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(titleBox.x + Math.min(48, titleBox.width), titleBox.y + titleBox.height / 2);
      await page.mouse.up();
      expect(state.draftEditPosts).toBe(0);
      expect(await page.locator("#draft-mode-body").getAttribute("aria-selected")).toBe("true");
      await page.locator("#draft-mode-sections").click();

      await page.evaluate(() => {
        (window as Window & { __draftScrollTargets?: string[] }).__draftScrollTargets = [];
        Element.prototype.scrollIntoView = function() {
          const paragraphId = (this as HTMLElement).dataset.editorParagraph;
          if (paragraphId !== undefined) {
            (window as Window & { __draftScrollTargets?: string[] }).__draftScrollTargets?.push(paragraphId);
          }
        };
      });
      await rows.nth(1).click();
      expect(await page.locator("#draft-mode-body").getAttribute("aria-selected")).toBe("true");
      await expect.poll(() => page.evaluate(
        () => (window as Window & { __draftScrollTargets?: string[] }).__draftScrollTargets ?? [],
      )).toContain("paragraph_b");
      await page.locator("#draft-mode-sections").click();
      await expect.poll(() => rows.count()).toBe(2);

      const source = await rows.nth(0).boundingBox();
      const target = await rows.nth(1).boundingBox();
      if (source === null || target === null) throw new Error("chapter row has no layout box");
      const handle = await rows.nth(0).locator("[data-section-drag-handle]").boundingBox();
      if (handle === null) throw new Error("chapter drag handle has no layout box");
      expect(handle.width).toBeGreaterThanOrEqual(32);
      expect(handle.height).toBeGreaterThanOrEqual(32);
      await page.mouse.move(source.x + 8, source.y + source.height / 2);
      await page.mouse.down();
      await page.mouse.move(target.x + 8, target.y + target.height - 5);
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("section_reorder");
      expect((state.lastEdit?.payload as Record<string, unknown>)?.before_heading_block_id).toBeNull();
      expect(await page.locator("#draft-editor-shell").evaluate((shell) => ({ ...shell.dataset })))
        .toMatchObject({
          editOperation: "section_reorder",
          editSelectionCaretRevalidationMs: "1",
          editImmutableChildWriteFsyncMs: "2",
          editProjectBriefTranscriptContextValidationMs: "3",
          editWorkflowSnapshotRefreshMs: "4",
          editDraftSnapshotRebuildMs: "5",
          editServerBeforeResponseMs: "6",
        });
    } finally {
      await page.close();
    }
  });

  it("updates the mounted section DOM immediately after shortcut undo and toolbar redo", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const initial = sectionHistorySnapshot(
      reviewSnapshot(),
      "candidate_a",
      1,
      [["heading_a", "第一章"], ["heading_b", "第二章"]],
      true,
      false,
    );
    const undone = sectionHistorySnapshot(
      initial,
      "candidate_undo",
      2,
      [["heading_b", "第二章（撤销后）"], ["heading_a", "第一章"]],
      true,
      true,
    );
    const redone = sectionHistorySnapshot(
      undone,
      "candidate_redo",
      3,
      [["heading_a", "第一章"], ["heading_b", "第二章"]],
      true,
      false,
    );
    const state = await installReview(page, initial, { undo: undone, redo: redone });
    try {
      await page.locator("#draft-mode-sections").click();
      const titles = page.locator("#draft-sections [data-section-row] h3");
      await expect.poll(() => titles.allTextContents()).toEqual(["第一章", "第二章"]);
      expect(await page.locator("#draft-sections").isVisible()).toBe(true);

      await page.keyboard.press("Meta+z");

      await expect.poll(() => state.undoPosts).toBe(1);
      expect(state.redoPosts).toBe(0);
      expect(state.draftEditorGets).toBe(1);
      expect(await page.locator("#draft-mode-sections").getAttribute("aria-selected"))
        .toBe("true");
      expect(await page.locator("#draft-sections").isVisible()).toBe(true);
      await expect.poll(() => titles.allTextContents()).toEqual([
        "第二章（撤销后）",
        "第一章",
      ]);

      await page.locator("#draft-redo").click();

      await expect.poll(() => state.redoPosts).toBe(1);
      expect(state.undoPosts).toBe(1);
      expect(state.draftEditorGets).toBe(1);
      expect(await page.locator("#draft-mode-sections").getAttribute("aria-selected"))
        .toBe("true");
      expect(await page.locator("#draft-sections").isVisible()).toBe(true);
      await expect.poll(() => titles.allTextContents()).toEqual(["第一章", "第二章"]);
    } finally {
      await page.close();
    }
  });

  it("records bounded client DOM and instrumentation timings with a mocked draft response", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, longReviewSnapshot());
    let watchingPointerMoves = false;
    page.on("request", (request) => {
      if (watchingPointerMoves && request.url().includes("/api/")) state.pointermoveApiCalls += 1;
    });
    try {
      const longTasks: number[] = [];
      await page.evaluate(() => {
        const target = window as Window & { __draftLongTasks?: number[]; __draftLongTaskObserver?: PerformanceObserver };
        target.__draftLongTasks = [];
        if (typeof PerformanceObserver !== "undefined"
          && PerformanceObserver.supportedEntryTypes.includes("longtask")) {
          const observer = new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) target.__draftLongTasks?.push(entry.duration);
          });
          observer.observe({ type: "longtask", buffered: true });
          target.__draftLongTaskObserver = observer;
        }
      });
      const before = await page.locator("#draft-editor-shell").evaluate(
        (shell) => ({ ...shell.dataset }),
      );
      const source = page.locator('[data-editor-paragraph="long_paragraph_0"] [data-text-fragment]').first();
      const sourceBox = await source.boundingBox();
      if (sourceBox === null) throw new Error("long source fragment has no layout box");
      const sourceY = sourceBox.y + Math.min(12, sourceBox.height / 2);
      await page.mouse.move(sourceBox.x + 6, sourceY);
      await page.mouse.down();
      await page.mouse.move(
        sourceBox.x + Math.max(12, Math.min(sourceBox.width - 5, 120)),
        sourceY,
      );
      await page.mouse.up();
      await expect.poll(() => source.getAttribute("class")).toContain("is-selected");

      const target = page.locator('[data-editor-paragraph="long_paragraph_2"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("long target fragment has no layout box");
      const sourceParagraph = page.locator('[data-editor-paragraph="long_paragraph_0"]');
      const targetParagraph = page.locator('[data-editor-paragraph="long_paragraph_2"]');
      const sourceTextBefore = await sourceParagraph.innerText();
      const targetTextBefore = await targetParagraph.innerText();
      await page.mouse.move(sourceBox.x + 8, sourceY);
      await page.mouse.down();
      watchingPointerMoves = true;
      await page.mouse.move(
        targetBox.x + Math.min(42, targetBox.width - 4),
        targetBox.y + Math.min(12, targetBox.height / 2),
        { steps: 40 },
      );
      watchingPointerMoves = false;
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.pointermoveApiCalls).toBe(0);

      const after = await page.locator("#draft-editor-shell").evaluate(
        (shell) => ({ ...shell.dataset }),
      );
      const sourceTextAfter = await sourceParagraph.innerText();
      const targetTextAfter = await targetParagraph.innerText();
      expect(sourceTextAfter).not.toBe(sourceTextBefore);
      expect(targetTextAfter).not.toBe(targetTextBefore);
      expect(targetTextAfter).toContain("【mock移入:");
      expect(Number(after.editPointermoveHitTestSamples)).toBeGreaterThanOrEqual(20);
      const measured = await page.evaluate(() => {
        const target = window as Window & { __draftLongTasks?: number[]; __draftLongTaskObserver?: PerformanceObserver };
        const observer = target.__draftLongTaskObserver;
        for (const entry of observer?.takeRecords() ?? []) {
          target.__draftLongTasks?.push(entry.duration);
        }
        observer?.disconnect();
        delete target.__draftLongTaskObserver;
        return target.__draftLongTasks ?? [];
      });
      longTasks.push(...measured);
      const trace = {
        measurement: "client_dom_instrumentation_mock",
        serverPerformance: "not_measured",
        fixture: { paragraphs: 10, nonEmptySections: 3, emptySections: 1 },
        before,
        after,
        longTasksMs: longTasks,
      };
      await page.evaluate((value) => {
        localStorage.setItem("m3-4-a-draft-edit-client-dom", JSON.stringify(value));
      }, trace);
      await page.context().storageState({
        path: "/private/tmp/m3-4-a-draft-edit-client-dom.json",
      });
      expect(Number(after.editPointermoveHitTestP95Ms)).toBeLessThanOrEqual(16);
      expect(longTasks.every((duration) => duration <= 50)).toBe(true);
    } finally {
      await page.evaluate(() => {
        const target = window as Window & { __draftLongTaskObserver?: PerformanceObserver };
        target.__draftLongTaskObserver?.disconnect();
        delete target.__draftLongTaskObserver;
      }).catch(() => undefined);
      await page.close();
    }
  });

  it("performs ten consecutive right-pane select-and-insert rounds without refresh", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page, undefined, undefined, {
      // The real server returns result_selection on every fresh insert 201;
      // mirror that so the second round starts from a draft ActiveSelection.
      resultSelection: (snapshot, body) => {
        const source = body?.source as Record<string, unknown> | undefined;
        const refs = source?.refs as Array<Record<string, unknown>> | undefined;
        const canonical = typeof source?.canonical_text === "string"
          ? source.canonical_text
          : "alpha bravo";
        const target = body?.target as Record<string, unknown> | undefined;
        const paragraphId = typeof target?.paragraph_id === "string"
          ? target.paragraph_id
          : "paragraph_a";
        const paragraph = snapshot.paragraphs.find((item) => item.paragraph_id === paragraphId);
        if (paragraph === undefined) throw new Error(`missing result paragraph ${paragraphId}`);
        // The mock appends the inserted content at the target offset; the
        // result selection must cover exactly the newly inserted occurrence.
        const index = typeof target?.utf16_offset === "number"
          ? target.utf16_offset
          : 0;
        const end = Math.min(paragraph.text.length, index + Array.from(canonical).length);
        const response: DraftEditorSelectionResponse = {
          candidate_id: snapshot.candidate.candidate_id,
          surface: "draft",
          resolution: {
            direction: "forward",
            canonical_text: canonical,
            refs: refs === undefined
              ? [{ ...paragraph.exact_refs[0]!, canonical_text: canonical }]
              : refs.map((ref) => ({ ...ref, canonical_text: canonical })) as DraftEditorSelectionResponse["resolution"]["refs"],
            start_caret: null,
            end_caret: null,
            adjusted: false,
            degraded: false,
            degradation_reasons: [],
          },
          display_range: {
            anchor: { paragraph_id: paragraphId, character_offset: index, utf16_offset: index },
            focus: { paragraph_id: paragraphId, character_offset: end, utf16_offset: end },
          },
          correspondence_groups: [],
          resolution_hash: "f".repeat(64),
        };
        return {
          surface: "draft",
          request: {
            anchor: { paragraph_id: paragraphId, offset: index, offset_encoding: "utf16" },
            focus: { paragraph_id: paragraphId, offset: end, offset_encoding: "utf16" },
          },
          response,
          accepted_degraded: true,
        };
      },
    });
    let watchingPointerMoves = false;
    page.on("request", (request) => {
      if (watchingPointerMoves && request.url().includes("/api/")) state.pointermoveApiCalls += 1;
    });
    try {
      const sourceText = page.locator('[data-editor-paragraph="source_paragraph_a"] [data-editor-text]');
      const sourceBox = await sourceText.boundingBox();
      if (sourceBox === null) throw new Error("source text has no layout box");

      // One shared helper for every round: select a right-pane range (real
      // Selection/Range + pointer events, pointerup inside the pane), wait
      // for the resolve and the visible ActiveSelection, then drag from
      // inside the active selection to the left pane.  The target box is
      // re-read on every round because the draft DOM changes after each
      // insert.
      async function sourceSelectAndInsert(
        startFraction: number,
        endFraction: number,
        targetLocator: import("playwright").Locator,
      ): Promise<void> {
        const box = sourceBox!;
        const selectionStart = box.x + Math.max(4, box.width * startFraction);
        const selectionEnd = box.x + Math.max(12, box.width * endFraction);
        const midY = box.y + box.height / 2;
        // Step 1: real pointer sequence in the right pane; pointerup stays inside it.
        await page.mouse.move(selectionStart, midY);
        await page.mouse.down();
        watchingPointerMoves = true;
        await page.mouse.move(selectionEnd, midY, { steps: 4 });
        await page.mouse.up();
        watchingPointerMoves = false;
        // Step 2: the resolve returns 200 and the right pane shows the active
        // selection.  Wait for the resolved status, not just a leftover
        // is-selected class from the previous round's DOM.
        await expect.poll(async () =>
          page.locator("#status").innerText(),
        ).toContain("已对齐可剪边界");
        await expect.poll(async () =>
          page.locator('[data-editor-paragraph="source_paragraph_a"] [data-text-fragment].is-selected').count(),
        ).toBeGreaterThan(0);
        // Step 3: a second pointer sequence drags from inside the active
        // selection to the left-pane target (box re-read each round).
        const targetBox = await targetLocator.boundingBox();
        if (targetBox === null) throw new Error("draft target has no layout box");
        await page.mouse.move(selectionStart + 4, midY);
        await page.mouse.down();
        watchingPointerMoves = true;
        await page.mouse.move(targetBox.x + Math.min(20, targetBox.width - 4), targetBox.y + targetBox.height / 2, { steps: 5 });
        await page.mouse.up();
        watchingPointerMoves = false;
      }

      const targetA = () => page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const targetB = () => page.locator('[data-editor-paragraph="paragraph_b"] [data-text-fragment]').first();

      // Ten consecutive rounds, no refresh, no controller rebuild: alternate
      // the right-pane ranges (alpha / bravo) and the left-pane targets.
      for (let round = 0; round < 10; round += 1) {
        const editsBefore = state.draftEditPosts;
        const selectionsBefore = state.selectionPosts;
        const pointerMovesBefore = state.pointermoveApiCalls;
        if (round % 2 === 0) {
          await sourceSelectAndInsert(0, 0.45, targetA());
        } else {
          await sourceSelectAndInsert(0.55, 0.95, targetB());
        }
        await expect.poll(() => state.draftEditPosts).toBe(editsBefore + 1);
        expect(state.selectionPosts).toBe(selectionsBefore + 1);
        expect(state.lastEdit?.operation).toBe("insert_source_refs");
        const generation = (state.lastEdit?.expected_checkpoint_ref as Record<string, unknown>)?.generation;
        expect(generation).toBe(round + 1);
        expect(state.pointermoveApiCalls).toBe(pointerMovesBefore);
      }
    } finally {
      await page.close();
    }
  });

  it("rejects one continuous source-to-draft gesture with zero writes and no lock", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      // G1 negative regression: press in the right pane, form a native
      // selection, and release over the left pane WITHOUT releasing first.
      // This single-gesture cross-pane flow is not a supported workflow: it
      // must not produce a draft-edit, a child, or a generation advance, and
      // must not lock the page.
      const sourceText = page.locator('[data-editor-paragraph="source_paragraph_a"] [data-editor-text]');
      const sourceBox = await sourceText.boundingBox();
      if (sourceBox === null) throw new Error("source text has no layout box");
      const target = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const targetBox = await target.boundingBox();
      if (targetBox === null) throw new Error("draft target has no layout box");
      await page.mouse.move(sourceBox.x + 4, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + sourceBox.width * 0.6, sourceBox.y + sourceBox.height / 2, { steps: 3 });
      await page.mouse.move(targetBox.x + 20, targetBox.y + targetBox.height / 2, { steps: 4 });
      await page.mouse.up();
      await page.waitForTimeout(400);
      expect(state.draftEditPosts).toBe(0);
      expect(state.selectionPosts).toBe(0);
      expect(state.lastEdit).toBeNull();
      // The page must not be locked: a follow-up ordinary source selection
      // still resolves and a two-sequence insert still works.
      await page.mouse.move(sourceBox.x + 4, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(sourceBox.x + sourceBox.width * 0.4, sourceBox.y + sourceBox.height / 2, { steps: 3 });
      await page.mouse.up();
      await expect.poll(() => state.selectionPosts).toBe(1);
      await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
      await page.mouse.down();
      await page.mouse.move(targetBox.x + 20, targetBox.y + targetBox.height / 2, { steps: 5 });
      await page.mouse.up();
      await expect.poll(() => state.draftEditPosts).toBe(1);
    } finally {
      await page.close();
    }
  });

  it("keeps menu writes private and starts a section split from a resolved body caret", async () => {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const state = await installReview(page);
    try {
      await page.locator("#draft-mode-sections").click();
      const rows = page.locator("[data-section-row]");
      page.once("dialog", (dialog) => void dialog.accept("改名后的章节"));
      await rows.nth(1).locator("summary").click();
      await rows.nth(1).getByText("重命名").click();
      await expect.poll(() => state.draftEditPosts).toBe(1);
      expect(state.lastEdit?.operation).toBe("section_rename");
      expect((state.lastEdit?.payload as Record<string, unknown>)?.heading_block_id).toBe("heading_b");

      await page.locator("#draft-mode-body").click();
      const paragraph = page.locator('[data-editor-paragraph="paragraph_a"] [data-text-fragment]').first();
      const box = await paragraph.boundingBox();
      if (box === null) throw new Error("paragraph has no layout box");
      await page.mouse.click(box.x + 3, box.y + box.height / 2);
      await expect.poll(() => state.caretPosts).toBe(1);
      expect(await page.locator("#draft-section-split").isDisabled()).toBe(false);
      page.once("dialog", (dialog) => void dialog.accept("新章节"));
      await page.locator("#draft-section-split").click();
      await expect.poll(() => state.draftEditPosts).toBe(2);
      expect(state.lastEdit?.operation).toBe("section_split");
      const payload = state.lastEdit?.payload as Record<string, unknown>;
      expect(payload.heading_block_id).toBe("heading_a");
      expect(payload.target).toEqual({ paragraph_id: "paragraph_a", block_id: "block_a", utf16_offset: expect.any(Number) });
    } finally {
      await page.close();
    }
  });
});

type ReviewState = {
  draftEditPosts: number;
  draftEditorGets: number;
  undoPosts: number;
  redoPosts: number;
  caretPosts: number;
  selectionPosts: number;
  pointermoveApiCalls: number;
  lastEdit: Record<string, unknown> | null;
};

async function installReview(
  page: Page,
  initialSnapshot: DraftEditorSnapshot = reviewSnapshot(),
  historySnapshots?: { undo: DraftEditorSnapshot; redo: DraftEditorSnapshot },
  options: {
    draftEditStatus?: number;
    selectionResponse?: (snapshot: DraftEditorSnapshot, surface: string) => DraftEditorSelectionResponse;
    resultSelection?: (
      snapshot: DraftEditorSnapshot,
      body: Record<string, unknown> | null,
    ) => {
      surface: "draft";
      request: DraftEditorSelectionRequest;
      response: DraftEditorSelectionResponse;
      accepted_degraded: boolean;
    };
    searchPage?: (surface: string, query: string) => unknown;
    editErrorMessage?: string;
    selectionDelayMs?: number;
    selectionResolveStatus?: number;
    selectionResolveMessage?: string;
    selectionResolveFailFirst?: boolean;
  } = {},
): Promise<ReviewState> {
  const state: ReviewState = {
    draftEditPosts: 0,
    draftEditorGets: 0,
    undoPosts: 0,
    redoPosts: 0,
    caretPosts: 0,
    selectionPosts: 0,
    pointermoveApiCalls: 0,
    lastEdit: null,
  };
  let currentSnapshot = initialSnapshot;
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const body = route.request().postDataJSON?.() as Record<string, unknown> | null;
    if (url.pathname === "/api/workflow/draft-editor") {
      state.draftEditorGets += 1;
      await json(route, currentSnapshot);
      return;
    }
    if (url.pathname === "/api/workflow/draft-transcript-window") {
      await json(route, transcriptWindow());
      return;
    }
    if (url.pathname === "/api/workflow/draft-selection-resolve") {
      state.selectionPosts += 1;
      if (options.selectionDelayMs !== undefined) {
        await new Promise((resolve) => setTimeout(resolve, options.selectionDelayMs!));
      }
      const surface = String(body?.surface ?? "draft");
      const selectionId = surface === "source"
        ? "source_paragraph_a"
        : (currentSnapshot.paragraphs[0]?.paragraph_id ?? "paragraph_a");
      const requestBody = body as {
        surface?: string;
        anchor?: { paragraph_id: string; offset: number; offset_encoding: string };
        focus?: { paragraph_id: string; offset: number; offset_encoding: string };
      } | null;
      const requestedAnchor = requestBody?.anchor;
      const requestedFocus = requestBody?.focus;
      const resolveStart = typeof requestedAnchor?.offset === "number"
        && requestedAnchor.paragraph_id === "source_paragraph_a"
        ? requestedAnchor.offset
        : 0;
      const resolveFocus = typeof requestedFocus?.offset === "number"
        && requestedFocus.paragraph_id === "source_paragraph_a"
        ? requestedFocus.offset
        : Math.min(5, Array.from("alpha bravo").length);
      if (options.selectionResolveStatus !== undefined
        || (options.selectionResolveFailFirst === true && state.selectionPosts === 1)) {
        await json(
          route,
          {
            error: {
              code: "invalid_workflow_change",
              message: options.selectionResolveMessage ?? "test resolve failure",
            },
          },
          options.selectionResolveStatus ?? 400,
        );
        return;
      }
      await json(route, options.selectionResponse === undefined
        ? (surface === "source"
            ? sourceSelectionResponse(currentSnapshot, resolveStart, resolveFocus)
            : selectionResponse(currentSnapshot, selectionId))
        : options.selectionResponse(currentSnapshot, surface));
      return;
    }
    if (url.pathname === "/api/workflow/draft-caret-resolve") {
      state.caretPosts += 1;
      await json(route, {
        candidate_id: currentSnapshot.candidate.candidate_id,
        paragraph_id: String(body?.paragraph_id ?? "paragraph_a"),
        character_offset: Number(body?.offset ?? 1),
        utf16_offset: Number(body?.offset ?? 1),
        boundary_id: "boundary_a",
        degraded: false,
        degradation_reason: null,
      });
      return;
    }
    if (url.pathname === "/api/workflow/draft-edit") {
      state.draftEditPosts += 1;
      state.lastEdit = body;
      if (options.draftEditStatus !== undefined) {
        await json(
          route,
          { error: { code: "invalid_workflow_change", message: options.editErrorMessage ?? "test flush failure" } },
          options.draftEditStatus,
        );
        return;
      }
      currentSnapshot = nextSnapshot(currentSnapshot, body);
      const resultSelection = options.resultSelection === undefined
        ? undefined
        : options.resultSelection(currentSnapshot, body);
      await json(route, {
        draft_editor: currentSnapshot,
        timing: serverTiming(),
        ...(resultSelection === undefined ? {} : { result_selection: resultSelection }),
      });
      return;
    }
    if (url.pathname === "/api/workflow/draft-undo") {
      state.undoPosts += 1;
      currentSnapshot = historySnapshots?.undo ?? currentSnapshot;
      await json(route, { draft_editor: currentSnapshot });
      return;
    }
    if (url.pathname === "/api/workflow/draft-redo") {
      state.redoPosts += 1;
      currentSnapshot = historySnapshots?.redo ?? currentSnapshot;
      await json(route, { draft_editor: currentSnapshot });
      return;
    }
    if (url.pathname.endsWith("/draft-search")) {
      await json(route, options.searchPage === undefined
        ? { surface: "draft", query: "", offset: 0, limit: 0, total: 0, next_cursor: null, matches: [] }
        : options.searchPage(String(body?.surface ?? "draft"), String(body?.query ?? "")));
      return;
    }
    await json(route, { error: { code: "not_found", message: "test route not found" } }, 404);
  });
  await page.goto(`${baseUrl}index.html`);
  await page.locator("#draft-document").waitFor({ state: "visible" });
  return state;
}

function serverTiming(): Record<string, number> {
  return {
    selection_caret_revalidation_ms: 1,
    immutable_child_write_fsync_ms: 2,
    project_brief_transcript_context_validation_ms: 3,
    workflow_snapshot_refresh_ms: 4,
    draft_snapshot_rebuild_ms: 5,
    server_before_response_ms: 6,
  };
}

async function json(route: Route, value: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

function reviewSnapshot(): DraftEditorSnapshot {
  const a = paragraph("paragraph_a", "alpha bravo", "block_a");
  const b = paragraph("paragraph_b", "charlie delta", "block_b");
  return {
    editor_schema_version: 1,
    review_mode: "draft_editor",
    project: { name: "Chromium direct drag" },
    brief: { theme: "test", target_duration_ticks: 120, focus: [], allow_reorder: true },
    candidate: { candidate_id: "candidate_a", parent_candidate_id: null, display_title: null, confirmed_by_user: false, has_unrecorded_narration: false },
    paragraphs: [a, b],
    blocks: [
      { block_id: "heading_a", kind: "section_title", title: "同名章节" },
      { block_id: "block_a", kind: "source_excerpt", refs: a.exact_refs, canonical_text: a.text },
      { block_id: "heading_b", kind: "section_title", title: "同名章节" },
      { block_id: "block_b", kind: "source_excerpt", refs: b.exact_refs, canonical_text: b.text },
    ],
    sources: [{
      source_id: "source_a",
      transcript_version_id: "transcript_a",
      display_name: "fixture",
      kind: "video",
      duration_ticks: 120,
      tags: [],
      note: "",
      media_url: "/fixture.mp4",
      playback_kind: "original",
      proxy_profile: null,
    }],
    history: { can_undo: false, can_redo: false, redo_scope: "draft_workspace" },
    workspace: {
      expected_checkpoint_ref: { generation: 1, checkpoint_hash: "a".repeat(64) },
      expected_current_candidate_ref: { artifact_id: "candidate_a", schema_version: 2, content_hash: "b".repeat(64) },
    },
    transcript_browser: {
      read_endpoint: "/api/workflow/draft-transcript-window",
      window_endpoint: "/api/workflow/draft-transcript-window",
      search_endpoint: "/api/workflow/draft-search",
      selection_endpoint: "/api/workflow/draft-selection-resolve",
      pagination: { offset_unit: "paragraph", max_limit: 50 },
    },
    candidate_handoff: { endpoint: "/api/workflow/draft-candidate-select", requires_exact_parent: true },
  };
}

function headingHierarchySnapshot(): DraftEditorSnapshot {
  const source = {
    ...paragraph("paragraph_heading_source", "普通章节中的正文", "block_heading_source"),
    section_title: "正文章节",
  };
  const narration: DraftEditorSnapshot["paragraphs"][number] = {
    paragraph_id: "paragraph_heading_narration",
    kind: "narration",
    person: { person_id: null, name: "解说", role: "解说", local_speaker_id: null },
    text: "这里是章节边界上的解说。",
    section_title: "解说章节",
    narration_status: "draft",
    block_id: "narration_heading",
    source_runs: [],
    exact_refs: [],
  };
  const empty: DraftEditorSnapshot["paragraphs"][number] = {
    paragraph_id: "paragraph_heading_empty",
    kind: "section_title",
    person: { person_id: null, name: "空章节", role: null, local_speaker_id: null },
    text: "",
    section_title: "空章节",
    narration_status: null,
    block_id: "heading_empty",
    source_runs: [],
    exact_refs: [],
  };
  return {
    ...reviewSnapshot(),
    project: { name: "Chromium heading hierarchy" },
    paragraphs: [source, narration, empty],
    blocks: [
      { block_id: "heading_source", kind: "section_title", title: "正文章节" },
      { block_id: "block_heading_source", kind: "source_excerpt", refs: source.exact_refs, canonical_text: source.text },
      { block_id: "heading_narration", kind: "section_title", title: "解说章节" },
      { block_id: "narration_heading", kind: "narration", text: narration.text, status: "draft", recorded_refs: [] },
      { block_id: "heading_empty", kind: "section_title", title: "空章节" },
    ],
  };
}

function nextSnapshot(
  snapshot: DraftEditorSnapshot,
  body: Record<string, unknown> | null = null,
): DraftEditorSnapshot {
  let paragraphs = snapshot.paragraphs;
  let blocks = snapshot.blocks ?? [];
  if (body?.operation === "punctuation_edit") {
    const payload = body.payload as Record<string, unknown> | undefined;
    const paragraphId = typeof payload?.paragraph_id === "string"
      ? payload.paragraph_id
      : null;
    const blockId = typeof payload?.block_id === "string" ? payload.block_id : null;
    const start = typeof payload?.start_utf16_offset === "number"
      ? payload.start_utf16_offset
      : null;
    const end = typeof payload?.end_utf16_offset === "number"
      ? payload.end_utf16_offset
      : null;
    const replacement = typeof payload?.replacement === "string"
      ? payload.replacement
      : null;
    const paragraph = paragraphId === null
      ? undefined
      : paragraphs.find((item) => item.paragraph_id === paragraphId);
    if (
      paragraph !== undefined
      && blockId !== null
      && start !== null
      && end !== null
      && replacement !== null
    ) {
      const nextText = replaceUtf16Range(paragraph.text, start, end, replacement);
      paragraphs = paragraphs.map((item) => item.paragraph_id === paragraphId
        ? withParagraphText(item, nextText)
        : item);
      blocks = blocks.map((block) => block.kind !== "source_excerpt" || block.block_id !== blockId
        ? block
        : {
            ...block,
            display_text: nextText === block.canonical_text ? undefined : nextText,
          });
    }
  }
  if (body?.operation === "move_selection") {
    const source = body.source as Record<string, unknown> | undefined;
    const displayRange = source?.display_range as Record<string, unknown> | undefined;
    const anchor = displayRange?.anchor as Record<string, unknown> | undefined;
    const target = body.target as Record<string, unknown> | undefined;
    const sourceParagraphId = typeof anchor?.paragraph_id === "string"
      ? anchor.paragraph_id
      : null;
    const targetParagraphId = typeof target?.paragraph_id === "string"
      ? target.paragraph_id
      : null;
    const targetBlockId = typeof target?.block_id === "string"
      ? target.block_id
      : null;
    const targetOffset = typeof target?.utf16_offset === "number"
      ? target.utf16_offset
      : 0;
    if (source?.kind === "narration_block") {
      // Narration is an atomic block: moving it reorders the paragraph list
      // so the narration row lands after the target paragraph.
      const narrationId = typeof source.block_id === "string"
        ? paragraphs.find((item) => item.kind === "narration" && item.block_id === source.block_id)?.paragraph_id
        : null;
      if (narrationId !== null && targetParagraphId !== null && targetParagraphId !== narrationId) {
        const rest = paragraphs.filter((item) => item.paragraph_id !== narrationId);
        const targetIndex = rest.findIndex((item) => item.paragraph_id === targetParagraphId);
        if (targetIndex >= 0) {
          const narration = paragraphs.find((item) => item.paragraph_id === narrationId)!;
          if (targetBlockId !== null && targetOffset === 0 && targetBlockId.startsWith("heading_")) {
            // Dropped onto the heading's own paragraph: narration follows
            // the heading (空章节 heading → narration).
            rest.splice(targetIndex + 1, 0, narration);
          } else {
            rest.splice(targetIndex + 1, 0, narration);
          }
          paragraphs = rest;
        }
      }
    } else {
      const sourceParagraph = paragraphs.find((item) => item.paragraph_id === sourceParagraphId);
      const targetParagraph = paragraphs.find((item) => item.paragraph_id === targetParagraphId);
      if (
        sourceParagraph !== undefined
        && targetParagraph !== undefined
        && sourceParagraphId !== null
        && targetParagraphId !== null
        && sourceParagraphId !== targetParagraphId
      ) {
        const movedText = Array.from(sourceParagraph.text).slice(0, 5).join("");
        const sourceText = Array.from(sourceParagraph.text).slice(5).join("");
        const targetText = `${targetParagraph.text}【mock移入:${movedText}】`;
        paragraphs = paragraphs.map((item) =>
          item.paragraph_id === sourceParagraphId
            ? withParagraphText(item, sourceText)
            : item.paragraph_id === targetParagraphId
              ? withParagraphText(item, targetText)
              : item,
        );
        const sourceBlockId = sourceParagraph.source_runs[0]?.block_id;
        const targetBlockId2 = targetParagraph.source_runs[0]?.block_id;
        blocks = blocks.map((block) => {
          if (block.kind !== "source_excerpt") return block;
          if (block.block_id === sourceBlockId) return { ...block, canonical_text: sourceText };
          if (block.block_id === targetBlockId2) return { ...block, canonical_text: targetText };
          return block;
        });
      }
    }
  }
  return {
    ...snapshot,
    candidate: { ...snapshot.candidate, candidate_id: "candidate_b", parent_candidate_id: "candidate_a" },
    paragraphs,
    blocks,
    workspace: {
      expected_checkpoint_ref: {
        generation: snapshot.workspace.expected_checkpoint_ref.generation + 1,
        checkpoint_hash: "c".repeat(64),
      },
      expected_current_candidate_ref: { artifact_id: "candidate_b", schema_version: 2, content_hash: "d".repeat(64) },
    },
  };
}

function sectionHistorySnapshot(
  snapshot: DraftEditorSnapshot,
  candidateId: string,
  generation: number,
  headings: Array<["heading_a" | "heading_b", string]>,
  canUndo: boolean,
  canRedo: boolean,
): DraftEditorSnapshot {
  const paragraphByHeading = {
    heading_a: snapshot.paragraphs.find((item) => item.paragraph_id === "paragraph_a")!,
    heading_b: snapshot.paragraphs.find((item) => item.paragraph_id === "paragraph_b")!,
  };
  const blockByHeading = {
    heading_a: "block_a",
    heading_b: "block_b",
  } as const;
  const paragraphs = headings.map(([headingId, title]) => ({
    ...paragraphByHeading[headingId],
    section_title: title,
  }));
  return {
    ...snapshot,
    candidate: {
      ...snapshot.candidate,
      candidate_id: candidateId,
      parent_candidate_id: generation === 1 ? null : snapshot.candidate.candidate_id,
    },
    paragraphs,
    blocks: headings.flatMap(([headingId, title]) => {
      const paragraph = paragraphByHeading[headingId];
      return [
        { block_id: headingId, kind: "section_title" as const, title },
        {
          block_id: blockByHeading[headingId],
          kind: "source_excerpt" as const,
          refs: paragraph.exact_refs,
          canonical_text: paragraph.text,
        },
      ];
    }),
    history: {
      can_undo: canUndo,
      can_redo: canRedo,
      redo_scope: "draft_workspace",
    },
    workspace: {
      expected_checkpoint_ref: {
        generation,
        checkpoint_hash: String.fromCharCode(96 + generation).repeat(64),
      },
      expected_current_candidate_ref: {
        artifact_id: candidateId,
        schema_version: 2,
        content_hash: String.fromCharCode(100 + generation).repeat(64),
      },
    },
  };
}

function withParagraphText(
  paragraph: DraftEditorSnapshot["paragraphs"][number],
  text: string,
): DraftEditorSnapshot["paragraphs"][number] {
  const characterLength = Array.from(text).length;
  return {
    ...paragraph,
    text,
    source_runs: paragraph.source_runs.map((run) => ({
      ...run,
      text,
      end_offset: characterLength,
      source_end_offset: characterLength,
    })),
  };
}

function replaceUtf16Range(text: string, start: number, end: number, replacement: string): string {
  return text.slice(0, start) + replacement + text.slice(end);
}

function selectionResponse(
  snapshot: DraftEditorSnapshot,
  paragraphId = snapshot.paragraphs[0]?.paragraph_id,
  surface: "draft" | "source" = "draft",
): DraftEditorSelectionResponse {
  if (paragraphId === undefined) throw new Error("selection snapshot has no paragraphs");
  const paragraph = snapshot.paragraphs.find((item) => item.paragraph_id === paragraphId);
  if (paragraph === undefined) throw new Error(`missing selection paragraph ${paragraphId}`);
  const focus = Math.min(5, Array.from(paragraph.text).length);
  return {
    candidate_id: snapshot.candidate.candidate_id,
    surface,
    resolution: {
      direction: "forward",
      canonical_text: Array.from(paragraph.text).slice(0, focus).join(""),
      refs: [{ ...paragraph.exact_refs[0]!, canonical_text: Array.from(paragraph.text).slice(0, focus).join("") }],
      start_caret: null,
      end_caret: null,
      adjusted: false,
      degraded: false,
      degradation_reasons: [],
    },
    display_range: {
      anchor: { paragraph_id: paragraphId, character_offset: 0, utf16_offset: 0 },
      focus: { paragraph_id: paragraphId, character_offset: focus, utf16_offset: Array.from(paragraph.text).slice(0, focus).join("").length },
    },
    correspondence_groups: [],
    resolution_hash: "e".repeat(64),
  };
}

function sourceSelectionResponse(
  snapshot: DraftEditorSnapshot,
  start = 0,
  end = Math.min(5, Array.from("alpha bravo").length),
): DraftEditorSelectionResponse {
  const windowParagraph = (transcriptWindow() as {
    paragraphs: Array<{ paragraph_id: string; text: string }>;
  }).paragraphs[0]!;
  const focus = Math.min(end, Array.from(windowParagraph.text).length);
  const startCp = Math.min(start, focus);
  const canonical = Array.from(windowParagraph.text).slice(startCp, focus).join("");
  const ref = { source_id: "source_a", transcript_version_id: "transcript_a", segment_id: "source_a_segment", start_ticks: 0, end_ticks: 120 };
  return {
    candidate_id: snapshot.candidate.candidate_id,
    surface: "source",
    resolution: {
      direction: "forward",
      canonical_text: canonical,
      refs: [{ ...ref, canonical_text: canonical }],
      start_caret: null,
      end_caret: null,
      adjusted: false,
      degraded: false,
      degradation_reasons: [],
    },
    display_range: {
      anchor: { paragraph_id: "source_paragraph_a", character_offset: startCp, utf16_offset: startCp },
      focus: { paragraph_id: "source_paragraph_a", character_offset: focus, utf16_offset: focus },
    },
    correspondence_groups: [],
    resolution_hash: "d".repeat(64),
  };
}

function resultSelectionResponse(
  snapshot: DraftEditorSnapshot,
  paragraphId: string,
  start: number,
  end: number,
  text: string,
): {
  surface: "draft";
  request: DraftEditorSelectionRequest;
  response: DraftEditorSelectionResponse;
  accepted_degraded: boolean;
} {
  const paragraph = snapshot.paragraphs.find((item) => item.paragraph_id === paragraphId);
  if (paragraph === undefined) throw new Error(`missing result selection paragraph ${paragraphId}`);
  const response: DraftEditorSelectionResponse = {
    candidate_id: snapshot.candidate.candidate_id,
    surface: "draft",
    resolution: {
      direction: "forward",
      canonical_text: text,
      refs: [{ ...paragraph.exact_refs[0]!, canonical_text: text }],
      start_caret: null,
      end_caret: null,
      adjusted: false,
      degraded: false,
      degradation_reasons: [],
    },
    display_range: {
      anchor: { paragraph_id: paragraphId, character_offset: start, utf16_offset: start },
      focus: { paragraph_id: paragraphId, character_offset: end, utf16_offset: end },
    },
    correspondence_groups: [],
    resolution_hash: "f".repeat(64),
  };
  return {
    surface: "draft",
    request: {
      anchor: { paragraph_id: paragraphId, offset: start, offset_encoding: "utf16" },
      focus: { paragraph_id: paragraphId, offset: end, offset_encoding: "utf16" },
    },
    response,
    accepted_degraded: true,
  };
}

function resolvedDisplaySnapshot(): DraftEditorSnapshot {
  const source = paragraph("resolved_paragraph", "主持人说：“请大家", "resolved_block");
  const base = reviewSnapshot();
  const trailingBlock = base.blocks?.[3];
  if (trailingBlock === undefined) throw new Error("resolved selection fixture is incomplete");
  return {
    ...base,
    project: { name: "Chromium resolved selection" },
    paragraphs: [source, base.paragraphs[1]!],
    blocks: [
      { block_id: "resolved_block", kind: "source_excerpt", refs: source.exact_refs, canonical_text: source.text },
      trailingBlock,
    ],
  };
}

function resolvedSelectionResponse(snapshot: DraftEditorSnapshot): DraftEditorSelectionResponse {
  const paragraph = snapshot.paragraphs[0]!;
  const response = selectionResponse(snapshot, paragraph.paragraph_id);
  return {
    ...response,
    resolution: {
      ...response.resolution,
      canonical_text: paragraph.text,
      refs: [{ ...paragraph.exact_refs[0]!, canonical_text: paragraph.text }],
      adjusted: true,
    },
    display_range: {
      anchor: { paragraph_id: paragraph.paragraph_id, character_offset: 3, utf16_offset: 3 },
      focus: { paragraph_id: paragraph.paragraph_id, character_offset: 5, utf16_offset: 5 },
    },
    resolved_display_range: {
      anchor: { paragraph_id: paragraph.paragraph_id, character_offset: 0, utf16_offset: 0 },
      focus: { paragraph_id: paragraph.paragraph_id, character_offset: 9, utf16_offset: 9 },
    },
  };
}

function paragraph(paragraphId: string, value: string, blockId: string): DraftEditorSnapshot["paragraphs"][number] {
  const characterLength = Array.from(value).length;
  const ref = { source_id: "source_a", transcript_version_id: "transcript_a", segment_id: `${blockId}_segment`, start_ticks: 0, end_ticks: 120 };
  return {
    paragraph_id: paragraphId,
    kind: "source_excerpt",
    person: { person_id: null, name: null, role: null, local_speaker_id: null },
    text: value,
    section_title: null,
    narration_status: null,
    source_runs: [{ block_id: blockId, source_id: "source_a", source_display_name: "fixture", paragraph_id: paragraphId, start_ticks: 0, end_ticks: 120, start_offset: 0, end_offset: characterLength, source_start_offset: 0, source_end_offset: characterLength, text: value, refs: [ref] }],
    exact_refs: [ref],
  };
}

function longReviewSnapshot(): DraftEditorSnapshot {
  const paragraphs = Array.from({ length: 10 }, (_, index) =>
    paragraph(
      `long_paragraph_${index}`,
        `第${index + 1}段：这是一个包含中文标点、空格、数字 2026 和 emoji 😀 的长正文，用于真实浏览器拖动性能回归；`
        .repeat(2),
      `long_block_${index}`,
    ),
  );
  const blocks: DraftEditorSnapshot["blocks"] = [];
  const sections = [
    { id: "long_heading_a", title: "长稿第一章", start: 0 },
    { id: "long_heading_b", title: "长稿第二章", start: 3 },
    { id: "long_heading_c", title: "长稿第三章", start: 6 },
    { id: "long_heading_empty", title: "长稿空章节", start: 10 },
  ];
  for (const section of sections.slice(0, 3)) {
    const paragraph = paragraphs[section.start];
    if (paragraph !== undefined) {
      paragraphs[section.start] = { ...paragraph, section_title: section.title };
    }
  }
  for (const section of sections) {
    blocks.push({ block_id: section.id, kind: "section_title", title: section.title });
    for (const item of paragraphs.slice(section.start, sections.find((next) => next.start > section.start)?.start ?? paragraphs.length)) {
      blocks.push({ block_id: item.block_id!, kind: "source_excerpt", refs: item.exact_refs, canonical_text: item.text });
    }
  }
  const snapshot = reviewSnapshot();
  return {
    ...snapshot,
    project: { name: "Chromium long Chinese drag" },
    paragraphs,
    blocks,
    workspace: {
      expected_checkpoint_ref: { generation: 1, checkpoint_hash: "a".repeat(64) },
      expected_current_candidate_ref: { artifact_id: "candidate_a", schema_version: 2, content_hash: "b".repeat(64) },
    },
  };
}

function punctuationLongSnapshot(suffix: string): DraftEditorSnapshot {
  const paragraphs = Array.from({ length: 300 }, (_, index) => {
    const value = `长稿${index}内容`;
    const display = `${value}${suffix}`;
    const item = paragraph(`punctuation_long_${index}`, value, `punctuation_block_${index}`);
    return {
      ...item,
      text: display,
      source_runs: item.source_runs.map((run) => ({
        ...run,
        end_offset: Array.from(display).length,
        text: display,
      })),
    };
  });
  const snapshot = reviewSnapshot();
  return {
    ...snapshot,
    project: { name: "Chromium punctuation long draft" },
    paragraphs,
    blocks: paragraphs.map((item) => ({
      block_id: item.block_id!,
      kind: "source_excerpt" as const,
      refs: item.exact_refs,
      canonical_text: item.text.slice(0, -suffix.length || undefined),
      ...(suffix.length === 0 ? {} : { display_text: item.text }),
    })),
    history: { can_undo: suffix.length > 0, can_redo: suffix.length === 0, redo_scope: "draft_workspace" },
  };
}

function punctuationEmojiSnapshot(): DraftEditorSnapshot {
  const item = paragraph("paragraph_a", "你好😀世界", "block_a");
  const snapshot = reviewSnapshot();
  return {
    ...snapshot,
    project: { name: "Chromium punctuation emoji" },
    paragraphs: [item],
    blocks: [{
      block_id: "block_a",
      kind: "source_excerpt",
      refs: item.exact_refs,
      canonical_text: item.text,
    }],
    history: { can_undo: true, can_redo: false, redo_scope: "draft_workspace" },
  };
}

function punctuationEmojiNavigationSnapshot(): DraftEditorSnapshot {
  const first = punctuationEmojiSnapshot();
  const second = paragraph("paragraph_b", "charlie delta", "block_b");
  return {
    ...first,
    paragraphs: [first.paragraphs[0]!, second],
    blocks: [
      first.blocks?.[0]!,
      { block_id: "block_b", kind: "source_excerpt", refs: second.exact_refs, canonical_text: second.text },
    ],
  };
}

function transcriptWindow(): unknown {
  return {
    candidate_id: "candidate_a",
    source_id: "source_a",
    offset: 0,
    limit: 50,
    total: 1,
    next_cursor: null,
    previous_cursor: null,
    located_paragraph_id: null,
    paragraphs: [{
      paragraph_id: "source_paragraph_a",
      display_number: "1",
      source_id: "source_a",
      transcript_version_id: "transcript_a",
      source_display_name: "fixture",
      local_speaker_id: null,
      local_speaker_ids: [],
      person_id: null,
      person_name: null,
      text: "alpha bravo",
      start_ticks: 0,
      end_ticks: 120,
      refs: [],
      adoption_status: "unadopted",
    }],
  };
}

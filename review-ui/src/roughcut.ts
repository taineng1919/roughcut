import {
  coverageSummary,
  loadProposalCoverage,
  proposalDiffSummary,
  type ProposalDiffPayload,
} from "./proposal-boundary";
import {
  type PlaybackPosition,
  positionForClip,
  positionForOutput,
  ticksToSeconds,
  transitionAtBoundary,
} from "./player";
import { formatReviewTime, manuscriptRuns } from "./roughcut-model";
import type {
  ReviewPayload,
  ReviewSource,
  RoughcutStateResponse,
} from "./types";
import { userFacingErrorMessage } from "./user-message";
import {
  createVirtualTimelineState,
  PAUSE_REQUESTED,
  PLAY_REQUESTED,
  reduceVirtualTimeline,
  SEEK_REQUESTED,
  SOURCE_CANPLAY,
  SOURCE_WAITING,
  type VirtualTimelineEvent,
  type VirtualTimelineState,
} from "./virtual-timeline";
import type { DraftEditorSnapshot, WorkflowApi } from "./workflow-types";

const ROUGHCUT_PLAYER_ID = "roughcut-player";
const ROUGHCUT_ADOPT_CONFIRMATION_ID = "roughcut-adopt-confirmation";
const ROUGHCUT_TRANSPORT_PLAY_ID = "roughcut-transport-play";
const ROUGHCUT_TRANSPORT_SEEK_ID = "roughcut-transport-seek";
const ROUGHCUT_ADOPT_CONFIRM_ID = "roughcut-adopt-confirm";
const ROUGHCUT_RETURN_DRAFT_ID = "roughcut-return-draft";
const ROUGHCUT_MEDIA_ERROR_ID = "roughcut-media-error";
const ROUGHCUT_ADOPT_OPEN_ID = "roughcut-adopt-open";
const LOADED_METADATA_EVENT = "loadedmetadata";

type Pane = "preview" | "manuscript";

export const ADOPTED_EXPORT_GUIDANCE =
  "当前粗剪版本已采用。需要正式导出时，请回到同一 Agent 对话并提出：正式导出当前已采用的粗剪版本。";

interface ReturnToDraftResponse {
  draft_editor: DraftEditorSnapshot;
}

export class RoughcutReviewController {
  private review: ReviewPayload;
  private locked = false;
  private busy = false;
  private evidenceReady = false;
  private evidenceGeneration = 0;
  private activeClipIndex = 0;
  private activeSourceId: string | null = null;
  private preloadedSourceId: string | null = null;
  private preloadPlayer: HTMLVideoElement | null = null;
  private transitionPending = false;
  private seekGeneration = 0;
  private ignoredSourceTicks: number | null = null;
  private activePane: Pane = "preview";
  private transport: VirtualTimelineState;

  constructor(
    initial: ReviewPayload,
    private readonly api: WorkflowApi,
    private readonly setStatus: (text: string, warning?: boolean) => void,
    private readonly onReturnToDraft: (snapshot: DraftEditorSnapshot) => void,
  ) {
    this.review = initial;
    this.transport = createVirtualTimelineState(
      initial.timeline.total_duration_ticks,
    );
  }

  async mount(): Promise<void> {
    this.bindStaticControls();
    const state = await this.api<RoughcutStateResponse>("/api/roughcut-state");
    this.review = state.review;
    this.transport = createVirtualTimelineState(
      this.review.timeline.total_duration_ticks,
    );
    this.render();
    this.setStatus(
      this.review.basis.type === "decision"
        ? ADOPTED_EXPORT_GUIDANCE
        : "请播放并核对当前粗剪；采用前不会改变当前确认版本。",
    );
    await this.refreshEvidence();
    const first = this.review.timeline.spans[0];
    if (first !== undefined) {
      await this.seek(positionForClip(this.review.timeline.spans, first.clip_id));
    }
  }

  private bindStaticControls(): void {
    element<HTMLButtonElement>(ROUGHCUT_TRANSPORT_PLAY_ID).addEventListener(
      "click",
      () => void this.togglePlayback(),
    );
    element<HTMLInputElement>(ROUGHCUT_TRANSPORT_SEEK_ID).addEventListener(
      "input",
      (event) => {
        const outputTicks = Number((event.currentTarget as HTMLInputElement).value);
        void this.seekOutput(outputTicks);
      },
    );
    element<HTMLButtonElement>(ROUGHCUT_ADOPT_OPEN_ID).addEventListener(
      "click",
      () => this.openAdoption(),
    );
    element<HTMLButtonElement>("roughcut-adopt-cancel").addEventListener(
      "click",
      () => {
        element<HTMLElement>(ROUGHCUT_ADOPT_CONFIRMATION_ID).hidden = true;
      },
    );
    element<HTMLButtonElement>(ROUGHCUT_ADOPT_CONFIRM_ID).addEventListener(
      "click",
      () => void this.adopt(),
    );
    element<HTMLButtonElement>(ROUGHCUT_RETURN_DRAFT_ID).addEventListener(
      "click",
      () => void this.returnToDraft(),
    );
    document.querySelectorAll<HTMLButtonElement>("[data-roughcut-pane]").forEach((button) => {
      button.addEventListener("click", () => {
        const pane = button.dataset.roughcutPane;
        if (pane === "preview" || pane === "manuscript") {
          this.activePane = pane;
          this.renderPane();
        }
      });
    });

    const player = element<HTMLVideoElement>(ROUGHCUT_PLAYER_ID);
    player.controls = false;
    player.addEventListener("play", () => {
      this.dispatchTransport({ type: SOURCE_CANPLAY });
    });
    player.addEventListener("pause", () => {
      if (this.ignoredSourceTicks === null && !this.transitionPending) {
        this.dispatchTransport({ type: PAUSE_REQUESTED });
      }
    });
    player.addEventListener("seeking", () => {
      this.dispatchTransport({
        type: SEEK_REQUESTED,
        outputTicks: this.transport.outputTicks,
      });
    });
    player.addEventListener("timeupdate", () => this.onTimeUpdate());
    player.addEventListener("waiting", () => {
      this.dispatchTransport({ type: SOURCE_WAITING });
    });
    player.addEventListener("canplay", () => {
      this.dispatchTransport({ type: SOURCE_CANPLAY });
    });
    player.addEventListener("seeked", () => {
      this.ignoredSourceTicks = null;
      this.dispatchTransport({ type: SOURCE_CANPLAY });
    });
    player.addEventListener("ended", () => {
      const span = this.review.timeline.spans[this.activeClipIndex];
      const sourceTicks = Math.floor(player.currentTime * 120_000);
      if (span !== undefined && sourceTicks + 1_000 < span.source_out_ticks) {
        this.showMediaFailure();
      } else {
        void this.advanceAtBoundary();
      }
    });
    player.addEventListener("error", () => this.showMediaFailure());
  }

  private render(): void {
    element<HTMLElement>("project-name").textContent = this.review.project.name;
    document.querySelector<HTMLElement>(".topbar .eyebrow")!.textContent =
      "粗剪预览与采用";
    element<HTMLElement>("revision").textContent =
      `${this.review.proposal.clips.length} 个片段 · ${formatReviewTime(this.review.timeline.total_duration_ticks)}`;
    this.renderManuscript();
    this.renderControls();
    this.renderTransport();
    this.renderPane();
    this.markActiveClip();
  }

  private renderManuscript(): void {
    const container = element<HTMLElement>("roughcut-manuscript");
    container.replaceChildren(
      ...manuscriptRuns(this.review.proposal.clips, this.review.transcript).map((run) => {
        const section = document.createElement("section");
        section.className = "roughcut-speaker-run";
        const heading = document.createElement("h3");
        heading.textContent = run.speakerLabel;
        const paragraph = document.createElement("p");
        for (const item of run.clips) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "roughcut-manuscript-clip";
          button.dataset.clipId = item.clip_id;
          button.textContent = item.display_text;
          button.addEventListener("click", () => void this.seekClip(item.clip_id));
          paragraph.append(button, document.createTextNode(" "));
        }
        section.append(heading, paragraph);
        return section;
      }),
    );
  }

  private renderControls(): void {
    const adopted = this.review.basis.type === "decision";
    const adopt = element<HTMLButtonElement>(ROUGHCUT_ADOPT_OPEN_ID);
    adopt.textContent = adopted ? "当前粗剪版本已采用" : "采用这个粗剪版本";
    adopt.disabled = adopted || this.locked || this.busy || !this.evidenceReady;
    element<HTMLButtonElement>(ROUGHCUT_ADOPT_CONFIRM_ID).disabled =
      this.locked || this.busy || !this.evidenceReady;
    element<HTMLButtonElement>(ROUGHCUT_RETURN_DRAFT_ID).disabled =
      this.locked || this.busy;
    element<HTMLElement>("roughcut-adopt-copy").textContent = adopted
      ? `${ADOPTED_EXPORT_GUIDANCE} 返回初稿后的任何修改都必须重新生成并采用新预览。`
      : "采用只确认当前粗剪版本，不会正式渲染或导出。";
  }

  private renderTransport(): void {
    const seek = element<HTMLInputElement>(ROUGHCUT_TRANSPORT_SEEK_ID);
    seek.max = String(this.transport.totalTicks);
    seek.value = String(this.transport.outputTicks);
    element<HTMLElement>("roughcut-current-time").textContent =
      formatReviewTime(this.transport.outputTicks);
    element<HTMLElement>("roughcut-total-time").textContent =
      formatReviewTime(this.transport.totalTicks);
    const play = element<HTMLButtonElement>(ROUGHCUT_TRANSPORT_PLAY_ID);
    play.textContent = this.transport.playIntent ? "暂停" : "播放";
    play.setAttribute(
      "aria-label",
      this.transport.playIntent ? "暂停粗剪" : "播放粗剪",
    );
    element<HTMLElement>("roughcut-transport-state").textContent =
      transportLabel(this.transport);
  }

  private renderPane(): void {
    document.querySelectorAll<HTMLElement>("[data-roughcut-panel]").forEach((panel) => {
      panel.dataset.roughcutActive = String(
        panel.dataset.roughcutPanel === this.activePane,
      );
    });
    document.querySelectorAll<HTMLButtonElement>("[data-roughcut-pane]").forEach((button) => {
      button.setAttribute(
        "aria-selected",
        String(button.dataset.roughcutPane === this.activePane),
      );
    });
  }

  private async togglePlayback(): Promise<void> {
    const player = element<HTMLVideoElement>(ROUGHCUT_PLAYER_ID);
    if (this.transport.playIntent) {
      this.dispatchTransport({ type: PAUSE_REQUESTED });
      player.pause();
      return;
    }
    if (this.transport.phase === "ended") {
      await this.seekOutput(0);
    }
    this.dispatchTransport({ type: PLAY_REQUESTED });
    try {
      await player.play();
      this.dispatchTransport({ type: SOURCE_CANPLAY });
    } catch (error) {
      this.handleError(error);
    }
  }

  private async seekOutput(outputTicks: number): Promise<void> {
    try {
      const position = positionForOutput(this.review.timeline.spans, outputTicks);
      await this.seek(position, this.transport.playIntent);
    } catch (error) {
      this.handleError(error);
    }
  }

  private async seekClip(clipId: string): Promise<void> {
    try {
      await this.seek(
        positionForClip(this.review.timeline.spans, clipId),
        this.transport.playIntent,
      );
    } catch (error) {
      this.handleError(error);
    }
  }

  private async seek(
    position: PlaybackPosition,
    resumePlayback = false,
  ): Promise<void> {
    const generation = ++this.seekGeneration;
    const shouldResume = resumePlayback
      && position.outputTicks < this.review.timeline.total_duration_ticks;
    this.dispatchTransport({
      type: SEEK_REQUESTED,
      outputTicks: position.outputTicks,
    });
    this.activeClipIndex = position.clipIndex;
    this.ignoredSourceTicks = position.sourceTicks;
    await this.selectSource(position.sourceId);
    if (generation !== this.seekGeneration) return;
    const player = element<HTMLVideoElement>(ROUGHCUT_PLAYER_ID);
    player.currentTime = ticksToSeconds(position.sourceTicks);
    this.markActiveClip();
    this.preloadNextSource(this.activeClipIndex);
    if (shouldResume) {
      this.dispatchTransport({ type: PLAY_REQUESTED });
      await player.play();
    } else if (position.outputTicks < this.transport.totalTicks) {
      this.dispatchTransport({ type: PAUSE_REQUESTED });
      player.pause();
    }
  }

  private selectSource(sourceId: string): Promise<void> {
    const source = this.sourceById(sourceId);
    const player = element<HTMLVideoElement>(ROUGHCUT_PLAYER_ID);
    if (
      this.activeSourceId === sourceId
      && player.getAttribute("src") === source.media_url
    ) {
      return Promise.resolve();
    }
    element<HTMLElement>(ROUGHCUT_MEDIA_ERROR_ID).hidden = true;
    this.dispatchTransport({ type: SOURCE_WAITING });
    return new Promise((resolve, reject) => {
      const loaded = (): void => {
        cleanup();
        resolve();
      };
      const failed = (): void => {
        cleanup();
        this.showMediaFailure();
        reject(new Error(`无法载入素材 ${source.display_name}`));
      };
      const cleanup = (): void => {
        player.removeEventListener(LOADED_METADATA_EVENT, loaded);
        player.removeEventListener("error", failed);
      };
      player.addEventListener(LOADED_METADATA_EVENT, loaded, { once: true });
      player.addEventListener("error", failed, { once: true });
      this.activeSourceId = sourceId;
      player.src = source.media_url;
      player.load();
    });
  }

  private onTimeUpdate(): void {
    if (this.transitionPending) return;
    const player = element<HTMLVideoElement>(ROUGHCUT_PLAYER_ID);
    const sourceTicks = Math.floor(player.currentTime * 120_000);
    if (
      this.ignoredSourceTicks !== null
      && Math.abs(sourceTicks - this.ignoredSourceTicks) > 1_000
    ) {
      return;
    }
    this.ignoredSourceTicks = null;
    const transition = transitionAtBoundary(
      this.review.timeline.spans,
      this.activeClipIndex,
      sourceTicks,
    );
    if (transition.action === "advance") {
      void this.advanceAtBoundary();
      return;
    }
    if (transition.action === "ended") {
      this.finishTimeline();
      return;
    }
    if (transition.position !== null) {
      this.dispatchTransport({
        type: "output-time",
        outputTicks: transition.position.outputTicks,
      });
      this.markActiveClip();
    }
  }

  private async advanceAtBoundary(): Promise<void> {
    if (this.transitionPending) return;
    const span = this.review.timeline.spans[this.activeClipIndex];
    if (span === undefined) return;
    const transition = transitionAtBoundary(
      this.review.timeline.spans,
      this.activeClipIndex,
      span.source_out_ticks,
    );
    if (transition.action === "ended" || transition.position === null) {
      this.finishTimeline();
      return;
    }
    this.transitionPending = true;
    try {
      await this.seek(transition.position, true);
    } catch (error) {
      this.handleError(error);
    } finally {
      this.transitionPending = false;
    }
  }

  private finishTimeline(): void {
    const player = element<HTMLVideoElement>(ROUGHCUT_PLAYER_ID);
    player.pause();
    this.dispatchTransport({ type: "timeline-ended" });
    this.setStatus("当前粗剪已播放完毕。");
  }

  private markActiveClip(): void {
    const span = this.review.timeline.spans[this.activeClipIndex];
    document.querySelectorAll<HTMLElement>("[data-clip-id]").forEach((node) => {
      node.classList.toggle("active", node.dataset.clipId === span?.clip_id);
    });
    if (span === undefined) return;
    const clip = this.review.proposal.clips.find(
      (item) => item.clip_id === span.clip_id,
    );
    if (clip === undefined) return;
    element<HTMLElement>("roughcut-current-clip").textContent = clip.display_text;
    element<HTMLElement>("roughcut-current-source").textContent =
      this.sourceById(clip.source_id).display_name;
  }

  private preloadNextSource(index: number): void {
    const next = this.review.timeline.spans[index + 1];
    if (
      next === undefined
      || next.source_id === this.activeSourceId
      || next.source_id === this.preloadedSourceId
    ) return;
    const source = this.sourceById(next.source_id);
    this.preloadPlayer = document.createElement("video");
    this.preloadPlayer.preload = "metadata";
    this.preloadPlayer.src = source.media_url;
    this.preloadedSourceId = source.source_id;
  }

  private async refreshEvidence(): Promise<boolean> {
    const generation = ++this.evidenceGeneration;
    this.evidenceReady = false;
    this.renderControls();
    if (this.review.basis.type !== "proposal") return true;
    const basisId = this.review.basis.id;
    try {
      const [coverage, diffPayload] = await Promise.all([
        loadProposalCoverage(this.api, basisId),
        this.api<ProposalDiffPayload>("/api/proposal-diff"),
      ]);
      if (generation !== this.evidenceGeneration || this.review.basis.id !== basisId) {
        return false;
      }
      coverageSummary(coverage.paragraphs, true);
      proposalDiffSummary(diffPayload);
      if (diffPayload.project_revision !== this.review.project.revision) {
        throw new Error("当前粗剪在核对期间发生了变化");
      }
      this.evidenceReady = true;
      return true;
    } catch (error) {
      if (generation !== this.evidenceGeneration) return false;
      this.setStatus(
        `当前粗剪尚未通过完整核对，暂时不能采用。${userFacingErrorMessage(error)}`,
        true,
      );
      return false;
    } finally {
      if (generation === this.evidenceGeneration) this.renderControls();
    }
  }

  private openAdoption(): void {
    if (!this.evidenceReady || this.review.basis.type !== "proposal") return;
    element<HTMLElement>("roughcut-adopt-confirmation-copy").textContent =
      `将采用当前 ${this.review.proposal.clips.length} 个片段、总时长 ${formatReviewTime(this.review.timeline.total_duration_ticks)} 的粗剪版本。此操作不会正式渲染或导出。`;
    element<HTMLElement>(ROUGHCUT_ADOPT_CONFIRMATION_ID).hidden = false;
  }

  private async adopt(): Promise<void> {
    if (
      this.locked
      || this.busy
      || !this.evidenceReady
      || this.review.basis.type !== "proposal"
    ) return;
    this.busy = true;
    this.renderControls();
    try {
      const response = await this.api<RoughcutStateResponse>("/api/roughcut-adopt", {
        method: "POST",
        body: JSON.stringify({
          basis_id: this.review.basis.id,
          expected_revision: this.review.project.revision,
        }),
      });
      this.review = response.review;
      element<HTMLElement>(ROUGHCUT_ADOPT_CONFIRMATION_ID).hidden = true;
      this.evidenceReady = false;
      this.render();
      this.setStatus(
        response.adoption?.changed === false
          ? "当前粗剪版本已经采用，没有重复创建新版本。"
          : ADOPTED_EXPORT_GUIDANCE,
      );
    } catch (error) {
      this.handleError(error);
    } finally {
      this.busy = false;
      this.renderControls();
    }
  }

  private async returnToDraft(): Promise<void> {
    if (this.locked || this.busy) return;
    this.busy = true;
    this.renderControls();
    try {
      const response = await this.api<ReturnToDraftResponse>(
        "/api/roughcut-return-draft",
        {
          method: "POST",
          body: JSON.stringify({
            basis_id: this.review.basis.id,
            expected_revision: this.review.project.revision,
          }),
        },
      );
      this.onReturnToDraft(response.draft_editor);
    } catch (error) {
      this.handleError(error);
    } finally {
      this.busy = false;
      this.renderControls();
    }
  }

  private showMediaFailure(): void {
    const message =
      "当前素材无法播放。请返回 Agent 检查播放素材后重新打开本页；页面不会自动切换素材或转码。";
    const error = element<HTMLElement>(ROUGHCUT_MEDIA_ERROR_ID);
    error.hidden = false;
    error.textContent = message;
    this.dispatchTransport({ type: "media-error", message });
  }

  private dispatchTransport(event: VirtualTimelineEvent): void {
    this.transport = reduceVirtualTimeline(this.transport, event);
    this.renderTransport();
  }

  private sourceById(sourceId: string): ReviewSource {
    const source = this.review.sources.find((item) => item.source_id === sourceId);
    if (source === undefined) throw new Error("当前粗剪引用了未授权素材");
    return source;
  }

  private handleError(error: unknown): void {
    const detail = error as { code?: string };
    if (detail.code === "stale_review") {
      this.locked = true;
      element<HTMLElement>("roughcut-stale").hidden = false;
      element<HTMLElement>(ROUGHCUT_ADOPT_CONFIRMATION_ID).hidden = true;
      this.renderControls();
      this.setStatus(
        "项目内容已在别处更新，本页已转为只读，请重新打开粗剪预览。",
        true,
      );
      return;
    }
    this.setStatus(userFacingErrorMessage(error), true);
  }
}

function transportLabel(state: VirtualTimelineState): string {
  if (state.phase === "playing") return "播放中";
  if (state.phase === "waiting") return "缓冲中";
  if (state.phase === "seeking") return "定位中";
  if (state.phase === "ended") return "已结束";
  if (state.phase === "error") return "播放失败";
  return "已暂停";
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (found === null) throw new Error(`missing #${id}`);
  return found as T;
}

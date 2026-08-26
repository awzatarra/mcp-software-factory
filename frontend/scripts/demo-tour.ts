import { mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium, type BrowserContext, type Page, type Video } from "playwright";

const VIEWPORT = { width: 1440, height: 900 } as const;
const DEFAULT_BASE_URL = "http://127.0.0.1:5173";
const DEFAULT_BACKEND_URL = "http://127.0.0.1:8000";
const DEFAULT_WORKFLOW_ID = "abb17f25-467e-42a2-a903-ec09f8217feb";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const EMPTY_STATE_PATTERN = /\b(?:sin datos|sin registros|no hay|no se encontraron|empty)\b/i;

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendDirectory = path.resolve(scriptDirectory, "..");
const videoDirectory = path.join(frontendDirectory, "demo-videos");
const recordingDirectory = path.join(videoDirectory, ".playwright");
const videoPath = path.join(videoDirectory, "mcp-software-factory-demo.webm");

function positiveInteger(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer.`);
  }
  return value;
}

function normalizeBaseUrl(value: string): string {
  return value.replace(/\/+$/, "");
}

const baseUrl = normalizeBaseUrl(process.env.DEMO_BASE_URL ?? DEFAULT_BASE_URL);
const backendUrl = normalizeBaseUrl(process.env.DEMO_BACKEND_URL ?? DEFAULT_BACKEND_URL);
const workflowId = process.env.DEMO_WORKFLOW_ID ?? DEFAULT_WORKFLOW_ID;
const screenPauseMs = positiveInteger("DEMO_SCREEN_PAUSE_MS", 9_000);
const sectionPauseMs = positiveInteger("DEMO_SECTION_PAUSE_MS", 6_500);
const scrollPauseMs = positiveInteger("DEMO_SCROLL_PAUSE_MS", 1_800);

type TourView = {
  label: string;
  path: string;
  priority?: boolean;
  timeline?: boolean;
};

type TourSection = TourView & {
  additionalViews?: TourView[];
};

type WorkflowHistoryPage = {
  events: Array<{ event_id: string; sequence: number }>;
  has_more: boolean;
  last_sequence: number;
};

const workflowPath = `/workflows/${encodeURIComponent(workflowId)}`;
const sections: TourSection[] = [
  { label: "Dashboard", path: "/dashboard" },
  { label: "Workflows", path: "/workflows" },
  { label: "Workflow Summary", path: `${workflowPath}?tab=overview`, priority: true },
  { label: "Workflow Execution", path: `${workflowPath}?tab=execution`, priority: true },
  { label: "Workflow Git", path: `${workflowPath}?tab=git`, priority: true },
  { label: "Workflow CI", path: `${workflowPath}?tab=ci`, priority: true },
  { label: "Workflow Timeline", path: `${workflowPath}?tab=timeline`, priority: true, timeline: true },
  {
    label: "Evaluations",
    path: "/evaluations",
    priority: true,
    additionalViews: [{ label: "Evaluation CI Operations", path: "/evaluations/ci" }],
  },
  { label: "Recommendations", path: "/evaluations/recommendations" },
  { label: "Experiments", path: "/evaluations/experiments" },
  { label: "Knowledge", path: "/knowledge" },
  { label: "Observability", path: "/observability", priority: true },
  { label: "LLM Costs / FinOps", path: "/llm-costs", priority: true },
  { label: "Alerts", path: "/alerts" },
  { label: "Notifications", path: "/notifications/deliveries" },
];

async function validateServices(): Promise<void> {
  const request = async (url: string, label: string) => {
    let response: Response;
    try {
      response = await fetch(url, { signal: AbortSignal.timeout(10_000) });
    } catch (error) {
      throw new Error(`${label} is not reachable at ${url}. Start it before running the demo.`, { cause: error });
    }
    if (!response.ok) {
      throw new Error(`${label} returned HTTP ${response.status} at ${url}.`);
    }
    return response;
  };

  const health = await request(`${backendUrl}/health`, "Backend");
  const payload = await health.json() as { status?: string };
  if (payload.status !== "ok") {
    throw new Error(`Backend health is not ok at ${backendUrl}/health.`);
  }
  await request(baseUrl, "Frontend");
}

async function getFinalWorkflowEventId(): Promise<string | null> {
  const events: WorkflowHistoryPage["events"] = [];
  let afterSequence = 0;

  for (let pageNumber = 0; pageNumber < 10; pageNumber += 1) {
    const query = new URLSearchParams({
      after_sequence: String(afterSequence),
      limit: "1000",
      branch_id: "original",
    });
    const response = await fetch(
      `${backendUrl}/api/workflows/${encodeURIComponent(workflowId)}/events/history?${query}`,
      { signal: AbortSignal.timeout(10_000) },
    );
    if (!response.ok) {
      console.log(`DEMO: final Timeline event unavailable (HTTP ${response.status})`);
      return null;
    }

    const page = await response.json() as WorkflowHistoryPage;
    events.push(...page.events);
    if (!page.has_more) break;
    afterSequence = page.events.at(-1)?.sequence ?? page.last_sequence;
  }

  return events.at(-1)?.event_id ?? null;
}

export async function slowScroll(
  page: Page,
  options: { maxSteps?: number } = {},
): Promise<boolean> {
  const maxSteps = options.maxSteps ?? 8;
  let moved = false;

  for (let step = 0; step < maxSteps; step += 1) {
    const metrics = await page.evaluate(() => ({
      current: window.scrollY,
      maximum: Math.max(0, document.documentElement.scrollHeight - window.innerHeight),
    }));
    if (metrics.maximum - metrics.current < 40) break;

    await page.evaluate(() => window.scrollBy({ top: 360, behavior: "smooth" }));
    moved = true;
    await page.waitForTimeout(scrollPauseMs);
  }

  return moved;
}

async function waitForView(page: Page): Promise<void> {
  await page.waitForLoadState("domcontentloaded");
  await page.locator("main").first().waitFor({ state: "visible", timeout: 20_000 });
  await page.waitForTimeout(1_000);
}

async function showView(
  page: Page,
  view: TourView,
  finalWorkflowEventId: string | null,
): Promise<void> {
  await page.goto(`${baseUrl}${view.path}`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await waitForView(page);

  const content = await page.locator("main").first().innerText().catch(() => "");
  if (EMPTY_STATE_PATTERN.test(content)) {
    console.log(`DEMO: ${view.label} sin datos`);
  }

  await page.waitForTimeout(screenPauseMs);
  await slowScroll(page, { maxSteps: view.timeline ? 10 : view.priority ? 4 : 3 });

  if (view.timeline) {
    if (finalWorkflowEventId) {
      const finalEventUrl = new URL(view.path, baseUrl);
      finalEventUrl.searchParams.set("event", finalWorkflowEventId);
      await page.goto(finalEventUrl.href, { waitUntil: "domcontentloaded", timeout: 30_000 });
      await waitForView(page);
      console.log(`DEMO: Timeline final event ${finalWorkflowEventId}`);
    }
    await page.waitForTimeout(Math.max(sectionPauseMs, 11_000));
  } else if (view.priority) {
    await page.waitForTimeout(sectionPauseMs);
  }
}

async function installReadOnlyGuard(
  context: BrowserContext,
  blockedRequests: string[],
): Promise<void> {
  await context.route("**/*", async (route) => {
    const method = route.request().method().toUpperCase();
    if (MUTATING_METHODS.has(method)) {
      const description = `${method} ${route.request().url()}`;
      blockedRequests.push(description);
      console.error(`DEMO READ-ONLY GUARD BLOCKED: ${description}`);
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
}

async function saveVideo(video: Video | null, target: string): Promise<void> {
  if (!video) throw new Error("Playwright did not create a video for the tour page.");
  await rm(target, { force: true });
  await video.saveAs(target);
}

async function main(): Promise<void> {
  await validateServices();
  const finalWorkflowEventId = await getFinalWorkflowEventId();
  await mkdir(videoDirectory, { recursive: true });
  await rm(recordingDirectory, { recursive: true, force: true });
  await mkdir(recordingDirectory, { recursive: true });

  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: recordingDirectory, size: VIEWPORT },
  });
  const blockedRequests: string[] = [];
  await installReadOnlyGuard(context, blockedRequests);

  const page = await context.newPage();
  const video = page.video();
  const visited: string[] = [];
  const errors: string[] = [];

  page.on("pageerror", (error) => {
    errors.push(error.message);
    console.error(`DEMO PAGE ERROR: ${error.message}`);
  });

  try {
    for (const [index, section] of sections.entries()) {
      console.log(`DEMO ${index + 1}/${sections.length} - ${section.label}`);
      await showView(page, section, finalWorkflowEventId);
      visited.push(section.label);

      for (const additionalView of section.additionalViews ?? []) {
        console.log(`DEMO ${index + 1}/${sections.length} - ${additionalView.label}`);
        await showView(page, additionalView, finalWorkflowEventId);
        visited.push(additionalView.label);
      }
    }

    console.log("DEMO FINAL - Workflow Summary");
    await page.goto(`${baseUrl}${workflowPath}?tab=overview`, {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    await waitForView(page);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
    await page.waitForTimeout(12_000);

    if (blockedRequests.length) {
      throw new Error(`The read-only guard blocked ${blockedRequests.length} mutating request(s).`);
    }
  } finally {
    await context.close();
    try {
      await saveVideo(video, videoPath);
    } finally {
      await rm(recordingDirectory, { recursive: true, force: true });
      await browser.close();
    }
  }

  console.log("========================================");
  console.log("MCP SOFTWARE FACTORY DEMO COMPLETED");
  console.log("Video saved at:");
  console.log(videoPath);
  console.log(`Views visited: ${visited.join(", ")}`);
  console.log(`Page errors observed: ${errors.length}`);
  console.log("Read-only guard: no mutating requests observed");
  console.log("========================================");
}

main().catch((error: unknown) => {
  console.error(`DEMO FAILED: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
});

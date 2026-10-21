import { request, type FullConfig } from "@playwright/test";

/** Compile every app route used by the suite before concurrent browser flows.
 * next-dev may hard-reload an open page when a cold route changes its client
 * reference manifest; that reload can cancel an otherwise valid Back click.
 * Await real HTTP readiness, without sleeps, retries or weaker UI assertions.
 */
export default async function prepareReviewRoutes(config: FullConfig) {
  const context = await request.newContext({ baseURL: config.projects[0].use.baseURL });
  try {
    for (const path of ["/", "/team-stats/", "/clip-review/", "/film-room/", "/login/"]) {
      const response = await context.get(path);
      if (!response.ok()) throw new Error(`Browser route preparation failed: ${path} (${response.status()})`);
      await response.body();
    }
  } finally {
    await context.dispose();
  }
}

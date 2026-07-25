/**
 * The FastAPI backend, running as a Cloudflare Container.
 *
 * The container image is `backend/Dockerfile`. Cloudflare starts an instance on
 * demand and stops it after `sleepAfter` of quiet, so the API costs nothing
 * while nobody is looking at film -- which is most of the week for a college
 * program.
 *
 * A single named instance is used rather than one per session: the backend is
 * stateless (Postgres holds everything) but its in-process SSE subscriber map
 * is not, so keeping one instance means alert streams behave. See the note in
 * `proxy.ts`.
 */

import { Container } from "@cloudflare/containers";

export class BackendContainer extends Container {
  /** uvicorn's port inside the image. */
  defaultPort = 8000;

  /**
   * Long enough that a coach working through a film session never pays a cold
   * start mid-review, short enough that an idle night is not billed.
   */
  sleepAfter = "20m";

  override onStart(): void {
    console.log(JSON.stringify({ event: "container_start" }));
  }

  override onStop(): void {
    console.log(JSON.stringify({ event: "container_stop" }));
  }

  override onError(error: unknown): never {
    console.error(
      JSON.stringify({
        event: "container_error",
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    throw error;
  }
}

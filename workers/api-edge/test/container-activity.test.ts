import { describe, expect, it, vi } from "vitest";
import { BackendContainer } from "../src/container";

function runtime(response: Response) {
  return {
    env: { SCHEDULER_TOKEN: "private-test-token" },
    containerFetch: vi.fn().mockResolvedValue(response),
    renewActivityTimeout: vi.fn(),
    stop: vi.fn().mockResolvedValue(undefined),
  };
}

describe("container processing lifetime", () => {
  it("keeps a live leased CPU job running after browsers disconnect", async () => {
    const state = runtime(Response.json({ active: true }));
    await BackendContainer.prototype.onActivityExpired.call(state as never);
    expect(state.renewActivityTimeout).toHaveBeenCalledOnce();
    expect(state.stop).not.toHaveBeenCalled();
    expect(state.containerFetch.mock.calls[0][1].headers.Authorization)
      .toBe("Bearer private-test-token");
  });

  it("lets an idle deployment sleep despite loopback worker polling", async () => {
    const state = runtime(Response.json({ active: false }));
    await BackendContainer.prototype.onActivityExpired.call(state as never);
    expect(state.stop).toHaveBeenCalledOnce();
  });

  it("does not keep a failed activity check billed indefinitely", async () => {
    const state = runtime(new Response(null, { status: 503 }));
    await BackendContainer.prototype.onActivityExpired.call(state as never);
    expect(state.stop).toHaveBeenCalledOnce();
  });
});

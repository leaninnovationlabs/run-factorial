/**
 * Every 401 — from a read or a write, on any page — reaches one handler, so no panel has to know
 * what a lapsed session looks like. And no write fails unseen: one whose page has gone is said in
 * a flag.
 */

import { MutationObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import { showFlag } from "./components/ui/Flag";
import { createQueryClient } from "./queryClient";

vi.mock("./components/ui/Flag", () => ({ showFlag: vi.fn() }));

afterEach(() => vi.mocked(showFlag).mockClear());

function fail(status: number) {
  return () => Promise.reject(new ApiError(status, `status ${status}`));
}

/** A write that fails when told to, and the component watching it, mounted. */
function watchedWrite(client: ReturnType<typeof createQueryClient>) {
  // Made up front: the cache calls `mutationFn` a few ticks after `mutate`, not inside it.
  let reject: (error: Error) => void = () => {};
  const answer = new Promise<never>((_, no) => {
    reject = no;
  });
  const observer = new MutationObserver(client, { mutationFn: () => answer });
  const unmount = observer.subscribe(() => {});
  const settled = observer.mutate().catch(() => {});
  return { unmount, fail: (error: Error) => reject(error), settled };
}

describe("createQueryClient", () => {
  it("hands a read's 401 to the session handler, once, without retrying it", async () => {
    const expire = vi.fn();
    const queryFn = vi.fn(fail(401));
    const client = createQueryClient(expire);
    await client.fetchQuery({ queryKey: ["projects"], queryFn }).catch(() => {});
    expect(expire).toHaveBeenCalledTimes(1);
    expect(queryFn).toHaveBeenCalledTimes(1);
  });

  it("hands a write's 401 to the session handler, and raises nothing else", async () => {
    const expire = vi.fn();
    const client = createQueryClient(expire);
    await client
      .getMutationCache()
      .build(client, { mutationFn: fail(401) })
      .execute(undefined)
      .catch(() => {});
    expect(expire).toHaveBeenCalledTimes(1);
    expect(showFlag).not.toHaveBeenCalled();
  });

  it("leaves a failure to the page that asked, while that page is still there", async () => {
    const expire = vi.fn();
    const client = createQueryClient(expire);
    await client
      .fetchQuery({ queryKey: ["ticket"], queryFn: fail(409), retry: false })
      .catch(() => {});
    const write = watchedWrite(client);
    write.fail(new ApiError(409, "the runtime is in use"));
    await write.settled;
    expect(expire).not.toHaveBeenCalled();
    expect(showFlag).not.toHaveBeenCalled();
  });

  it("stays quiet about a request its page replaced with a newer one", async () => {
    // A mounted component that sends again is removed from its older request, but it is still
    // on the page: the older one failing after it was replaced is not news.
    const client = createQueryClient(vi.fn());
    let reject: (error: Error) => void = () => {};
    const first = new Promise<never>((_, no) => {
      reject = no;
    });
    const observer = new MutationObserver<unknown, Error, number>(client, {
      mutationFn: (attempt) => (attempt === 1 ? first : new Promise(() => {})),
    });
    const unmount = observer.subscribe(() => {});
    const settled = observer.mutate(1).catch(() => {});
    observer.mutate(2).catch(() => {});
    reject(new ApiError(503, "the validator is busy"));
    await settled;
    expect(showFlag).not.toHaveBeenCalled();
    unmount();
  });

  it("says a write's failure itself once the page that asked has gone", async () => {
    const client = createQueryClient(vi.fn());
    const write = watchedWrite(client);
    write.unmount();
    write.fail(new ApiError(409, "the runtime is in use"));
    await write.settled;
    expect(showFlag).toHaveBeenCalledWith(
      expect.objectContaining({ appearance: "error", description: "the runtime is in use" }),
    );
  });
});

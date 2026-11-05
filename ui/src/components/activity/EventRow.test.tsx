// @vitest-environment happy-dom
/**
 * An activity row's link names the project's own host, even where there is nothing to open yet.
 */

import { afterEach, describe, expect, it } from "vitest";
import { forgeHost } from "../../forge";
import { click, type Mounted, mount, settle } from "../../testDom";
import { EventRow } from "./EventRow";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

describe("an activity row's link", () => {
  it("says a GitLab project's unchosen branch has nothing on GitLab to open", async () => {
    view = await mount(
      <ol>
        <EventRow
          entry={{
            id: 2,
            type: "agent_spawned",
            details: { step: "build", attempt: 1, branch: "impl/4-{slug}" },
            created_at: "2026-08-25T09:00:00Z",
          }}
          repo="group/app"
          forge={forgeHost({ kind: "gitlab" }, {})}
          held={null}
          onOpenText={() => {}}
        />
      </ol>,
    );
    await click(
      document.querySelector<HTMLButtonElement>('button[aria-label="About impl/4-{slug}"]'),
    );
    await settle();
    expect(document.body.textContent).toContain(
      "The agent had not chosen this name yet, so there is nothing on GitLab to open.",
    );
  });
});

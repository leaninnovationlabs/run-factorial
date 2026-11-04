// @vitest-environment happy-dom
/**
 * The ARIA tabs pattern — one tab stop, arrows that select as they move — and the segmented
 * control's pressed state.
 */

import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { byText, click, type Mounted, mount, press } from "../../testDom";
import { SegmentedControl, Tab, TabList, TabPanel, Tabs } from "./Tabs";

let view: Mounted | null = null;

afterEach(async () => {
  await view?.unmount();
  view = null;
});

function ProjectTabs() {
  const [tab, setTab] = useState("tickets");
  return (
    <Tabs id="project" selected={tab} onChange={setTab}>
      <TabList label="Project views">
        <Tab value="tickets">Tickets</Tab>
        <Tab value="waiting" badge={3}>
          Waiting
        </Tab>
        <Tab value="configuration">Configuration</Tab>
      </TabList>
      <TabPanel value="tickets">Tickets body</TabPanel>
      <TabPanel value="waiting">Waiting body</TabPanel>
      <TabPanel value="configuration">Configuration body</TabPanel>
    </Tabs>
  );
}

const tabs = () => [...document.querySelectorAll<HTMLElement>('[role="tab"]')];

describe("the tab's panel reference", () => {
  it("points only the selected tab at a panel, the one panel that exists", async () => {
    view = await mount(<ProjectTabs />);
    const controls = tabs().map((tab) => tab.getAttribute("aria-controls"));
    expect(controls.filter(Boolean)).toHaveLength(1);
    const target = controls.find(Boolean) ?? "";
    expect(document.getElementById(target)?.getAttribute("role")).toBe("tabpanel");
  });
});
const selected = () => tabs().find((tab) => tab.getAttribute("aria-selected") === "true");
const panel = () => document.querySelector('[role="tabpanel"]');

describe("Tabs", () => {
  it("puts only the selected tab in the tab order", async () => {
    view = await mount(<ProjectTabs />);
    expect(tabs().map((tab) => tab.tabIndex)).toEqual([0, -1, -1]);
  });

  it("selects and focuses as the arrows move, wrapping at either end", async () => {
    view = await mount(<ProjectTabs />);
    tabs()[0]?.focus();
    await press("ArrowRight", document.activeElement ?? document);
    expect(selected()?.textContent).toContain("Waiting");
    expect(document.activeElement).toBe(selected());
    expect(tabs().map((tab) => tab.tabIndex)).toEqual([-1, 0, -1]);
    await press("ArrowLeft", document.activeElement ?? document);
    await press("ArrowLeft", document.activeElement ?? document);
    expect(selected()?.textContent).toBe("Configuration");
  });

  it("jumps to the first and last tab with Home and End", async () => {
    view = await mount(<ProjectTabs />);
    tabs()[0]?.focus();
    await press("End", document.activeElement ?? document);
    expect(selected()?.textContent).toBe("Configuration");
    await press("Home", document.activeElement ?? document);
    expect(selected()?.textContent).toBe("Tickets");
  });

  it("shows the selected tab's panel alone, named by its tab", async () => {
    view = await mount(<ProjectTabs />);
    await click(byText(document, '[role="tab"]', "Configuration"));
    expect(document.querySelectorAll('[role="tabpanel"]')).toHaveLength(1);
    expect(panel()?.textContent).toBe("Configuration body");
    expect(document.getElementById(panel()?.getAttribute("aria-labelledby") ?? "")).toBe(
      selected(),
    );
  });
});

describe("SegmentedControl", () => {
  it("presses the chosen option alone, and reports a choice by value", async () => {
    const onChange = vi.fn();
    view = await mount(
      <SegmentedControl
        label="Status"
        value="open"
        onChange={onChange}
        options={[
          { value: "open", label: "Open", count: 4 },
          { value: "done", label: "Done" },
        ]}
      />,
    );
    const group = document.querySelector('[role="group"]');
    expect(group?.getAttribute("aria-label")).toBe("Status");
    const [open, done] = [...document.querySelectorAll("button")];
    expect(open?.getAttribute("aria-pressed")).toBe("true");
    expect(done?.getAttribute("aria-pressed")).toBe("false");
    await click(done);
    expect(onChange).toHaveBeenCalledWith("done");
  });
});

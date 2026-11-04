// @vitest-environment happy-dom
/**
 * The overlay stack: whatever is on top is the only thing that can be reached or dismissed.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { openLayerCount, pushLayer } from "./layers";

function layer(): HTMLElement {
  const element = document.createElement("div");
  document.body.append(element);
  return element;
}

function pressEscape(): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  document.dispatchEvent(event);
  return event;
}

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  root.id = "root";
  document.body.append(root);
});

afterEach(() => {
  document.body.replaceChildren();
  expect(openLayerCount()).toBe(0);
});

describe("pushLayer", () => {
  it("makes the app inert while a layer is open, and gives it back when it closes", () => {
    const pop = pushLayer(layer());
    expect(root.inert).toBe(true);
    pop();
    expect(root.inert).toBe(false);
  });

  it("makes every layer but the top one inert, so a dialog over a drawer holds the focus", () => {
    const drawer = layer();
    const dialog = layer();
    const popDrawer = pushLayer(drawer);
    const popDialog = pushLayer(dialog);
    expect(drawer.inert).toBe(true);
    expect(dialog.inert).toBe(false);
    popDialog();
    expect(drawer.inert).toBe(false);
    expect(root.inert).toBe(true);
    popDrawer();
    expect(root.inert).toBe(false);
  });

  it("sends Escape to the top layer alone", () => {
    const under = vi.fn();
    const top = vi.fn();
    const popUnder = pushLayer(layer(), under);
    const popTop = pushLayer(layer(), top);
    const event = pressEscape();
    expect(top).toHaveBeenCalledTimes(1);
    expect(under).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
    popTop();
    pressEscape();
    expect(under).toHaveBeenCalledTimes(1);
    popUnder();
  });

  it("leaves an Escape that something inside the layer already handled", () => {
    // The editor's menu closes itself on Escape and says so with preventDefault; the drawer it
    // sits in must not close on the same key press.
    const onEscape = vi.fn();
    const pop = pushLayer(layer(), onEscape);
    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    event.preventDefault();
    document.dispatchEvent(event);
    expect(onEscape).not.toHaveBeenCalled();
    pop();
  });

  it("closes out of order without leaving a stale layer inert", () => {
    const first = layer();
    const second = layer();
    const popFirst = pushLayer(first);
    const popSecond = pushLayer(second);
    popFirst();
    expect(second.inert).toBe(false);
    expect(root.inert).toBe(true);
    popSecond();
    expect(root.inert).toBe(false);
  });

  it("lets a menu over a drawer take Escape without making anything inert", () => {
    const drawer = layer();
    const menu = layer();
    const closeDrawer = vi.fn();
    const closeMenu = vi.fn();
    const popDrawer = pushLayer(drawer, closeDrawer);
    const popMenu = pushLayer(menu, closeMenu, { modal: false });
    expect(drawer.inert).toBe(false);
    expect(menu.inert).toBe(false);
    expect(root.inert).toBe(true);
    pressEscape();
    expect(closeMenu).toHaveBeenCalledTimes(1);
    expect(closeDrawer).not.toHaveBeenCalled();
    popMenu();
    popDrawer();
  });

  it("leaves the page live under a non-modal layer alone", () => {
    const pop = pushLayer(layer(), undefined, { modal: false });
    expect(root.inert).toBe(false);
    pop();
  });

  it("ignores a second pop of the same layer", () => {
    const pop = pushLayer(layer());
    const keep = pushLayer(layer());
    pop();
    pop();
    expect(openLayerCount()).toBe(1);
    keep();
  });
});

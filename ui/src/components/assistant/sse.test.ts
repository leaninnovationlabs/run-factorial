import { describe, expect, test } from "vitest";
import { parseSse } from "./sse";

describe("parseSse", () => {
  test("a message split across chunks is parsed once", () => {
    const first = parseSse('id: 4\nevent: item\ndata: {"id":', "");
    expect(first.messages).toEqual([]);
    const second = parseSse("1}\n\nevent: turn\ndata: null\n\nid: 5\nev", first.carry);
    expect(second.messages).toEqual([
      { id: "4", event: "item", data: '{"id":1}' },
      { event: "turn", data: "null" },
    ]);
    const third = parseSse("ent: item\ndata: {}\n\n", second.carry);
    expect(third.messages).toEqual([{ id: "5", event: "item", data: "{}" }]);
    expect(third.carry).toBe("");
  });

  test("comments (: ping) are ignored", () => {
    const { messages, carry } = parseSse(": ping\n\nevent: turn\n: aside\ndata: null\n\n", "");
    expect(messages).toEqual([{ event: "turn", data: "null" }]);
    expect(carry).toBe("");
  });

  test("a message's data lines are joined, and CRLF ends a line too", () => {
    const { messages } = parseSse("data: one\r\ndata: two\r\n\r\n", "");
    expect(messages).toEqual([{ event: "message", data: "one\ntwo" }]);
  });
});

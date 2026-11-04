import { describe, expect, it } from "vitest";
import { newPathProblem, parseCreatedFiles } from "./createdFiles";

const DIR = ".factorial";
const TAKEN = [".factorial/agents/plan-agent/agent.yaml", ".factorial/processes/process-a.yaml"];
const problem = (path: string, kind: "file" | "folder" = "file", folders: string[] = []) =>
  newPathProblem(path, kind, DIR, TAKEN, folders);

describe("newPathProblem", () => {
  it("allows a new path under the config folder", () => {
    expect(problem(".factorial/notes/runbook.md")).toBeNull();
    expect(problem(".factorial/agents/plan-agent/extra.md")).toBeNull();
    expect(problem(".factorial/notes", "folder")).toBeNull();
  });

  it.each([
    ".github/workflows/ci.yml",
    ".factorial",
    ".factorial/../x.md",
    ".factorial/.hidden/x.md",
    ".factorial/notes//x.md",
    ".factorial/notes/x y.md",
  ])("refuses %s as the server does", (path) => {
    expect(problem(path)).toMatch(/starts with a letter or digit/);
  });

  it("never lands on, under or over what is already there", () => {
    expect(problem(TAKEN[0] ?? "")).toMatch(/already here/);
    expect(problem(".factorial/agents/plan-agent/agent.yaml/x.md")).toMatch(/is a file/);
    expect(problem(".factorial/agents/plan-agent")).toMatch(/folder with that name/);
    expect(problem(".factorial/agents", "folder")).toMatch(/folder is already here/);
    expect(problem(".factorial/notes", "folder", [".factorial/notes"])).toMatch(/already here/);
  });
});

describe("parseCreatedFiles", () => {
  it("keeps only string bodies, and survives what an older build stored", () => {
    expect(parseCreatedFiles('{"a.md":"x","b.md":3}')).toEqual({ "a.md": "x" });
    expect(parseCreatedFiles("not json")).toEqual({});
    expect(parseCreatedFiles("[1]")).toEqual({});
    expect(parseCreatedFiles(null)).toEqual({});
  });
});

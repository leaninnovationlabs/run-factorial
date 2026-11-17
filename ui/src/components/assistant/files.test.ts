/** Files waiting to be sent: read into the draft, or refused with a sentence before they are. */

import { describe, expect, test } from "vitest";
import type { AttachedFile } from "./draft";
import { attach, MAX_FILES, NO_IMAGES } from "./files";

const png = (name = "shot.png", size = 12) =>
  new File([new Uint8Array(size).fill(7)], name, { type: "image/png" });
const log = (name = "worker.log", size = 12) =>
  new File(["x".repeat(size)], name, { type: "text/plain" });
const held = (count: number): AttachedFile[] =>
  Array.from({ length: count }, (_, at) => ({
    name: `f${at}`,
    size: 1,
    isImage: false,
    data: "eA==",
  }));

describe("attach", () => {
  test("a file is read into base64 with its name, its size and whether it is an image", async () => {
    const { added, refused } = await attach([png(), log("notes.txt", 3)], [], true);
    expect(refused).toBeNull();
    expect(added).toEqual([
      { name: "shot.png", size: 12, isImage: true, data: btoa("\x07".repeat(12)) },
      { name: "notes.txt", size: 3, isImage: false, data: btoa("xxx") },
    ]);
  });

  test("a fifth file is refused, and the ones that fit before it are kept", async () => {
    const { added, refused } = await attach(
      [log("a.log"), log("b.log")],
      held(MAX_FILES - 1),
      true,
    );
    expect(added.map((file) => file.name)).toEqual(["a.log"]);
    expect(refused).toBe("A message can hold four files.");
  });

  test("an image over 3.75 MB and a text file over 100 KB are refused by name", async () => {
    const big = await attach([png("big.png", 3_750_001)], [], true);
    expect([big.added, big.refused]).toEqual([[], "`big.png` is larger than 3.75 MB."]);
    expect((await attach([png("edge.png", 3_750_000)], [], true)).refused).toBeNull();
    const long = await attach([log("big.log", 100 * 1024 + 1)], [], true);
    expect([long.added, long.refused]).toEqual([
      [],
      "`big.log` is larger than 100 KB, the limit for a text file.",
    ]);
  });

  test("an image is refused where the assistant's backend takes none, and a text file is not", async () => {
    const { added, refused } = await attach([png(), log()], [], false);
    expect(added.map((file) => file.name)).toEqual(["worker.log"]);
    expect(refused).toBe(NO_IMAGES);
  });

  test("an empty file is refused", async () => {
    const { added, refused } = await attach([new File([], "empty.txt")], [], true);
    expect([added, refused]).toEqual([[], "`empty.txt` is empty."]);
  });

  test("an image the browser gives no type for is known by its name, and held to the image's limit", async () => {
    const untyped = new File([new Uint8Array(200 * 1024)], "Shot.PNG");
    const { added, refused } = await attach([untyped], [], true);
    expect(refused).toBeNull();
    expect(added[0]?.isImage).toBe(true);
  });

  test("a large file that is not an image is not called a text file", async () => {
    const zip = new File([new Uint8Array(200 * 1024)], "logs.zip", { type: "application/zip" });
    const { refused } = await attach([zip], [], true);
    expect(refused).toBe(
      "`logs.zip` is not an image, and is larger than 100 KB, the limit for a text file.",
    );
  });

  test("a long name is cut to what the API takes, its ending kept", async () => {
    const long = new File(["x"], `${"n".repeat(200)}.log`, { type: "text/plain" });
    const { added } = await attach([long], [], true);
    expect(added[0]?.name).toHaveLength(120);
    expect(added[0]?.name.endsWith(".log")).toBe(true);
  });
});

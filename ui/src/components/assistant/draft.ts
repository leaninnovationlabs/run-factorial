/**
 * What a person has written to the assistant and not yet sent: the text, the things on the
 * dashboard they pointed at, and the files they attached. The provider holds one draft, so it is
 * the same in every frame the conversation is drawn in and is still there when a frame closes
 * and opens again.
 */

/** Something on the dashboard the person pointed at, as the assistant is told of it. */
export type Pointed = {
  /** The chip's words. */
  label: string;
  /** What was there to read. */
  text: string;
  /** The dashboard page it was on. */
  page: string | null;
  /** The nearest dashboard link: a row's ticket, say. */
  href?: string | null;
  /** The control's name, where it carries one (`assistAnchors.ts`). */
  anchor?: string | null;
  /** In the config editor: the file, and the lines pointed at. */
  file?: string | null;
  lines?: [number, number] | null;
};

export type AttachedFile = {
  name: string;
  size: number;
  isImage: boolean;
  /** The bytes, in base64, as the API takes them. */
  data: string;
};

export type Draft = { text: string; pointed: Pointed[]; files: AttachedFile[] };

export const EMPTY_DRAFT: Draft = { text: "", pointed: [], files: [] };

/** Nothing to send: no words, nothing pointed at, no file. */
export function isEmpty(draft: Draft): boolean {
  return draft.text.trim() === "" && draft.pointed.length === 0 && draft.files.length === 0;
}

/**
 * Files a person attaches to a message, read here into the draft: an image the model can look
 * at, or a small text file. The limits are the API's (`assistant/attachments.py`), checked first
 * here so a file that cannot go is refused when it is picked, with a sentence, and not after the
 * message was written. The API decides again from the bytes: a browser's word for a file's type
 * is only a hint, used here to pick which limit to hold it to.
 */

import type { AttachedFile } from "./draft";

export const MAX_FILES = 4;
/** 3.75 MB: 5 MB once it is base64, which is how a model's API measures an image. */
const MAX_IMAGE = 3_750_000;
const MAX_TEXT = 100 * 1024;

export const NO_IMAGES =
  "The assistant's model connection takes no images. Attach a text file, or describe the image.";

async function base64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  // In pieces: a call cannot take a whole file's bytes as its arguments.
  for (let at = 0; at < bytes.length; at += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(at, at + 0x8000));
  }
  return btoa(binary);
}

/** A browser gives some images no type at all: a name that ends as one of the four is one. */
const IMAGE_NAME = /\.(png|jpe?g|gif|webp)$/i;
/** What a browser calls text; anything else over the text limit is not a text file at all. */
const TEXT_TYPE = /^(text\/|application\/(json|xml|x-yaml|yaml|toml)$)/;
const NAME_MAX = 120;

/** A name the API takes, cut in its middle so that its ending still says what it is. */
function named(name: string): string {
  if (name.length <= NAME_MAX) return name;
  const ending = name.slice(name.lastIndexOf(".")).slice(0, 12);
  return name.slice(0, NAME_MAX - ending.length) + ending;
}

function whyNot(file: File, isImage: boolean, acceptsImages: boolean): string | null {
  if (file.size === 0) return `\`${file.name}\` is empty.`;
  if (isImage && !acceptsImages) return NO_IMAGES;
  if (isImage && file.size > MAX_IMAGE) return `\`${file.name}\` is larger than 3.75 MB.`;
  if (!isImage && file.size > MAX_TEXT) {
    return file.type === "" || TEXT_TYPE.test(file.type)
      ? `\`${file.name}\` is larger than 100 KB, the limit for a text file.`
      : `\`${file.name}\` is not an image, and is larger than 100 KB, the limit for a text file.`;
  }
  return null;
}

/** The files that can join `held`, read, and the sentence for the first that could not. */
export async function attach(
  files: File[],
  held: AttachedFile[],
  acceptsImages: boolean,
): Promise<{ added: AttachedFile[]; refused: string | null }> {
  const added: AttachedFile[] = [];
  let refused: string | null = null;
  for (const file of files) {
    const isImage = file.type.startsWith("image/") || (!file.type && IMAGE_NAME.test(file.name));
    const why =
      held.length + added.length >= MAX_FILES
        ? "A message can hold four files."
        : whyNot(file, isImage, acceptsImages);
    if (why) {
      refused ??= why;
      continue;
    }
    added.push({ name: named(file.name), size: file.size, isImage, data: await base64(file) });
  }
  return { added, refused };
}

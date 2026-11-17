/**
 * The assistant's reply, as markdown (tech-stack decision 63): GitHub's tables, lists and code,
 * and nothing a model's text could use to run or fetch anything.
 *
 * Raw HTML is skipped, not escaped into view. A link keeps only an `http(s)` address, which opens
 * in a new tab, or a dashboard path, which the router follows and which closes the Drawer (a
 * docked or floating conversation stays beside the page it opens); any
 * other link is its text alone. An image is never loaded, whatever its address: a reply that a
 * prompt injection shaped could otherwise send data out in an image's URL with no click at all.
 *
 * Some models write a list with a bullet character, which markdown reads as one paragraph, so a
 * line that starts with one is made a `-` item before the text is parsed (`bulletLines`).
 */

import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Code, CodeBlock, cx } from "../../ui";
import { DashboardLink, isDashboardPath } from "../AssistantProvider";
import { useBodyText } from "../frames";

const LINK = "font-medium underline underline-offset-2";

function safeUrl(url: string): string {
  return /^https?:\/\//i.test(url) || isDashboardPath(url) ? url : "";
}

const BULLET = /^(\s*)[•◦▪‣·]\s+/;
const FENCE = /^\s*(```|~~~)/;

/** Outside a code fence, a line that starts with a bullet character becomes a `-` list item. */
export function bulletLines(text: string): string {
  let fenced = false;
  return text
    .split("\n")
    .map((line) => {
      if (FENCE.test(line)) {
        fenced = !fenced;
        return line;
      }
      return fenced ? line : line.replace(BULLET, "$1- ");
    })
    .join("\n");
}

type Hast = { type: string; value?: string; children?: Hast[] };

function textOf(node: Hast | undefined): string {
  if (!node) return "";
  return node.type === "text" ? (node.value ?? "") : (node.children ?? []).map(textOf).join("");
}

const COMPONENTS: Components = {
  a: ({ href, children }) => {
    if (!href) return <span>{children}</span>;
    if (href.startsWith("/")) return <DashboardLink to={href}>{children}</DashboardLink>;
    return (
      <a href={href} target="_blank" rel="noreferrer noopener" className={LINK}>
        {children}
      </a>
    );
  },
  img: ({ alt }) => (alt ? <span>{alt}</span> : null),
  h1: ({ children }) => <h3 className="text-heading-sm">{children}</h3>,
  h2: ({ children }) => <h3 className="text-heading-sm">{children}</h3>,
  h3: ({ children }) => <h4 className="text-heading-xs">{children}</h4>,
  h4: ({ children }) => <h4 className="text-heading-xs">{children}</h4>,
  h5: ({ children }) => <h4 className="text-heading-xs">{children}</h4>,
  h6: ({ children }) => <h4 className="text-heading-xs">{children}</h4>,
  ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
  // A list that resumes after a code block or a paragraph says where it resumes.
  ol: ({ children, start }) => (
    <ol start={start} className="list-decimal space-y-1 pl-5">
      {children}
    </ol>
  ),
  // A task item has its checkbox for a marker.
  li: ({ children, className }) => (
    <li className={className?.includes("task-list-item") ? "list-none" : undefined}>{children}</li>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-bold pl-3 text-subtle">{children}</blockquote>
  ),
  code: ({ children }) => <Code>{children}</Code>,
  pre: ({ node }) => <CodeBlock text={textOf(node as Hast).replace(/\n$/, "")} label="code" />,
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse">{children}</table>
    </div>
  ),
  th: ({ children, style }) => (
    <th
      style={style}
      className="border border-default bg-surface-sunken px-2 py-1 text-left text-body-sm font-semibold text-subtle"
    >
      {children}
    </th>
  ),
  td: ({ children, style }) => (
    <td style={style} className="border border-default px-2 py-1 align-top">
      {children}
    </td>
  ),
};

/** Memoized on its text: the provider holds the draft, so every key typed in the composer draws
 *  the conversation again, and markdown is parsed on every draw of a reply. */
export const AssistantText = memo(function AssistantText({ text }: { text: string }) {
  const body = useBodyText();
  return (
    <div className={cx("space-y-2 leading-relaxed break-words text-default", body)}>
      <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={COMPONENTS}>
        {bulletLines(text)}
      </Markdown>
    </div>
  );
});

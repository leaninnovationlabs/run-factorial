/**
 * The assistant's reply, as markdown (tech-stack decision 63): GitHub's tables, lists and code,
 * and nothing a model's text could use to run or fetch anything.
 *
 * Raw HTML is skipped, not escaped into view. A link keeps only an `http(s)` address, which opens
 * in a new tab, or a dashboard path, which the router follows and which closes the Drawer; any
 * other link is its text alone. An image is never loaded, whatever its address: a reply that a
 * prompt injection shaped could otherwise send data out in an image's URL with no click at all.
 */

import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { DashboardLink, isDashboardPath } from "../AssistantProvider";

const LINK = "font-medium underline underline-offset-2";

function safeUrl(url: string): string {
  return /^https?:\/\//i.test(url) || isDashboardPath(url) ? url : "";
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
  ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-bold pl-3 text-subtle">{children}</blockquote>
  ),
  code: ({ children }) => (
    <code className="rounded-xs bg-neutral px-1 py-px font-mono text-code break-words">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="overflow-x-auto rounded-md bg-neutral p-3 text-body-sm [&>code]:bg-transparent [&>code]:p-0">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-body">{children}</table>
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

export function AssistantText({ text }: { text: string }) {
  return (
    <div className="space-y-2 text-body leading-relaxed break-words text-default">
      <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={COMPONENTS}>
        {text}
      </Markdown>
    </div>
  );
}

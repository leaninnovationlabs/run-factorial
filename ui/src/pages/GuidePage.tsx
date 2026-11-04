/**
 * The guide reference page (docs/design-system.md, Reference pages). It renders `docs/design-system.md` itself rather than
 * a copy, so it cannot say something the doc does not, and adds only what markdown cannot show:
 * the colors drawn live, in whichever theme is on.
 */

import type { ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import DOC from "../../../docs/design-system.md?raw";
import { Lozenge, PageHeader, Panel, usePageTitle } from "../components/ui";
import type { Role } from "../status";
import { ReferenceNav } from "./ReferenceNav";

/** The anchor a heading is reached by, from its text. */
export function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node) {
    return textOf((node.props as { children?: ReactNode }).children);
  }
  return "";
}

/** The doc's sections, in order: the contents list. */
const SECTIONS = [...DOC.matchAll(/^## (.+)$/gm)].map(([, title = ""]) => ({
  id: slug(title),
  title,
}));

const LINK = "font-medium underline underline-offset-2";

const COMPONENTS: Components = {
  // The page header carries the title.
  h1: () => null,
  h2: ({ children }) => (
    <h2 id={slug(textOf(children))} className="scroll-mt-6 pt-4 text-heading-lg">
      {children}
    </h2>
  ),
  h3: ({ children }) => <h3 className="pt-2 text-heading-md">{children}</h3>,
  h4: ({ children }) => <h4 className="pt-1 text-heading-sm">{children}</h4>,
  a: ({ href, children }) => (
    <a href={href} className={LINK}>
      {children}
    </a>
  ),
  img: ({ alt }) => (alt ? <span>{alt}</span> : null),
  hr: () => <hr className="border-default" />,
  ul: ({ children }) => <ul className="list-disc space-y-1.5 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal space-y-1.5 pl-5">{children}</ol>,
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
    <div className="overflow-x-auto rounded-md border border-default">
      <table className="w-full border-collapse text-body">{children}</table>
    </div>
  ),
  th: ({ children, style }) => (
    <th
      style={style}
      className="border-b border-default bg-table-head px-3 py-2 text-left text-body-sm font-semibold text-subtle"
    >
      {children}
    </th>
  ),
  td: ({ children, style }) => (
    <td style={style} className="border-b border-default px-3 py-2 align-top">
      {children}
    </td>
  ),
};

/** Every class here is written out whole: Tailwind finds a class by reading the source, so a
 *  name built at run time would compile to nothing. */
const SURFACES = [
  { name: "bg-surface-sunken", use: "The page ground", className: "bg-surface-sunken" },
  { name: "bg-surface", use: "Panels and cards", className: "bg-surface" },
  { name: "bg-surface-raised", use: "Raised above the page", className: "bg-surface-raised" },
  { name: "bg-surface-overlay", use: "Popups and drawers", className: "bg-surface-overlay" },
];

const TEXT = [
  { name: "text-default", className: "text-default" },
  { name: "text-subtle", className: "text-subtle" },
  { name: "text-subtlest", className: "text-subtlest" },
  { name: "text-disabled", className: "text-disabled" },
];

const ROLES: Role[] = ["neutral", "information", "success", "warning", "danger", "discovery"];

function Name({ children }: { children: string }) {
  return <code className="font-mono text-body-sm text-subtle">{children}</code>;
}

function Colors() {
  return (
    <Panel
      title="Color at a glance"
      subtitle="Drawn live from the tokens: switch the theme and every value changes"
    >
      <div className="grid gap-6 p-4 lg:grid-cols-2">
        <section aria-label="Surfaces" className="space-y-2">
          <h3 className="text-heading-xs">Surfaces</h3>
          <ul className="grid grid-cols-2 gap-2">
            {SURFACES.map((surface) => (
              <li key={surface.name} className="space-y-1">
                <div className={`h-12 rounded-md border border-default ${surface.className}`} />
                <Name>{surface.name}</Name>
                <p className="text-body-sm text-subtle">{surface.use}</p>
              </li>
            ))}
          </ul>
        </section>
        <section aria-label="Text and roles" className="space-y-2">
          <h3 className="text-heading-xs">Text</h3>
          <ul className="space-y-1">
            {TEXT.map((text) => (
              <li key={text.name} className="flex items-baseline justify-between gap-4">
                <span className={`text-body ${text.className}`}>Ship the spec for review</span>
                <Name>{text.name}</Name>
              </li>
            ))}
          </ul>
          <h3 className="pt-2 text-heading-xs">Roles</h3>
          <ul className="space-y-1">
            {ROLES.map((role) => (
              <li key={role} className="flex items-center gap-2">
                <Lozenge appearance={role}>{role}</Lozenge>
                <Lozenge appearance={role} isBold>
                  {role}
                </Lozenge>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </Panel>
  );
}

function Contents() {
  return (
    <nav aria-label="Contents" className="space-y-1">
      <h2 className="px-2 text-heading-xxs text-subtle">Contents</h2>
      <ul className="space-y-0.5">
        <li>
          <a
            href="#color-at-a-glance"
            className="block rounded-md px-2 py-1 text-body text-subtle hover:bg-neutral-subtle-hovered hover:text-default"
          >
            Color at a glance
          </a>
        </li>
        {SECTIONS.map((section) => (
          <li key={section.id}>
            <a
              href={`#${section.id}`}
              className="block rounded-md px-2 py-1 text-body text-subtle hover:bg-neutral-subtle-hovered hover:text-default"
            >
              {section.title}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export default function GuidePage() {
  usePageTitle(["Design system"]);
  return (
    <div className="min-h-dvh bg-surface-sunken px-4 py-6 lg:px-8">
      <div className="space-y-6">
        <PageHeader
          title="Design system"
          actions={<ReferenceNav current="guide" />}
          bottomBar={
            <span>
              How the dashboard's UI is built, and the rules every change follows. The catalogue
              shows every design primitive; the sample shows them composed into a page.
            </span>
          }
        />
        <div className="grid gap-8 lg:grid-cols-[14rem_1fr]">
          <aside className="lg:sticky lg:top-6 lg:self-start">
            <Contents />
          </aside>
          <div className="min-w-0 space-y-6">
            <div id="color-at-a-glance" className="scroll-mt-6">
              <Colors />
            </div>
            <article className="space-y-4 rounded-lg border border-default bg-surface p-6 text-body leading-relaxed">
              <Markdown remarkPlugins={[remarkGfm]} skipHtml components={COMPONENTS}>
                {DOC}
              </Markdown>
            </article>
          </div>
        </div>
      </div>
    </div>
  );
}

"use client";

import { memo, useMemo, type ComponentPropsWithoutRef, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { citationOrder, remarkCitations, sourceResolver } from "@/lib/chat/citations";
import type { Source } from "@/lib/chat/types";
import { CitationGroup } from "./CitationChip";
import { CopyButton } from "./CopyButton";

/* Assistant answers. react-markdown builds React elements from the Markdown
   tree, never HTML strings, and without rehype-raw any raw HTML in a model
   answer stays inert text — the answer can't inject markup into the console.
   Images never load (chat.2): an injected `![](https://…)` in a source must
   not make the console fetch a URL, so an image renders as its alt text. */

/**
 * Keeps http(s), mailto and same-origin relative links; anything else
 * (javascript:, data:, protocol-relative //host) is dropped. Browsers strip
 * tabs and newlines from URLs, so the scheme is read with them removed.
 */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  const bare = url.replace(/[\u0000-\u0020\u007f]/g, "");
  if (!bare) return null;
  if (/^[/\\]{2}/.test(bare)) return null;
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(bare);
  if (scheme) return ["http", "https", "mailto"].includes(scheme[1].toLowerCase()) ? url.trim() : null;
  return url.trim();
}

interface HastNode {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

function hastText(node: HastNode | undefined): string {
  if (!node) return "";
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(hastText).join("");
}

function CodeBlock({ node }: { node?: HastNode }) {
  const code = node?.children?.find((child) => child.tagName === "code");
  const classes = code?.properties?.className;
  const language = (Array.isArray(classes) ? classes : [])
    .map(String)
    .find((name) => name.startsWith("language-"))
    ?.slice("language-".length);
  const text = hastText(code ?? node).replace(/\n$/, "");
  return (
    <div className="my-3 overflow-hidden rounded-lg border border-line bg-surface-3">
      <div className="flex h-8 items-center justify-between border-b border-line px-3 text-xs text-muted">
        <span className="font-mono">{language || "text"}</span>
        <CopyButton text={text} label="Copy code" compact />
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[13px] leading-relaxed text-ink">
        <code>{text}</code>
      </pre>
    </div>
  );
}

const TABLE_CELL = "border-b border-line px-3 py-2 text-left align-top";

function baseComponents(sources: Map<string, Source>, numbers: Map<string, number>, onOpenSource?: (id: string) => void): Components {
  return {
    p: ({ children }) => <p className="my-3 leading-7 first:mt-0 last:mb-0">{children}</p>,
    h1: ({ children }) => <h3 className="mt-5 mb-2 text-lg font-semibold first:mt-0">{children}</h3>,
    h2: ({ children }) => <h3 className="mt-5 mb-2 text-base font-semibold first:mt-0">{children}</h3>,
    h3: ({ children }) => <h4 className="mt-4 mb-2 font-semibold first:mt-0">{children}</h4>,
    h4: ({ children }) => <h5 className="mt-4 mb-1 font-semibold first:mt-0">{children}</h5>,
    ul: ({ children }) => <ul className="my-3 list-disc space-y-1.5 pl-6 marker:text-faint">{children}</ul>,
    ol: ({ children }) => <ol className="my-3 list-decimal space-y-1.5 pl-6 marker:text-muted">{children}</ol>,
    li: ({ children }) => <li className="pl-1 leading-7">{children}</li>,
    strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
    blockquote: ({ children }) => <blockquote className="my-3 border-l-2 border-line-strong pl-4 text-muted">{children}</blockquote>,
    hr: () => <hr className="my-5 border-line" />,
    a: ({ href, children }) => {
      const safe = safeHref(href);
      if (!safe) return <span>{children}</span>;
      const external = /^https?:/i.test(safe);
      return (
        <a
          href={safe}
          rel="noopener noreferrer"
          {...(external ? { target: "_blank" } : {})}
          className="text-info underline decoration-info/40 underline-offset-2 hover:decoration-info"
        >
          {children}
        </a>
      );
    },
    img: ({ alt }) => (alt ? <span>{alt}</span> : null),
    pre: ({ node }) => <CodeBlock node={node as HastNode | undefined} />,
    code: ({ children }) => <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[0.85em] text-ink">{children}</code>,
    table: ({ children }) => (
      <div className="my-4 overflow-x-auto rounded-lg border border-line">
        <table className="nums w-full border-collapse text-sm">{children}</table>
      </div>
    ),
    thead: ({ children }) => <thead className="bg-surface-2 text-ink">{children}</thead>,
    th: ({ children, style }) => <th style={style} className={`${TABLE_CELL} font-semibold`}>{children}</th>,
    td: ({ children, style }) => <td style={style} className={TABLE_CELL}>{children}</td>,
    tr: ({ children }) => <tr className="last:[&>td]:border-b-0">{children}</tr>,
    // Citations arrive as <span data-citation="id1 id2"> from remarkCitations.
    span: (props: ComponentPropsWithoutRef<"span"> & { node?: unknown; "data-citation"?: string }) => {
      const ids = props["data-citation"];
      if (!ids) return <span>{props.children}</span>;
      const items = ids
        .split(" ")
        .map((id) => ({ source: sources.get(id), number: numbers.get(id) }))
        .filter((item): item is { source: Source; number: number } => !!item.source && !!item.number);
      return <CitationGroup items={items} onOpen={onOpenSource} />;
    },
  };
}

export const Markdown = memo(function Markdown({
  content,
  sources = [],
  onOpenSource,
  className = "",
}: {
  content: string;
  sources?: Source[];
  onOpenSource?: (id: string) => void;
  className?: string;
}): ReactNode {
  const byId = useMemo(() => new Map(sources.map((source) => [source.id, source])), [sources]);
  const resolve = useMemo(() => sourceResolver(byId.keys()), [byId]);
  // Chips are numbered by first citation in this answer (1, 2, 3 …), the
  // same order the Sources panel lists cited sources in.
  // Keyed on the order itself, not the content: while an answer streams the
  // components below must keep their identity, or every token would remount
  // the whole answer.
  const orderKey = useMemo(() => citationOrder(content, resolve).join(" "), [content, resolve]);
  const numbers = useMemo(() => new Map(orderKey ? orderKey.split(" ").map((id, index) => [id, index + 1]) : []), [orderKey]);
  const remarkPlugins = useMemo(
    () => [remarkGfm, [remarkCitations, { resolve }]] as NonNullable<ComponentPropsWithoutRef<typeof ReactMarkdown>["remarkPlugins"]>,
    [resolve],
  );
  const components = useMemo(() => baseComponents(byId, numbers, onOpenSource), [byId, numbers, onOpenSource]);
  return (
    <div className={`min-w-0 text-[15px] text-ink [overflow-wrap:anywhere] ${className}`}>
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components} urlTransform={(url) => safeHref(url) ?? ""}>
        {content}
      </ReactMarkdown>
    </div>
  );
});

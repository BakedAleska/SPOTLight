/* Citation markers in an answer: the one rule shared with the backend
   (contract §2 "Grounding check", chat.2).

   - A citation group is `[` … `]` on one line, not starting inside inline or
     fenced code, and not a Markdown link's text (`[text](url)`).
   - Its inside splits on `,` and `;`; each part is trimmed of whitespace,
     straight and curly quotes, and backticks.
   - A part *cites* a source when it equals a source id case-insensitively.
   - A group with at least one citing part renders as chips (one per citing
     part, other parts dropped); a group with none stays literal text.

   Chips are numbered by first citation in the answer (1, 2, 3 …), not by the
   backend's retrieval order `n`. */

/** Shape of a source id (contract §2): `kind:rest`, e.g. `event:act-A0001`. */
export const CITATION_ID = /^[A-Za-z_]+:[\w.:/\-]+$/;

/** backend GROUP: `\[([^\[\]\n]+)\](?!\()`; the `(` check is done by hand. */
const BRACKETS = /\[([^[\]\n]+)\]/g;
/** backend PART_TRIM: " \t\r\n'\"`“”‘’" */
const TRIM = /^[ \t\r\n'"`“”‘’]+|[ \t\r\n'"`“”‘’]+$/g;

/** Maps a citation part to the canonical source id it cites, or null. */
export type Resolve = (part: string) => string | null;

export function sourceResolver(ids: Iterable<string>): Resolve {
  // Case-folded only, first id wins: exactly the backend's lookup.
  const folded = new Map<string, string>();
  for (const id of ids) {
    const key = id.toLowerCase();
    if (!folded.has(key)) folded.set(key, id);
  }
  return (part) => folded.get(part.toLowerCase()) ?? null;
}

/** The parts of one group's inside, trimmed, empties dropped. */
export function groupParts(inner: string): string[] {
  return inner.split(/[,;]/).map((part) => part.replace(TRIM, "")).filter(Boolean);
}

/** A run of inline content: plain text, or a code span (never scanned). */
export interface Segment {
  code: boolean;
  value: string;
}

export type CitationToken =
  | { type: "text"; value: string }
  /** A code span kept as is; `value` is the part outside any citing group. */
  | { type: "code"; segment: number; value: string }
  | { type: "cite"; ids: string[] };

interface Group {
  start: number;
  end: number;
  parts: string[];
}

/** Bracket groups whose `[` is outside code. A group may contain a code
    span (`` [`incident:AN012`] ``); a group starting inside one is never
    seen. A group followed by `(` is a Markdown link, not a citation. */
function findGroups(segments: Segment[]): { raw: string; groups: Group[] } {
  let raw = "";
  let scan = "";
  for (const segment of segments) {
    raw += segment.value;
    // A `[` inside code can't open a group.
    scan += segment.code ? segment.value.replace(/\[/g, "\u0000") : segment.value;
  }
  const groups: Group[] = [];
  for (const match of scan.matchAll(BRACKETS)) {
    const start = match.index;
    const end = start + match[0].length;
    if (scan[end] === "(") continue;
    groups.push({ start, end, parts: groupParts(raw.slice(start + 1, end - 1)) });
  }
  return { raw, groups };
}

/**
 * Splits inline content into text, untouched code spans and citation groups.
 * Adjacent citing groups (`[a][b]`) merge into one chip group.
 */
export function tokenizeSegments(segments: Segment[], resolve: Resolve): CitationToken[] {
  const { groups } = findGroups(segments);
  const citing = groups
    .map((group) => ({ ...group, ids: group.parts.map(resolve).filter((id): id is string => !!id) }))
    .filter((group) => group.ids.length > 0);

  const out: CitationToken[] = [];
  const pushText = (value: string) => {
    if (!value) return;
    const last = out[out.length - 1];
    if (last?.type === "text") last.value += value;
    else out.push({ type: "text", value });
  };
  const pushCite = (ids: string[]) => {
    const last = out[out.length - 1];
    if (last?.type !== "cite") {
      out.push({ type: "cite", ids: [...new Set(ids)] });
      return;
    }
    for (const id of ids) if (!last.ids.includes(id)) last.ids.push(id);
  };

  // Walk the segments, cutting them at group boundaries. A group always
  // opens in text (a `[` in code can't open one), so its chips are emitted
  // from a text segment; code inside a citing group is consumed with it.
  let offset = 0;
  let next = 0;
  segments.forEach((segment, index) => {
    const segStart = offset;
    const segEnd = offset + segment.value.length;
    offset = segEnd;
    let kept = "";
    let cursor = segStart;
    while (cursor < segEnd) {
      while (next < citing.length && citing[next].end <= cursor) next++;
      const group = citing[next];
      if (!group || group.start >= segEnd) {
        kept += segment.value.slice(cursor - segStart);
        cursor = segEnd;
      } else if (group.start > cursor) {
        kept += segment.value.slice(cursor - segStart, group.start - segStart);
        cursor = group.start;
      } else {
        if (segment.code) {
          if (kept) out.push({ type: "code", segment: index, value: kept });
        } else pushText(kept);
        kept = "";
        // Inside a citing group: emit its chips once, at its opening bracket.
        if (cursor === group.start) pushCite(group.ids);
        cursor = Math.min(group.end, segEnd);
      }
    }
    if (segment.code) {
      if (kept) out.push({ type: "code", segment: index, value: kept });
    } else pushText(kept);
  });
  return out;
}

/** Plain text (no code spans) to tokens; the common case in tests. */
export function tokenizeCitations(text: string, resolve: Resolve) {
  return tokenizeSegments([{ code: false, value: text }], resolve) as Exclude<CitationToken, { type: "code" }>[];
}

/* ── raw Markdown ───────────────────────────────────────────────────────────
   The chip numbers and the Sources panel order need the citation order of a
   whole answer, outside the Markdown renderer. Fenced blocks and inline code
   spans are cut out first, so this agrees with the remark plugin below. */

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** Splits Markdown into text and code segments (fenced blocks, code spans). */
export function markdownSegments(markdown: string): Segment[] {
  const segments: Segment[] = [];
  const push = (code: boolean, value: string) => {
    if (!value) return;
    const last = segments[segments.length - 1];
    if (last && last.code === code) last.value += value;
    else segments.push({ code, value });
  };
  const inline = (text: string) => {
    let i = 0;
    while (i < text.length) {
      const open = text.indexOf("`", i);
      if (open === -1) break;
      let n = 1;
      while (text[open + n] === "`") n++;
      // The closing run must be exactly as long as the opening one.
      let close = -1;
      let from = open + n;
      while (from < text.length) {
        const at = text.indexOf("`", from);
        if (at === -1) break;
        let m = 1;
        while (text[at + m] === "`") m++;
        if (m === n) { close = at; break; }
        from = at + m;
      }
      if (close === -1) {
        push(false, text.slice(i, open + n));
        i = open + n;
        continue;
      }
      push(false, text.slice(i, open));
      push(true, text.slice(open, close + n));
      i = close + n;
    }
    push(false, text.slice(i));
  };

  const lines = markdown.split(/(?<=\n)/);
  let fence: { char: string; size: number } | null = null;
  let prose = "";
  for (const line of lines) {
    if (fence) {
      push(true, line);
      const close = new RegExp(`^ {0,3}${fence.char === "`" ? "`" : "~"}{${fence.size},}\\s*$`);
      if (close.test(line.replace(/\n$/, ""))) fence = null;
      continue;
    }
    const open = FENCE.exec(line);
    if (open) {
      inline(prose);
      prose = "";
      fence = { char: open[1][0], size: open[1].length };
      push(true, line);
      continue;
    }
    prose += line;
  }
  inline(prose);
  return segments;
}

/** Canonical ids an answer cites, in order of first citation (chip 1, 2, 3 …). */
export function citationOrder(markdown: string, resolve: Resolve): string[] {
  const seen: string[] = [];
  for (const token of tokenizeSegments(markdownSegments(markdown), resolve)) {
    if (token.type === "cite") for (const id of token.ids) if (!seen.includes(id)) seen.push(id);
  }
  return seen;
}

/** Every bracket group outside code, in order, with the canonical ids it
    renders as chips (empty: the group stays literal text). Mirrors the
    backend's `citation_groups` and the shared fixtures. */
export function citationGroups(markdown: string, resolve: Resolve): { raw: string; chips: string[] }[] {
  const { raw, groups } = findGroups(markdownSegments(markdown));
  return groups.map((group) => ({
    raw: raw.slice(group.start, group.end),
    chips: group.parts.map(resolve).filter((id): id is string => !!id),
  }));
}

/** The grounding check's citation half, as the backend computes it:
    cited ids in order of first use, and id-shaped parts that cite nothing. */
export function citationsIn(markdown: string, sourceIds: string[]): { cited: string[]; unknown: string[] } {
  const resolve = sourceResolver(sourceIds);
  const cited: string[] = [];
  const unknown: string[] = [];
  for (const group of findGroups(markdownSegments(markdown)).groups) {
    for (const part of group.parts) {
      const id = resolve(part);
      if (id) {
        if (!cited.includes(id)) cited.push(id);
      } else if (CITATION_ID.test(part) && !unknown.includes(part)) {
        unknown.push(part);
      }
    }
  }
  return { cited, unknown };
}

/* ── remark plugin ───────────────────────────────────────────────────────────
   Runs on the Markdown tree. Fenced blocks are their own node type and are
   never entered; inline code spans are kept verbatim unless they sit inside a
   citing group. Each group becomes a <span data-citation="id1 id2"> that the
   renderer swaps for chips. Source ids never contain spaces (CITATION_ID), so
   a space-joined list round-trips. */

interface MdNode {
  type: string;
  value?: string;
  children?: MdNode[];
  data?: Record<string, unknown>;
}

/** Node types whose text must stay verbatim. Links are skipped so a chip
    (a button) never ends up nested inside an anchor. */
const SKIP = new Set(["code", "inlineCode", "link", "linkReference", "html", "definition", "image", "imageReference"]);

export function remarkCitations(options: { resolve: Resolve }) {
  const { resolve } = options;

  function rewriteRun(run: MdNode[]): MdNode[] {
    if (!run.some((node) => node.type === "text" && node.value?.includes("["))) return run;
    const segments = run.map((node) => ({ code: node.type === "inlineCode", value: node.value ?? "" }));
    const out: MdNode[] = [];
    for (const token of tokenizeSegments(segments, resolve)) {
      if (token.type === "text") out.push({ type: "text", value: token.value });
      else if (token.type === "code") {
        const node = run[token.segment];
        out.push(token.value === node.value ? node : { ...node, value: token.value });
      }
      else out.push({ type: "citation", children: [], data: { hName: "span", hProperties: { dataCitation: token.ids.join(" ") }, hChildren: [] } });
    }
    return out;
  }

  function walk(node: MdNode) {
    if (!node.children || SKIP.has(node.type)) return;
    const next: MdNode[] = [];
    let run: MdNode[] = [];
    const flush = () => {
      if (run.length) next.push(...rewriteRun(run));
      run = [];
    };
    for (const child of node.children) {
      if (child.type === "text" || child.type === "inlineCode") {
        run.push(child);
        continue;
      }
      flush();
      walk(child);
      next.push(child);
    }
    flush();
    node.children = next;
  }
  return (tree: MdNode) => walk(tree);
}

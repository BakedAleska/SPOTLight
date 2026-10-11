/* A small, incremental Server-Sent Events parser (the WHATWG "event stream"
   format), for fetch() bodies. EventSource can't POST, so the chat reads the
   stream itself and needs this.

   Network chunks split anywhere: mid-line, mid-field, even between the CR and
   LF of one line ending. So the parser keeps the unfinished line in a buffer
   and only acts on complete lines. */

export interface SSEMessage {
  /** The `event:` field, or "message" when the block had none. */
  event: string;
  /** All `data:` lines of one block, joined with "\n". */
  data: string;
  id?: string;
}

export interface SSEParser {
  /** Feed decoded text. Calls onMessage once per complete event block. */
  feed(chunk: string): void;
  /** End of stream: a block without its blank line is discarded, per spec. */
  end(): void;
}

export function createSSEParser(onMessage: (message: SSEMessage) => void): SSEParser {
  let buffer = "";
  // A chunk ending in "\r" may be the first half of "\r\n". Remember that so
  // a "\n" opening the next chunk isn't read as a second (blank) line.
  let pendingCR = false;
  let event = "";
  let data: string[] = [];
  let hasData = false;
  let id: string | undefined;

  function dispatch() {
    if (hasData) {
      onMessage({ event: event || "message", data: data.join("\n"), ...(id !== undefined ? { id } : {}) });
    }
    event = "";
    data = [];
    hasData = false;
  }

  function line(text: string) {
    if (text === "") return dispatch();
    if (text.startsWith(":")) return; // comment, e.g. ": keep-alive"
    const colon = text.indexOf(":");
    const field = colon === -1 ? text : text.slice(0, colon);
    let value = colon === -1 ? "" : text.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") {
      data.push(value);
      hasData = true;
    } else if (field === "id" && !value.includes("\0")) id = value;
    // "retry" and unknown fields are ignored.
  }

  return {
    feed(chunk) {
      if (!chunk) return;
      if (pendingCR && chunk.startsWith("\n")) chunk = chunk.slice(1);
      pendingCR = false;
      buffer += chunk;
      let start = 0;
      for (let i = 0; i < buffer.length; i++) {
        const ch = buffer[i];
        if (ch !== "\n" && ch !== "\r") continue;
        line(buffer.slice(start, i));
        if (ch === "\r") {
          if (i + 1 < buffer.length) {
            if (buffer[i + 1] === "\n") i++;
          } else {
            pendingCR = true;
          }
        }
        start = i + 1;
      }
      buffer = buffer.slice(start);
    },
    end() {
      buffer = "";
      pendingCR = false;
      event = "";
      data = [];
      hasData = false;
    },
  };
}

/** Reads a fetch body to the end, calling onMessage per event. Throws
    AbortError when the signal aborts, like fetch itself. */
export async function readSSE(body: ReadableStream<Uint8Array>, onMessage: (message: SSEMessage) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = createSSEParser(onMessage);
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    parser.end();
  } finally {
    reader.releaseLock();
  }
}

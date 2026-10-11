"use client";

import { useEffect, useState } from "react";
import { useReducedMotion } from "./QwenDraft";

/* Types a phrase, holds it under a blinking caret, deletes it and types the
 * next, round and round: the banner asks each of the questions the console
 * answers, one after another.
 *
 * ACCESSIBILITY CONTRACT: the output is aria-hidden, because a heading whose
 * text changes every few seconds would be re-read or read half-typed. The
 * caller MUST give the wrapping element a fixed accessible name —
 * `aria-label={phrases[0]}` on the heading. Reduced motion shows the first
 * phrase, settled, and never cycles.
 */

const START_MS = 140;
const TYPE_MS = 55;
const DELETE_MS = 28;
/** Long enough to read the phrase twice at a glance. */
const HOLD_MS = 2600;
const GAP_MS = 380;

export function TypeCycle({ phrases }: { phrases: readonly string[] }) {
  const reduced = useReducedMotion();
  const [text, setText] = useState("");
  /** While characters are landing the caret holds solid, as a real one does. */
  const [typing, setTyping] = useState(true);

  useEffect(() => {
    if (reduced) return;
    let phrase = 0;
    let length = 0;
    let deleting = false;
    let timer: ReturnType<typeof setTimeout>;

    const tick = () => {
      const current = phrases[phrase];
      length += deleting ? -1 : 1;
      setText(current.slice(0, length));
      setTyping(true);
      if (!deleting && length === current.length) {
        deleting = true;
        setTyping(false);
        timer = setTimeout(tick, HOLD_MS);
      } else if (deleting && length === 0) {
        deleting = false;
        phrase = (phrase + 1) % phrases.length;
        setTyping(false);
        timer = setTimeout(tick, GAP_MS);
      } else {
        timer = setTimeout(tick, deleting ? DELETE_MS : TYPE_MS);
      }
    };

    timer = setTimeout(tick, START_MS);
    return () => clearTimeout(timer);
  }, [phrases, reduced]);

  return (
    <span aria-hidden="true">
      {reduced ? phrases[0] : text}
      {!reduced && <span className="type-caret" data-typing={typing ? "" : undefined} />}
    </span>
  );
}

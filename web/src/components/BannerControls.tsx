"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, RotateCcw } from "lucide-react";

/* The classes that carry a load animation. Listed explicitly rather than
   matched on a prefix so a future `.animate-*` utility can't get swept up. */
const ANIMATED = ".anim-rise, .anim-float, .anim-type, .anim-caret";

/**
 * Restart every load animation on the page.
 *
 * Removing and re-adding the class is the restart that survives this
 * codebase's details: the stagger lives in inline `animation-delay` (set per
 * character by TypeIn and per panel by Panel), and anything that touches the
 * `animation` shorthand — the other common restart trick — would wipe those
 * delays and collapse the cascade into one flat pop.
 *
 * Classes come off every element first, then one forced reflow commits the
 * removal, then they go back on. Batching that way costs one layout instead
 * of one per element, and no paint happens in between, so there is no flash
 * of the un-animated state.
 */
function replayAnimations() {
  const els = Array.from(document.querySelectorAll<HTMLElement>(ANIMATED));
  const classes = els.map((el) =>
    Array.from(el.classList).filter((c) => c.startsWith("anim-")),
  );

  els.forEach((el, i) => el.classList.remove(...classes[i]));
  void document.documentElement.offsetWidth; // one reflow for the whole page
  els.forEach((el, i) => el.classList.add(...classes[i]));
}

/* "auto" is the absence of [data-hero]: nothing is pinned, so all three stops
   sweep together through eight moods on a 48s loop and the banner is never on
   a named ramp for more than an instant (the @keyframes live in globals.css,
   beside the stop values they repeat). Under prefers-reduced-motion the sweep
   is dropped and Auto falls back to the theme-aware default ramp, which shifts
   with light/dark.

   The named ramps are fixed moods and look the same in either theme — but
   "fixed" is only the palette. The two lamp bodies keep moving under every
   preset; pinning stops the colour changing, not the card. */
const HEROES = ["auto", "dawn", "noon", "dusk", "midnight"] as const;
type Hero = (typeof HEROES)[number];

const LABEL: Record<Hero, string> = {
  auto: "Auto",
  dawn: "Dawn",
  noon: "Noon",
  dusk: "Dusk",
  midnight: "Midnight",
};

/* The menu rows get a word about the ramp rather than a swatch each. A swatch
   would have to read --c-hero-*, and those resolve to whatever is *currently*
   applied — so five swatches would all paint the same gradient.
   Picking crossfades the banner behind the open menu anyway, which is a far
   better preview than a 12px chip could be. */
const HINT: Record<Hero, string> = {
  auto: "Tours all eight moods",
  dawn: "Violet into amber",
  noon: "Bright blue into gold",
  dusk: "The dark-mode default",
  midnight: "Near-monochrome navy",
};

/* Transparent fill, faint edge. These sit on artwork, so the controls are
   ghosts: no fill, a soft border and slightly dimmed ink that firm up on
   hover and focus.

   The trade-off is deliberate: white/35 sits below a 3:1 boundary over the
   lightest ramps (noon's warm end), so at rest the controls are identified
   mostly by their label and icon. Hover and focus bring the edge back to
   white/70, which clears it. */
const CHIP =
  "inline-flex h-7 cursor-pointer items-center gap-1.5 rounded border border-white/35 text-white/90 transition-colors hover:border-white/70 hover:bg-black/15 hover:text-white focus-visible:border-white/70 focus-visible:bg-black/15 focus-visible:text-white";

export function BannerControls() {
  const [hero, setHero] = useState<Hero>("auto");
  const [open, setOpen] = useState(false);

  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);

  function choose(next: Hero) {
    setHero(next);
    // The ramp is a document-level concern — the tokens live on :root — so it
    // is set imperatively rather than threaded through a provider for one
    // attribute. Absence of the attribute *is* the "auto" state.
    const root = document.documentElement;
    if (next === "auto") root.removeAttribute("data-hero");
    else root.setAttribute("data-hero", next);

    setOpen(false);
    triggerRef.current?.focus();
  }

  // Pressing anywhere else closes. pointerdown rather than click, so the menu
  // is gone before the outside element reacts to its own press.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  // Opening moves focus onto the current choice, which is where a keyboard
  // user and a screen reader both expect to land in a listbox. Selecting also
  // closes, so this never fights the focus return in `choose`.
  useEffect(() => {
    if (open) optionRefs.current[HEROES.indexOf(hero)]?.focus();
  }, [open, hero]);

  /** Roving focus inside the menu: the arrow keys move, they don't select. */
  function onMenuKeyDown(event: React.KeyboardEvent) {
    const delta =
      event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;

    if (delta !== 0) {
      event.preventDefault();
      const from = optionRefs.current.findIndex(
        (el) => el === document.activeElement,
      );
      optionRefs.current[(from + delta + HEROES.length) % HEROES.length]?.focus();
      return;
    }

    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    }
  }

  return (
    <div
      ref={rootRef}
      className="absolute top-3 right-3 flex items-center gap-1.5"
    >
      <div className="relative">
        <button
          ref={triggerRef}
          type="button"
          onClick={() => setOpen((isOpen) => !isOpen)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setOpen(true);
            }
          }}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls="hero-listbox"
          // The visible name is inside the accessible name, so the two agree
          // for anyone driving this by voice.
          aria-label={`Banner gradient: ${LABEL[hero]}`}
          className={`${CHIP} w-38 px-2`}
        >
          {/* The swatch reads the same colour properties the banner does, so it
              always shows the live ramp with nothing to keep in sync — in Auto
              that means it sweeps along with it. Colours only: the body
              coordinates stay with the banner, since a 12px circle has no room
              to show a wandering pool of light. */}
          <span
            aria-hidden="true"
            className="hero-swatch size-3 shrink-0 rounded-full border border-white/25"
          />
          <span className="flex-1 text-left text-[11px] font-medium">{LABEL[hero]}</span>
          <ChevronDown
            size={12}
            aria-hidden="true"
            className={`transition-transform duration-(--dur-fast) ${open ? "rotate-180" : ""}`}
          />
        </button>

        {open && (
          <ul
            id="hero-listbox"
            role="listbox"
            aria-label="Banner gradient"
            onKeyDown={onMenuKeyDown}
            /* The trigger is a ghost because it sits on artwork. A menu is a
               surface you read, so it gets the console's real panel tokens —
               opaque background, standard border and shadow — rather than
               more glass. It spans exactly the trigger's width (the trigger is
               sized for the longest hint), so both edges line up. */
            className="panel-shadow absolute inset-x-0 top-full z-20 mt-1.5 overflow-hidden rounded border border-line bg-surface py-1"
          >
            {HEROES.map((option, i) => {
              const selected = option === hero;
              return (
                <li key={option} role="none">
                  <button
                    ref={(el) => {
                      optionRefs.current[i] = el;
                    }}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    // Focus is managed by the listbox, so the options stay out
                    // of the page's tab order: Tab leaves the menu, it does
                    // not walk through five more stops inside it.
                    tabIndex={-1}
                    onClick={() => choose(option)}
                    className="flex w-full cursor-pointer items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-surface-2 focus-visible:bg-surface-2"
                  >
                    {/* The check holds its slot whether or not it shows, so
                        the labels don't shift sideways as the choice moves. */}
                    <Check
                      size={13}
                      aria-hidden="true"
                      // accent-ink, not accent: this amber is on the panel
                      // surface, which is what that token is solved for.
                      className={`shrink-0 text-accent-ink ${selected ? "" : "invisible"}`}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium text-ink">
                        {LABEL[option]}
                      </span>
                      <span className="block truncate text-[11px] text-muted">
                        {HINT[option]}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <button
        type="button"
        onClick={replayAnimations}
        aria-label="Replay the intro animation"
        title="Replay animation"
        className={`${CHIP} w-7 justify-center`}
      >
        <RotateCcw size={13} aria-hidden="true" />
      </button>
    </div>
  );
}

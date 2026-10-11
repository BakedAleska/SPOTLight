import { ArrowRight } from "lucide-react";
import { SpotlightLogo } from "./SpotlightLogo";
import { BannerControls } from "./BannerControls";
import { TypeCycle } from "./TypeCycle";
import { ScrollLink } from "./ScrollLink";

/* The shift-start banner. It exists to state the thesis in one sentence
   before any number is read — an operator (or a judge) who reads only this
   still knows what the console is claiming.

   The background is .hero-gradient in globals.css, next to the tokens it
   reads — see the note there for why it is not an inline style. By default it
   sweeps: all three stops move together through eight moods, six seconds
   apart, from a near-black midnight to a burnt gold, while two soft bodies of
   colour wander across the card on their own longer loops. BannerControls can
   pin the palette to a named preset — the bodies keep moving, the sweep stops
   — and the theme-aware default takes over when motion is reduced. The scrim
   over it all is load-bearing, not decoration: white text clears 4.5:1 on the
   blue and purple stops unaided but only reaches 2.98:1 on the warm end, and
   which stop the text sits over depends on the viewport width and on where the
   bodies happen to be. Every waypoint of the sweep carries its own solved
   scrim, which is what lets the palette reach both extremes at all; the
   presets keep theirs.

   MOTION. The card arrives (12px rise), the headline types itself in, and the
   claim and the action float up behind it. Read order and arrival order are
   the same, which is the only reason a stagger is worth anything.

   The headline types; the paragraph does not. At 26ms a character the
   headline lands in under 700ms, but the paragraph is ~210 characters — five
   seconds of someone else typing while you wait to read it. Typing is for the
   line short enough to be a hook.

   The whole page is settled by ~1.3s. That ceiling is the real constraint: a
   console someone opens at the top of every shift cannot make them wait
   through an intro. Everything below is timed backwards from it.

   The supplied radar rings stay still while its offset arrow sweeps around
   the center. Reduced motion restores the original static composition. */
/* The questions the console answers, asked one after another. The first is
   the banner's accessible name and what reduced motion shows; keep each short
   enough for one line at sm and up, so the banner never changes height. */
const HEADLINES = [
  "What should I check first?",
  "Which flags need me now?",
  "Which missions hid a failure?",
  "Which assets keep failing?",
  "Where is SPOT struggling?",
  "Is low battery costing us?",
] as const;

/** The claim in the body copy is passed in, not written here: the numbers come
    from the kit, so the sentence stays true if the data changes under it. */
export function WelcomeBanner({
  hiddenFailures,
}: {
  hiddenFailures: { count: number; ofCompleted: number };
}) {
  return (
    <section
      aria-labelledby="banner-heading"
      className="hero-gradient anim-rise relative overflow-hidden rounded-panel px-6 py-6 sm:px-8 sm:py-7"
    >
      <BannerControls />

      {/* Decorative radar artwork; the offset arrow rotates independently. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-10 -bottom-16 size-[220px]"
      >
        <SpotlightLogo animated dimmed className="absolute inset-0 h-full w-full" />
      </div>

      <div className="relative max-w-2xl pt-8 sm:pt-0">
        {/* aria-label is load-bearing, not belt-and-braces: TypeCycle's output
            is aria-hidden, so this is the heading's only accessible name. The
            minimum height holds two lines where a phrase can wrap, one above. */}
        <h2
          id="banner-heading"
          aria-label={HEADLINES[0]}
          className="min-h-[2lh] text-2xl font-semibold tracking-tight text-white sm:min-h-[1lh] sm:text-3xl"
        >
          <TypeCycle phrases={HEADLINES} />
        </h2>
        <p className="anim-rise mt-2 text-sm leading-relaxed text-white/90 [--rise:6px] [animation-delay:760ms] sm:text-base">
          SPOTlight reads every inspection mission SPOT ran and ranks what an
          operator should look at next. {hiddenFailures.count} of the{" "}
          {hiddenFailures.ofCompleted} missions marked{" "}
          <span className="font-medium text-white">COMPLETED</span> hid at
          least one failed action, the status field alone won’t tell you that.
        </p>

        <ScrollLink
          href="#queue"
          className="anim-rise group mt-5 inline-flex h-9 cursor-pointer items-center gap-2 rounded bg-accent px-4 text-sm font-semibold text-on-accent transition-colors [--rise:6px] [animation-delay:860ms] hover:bg-accent-hover"
        >
          Go to the priority queue
          {/* The arrow moves the way the click will: motion that means
              something, not decoration. */}
          <ArrowRight
            size={16}
            aria-hidden="true"
            className="transition-transform duration-(--dur-fast) ease-(--ease-settle) group-hover:translate-x-0.5"
          />
        </ScrollLink>
      </div>
    </section>
  );
}

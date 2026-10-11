"use client";

import { useRef, useState } from "react";
import Image from "next/image";
import {
  Crosshair,
  Maximize2,
  Minus,
  Plus,
  ScanText,
  Upload,
} from "lucide-react";
import { Panel } from "./Panel";
import { SeverityBadge } from "./SeverityBadge";
import { VisionLanes } from "./VisionLanes";
import type { InspectionImage, ModelLane, OcrLine, VisionReading } from "@/lib/types";

/* ─────────────────────────────────────────────────────────────────────────────
   The inspection image viewer.

   One image at a time, at whatever magnification you need, with every number
   the readers computed shown *where on the frame it was measured*. That link
   is the point of the page: a value in a table is a claim, and a value tied
   to a box on the pixels it came from is evidence. Hovering a reading lights
   its mark and vice versa, so checking one against the other costs no
   clicking.

   CLIENT COMPONENT, deliberately and minimally. Zoom, pan, selection and the
   hover link are all local interaction state with no server involvement; the
   analyses arrive as props, already mapped by @/lib/vision, so this file
   knows nothing about the vision contract or where the numbers came from.
   ───────────────────────────────────────────────────────────────────────── */

/* Mark colours are hardcoded, and that is not an oversight about tokens. Every
   other colour in this app sits on a known surface, so it can be a semantic
   token that resolves per theme. These sit on a photograph — a thermal frame
   that is mostly orange, a grey pipe gallery — where the theme says nothing
   about what will read. Cyan and rose clear both, and each mark carries a dark
   halo so it survives a light background too. */
const MARK = {
  high: "#22d3ee",
  low: "#fb7185",
  ocr: "#a78bfa",
  active: "#ffffff",
};

/* Fixed stops rather than a continuous slider: an operator zooming into a
   gauge wants "closer", not a number, and discrete steps make the + and −
   buttons predictable. 6× on a 245px-wide kit sample is already well past the
   point where the JPEG runs out of detail, which is itself worth seeing. */
const ZOOM_STOPS = [1, 1.5, 2, 3, 4, 6];

/** What a reader caveat means, in one line. These are the flags the vision
    contract documents; an unknown one still renders as a chip, just without a
    tooltip, so a new reader flag degrades to "shown but unexplained". */
const FLAG_HELP: Record<string, string> = {
  CLIPPED: "The hotspot reaches the top of the colour scale, so this value is a lower bound — the true one may be higher.",
  AMBIGUOUS_NEEDLE: "Two needle candidates fit the dial. The reader will not pick between them, so it reports no value.",
  DEFAULT_SWEEP: "No scale labels could be read, so a default 270° sweep was assumed. Treat the value as indicative.",
  USED_HINT: "The dial or colorbar labels were unreadable; a per-asset range hint was used instead.",
  RELATIVE_ONLY: "A spectrogram image carries no absolute dB scale. These are relative levels only.",
  LABEL_OUTLIER: "An OCR'd scale label did not fit the fitted axis and was dropped from the calibration.",
  WEAK_NEEDLE: "The needle contrast against the dial face was low.",
  NEEDLE_OUTSIDE_SCALE: "The needle points outside the calibrated range of the dial.",
  LOW_PALETTE_MATCH: "Many pixels did not match any colour on the scale bar, so the temperature map is partial.",
  MISSING_VALUE: "A Key Results label was found with no value beside it — usually something occluding the table.",
  NO_KEY_RESULTS: "No Key Results table was found on this screenshot.",
  NO_TEXT: "OCR found no text on this image. Expected on a plain photograph.",
  READER_FAILED: "The reader raised an error. Any OCR text below still stands; there are no computed values.",
};

/** Deterministic, locale-free, and short enough to sit in a column of
    figures: 100, 81.5, 0.634. `toLocaleString` would risk a server/client
    mismatch on a page that server-renders. */
const fmt = (value: number) => String(Math.round(value * 1000) / 1000);

const pct = (value: number) => `${Math.round(value * 100)}%`;

// ── Small shared pieces ──────────────────────────────────────────────────────

function Chip({
  children,
  title,
  tone = "",
}: {
  children: React.ReactNode;
  title?: string;
  tone?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-sm border border-line bg-surface-3 px-1.5 py-0.5 font-mono text-[11px] leading-none text-muted ${
        title ? "cursor-help" : ""
      } ${tone}`}
    >
      {children}
    </span>
  );
}

/** Toolbar buttons and the filmstrip's upload slot share one shape, so the
    strip reads as one control group rather than a row of different buttons. */
const TOOL =
  "inline-flex h-7 cursor-pointer items-center gap-1.5 rounded border border-line px-2 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-45";

function ToolButton({
  active,
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      className={`${TOOL} ${
        active
          ? "border-line-strong bg-surface-2 font-medium text-ink"
          : "text-muted hover:bg-surface-2 hover:text-ink"
      }`}
      {...props}
    >
      {children}
    </button>
  );
}

// ── The frame ────────────────────────────────────────────────────────────────

/**
 * One mark, positioned in percentages of the image's own pixel grid.
 *
 * HTML and not SVG, because of zoom. The readings carry `bbox`/`point` in
 * original image pixels, which maps to a percentage of the frame no matter
 * how large it is drawn — and because the frame is scaled by a CSS transform,
 * anything with a px size inside it (a border, a label) grows with the image.
 * A 2px outline at 6× is a 12px slab across a dial needle. Dividing those by
 * the zoom holds every piece of annotation at a constant on-screen size,
 * which is what an annotation is for. An SVG overlay would need the frame's
 * measured width to do the same arithmetic in user units.
 */
function Mark({
  reading,
  image,
  zoom,
  active,
  onActivate,
}: {
  reading: VisionReading;
  image: InspectionImage;
  zoom: number;
  active: boolean;
  onActivate: (id: string | null) => void;
}) {
  const colour = active ? MARK.active : reading.trust === "HIGH" ? MARK.high : MARK.low;
  const width = (active ? 2.5 : 1.5) / zoom;

  const box = reading.bbox;
  const point = reading.point;
  if (!box && !point) return null;

  const [x0, y0] = box ? [box[0], box[1]] : [point![0], point![1]];

  return (
    <div
      onPointerEnter={() => onActivate(reading.id)}
      onPointerLeave={() => onActivate(null)}
      /* inset-0, not a zero-size anchor: every child below is positioned as a
         percentage, and a percentage of nothing is nothing. */
      className="pointer-events-none absolute inset-0"
    >
      {box && (
        <div
          className="pointer-events-auto absolute"
          style={{
            left: `${(box[0] / image.width) * 100}%`,
            top: `${(box[1] / image.height) * 100}%`,
            width: `${((box[2] - box[0]) / image.width) * 100}%`,
            height: `${((box[3] - box[1]) / image.height) * 100}%`,
            border: `${width}px solid ${colour}`,
            // A dark halo outside the stroke, so the mark survives landing on
            // a pale region of the image without a second colour decision.
            boxShadow: `0 0 0 ${width}px rgb(0 0 0 / 0.45)`,
          }}
        />
      )}

      {point && (
        <div
          className="pointer-events-auto absolute rounded-full"
          style={{
            left: `${(point[0] / image.width) * 100}%`,
            top: `${(point[1] / image.height) * 100}%`,
            width: `${14 / zoom}px`,
            height: `${14 / zoom}px`,
            marginLeft: `${-7 / zoom}px`,
            marginTop: `${-7 / zoom}px`,
            border: `${width}px solid ${colour}`,
            boxShadow: `0 0 0 ${width}px rgb(0 0 0 / 0.45)`,
          }}
        />
      )}

      {/* The V# label rides above its mark at a constant size. `scale(1/zoom)`
          with a bottom-left origin keeps it anchored to the same pixel of the
          image however far in you are. */}
      <div
        className="pointer-events-none absolute font-mono whitespace-nowrap"
        style={{
          left: `${(x0 / image.width) * 100}%`,
          top: `${(y0 / image.height) * 100}%`,
          transform: `scale(${1 / zoom}) translate(0, -118%)`,
          transformOrigin: "left top",
          background: "rgb(8 10 14 / 0.82)",
          color: colour,
          border: `1px solid ${colour}`,
          borderRadius: 2,
          padding: "1px 4px",
          fontSize: 11,
          lineHeight: 1.3,
        }}
      >
        {reading.id}
        {reading.value !== null && (
          <span className="text-white">
            {" "}
            {fmt(reading.value)} {reading.unit}
          </span>
        )}
      </div>
    </div>
  );
}

function OcrBox({ line, image, zoom }: { line: OcrLine; image: InspectionImage; zoom: number }) {
  return (
    <div
      title={`${line.text} · OCR confidence ${pct(line.conf)}`}
      className="pointer-events-none absolute"
      style={{
        left: `${(line.bbox[0] / image.width) * 100}%`,
        top: `${(line.bbox[1] / image.height) * 100}%`,
        width: `${((line.bbox[2] - line.bbox[0]) / image.width) * 100}%`,
        height: `${((line.bbox[3] - line.bbox[1]) / image.height) * 100}%`,
        border: `${1 / zoom}px dashed ${MARK.ocr}`,
      }}
    />
  );
}

// ── The readings column ──────────────────────────────────────────────────────

function TrustBadge({ trust }: { trust: VisionReading["trust"] }) {
  return trust === "HIGH" ? (
    <Chip tone="text-ok" title="Confidence is above the 0.5 cut, so System 2 may state this value and cite it.">
      HIGH TRUST
    </Chip>
  ) : (
    <Chip
      tone="text-warn"
      title="Below the confidence cut, or the reader could not tell which value it saw. System 2 receives this row with no value at all, so the number can never be quoted."
    >
      LOW TRUST
    </Chip>
  );
}

function ReadingCard({
  reading,
  active,
  onActivate,
}: {
  reading: VisionReading;
  active: boolean;
  onActivate: (id: string | null) => void;
}) {
  const locatable = Boolean(reading.bbox || reading.point);

  return (
    <li
      onPointerEnter={() => onActivate(reading.id)}
      onPointerLeave={() => onActivate(null)}
      onFocus={() => onActivate(reading.id)}
      onBlur={() => onActivate(null)}
      tabIndex={0}
      className={`border-b border-line px-4 py-3 transition-colors last:border-0 ${
        active ? "bg-surface-2" : ""
      }`}
    >
      <div className="flex items-baseline gap-2">
        {/* The V# is the evidence ID System 2 has to cite, so it is set as an
            identifier rather than as a row number. */}
        <span
          className="font-mono text-xs font-semibold"
          style={{ color: reading.trust === "HIGH" ? MARK.high : MARK.low }}
        >
          {reading.id}
        </span>
        <span className="min-w-0 truncate text-sm font-medium text-ink">{reading.label}</span>
        <span className="ml-auto shrink-0">
          <TrustBadge trust={reading.trust} />
        </span>
      </div>

      <p className="nums mt-1 font-mono text-2xl leading-none font-semibold text-ink">
        {reading.value === null ? (
          <span className="text-lg font-normal text-muted">no value reported</span>
        ) : (
          <>
            {fmt(reading.value)}
            {reading.unit && (
              <span className="ml-1 text-base font-normal text-muted">{reading.unit}</span>
            )}
          </>
        )}
      </p>

      {/* Confidence as a bar and a number: the bar is for scanning six rows at
          once, the number is for quoting. */}
      <div className="mt-2 flex items-center gap-2">
        <div className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-2">
          <div
            className={`h-full rounded-full ${reading.trust === "HIGH" ? "bg-ok" : "bg-warn"}`}
            style={{ width: pct(reading.confidence) }}
          />
        </div>
        <span className="nums shrink-0 font-mono text-xs text-muted">
          {pct(reading.confidence)} confidence
        </span>
      </div>

      <div className="mt-2 flex flex-wrap gap-1">
        <Chip title="How this value was computed — the audit trail in one word.">{reading.method}</Chip>
        {reading.flags.map((flag) => (
          <Chip key={flag} tone="text-warn" title={FLAG_HELP[flag]}>
            {flag}
          </Chip>
        ))}
        {!locatable && (
          <Chip title="This value summarises the whole frame, so there is nothing to box on it.">
            whole frame
          </Chip>
        )}
      </div>

      {reading.note && <p className="mt-2 text-xs leading-snug text-muted">{reading.note}</p>}
    </li>
  );
}

// ── The viewer ───────────────────────────────────────────────────────────────

export function InspectionViewer({
  images,
  lanes,
}: {
  images: InspectionImage[];
  lanes: ModelLane[];
}) {
  const [selected, setSelected] = useState(0);
  const [layer, setLayer] = useState<"raw" | "annotated">("raw");
  const [marks, setMarks] = useState(true);
  const [ocrBoxes, setOcrBoxes] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [active, setActive] = useState<string | null>(null);

  const viewport = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; from: { x: number; y: number }; at: { x: number; y: number } } | null>(null);

  const image = images[selected];

  /** Past the frame's edge there is nothing to look at, so panning stops at
      the point where the image still covers the viewport. At 1× that pins it
      to centre, which is why the drag handler ignores zoom 1 entirely. */
  function clamp(next: { x: number; y: number }, at: number) {
    const box = viewport.current?.getBoundingClientRect();
    if (!box) return { x: 0, y: 0 };
    const maxX = (box.width * (at - 1)) / 2;
    const maxY = (box.height * (at - 1)) / 2;
    return {
      x: Math.min(maxX, Math.max(-maxX, next.x)),
      y: Math.min(maxY, Math.max(-maxY, next.y)),
    };
  }

  function zoomTo(next: number) {
    const at = Math.min(ZOOM_STOPS[ZOOM_STOPS.length - 1], Math.max(1, next));
    setZoom(at);
    // Re-clamp rather than keep the offset: zooming back out with a stale pan
    // would leave the image hanging off one edge against a blank gutter.
    setPan((current) => clamp(current, at));
  }

  function step(direction: 1 | -1) {
    const index = ZOOM_STOPS.indexOf(zoom);
    const from = index === -1 ? 0 : index;
    zoomTo(ZOOM_STOPS[Math.min(ZOOM_STOPS.length - 1, Math.max(0, from + direction))]);
  }

  function reset() {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }

  function select(index: number) {
    setSelected(index);
    setActive(null);
    // A new image at the previous image's magnification and offset would open
    // on an arbitrary crop of something you have not seen yet.
    reset();
  }

  /* The annotated JPEG already has the service's own marks burned into it, so
     drawing ours on top would double every box. Deriving it here rather than
     clearing the toggle means switching back to the raw layer restores
     whatever the operator had chosen. */
  const showMarks = marks && layer === "raw";
  const showOcr = ocrBoxes && layer === "raw";
  const src = layer === "annotated" && image.annotated ? image.annotated : image.src;

  return (
    <div className="panel-columns grid gap-4 xl:grid-cols-12">
      <div className="panel-stack flex min-h-0 flex-col gap-4 xl:col-span-8">
        {/* ── Filmstrip ─────────────────────────────────────────────────── */}
        <Panel
          title="Inspection images"
          aside={
            <button
              type="button"
              disabled
              title="An operator's own photo is a `linked` image: it gets compared with the asset's logged reading. That upload goes through the backend, and the route isn't built yet."
              className={`${TOOL} text-faint`}
            >
              <Upload size={13} aria-hidden="true" />
              Upload
            </button>
          }
          bodyClassName="p-3"
        >
          {/* A grid rather than a scroller. Six is the whole set, and a strip
              that hides its last thumbnail behind an overflow edge makes the
              catalogue look longer than it is — the one thing a filmstrip is
              supposed to settle at a glance. */}
          <ul className="grid grid-cols-3 gap-2 sm:grid-cols-6">
            {images.map((item, index) => (
              <li key={item.mediaId} className="min-w-0">
                <button
                  type="button"
                  onClick={() => select(index)}
                  aria-current={index === selected ? "true" : undefined}
                  className={`block w-full cursor-pointer rounded border p-1 text-left transition-colors ${
                    index === selected
                      ? "border-accent bg-surface-2"
                      : "border-line hover:border-line-strong hover:bg-surface-2"
                  }`}
                >
                  <span className="relative block aspect-[3/2] overflow-hidden rounded-sm bg-surface-3">
                    <Image
                      src={item.src}
                      alt=""
                      fill
                      unoptimized
                      sizes="(min-width: 640px) 12vw, 30vw"
                      className="object-cover"
                    />
                  </span>
                  <span className="mt-1.5 block truncate text-xs font-medium text-ink">
                    {item.title}
                  </span>
                  <span className="nums mt-0.5 flex items-baseline gap-1.5 overflow-hidden font-mono text-[11px] whitespace-nowrap text-muted">
                    <span className="truncate text-accent-ink">{item.kind}</span>
                    <span className="shrink-0">
                      {item.readings.length === 0
                        ? "no values"
                        : `${item.readings.length} value${item.readings.length === 1 ? "" : "s"}`}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </Panel>

        {/* ── Viewer ────────────────────────────────────────────────────── */}
        <Panel
          title={image.title}
          aside={
            <span className="nums">
              {image.width}×{image.height} px
            </span>
          }
          bodyClassName="flex flex-col gap-3 p-3"
        >
          <div className="flex flex-wrap items-center gap-2">
            {/* Which pixels you are looking at. The annotated layer is not a
                nicer rendering of the same thing — it is the exact JPEG the
                analyst model receives, marks and all, which is worth being
                able to see. */}
            <div className="flex gap-1" role="group" aria-label="Image layer">
              <ToolButton active={layer === "raw"} onClick={() => setLayer("raw")}>
                Raw
              </ToolButton>
              <ToolButton
                active={layer === "annotated"}
                disabled={!image.annotated}
                onClick={() => setLayer("annotated")}
                title="The service's own annotated JPEG — the bytes SPOTbot is sent."
              >
                As sent to SPOTbot
              </ToolButton>
            </div>

            <div className="flex gap-1" role="group" aria-label="Overlays">
              <ToolButton
                active={showMarks}
                disabled={layer !== "raw" || image.readings.length === 0}
                onClick={() => setMarks((on) => !on)}
                title={
                  layer !== "raw"
                    ? "The annotated JPEG already carries these marks."
                    : "Box each V# reading where it was measured."
                }
              >
                <Crosshair size={13} aria-hidden="true" />
                V# marks
              </ToolButton>
              <ToolButton
                active={showOcr}
                disabled={layer !== "raw" || image.ocr.length === 0}
                onClick={() => setOcrBoxes((on) => !on)}
                title="Every line OCR found. Context only — never evidence."
              >
                <ScanText size={13} aria-hidden="true" />
                OCR boxes
              </ToolButton>
            </div>

            <div className="ml-auto flex items-center gap-1" role="group" aria-label="Zoom">
              <ToolButton onClick={() => step(-1)} disabled={zoom <= 1} aria-label="Zoom out">
                <Minus size={13} aria-hidden="true" />
              </ToolButton>
              <span className="nums w-10 text-center font-mono text-xs text-muted">
                {zoom}×
              </span>
              <ToolButton
                onClick={() => step(1)}
                disabled={zoom >= ZOOM_STOPS[ZOOM_STOPS.length - 1]}
                aria-label="Zoom in"
              >
                <Plus size={13} aria-hidden="true" />
              </ToolButton>
              <ToolButton onClick={reset} disabled={zoom === 1 && pan.x === 0 && pan.y === 0}>
                <Maximize2 size={13} aria-hidden="true" />
                Fit
              </ToolButton>
            </div>
          </div>

          {/* The viewport carries the image's own aspect ratio, so the frame
              inside it is exactly the image box — which is what lets every
              mark be positioned as a plain percentage with no letterboxing
              arithmetic. The height cap is expressed as a max-width through
              the ratio, because a max-height against a full-width box would
              simply override the ratio and reintroduce the gutters. */}
          <div
            ref={viewport}
            role="group"
            aria-label={`${image.title}, ${image.kind} reader`}
            onPointerDown={(e) => {
              if (zoom <= 1) return;
              e.currentTarget.setPointerCapture(e.pointerId);
              drag.current = { id: e.pointerId, from: pan, at: { x: e.clientX, y: e.clientY } };
            }}
            onPointerMove={(e) => {
              const d = drag.current;
              if (!d || d.id !== e.pointerId) return;
              setPan(
                clamp(
                  { x: d.from.x + (e.clientX - d.at.x), y: d.from.y + (e.clientY - d.at.y) },
                  zoom,
                ),
              );
            }}
            onPointerUp={(e) => {
              if (drag.current?.id === e.pointerId) drag.current = null;
            }}
            onPointerCancel={() => {
              drag.current = null;
            }}
            onDoubleClick={() => (zoom === 1 ? zoomTo(3) : reset())}
            style={{
              aspectRatio: `${image.width} / ${image.height}`,
              maxWidth: `calc(58vh * ${image.width / image.height})`,
              cursor: zoom > 1 ? "grab" : "zoom-in",
            }}
            className="relative mx-auto w-full touch-none overflow-hidden rounded border border-line bg-surface-3"
          >
            <div
              className="absolute inset-0 origin-center"
              style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}
            >
              <Image
                key={src}
                src={src}
                alt={`${image.title} — ${image.kind} inspection frame`}
                fill
                unoptimized
                priority
                draggable={false}
                sizes="(min-width: 1280px) 60vw, 100vw"
                className="select-none"
              />

              {showMarks &&
                image.readings.map((reading) => (
                  <Mark
                    key={reading.id}
                    reading={reading}
                    image={image}
                    zoom={zoom}
                    active={active === reading.id}
                    onActivate={setActive}
                  />
                ))}

              {showOcr &&
                image.ocr.map((line, i) => (
                  <OcrBox key={`${line.text}-${i}`} line={line} image={image} zoom={zoom} />
                ))}
            </div>
          </div>

          <p className="text-xs leading-snug text-muted">
            Routed to the <span className="font-mono text-accent-ink">{image.kind}</span> reader by{" "}
            <span className="font-mono">{image.router.method}</span>
            {image.actionType ? (
              <>
                {" "}
                — the orchestrator pairs this sample with{" "}
                <span className="font-mono">{image.actionType}</span> actions.
              </>
            ) : (
              <> — the orchestrator pairs no action type with this sample.</>
            )}{" "}
            {zoom > 1 && "Drag to pan. "}
            Double-click to {zoom === 1 ? "zoom in" : "fit"}.
          </p>
        </Panel>

        <VisionLanes lanes={lanes} />
      </div>

      {/* ── Analysis ─────────────────────────────────────────────────────── */}
      <div className="panel-stack flex min-h-0 flex-col gap-4 xl:col-span-4">
        {image.alert && (
          <Panel title="What this image alone would raise">
            <div className="flex items-baseline gap-3">
              <SeverityBadge severity={image.alert.severity} />
              <span className="font-mono text-xs text-muted">{image.alert.source}</span>
            </div>
            <p className="mt-1.5 leading-snug font-medium text-ink">{image.alert.title}</p>
            <p className="mt-2 text-xs leading-snug text-muted">
              It raises nothing by itself. This is a kit sample paired by action type, not a photo
              of any asset in the database, so the orchestrator files its alert as{" "}
              <span className="font-mono">demo_alert</span> and never escalates an asset on it.
            </p>
          </Panel>
        )}

        <Panel
          title="Readings"
          className="max-h-[32rem]"
          aside={
            image.readings.length > 0
              ? `${image.readings.filter((r) => r.trust === "HIGH").length} of ${image.readings.length} citable`
              : "none"
          }
          bodyClassName="overflow-y-auto"
        >
          {image.readings.length === 0 ? (
            <p className="px-4 py-6 text-sm leading-snug text-muted">
              Nothing to compute. A plain photograph carries no scale, colorbar or dial, so the{" "}
              <span className="font-mono">visual</span> reader runs OCR and stops. A model may
              describe what it sees here, but no number can be derived from it — and the page would
              rather show none than one nobody can defend.
            </p>
          ) : (
            <ul>
              {image.readings.map((reading) => (
                <ReadingCard
                  key={reading.id}
                  reading={reading}
                  active={active === reading.id}
                  onActivate={setActive}
                />
              ))}
            </ul>
          )}
        </Panel>

        {image.keyResults.length > 0 && (
          <Panel title="Key results" aside="Orbit format" bodyClassName="">
            <dl className="divide-y divide-line">
              {image.keyResults.map((row) => (
                <div key={row.name} className="flex items-baseline gap-3 px-4 py-1.5">
                  <dt className="min-w-0 flex-1 truncate text-sm text-muted">{row.name}</dt>
                  <dd className="nums shrink-0 font-mono text-sm text-ink">
                    {row.value}
                    {row.units && <span className="ml-1 text-muted">{row.units}</span>}
                  </dd>
                </div>
              ))}
            </dl>
          </Panel>
        )}

        <Panel
          title="Text on the image"
          aside={`${image.ocr.length} line${image.ocr.length === 1 ? "" : "s"}`}
        >
          <p className="text-xs leading-snug text-muted">
            RapidOCR, verbatim. Context only: a number that appears <em>only</em> here is not
            evidence and System&nbsp;2 cannot state it — a reading has to come from a V# row.
            {/* The kit's gauge is printed wrong, which is the most useful thing
                on this page: it is exactly the case where a model reading the
                text would confidently state a number that is not there. Said
                only on that image, because it is a fact about that image. */}
            {image.kind === "gauge" && (
              <>
                {" "}
                This dial is printed <span className="font-mono">290</span> where 250 belongs. The
                reader passes the misprint on uncorrected and leaves the value unreported, rather
                than calibrating a scale against a label that cannot be right.
              </>
            )}
          </p>
          {image.ocr.length === 0 ? (
            <p className="mt-3 text-sm text-muted">
              No text found{image.flags.includes("NO_TEXT") ? " — flagged NO_TEXT" : ""}.
            </p>
          ) : (
            <ul className="mt-3 flex flex-wrap gap-1.5">
              {image.ocr.map((line, i) => (
                <li key={`${line.text}-${i}`}>
                  <Chip
                    title={`OCR confidence ${pct(line.conf)}`}
                    tone={line.conf < 0.8 ? "text-warn" : "text-ink"}
                  >
                    {line.text}
                  </Chip>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}

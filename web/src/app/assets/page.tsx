import { Suspense } from "react";
import { Images, TriangleAlert } from "lucide-react";
import { InspectionViewer } from "@/components/InspectionViewer";
import { LoadingIndicator } from "@/components/LoadingIndicator";
import { Panel } from "@/components/Panel";
import { getInspectionBoard } from "@/lib/vision";

/* Asset imagery: what SPOT's camera saw, and what was read off it.

   The Overview answers "do I need to look at anything?". This page is the
   looking. Everything on it comes from one `POST /v1/vision` response per
   image (`docs/vision-contract.md`), mapped in @/lib/vision — the V# values,
   where on the frame each was measured, the OCR lines, and the reader's own
   caveats. No model produced any number here, and the page says so rather
   than leaving it to be assumed. */

function Provenance({ source }: { source: "sample" | "live" }) {
  /* Where the numbers came from, stated before any of them are read. The
     distinction matters more here than anywhere else in the console: an image
     analysis looks identical whether it was computed a second ago or baked in
     last week, and a judge is entitled to know which they are looking at. */
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-panel border border-line bg-surface-2 px-4 py-3"
    >
      <Images size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-accent-ink" />
      <div className="min-w-0">
        <p className="text-sm font-medium text-ink">
          {source === "live"
            ? "Analysed on this request, through the backend."
            : "The challenge kit’s six sample images, really analysed — just not on this request."}
        </p>
        <p className="mt-0.5 text-xs leading-snug text-muted">
          {source === "live" ? (
            <>
              Each frame was sent to the vision service for this page load, so the values below are
              this run’s, not a recording.
            </>
          ) : (
            <>
              Every value below is a committed{" "}
              <span className="font-mono">POST /v1/vision</span> response — RapidOCR and the code
              readers over the kit’s own JPEGs, produced by{" "}
              <span className="font-mono">amd/vision/scripts/run_kit.py</span>, not written by
              hand. What is not live is the <em>call</em>: set{" "}
              <span className="font-mono">BACKEND_VISION_PATH</span> and the same page re-analyses
              every frame on each load.
            </>
          )}
        </p>
      </div>
    </div>
  );
}

function Pairing() {
  /* The one thing that must not be misread on this page. These are kit
     samples chosen by action type, not photographs of the assets in the
     database — no CSV row in the kit links to an image at all — so nothing
     here may be read as a finding about a real asset. The rule is enforced
     all the way down the stack (`research/decisions.md`, "Demo images never
     judge the asset"); stating it here is what keeps the UI honest about it
     instead of relying on the operator knowing. */
  return (
    <div className="flex items-start gap-3 rounded-panel border border-line bg-surface-2 px-4 py-3">
      <TriangleAlert size={16} aria-hidden="true" className="mt-0.5 shrink-0 text-warn" />
      <div className="min-w-0">
        <p className="text-sm font-medium text-ink">
          Illustrative imagery — <span className="font-mono">pairing: demo</span>.
        </p>
        <p className="mt-0.5 text-xs leading-snug text-muted">
          No reading in the kit links to an image, so these are paired to actions by type only. A
          demo image is never compared with an asset’s logged value, never carries{" "}
          <span className="font-mono">agrees</span> or <span className="font-mono">delta</span>,
          and can never escalate anything: a kit photo of some other pump must not flag this one.
          An operator’s own upload is a <span className="font-mono">linked</span> image and is
          compared.
        </p>
      </div>
    </div>
  );
}

async function Board() {
  const { images, lanes, source } = await getInspectionBoard();

  if (images.length === 0) {
    return (
      <Panel title="Inspection images">
        <p className="text-sm text-muted">
          No inspection imagery available. The committed sample analyses
          (<span className="font-mono">web/src/lib/vision-samples.json</span>) and the catalogue in{" "}
          <span className="font-mono">web/src/lib/vision.ts</span> have drifted apart.
        </p>
      </Panel>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Provenance source={source} />
      <Pairing />
      <InspectionViewer images={images} lanes={lanes} />
    </div>
  );
}

function BoardSkeleton() {
  return <LoadingIndicator label="Loading asset inspections…" />;
}

export default function Assets() {
  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4 p-4">
      <h1 className="sr-only">Asset inspection imagery</h1>

      <Suspense fallback={<BoardSkeleton />}>
        <Board />
      </Suspense>
    </div>
  );
}

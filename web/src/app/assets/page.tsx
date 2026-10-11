import { Suspense } from "react";
import { InspectionViewer } from "@/components/InspectionViewer";
import { LoadingIndicator } from "@/components/LoadingIndicator";
import { MediaGallery } from "@/components/MediaGallery";
import { Panel } from "@/components/Panel";
import { getInspectionBoard } from "@/lib/vision";

/* Asset imagery: what SPOT's camera saw, and what was read off it.

   The Overview answers "do I need to look at anything?". This page is the
   looking. Everything on it comes from one `POST /v1/vision` response per
   image (`docs/vision-contract.md`), mapped in @/lib/vision — the V# values,
   where on the frame each was measured, the OCR lines, and the reader's own
   caveats. No model produced any number here. Below it, the database's
   inspection images (raw.inspection_media) by type, with each asset's finding
   and the image's source (components/MediaGallery.tsx). */

async function Board() {
  const { images, lanes } = await getInspectionBoard();

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
      <Suspense fallback={<LoadingIndicator label="Loading inspection gallery…" />}>
        <MediaGallery />
      </Suspense>
    </div>
  );
}

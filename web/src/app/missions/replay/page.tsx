import { Suspense } from "react";
import { PathPreview } from "@/components/PathPreview";
import { getPathData } from "@/lib/path-data";
type Props = { searchParams: Promise<{ mission?: string }> };
export default function ReplayPage({ searchParams }: Props) {
 return <Suspense fallback={<PathPreview {...getPathData()} />}><Replay searchParams={searchParams} /></Suspense>;
}
async function Replay({ searchParams }: Props) {
 const { mission } = await searchParams;
 const missionId = typeof mission === "string" ? mission : undefined;
 // Keyed by the requested mission: Next keeps visited pages mounted, so a link to another mission must remount the
 // preview rather than hand new props to state that already picked its mission.
 return <PathPreview key={missionId ?? ""} {...getPathData()} initialMissionId={missionId} />;
}

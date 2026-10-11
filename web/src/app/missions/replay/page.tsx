import { Suspense } from "react";
import { PathPreview } from "@/components/PathPreview";
import { getPathData } from "@/lib/path-data";
type Props = { searchParams: Promise<{ mission?: string }> };
export default function ReplayPage({ searchParams }: Props) {
 return <Suspense fallback={<PathPreview {...getPathData()} />}><Replay searchParams={searchParams} /></Suspense>;
}
async function Replay({ searchParams }: Props) {
 const { mission } = await searchParams;
 return <PathPreview {...getPathData()} initialMissionId={typeof mission === "string" ? mission : undefined} />;
}

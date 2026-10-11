import { PathPreview } from "@/components/PathPreview";
import { getPathData } from "@/lib/path-data";
export default async function ReplayPage({ searchParams }: { searchParams: Promise<{ mission?: string }> }) {
 const { mission } = await searchParams;
 return <PathPreview {...getPathData()} initialMissionId={typeof mission === "string" ? mission : undefined} />;
}

import { PathPreview } from "@/components/PathPreview";
import { getPathData } from "@/lib/path-data";
export default function ReplayPage() {
  return <PathPreview {...getPathData()} />;
}

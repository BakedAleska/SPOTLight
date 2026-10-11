import { backendRequest } from "./backend";

/* Inspection images stored in the database (raw.inspection_media with bytes; backend GET /assets/media), shown in
   the Assets page gallery. The demo set carries mock asset details and says so (`metadata.mock`). */

export type InspectionType = "thermal" | "gauge" | "acoustic" | "visual";

export interface MediaItem {
  media_id: string;
  media_type: string | null;
  bytes: number | null;
  captured_at: string | null;
  metadata: {
    mock?: boolean;
    inspection_type?: InspectionType;
    asset_id?: string;
    asset_name?: string;
    location_id?: string;
    location_name?: string | null;
    condition?: "normal" | "anomaly" | null;
    finding?: string | null;
    reading?: { value: number; unit: string } | null;
    source?: { dataset?: string; url?: string | null; licence?: string; credit?: string } | null;
  };
}

export const validMediaId = (id: string) => /^[\w:.-]{1,120}$/.test(id);

export const mediaSrc = (id: string) => `/api/assets/media/${encodeURIComponent(id)}`;

/** The gallery's rows, or null when the backend cannot be read (the page then shows the kit samples only). */
export async function loadMedia(): Promise<MediaItem[] | null> {
  try {
    return await backendRequest<MediaItem[]>("/assets/media", {}, 15_000);
  } catch {
    return null;
  }
}

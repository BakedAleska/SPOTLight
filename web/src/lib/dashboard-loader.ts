import { unstable_rethrow } from "next/navigation";
import { loadDashboard } from "./backend";
import type { DashboardData } from "./dashboard";

export async function getDashboard(): Promise<DashboardData> {
  try {
    return await loadDashboard();
  } catch (error) {
    unstable_rethrow(error);
    console.warn("[dashboard] Backend unavailable; serving labeled kit data.");
    const kit = await import("./analysis");
    return {
      source: "kit",
      degraded: "Backend unavailable. Live flag actions and explanations are disabled.",
      features: { flagDetails: false, flagActions: false, flagExplanations: false },
      stats: kit.stats,
      flags: kit.flags,
      flagCount: kit.flags.length,
      actionSuccess: kit.actionSuccess,
      hiddenFailures: kit.headline.hiddenFailures,
      generatedAt: new Date().toISOString(),
    };
  }
}

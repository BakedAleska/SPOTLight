import type { ActionSuccess } from "./action-success";
import type { Flag, Stat } from "./types";

export interface DashboardData {
  source: "backend" | "kit";
  degraded: string | null;
  features: { flagDetails: boolean; flagActions: boolean; flagExplanations: boolean };
  stats: Stat[];
  flags: Flag[];
  flagCount: number;
  actionSuccess: ActionSuccess;
  hiddenFailures: { count: number; ofCompleted: number };
  generatedAt: string;
}

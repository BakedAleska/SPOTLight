import type { BatteryBand, Flag, Stat } from "./types";

export interface DashboardData {
  source: "backend" | "kit";
  degraded: string | null;
  features: { flagDetails: boolean; flagActions: boolean; flagExplanations: boolean };
  stats: Stat[];
  flags: Flag[];
  flagCount: number;
  batteryBands: BatteryBand[];
  hiddenFailures: { count: number; ofCompleted: number };
  batteryCaption: string;
  aboveCliffRate: number | null;
  generatedAt: string;
}

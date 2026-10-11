import {
  Boxes,
  ChartColumn,
  LayoutDashboard,
  Route,
  Settings,
  Sparkles,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  label: string;
  icon: LucideIcon;
  /** Omitted while the route doesn't exist yet — rendered as a dead item
      rather than a link to a 404. */
  href?: string;
  children?: { label: string; href?: string }[];
}

/* The console's whole information architecture, in one list. Sections mirror
   the operator's workflow: what needs attention now → the missions behind it →
   the trends across missions. */
export const nav: NavItem[] = [
  { label: "Overview", icon: LayoutDashboard, href: "/" },
  {
    label: "Missions",
    icon: Route,
    children: [
      { label: "All missions", href: "/missions" },
      { label: "Flagged", href: "/missions/flagged" },
      { label: "Replay", href: "/missions/replay" },
    ],
  },
  { label: "Analytics", icon: ChartColumn, href: "/analytics" },
  { label: "Assets", icon: Boxes },
  { label: "AI review", icon: Sparkles, href: "/review" },
  { label: "Settings", icon: Settings, href: "/settings" },
];

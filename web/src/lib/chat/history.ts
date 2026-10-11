import type { ConversationSummary } from "./types";

export type HistoryGroupLabel = "Pinned" | "Today" | "Yesterday" | "Previous 7 days" | "Older";

export interface HistoryGroup {
  label: HistoryGroupLabel;
  items: ConversationSummary[];
}

function startOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/* Calendar days, not 24-hour windows: across a DST change a day is 23 or 25
   hours long, so step the date rather than subtracting milliseconds. */
function daysBefore(dayStart: number, n: number) {
  const date = new Date(dayStart);
  date.setDate(date.getDate() - n);
  return date.getTime();
}

/**
 * Groups chats the way ChatGPT's sidebar does, by last activity in the
 * viewer's local time: Pinned first, then Today / Yesterday / Previous 7 days
 * / Older. Empty groups are dropped; order inside a group is most recent
 * first. Unparseable dates fall into Older rather than vanishing.
 */
export function groupConversations(items: ConversationSummary[], now: Date = new Date()): HistoryGroup[] {
  const today = startOfDay(now);
  const yesterday = daysBefore(today, 1);
  const weekAgo = daysBefore(today, 7);

  const groups: Record<HistoryGroupLabel, ConversationSummary[]> = {
    Pinned: [], Today: [], Yesterday: [], "Previous 7 days": [], Older: [],
  };
  const time = (c: ConversationSummary) => {
    const t = Date.parse(c.updated_at);
    return Number.isNaN(t) ? -Infinity : t;
  };
  for (const item of [...items].sort((a, b) => time(b) - time(a))) {
    const t = time(item);
    if (item.pinned) groups.Pinned.push(item);
    else if (t >= today) groups.Today.push(item);
    else if (t >= yesterday) groups.Yesterday.push(item);
    else if (t >= weekAgo) groups["Previous 7 days"].push(item);
    else groups.Older.push(item);
  }
  return (Object.keys(groups) as HistoryGroupLabel[])
    .filter((label) => groups[label].length > 0)
    .map((label) => ({ label, items: groups[label] }));
}

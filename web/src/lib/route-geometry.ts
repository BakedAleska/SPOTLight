// Drawn route geometry over spot-blueprint.svg (1095 x 595). Presentation only: the logs record stops, not the
// robot's path, so travel between stops is the shortest walk along the blueprint's hallways and through its doors,
// taking the stairs between floors.

import { anchors } from "./waypoints";

export interface XY { x: number; y: number }
export type Leg =
  /** A walk on one floor; `travel` is the index of the stop-to-stop move it belongs to. */
  | { kind: "walk"; travel: number; points: XY[] }
  /** Taking the stairs: SPOT leaves the plan at `from` and reappears at `to` on the other floor. */
  | { kind: "climb"; travel: number; from: XY; to: XY; up: boolean };

/** Floor 2 is drawn in the right-hand panel of the blueprint. */
export const floorOf = (p: XY) => (p.x >= 751 ? 2 : 1);
export const STAIRS = { id: "Stairs", name: "Stairwell" };

/* The walkable network, traced from the blueprint: hallway centrelines, door gaps and stair ends. Every edge is
   horizontal or vertical, so routes read as clean right-angled runs; corner nodes inside rooms turn SPOT from a door
   toward its waypoint. Floor 1 hallways: north y145-175, south y330-360, west x165-195, east x600-630. Floor 2: one
   hallway y225-255 with a stair column x915-945 running south to the WP15 hall. */
const nodes: Record<string, [number, number]> = {
  ...anchors,
  // Floor 1: north hall, WP04 and WP03 off it, and the pipe gallery (entered where the hall meets its railing)
  wp04Corner: [235, 102], wp04Door: [235, 145], northHall: [235, 160], northEnd: [305, 160], galleryLane: [447, 160],
  wp03Top: [235, 175], wp03Aisle: [235, 249], wp03Bottom: [235, 330],
  // South hall
  southWest: [180, 345], southWp03: [235, 345], southWp08: [380, 345], southWp09: [551, 345], southEast: [615, 345],
  wp08Door: [380, 360], wp09Door: [551, 360],
  wp09Junction: [551, 446], stairWp09Foot: [482, 446], stairWp09: [482, 486], wp10Door: [630, 446], wp10Corner: [672, 446],
  // West hall, WP02 and its garage door, and the loading dock
  westWp02: [180, 390], westDock: [180, 420], dockEast: [180, 475],
  wp02Door: [165, 390], wp02Corner: [113, 390], garage: [113, 420], dockWest: [113, 475],
  // East hall, WP06 and WP07
  eastWp07: [615, 240], eastTop: [615, 145], wp06Corner: [615, 103],
  wp07Door: [630, 240], wp07Corner: [664, 240], stairWp07Foot: [698, 240], stairWp07: [698, 172],
  // Floor 2: the hallway, its rooms, the stair column and the WP15 hall
  wp11Door: [850, 225], hallWp11: [850, 240], hallStair: [930, 240], hallWp12: [992, 240], wp12Door: [992, 225],
  wp12Corner: [992, 158], wp13Top: [850, 255], wp13Corner: [850, 338], wp13Bottom: [850, 420], wp14Top: [992, 255],
  wp14Bottom: [992, 420], stairTop: [930, 255], landing: [930, 337], stairBottom: [930, 420],
  lane13: [850, 489], laneStair: [930, 489], lane14: [992, 489],
};
const walks: [string, string][] = [
  // Floor 1: north hall and its rooms
  ["WP04", "wp04Corner"], ["wp04Corner", "wp04Door"], ["wp04Door", "northHall"], ["northHall", "wp03Top"],
  ["northHall", "northEnd"], ["northEnd", "galleryLane"], ["galleryLane", "WP05"],
  ["wp03Top", "wp03Aisle"], ["wp03Aisle", "WP03"], ["wp03Aisle", "wp03Bottom"], ["wp03Bottom", "southWp03"],
  // South hall
  ["southWest", "southWp03"], ["southWp03", "southWp08"], ["southWp08", "southWp09"], ["southWp09", "southEast"],
  ["southWp08", "wp08Door"], ["wp08Door", "WP08"], ["southWp09", "wp09Door"], ["wp09Door", "WP09"],
  ["WP09", "wp09Junction"], ["wp09Junction", "stairWp09Foot"], ["stairWp09Foot", "stairWp09"],
  ["wp09Junction", "wp10Door"], ["wp10Door", "wp10Corner"], ["wp10Corner", "WP10"],
  // West hall, the WP02 garage door and the loading dock
  ["southWest", "westWp02"], ["westWp02", "westDock"], ["westDock", "dockEast"], ["dockEast", "WP01"],
  ["westWp02", "wp02Door"], ["wp02Door", "wp02Corner"], ["wp02Corner", "WP02"],
  ["wp02Corner", "garage"], ["garage", "dockWest"], ["dockWest", "WP01"],
  // East hall
  ["southEast", "eastWp07"], ["eastWp07", "eastTop"], ["eastTop", "wp06Corner"], ["wp06Corner", "WP06"],
  ["eastWp07", "wp07Door"], ["wp07Door", "wp07Corner"], ["wp07Corner", "WP07"],
  ["wp07Corner", "stairWp07Foot"], ["stairWp07Foot", "stairWp07"],
  // Floor 2: the hallway, its rooms and the stair column
  ["hallWp11", "hallStair"], ["hallStair", "hallWp12"],
  ["hallWp11", "wp11Door"], ["wp11Door", "WP11"], ["hallWp12", "wp12Door"], ["wp12Door", "wp12Corner"], ["wp12Corner", "WP12"],
  ["hallWp11", "wp13Top"], ["wp13Top", "wp13Corner"], ["wp13Corner", "WP13"], ["wp13Corner", "wp13Bottom"],
  ["hallWp12", "wp14Top"], ["wp14Top", "WP14"], ["WP14", "wp14Bottom"],
  ["hallStair", "stairTop"], ["stairTop", "landing"], ["landing", "stairBottom"],
  ["wp13Bottom", "lane13"], ["stairBottom", "laneStair"], ["wp14Bottom", "lane14"],
  ["lane13", "WP15"], ["WP15", "laneStair"], ["laneStair", "lane14"],
];
// Both floor 1 stairs arrive at the floor 2 landing between its two flights. Their weight stands in for the climb.
const climbs: [string, string][] = [["stairWp07", "landing"], ["stairWp09", "landing"]];
const CLIMB_COST = 200;

const at = (id: string): XY => ({ x: nodes[id][0], y: nodes[id][1] });
const dist = (a: XY, b: XY) => Math.hypot(b.x - a.x, b.y - a.y);
const graph = new Map<string, { to: string; cost: number }[]>();
for (const [pairs, cost] of [[walks, undefined], [climbs, CLIMB_COST]] as const) for (const [a, b] of pairs) {
  const weight = cost ?? dist(at(a), at(b));
  graph.set(a, [...(graph.get(a) ?? []), { to: b, cost: weight }]);
  graph.set(b, [...(graph.get(b) ?? []), { to: a, cost: weight }]);
}

/** Node ids from `from` to `to` along the network (Dijkstra; the graph is a few dozen nodes). */
function shortestPath(from: string, to: string): string[] {
  const best = new Map([[from, 0]]), previous = new Map<string, string>(), done = new Set<string>();
  while (true) {
    let here: string | undefined;
    for (const [id, cost] of best) if (!done.has(id) && (here === undefined || cost < best.get(here)!)) here = id;
    if (here === undefined) return [from, to];   // unreachable: fall back to a straight line
    if (here === to) break;
    done.add(here);
    for (const { to: next, cost } of graph.get(here) ?? []) {
      const total = best.get(here)! + cost;
      if (total < (best.get(next) ?? Infinity)) { best.set(next, total); previous.set(next, here); }
    }
  }
  const path = [to];
  while (path[0] !== from) path.unshift(previous.get(path[0])!);
  return path;
}

/** Legs visiting the waypoints `stops` (location ids) in order, split into walks and climbs. */
export function routeLegs(stops: string[]): Leg[] {
  const legs: Leg[] = [];
  stops.slice(1).forEach((stop, travel) => {
    if (stop === stops[travel] || !nodes[stop] || !nodes[stops[travel]]) return;
    let walk: XY[] = [];
    for (const point of shortestPath(stops[travel], stop).map(at)) {
      const last = walk.at(-1);
      if (last && floorOf(last) !== floorOf(point)) {
        legs.push({ kind: "walk", travel, points: walk }, { kind: "climb", travel, from: last, to: point, up: floorOf(point) === 2 });
        walk = [];
      }
      walk.push(point);
    }
    legs.push({ kind: "walk", travel, points: walk });
  });
  return legs.filter(leg => leg.kind === "climb" || leg.points.length > 1);
}

export const legLength = (leg: Leg) => leg.kind === "walk" ? leg.points.slice(1).reduce((sum, p, i) => sum + dist(leg.points[i], p), 0) : 0;

/** The walked part of a walk at `t` (0–1 of its length), ending exactly where SPOT is. */
function walked(points: XY[], t: number): XY[] {
  let remaining = t * legLength({ kind: "walk", travel: 0, points });
  const out = [points[0]];
  for (let i = 1; i < points.length; i++) {
    const step = dist(points[i - 1], points[i]);
    if (remaining <= step) {
      const f = step === 0 ? 0 : remaining / step;
      out.push({ x: points[i - 1].x + (points[i].x - points[i - 1].x) * f, y: points[i - 1].y + (points[i].y - points[i - 1].y) * f });
      return out;
    }
    remaining -= step;
    out.push(points[i]);
  }
  return out;
}

/** SPOT's position `t` (0–1) along a leg. On a climb it stays at the stair for the first half, then pops up. */
export function pointOnLeg(leg: Leg, t: number): XY {
  return leg.kind === "walk" ? walked(leg.points, t).at(-1)! : t < 0.5 ? leg.from : leg.to;
}

/** The part of a walk between `from` and `to` (0–1 of its length). */
function slice(points: XY[], from: number, to: number): XY[] {
  if (to <= from) return [];
  const head = walked(points, to);
  if (from <= 0) return head;
  const reversed = walked([...head].reverse(), 1 - from / to);
  return reversed.reverse();
}

const JOG = 16;

/** SVG path data for the walks, the first one starting at `from` and the last one ending at `to` (0–1 of its
    length). Climbs leave a gap: the floors are separate plans. */
export function legsPath(legs: Leg[], { from = 0, to = 1 }: { from?: number; to?: number } = {}) {
  return legs.flatMap((leg, i) => {
    if (leg.kind !== "walk") return [];
    const start = i === 0 ? from : 0, end = i === legs.length - 1 ? to : 1;
    const points = slice(leg.points, start, end);
    // A pin can sit a few pixels off its hallway line; drop that last little jog so the run (and its arrow) stays
    // straight into the pin. Only at whole ends, never where the line is cut at SPOT.
    if (end === 1 && points.length > 2 && dist(points.at(-2)!, points.at(-1)!) < JOG) points.pop();
    if (start === 0 && points.length > 2 && dist(points[0], points[1]) < JOG) points.shift();
    return points.length > 1 ? [`M${points.map(p => `${p.x},${p.y}`).join("L")}`] : [];
  }).join("");
}

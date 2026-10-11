import Link from "next/link";
import { ArrowDown, ArrowRight, ArrowUp } from "lucide-react";

export const metadata = { title: "Help · SPOTlight" };

/* The console's manual. Same shape as Settings: a sticky index on the left,
   one card per topic on the right. It follows the operator's loop (triage →
   review → act) first, then each page in sidebar order, then the reference
   material (signals, shortcuts, outages, terms) people come back to. */

const SECTIONS = [
  { id: "start", label: "Getting started" },
  { id: "overview", label: "Overview" },
  { id: "review", label: "Reviewing a flag" },
  { id: "live", label: "Live patrol" },
  { id: "missions", label: "Missions" },
  { id: "analytics", label: "Analytics" },
  { id: "assets", label: "Assets" },
  { id: "spotbot", label: "SPOTbot" },
  { id: "signals", label: "Reading the signals" },
  { id: "search", label: "Search & shortcuts" },
  { id: "settings", label: "Settings" },
  { id: "offline", label: "When something is unavailable" },
  { id: "terms", label: "Terms" },
] as const;

const LINK = "font-medium text-accent-ink hover:underline";

function Section({ id, title, lead, children }: { id: string; title: string; lead?: string; children: React.ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-4 rounded-panel border border-line bg-surface panel-shadow">
      <header className="border-b border-line px-5 py-4">
        <h2 id={`${id}-title`} className="text-base font-semibold text-ink">{title}</h2>
        {lead && <p className="mt-1 text-sm text-muted">{lead}</p>}
      </header>
      <div className="space-y-3 px-5 py-4 text-sm leading-relaxed text-ink [&_li]:text-muted [&_p]:text-muted">{children}</div>
    </section>
  );
}

function Steps({ children }: { children: React.ReactNode }) {
  return <ol className="list-decimal space-y-1.5 pl-5 marker:text-faint">{children}</ol>;
}

function Bullets({ children }: { children: React.ReactNode }) {
  return <ul className="list-disc space-y-1.5 pl-5 marker:text-faint">{children}</ul>;
}

function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="rounded border border-line-strong px-1.5 py-0.5 font-mono text-[11px] text-ink">{children}</kbd>;
}

function Term({ name, children }: { name: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 border-b border-line py-2.5 last:border-0 sm:grid-cols-[10rem_1fr] sm:gap-4">
      <dt className="font-medium text-ink">{name}</dt>
      <dd className="text-muted">{children}</dd>
    </div>
  );
}

export default function HelpPage() {
  return (
    <div className="mx-auto max-w-6xl p-4 sm:p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold text-ink">Help</h1>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          SPOTlight reads every inspection mission the SPOT robot runs and ranks what an operator should look at next.
          This page explains each part of the console, how to read what it shows, and what to do when a service is offline.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <nav aria-label="Help sections" className="lg:sticky lg:top-4 lg:self-start">
          <ul className="flex flex-wrap gap-1 lg:flex-col">
            {SECTIONS.map((s) => (
              <li key={s.id}>
                <a href={`#${s.id}`} className="block rounded px-2.5 py-1.5 text-sm text-muted transition-colors hover:bg-surface-2 hover:text-ink">
                  {s.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-w-0 space-y-6">
          <Section id="start" title="Getting started" lead="The console is built around one loop: see what needs attention, understand why, then act on it.">
            <Steps>
              <li><strong className="text-ink">Triage.</strong> Open the <Link href="/" className={LINK}>Overview</Link>. The <em>Needs attention</em> queue lists every open flag, most urgent first.</li>
              <li><strong className="text-ink">Understand.</strong> Click a flag to see every signal that raised it, then choose <em>Review &amp; act</em> for the evidence and an AI explanation.</li>
              <li><strong className="text-ink">Check the context.</strong> Replay the mission that raised it, look at the asset&apos;s other flags, or ask SPOTbot.</li>
              <li><strong className="text-ink">Act.</strong> Acknowledge or resolve the flag under your operator name. It leaves the queue, and its history is kept.</li>
            </Steps>
            <p>
              The data comes from the NextEra SPOT challenge kit: recorded missions, the actions SPOT took at each waypoint,
              the readings and images it captured, and the anomalies it reported.
            </p>
          </Section>

          <Section id="overview" title="Overview" lead="The home page. A banner with the headline finding, the priority queue, and fleet charts that follow whatever flag you open.">
            <h3 className="font-medium">Needs attention</h3>
            <Bullets>
              <li>Each row is an open flag: its severity, a confidence score, what it is about (asset · waypoint · mission), and its strongest two signals. <em>+N more signals</em> means more rules fired; open the row to see them all.</li>
              <li>The coloured bar on the left of each row shows severity, so you can read the shape of the backlog at a glance.</li>
              <li>Use the <em>All / Critical / High / Medium</em> pills in the panel header to narrow the queue. The number on each pill is how many open flags it holds.</li>
              <li>Clicking a row opens it in place and lifts it to the top of the list. From there: <em>Review &amp; act</em>, <em>Replay mission</em>, <em>More on this asset</em>, or <em>Ask SPOTbot</em>. Press <Kbd>Esc</Kbd> or click the row&apos;s title again to close it.</li>
            </Bullets>
            <h3 className="pt-1 font-medium">Fleet analytics</h3>
            <Bullets>
              <li>Four charts behind the toggles in the panel header: success rate by <em>inspection type</em>, by <em>battery</em> band, action outcomes per <em>mission</em>, and failed actions by <em>failure mode</em>. Your last choice is remembered.</li>
              <li>With a flag open, the charts turn to it: its mission is marked, and its own numbers (in the accent colour) sit beside the fleet&apos;s.</li>
              <li>Click any bar to see the actions behind it. <em>More on Analytics</em> opens the full set of charts.</li>
            </Bullets>
          </Section>

          <Section id="review" title="Reviewing a flag" lead="Review & act opens a flag's full record: status, evidence, an explanation, and the actions you can take.">
            <Bullets>
              <li><strong className="text-ink">Status line.</strong> Shows whether the flag is <em>OPEN</em>, <em>ACKNOWLEDGED</em> or <em>RESOLVED</em>, along with its severity and confidence.</li>
              <li><strong className="text-ink">Explanation.</strong> Choose <em>Load explanation</em>. The model&apos;s draft streams in as it is written and is marked unverified. It is replaced by the checked answer once every claim has been matched to the flag&apos;s evidence. If the check fails, the draft is discarded rather than shown as fact.</li>
              <li><strong className="text-ink">Actions.</strong> Enter your operator name, then <em>Acknowledge</em> (seen, being handled), <em>Resolve</em> (dealt with) or <em>Reopen flag</em> (put it back in the queue). Acknowledged and resolved flags leave the open queue; notes and action history are always kept.</li>
            </Bullets>
            <p>
              A flag can be linked directly: the address bar carries <code className="font-mono text-xs text-ink">?flag=&lt;id&gt;</code> while it is open.
              The <Link href="/review" className={LINK}>AI review</Link> page shows the same queue without the charts.
            </p>
          </Section>

          <Section id="live" title="Live patrol" lead="Watch a patrol stop by stop as SPOTlight triages it in real time.">
            <Bullets>
              <li>Choose a patrol to watch. Mission 1 runs as a mock patrol, so no robot is needed: it re-sends the kit&apos;s rows for each action, in order.</li>
              <li>The map shows SPOT moving through the building. A solid line is the route already walked, a dashed line is the route to the next stop.</li>
              <li>Each stop rolls in as a card under the map: the robot&apos;s log row, the photo, the triage decision (confidence model and rules), and SPOTbot&apos;s explanation when the stop was escalated.</li>
              <li>The battery chart draws as SPOT reaches each stop. The dashed line is the 25% battery cliff; dots are red when a stop needs attention and green when everything was fine.</li>
            </Bullets>
          </Section>

          <Section id="missions" title="Missions" lead="Every recorded mission, the ones that need a look, and a replay of any of them.">
            <Bullets>
              <li><Link href="/missions" className={LINK}>All missions</Link> answers &quot;what ran&quot;. Click a card to replay the mission, or use <em>Ask SPOTbot</em> on it.</li>
              <li><Link href="/missions/flagged" className={LINK}>Flagged missions</Link> answers &quot;what needs a look&quot;: only missions with a failed action or an anomaly, worst first, each saying why it is listed.</li>
              <li><Link href="/missions/replay" className={LINK}>Replay</Link>: choose a mission, then play or scrub the timeline. Click a waypoint on the map (or focus it and press <Kbd>Enter</Kbd>) to see what happened there. A <strong className="text-ink">!</strong> marks an error. Travel follows the hallways and stairs between stops, and time spent at each stop is compressed from the logs.</li>
            </Bullets>
          </Section>

          <Section id="analytics" title="Analytics" lead="The full set of fleet charts, grouped by question.">
            <Bullets>
              <li>Sections: <em>Reliability</em> (where actions succeed and what breaks them), <em>Configuration</em>, <em>Missions</em>, <em>Failures &amp; anomalies</em> and <em>Waypoints</em>.</li>
              <li>Click any bar, slice, point or cell to open the rows behind it: the matching actions, missions, anomalies or readings, which you can drill into further.</li>
              <li>Shaded bands and whiskers are 95% intervals. A wide interval means few samples, so read the bar as a rough estimate.</li>
              <li>A bar in red marks the weak spot the chart is pointing at, such as the worst inspection type or the low-battery band.</li>
            </Bullets>
          </Section>

          <Section id="assets" title="Assets" lead="Inspection images from the database, and a viewer that ties every reading to where on the image it was measured.">
            <Bullets>
              <li>The gallery groups captures by inspection type, with each asset&apos;s finding and the image&apos;s source and licence. Asset details in the demo set are mock data, and each card says so.</li>
              <li>In the viewer, zoom and pan to any magnification. Hover a reading to light up its box on the image, and the other way round.</li>
              <li>The strip above the image lists every step that touched it, in order. Code computes every number; the AI models only describe and explain. A step that hasn&apos;t run yet is shown dimmed rather than hidden.</li>
            </Bullets>
          </Section>

          <Section id="spotbot" title="SPOTbot" lead="The console's assistant. Ask it about flags, missions, assets or the plant in plain language.">
            <Bullets>
              <li>Questions about the plant are answered from retrieved sources, which are cited in the answer. Greetings and general questions are answered without plant data.</li>
              <li>Every answer carries a badge saying how far to trust it:
                <ul className="mt-1.5 list-[circle] space-y-1 pl-5">
                  <li><em>Grounded · N sources</em>: every cited id matches a source and every figure appears in the sources.</li>
                  <li><em>N figures not found in sources</em>: numbers the check couldn&apos;t confirm. Verify them before acting.</li>
                  <li><em>No sources cited</em>: treat the answer as unverified.</li>
                  <li><em>Cited an unknown source</em>: the answer points at evidence that doesn&apos;t exist. Don&apos;t rely on it.</li>
                  <li><em>General answer</em>: no plant data was needed or used.</li>
                </ul>
              </li>
              <li>Hover a badge for the details. <em>Ask SPOTbot</em> buttons across the console open a chat with the question already written.</li>
              <li>Conversations are saved in the sidebar of the <Link href="/assistant" className={LINK}>SPOTbot</Link> page. If a model hits its rate limit, SPOTbot offers the next one.</li>
            </Bullets>
          </Section>

          <Section id="signals" title="Reading the signals" lead="What the numbers and colours on a flag mean.">
            <Bullets>
              <li><strong className="text-ink">Severity</strong> (Critical, High, Medium, Low) is how bad the finding would be if it were real.</li>
              <li><strong className="text-ink">Confidence</strong> is how sure SPOTlight is that it is real: the confidence model&apos;s score, adjusted by the rules that fired. The queue&apos;s order comes from the backend&apos;s score, which weighs severity, priority and confidence.</li>
              <li><strong className="text-ink">Signals</strong> are the rules that fired, heaviest first. The common ones:
                <ul className="mt-1.5 list-[circle] space-y-1 pl-5">
                  <li>SPOT reported an anomaly, or a reading failed or came back marked questionable.</li>
                  <li>Low battery: below 25%, far fewer actions succeed, so findings there are less reliable.</li>
                  <li>Repeat offender: the same asset has failed across several missions.</li>
                  <li>Hard environment: a high-noise or dusty area that fails more often than the rest of the plant.</li>
                  <li>Hidden failure: the mission reported <em>COMPLETED</em> even though an action inside it failed. The status field alone would never have surfaced it.</li>
                </ul>
              </li>
            </Bullets>
          </Section>

          <Section id="search" title="Search & shortcuts">
            <p>The search box in the top bar finds missions, assets and flags.</p>
            <dl>
              <Term name={"⌘K / Ctrl K"}>Jump to search from anywhere.</Term>
              <Term name={<span className="inline-flex items-center gap-1">Tab or <ArrowRight size={14} aria-label="Right arrow" /></span>}>Accept the suggested completion.</Term>
              <Term name={<span className="inline-flex items-center gap-1"><ArrowUp size={14} aria-label="Up arrow" /><ArrowDown size={14} aria-label="Down arrow" /></span>}>Move through the results.</Term>
              <Term name="Enter">Open the highlighted result, or see every match on the search page.</Term>
              <Term name="Esc">Close search, or close the open flag in the queue.</Term>
            </dl>
          </Section>

          <Section id="settings" title="Settings">
            <p>
              <Link href="/settings" className={LINK}>Settings</Link> holds your operator profile, appearance, notifications, triage and AI review preferences.
              The theme (light, dark or system) is saved and applies immediately. The other controls are there for the demo and are not saved yet; the page says so.
            </p>
          </Section>

          <Section id="offline" title="When something is unavailable" lead="Each part of the console degrades on its own, so one outage doesn't take down the rest.">
            <Bullets>
              <li><strong className="text-ink">Backend offline.</strong> Challenge-kit data (missions, replays, analytics) stays readable. Live flags, flag actions and explanations need the backend; the Overview offers <em>Retry</em> so you can reconnect once it is back.</li>
              <li><strong className="text-ink">Explanation didn&apos;t finish.</strong> Its unverified draft is discarded. Choose <em>Reload explanation</em> to try again.</li>
              <li><strong className="text-ink">SPOTbot unavailable.</strong> Either the assistant is switched off, its API key isn&apos;t configured, or its backend can&apos;t be reached. The chat names which one.</li>
              <li><strong className="text-ink">Confidence model unavailable.</strong> The rules triage on their own, and the stop card says so.</li>
            </Bullets>
            <p>Setup and service details are in <code className="font-mono text-xs text-ink">docs/RUNNING.md</code> in the repository.</p>
          </Section>

          <Section id="terms" title="Terms">
            <dl>
              <Term name="Mission">One recorded run of SPOT through the plant, made of actions at waypoints.</Term>
              <Term name="Waypoint">A stop on the route where SPOT inspects one or more assets.</Term>
              <Term name="Action">One thing SPOT did at a waypoint: a visual, thermal, gauge or acoustic inspection, or navigation. Each succeeds or fails.</Term>
              <Term name="Asset">A piece of plant equipment SPOT inspects, such as an electrical panel or a heat exchanger.</Term>
              <Term name="Anomaly">Something SPOT itself reported as out of the ordinary.</Term>
              <Term name="Flag">SPOTlight&apos;s call that something needs an operator: an anomaly or failure, scored and explained.</Term>
              <Term name="Challenge kit">The NextEra SPOT dataset the console runs on.</Term>
            </dl>
          </Section>
        </div>
      </div>
    </div>
  );
}

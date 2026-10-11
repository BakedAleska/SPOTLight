"use client";

import { useState, useSyncExternalStore } from "react";
import {
  Bell,
  Bot,
  Check,
  Monitor,
  Moon,
  Palette,
  Radio,
  ShieldAlert,
  Sun,
  User,
  type LucideIcon,
} from "lucide-react";
import { readTheme, setTheme, subscribeTheme, type ThemeChoice } from "@/lib/theme";

/* The settings page follows the familiar SaaS layout (Linear, Vercel, GitHub):
   a sticky section index on the left, and on the right one card per section.
   Each row puts the label and description on the left and the control on the
   right. A card's footer only wakes up once something in it has changed.

   Apart from the theme, nothing here is persisted. These are demo controls,
   and they are honest about it in the page intro. */

const SECTIONS: { id: string; label: string; icon: LucideIcon }[] = [
  { id: "profile", label: "Operator profile", icon: User },
  { id: "appearance", label: "Appearance", icon: Palette },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "triage", label: "Triage", icon: Radio },
  { id: "ai", label: "AI review", icon: Bot },
  { id: "danger", label: "Danger zone", icon: ShieldAlert },
];

export function SettingsForm() {
  return (
    <div className="mx-auto max-w-5xl p-4 md:p-6">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold text-ink">Settings</h1>
        <p className="mt-1 text-sm text-muted">
          Tune the console for your shift. Only the theme is saved; everything else resets on reload.
        </p>
      </header>

      <div className="grid gap-6 md:grid-cols-[200px_1fr]">
        <nav aria-label="Settings sections" className="md:sticky md:top-4 md:self-start">
          <ul className="flex gap-1 overflow-x-auto md:flex-col">
            {SECTIONS.map(({ id, label, icon: Icon }) => (
              <li key={id}>
                <a
                  href={`#${id}`}
                  className={`flex items-center gap-2 whitespace-nowrap rounded px-3 py-1.5 text-sm transition-colors hover:bg-surface-2 hover:text-ink ${
                    id === "danger" ? "text-bad" : "text-muted"
                  }`}
                >
                  <Icon size={15} aria-hidden="true" />
                  {label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="flex min-w-0 flex-col gap-6">
          <ProfileCard />
          <AppearanceCard />
          <NotificationsCard />
          <TriageCard />
          <AiCard />
          <DangerCard />
        </div>
      </div>
    </div>
  );
}

/* ── Cards ──────────────────────────────────────────────────────────────── */

function ProfileCard() {
  const form = useDraft({ name: "Operator 1", callsign: "SPOT-OPS", shift: "night" });
  return (
    <Card id="profile" title="Operator profile" description="How you appear on acknowledgements and resolutions." form={form}>
      <Row label="Display name" htmlFor="name">
        <input id="name" className={INPUT} value={form.value.name} onChange={(e) => form.set({ name: e.target.value })} />
      </Row>
      <Row label="Callsign" description="Shown next to your actions in the mission log." htmlFor="callsign">
        <input
          id="callsign"
          className={`${INPUT} font-mono uppercase`}
          value={form.value.callsign}
          onChange={(e) => form.set({ callsign: e.target.value })}
        />
      </Row>
      <Row label="Shift" htmlFor="shift">
        <select id="shift" className={INPUT} value={form.value.shift} onChange={(e) => form.set({ shift: e.target.value })}>
          <option value="day">Day (06:00–14:00)</option>
          <option value="swing">Swing (14:00–22:00)</option>
          <option value="night">Night (22:00–06:00)</option>
        </select>
      </Row>
    </Card>
  );
}

function AppearanceCard() {
  const theme = useSyncExternalStore<ThemeChoice>(subscribeTheme, readTheme, () => "system");
  const form = useDraft({ density: "compact", motion: true, units: "metric" });
  return (
    <Card id="appearance" title="Appearance" description="Theme applies instantly and is remembered on this device." form={form}>
      <Row label="Theme">
        <Segmented
          label="Theme"
          value={theme}
          onChange={setTheme}
          options={[
            { value: "system", label: "System", icon: Monitor },
            { value: "light", label: "Light", icon: Sun },
            { value: "dark", label: "Dark", icon: Moon },
          ]}
        />
      </Row>
      <Row label="Density" description="Compact fits more telemetry on a single screen.">
        <Segmented
          label="Density"
          value={form.value.density}
          onChange={(density) => form.set({ density })}
          options={[
            { value: "compact", label: "Compact" },
            { value: "comfortable", label: "Comfortable" },
          ]}
        />
      </Row>
      <Row label="Units">
        <Segmented
          label="Units"
          value={form.value.units}
          onChange={(units) => form.set({ units })}
          options={[
            { value: "metric", label: "Metric" },
            { value: "imperial", label: "Imperial" },
          ]}
        />
      </Row>
      <Row label="Animations" description="Panel entrances, radar sweeps and the hero gradient.">
        <Toggle label="Animations" checked={form.value.motion} onChange={(motion) => form.set({ motion })} />
      </Row>
    </Card>
  );
}

function NotificationsCard() {
  const form = useDraft({ critical: true, warnings: true, digest: false, sound: true, quiet: "off" });
  return (
    <Card id="notifications" title="Notifications" description="Choose what interrupts you mid-shift." form={form}>
      <Row label="Critical flags" description="Robot fall, E-stop, or a gauge reading out of range.">
        <Toggle label="Critical flags" checked={form.value.critical} onChange={(critical) => form.set({ critical })} />
      </Row>
      <Row label="Warnings" description="Low battery, missed waypoint, degraded localisation.">
        <Toggle label="Warnings" checked={form.value.warnings} onChange={(warnings) => form.set({ warnings })} />
      </Row>
      <Row label="End-of-shift digest" description="A summary of every mission you touched, emailed at handover.">
        <Toggle label="End-of-shift digest" checked={form.value.digest} onChange={(digest) => form.set({ digest })} />
      </Row>
      <Row label="Alert sound">
        <Toggle label="Alert sound" checked={form.value.sound} onChange={(sound) => form.set({ sound })} />
      </Row>
      <Row label="Quiet hours" htmlFor="quiet">
        <select id="quiet" className={INPUT} value={form.value.quiet} onChange={(e) => form.set({ quiet: e.target.value })}>
          <option value="off">Off</option>
          <option value="lunch">During breaks</option>
          <option value="night">00:00–04:00</option>
        </select>
      </Row>
    </Card>
  );
}

function TriageCard() {
  const form = useDraft({ threshold: 60, autoAck: false, window: "24h", group: true });
  return (
    <Card id="triage" title="Triage" description="How the priority queue ranks and groups incoming flags." form={form}>
      <Row label="Severity threshold" description="Flags scoring below this are hidden from the queue." htmlFor="threshold">
        <div className="flex w-full items-center gap-3 sm:w-56">
          <input
            id="threshold"
            type="range"
            min={0}
            max={100}
            step={5}
            value={form.value.threshold}
            onChange={(e) => form.set({ threshold: Number(e.target.value) })}
            className="h-1 flex-1 cursor-pointer accent-[var(--c-accent)]"
          />
          <span className="nums w-8 text-right text-sm text-ink">{form.value.threshold}</span>
        </div>
      </Row>
      <Row label="Replay window" htmlFor="window">
        <select id="window" className={INPUT} value={form.value.window} onChange={(e) => form.set({ window: e.target.value })}>
          <option value="6h">Last 6 hours</option>
          <option value="24h">Last 24 hours</option>
          <option value="7d">Last 7 days</option>
        </select>
      </Row>
      <Row label="Group by asset" description="Collapse repeated flags on the same gauge or valve.">
        <Toggle label="Group by asset" checked={form.value.group} onChange={(group) => form.set({ group })} />
      </Row>
      <Row label="Auto-acknowledge info flags" description="Informational events skip the queue entirely.">
        <Toggle label="Auto-acknowledge info flags" checked={form.value.autoAck} onChange={(autoAck) => form.set({ autoAck })} />
      </Row>
    </Card>
  );
}

function AiCard() {
  const form = useDraft({ explain: true, verbosity: "brief", citations: true });
  return (
    <Card
      id="ai"
      title="AI review"
      description="Controls for the model that explains flags and suggests actions."
      badge="Beta"
      form={form}
    >
      <Row label="Explain flags automatically" description="Generate an explanation as soon as a flag is opened.">
        <Toggle label="Explain flags automatically" checked={form.value.explain} onChange={(explain) => form.set({ explain })} />
      </Row>
      <Row label="Explanation length">
        <Segmented
          label="Explanation length"
          value={form.value.verbosity}
          onChange={(verbosity) => form.set({ verbosity })}
          options={[
            { value: "brief", label: "Brief" },
            { value: "detailed", label: "Detailed" },
          ]}
        />
      </Row>
      <Row label="Show citations" description="Link every claim back to the telemetry or image that supports it.">
        <Toggle label="Show citations" checked={form.value.citations} onChange={(citations) => form.set({ citations })} />
      </Row>
    </Card>
  );
}

function DangerCard() {
  const [done, setDone] = useState<string | null>(null);
  const actions = [
    { id: "cache", label: "Clear replay cache", description: "Re-download mission telemetry next time you open a replay.", cta: "Clear cache" },
    { id: "reset", label: "Reset all settings", description: "Restore defaults for every section above, including theme.", cta: "Reset" },
  ];
  return (
    <section id="danger" className="scroll-mt-4 rounded-panel border border-bad/40 bg-surface panel-shadow">
      <CardHeader title="Danger zone" description="These take effect immediately." />
      <div className="divide-y divide-line">
        {actions.map((a) => (
          <Row key={a.id} label={a.label} description={a.description}>
            <button
              type="button"
              onClick={() => {
                if (a.id === "reset") setTheme("system");
                setDone(a.id);
              }}
              className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded border border-bad/50 px-3 text-sm font-medium text-bad transition-colors hover:bg-bad hover:text-white"
            >
              {done === a.id ? <><Check size={14} aria-hidden="true" /> Done</> : a.cta}
            </button>
          </Row>
        ))}
      </div>
    </section>
  );
}

/* ── Primitives ─────────────────────────────────────────────────────────── */

const INPUT =
  "h-8 w-full rounded border border-line-strong/60 bg-surface-3 px-2.5 text-sm text-ink outline-none transition-colors focus:border-focus focus-visible:ring-2 focus-visible:ring-focus/30 sm:w-56";

/** Local draft state with a saved baseline, so a card knows when it's dirty. */
function useDraft<T extends Record<string, unknown>>(initial: T) {
  const [saved, setSaved] = useState(initial);
  const [value, setValue] = useState(initial);
  const [justSaved, setJustSaved] = useState(false);
  const dirty = (Object.keys(value) as (keyof T)[]).some((k) => value[k] !== saved[k]);
  return {
    value,
    dirty,
    justSaved,
    set: (patch: Partial<T>) => {
      setJustSaved(false);
      setValue((v) => ({ ...v, ...patch }));
    },
    save: () => {
      setSaved(value);
      setJustSaved(true);
    },
    discard: () => setValue(saved),
  };
}

type Draft = ReturnType<typeof useDraft>;

function Card({
  id,
  title,
  description,
  badge,
  form,
  children,
}: {
  id: string;
  title: string;
  description: string;
  badge?: string;
  form: Draft;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-4 rounded-panel border border-line bg-surface panel-shadow">
      <CardHeader title={title} description={description} badge={badge} />
      <div className="divide-y divide-line">{children}</div>
      <footer className="flex min-h-12 items-center justify-end gap-2 border-t border-line bg-surface-3 px-5 py-2">
        <span className="mr-auto text-xs text-faint" aria-live="polite">
          {form.dirty ? "You have unsaved changes." : form.justSaved ? "Saved." : ""}
        </span>
        <button
          type="button"
          onClick={form.discard}
          disabled={!form.dirty}
          className="h-8 cursor-pointer rounded px-3 text-sm text-muted transition-colors hover:bg-surface-2 hover:text-ink disabled:pointer-events-none disabled:opacity-0"
        >
          Discard
        </button>
        <button
          type="button"
          onClick={form.save}
          disabled={!form.dirty}
          className="h-8 cursor-pointer rounded bg-accent px-3 text-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:cursor-default disabled:bg-surface-2 disabled:text-faint"
        >
          Save
        </button>
      </footer>
    </section>
  );
}

function CardHeader({ title, description, badge }: { title: string; description: string; badge?: string }) {
  return (
    <header className="border-b border-line px-5 py-4">
      <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
        {title}
        {badge && (
          <span className="rounded-full border border-accent/50 px-1.5 py-px text-[11px] font-medium text-accent-ink">
            {badge}
          </span>
        )}
      </h2>
      <p className="mt-0.5 text-sm text-muted">{description}</p>
    </header>
  );
}

function Row({
  label,
  description,
  htmlFor,
  children,
}: {
  label: string;
  description?: string;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  const Label = htmlFor ? "label" : "div";
  return (
    <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-8">
      <div className="min-w-0">
        <Label htmlFor={htmlFor} className="text-sm font-medium text-ink">
          {label}
        </Label>
        {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 cursor-pointer items-center rounded-full transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ${
        checked ? "bg-accent" : "bg-line-strong/50"
      }`}
    >
      <span
        className={`inline-block size-4 rounded-full bg-white shadow transition-transform duration-150 ${
          checked ? "translate-x-[18px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function Segmented<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string; icon?: LucideIcon }[];
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded border border-line bg-surface-3 p-0.5">
      {options.map(({ value: v, label: l, icon: Icon }) => {
        const active = v === value;
        return (
          <button
            key={v}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(v)}
            className={`inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-[3px] px-3 text-sm transition-colors ${
              active ? "bg-surface text-ink shadow-sm ring-1 ring-line" : "text-muted hover:text-ink"
            }`}
          >
            {Icon && <Icon size={14} aria-hidden="true" />}
            {l}
          </button>
        );
      })}
    </div>
  );
}

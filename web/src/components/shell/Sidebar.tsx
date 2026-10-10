"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronRight, X } from "lucide-react";
import { SpotlightLogo } from "../SpotlightLogo";
import { nav, type NavItem } from "./nav";

/* Shared geometry for every row, so icons line up down the whole rail whether
   the row is a link, a disclosure summary or a child. */
const ROW =
  "flex h-9 w-full items-center gap-3 rounded px-3 text-sm transition-colors";

function Row({ item, active }: { item: NavItem; active: boolean }) {
  const Icon = item.icon;
  const content = (
    <>
      <Icon size={16} aria-hidden="true" className="shrink-0" />
      <span className="truncate">{item.label}</span>
    </>
  );

  // Routes that don't exist yet are shown but inert. Linking them would send a
  // judge mid-demo to a 404; hiding them would hide the shape of the product.
  if (!item.href) {
    return (
      <span
        aria-disabled="true"
        title="Not built yet"
        className={`${ROW} cursor-default text-faint`}
      >
        {content}
      </span>
    );
  }

  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={`${ROW} relative cursor-pointer ${
        active
          ? "bg-surface-2 font-medium text-ink before:absolute before:inset-y-1 before:left-0 before:w-0.5 before:rounded-full before:bg-accent before:content-['']"
          : "text-muted hover:bg-surface-2 hover:text-ink"
      }`}
    >
      {content}
    </Link>
  );
}

/* <details> gives us a keyboard-accessible disclosure with correct semantics
   and no state to manage or hydrate. */
function Section({ item, pathname }: { item: NavItem; pathname: string }) {
  const Icon = item.icon;
  return (
    <details className="group">
      <summary
        className={`${ROW} cursor-pointer list-none text-muted hover:bg-surface-2 hover:text-ink [&::-webkit-details-marker]:hidden`}
      >
        <Icon size={16} aria-hidden="true" className="shrink-0" />
        <span className="truncate">{item.label}</span>
        <ChevronRight
          size={14}
          aria-hidden="true"
          className="ml-auto shrink-0 transition-transform group-open:rotate-90"
        />
      </summary>
      <ul className="mt-0.5 space-y-0.5">
        {item.children?.map((child) => (
          <li key={child.label}>
            {child.href ? (
              <Link
                href={child.href}
                aria-current={pathname === child.href ? "page" : undefined}
                className={`${ROW} cursor-pointer pl-10 text-muted hover:bg-surface-2 hover:text-ink`}
              >
                <span className="truncate">{child.label}</span>
              </Link>
            ) : (
              <span
                aria-disabled="true"
                title="Not built yet"
                className={`${ROW} cursor-default pl-10 text-faint`}
              >
                <span className="truncate">{child.label}</span>
              </span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

export function Sidebar({ onClose }: { onClose: () => void }) {
  const pathname = usePathname();

  return (
    <div className="flex h-full w-60 flex-col border-r border-line bg-chrome">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-3">
        <SpotlightLogo small className="size-7 shrink-0" />
        <span className="font-semibold tracking-tight">SPOTlight</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close menu"
          className="ml-auto grid size-7 cursor-pointer place-items-center rounded text-muted transition-colors hover:bg-surface-2 hover:text-ink"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      <nav aria-label="Primary" className="flex-1 overflow-y-auto p-2">
        <ul className="space-y-0.5">
          {nav.map((item) => (
            <li key={item.label}>
              {item.children ? (
                <Section item={item} pathname={pathname} />
              ) : (
                <Row item={item} active={pathname === item.href} />
              )}
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}

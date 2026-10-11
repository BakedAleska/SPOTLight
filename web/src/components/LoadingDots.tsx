/* Vercel's LoadingDots from its Next.js templates (vercel/platforms, vercel/ai-chatbot): three dots that blink in turn.
   Styles live in globals.css under .loading-dots. */
export function LoadingDots({ color = "#000" }: { color?: string }) {
  return <span className="loading-dots">
    <span style={{ backgroundColor: color }} />
    <span style={{ backgroundColor: color }} />
    <span style={{ backgroundColor: color }} />
  </span>;
}

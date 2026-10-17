import type { ReactNode } from "react";

// Google's grounded text is shown exactly as returned, as TEXT: React inserts these strings as
// text nodes, never as HTML. Markdown is not interpreted. The one presentation aid is bold for a
// balanced **span** on one line, and the asterisks stay in the text (dimmed), so the text content
// of the element is character for character what the server sent.
const BOLD = /(\*\*[^*\n]+\*\*)/g;

export function VerbatimText({ text, className }: { text: string; className?: string }) {
  const parts: ReactNode[] = [];
  text.split(BOLD).forEach((part, index) => {
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) {
      parts.push(
        <span key={index}>
          <span className="md-mark">**</span>
          <strong>{part.slice(2, -2)}</strong>
          <span className="md-mark">**</span>
        </span>,
      );
    } else if (part !== "") {
      parts.push(part);
    }
  });
  return <p className={className}>{parts}</p>;
}

// Google's grounded text is shown exactly as returned, as PLAIN TEXT: one text node (React never
// inserts it as HTML), no Markdown and no emphasis (policy summary 6, decision 16; emphasis is open
// legal question 12). Line breaks are kept by CSS (white-space: pre-wrap), not by changing the text.
export function VerbatimText({ text, className }: { text: string; className?: string }) {
  return <p className={className}>{text}</p>;
}

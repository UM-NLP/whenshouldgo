import type { DiscoveryBlock } from "../api/client";
import { VerbatimText } from "./VerbatimText";

// A link is only rendered as one when it is https on Google's own host, as the server already
// guarantees. This is a second check, not a rewrite: the URL is used exactly as provided.
export function isGoogleHttpsUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "www.google.com";
  } catch {
    return false;
  }
}

function SourceLink({ href, children }: { href: string; children: string }) {
  if (!isGoogleHttpsUrl(href)) return <span>{children}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

/**
 * Google's grounded answer. Order is fixed: the branding label, then the verbatim text, then the
 * sources immediately after it with nothing in between. The server's own framing is outside this
 * component (the reply bubble above it).
 *
 * Each source is ONLY the title link to attribution_url exactly as supplied: no client-authored
 * second link. OPEN ITEM: counsel must confirm this layout (link preview, the extra "Retrieved"
 * line after the sources) against Google's attribution requirements
 * (docs/legal/open-questions.md, "Other open items").
 */
export function DiscoveryBlockView({ block }: { block: DiscoveryBlock }) {
  const retrieved = new Date(block.retrieved_at);
  return (
    <section className="discovery" aria-label="Google Maps results">
      <div className="discovery-brand">
        <span translate="no">Google Maps</span>
      </div>
      <VerbatimText text={block.text} className="discovery-text" />
      <ul className="discovery-sources" aria-label="Sources">
        {block.places.map((place, index) => (
          <li key={`${index}-${place.place_url}`}>
            <SourceLink href={place.attribution_url}>{place.title}</SourceLink>
          </li>
        ))}
      </ul>
      {Number.isNaN(retrieved.getTime()) ? null : (
        <p className="discovery-meta">
          Retrieved <time dateTime={block.retrieved_at}>{retrieved.toLocaleString()}</time>
        </p>
      )}
    </section>
  );
}

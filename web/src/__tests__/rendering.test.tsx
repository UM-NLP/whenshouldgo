import { render, screen, within } from "@testing-library/react";

import { AssistantMessage } from "../components/AssistantMessage";
import { DiscoveryBlockView, isGoogleHttpsUrl } from "../components/DiscoveryBlockView";
import { VerbatimText } from "../components/VerbatimText";
import { GROUNDED_TEXT, discoveryBlock, turnResponse } from "./support";

describe("VerbatimText", () => {
  it("keeps the text content character for character, asterisks included", () => {
    const { container } = render(<VerbatimText text={GROUNDED_TEXT} />);
    expect(container.querySelector("p")?.textContent).toBe(GROUNDED_TEXT);
  });

  it("inserts markup-looking text as text, never as HTML", () => {
    const { container } = render(<VerbatimText text={GROUNDED_TEXT} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<b>vegetarian</b>");
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("renders Google text plain: no emphasis, textContent equals the server string", () => {
    const text = "Go to **Noodle Bar** now\nand **b\nc d ** e";
    const { container } = render(<VerbatimText text={text} />);
    expect(container.querySelector("strong, b, em")).toBeNull();
    expect(container.querySelector(".md-mark")).toBeNull();
    expect(container.querySelector("p")?.childNodes).toHaveLength(1);
    expect(container.textContent).toBe(text);
  });

  it("does not interpret other Markdown (links, headings)", () => {
    const text = "# Title\n[x](https://evil.example)";
    const { container } = render(<VerbatimText text={text} />);
    expect(container.querySelector("a, h1")).toBeNull();
    expect(container.textContent).toBe(text);
  });
});

describe("DiscoveryBlockView", () => {
  it("puts the sources immediately after the text, with nothing between", () => {
    const { container } = render(<DiscoveryBlockView block={discoveryBlock()} />);
    const text = container.querySelector(".discovery-text");
    expect(text).not.toBeNull();
    expect(text?.nextElementSibling?.tagName).toBe("UL");
    expect(text?.nextElementSibling?.getAttribute("aria-label")).toBe("Sources");
  });

  it("links each attribution title to the URL exactly as provided", () => {
    const block = discoveryBlock();
    render(<DiscoveryBlockView block={block} />);
    for (const place of block.places) {
      const link = screen.getByRole("link", { name: place.title });
      expect(link).toHaveAttribute("href", place.attribution_url);
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
      expect(link).toHaveAttribute("target", "_blank");
    }
  });

  it("renders only the title link to attribution_url, never a client-authored second link", () => {
    const base = discoveryBlock();
    const first = base.places[0]!;
    const other = base.places[1]!.place_url;
    render(
      <DiscoveryBlockView
        block={{ ...base, places: [first, { ...first, title: "Other", place_url: other }] }}
      />,
    );
    expect(screen.queryByRole("link", { name: "Open in Google Maps" })).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Other" })).toHaveAttribute("href", first.attribution_url);
  });

  it("shows the Google Maps branding with translation turned off", () => {
    render(<DiscoveryBlockView block={discoveryBlock()} />);
    const brand = within(screen.getByRole("region", { name: "Google Maps results" })).getByText("Google Maps");
    expect(brand).toHaveAttribute("translate", "no");
  });

  it("renders a title as plain text when its URL is not https on Google", () => {
    const base = discoveryBlock();
    const bad = { ...base.places[0]!, title: "Sneaky", attribution_url: "javascript:alert(1)", place_url: "javascript:alert(1)" };
    render(<DiscoveryBlockView block={{ ...base, places: [bad] }} />);
    expect(screen.getByText("Sneaky")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Sneaky" })).toBeNull();
    expect(isGoogleHttpsUrl("http://www.google.com/x")).toBe(false);
    expect(isGoogleHttpsUrl("https://evil.example/x")).toBe(false);
  });

  it("renders a place title containing markup as text", () => {
    const base = discoveryBlock();
    const place = { ...base.places[0]!, title: "<img src=x onerror=alert(1)> Cafe" };
    const { container } = render(<DiscoveryBlockView block={{ ...base, places: [place] }} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("link")).toHaveTextContent("<img src=x onerror=alert(1)> Cafe");
  });
});

describe("AssistantMessage", () => {
  it("shows the server reply above Google's block as separate elements", () => {
    const { container } = render(<AssistantMessage response={turnResponse()} />);
    const reply = container.querySelector(".reply");
    const block = container.querySelector(".discovery");
    expect(reply).toHaveTextContent("Here is what I found nearby.");
    expect(reply?.compareDocumentPosition(block as Node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(reply?.contains(block as Node)).toBe(false);
  });

  it("treats NEEDS_AREA as a normal reply: no error, no discovery, no warning notice", () => {
    render(
      <AssistantMessage
        response={turnResponse({ reply: "Where should I look?", discovery: null, warnings: ["NEEDS_AREA"] })}
      />,
    );
    expect(screen.getByText("Where should I look?")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("shows a limit notice when the allowance is exhausted, without computing anything", () => {
    render(<AssistantMessage response={turnResponse({ allowance_exhausted: true, discovery: null })} />);
    expect(screen.getByRole("status")).toHaveTextContent(/reached your allowance/i);
  });

  it("explains typed partial-result warnings in fixed copy", () => {
    render(<AssistantMessage response={turnResponse({ warnings: ["TOOL_BUDGET_EXHAUSTED"] })} />);
    expect(screen.getByText(/Some results may be missing/)).toBeInTheDocument();
  });
});

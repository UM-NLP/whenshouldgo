# Open legal and privacy questions

Updated 2026-10-14. This file tracks the legal and privacy items that gate each phase in [../trip-assistant-roadmap.md](../trip-assistant-roadmap.md). It comes from research reports prepared on 2026-10-13 by an analyst agent. It is not legal advice and not a determination from Google. Technical work continues on the stated assumptions; each item below has a draft GitHub issue in [github-issues-draft.md](github-issues-draft.md).

Sources that could not be read in full: the live Google Maps Platform Terms of Service and Service Specific Terms (client-rendered pages), several Canadian government pages (CRTC, Légis Québec, Justice Laws), and Apple and Firebase terms. Statements that rest on search excerpts or memory are marked unverified. Counsel or the owner should save the current live terms as dated PDFs in this folder.

## Working assumptions used by the technical design

- Discovery uses Google Maps Grounding Lite with an OpenRouter-hosted model, as described in the owner-supplied summary ([google-maps-llm-policy-summary.md](google-maps-llm-policy-summary.md)).
- Ordinary Places and Routes payloads stay out of model context (decision 17). The owner disagrees with this and wants user consent to be enough; consent does not change Google's licence terms, so the question is sent to Google below.
- Stored history holds place IDs and user data only; the retention ceiling is 180 days and the user can shorten it. Grounding Lite weather is treated as Maps content until Google answers.

## Verification status of the owner-supplied legal summary

Confirmed against live sources by the research: the 30-day cache for grounded output (SST section 10.2.2, second-hand), the indefinite place-ID storage rule for the Places API, the attribution rules (sources immediately after content, viewable in one interaction), that a non-Gemini model may be used if compliant, and the requirement that the model not train on, cache or store Maps content.

Not confirmed: that the “test, validate” machine-learning restriction applies to Grounding Lite (it is documented for the Analytics terms), Canadian billing eligibility, the launch stage, the “persistent knowledge base” wording, and whether place IDs from Grounding Lite fall under the indefinite-storage rule.

## Questions for Google Maps Platform support or the account team

Ask in writing, keep the case number and have counsel review the answers.

> We are building a consumer travel app for Toronto, Canada (billing account country: Canada). We use the Maps Grounding Lite MCP server (`mapstools.googleapis.com`) with a non-Gemini open-weight model reached through an aggregator (OpenRouter) configured for zero data retention and no training. We need written answers to the following.
>
> 1. What is the current launch stage of Maps Grounding Lite, and is it covered by the standard Maps Platform Terms and SLA? Is it available to billing accounts in Canada?
> 2. The Service Specific Terms say Google Maps Content must not be “cached by, stored by, or used to improve the LLM”. Our model provider does not store prompts or outputs or train on them, but its terms allow transient in-memory prompt caching. Is that compliant?
> 3. Place IDs returned by Grounding Lite (`search_places`, `resolve_names`, `resolve_maps_urls`): may we store them indefinitely, as the Places API policy allows, and associate them with a user's own labels and visit dates in a per-user trip history?
> 4. May we compute counts or time-of-week patterns over those stored place IDs and user-entered data, without storing any other Google Maps Content, and use the result to suggest outings to the same user? Is that “content based on Google Maps content”?
> 5. Do the restrictions on using Google Maps Content and grounded output to train, test, validate or improve ML or AI systems cover (a) automated tests of our agent that call Grounding Lite, (b) a model trained only on user-entered times and labels, or (c) one that also uses place IDs?
> 6. Does the 30-day cache for Grounded Output allow us to show a user the grounded answer they received in a “recent” view during those 30 days?
> 7. Does the 30-day latitude/longitude limit apply to coordinates returned by Grounding Lite tools?
> 8. May we send a limited set of fields returned by the ordinary Places API (name, types, price level, rating, opening-hours text, derived distance) to a third-party LLM, hosted with zero data retention, as prompt input so that it writes a short recommendation shown beside a Google Map and Google Maps attribution? If yes, which fields may never be included (reviews, photos, editorial and AI-generated summaries), and may the LLM's output be stored in the user's trip history? If no, please confirm that Grounding Lite is the intended path.

> 9. Is output from the Grounding Lite weather tool (`lookup_weather`) Google Maps Content for the purposes of the caching, display, attribution and no-train/test/validate rules? If not, may we store and reuse it like ordinary weather data?

> 10. Do the summary text and the place data from `search_places` follow the same display, caching and evaluation rules? May a `save_to_maps_url` be cached? What wording and display rules apply to the walking-route warning?
> 11. Do the attribution page's voice-first rules (companion UI, disclosure at first use and at least every six months) apply to a mobile web client? May an unmodified summary be shown after the user's session ends, and what counts as "within one user interaction" for sources?

## Checklist for counsel: notifications, profiling and privacy

1. Is a web-push or FCM notification an “electronic address” message under CASL? Is a notification such as “You usually go out Saturday morning. Want ideas?” a commercial electronic message, given that the service is free and recommends third-party businesses?
2. If it is, does the user confirming a routine in the app count as express consent that meets section 10? What wording and record are needed? Can implied consent apply?
3. What unsubscribe mechanism is acceptable for push (in-notification action plus settings), and what response time applies?
4. Does section 6(6) exempt reminders about a routine the user created?
5. Are our pattern inferences personal information for PIPEDA access and correction? What must an access response include?
6. Are separate consents for storing history and for pattern suggestions both required, and must the second be express?
7. Are outings that could imply health, religion, sexual orientation or children's activity a sensitive-inference risk? Should we block them or let the user hide them?
8. Does Quebec Law 25 require profiling to be off by default and activated by the user? Does it apply to notifications? Is a privacy impact assessment needed for OpenRouter and its hosts? Do Quebec residents need a French-language notice? (Section numbers from the research are unverified.)
9. What contract terms are needed with OpenRouter and the upstream host (retention, sub-processors, foreign-authority access, breach notice)? Is a data processing agreement available? OpenRouter publishes no sub-processor list or Canada-specific terms.
10. What retention maximum is appropriate for history, and what counts as sufficient anonymisation, given the OPC's Loblaw finding?
11. Which Bill C-36 provisions could change these answers, and when? (Tabled 2026-06-15; not law.)
12. Confirm the PIPEDA obligation to report breaches that pose a real risk of significant harm and to keep records (from memory, unverified).

## Other open items

| Item | How to resolve |
|---|---|
| ZDR with in-memory prompt caching vs Google's “not cached by the LLM” | Google's written answer (question 2) and written terms from OpenRouter and the chosen upstream host |
| OpenRouter has no sub-processor list or Canadian terms | Request a DPA and sub-processor information; select hosts after reading their terms |
| iOS web-push requirements and FCM data terms | Re-fetch Apple and Firebase pages from a machine that can load them |
| Whether the Google Cloud billing account is in the EEA (different terms apply) | Check the billing address |
| Grounding Lite pricing and quotas (research found conflicting sources, one paid SKU listed) | Confirm in the Google Cloud console |
| OpenRouter returns HTTP 429 after an upstream host already processed a request (for example during provider fallback); our gateway treats a real 429 as a free rejection | Ask OpenRouter in writing how 429 is billed and whether it can follow upstream processing; counsel to note the trust assumption |
| Attribution titles that contain ZWNJ (Persian, several Indic scripts) are rejected by the short-field Unicode rule, while ZWNJ is allowed in the grounded summary | Owner decision: a script-aware rule allowing ZWNJ between letters, or keep rejecting titles |
| Place-ID refresh guidance | Check Google's current guidance before building the refresh job |
| Grounding Lite live behavior: JSON vs server-sent events, whether an initialize or session step is needed, input and output casing, the real link hosts and error format | One live `tools/list` and `tools/call` capture with a Maps demo key, saved outside Git (all are marked `TODO(verify-live)` in `backend/app/discovery/`) |
| Privacy policy and terms of use must include the Google Maps terms and Google privacy notice links (summary section 8) | Counsel to draft wording |

## Gates by phase

**Phase 0:** launch stage and Canadian eligibility confirmed in writing, or the owner accepts a pilot-only Phase 0 on pre-GA terms with no SLA; OpenRouter zero retention enforced per request with a failing test when missing, written terms for the chosen upstream host on file; Google attribution and non-modification tests pass; history schema limited to place ID, user label, user tag and time; privacy policy and in-context notice naming Google, OpenRouter and the upstream host, stating cross-border processing and retention, with consent recorded by version; access, export, delete and withdrawal working; synthetic fixtures only; Google's written answer to question 3 (otherwise store only IDs the user picked from an ordinary Places result or a Maps URL they supplied).

**Phase 1:** everything above, plus the retention ceiling (180 days, user can shorten) in the notice and a tested backup deletion window.

**Phase 2:** Google's written answers to questions 4 and 5 (otherwise deterministic counts only and no place IDs in any trained model); separate express opt-in for pattern suggestions with the basis shown; counsel's view on inferences as personal information and an access format that includes them; the sensitive-inference rule implemented and tested; Quebec profiling and privacy impact assessment settled if Quebec residents are served.

**Phase 3:** counsel's CASL answer before any push is sent; express opt-in at routine confirmation naming the sender; in-notification stop and settings control; consent and unsubscribe logs; quiet hours and caps; generic lock-screen text; no background location; iOS home-screen requirement and FCM terms verified; re-check Google's terms and Bill C-36 at launch.

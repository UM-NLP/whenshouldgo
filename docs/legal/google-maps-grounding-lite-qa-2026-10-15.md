# Answers to the Google Maps Grounding Lite questions

> **Provenance.** Supplied by the project owner on 2026-10-15 under the heading "Answer to google questions", in reply to the eleven questions in [open-questions.md](open-questions.md). Saved verbatim below this note. The text is a documentation-based analysis with inline source markers. It says several times that Google should clarify and that it cannot give an unconditional approval, and it states that its availability conclusion is "not verification of your particular account". It is therefore **not** a written determination from Google, and it has not been verified against the live terms. The owner should record who produced it and, if it came from Google, the case number. Items it leaves open stay open in [open-questions.md](open-questions.md).

---

Answer to google questions:

1. Launch stage, terms, SLA and Canadian availability
   Maps Grounding Lite is Generally Available, announced on 22 April 2026. It is listed among Google Maps Platform Core Services and is governed by the Maps Platform Terms and applicable Service Specific Terms. Google Maps Platform
   The standard SLA provides a 99.9% monthly uptime objective, subject to its exclusions. This covers the Google service, not your complete OpenRouter/model/application pipeline. Google Cloud
   I found no restriction excluding Canadian billing accounts. Canada is not a prohibited territory; normal billing and API-enablement requirements apply. This is a documentation-based conclusion, not verification of your particular account. Google Cloud
2. Transient in-memory prompt caching
   Zero data retention alone does not establish compliance. OpenRouter explicitly allows some implicit, in-memory prompt caching under its ZDR policy because it does not classify that caching as retention. openrouter.ai
   Google’s compatible-LLM documentation separately prohibits caching Google Maps Content by the LLM. It publishes no explicit exception for reusable in-memory prompt caches. Consequently, the configuration described cannot be confirmed compliant merely because it excludes logging and training. developers.google.com
   Verify the actual upstream endpoint and fallback providers: disable reusable prompt caching or obtain Google’s written acceptance. Ordinary working memory during a single inference and a cache retained for later requests are technically distinct; Google should clarify that boundary.
3. Indefinite storage of Place IDs and personal trip history
   The general Place ID exception permits indefinite storage. Grounding Lite returns canonical Place IDs, including through its resolution tools. However, its own documentation does not expressly confirm the indefinite-storage exception for all three tools you name. developers.google.com
   My interpretation is that storing opaque IDs alongside independently user-entered labels and visit dates is consistent with the general exception. For the product-specific written assurance you request, Google should confirm that interpretation. This does not establish permission to save accompanying names, summaries, coordinates or other fields indefinitely.
4. Counts, time-of-week patterns and personalized suggestions
   Interpretation: calculating patterns solely from independently supplied visit dates and labels is analysis of user activity. Grouping that activity by Google Place ID is the unresolved part.
   The published prohibition on creating content from Google Maps Content does not expressly decide this identifier-only scenario. Therefore, I cannot give an unconditional approval for the proposed recommendation feature. Keeping results private to one user does not itself establish an exception. Google Cloud
5. Automated tests and models using user data or Place IDs
   The restrictions cover model testing and validation using Google Maps Content, and developing or improving AI systems using Grounded Output. Google Cloud
   Scenario	Assessment
   (a) Automated agent tests calling Grounding Lite	Connectivity/schema checks differ from content-based model benchmarking. The latter is restricted; mixed agent evaluations need clarification.
   (b) Training exclusively on independent user-entered times and labels	Outside these Google-content restrictions, provided no Google-derived features, labels or outputs enter training.
   (c) Training also using Place IDs	Do not assume permission. An ID-storage exception does not establish an AI-training exception.


   For regression tests, independently authored synthetic fixtures avoid relying on Google responses as evaluation data.
6. Showing cached answers in a 30-day “Recent” view
   Not automatically authorized. Section 10.2.2 permits up to 30 days specifically for evaluating or optimizing Grounded Output performance/display. It does not expressly approve a general recent-answer history. Obtain clarification that your proposed view falls within that purpose. Google Cloud
7. Thirty-day coordinate caching
   Do not automatically transfer the Places API allowance to Grounding Lite. Section 14.3 specifically covers Places API coordinates; Grounding Lite’s section does not state an equivalent coordinate-cache allowance. Its returned coordinates therefore need product-specific clarification. Google Cloud
8. Ordinary Places API fields supplied to a third-party LLM
   I cannot approve that pipeline under the published standard terms. The restriction on creating content from Google Maps Content is broader than the training restriction. ZDR, limited fields and adjacent map attribution do not establish an exception. No published field whitelist authorizes the proposed recommendation generation. Google Cloud
   Grounding Lite supplies the explicit permission for LLM grounding. Its output remains subject to its retention restrictions; generation does not create unrestricted trip-history storage rights. Google Cloud
   Accordingly, I would not offer a purported “safe fields” list separating names/ratings from reviews, photos or summaries for this otherwise unapproved workflow.
9. Weather returned by lookup_weather
   Treat it as Google Maps Content subject to the applicable restrictions. The general definition encompasses content supplied through the services, including third-party content. The weather tool also explicitly returns required attribution. There is no documented unrestricted-weather-data exception for this tool. Google Cloud
   Preserve the returned weather attribution and apply Grounding Lite’s display requirements. Do not assume that factual weather observations become freely reusable merely because they are weather data. developers.google.com
10. Summary text, place data, save links and walking warnings
   The summary and places fields are distinct: the summary contains generated text and citations; place records supply identifiers, coordinates, links and attribution. Preserve their source correspondence. developers.google.com
   They share applicable content restrictions, but the Grounded Output cache allowance is not blanket permission to retain every response field. Google Cloud
   For save_to_maps_url, the documentation authorizes presenting the returned link for saving, sharing or opening the list. It states no explicit caching period. Use the returned URL; do not construct a replacement. Storage permission needs clarification. developers.google.com
   For every displayed walking route, show the warning that walking routes are beta and may omit pedestrian infrastructure. Suitable wording is:
Walking routes are in beta and might sometimes be missing clear sidewalks or pedestrian paths.

   Present it with the walking result so users encounter it when viewing the route. The tool documentation requires the warning for all displayed walking routes. developers.google.com
11. Mobile web, voice disclosure, post-session display and source access
   Voice-first requirements depend on the interface, not whether it is a native app or mobile website. A voice-first mobile web experience should provide the companion UI with a verbatim conversation history and sources, subject to storage limits. A text-only interface does not acquire voice-announcement obligations simply because it runs on a phone.
   The active-disclosure requirement offers at least one of these methods:
- Visual: display the disclosure whenever Maps grounding supports a voice interaction on a screened device.
- Spoken: announce it at the first supported interaction and at least every six months.
   A persistent explanation must also remain accessible. The documentation does not require both disclosure methods in every case.

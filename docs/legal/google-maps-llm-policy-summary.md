# Google Maps place information in LLM-powered applications: policy summary

> **Provenance.** Supplied by the project owner on 2026-10-13 as the written summary from
> their own lawyer. Saved verbatim below this note. It is a summary of Google's published
> terms, not a contract amendment or a written determination from Google. It has not been
> independently verified against the live Google Maps Platform terms. The agreement,
> service-specific terms and documentation in force at the time of use govern.
> Re-check it before launch and whenever Google's terms change.

---

**Google Maps Place Information in LLM-Powered Applications — Policy Summary**

This summary applies to customers with billing addresses outside the European Economic Area, including Canada. EEA billing accounts have separate terms. The applicable Google Maps Platform agreement, service-specific terms, and documentation govern the customer’s actual use.

**1. Permission to generate LLM responses**

Google Maps Platform’s service-specific terms permit customers to use **Maps Grounding Lite** to ground a large language model with Google Maps information and generate grounded responses for end users. This permission requires compliance with Google’s Generative AI Prohibited Use Policy and inclusion of the associated Google Maps sources. It is a conditional permission under the published agreement and does not constitute certification or endorsement of a particular application.

**2. Ordinary Places API has different restrictions**

Ordinary Places API remains subject to the general restriction against creating content based on Google Maps content. The specific content-generation exception for Maps Grounding Lite does not automatically extend to ordinary Places API.

The practical interpretation is that passing raw Places API responses into an LLM to produce new summaries, comparisons, or recommendations should not be assumed permissible under the standard agreement. Using an LLM to formulate a search request, while displaying the returned Places results directly, is a different integration; it must still satisfy the applicable API requirements.

**3. Requirements for the chosen model**

Maps Grounding Lite is not restricted to Gemini. Another provider’s model or a self-hosted model may be used if it complies with Google’s requirements.

Google Maps content must not be stored or cached by the chosen LLM or used for model training or improvement. The developer must verify the provider’s actual retention, logging, and caching practices. Disabling training alone does not address the separate storage restrictions.

Google Maps content and grounded output must not be used to train, test, validate, fine-tune, develop, or improve machine-learning models or artificial-intelligence systems.

**4. Attribution and source presentation**

Google Maps sources must immediately follow the generated content they support and be accessible within one user interaction. Each place source requires a link preview, Google Maps attribution, and the Google Maps destination supplied in the response.

Required branding, copyright notices, and third-party attribution must remain visible. Google Maps information must be distinguishable from other content.

**5. Storage and caching**

Maps Grounding Lite grounded output may be cached for up to **30 consecutive days**, solely to evaluate or optimize its performance or display within the application. This exception does not provide general permission to retain raw Maps responses, maintain permanent conversation history containing Maps content, or build a persistent knowledge base from it.

For ordinary Places API, place IDs may be stored indefinitely. Latitude and longitude may be cached for up to 30 consecutive days and must then be deleted. This does not establish a general 30-day storage allowance for names, addresses, opening hours, ratings, reviews, or other Places content.

**6. Integrity of grounded output**

Customers must not extract or separate Google Maps content from grounded output, modify or intersperse content with that output, insert interstitial content, or redirect users away from or obstruct the full display of linked destination pages.

Ordinary Maps services also prohibit modifying search results, unauthorized bulk extraction, and recreating Google products without substantial independent value.

**7. Additional Places display requirements**

Ordinary Places content may be displayed without a map. When displayed on a map, it must use a Google Map under the standard non-EEA terms. Required Google Maps attribution must accompany the content.

Photos and reviews require author attribution and access to their individual Google Maps sources. Review ordering and filtering must be explained. For reviews of places in France and French territories, the supplied visit month and year must also be displayed.

When displaying Google-provided AI summaries, users must be able to read the complete supplied summary. Its disclosure must appear immediately below it without alteration. Required information and reporting controls must be provided; review summaries also require the appropriate heading and access to the underlying reviews.

**8. Privacy and user notices**

The application must provide appropriate terms of use and a privacy policy incorporating the required Google terms and privacy notices. Location collection or caching requires express, prior, revocable consent and notice explaining its collection and use. Requests must respect the agreement’s restrictions on supplying personally identifiable information to Google.

**9. Prohibited uses**

The application must not facilitate illegal activities, child exploitation, terrorism, self-harm, non-consensual intimate imagery, privacy or intellectual-property violations, or tracking people without consent. It must not facilitate malware, phishing, service abuse, hateful or sexually explicit content, harassment, violence, fraud, deceptive impersonation, or harmful misinformation. Automated decisions materially harming individual rights in sensitive domains require human supervision.

**10. Operation and responsibility**

The integration requires an appropriately configured Google Cloud project, enabled services, authentication, and applicable billing arrangements. Usage charges and quotas apply.

The developer remains responsible for verifying generated answers, controlling automated API usage, and ensuring compliance by the application, its users, and its model provider. Google does not assume responsibility for the chosen LLM’s accuracy or excessive charges caused by its automated requests.

# Technical SEO and Search Access

Load for crawlability, indexability, rendering, sitemap, canonical, internal-link, structured-data, migration, local/ecommerce, or search-discovery changes.

## Evidence-bearing checks

For each representative public route, inspect the response and rendered page where feasible:

| Check | What to verify |
| --- | --- |
| URL/status | Intended public URL resolves to appropriate content and status |
| Rendering | Essential content and meaningful links survive rendering/hydration |
| Crawl/indexing controls | robots.txt, meta/X-Robots-Tag, auth, CDN/WAF match intent |
| Canonical | Intended equivalent URL without contradictory signals |
| Sitemap | Intended canonical/indexable URLs; truthful update data |
| Links | Important routes reachable through normal links; moved links resolve |
| Not found | Missing routes are genuine not-found responses |
| Page identity | Title, main heading, language, content, and intent align |
| Variants | Pagination/filter/search/locale strategy is deliberate |
| Structured data | Truthful, visible, eligible, and validated for the actual page |
| Private routes | Account/checkout/admin data is protected, not merely hidden |

Record TESTED scope; do not claim site-wide results from a sample. Choose rendering patterns appropriate to the stack and test the result instead of treating any rendering mode as universally required.

## Internal linking and architecture

Map pages to distinct customer jobs, not keyword variations. Use concise descriptive navigation and contextual links. Investigate orphan pages before adding every route to global navigation. Avoid arbitrary click-depth quotas. Preserve valuable routes and incoming links unless evidence supports a change.

## Migration and recovery

Separate URL/domain/framework/design changes where practical. For material moves, capture protected assets, write old-to-new mappings, redirect only to genuinely equivalent destinations, update internal links/canonicals/sitemaps/language signals, test chains/loops, prepare rollback, and obtain production approval. Never use robots.txt as privacy protection or a canonical as a deletion substitute.

## Conditional branches

- **Local:** verify real identity, service area, hours, eligibility, routing, and local substance. Do not invent addresses or interchangeable location pages.
- **Ecommerce:** align price, currency, availability, variants, delivery/returns, feeds, and visible product facts; test inventory/cart/checkout/refund paths.
- **Multilingual:** localize the actual offer and support, define URLs and language alternates deliberately, test switching/fallbacks, and obtain review for consequential translations.
- **High stakes:** require qualified review for consequential health, legal, financial, or safety claims. A disclaimer does not repair an unsupported claim.

Consult current official platform documentation for mutable rules and eligibility; valid markup or correct technical implementation does not guarantee indexing, snippets, or rich results.

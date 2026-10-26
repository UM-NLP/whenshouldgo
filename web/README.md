# WhenShouldGo web (slice 1)

Mobile-first chat screen with a server-driven usage meter (bootstrap on load, updated from each turn; the client never computes entitlements): reply, Google's grounded block verbatim with its sources, and the
foreground location consent flow. Phase 0 only. Nothing is persisted client-side.

## Run (dev, dry run: no keys, no network)

Terminal 1, from `backend/`:

    python scripts/dev_server.py --i-understand-this-is-dev --dry-run

Terminal 2, from `web/`:

    npm install
    npm run dev          # http://127.0.0.1:5173, proxies /v1 to 127.0.0.1:8787

Live: replace the first command with
`python scripts/dev_server.py --i-understand-this-is-dev --env-file C:\path\to\.env --max-spend-usd 0.05`.

## Contract types

`npm run api:generate` exports the backend OpenAPI schema (`backend/scripts/export_openapi.py`)
and regenerates `src/api/schema.d.ts` with openapi-typescript. Re-run it after any backend
contract change and commit both generated files.

## Checks

    npm test
    npm run build

The dev auth provider and the dev server are NON-PRODUCTION. A production build gets a
`NoAuthProvider` and sends no token.


## Hosting headers (production)

Set these at the hosting layer (a `<meta>` CSP cannot set `frame-ancestors`, and a strict meta CSP
would break the Vite dev server's HMR, so none is baked into `index.html`):

    Content-Security-Policy: default-src 'self'; connect-src 'self'; frame-ancestors 'none'
    Permissions-Policy: geolocation=(self)

`connect-src 'self'` assumes the API is served from the same origin (or add its origin
explicitly). Google text is rendered as plain text and Google links are plain `https` anchors.

# Branding: custom logo + app title

The top-left logo and the app title are configurable at **deploy time** (no in-app editor).
With nothing set, the app uses the built-in lucide "Languages" icon and the title
"Doc Translation Review", so existing deployments are unchanged.

## Config values

Set these in `.databricks/bundle/<target>/variable-overrides.json` (they flow through the
secret scope → app env vars → `/api/config`, exactly like the other config values):

| Key            | Meaning                                             | Default                  |
|----------------|-----------------------------------------------------|--------------------------|
| `app_title`    | App title (navbar + browser tab)                    | `Doc Translation Review` |
| `app_logo_url` | Served path to the logo asset; blank → lucide icon  | *(unset)*                |
| `app_logo_alt` | Label / alt text for the logo; blank → the title    | *(unset)*                |

## Providing the logo image

The logo is shipped as a bundled static asset:

1. Drop your image in `frontend/public/`, e.g. `frontend/public/brand-logo.png`
   (PNG or SVG recommended; keep it small — it renders in a 32×32 box, `object-contain`).
   Files in `frontend/public/` are copied to `static/` by Vite at build and served at the
   site root.
2. Set `app_logo_url` to the served path — for the file above that's `/brand-logo.png` — and
   optionally `app_logo_alt` and `app_title`.
3. Redeploy. Because `deploy.sh` skips the frontend build when `static/` already exists, force
   a rebuild so the new asset (and any frontend change) is copied in:
   ```
   FORCE_BUILD=1 ./deploy.sh
   ```

## Wanting runtime logo swaps later?

If you ever want to change the logo without a rebuild, host it in the UC Volume under
`branding/` and serve it via a small `GET /api/branding/logo` endpoint (`volume.read_docx` +
`StreamingResponse` with an extension-inferred content-type), then point `app_logo_url` at
that endpoint. Not needed for the deploy-time model above.

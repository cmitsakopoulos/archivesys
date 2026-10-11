# Repository Guidelines & Instructions

## Frontend Cache Busting Rule
- **Always bump asset version numbers**: Whenever modifying frontend static assets in `src/curation/static/` (such as `studio.js` or `studio.css`), **always bump the cache-busting version query string** (`?v=YYYYMMDD_X`) in [studio.html](src/curation/static/studio.html).
- Web browsers (Chrome, Safari, Firefox) aggressively cache `.js` and `.css` files on `localhost`. Without bumping the query string version, the browser serves stale cached files and changes are not visible to the user.

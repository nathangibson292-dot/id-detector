# IDea web templates

`home.html`, `index.html` and `job.html` own the FastAPI page structure. The established
presentation helpers in `id_detector.present.server` and `id_detector.present.theme` still supply
the theme, form, card, player and footer fragments, so the pages keep their exact look. Those
fragments escape every user-controlled value themselves; each is passed with `| safe`, and every
other Jinja value is autoescaped.

The shared stylesheet and page script are not inlined (4a-iii, U-F29): the pages link the
versioned `/static/app.<hash>.css` and `/static/app.<hash>.js` the application serves as
immutable, so a browser fetches them once. The only inline script left is the job page's
per-job constants (`config`), which the page's Content-Security-Policy names by hash.

Result pages are not templates: they are immutable, pre-rendered bundles served byte-for-byte.

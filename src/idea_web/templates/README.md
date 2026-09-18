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

`login.html`, `set_password.html` and `admin.html` are hosted-mode pages only (4c-i, 4c-ii); local
mode never renders them. They have no inline script at all, and every value they show — an email
address, an admin's comment, an error that echoes what was typed — is autoescaped. The only
`| safe` values are the web layer's own head, bar and account-strip fragments.

Result pages are not templates: they are immutable, pre-rendered bundles served byte-for-byte.

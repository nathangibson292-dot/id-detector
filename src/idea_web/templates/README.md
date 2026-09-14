# IDea web templates

`home.html`, `index.html` and `job.html` own the FastAPI page structure. The established
presentation helpers in `id_detector.present.server` and `id_detector.present.theme` still supply
the theme, form, card, player and footer fragments, so the pages keep their exact look. Those
fragments escape every user-controlled value themselves; each is passed with `| safe`, and every
other Jinja value is autoescaped.

Result pages are not templates: they are immutable, pre-rendered bundles served byte-for-byte.

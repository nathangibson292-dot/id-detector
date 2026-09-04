"""Stage 6 enrichment: non-authoritative "where to get it" acquisition links.

Enrichment attaches candidate acquisition links (direct catalogue links, labelled search links, and
SoundCloud acquisition flags) to already-identified episodes.  It never rewrites an episode's
identity; a recording-specific id fed back into the identity graph may only raise ``version_status``
when the plan's corroboration rule (≥ 2 independent sources or a held reference) is met.
"""

from id_detector.enrich.run import AcquireResult, build_acquire, enrich_media_dir

__all__ = ["AcquireResult", "build_acquire", "enrich_media_dir"]

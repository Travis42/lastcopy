"""lastcopy — last-copy registry prototype (M1: keyless OL+IA+Wikidata pipeline)."""

__version__ = "0.1.0"

USER_AGENT = "lastcopy/0.1 (+https://github.com/lastcopy)"
CACHE_TTL_DAYS = 30
MIN_INTERVAL_PER_HOST = 1.0  # seconds between requests to the same host
JITTER_MAX = 0.25  # extra random delay 0..JITTER_MAX seconds

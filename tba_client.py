"""The Blue Alliance API client for looking up match videos."""

import logging

import requests
from requests.adapters import HTTPAdapter, Retry

logger = logging.getLogger(__name__)

TBA_BASE = "https://www.thebluealliance.com/api/v3"

# Double elimination match number -> TBA comp level mapping
# Based on AdvantageScope's elimination match key logic
# For 8-team double elimination (playoff_type=10), the sequential match numbers
# map to specific comp levels and set numbers.
_DOUBLE_ELIM_8_MAP = {
    1: ("sf", 1, 1),   # Upper bracket round 1 match 1
    2: ("sf", 2, 1),   # Upper bracket round 1 match 2
    3: ("sf", 3, 1),   # Upper bracket round 1 match 3
    4: ("sf", 4, 1),   # Upper bracket round 1 match 4
    5: ("sf", 5, 1),   # Lower bracket round 1 match 1
    6: ("sf", 6, 1),   # Lower bracket round 1 match 2
    7: ("sf", 7, 1),   # Upper bracket round 2 match 1
    8: ("sf", 8, 1),   # Upper bracket round 2 match 2
    9: ("sf", 9, 1),   # Lower bracket round 2 match 1
    10: ("sf", 10, 1),  # Lower bracket round 2 match 2
    11: ("sf", 11, 1),  # Upper bracket final
    12: ("sf", 12, 1),  # Lower bracket round 3
    13: ("sf", 13, 1),  # Lower bracket final
    14: ("f", 1, 1),    # Grand final 1
    15: ("f", 1, 2),    # Grand final 2
    16: ("f", 1, 3),    # Grand final 3 (if needed)
}


def _make_session(api_key: str) -> requests.Session:
    """Create a requests session with retry logic and TBA auth."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({
        "X-TBA-Auth-Key": api_key,
        "Accept": "application/json",
    })
    session.timeout = 15
    return session


class TBAClient:
    """Client for The Blue Alliance API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session = _make_session(api_key)
        # Cache: year -> list of events
        self._events_cache: dict[int, list[dict]] = {}
        # Cache: tba_event_key -> event detail
        self._event_detail_cache: dict[str, dict] = {}

    def _get(self, path: str) -> dict | list:
        """Make a GET request to the TBA API."""
        url = f"{TBA_BASE}{path}"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_tba_event_key(self, first_event_code: str, year: int) -> str | None:
        """Convert a FIRST event code to a TBA event key.

        e.g., "MIBKN" + 2026 -> "2026mibkn"
        """
        if year not in self._events_cache:
            try:
                events = self._get(f"/events/{year}")
                self._events_cache[year] = events
            except requests.RequestException as e:
                logger.error("Failed to fetch events for %d: %s", year, e)
                raise

        code_lower = first_event_code.lower()
        for event in self._events_cache[year]:
            if (event.get("first_event_code") or "").lower() == code_lower:
                return event["key"]

        logger.warning(
            "Could not find TBA event key for %s in %d", first_event_code, year
        )
        return None

    def get_event_playoff_type(self, tba_event_key: str) -> int | None:
        """Get the playoff type for an event."""
        if tba_event_key not in self._event_detail_cache:
            try:
                detail = self._get(f"/event/{tba_event_key}")
                self._event_detail_cache[tba_event_key] = detail
            except requests.RequestException as e:
                logger.error("Failed to fetch event %s: %s", tba_event_key, e)
                raise
        return self._event_detail_cache[tba_event_key].get("playoff_type")

    def get_match_key(
        self, tba_event_key: str, match_type: str, match_number: int
    ) -> str | None:
        """Build a TBA match key from event key, match type, and number.

        match_type: "Q" for qualification, "E" for elimination
        """
        if match_type == "Q":
            return f"{tba_event_key}_qm{match_number}"

        # Elimination: need playoff type to map sequential number
        try:
            playoff_type = self.get_event_playoff_type(tba_event_key)
        except requests.RequestException:
            return None

        if playoff_type == 10:
            # Double elimination 8-team (standard since 2023)
            mapping = _DOUBLE_ELIM_8_MAP.get(match_number)
            if mapping:
                comp_level, set_num, match_num = mapping
                return f"{tba_event_key}_{comp_level}{set_num}m{match_num}"
            else:
                logger.warning(
                    "Unknown elimination match number %d for double elim",
                    match_number,
                )
                return None
        else:
            # For other playoff types, try a simple mapping
            # This covers best-of-3 bracket formats
            logger.warning(
                "Playoff type %s not fully supported, attempting best guess",
                playoff_type,
            )
            # Fall back: assume it's a semifinal or final
            if match_number <= 12:
                set_num = (match_number - 1) // 2 + 1
                match_in_set = (match_number - 1) % 2 + 1
                return f"{tba_event_key}_sf{set_num}m{match_in_set}"
            else:
                match_in_set = match_number - 12
                return f"{tba_event_key}_f1m{match_in_set}"

    def get_match_video_ids(self, match_key: str) -> list[str]:
        """Get YouTube video IDs for a match from TBA.

        Returns a list of YouTube video IDs (may be empty).
        """
        try:
            match_data = self._get(f"/match/{match_key}")
        except requests.RequestException as e:
            logger.error("Failed to fetch match %s: %s", match_key, e)
            raise

        videos = match_data.get("videos", [])
        youtube_ids = [
            v["key"] for v in videos
            if v.get("type") == "youtube" and v.get("key")
        ]
        return youtube_ids

    def get_video_urls_for_log(self, match_info: dict) -> list[str]:
        """Given a parsed match log dict, return YouTube URLs for the match.

        Returns list of YouTube URLs, or empty list if not found.
        Raises requests.RequestException on network errors.
        """
        event_key = self.get_tba_event_key(match_info["event"], match_info["year"])
        if not event_key:
            return []

        match_key = self.get_match_key(
            event_key, match_info["match_type"], match_info["match_number"]
        )
        if not match_key:
            return []

        logger.info("Looking up videos for TBA match: %s", match_key)
        video_ids = self.get_match_video_ids(match_key)
        return [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]

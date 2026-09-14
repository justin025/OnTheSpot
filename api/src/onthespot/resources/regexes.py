# ---------------------------------------------------------------------------
# Compiled URL regular expressions
# ---------------------------------------------------------------------------
import re

# Audio services
APPLE_MUSIC_URL_REGEX = re.compile(
    r"https?://music.apple.com/([a-z]{2})/(?P<type>album|playlist|artist)"
    r"(?:/(?P<title>[^/]+))?/(?P<id>[\w.-]+)"
    r"(?:\?i=(?P<track_id>\d+))?(?:&.*)?$"
)
BANDCAMP_URL_REGEX = re.compile(
    r"https?://[a-z0-9-]+.bandcamp.com(?:/(?P<type>track|album|music)/[a-z0-9-]+)?"
)
DEEZER_URL_REGEX = re.compile(
    r"https?://www.deezer.com/(?:[a-z]{2}/)?(?P<type>album|playlist|track|artist)/(?P<id>\d+)"
)
DEEZER_SHARE_URL_REGEX = re.compile(r"https?://link.deezer.com/s/([-a-z0-9]+)")
QOBUZ_URL_REGEX = re.compile(
    r"https?://(www.|play.|open.)?qobuz.com/(?:[a-z]{2}-[a-z]{2}/)?"
    r"(?P<type>album|playlist|artist|track|label|interpreter)"
    r"(?:/[^/]+)?(?:/[^/]+)?/(?P<id>[-a-z0-9]+)"
)
SOUNDCLOUD_URL_REGEX = re.compile(r"https?://(m.)?soundcloud.com/[-\w:/]+")
SPOTIFY_URL_REGEX = re.compile(
    r"https?://open.spotify.com/(intl-([a-zA-Z]+)/|)"
    r"(?P<type>track|album|artist|playlist|episode|show)/(?P<id>[0-9a-zA-Z]{22})"
    r"(\?si=.+?)?$"
)
TIDAL_URL_REGEX = re.compile(
    r"https?://(www.|listen.)?tidal.com/(browse/)?"
    r"(?P<type>album|track|artist|playlist|mix)/(?P<id>[-a-z0-9]+)"
)
YOUTUBE_MUSIC_URL_REGEX = re.compile(
    r"https?://music.youtube.com/"
    r"(watch\?v=(?P<video_id>[a-zA-Z0-9_-]+)"
    r"|channel/(?P<channel_id>[a-zA-Z0-9_-]+)"
    r"|playlist\?list=(?P<playlist_id>[a-zA-Z0-9_-]+))"
)
YOUTUBE_URL_REGEX = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?(?:[^#]*?&)?v=|shorts/|live/)|youtu\.be/)"
    r"(?P<video_id>[a-zA-Z0-9_-]+)"
)

# Video services
CRUNCHYROLL_URL_REGEX = re.compile(
    r"https?://(www.)?crunchyroll.com/(?P<type>watch|series)/(musicvideo/)?"
    r"(?P<id>[-A-Z0-9]+)/(?P<title>[-a-z0-9]+)"
)

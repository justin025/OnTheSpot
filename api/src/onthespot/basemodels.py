from pydantic import BaseModel


# Pydantic schemas of body data
class AccountData(BaseModel):
    username: str | None = None
    token: str | None = None


class SpotifyCompanionLogin(BaseModel):
    pairing_token: str
    login: dict[str, Any]


class YouTubeAuthentication(BaseModel):
    mode: str = "none"
    browser: str | None = None
    cookie_file: str | None = None


## Queue Models
class QueueOrder(BaseModel):
    local_ids: list[str]


class QueueBatch(BaseModel):
    local_ids: list[str]
    action: str
    priority: int | None = None
    profile_id: str | None = None


class QueueVerify(BaseModel):
    local_ids: list[str] = []
    retry: bool = True


## Profiles Models
class DownloadProfile(BaseModel):
    id: str
    name: str
    format: str = "mp3"
    bitrate: int = 320
    download_path: str = ""


class ActiveProfile(BaseModel):
    profile_id: str


## Library Models
class LibraryPath(BaseModel):
    path: str


class LibraryPaths(BaseModel):
    paths: list[str] = []


class LibraryVerify(BaseModel):
    paths: list[str] = []


class LibraryRename(BaseModel):
    path: str
    new_name: str


class LibraryMetadata(BaseModel):
    path: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    genre: str | None = None
    year: str | int | None = None
    release_date: str | None = None
    track_number: str | int | None = None
    disc_number: str | int | None = None
    lyrics: str | None = None


class LibraryM3U(BaseModel):
    name: str
    paths: list[str]


class LibraryOpen(BaseModel):
    path: str
    action: str = "folder"


## Config Models


class AccountLogin(BaseModel):
    client_id: str | None = None
    app_version: str | None = None
    app_locale: str | None = None


class Account(BaseModel):
    uuid: str
    service: str
    active: bool = True
    login: AccountLogin | None = None


class AppSettings(BaseModel):
    version: str = "v2.0.0 Alpha 2"
    debug_mode: bool = False
    language_index: int = 0
    total_downloaded_items: int = 0
    total_downloaded_data: int = 0
    m3u_format: str = "m3u8"
    use_double_digit_path_numbers: bool = False
    ffmpeg_args: list[str] = Field(default_factory=list)
    active_account_number: int = 0
    accounts: list[Account] = Field(default_factory=list)

    language: str = "en_US"
    theme: str = "dark"
    active_download_profile: str = "mp3-320"
    export_folder_path: str = ""
    playlist_backup_folder_path: str = ""
    youtube_auth_mode: str = "none"
    youtube_cookies_browser: str = ""
    youtube_cookies_file: str = ""
    download_profiles: list[DownloadProfile] = Field(default_factory=list)
    explicit_label: str = "🅴"
    download_copy_btn: bool = False
    download_open_btn: bool = False
    download_locate_btn: bool = True
    download_delete_btn: bool = True
    show_search_thumbnails: bool = False
    show_download_thumbnails: bool = True
    thumbnail_size: int = 60
    max_search_results: int = 10
    disable_download_popups: bool = False
    windows_10_explorer_thumbnails: bool = False
    mirror_spotify_playback: bool = False

    check_for_updates: bool = True
    update_repository: str = "ots-downloader/onthespot"
    update_check_interval_hours: int = 12
    illegal_character_replacement: str = "-"
    raw_media_download: bool = False
    rotate_active_account_number: bool = False
    download_delay: int = 10
    download_delay_variance: int = 5
    download_chunk_size: int = 50000
    maximum_queue_workers: int = 1
    maximum_download_workers: int = 1
    enable_retry_worker: bool = False
    retry_worker_delay: int = 5
    api_retry_max_attempts: int = 3
    api_retry_base_delay: int = 2
    api_retry_max_delay: int = 60
    api_request_delay: int = 1
    cache_api_calls: bool = True
    api_response_cache_ttl_seconds: int = 86400
    spotify_metadata_cache_ttl_seconds: int = 604800
    spotify_search_cache_ttl_seconds: int = 900
    playlist_automation_cache_ttl_seconds: int = 60
    spotify_connect_port: int = 6768
    spotify_webapi_override_client_id: str = ""
    spotify_webapi_override_client_secret: str = ""
    cache_metadata_in_queue: bool = True
    fetch_genre_metadata: bool = False
    fetch_extended_album_metadata: bool = False
    fetch_audio_features: bool = False
    fetch_track_credits: bool = False
    enable_search_tracks: bool = True
    enable_search_albums: bool = True
    enable_search_playlists: bool = True
    enable_search_artists: bool = True
    enable_search_episodes: bool = True
    enable_search_podcasts: bool = True
    enable_search_audiobooks: bool = True
    f_search_tracks: bool = False
    f_search_albums: bool = False
    f_search_artists: bool = False
    f_search_playlists: bool = False
    search_prefix: str = "the"
    download_queue_show_waiting: bool = True
    download_queue_show_failed: bool = True
    download_queue_show_cancelled: bool = True
    download_queue_show_unavailable: bool = True
    download_queue_show_completed: bool = True
    audio_download_path: str = "/root/Music/OnTheSpot"
    track_path_formatter: str = "Tracks/{album_artist}/{year} {album}/{track_number}. {name}"
    podcast_file_format: str = "mp3"
    podcast_path_formatter: str = "Episodes/{album}/{name}"
    use_playlist_path: bool = False
    playlist_path_formatter: str = "Playlists/{playlist_name} by {playlist_owner}/{playlist_number}. {name} - {artist}"
    create_m3u_file: bool = False
    m3u_path_formatter: str = "M3U/{playlist_name} by {playlist_owner}"
    extinf_separator: str = "; "
    extinf_label: str = "{playlist_number}. {artist} - {name}"
    save_album_cover: bool = False
    album_cover_format: str = "png"
    file_hertz: int = 44100
    use_custom_file_bitrate: bool = False
    use_source_format: bool = False
    prefer_best_source_format: bool = True
    download_lyrics: bool = False
    only_download_synced_lyrics: bool = False
    only_download_plain_lyrics: bool = False
    save_lrc_file: bool = False
    translate_file_path: bool = False
    metadata_separator: str = "; "
    overwrite_existing_metadata: bool = False
    embed_branding: bool = False
    embed_cover: bool = True
    embed_artist: bool = True
    embed_album: bool = True
    embed_albumartist: bool = True
    embed_name: bool = True
    embed_year: bool = True
    embed_discnumber: bool = True
    embed_tracknumber: bool = True
    embed_genre: bool = True
    embed_performers: bool = False
    embed_producers: bool = False
    embed_writers: bool = True
    embed_composer: bool = True
    prefer_composer_as_album_artist: bool = False
    shorten_composer_tag: bool = False
    embed_label: bool = True
    embed_copyright: bool = True
    embed_description: bool = True
    embed_language: bool = True
    embed_isrc: bool = True
    embed_length: bool = True
    embed_url: bool = True
    embed_key: bool = False
    embed_bpm: bool = False
    embed_compilation: bool = False
    embed_lyrics: bool = False
    embed_explicit: bool = False
    embed_upc: bool = False
    embed_service_id: bool = False
    embed_timesignature: bool = False
    embed_acousticness: bool = False
    embed_danceability: bool = False
    embed_energy: bool = False
    embed_instrumentalness: bool = False
    embed_liveness: bool = False
    embed_loudness: bool = False
    embed_speechiness: bool = False
    embed_valence: bool = False
    video_download_path: str = "/root/Videos/OnTheSpot"
    movie_file_format: str = "mkv"
    movie_path_formatter: str = "Movies/{name} ({release_year})"
    show_file_format: str = "mkv"
    show_path_formatter: str = "Shows/{show_name}/Season {season_number}/{episode_number}. {name}"
    preferred_video_resolution: int = 1080
    download_subtitles: bool = False
    download_chapters: bool = False
    preferred_audio_language: str = "en-US"
    preferred_subtitle_language: str = "en-US"
    download_all_available_audio: bool = False
    download_all_available_subtitles: bool = False
    v2a_enable: bool = False
    v2a_preferred_codec: str = "mp3"
    v2a_preferred_bitrate: int = 192

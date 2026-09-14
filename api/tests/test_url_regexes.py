import unittest

from _support import TEST_ROOT  # noqa: F401

from onthespot.resources.regexes import APPLE_MUSIC_URL_REGEX  # noqa: E402


class AppleMusicUrlRegexTests(unittest.TestCase):
    def test_percent_encoded_playlist_slug(self):
        match = APPLE_MUSIC_URL_REGEX.search(
            "https://music.apple.com/ru/playlist/%D0%BF%D0%B5%D1%81%D0%BD%D0%B8/pl.a3f7a612412545759582b0c9ac082a0d"
        )

        self.assertIsNotNone(match)
        self.assertEqual(match.group("type"), "playlist")
        self.assertEqual(match.group("id"), "pl.a3f7a612412545759582b0c9ac082a0d")


if __name__ == "__main__":
    unittest.main()

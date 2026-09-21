"""VA × NetMirror addon tests — offline, mocked sources."""

import json
import sys
import unittest
from unittest import mock

sys.path.insert(0, "/home/user/va-netmirror-addon")
import addon  # noqa: E402


def reset_caches():
    addon._VA_CACHE.clear()


class FakeResponse:
    def __init__(self, status=200, body="", json_value=None):
        self.status_code = status
        self._body = body if isinstance(body, bytes) else str(body).encode()
        self._json = json_value

    @property
    def text(self):
        return self._body.decode("utf-8", "ignore")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, _n):
        yield self._body[:64]

    def close(self):
        pass


class TokenTests(unittest.TestCase):
    def test_roundtrip_defaults(self):
        t = addon.encode_cfg({"va": True, "nm": True, "mb": True})
        self.assertEqual(addon.decode_cfg(t),
                         {"va": True, "nm": True, "mb": True})

    def test_legacy_two_key_token_gains_mb_default_on(self):
        t = addon.encode_cfg({"va": False, "nm": True})   # pre-1.1.0 token
        self.assertEqual(addon.decode_cfg(t),
                         {"va": False, "nm": True, "mb": True})

    def test_partial_and_invalid(self):
        t = addon.encode_cfg({"va": True, "nm": False, "mb": False})
        self.assertEqual(addon.decode_cfg(t),
                         {"va": True, "nm": False, "mb": False})
        self.assertIsNone(addon.decode_cfg("!!!not-base64-json!!!"))

    def test_cfg_from_path(self):
        t = addon.encode_cfg({"va": False, "nm": True, "mb": True})
        cfg, rest = addon.cfg_from_path("/cfg-%s/stream/movie/tt1.json" % t)
        self.assertEqual(cfg, {"va": False, "nm": True, "mb": True})
        self.assertEqual(rest, "/stream/movie/tt1.json")
        cfg2, rest2 = addon.cfg_from_path("/stream/movie/tt1.json")
        self.assertEqual(cfg2, {"va": True, "nm": True, "mb": True})


class ManifestTests(unittest.TestCase):
    def test_configurable_and_description(self):
        m = addon.manifest_for({"va": True, "nm": False})
        self.assertTrue(m["behaviorHints"]["configurable"])
        self.assertEqual(m["catalogs"], [])
        self.assertIn("VA Player", m["description"])
        self.assertNotIn("NetMirror:", m["description"].split("Sources: ")[1])

    def test_base_fields(self):
        m = addon.MANIFEST_BASE
        self.assertEqual(m["idPrefixes"], ["tt"])
        self.assertEqual(m["resources"], ["stream"])


class VaSourceTests(unittest.TestCase):
    def setUp(self):
        reset_caches()

    def test_three_urls_become_probed_cards(self):
        urls = ["https://cdn.example/pl/1", "https://cdn.example/pl/2",
                "https://cdn.example/pl/3"]

        def route(url, **kw):
            if "vaplayer.ru" in url:
                return FakeResponse(json_value={
                    "status_code": "200",
                    "data": {"title": "Inception 2010", "stream_urls": urls}})
            # the HLS probe
            return FakeResponse(body=b"#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS")
        with mock.patch.object(addon.HTTP, "get", side_effect=route):
            cards = addon.va_streams("tt1375666", "movie")
        self.assertEqual(len(cards), 3)
        self.assertEqual(cards[0]["name"], "▶️ VA · Server 1")
        self.assertIn("Inception 2010", cards[0]["title"])
        self.assertFalse(cards[0]["behaviorHints"]["notWebReady"])

    def test_bad_status_means_empty(self):
        with mock.patch.object(addon.HTTP, "get",
                               return_value=FakeResponse(json_value={
                                   "status_code": "404", "data": {}})):
            self.assertEqual(addon.va_streams("tt0000000", "movie"), [])

    def test_series_passes_season_episode(self):
        seen = {}

        def route(url, **kw):
            if "vaplayer.ru" in url:
                seen.update(kw.get("params") or {})
                return FakeResponse(json_value={
                    "status_code": "200",
                    "data": {"title": "GoT", "stream_urls": []}})
            return FakeResponse(body=b"#EXTM3U")
        with mock.patch.object(addon.HTTP, "get", side_effect=route):
            addon.va_streams("tt0944947", "series", 1, 3)
        self.assertEqual(seen.get("type"), "tv")
        self.assertEqual(seen.get("season"), "1")
        self.assertEqual(seen.get("episode"), "3")

    def test_cache_positive_and_negative(self):
        urls = ["https://cdn.example/pl/x"]

        def route(url, **kw):
            if "vaplayer.ru" in url:
                return FakeResponse(json_value={
                    "status_code": "200",
                    "data": {"title": "T", "stream_urls": urls}})
            return FakeResponse(body=b"#EXTM3U")
        with mock.patch.object(addon.HTTP, "get", side_effect=route) as g:
            addon.va_streams("tt1", "movie")
            addon.va_streams("tt1", "movie")
        self.assertEqual(g.call_count, 2)  # 1 api + 1 probe, then cached


class MergeTests(unittest.TestCase):
    def setUp(self):
        reset_caches()

    def test_both_sources_merge(self):
        va = [{"name": "▶️ VA · Server 1", "url": "https://va/x.m3u8",
               "behaviorHints": {"notWebReady": False}}]
        nm = [{"name": "NM card", "url": "https://nm/x.m3u8",
               "behaviorHints": {}}]
        with mock.patch.object(addon, "va_streams", return_value=va), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=nm):
            out = addon.build_streams({"va": True, "nm": True,
                                       "mb": False}, "movie", "tt1375666")
        self.assertEqual([c["name"] for c in out["streams"]],
                         ["▶️ VA · Server 1", "NM card"])

    def test_selection_respected(self):
        with mock.patch.object(addon, "va_streams") as va, \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=[]) as nm:
            out = addon.build_streams({"va": True, "nm": False,
                                       "mb": False}, "movie", "tt1")
            va.assert_called_once()
            nm.assert_not_called()
        self.assertEqual(out["streams"], [])

    def test_relative_nm_urls_rebased(self):
        nm = [{"name": "rel", "url": "/hls/abc/master.m3u8",
               "behaviorHints": {}}]
        with mock.patch.object(addon, "va_streams", return_value=[]), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=nm):
            out = addon.build_streams({"va": False, "nm": True,
                                       "mb": False}, "movie", "tt2")
        self.assertTrue(out["streams"][0]["url"].startswith("https://"))

    def test_empty_message_mentions_notes(self):
        with mock.patch.object(addon, "va_streams", return_value=[]), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=[]):
            out = addon.build_streams({"va": True, "nm": True,
                                       "mb": False}, "movie", "tt0")
        self.assertIn("VA", out["message"])
        self.assertIn("NetMirror", out["message"])


class MovieBoxMergeTests(unittest.TestCase):
    def setUp(self):
        reset_caches()

    def test_mb_cards_relabeled_and_rebased(self):
        mb = {"streams": [{"name": "♧ FHD 1080p  ✹ Inception",
                           "url": "/hls/123456789/1/1/master.m3u8",
                           "behaviorHints": {"notWebReady": True}}]}
        with mock.patch.object(addon, "va_streams", return_value=[]), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=[]), \
             mock.patch.object(addon.mb_core, "build_streams",
                               return_value=mb):
            out = addon.build_streams(
                dict(addon.DEFAULTS), "movie", "tt1375666",
                host_base="https://vnh.example")
        self.assertEqual(len(out["streams"]), 1)
        c = out["streams"][0]
        self.assertTrue(c["name"].startswith("📦"))
        self.assertFalse(c["name"].startswith("♧"))
        self.assertEqual(c["url"],
                         "https://vnh.example/hls/123456789/1/1/master.m3u8")

    def test_mb_off_means_no_call(self):
        with mock.patch.object(addon, "va_streams", return_value=[]), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=[]), \
             mock.patch.object(addon.mb_core, "build_streams") as mbc:
            out = addon.build_streams({"va": False, "nm": True,
                                       "mb": False}, "movie", "tt1")
            mbc.assert_not_called()
        self.assertEqual(out["streams"], [])

    def test_mb_error_is_honest_note_not_crash(self):
        with mock.patch.object(addon, "va_streams", return_value=[]), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=[]), \
             mock.patch.object(addon.mb_core, "build_streams",
                               side_effect=RuntimeError("platform down")):
            out = addon.build_streams(dict(addon.DEFAULTS),
                                      "movie", "tt1")
        self.assertIn("MovieBox error", out["message"])

    def test_mb_se_ep_passthrough(self):
        with mock.patch.object(addon, "va_streams", return_value=[]), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=[]), \
             mock.patch.object(addon.mb_core, "build_streams",
                               return_value=[]) as mbc:
            addon.build_streams(dict(addon.DEFAULTS), "series", "tt9",
                                2, 7)
        mbc.assert_called_once_with("series", "tt9", 2, 7)

    def test_mb_direct_urls_untouched(self):
        mb = {"streams": [{"name": "♧ SD 480p ✹ X",
                           "url": "https://sbcdn.example/dash/x/index.mpd"}]}
        with mock.patch.object(addon, "va_streams", return_value=[]), \
             mock.patch.object(addon.nm_core, "streams_for_tt",
                               return_value=[]), \
             mock.patch.object(addon.mb_core, "build_streams",
                               return_value=mb):
            out = addon.build_streams(dict(addon.DEFAULTS),
                                      "movie", "tt3")
        self.assertEqual(out["streams"][0]["url"],
                         "https://sbcdn.example/dash/x/index.mpd")


class HlsRouteTests(unittest.TestCase):
    def test_hls_route_serves_mb_playlist_text(self):
        import threading
        import http.client
        calls = []

        def fake_lazy(sid, se, ep, kind):
            calls.append((sid, se, ep, kind))
            return "#EXTM3U\n#EXT-X-VERSION:7\n"

        server = addon.ThreadingHTTPServer(("127.0.0.1", 0), addon.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(addon.mb_core, "_lazy_hls",
                                   side_effect=fake_lazy):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/hls/123456789/1/1/master.m3u8")
                r = c.getresponse()
                body = r.read()
                c.close()
            self.assertEqual(r.status, 200)
            self.assertEqual(body, b"#EXTM3U\n#EXT-X-VERSION:7\n")
            self.assertIn("mpegurl", r.getheader("Content-Type"))
            self.assertEqual(calls, [("123456789", 1, 1, "master")])
            # miss -> honest HLS 404
            with mock.patch.object(addon.mb_core, "_lazy_hls",
                                   return_value=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/hls/123456789/1/1/v0.m3u8")
                r = c.getresponse()
                body = r.read()
                c.close()
            self.assertEqual(r.status, 404)
            self.assertTrue(body.startswith(b"#EXTM3U"))
        finally:
            server.shutdown()
            server.server_close()


class ServerSmokeTests(unittest.TestCase):
    def test_configure_manifest_and_stream(self):
        import threading
        import http.client
        server = addon.ThreadingHTTPServer(("127.0.0.1", 0), addon.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def get(path):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", path)
                r = c.getresponse()
                body = r.read()
                c.close()
                return r.status, body
            st, body = get("/configure")
            self.assertEqual(st, 200)
            self.assertIn(b"VA Player", body)
            self.assertIn(b"MovieBox", body)
            t = addon.encode_cfg({"va": False, "nm": True, "mb": True})
            st, body = get("/cfg-%s/manifest.json" % t)
            m = json.loads(body)
            self.assertIn("NetMirror", m["description"])
            st, body = get("/health")
            self.assertEqual(json.loads(body)["ok"], True)
            self.assertEqual(json.loads(body)["version"], addon.VERSION)
            st, body = get("/nope")
            self.assertEqual(st, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

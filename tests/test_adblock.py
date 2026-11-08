import pytest

from alpekabot.ui.adblock import is_ad_request


@pytest.mark.parametrize("host, path", [
    ("googleads.g.doubleclick.net", "/pagead/id"),
    ("static.doubleclick.net", "/instream/ad_status.js"),
    ("pagead2.googlesyndication.com", "/pagead/js/adsbygoogle.js"),
    ("www.googleadservices.com", "/pagead/conversion/"),
    ("imasdk.googleapis.com", "/js/sdkloader/ima3.js"),
    ("www.youtube.com", "/pagead/viewthroughconversion/123"),
    ("www.youtube.com", "/api/stats/ads"),
    ("www.youtube.com", "/ptracking"),
    ("www.youtube.com", "/get_midroll_info"),
    ("www.youtube-nocookie.com", "/pagead/1p-user-list/1"),
    ("WWW.YOUTUBE.COM.", "/api/stats/ads"),
])
def test_blocks_ads(host, path):
    assert is_ad_request(host, path)


@pytest.mark.parametrize("host, path", [
    ("www.youtube.com", "/watch"),
    ("www.youtube.com", "/embed/abc"),
    ("www.youtube.com", "/iframe_api"),
    ("www.youtube.com", "/youtubei/v1/player"),
    ("www.youtube.com", "/api/stats/playback"),
    ("rr3---sn-abc.googlevideo.com", "/videoplayback"),
    ("i.ytimg.com", "/vi/abc/hqdefault.jpg"),
    ("www.gstatic.com", "/youtube/img/x.png"),
    ("notdoubleclick.net", "/x"),            # no es subdominio
    ("evil.com", "/pagead/x"),               # ruta de anuncios, pero no en YouTube
    ("youtube.com.evil.com", "/pagead/x"),
    ("127.0.0.1", "/player"),
    ("", ""),
])
def test_allows_everything_else(host, path):
    assert not is_ad_request(host, path)

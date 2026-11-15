from alpekabot.ui.youtube_player import WebPlayer


def test_pasar_a_un_video_de_la_pagina_de_youtube_detiene_el_incrustado(monkeypatch):
    """Bug: el incrustado seguía sonando con la canción anterior y se oían dos a la vez."""
    p = WebPlayer()
    sent, opened = [], []
    monkeypatch.setattr(p, "_embed_js", sent.append)
    monkeypatch.setattr(p, "_open_watch", opened.append)
    p.load("aaaaaaaaaaa", 50)                    # suena en el incrustado
    p._watch_only.add("bbbbbbbbbbb")
    p.load("bbbbbbbbbbb", 50)                    # «Siguiente» → vídeo que solo va en youtube.com
    assert sent[-1] == "stopVideo()" and opened == ["bbbbbbbbbbb"]

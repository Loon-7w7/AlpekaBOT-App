"""Bus de eventos central basado en señales de Qt.

Las plataformas emiten aquí; la interfaz y (en fases siguientes) comandos,
moderación y estadísticas se suscriben. Todo corre en el hilo de Qt gracias a
qasync, así que no hacen falta locks.
"""

from PySide6.QtCore import QObject, Signal


class EventBus(QObject):
    # ChatMessage
    chat_message = Signal(object)
    # (Platform, ConnectionState, str detalle)
    connection_state = Signal(object, object, str)
    # (Platform, AccountRole, AccountInfo | None)
    account_changed = Signal(object, object, object)
    # StreamInfo (cambia al entrar/salir de directo o al cambiar título/categoría)
    stream_status = Signal(object)
    # La lista de comandos / timers cambió (desde la app o desde el chat)
    commands_changed = Signal()
    timers_changed = Signal()
    # Song requests: cola / respaldo / lista negra cambiaron
    songs_changed = Signal()
    # SongStatus (canción actual, pausa, volumen)
    song_status = Signal(object)
    # SongQueued: una petición del chat entró en la cola (tarjeta en el chat)
    song_queued = Signal(object)
    # Moderación: ModAction recién ejecutada / historial o palabras prohibidas cambiaron
    mod_action = Signal(object)
    mod_actions_changed = Signal()
    banned_words_changed = Signal()
    # Fase 7: ChannelEvent (canje, follow, sub, bits, raid...) / reacciones / estado de VTube Studio
    channel_event = Signal(object)
    reactions_changed = Signal()
    rewards_changed = Signal()
    vts_status = Signal(object)
    # TTS: cambió la cola, el mensaje que suena o el último error
    tts_changed = Signal()

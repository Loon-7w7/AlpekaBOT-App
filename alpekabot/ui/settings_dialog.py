"""Ventana de configuración: credenciales de la app de Twitch y cuentas conectadas."""

from __future__ import annotations

import asyncio
import logging
from html import escape

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, credentials
from ..bot import AlpekaBot
from ..core.models import AccountInfo, AccountRole, Platform
from ..features.updater import UpdateError
from ..features.youtube import API_KEY_SECRET
from ..paths import IS_FROZEN
from ..platforms.kick import auth as kick_auth
from ..platforms.kick.tunnel import normalize_domain
from ..platforms.kick.webhook import WEBHOOK_PATH
from ..platforms.base import PlatformError
from . import theme as t
from .widgets import ModalDialog, button

log = logging.getLogger(__name__)

_TWITCH_HELP = (
    "Solo tienes que iniciar sesión con tus dos cuentas: al pulsar <b>Conectar</b> se abre "
    "<b>twitch.tv/activate</b> con un código. Comprueba que coincide con el de aquí y pulsa <b>Activar</b>."
)


class _AccountRow(QWidget):
    """Fila con el estado de una cuenta y los botones conectar/olvidar."""

    def __init__(self, dialog: "SettingsDialog", platform: Platform, role: AccountRole, hint: str) -> None:
        super().__init__()
        self._dialog = dialog
        self._platform = platform
        self._role = role
        self._task: asyncio.Task | None = None

        self._status = QLabel()
        self._status.setToolTip(hint)
        self._status.setWordWrap(True)
        self._status.setOpenExternalLinks(True)
        self._code_url = ""
        # Twitch: copiar el enlace de activación (p. ej. para abrirlo en una ventana de incógnito)
        self._copy_link = QPushButton("Copiar enlace")
        self._copy_link.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._code_url))
        self._copy_link.hide()
        self._connect = QPushButton()
        self._connect.clicked.connect(self._on_connect_clicked)
        self._forget = QPushButton("Olvidar")
        self._forget.clicked.connect(self._on_forget)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._status, 1)
        lay.addWidget(self._copy_link)
        lay.addWidget(self._connect)
        lay.addWidget(self._forget)
        self.refresh()

    def refresh(self) -> None:
        info = self._dialog.bot.account(self._platform, self._role)
        self._set_info(info)

    def _set_info(self, info: AccountInfo | None) -> None:
        self._copy_link.hide()
        if info:
            self._status.setText(f"✅ <b>{info.display_name}</b> <span style='color:#6b7280'>({info.login})</span>")
            self._connect.setText("Cambiar cuenta")
        else:
            self._status.setText("<span style='color:#6b7280'>Sin conectar</span>")
            self._connect.setText("Conectar")
        self._forget.setEnabled(info is not None)

    def _on_connect_clicked(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            return
        if not self._dialog.save_platform_settings(self._platform):
            return
        self._task = asyncio.ensure_future(self._authorize())

    async def _authorize(self) -> None:
        self._connect.setText("Cancelar")
        self._forget.setEnabled(False)
        self._status.setText(
            f"🌐 Inicia sesión en el navegador con la cuenta del <b>{self._role.label.lower()}</b>…"
        )
        name = self._platform.label
        try:
            info = await self._dialog.bot.authorize(self._platform, self._role, self._show_code)
            self._set_info(info)
            if self._dialog.bot.platforms[self._platform].is_connected:
                self._dialog.note(f"Reconecta {name} para usar la nueva cuenta.")
        except asyncio.CancelledError:
            self.refresh()
        except asyncio.TimeoutError:
            self.refresh()
            QMessageBox.warning(self, name, "Se agotó el tiempo para iniciar sesión.")
        except PlatformError as exc:
            self.refresh()
            QMessageBox.warning(self, name, str(exc))
        except Exception as exc:
            log.exception("Error autorizando cuenta %s de %s", self._role.value, name)
            self.refresh()
            QMessageBox.critical(self, name, f"Error inesperado ({type(exc).__name__}).")

    def _show_code(self, code: str, url: str) -> None:
        """Twitch (Device Code): el código que el usuario debe confirmar en twitch.tv/activate."""
        self._code_url = url
        self._status.setText(
            f"Código: <b style='font-size:15px; letter-spacing:2px'>{escape(code)}</b><br>"
            f"Confírmalo en <a href='{escape(url, quote=True)}'>twitch.tv/activate</a> con la cuenta del "
            f"<b>{self._role.label.lower()}</b>. ¿Sale otra cuenta? Copia el enlace en una ventana de incógnito."
        )
        self._copy_link.show()

    def _on_forget(self) -> None:
        answer = QMessageBox.question(
            self, "Olvidar cuenta", f"¿Borrar la sesión guardada de la cuenta del {self._role.label.lower()}?"
        )
        if answer == QMessageBox.Yes:
            self._dialog.bot.forget(self._platform, self._role)
            self.refresh()

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


class SettingsDialog(ModalDialog):
    update_found = Signal(object)  # UpdateInfo

    def __init__(self, bot: AlpekaBot, parent=None) -> None:
        super().__init__("Configuración", "Cuentas de Twitch y Kick, clave de YouTube y actualizaciones.",
                         "settings", parent, width=720)
        self.bot = bot
        self._update_task: asyncio.Task | None = None

        tabs = QTabWidget()
        self._rows: list[_AccountRow] = []
        tabs.addTab(self._build_twitch_tab(), t.icon("tv", t.ACCENT), "Twitch")
        tabs.addTab(self._build_kick_tab(), t.icon("zap", t.KICK), "Kick")
        tabs.addTab(self._build_youtube_tab(), t.icon("youtube", t.RED), "YouTube")
        tabs.addTab(self._build_updates_tab(), t.icon("download", t.TEXT_2), "Actualizaciones")

        self._note = QLabel()
        self._note.setStyleSheet(f"color:{t.AMBER}")
        self._note.setWordWrap(True)
        self._note.hide()

        self.body.addWidget(tabs)
        self.body.addWidget(self._note)
        self.footer.addWidget(button("Cerrar", slot=self.reject))
        self.footer.addStretch(1)
        self.footer.addWidget(button("Guardar", "check", "primary", self._on_save))

    # ---------------------------------------------------------------- twitch
    def _build_twitch_tab(self) -> QWidget:
        cfg = self.bot.config.twitch

        help_label = QLabel(_TWITCH_HELP)
        help_label.setWordWrap(True)

        rows = [
            _AccountRow(self, Platform.TWITCH, AccountRole.BOT, "La cuenta que escribe en el chat"),
            _AccountRow(self, Platform.TWITCH, AccountRole.STREAMER, "Tu cuenta: el canal donde trabaja el bot"),
        ]
        self._rows += rows
        acc_box = QGroupBox("Cuentas")
        acc_form = QFormLayout(acc_box)
        acc_form.addRow(help_label)
        acc_form.addRow("Bot:", rows[0])
        acc_form.addRow("Streamer:", rows[1])
        tip = QLabel(
            "<span style='color:#6b7280'>Twitch usa la cuenta que tengas abierta en el navegador. Antes de "
            "conectar la del <b>bot</b>, cambia de cuenta en twitch.tv o abre el enlace en una ventana de "
            "incógnito; comprueba aquí el nombre que aparece. Haz al bot moderador: /mod nombre_del_bot.</span>"
        )
        tip.setWordWrap(True)
        acc_form.addRow(tip)

        self._auto_connect = QCheckBox("Conectar automáticamente al abrir AlpekaBOT")
        self._auto_connect.setChecked(cfg.auto_connect)

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(acc_box)
        lay.addWidget(self._auto_connect)
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------------ kick
    @staticmethod
    def _secret_field(value: str) -> tuple[QLineEdit, QHBoxLayout]:
        edit = QLineEdit(value)
        edit.setEchoMode(QLineEdit.Password)
        show = QPushButton("👁")
        show.setFixedWidth(36)
        show.setCheckable(True)
        show.toggled.connect(lambda on: edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(show)
        return edit, row

    @staticmethod
    def _copy_field(edit: QLineEdit) -> QHBoxLayout:
        copy = QPushButton("Copiar")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(edit.text()))
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(copy)
        return row

    def _build_kick_tab(self) -> QWidget:
        cfg = self.bot.config.kick
        ngrok_help = QLabel(
            "<b>1. ngrok</b> (gratis): regístrate en <a href='https://dashboard.ngrok.com/signup'>ngrok.com</a>, "
            "copia tu <a href='https://dashboard.ngrok.com/get-started/your-authtoken'>Authtoken</a> y en "
            "<a href='https://dashboard.ngrok.com/domains'>Domains</a> reclama tu <b>dominio estático gratuito</b>."
        )
        kick_help = QLabel(
            "<b>2. App de Kick</b>: en <a href='https://kick.com/settings/developer'>kick.com → Settings → "
            "Developer</a> crea una app con tu cuenta de streamer:<br>"
            "• <b>Redirect URL</b> y <b>Webhook URL</b>: copia las de abajo y activa <i>Enable Webhooks</i>.<br>"
            "• <b>Scopes</b>: user:read, channel:read, chat:write, events:subscribe, moderation:ban, "
            "moderation:chat_message:manage.<br>"
            "• Copia aquí el <b>Client ID</b> y el <b>Client Secret</b>."
        )
        for lbl in (ngrok_help, kick_help):
            lbl.setOpenExternalLinks(True)
            lbl.setWordWrap(True)

        self._ngrok_token, ngrok_token_row = self._secret_field(
            credentials.get_secret(kick_auth.NGROK_TOKEN_KEY) or "")
        self._ngrok_domain = QLineEdit(cfg.ngrok_domain)
        self._ngrok_domain.setPlaceholderText("mi-bot.ngrok-free.app")
        self._webhook_url = QLineEdit()
        self._webhook_url.setReadOnly(True)
        self._ngrok_domain.textChanged.connect(self._update_webhook_url)
        self._update_webhook_url()

        self._kick_id = QLineEdit(cfg.client_id)
        self._kick_secret, kick_secret_row = self._secret_field(kick_auth.get_client_secret())
        self._kick_redirect = QLineEdit(cfg.redirect_url)

        box = QGroupBox("Conexión con Kick")
        form = QFormLayout(box)
        form.addRow(ngrok_help)
        form.addRow("Authtoken de ngrok:", ngrok_token_row)
        form.addRow("Dominio de ngrok:", self._ngrok_domain)
        form.addRow(kick_help)
        form.addRow("Redirect URL:", self._copy_field(self._kick_redirect))
        form.addRow("Webhook URL:", self._copy_field(self._webhook_url))
        form.addRow("Client ID:", self._kick_id)
        form.addRow("Client Secret:", kick_secret_row)

        rows = [
            _AccountRow(self, Platform.KICK, AccountRole.BOT, "La cuenta de Kick que escribe en el chat"),
            _AccountRow(self, Platform.KICK, AccountRole.STREAMER, "Tu cuenta de Kick: el canal donde trabaja el bot"),
        ]
        self._rows += rows
        acc_box = QGroupBox("Cuentas de Kick")
        acc_form = QFormLayout(acc_box)
        acc_form.addRow("Bot:", rows[0])
        acc_form.addRow("Streamer:", rows[1])
        tip = QLabel(
            "<span style='color:#6b7280'>Kick inicia sesión con la cuenta que tengas abierta en el navegador. "
            "Antes de conectar la del <b>bot</b>, cierra sesión en kick.com (o entra con la cuenta del bot); "
            "comprueba aquí el nombre que aparece. Haz al bot moderador de tu canal.</span>"
        )
        tip.setWordWrap(True)
        acc_form.addRow(tip)

        shared_help = QLabel(
            f"<b style='color:{t.AMBER}'>Solo por white list.</b> "
            "Solo tienes que iniciar sesión con tus dos cuentas: AlpekaBOT usa su propia app de Kick y "
            "su servidor, sin ngrok ni claves. Tu canal debe estar en la <b>white list</b> del administrador "
            "de AlpekaBOT: si al conectar te dice que no lo está, pásale el nombre de tu canal."
        )
        shared_help.setWordWrap(True)
        self._kick_own = QCheckBox("Usar mi propia app de Kick y ngrok (avanzado)")
        self._kick_own.setChecked(cfg.use_own_app)

        def on_own(checked: bool) -> None:
            box.setVisible(checked)
            shared_help.setVisible(not checked)

        self._kick_own.toggled.connect(on_own)
        on_own(cfg.use_own_app)

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(shared_help)
        lay.addWidget(acc_box)
        lay.addWidget(self._kick_own)
        lay.addWidget(box)
        lay.addStretch(1)
        return page

    def _update_webhook_url(self) -> None:
        domain = normalize_domain(self._ngrok_domain.text())
        self._webhook_url.setText(f"https://{domain}{WEBHOOK_PATH}" if domain else "")

    def _save_kick(self) -> bool:
        cfg = self.bot.config.kick
        own = self._kick_own.isChecked()
        changed = own != cfg.use_own_app
        if not own:
            cfg.use_own_app = False
            self.bot.config.save()
            if changed and self.bot.platforms[Platform.KICK].is_connected:
                self.note("Reconecta Kick para aplicar los cambios.")
            return True
        client_id = self._kick_id.text().strip()
        secret = self._kick_secret.text().strip()
        redirect = self._kick_redirect.text().strip()
        token = self._ngrok_token.text().strip()
        domain = normalize_domain(self._ngrok_domain.text())
        missing = [n for n, v in (("Client ID", client_id), ("Client Secret", secret),
                                  ("Authtoken de ngrok", token), ("Dominio de ngrok", domain)) if not v]
        if missing:
            QMessageBox.warning(self, "Kick", "Falta: " + ", ".join(missing) + ".")
            return False
        if not redirect.startswith("http://localhost:"):
            QMessageBox.warning(self, "Kick", "La Redirect URL debe ser del tipo http://localhost:PUERTO/callback")
            return False
        cfg.use_own_app = True
        cfg.client_id, cfg.redirect_url, cfg.ngrok_domain = client_id, redirect, domain
        kick_auth.set_client_secret(secret)
        credentials.set_secret(kick_auth.NGROK_TOKEN_KEY, token)
        self.bot.config.save()
        if self.bot.platforms[Platform.KICK].is_connected:
            self.note("Reconecta Kick para aplicar los cambios.")
        return True

    # --------------------------------------------------------------- youtube
    def _build_youtube_tab(self) -> QWidget:
        help_label = QLabel(
            "La clave se usa para <b>!sr</b> (buscar canciones y comprobar su duración).<br>"
            "1. Entra en <a href='https://console.cloud.google.com/apis/library/youtube.googleapis.com'>"
            "Google Cloud → YouTube Data API v3</a> y pulsa <b>Habilitar</b> (crea un proyecto si te lo pide).<br>"
            "2. Ve a <a href='https://console.cloud.google.com/apis/credentials'>Credenciales</a> → "
            "<b>Crear credenciales → Clave de API</b>.<br>"
            "3. Recomendado: en la clave, <i>Restricciones de API</i> → solo <i>YouTube Data API v3</i>.<br>"
            "4. Pégala aquí. Es gratis: unas 100 búsquedas por texto al día (los enlaces gastan mucho menos)."
        )
        help_label.setOpenExternalLinks(True)
        help_label.setWordWrap(True)
        self._yt_key = QLineEdit(credentials.get_secret(API_KEY_SECRET) or "")
        self._yt_key.setEchoMode(QLineEdit.Password)
        show = QPushButton("👁")
        show.setFixedWidth(36)
        show.setCheckable(True)
        show.toggled.connect(lambda on: self._yt_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        row = QHBoxLayout()
        row.addWidget(self._yt_key)
        row.addWidget(show)
        box = QGroupBox("API de YouTube")
        form = QFormLayout(box)
        form.addRow(help_label)
        form.addRow("Clave de API:", row)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(box)
        lay.addStretch(1)
        return page

    def _save_youtube(self) -> None:
        key = self._yt_key.text().strip()
        if key:
            credentials.set_secret(API_KEY_SECRET, key)
        else:
            credentials.delete_secret(API_KEY_SECRET)

    # ------------------------------------------------------- actualizaciones
    def _build_updates_tab(self) -> QWidget:
        self._check_btn = button("Buscar actualizaciones", "refresh-cw", slot=self._on_check_updates)
        self._update_status = QLabel()
        self._update_status.setWordWrap(True)
        if not IS_FROZEN:
            self._check_btn.setEnabled(False)
            self._update_status.setText("Se está ejecutando desde el código fuente: se actualiza con git.")
        box = QGroupBox("AlpekaBOT")
        lay = QVBoxLayout(box)
        lay.addWidget(QLabel(f"Versión instalada: <b>{__version__}</b>"))
        lay.addWidget(QLabel("La app busca versiones nuevas al abrirse y siempre te pregunta antes de instalarlas."))
        row = QHBoxLayout()
        row.addWidget(self._check_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self._update_status)
        page = QWidget()
        page_lay = QVBoxLayout(page)
        page_lay.addWidget(box)
        page_lay.addStretch(1)
        return page

    def _on_check_updates(self) -> None:
        self._check_btn.setEnabled(False)
        self._update_status.setText("Buscando…")
        self._update_task = asyncio.ensure_future(self._check_updates())

    async def _check_updates(self) -> None:
        try:
            info = await self.bot.updater.check()
        except UpdateError as exc:
            self._update_status.setText(str(exc))
        except Exception:
            log.exception("Error comprobando actualizaciones")
            self._update_status.setText("Error inesperado al buscar actualizaciones (mira la consola).")
        else:
            if info is not None:
                self.reject()  # primero se cierra esta ventana: no se apilan ventanas modales
                self.update_found.emit(info)
                return
            self._update_status.setText("Ya tienes la última versión.")
        self._check_btn.setEnabled(True)

    # ------------------------------------------------------------- guardado
    def save_platform_settings(self, platform: Platform) -> bool:
        return self._save_twitch() if platform is Platform.TWITCH else self._save_kick()

    def _save_twitch(self) -> bool:
        # La app de Twitch es compartida: solo queda la preferencia de conexión automática
        self.bot.config.twitch.auto_connect = self._auto_connect.isChecked()
        self.bot.config.save()
        return True

    def _on_save(self) -> None:
        self._save_youtube()
        self._save_twitch()
        if not self._save_kick():  # con app propia valida los campos
            return
        log.info("Configuración guardada")
        self.accept()

    def note(self, text: str) -> None:
        self._note.setText(text)
        self._note.show()

    def done(self, result: int) -> None:
        for row in self._rows:
            row.cancel()
        if self._update_task is not None and self._update_task is not asyncio.current_task():
            self._update_task.cancel()
        super().done(result)

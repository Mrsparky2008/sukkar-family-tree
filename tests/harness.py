"""
A stand-in for Telegram, so the conversation can be tested without a network.

`Conversation` below drives the real handlers through the real routing table
from `bot.main.build_conversation()` — so the callback-data patterns are
exercised, not reimplemented. What it fakes is only the transport: Update,
CallbackQuery, and reply_text.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from telegram.ext import CallbackQueryHandler, MessageHandler

from bot import handlers
from bot.main import build_conversation


class Sent:
    """One message the bot sent."""

    def __init__(self, text: str, markup: Any):
        self.text = text
        self.buttons: dict[str, str] = {}
        if markup is not None:
            for row in markup.inline_keyboard:
                for button in row:
                    self.buttons[button.text] = button.callback_data

    def __repr__(self) -> str:
        return f"<Sent {self.text[:40]!r} buttons={list(self.buttons)}>"


class _Message:
    def __init__(self, conversation: "Conversation", text: str | None = None):
        self._conversation = conversation
        self.text = text

    async def reply_text(self, text, reply_markup=None, **_kwargs):
        self._conversation.sent.append(Sent(text, reply_markup))
        return _Message(self._conversation)


class _Query:
    def __init__(self, conversation: "Conversation", data: str):
        self.data = data
        self.message = _Message(conversation)
        self.answered = False

    async def answer(self, *_args, **_kwargs):
        self.answered = True


class Conversation:
    """Talks to the bot the way a person on a phone would."""

    def __init__(self, user_id: int = 1001):
        self.user_id = user_id
        self.sent: list[Sent] = []
        self.state: int | None = None
        self.context = SimpleNamespace(user_data={}, chat_data={}, error=None)
        self._conversation_handler = build_conversation()

    # --- inspecting what came back ---------------------------------------

    @property
    def last(self) -> Sent:
        return self.sent[-1]

    @property
    def text(self) -> str:
        return self.last.text

    @property
    def buttons(self) -> dict[str, str]:
        return self.last.buttons

    def transcript(self) -> str:
        return "\n---\n".join(message.text for message in self.sent)

    def button_for(self, label_fragment: str) -> str:
        """Callback data for the first button whose label contains this text."""
        for sent in reversed(self.sent):
            for label, data in sent.buttons.items():
                if label_fragment.lower() in label.lower():
                    return data
        raise AssertionError(
            f"no button matching {label_fragment!r}; last offered {list(self.buttons)}"
        )

    # --- acting ------------------------------------------------------------

    def _update(self, text: str | None = None, data: str | None = None):
        query = _Query(self, data) if data is not None else None
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=self.user_id),
            effective_message=_Message(self, text),
            callback_query=query,
            message=None,
        )

    async def start(self) -> int | None:
        self.state = await handlers.start(self._update("/start"), self.context)
        return self.state

    async def say(self, text: str) -> int | None:
        """Type a message."""
        return await self._dispatch(self._update(text=text), data=None)

    async def tap(self, label_fragment: str) -> int | None:
        """Press the button whose label contains this text."""
        data = self.button_for(label_fragment)
        return await self._dispatch(self._update(data=data), data=data)

    async def tap_data(self, data: str) -> int | None:
        return await self._dispatch(self._update(data=data), data=data)

    async def _dispatch(self, update, data: str | None) -> int | None:
        candidates = self._conversation_handler.states.get(self.state, [])
        for handler in candidates:
            if data is not None and isinstance(handler, CallbackQueryHandler):
                if handler.pattern is None or handler.pattern.search(data):
                    self.state = await handler.callback(update, self.context)
                    return self.state
            if data is None and isinstance(handler, MessageHandler):
                self.state = await handler.callback(update, self.context)
                return self.state
        raise AssertionError(
            f"nothing in state {self.state} handles "
            f"{'button ' + repr(data) if data else 'typed text'}"
        )

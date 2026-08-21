"""
The bot's entry point.

    python -m bot

Needs TELEGRAM_BOT_TOKEN in the environment or in .env. Long polling, so there
is no webhook, no public hostname, and no TLS certificate to keep alive — it
runs anywhere Python runs, including a laptop, which is the right size for a
pilot on one branch.
"""

from __future__ import annotations

import logging
import sys
import warnings

from telegram.warnings import PTBUserWarning
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from bot import handlers

log = logging.getLogger(__name__)

# This conversation deliberately mixes typed answers with button presses, so
# per_message tracking cannot be on. The warning is expected and would
# otherwise print on every start for the life of the project.
warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)

TEXT_ANSWER = filters.TEXT & ~filters.COMMAND


def build_conversation() -> ConversationHandler:
    """One conversation, five states, driven by the flows in flows.py."""
    cancel_button = CallbackQueryHandler(
        handlers.on_cancel_button, pattern=r"^cancel$"
    )

    with warnings.catch_warnings():
        # This conversation deliberately mixes typed answers with button
        # presses, so per_message tracking cannot be on. PTB warns about that
        # combination; it is expected here, and would otherwise print on every
        # single start for the life of the project.
        warnings.filterwarnings(
            "ignore", message=".*per_message.*", category=PTBUserWarning
        )
        return _conversation(cancel_button)


def _conversation(cancel_button: CallbackQueryHandler) -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", handlers.start)],
        states={
            handlers.MENU: [
                CallbackQueryHandler(handlers.on_menu, pattern=r"^menu:"),
            ],
            handlers.ASK: [
                CallbackQueryHandler(handlers.on_choice, pattern=r"^(ans:|skip$)"),
                cancel_button,
                # A typed answer to a question.
                MessageHandler(TEXT_ANSWER, handlers.on_text),
            ],
            handlers.CONFIRM_ANSWER: [
                CallbackQueryHandler(
                    handlers.on_answer_confirmed, pattern=r"^(yes|no)$"
                ),
                cancel_button,
                # Retyping instead of tapping "no" should just work.
                MessageHandler(TEXT_ANSWER, handlers.on_text),
            ],
            handlers.CONFIRM_SUBMIT: [
                CallbackQueryHandler(handlers.on_submit, pattern=r"^(send|over)$"),
                cancel_button,
            ],
            handlers.IDENTITY_MATCH: [
                CallbackQueryHandler(handlers.on_identity_choice, pattern=r"^who"),
                cancel_button,
            ],
            handlers.PICK_SUBMISSION: [
                CallbackQueryHandler(handlers.on_pick_submission, pattern=r"^fix:"),
                cancel_button,
            ],
        },
        fallbacks=[
            CommandHandler("cancel", handlers.cancel),
            CommandHandler("start", handlers.start),
            MessageHandler(TEXT_ANSWER, handlers.on_stray_text),
        ],
        # /start should always get you back to a working state, even from
        # halfway through a flow.
        allow_reentry=True,
    )


def build_application(token: str | None = None) -> Application:
    application = Application.builder().token(token or config.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(build_conversation())
    application.add_handler(CommandHandler("share", handlers.share))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_error_handler(handlers.on_error)
    return application


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
    )
    # httpx logs every Telegram poll at INFO, which buries everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not config.TELEGRAM_BOT_TOKEN:
        print(
            "TELEGRAM_BOT_TOKEN is not set.\n\n"
            "Get a token from @BotFather, then either put it in .env:\n"
            "    TELEGRAM_BOT_TOKEN=123456:ABC...\n"
            "or export it in the environment.",
            file=sys.stderr,
        )
        return 1

    log.info("starting @%s", config.TELEGRAM_BOT_USERNAME)
    build_application().run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

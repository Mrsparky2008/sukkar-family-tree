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
    PicklePersistence,
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
                CallbackQueryHandler(handlers.on_review, pattern=r"^menu:review$"),
                CallbackQueryHandler(handlers.on_menu, pattern=r"^menu:"),
                # "Add someone else" and Cancel both land here when a reply
                # re-drew the menu without changing state; dead buttons read
                # as a broken bot.
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_menu_text),
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
                CallbackQueryHandler(handlers.on_source_button, pattern=r"^source$"),
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_confirm_text),
            ],
            handlers.ASK_SOURCE: [
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_source_given),
            ],
            handlers.PICK_SUBJECT: [
                CallbackQueryHandler(handlers.on_pick_subject, pattern=r"^subj:"),
                cancel_button,
            ],
            handlers.CLIMB: [
                CallbackQueryHandler(handlers.on_next, pattern=r"^next:"),
                CallbackQueryHandler(handlers.on_review, pattern=r"^menu:review$"),
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_next_text),
            ],
            handlers.CONFIRM_PERSON: [
                CallbackQueryHandler(
                    handlers.on_person_confirmed, pattern=r"^(keep|redo)$"
                ),
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_person_confirm_text),
            ],
            handlers.TOUR: [
                CallbackQueryHandler(handlers.on_tour_button, pattern=r"^tour:"),
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_tour_text),
            ],
            handlers.CLARIFY: [
                CallbackQueryHandler(handlers.on_sex_button, pattern=r"^sexq:"),
                CallbackQueryHandler(handlers.on_link_button, pattern=r"^linkq:"),
                CallbackQueryHandler(handlers.on_self_button, pattern=r"^selfq:"),
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_clarify_text),
            ],
            handlers.REVIEW: [
                CallbackQueryHandler(handlers.on_edit_pick, pattern=r"^edit:"),
                CallbackQueryHandler(handlers.on_send_all, pattern=r"^sendall$"),
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_review_text),
            ],
            handlers.EDIT_VALUE: [
                CallbackQueryHandler(handlers.on_edit_remove, pattern=r"^remove$"),
                cancel_button,
                MessageHandler(TEXT_ANSWER, handlers.on_edit_value),
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
        # Survive restarts. A code update must never eat somebody's
        # half-entered answers — the first live restart did exactly that, to
        # the first live user, mid-question.
        name="capture",
        persistent=True,
    )


def build_application(token: str | None = None) -> Application:
    state_path = config.DATABASE_PATH.parent / "bot-state.pickle"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    application = (
        Application.builder()
        .token(token or config.TELEGRAM_BOT_TOKEN)
        .persistence(PicklePersistence(filepath=state_path))
        .build()
    )
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

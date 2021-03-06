#!/usr/bin/env python3

import logging
import logging.handlers
import sys

from colorstreamhandler import ColorStreamHandler

from bots import IRCBot, TelegramImageBot
import config
from handlers import AuthHandler, ImageHandler
from models.image import ImageDatabase
from models.user import UserDatabase


CONFIG_FILE = "config.yaml"

l = logging.getLogger(__name__)


def verify_config(conf):
    if not conf.telegram.token:
        l.critical("no telegram token found")

    elif not conf.imgur.client_id or not conf.imgur.client_secret:
        l.critical("no imgur client info found")

    elif not conf.imgur.refresh_token:
        l.critical("no imgur refresh_token found. Create one with authenticate_imgur.py")

    elif not conf.irc.host or not conf.irc.channel:
        l.critical("no sufficient irc configuration found")

    else:
        return True
    return False


def init_logging(conf, console_level):
    console_fmt = "| {levelname:^8} | {message} (from {name}; {threadName})"
    file_fmt = "| {asctime} " + console_fmt
    asyncirc_logger = logging.getLogger("asyncirc")

    class NewStyleLogRecord(logging.LogRecord):
        def getMessage(self):  # noqa
            msg = self.msg
            if not isinstance(self.msg, str):
                msg = str(self.msg)
            if not isinstance(self.args, tuple):
                self.args = (self.args,)
            return msg.rstrip().format(*self.args)
    logging.setLogRecordFactory(NewStyleLogRecord)

    handler = ColorStreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(console_fmt, style='{'))
    handler.addFilter(lambda r: r.levelno >= console_level)

    l.addHandler(handler)
    asyncirc_logger.addHandler(handler)

    conf_level = console_level
    if conf.logging.active:
        conf_level = getattr(logging, (conf.logging.level or "WARN").upper())

        handler = logging.handlers.TimedRotatingFileHandler(conf.logging.path or "log",
                                                            **conf.logging.rotate)
        handler.setFormatter(logging.Formatter(file_fmt, style='{'))
        handler.addFilter(lambda r: r.levelno >= conf_level)

        l.addHandler(handler)
        asyncirc_logger.addHandler(handler)

    min_level = min(console_level, conf_level)
    max_level = max(console_level, conf_level)
    l.setLevel(min_level)
    asyncirc_logger.setLevel(min_level)

    l.log(max_level,
          "application started; console logging level: {}; file logging level: {}",
          console_level,
          conf_level if conf.logging.active else "disabled")

    # return minimum level required to pass all filters
    return max_level


###############################################################################


def main():
    # Determine console logging level from arguments
    console_level = logging.WARN
    if len(sys.argv) > 1:
        if sys.argv[1].isdigit():
            console_level = int(sys.argv[1])
        else:
            console_level = getattr(logging, sys.argv[1].upper(), console_level)
    # Read config and init logging
    conf = config.read_file(CONFIG_FILE)
    all_log_level = init_logging(conf=conf, console_level=console_level)
    l.info("config: {!s}", conf)

    # Verify other config
    if not verify_config(conf):
        return 2

    # Load user database
    user_db = UserDatabase(conf.storage.user_database or "users.json")

    # Start IRC bot
    irc_bot = IRCBot(
        host=conf.irc.host,
        port=conf.irc.port or 6667,
        nick=conf.irc.nick or "TelegramBot",
        realname=conf.irc.nick,
        password=conf.irc.password or None,
        use_ssl=conf.irc.ssl or False
    )
    irc_bot.start()
    if not irc_bot.wait_connected(conf.irc.timeout or 7):
        l.critical("couldn't connect to IRC")
        irc_bot.stop()
        return 3
    l.info("connected to IRC")
    irc_bot.join(conf.irc.channel)

    # Start Telegram bot
    tg_bot = TelegramImageBot(conf, user_db, token=conf.telegram.token)
    l.info("Me: {}", tg_bot.update_bot_info().wait())

    # Register image callback as a closure
    def on_image(img):
        nonlocal conf, irc_bot, tg_bot, user_db
        thread = ImageHandler(
            conf=conf,
            irc_bot=irc_bot,
            tg_bot=tg_bot,
            user_db=user_db,
            img=img
        )
        thread.start()
        return thread

    tg_bot.on_image = on_image

    # Register image callback as a closure
    def on_auth(message):
        nonlocal conf, irc_bot, tg_bot, user_db
        thread = AuthHandler(
            conf=conf,
            irc_bot=irc_bot,
            tg_bot=tg_bot,
            user_db=user_db,
            message=message
        )
        thread.start()
        return thread

    tg_bot.on_auth = on_auth

    # Go through backlog and reschedule failed image uploads
    if conf.storage.database:
        with ImageDatabase(conf.storage.database) as db:
            backlog = db.get_unfinished_images()
        if backlog:
            l.info("Going through backlog, size: {}", len(backlog))
            for img in backlog:
                on_image(img).join()
            l.info("Finished backlog")

    # Main loop
    try:
        tg_bot.poll_loop()
    except KeyboardInterrupt:
        print("user interrupt...")
    except:
        l.exception()
    finally:
        logging.log(all_log_level, "shutting down")
        irc_bot.stop()


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3

from datetime import datetime
from functools import partial
import logging
import os
from string import Template
import sys
import tempfile
from threading import Thread
import time

from imgurpython import ImgurClient
from imgurpython.helpers.error import ImgurClientError
from twx import botapi

from bots import IRCBot, TelegramImageBot
import config
from models.image import ImageDatabase
from models.user import UserDatabase
from util import wrap


CONFIG_FILE = "config.yaml"

l = logging.getLogger(__name__)


###############################################################################


class ImageReceivedThread(Thread):
    def __init__(self, conf, irc_bot, tg_bot, user_db, img, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conf = conf
        self.irc_bot = irc_bot
        self.tg_bot = tg_bot
        self.user_db = user_db
        self.img = img

    def reply(self, msg):
        self.tg_bot.send_message(
            self.img.c_id,
            msg,
            disable_web_page_preview=True,
            reply_to_message_id=self.img.m_id,
            on_success=partial(l.info, "sent message to {0.chat}: {0.text}")
        )

    def run(self):
        # Check if user may send images at all
        if self.img.c_id in self.user_db.blacklist:
            l.info("discarding image from blacklisted user {}", self.img.c_id)
            return
        if self.img.c_id not in self.user_db.name_map:
            self.reply("You need to authenticate via /auth before sending pictures")
            l.info("discarding image from unauthorized user {}", self.img.c_id)
            return

        self.img = self.img._replace(username=self.user_db.name_map[self.img.c_id])

        # Show that we're doing something
        self.tg_bot.send_chat_action(self.img.c_id, botapi.ChatAction.PHOTO)

        # Must be created in thread because multi-threading is now allowed
        db = ImageDatabase(self.conf.storage.database) if self.conf.storage.database else None

        try:
            l.debug("Running ImageReceivedThread with {}", self.img)
            # Check if we recieved the file already and see how far we got
            if db:
                db_img = db.find_image(self.img)
                if db_img:
                    self.img = db_img

            # Download file if necessary
            if not self.img.local_path or not os.path.exists(self.img.local_path):
                if not self.download_file():
                    return
            else:
                l.warn("File exists already, skipping download: {}", self.img.local_path)

            # Upload file if necessary
            if not self.img.url:
                self.upload_file()
            else:
                l.warn("File already uploaded: {}", self.img.url)

            # Post to IRC
            self.post_to_irc()

            # Report success
            self.reply("Image delivered. Uploaded to: " + self.img.url)
            self.img = self.img._replace(finished=True)

            # Cleanup
            if self.conf.storage.delete_images:
                os.remove(self.img.local_path)
                self.img = self.img._replace(local_path=None)

        except Exception as e:
            self.reply("Oops, there was an error. Contact @fichtefoll and run in circles.\n"
                       "Error: " + str(e))
            l.exception("Uncaught exception in ImageReceivedThread: {}", e)

        finally:
            if db:
                if not db_img:
                    db.insert_image(self.img)
                elif self.img != db_img:
                    db.update_image(self.img)
                db.close()

    def download_file(self):
        # Get file info
        file_info = self.tg_bot.get_file(self.img.f_id).wait()
        if isinstance(file_info, botapi.Error):
            msg = "Error getting file info: {}".format(file_info)
            l.error(msg)
            self.reply(msg)
            return False

        l.info("file info: {}", file_info)

        # Build file path
        directory = (Template(self.conf.storage.directory or "$temp/telegram")
                     .substitute(temp=tempfile.gettempdir()))
        directory = os.path.abspath(directory)
        basename = file_info.file_path.replace("/", "_")
        out_file = os.path.join(directory, basename)
        self.img = self.img._replace(remote_path=file_info.file_path, local_path=out_file)

        # Do download
        os.makedirs(directory, exist_ok=True)
        result = self.tg_bot.download_file(self.img.remote_path,
                                           out_file=self.img.local_path).wait()
        if isinstance(result, Exception):
            msg = "Error downloading file: {}".format(result)
            l.error(msg)
            self.reply(msg)
            return False
        else:
            l.info("Downloaded file to: {}", self.img.local_path)
            return True

    def upload_file(self):
        timestamp = datetime.fromtimestamp(self.img.time).strftime(
            self.conf.imgur.timestamp_format or "%Y-%m-%dT%H:%M:%S"
        )
        config = dict(
            album=self.conf.imgur.album,
            name="{}_{}".format(timestamp, self.img.username).replace(":", "-"),
            title="{} (by {}; {})".format(self.img.caption or "No caption",
                                          self.img.username, timestamp)
        )

        try:
            client = ImgurClient(self.conf.imgur.client_id, self.conf.imgur.client_secret,
                                 refresh_token=self.conf.imgur.refresh_token)
            data = client.upload_from_path(self.img.local_path, config=config, anon=False)
        except ImgurClientError as e:
            msg = "Error uploading to imgur: {0.status_code} {0.error_message}".format(e)
            l.error(msg)
            self.reply(msg)
            raise

        l.info("uploaded image: {}", data)
        l.debug("X-RateLimit-ClientRemaining: {}", client.credits['ClientRemaining'])

        self.img = self.img._replace(url=data['link'])
        return True

    def post_to_irc(self):
        pre_msg = ("<{{0.username}}>: {}{{0.url}}"
                   .format("{0.caption} " if self.img.caption else ""))
        msg = pre_msg.format(self.img)
        self.irc_bot.msg(self.conf.irc.channel, msg)


###############################################################################


class AuthThread(Thread):
    def __init__(self, conf, irc_bot, tg_bot, user_db, message, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conf = conf
        self.irc_bot = irc_bot
        self.tg_bot = tg_bot
        self.user_db = user_db
        self.message = message

        self.authenticated = False

    def do_authentication(self, name):
        if self.authenticated:
            return

        self.user_db.add_to_name_map(self.message.sender.id, name)

        self.irc_bot.msg(self.conf.irc.channel, "{}: Authentication successful.".format(name))
        self.tg_bot.send_message(self.message.chat.id, "Authenticated as {}.".format(name))
        l.info("{0} authenticated as {1.sender}", name, self.message)

        self.authenticated = True

    def run(self):
        # Create unused authcode and register callback
        authcode = self.irc_bot.new_auth_callback(self.do_authentication)

        msg = wrap("""
            Your Authcode is: {authcode}

            Within {conf.irc.auth_timeout}s,
            send "{nick} auth {authcode}" in
            {conf.irc.channel} on {conf.irc.host}
            with your usual nickname.
            If you want the bot to use a different name
            than your current IRC name,
            add an additional argument which will be stored instead
            (for the slack <-> IRC proxy).

            Example: "{nick} auth {authcode} my_actual_name"

            You can re-authenticate any time
            to overwrite the stored nick.
        """).format(conf=self.conf, authcode=authcode, nick=self.irc_bot.nick)
        self.tg_bot.send_message(self.message.chat.id, msg)

        # Register callback ...
        l.info("initiated authentication for {0.sender}, authcode: {1}",
               self.message, authcode)

        # ... and wait until do_authentication gets called, or timeout
        start_time = time.time()
        while (
            not self.authenticated
            and time.time() < start_time + (self.conf.irc.auth_timeout or 3000)
        ):
            time.sleep(0.5)

        # Finish thread
        if not self.authenticated:
            l.info("authentication timed out for {0.sender}", self.message)
            self.tg_bot.send_message(self.message.chat.id, "Authentication timed out")
        self.irc_bot.remove_auth_callback(authcode)


###############################################################################


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


def init_logging(conf, console_level):
    class NewStyleLogRecord(logging.LogRecord):
        def getMessage(self):
            msg = self.msg
            if not isinstance(self.msg, str):
                msg = str(self.msg)
            if not isinstance(self.args, tuple):
                self.args = (self.args,)
            return msg.rstrip().format(*self.args)
    logging.setLogRecordFactory(NewStyleLogRecord)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "| {levelname:^8} | {message} (from {name}; {threadName})",
        style='{'
    ))
    # Filter out requests logging
    handler.addFilter(
        lambda r: (not r.name.startswith("requests")) or r.levelno > 20
    )
    handler.addFilter(lambda r: r.levelno >= console_level)

    handlers = [handler]

    if conf.logging.active:
        conf_level = getattr(logging, (conf.logging.level or "WARN").upper())
        f = open(conf.logging.path or "errors.log", "a")
        f.write("-- started application; logging level: {}\n".format(conf_level))

        handler = logging.StreamHandler(f)
        handler.setFormatter(logging.Formatter(
            "| {asctime} | {levelname:^8} | {message} (from {name}; {threadName})",
            style='{'
        ))
        handler.addFilter(lambda r: r.levelno >= conf_level)
        # Filter out requests logging
        handler.addFilter(
            lambda r: (not r.name.startswith("requests")) or r.levelno > 20
        )

        handlers.append(handler)

    logging.basicConfig(level=min(console_level, conf_level), handlers=handlers)
    print("-- console logging level: {}".format(l.getEffectiveLevel()))


###############################################################################


def main():
    # Read config, init logging
    conf = config.read_file(CONFIG_FILE)
    init_logging(conf=conf, console_level=logging.DEBUG)
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
        # use_ssl=conf.irc.ssl or False
    )
    irc_bot.start()
    if not irc_bot.wait_connected(conf.irc.timeout or 7):
        l.critical("Couldn't connect to IRC")
        return 3
    irc_bot.join(conf.irc.channel)

    # Start Telegram bot
    tg_bot = TelegramImageBot(conf, token=conf.telegram.token)
    l.info("Me: {}", tg_bot.update_bot_info().wait())

    # Register image callback as a closure
    def on_image(img):
        nonlocal conf, irc_bot, tg_bot
        thread = ImageReceivedThread(
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
        nonlocal conf, irc_bot, tg_bot
        thread = AuthThread(
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
    tg_bot.poll_loop()

if __name__ == '__main__':
    sys.exit(main())

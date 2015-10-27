#!/usr/bin/env python3

from datetime import datetime
from functools import partial
import logging
import mimetypes
import os
from string import Template
import sys
import tempfile
from threading import Thread
import time

import asyncirc
from imgurpython import ImgurClient
from imgurpython.helpers.error import ImgurClientError
from twx import botapi

import config
from models.image import ImageInfo, ImageDatabase


CONFIG_FILE = "config.yaml"
IMAGE_EXTENSIONS = (".jpg", ".png", ".gif")

l = logging.getLogger(__name__)


###############################################################################


class TelegramImageBot(botapi.TelegramBot):

    def __init__(self, conf, on_image=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._offset = None
        self.conf = conf
        self.on_image = on_image

    @staticmethod
    def build_name(user):
        return user.username or ' '.join(filter([user.first_name, user.last_name]))

    @property
    def offset(self):
        return self._offset

    @offset.setter
    def offset(self, offset):
        l.info("new offset: {}", offset)
        self._offset = offset

    def handle_updates(self, updates):
        if not updates:
            return

        for update in updates:
            upd_id, message = update.update_id, update.message

            l.debug("handling update: {}", update)

            # Out data storage object
            img = ImageInfo(f_id=None,
                            time=message.date,
                            username=message.sender.username,
                            c_id=message.chat.id, m_id=message.message_id,
                            caption=message.caption, ext='.jpg',
                            remote_path=None, local_path=None, url=None, finished=False)

            if message.document:
                # Check for image mime types
                mime_type = message.document.mime_type
                l.info("received document from {0.sender.username}: {0.document}", message)
                if mime_type:
                    ext = mimetypes.guess_extension(mime_type)
                    l.debug("guessed extension '{}' from MIME-type '{}'", ext, mime_type)
                    if ext in IMAGE_EXTENSIONS:
                        # Download document (image file)
                        img = img._replace(ext=ext, f_id=message.document.file_id)
                        self.on_image(img)
                    else:
                        self.send_message(message.chat.id, "I do not know how to handle that")

            elif message.photo:
                l.info("received photo from {1} ({0.sender.id}): {0.photo}",
                       message, self.build_name(message.sender))
                sorted_photo = sorted(message.photo, key=lambda p: p.file_size)
                if sorted_photo != message.photo:
                    l.critical("PhotoSizes were not sorted by size; {}", message)

                # Download the file (always jpg)
                img = img._replace(f_id=sorted_photo[-1].file_id)
                self.on_image(img)

            elif message.text:
                l.info("received text from {1} ({0.sender.id}): {0.text}",
                       message, self.build_name(message.sender))
                self.send_message(message.chat.id, "Just send me photos or images")

            else:
                l.warn("didn't handle update: {}", update)
                self.send_message(message.chat.id, "I do not know how to handle that")

            if not self.offset or upd_id >= self.offset:
                self.offset = upd_id + 1

    def handle_error(self, error):
        l.error("failed to fetch data; {}", error)
        # Delay next poll if there was an error
        time.sleep(self.conf.telegram.timeout)

    def poll_loop(self):
        timeout = self.conf.telegram.timeout
        l.info("poll loop initiated with timeout {}", timeout)

        i = 0
        while True:
            i += 1
            l.debug("poll #{}", i)

            # Long polling
            self.get_updates(
                timeout=timeout,
                offset=self.offset,
                on_success=self.handle_updates,
                on_error=self.handle_error
            ).wait()


class MyIRCClient(asyncirc.IRCClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connected = False

    def _process_data(self, line):
        try:
            code = int(line.split()[1])
        except:
            pass
        else:
            if code == 376:  # End of /MOTD command
                self._connected = True
                l.info("IRCClient connected")
            elif code == 433:  # Nickname is already in use
                self.nick += "_"
                self.send_raw("NICK {nick}".format(nick=self.nick))

        super()._process_data(line)

    def wait_connected(self, timeout=7):
        start = time.time()
        l.debug("Waiting for IRCClient to connect")
        while time.time() < start + timeout:
            if self._connected:
                return True
            time.sleep(0.1)
        else:
            return False


class ImageReceivedThread(Thread):
    def __init__(self, conf, irc_bot, tg_bot, img, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conf = conf
        self.irc_bot = irc_bot
        self.tg_bot = tg_bot
        self.img = img

    def reply(self, msg):
        self.tg_bot.send_message(
            self.img.c_id,
            msg,
            disable_web_page_preview=True,
            reply_to_message_id=self.img.m_id,
            on_success=partial(l.info, "sent message to {0.chat.username} ({0.chat.id}): {0.text}")
        )

    def run(self):
        # Show that we're doing something
        self.send_chat_action(self.img.c_id, botapi.ChatAction.PHOTO)

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
            l.error("Uncaught error in ImageReceivedThread: {}", e)
            raise
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

    fmt = logging.Formatter("| {levelname:^8} | {message} (from {name}; {threadName})",
                            style='{')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    # Filter out requests logging, for now
    handler.addFilter(
        lambda r: (not r.name.startswith("requests")) or r.levelno > 20
    )

    handlers = [handler]

    if conf.logging.active:
        fmt = logging.Formatter(
            "| {asctime} | {levelname:^8} | {message} (from {name}; {threadName})",
            style='{'
        )
        f = open(conf.logging.path or "errors.log", "a")
        handler = logging.StreamHandler(f)
        handler.setFormatter(fmt)
        conf_level = getattr(logging, (conf.logging.level or "WARN").upper())
        handler.addFilter(lambda r: r.levelno >= conf_level)

        f.write("-- started application; logging level: {}\n".format(conf_level))
        handlers.append(handler)

    logging.basicConfig(level=console_level, handlers=handlers)
    print("-- console logging level: {}".format(l.getEffectiveLevel()))


###############################################################################


def main():
    # Read config, init logging
    conf = config.read_file(CONFIG_FILE)
    init_logging(conf=conf, console_level=logging.INFO)
    l.info("config: {!s}", conf)

    # Verify other config
    if not verify_config(conf):
        return 2

    # Start IRC bot
    irc_bot = MyIRCClient(
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
    # Don't need to join channel because chanmode 'n' is not set
    irc_bot.join(conf.irc.channel)

    # Start Telegram bot
    tg_bot = TelegramImageBot(conf, token=conf.telegram.token)
    l.info("Me: {}", tg_bot.update_bot_info().wait())

    # Register main callback as a closure
    def on_image(img):
        nonlocal conf, irc_bot, tg_bot
        thread = ImageReceivedThread(
            conf=conf,
            irc_bot=irc_bot,
            tg_bot=tg_bot,
            img=img
        )
        thread.start()
        return thread

    tg_bot.on_image = on_image

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

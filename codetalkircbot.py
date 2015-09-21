#!/usr/bin/env python3

from collections import namedtuple
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

# Required to use my up2date fork
sys.path.insert(0, R"E:\Development\Python\twx.botapi")

import asyncirc
from imgurpython import ImgurClient
from imgurpython.helpers.error import ImgurClientError
from twx import botapi

import config


CONFIG_FILE = "config.yaml"
IMAGE_EXTENSIONS = (".jpg", ".png", ".gif")


def init_logging():
    class NewStyleLogRecord(logging.LogRecord):
        def getMessage(self):
            msg = self.msg
            if not isinstance(self.msg, str):
                msg = str(self.msg)
            if not isinstance(self.args, tuple):
                self.args = (self.args,)
            return msg.format(*self.args)
    logging.setLogRecordFactory(NewStyleLogRecord)

    fmt = logging.Formatter("| {levelname:^8} | {message} (from {name})",
                            style='{')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    # Filter out requests logging, for now
    handler.addFilter(
        lambda r: (not r.name.startswith("requests")) or r.levelno > 20
    )

    logging.basicConfig(level=logging.DEBUG, handlers=[handler])

init_logging()
l = logging.getLogger(__name__)


###############################################################################

_ImageInfo = namedtuple(
    '_ImageInfo',
    ['time', 'username', 'c_id', 'm_id', 'caption', 'ext', 'f_id',
     'remote_path', 'local_path', 'url']
)


class ImageInfo(_ImageInfo):
    __slots__ = ()

    def make_reply_func(self, bot):
        return partial(bot.send_message,
                       self.c_id,
                       disable_web_page_preview=True,
                       reply_to_message_id=self.m_id,
                       on_success=partial(l.info, "sent message | {}"))


class CodetalkIRCBot_Telegram(botapi.TelegramBot):

    def __init__(self, conf, on_file, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._offset = None
        self.conf = conf
        self.on_file = on_file

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

            c_id = message.chat
            self.send_chat_action(c_id, botapi.ChatAction.PHOTO)

            l.debug("handling update: {}", update)

            # Out data storage object
            img = ImageInfo(time=message.date, username=message.sender.username,
                            c_id=message.chat.id, m_id=message.message_id,
                            caption=message.caption, ext='.jpg', f_id=None,
                            remote_path=None, local_path=None, url=None)

            if message.document:
                # Check for image mime types
                mime_type = message.document.mime_type
                l.info("Received document from {0.sender.username}: {0.document}", message)
                if mime_type:
                    ext = mimetypes.guess_extension(mime_type)
                    l.debug("Guessed extension '{}' from MIME-type '{}'", ext, mime_type)
                    if ext in IMAGE_EXTENSIONS:
                        # Download document (image file)
                        img = img._replace(ext=ext, f_id=message.document.file_id)
                        Thread(target=self.download_file_thread,
                               args=(img, img.make_reply_func(self))).run()  # XXX change to start()
                    else:
                        self.send_message(message.chat.id, "I do not know how to handle that")

            elif message.photo:
                l.info("Received photo from {0.sender.username}: {0.photo}", message)
                sorted_photo = sorted(message.photo, key=lambda p: p.file_size)
                if sorted_photo != message.photo:
                    l.critical("PhotoSizes were not sorted by size; {}", message.photo)

                # Download the file (always jpg)
                img = img._replace(f_id=sorted_photo[-1].file_id)
                Thread(target=self.download_file_thread,
                       args=(img, img.make_reply_func(self))).run()  # XXX change to start()

            elif message.text:
                self.send_message(message.chat.id, "Just send me photos or images")
            else:
                l.warn("didn't handle update: {}", update)
                self.send_message(message.chat.id, "I do not know how to handle that")

            if not self.offset or upd_id >= self.offset:
                self.offset = upd_id + 1

    def download_file_thread(self, img, reply_func):
        def on_get_file_error(error):
            msg = "Error getting file info: {}".format(dict(error._asdict()))
            l.error(msg)
            reply_func(msg)

        file_info = self.get_file(img.f_id, on_error=on_get_file_error).wait()
        l.info("file info: {}", file_info)

        # Build file path
        directory = (Template(self.conf.storage.directory or "$temp/telegram")
                     .substitute(temp=tempfile.gettempdir()))
        directory = os.path.abspath(directory)
        basename = file_info.file_path.replace("/", "_")
        out_file = os.path.join(directory, basename)
        img = img._replace(remote_path=file_info.file_path, local_path=out_file)

        if os.path.exists(out_file):
            l.warn("File exists already, skipping download: {}", out_file)
        else:
            os.makedirs(directory, exist_ok=True)
            # Do download
            result = self.download_file(img.remote_path, out_file=img.local_path).wait()

            if isinstance(result, Exception):
                msg = "Error downloading file: {}".format(result)
                l.warn(msg)
                reply_func(msg)
                return
            else:
                l.info("Downloaded file to: {}", img.local_path)

        # Continue elsewhere
        self.on_file(img, reply_func)

    def handle_error(self, error):
        l.error("failed to fetch data; {}", error)

    def poll_loop(self):
        timeout = self.conf.telegram.timeout
        l.info("poll loop initiated with timeout {}", timeout)

        i = 0
        while True:
            i += 1
            l.debug("poll #{}", i)

            # Long polling
            result = self.get_updates(
                timeout=timeout,
                offset=self.offset,
                on_success=self.handle_updates,
                on_error=self.handle_error
            ).wait()
            if result.error_code != 200:
                # Delay next poll if there was an error
                time.sleep(timeout)


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


def upload_to_imgur(conf, img, reply_func):
    timestamp = datetime.fromtimestamp(img.time).strftime(
        conf.imgur.timestamp_format or "%Y-%m-%dT%H.%M.%S"
    )
    config = dict(album=conf.imgur.album,
                  name="{}_{}".format(timestamp, img.username),
                  title=img.caption)

    try:
        client = ImgurClient(conf.imgur.client_id, conf.imgur.client_secret,
                             refresh_token=conf.imgur.refresh_token)
        data = client.upload_from_path(img.local_path, config=config, anon=False)
    except ImgurClientError as e:
        msg = "Error uploading to imgur: {0.status_code} {0.error_message}".format(e)
        l.error(msg)
        reply_func(msg)
        raise

    l.info("uploaded image {}", data)
    l.debug("X-RateLimit-ClientRemaining: {}", client.credits['ClientRemaining'])

    return data['link']


###############################################################################

def main():
    msg = "logging level: {}".format(l.getEffectiveLevel())
    l.critical(msg)

    # Read and verify config
    conf = config.read_file(CONFIG_FILE)

    if not config.verify(conf):
        return 2

    # Start IRC bot
    irc_bot = MyIRCClient(
        host=conf.irc.host,
        port=conf.irc.port or 6667,
        nick=conf.irc.nick,
        realname=conf.irc.nick + '_',
        # use_ssl=conf.irc.ssl or False
    )
    irc_bot.start()
    if not irc_bot.wait_connected():
        l.error("Couldn't connect to IRC")
        return 3
    # Don't need to join spam because chanmode 'n' is not set
    # irc_bot.join(conf.irc.channel)

    # File handling logic
    def handle_image_file(img, reply_func):
        nonlocal conf, irc_bot

        url = upload_to_imgur(conf, img, reply_func)
        img = img._replace(url=url)

        # Send message
        pre_msg = ("<{{0.username}}>: {}{{0.url}}"
                   .format("{0.caption} " if img.caption else ""))
        msg = pre_msg.format(img)
        irc_bot.msg(conf.irc.channel, msg)

        if conf.storage.delete_images:
            os.remove(img.local_path)
            img = img._replace(local_path=None)

        reply_func("Image delivered. Uploaded to: " + img.url)

    # Start Telegram bot
    tg_bot = CodetalkIRCBot_Telegram(conf, on_file=handle_image_file, token=conf.telegram.token)
    l.info("Me: {}", tg_bot.update_bot_info().wait())

    # Main loop
    tg_bot.poll_loop()

if __name__ == '__main__':
    sys.exit(main())

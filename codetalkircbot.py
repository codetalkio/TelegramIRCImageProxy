#!/usr/bin/env python

from collections import namedtuple
from functools import partial
import logging
import mimetypes
import os
import sys
import tempfile
from threading import Thread

# Required to use my up2date fork
sys.path.insert(0, R"E:\Development\Python\twx.botapi")

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
    ['time', 'username', 'c_id', 'm_id', 'caption', 'f_id', 'remote_path', 'ext',
     'local_path']
)


class ImageInfo(_ImageInfo):
    __slots__ = ()

    def make_reply_func(self, bot):
        return partial(bot.send_message,
                       self.c_id,
                       reply_to_message_id=self.m_id,
                       on_success=partial(l.info, "sent message | result: {}"))


class CodetalkIRCBot_Telegram(botapi.TelegramBot):

    def __init__(self, *args, on_file=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._offset = None
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
                            caption=message.caption, ext='.jpg',
                            remote_path=None, local_path=None, f_id=None)

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
                               args=(img, img.make_reply_func(self))).run()
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
                       args=(img, img.make_reply_func(self))).run()

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

        out_file = tempfile.mkstemp(suffix=img.ext, prefix="telegram_")
        img = img._replace(remote_path=file_info.file_path, local_path=out_file)

        # Start download
        result = self.download_file(img.remote_path, out_file=img.local_path).wait()

        if isinstance(result, Exception):
            msg = "Error downloading file: {}".format(result)
            l.warn(msg)
            reply_func(msg)
            return
        else:
            l.info("Downloaded file to: {}", out_file)
            self.on_file(img, reply_func)

    def handle_error(self, error):
        l.error("failed to fetch data; {}", dict(error._asdict()))

    def poll_loop(self, timeout):
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


def upload_to_imgur(img, reply_func):
    pass  # TODO


###############################################################################


def main():
    msg = "logging level: {}".format(l.getEffectiveLevel())
    l.error(msg)

    # Read config
    conf = config.read_file(CONFIG_FILE)

    if not conf.telegram.token:
        l.error("no token found in config")
        return 2

    # Start IRC bot
    # TODO

    # File handling logic
    def handle_image_file(img, reply_func):
        l.info("Handling image {}", img)
        # url = upload_to_imgur(img.local_path, reply_func)
        # irc.post(url)
        if config.storage.delete_images:
            os.remove
        pass

    # Start Telegram bot
    bot = CodetalkIRCBot_Telegram(token=conf.telegram.token, on_file=handle_image_file)
    l.info("Me: {}", bot.update_bot_info().wait())

    # Main loop
    bot.poll_loop(conf.telegram.timeout or 1)

if __name__ == '__main__':
    sys.exit(main())

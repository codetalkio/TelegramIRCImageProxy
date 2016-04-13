from datetime import datetime
from functools import partial
import logging
import os
from string import Template
import tempfile

from imgurpython import ImgurClient
from imgurpython.helpers.error import ImgurClientError
from twx import botapi

from models.image import ImageDatabase

from . import BaseHandler


l = logging.getLogger(__name__)


class ImageHandler(BaseHandler):
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

    def run_(self):
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
            l.debug("Running ImageHandler with {}", self.img)
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
            self.reply("Oops, there was an error. Contact {} and run in circles.\n"
                       "Error: {}"
                       .format(self.conf.telegram.username_for_help, e))
            l.exception("Uncaught exception in ImageHandler: {}", e)

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
        pre_msg = ("<{{0.username}}> {{0.url}}{}"
                   .format(" {0.caption}" if self.img.caption else ""))
        msg = pre_msg.format(self.img)
        self.irc_bot.msg(self.conf.irc.channel, msg)

#!/usr/bin/env python3

from collections import defaultdict
from datetime import datetime
from functools import partial
import logging
import mimetypes
import os
from string import Template
import sys
import tempfile
from threading import Lock, Thread
import time

import asyncirc
from imgurpython import ImgurClient
from imgurpython.helpers.error import ImgurClientError
from twx import botapi

import config
from models.image import ImageInfo, ImageDatabase
from models.user import UserDatabase
from util import wrap, randomstr


CONFIG_FILE = "config.yaml"
IMAGE_EXTENSIONS = ('.jpg', '.png', '.gif')

l = logging.getLogger(__name__)


###############################################################################


class TelegramImageBot(botapi.TelegramBot):
    _command_handlers = defaultdict(list)

    def __init__(self, conf, on_image=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._offset = None
        self.conf = conf
        self.on_image = on_image

    # @command('cmdname') decorator
    @classmethod
    def command(cls, name):
        def decorator(func):
            cls._command_handlers[name].append(func)
            return func
        return decorator

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
                            username=None,
                            c_id=message.chat.id, m_id=message.message_id,
                            caption=message.caption, ext='.jpg',
                            remote_path=None, local_path=None, url=None, finished=False)

            if message.document:
                # Check for image mime types
                mime_type = message.document.mime_type
                l.info("received document from {0.sender}: {0.document}", message)
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
                l.info("received photo from {0.sender}: {0.photo}",
                       message)
                sorted_photo = sorted(message.photo, key=lambda p: p.file_size)
                if sorted_photo != message.photo:
                    l.critical("PhotoSizes were not sorted by size; {}", message)

                # Download the file (always jpg)
                img = img._replace(f_id=sorted_photo[-1].file_id)
                self.on_image(img)

            elif message.text:
                self.on_text(message)

            else:
                l.warn("didn't handle update: {}", update)
                self.send_message(message.chat.id, "I do not know how to handle that")

            if not self.offset or upd_id >= self.offset:
                self.offset = upd_id + 1

    def on_text(self, message):
        l.info("received text from {0.sender}: {0.text!r}", message)

        # check if this is a command
        if message.text.startswith("/") and len(message.text) > 1:
            cmd, *args = message.text[1:].split()
            cmd, _, botname = cmd.partition("@")
            if botname and botname != self.username:
                return
            for func in self._command_handlers[cmd]:
                if func(self, args, message):
                    break
        else:
            self.send_message(message.chat.id,
                              "Just send me photos or images or type /help for a list of commands")

    def handle_error(self, error):
        l.error("failed to fetch data; {}", error)
        # Delay next poll if there was an error
        time.sleep(self.conf.telegram.timeout or 5)

    def poll_loop(self):
        timeout = self.conf.telegram.timeout or 5
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


# Add text commands (how2decorator in class)
@TelegramImageBot.command('start')
def cmd_start(self, args, message):
    msg = wrap("""
        Authenticate yourself via /auth and follow the instructions.
        Afterwards you can send me photos or images,
        which I will upload
        and link to in the IRC channel
        {conf.irc.channel} on {conf.irc.host}.
    """).format(conf=self.conf)
    self.send_message(message.chat.id, msg)


TelegramImageBot.command('help')(cmd_start)


@TelegramImageBot.command('auth')
def cmd_auth(self, args, message):
    self.on_auth(message)


###############################################################################


class MyIRCClient(asyncirc.IRCBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._connected = False
        self.auth_map = {}
        self._auth_map_lock = Lock()

        self.on_chanmsg(self.__class__.on_msg_command)

    def new_auth_callback(self, callback, authcode=None):
        with self._auth_map_lock:
            while not authcode or authcode in self.auth_map:
                authcode = randomstr(10)
            l.debug("added authcode callback for: {}", authcode)
            self.auth_map[authcode] = callback
        return authcode

    def remove_auth_callback(self, authcode):
        with self._auth_map_lock:
            l.debug("removed authcode callback for: {}", authcode)
            del self.auth_map[authcode]

    def on_msg_command(self, nick, host, channel, message):
        _, _, text = message.partition(self.nick)
        if not text:
            return
        _, command, *args = text.split(" ")  # also strips ": " after nick
        print(command, args)
        if command == 'auth':
            l.info("auth attempt on IRC from {0[nick]} with {0[args]}", locals())
            with self._auth_map_lock:
                cb = self.auth_map.get(args[0])
                if cb:
                    l.debug("calling callback {1} for authcode: {0}", args[0], cb)
                    cb(args[1] if len(args) > 1 else nick)
                else:
                    self.msg(channel, "{}: Auth code invalid".format(nick))
                    l.info("no such authcode record: {}", args[0])
        else:
            self.msg(channel, "{}: Unknown command".format(nick))
            l.info("unknown IRC command message from {0[nick]}: {0[command]} {0[args]}", locals())

    # Check for successful connection and auto-rename if nick already in use
    def _process_data(self, line):
        try:
            code = int(line.split()[1])
        except:
            pass
        else:
            # Previously used 376 End of /MOTD command, but not all ircds send this
            if code == 266:  # Current global users
                self._connected = True
                l.info("IRC client connected as {}", self.nick)
            elif code == 433:  # Nickname is already in use
                self.nick += "_"
                self.send_raw("NICK {nick}".format(nick=self.nick))

        super()._process_data(line)

    def wait_connected(self, timeout=7):
        start = time.time()
        l.debug("Waiting for IRC client to connect")
        while time.time() < start + timeout:
            if self._connected:
                return True
            time.sleep(0.1)
        else:
            return False


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

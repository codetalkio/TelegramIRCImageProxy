from collections import defaultdict
import logging
import mimetypes
import time

from twx import botapi

from models.image import ImageInfo
from util import wrap


IMAGE_EXTENSIONS = ('.jpg', '.png', '.gif')

l = logging.getLogger(__name__)


class TelegramImageBot(botapi.TelegramBot):
    _command_handlers = defaultdict(list)

    def __init__(self, conf, user_db, on_image=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._offset = None
        self.conf = conf
        self.user_db = user_db
        self.on_image = on_image

    # @command('cmdname') decorator
    @classmethod
    def command(cls, name, admin=False):
        def decorator(func):
            cls._command_handlers[name].append((func, admin))
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
                # Command was for another bot (in group chat)
                return

            if cmd not in self._command_handlers:
                self.send_message(message.chat.id,
                                  "Unknown command. Type /help for a list of commands.")
                l.info("unknown command '{}' from {}", cmd, message.sender)
                return

            for func, admin in self._command_handlers[cmd]:
                if admin:
                    if message.sender.id not in (self.conf.telegram.admin or []):
                        self.send_message(message.chat.id,
                                          "You must be an admin to use this command.")
                    continue
                reply = func(self, args, message)
                if reply:
                    if reply is not True:
                        self.send_message(message.chat.id, reply)
                    break
        else:
            self.send_message(message.chat.id,
                              "Just send me photos or images or type /help for a list of commands")

    def handle_error(self, error):
        l.error("failed to fetch data; {}", error)
        # Delay next poll if there was an error
        time.sleep(self.conf.telegram.timeout or 60)

    def poll_loop(self):
        timeout = self.conf.telegram.timeout or 60
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


# Add text commands (how2decorator in-class)

@TelegramImageBot.command('start')
def cmd_start(self, args, message):
    """/start"""
    msg = wrap("""
        Authenticate yourself via /auth and follow the instructions.
        Afterwards you can send me photos or images,
        which I will upload
        and link to in the IRC channel
        {conf.irc.channel} on {conf.irc.host}.
    """).format(conf=self.conf)

    if message.sender.id in (self.conf.telegram.admin or []):
        msg = msg + "\n\n" + wrap("""
            More commands:

            /get_id - get your id

            More commands for admins:

            /blacklist [add | remove] <id> - modify the blacklist
        """)

    return msg


TelegramImageBot.command('help')(cmd_start)


@TelegramImageBot.command('auth')
def cmd_auth(self, args, message):
    """/auth"""
    self.on_auth(message)
    return True


@TelegramImageBot.command('get_id')
def cmd_get_id(self, args, message):
    """/get_id"""
    return message.sender.id


@TelegramImageBot.command('blacklist', True)
def cmd_blacklist(self, args, message):
    """/blacklist [add | remove] <id>"""
    funcs = dict(
        add=self.user_db.add_to_blacklist,
        remove=self.user_db.remove_from_blacklist
    )
    if not (len(args) == 2 and args[0].lower() in funcs and args[1].isdigit()):
        return ("Invalid arguments; expected an action and a user ID.\n"
                "Command signature: {}".format(cmd_blacklist.__doc__))

    if funcs[args[0].lower()](int(args[1])):
        return "Blacklist changed."
    else:
        return "Blacklist operation failed."

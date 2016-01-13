import logging
import time

from util import wrap

from . import BaseHandler

l = logging.getLogger(__name__)


class AuthHandler(BaseHandler):
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
        l.info("{1.sender} authenticated as '{0}'", name, self.message)

        self.authenticated = True

    def run_(self):
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
        while time.time() < start_time + (self.conf.irc.auth_timeout or 300):
            if self.authenticated:
                break
            time.sleep(0.5)
        else:
            l.info("authentication timed out for {0.sender}", self.message)
            self.tg_bot.send_message(self.message.chat.id, "Authentication timed out")

        # Finish thread
        self.irc_bot.remove_auth_callback(authcode)

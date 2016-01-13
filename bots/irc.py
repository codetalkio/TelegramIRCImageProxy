import logging
from threading import Lock
import time

import asyncirc

from util import randomstr


l = logging.getLogger(__name__)


class IRCBot(asyncirc.IRCBot):
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
        l.debug("IRC command message from {0[nick]}: {0[command]} {0[args]}", locals())
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

#!/usr/bin/env python

from functools import partial
import logging
import sys

from twx import botapi
import yaml

CONFIG_FILE = "config.yaml"


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

    fmt = logging.Formatter("| {levelname:^8} | {message} (from {name})", style='{')
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    # Filter out requests logging, for now
    handler.addFilter(lambda r: (not r.name.startswith("requests")) or r.levelno > 20)

    # TODO filter for requests
    logging.basicConfig(level=logging.DEBUG, handlers=[handler])

init_logging()
l = logging.getLogger(__name__)


##############################################################################


def preprocess_stack(items):
    return [(item[1], item[2], item[3], item[4][-1])
            for item in items]


def replace_with_type(type_, replace_type, data):
    if isinstance(data, type_):
        return replace_type(data)
    return data


class Config(dict):

    def __init__(self, items=None):
        if items is not None:
            if hasattr(items, 'items'):
                items = list(items.items())
            for i, (k, v) in enumerate(items):
                items[i] = (k, replace_with_type(dict, Config, v))
            super().__init__(items)
        else:
            super().__init__()

    def __getattr__(self, key):
        if key in self:
            return self[key]
        else:
            l.warn("AttrDict: did not find key '{}' in keys {}", key, self.keys())

            if l.getEffectiveLevel() <= logging.INFO:
                import inspect
                stack = inspect.stack(1)[1:]
                l.info("-- AttrDict stack --")
                for info in reversed(stack):
                    l.info('  File "{0[1]}", line {0[2]}, in {0[3]} -- {1}',
                           info, info[4][-1].strip())
                l.info("-- AttrDict stack -- end")

            return Config()  # return empty 'dict' as default


##############################################################################


class CodetalkIRCBot_Telegram(botapi.TelegramBot):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._offset = None

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

            l.info("handling update: {}", update)
            if message.photo:
                l.warn("Received photo from {0.sender.username}: {0.photo}", message)
                f_ids = set(ps.file_id for ps in message.photo)
                # f_id = f_ids[0]
                # self.send_photo(c_id, f_id, caption=f_id, reply_to_message_id=message.message_id)
                l.info("file ids {}", f_ids)
                sorted_photo = sorted(message.photo, key=lambda p: p.size)
                if sorted_photo != message.photo:
                    l.critical("PhotoSizes were not sorted by size; {}", message.photo)

                self.send_message(c_id, str(message.photo), reply_to_message_id=message.message_id,
                                  callback=partial(l.info, "sent message~ | result: {}"))

            if not self.offset or upd_id >= self.offset:
                self.offset = upd_id + 1

    def handle_error(self, error):
        l.error("failed to fetch data; {0}", dict(error._asdict()))

    def poll_loop(self, sleep):
        l.info("poll loop initiated with sleep {}", sleep)

        i = 1
        while True:
            l.debug("poll #{}", i)
            i = i + 1

            # Long polling
            self.get_updates(
                timeout=sleep,
                offset=self.offset,
                callback=self.handle_updates,  # on_succes=
                on_error=self.handle_error
            ).wait()


##############################################################################


def main():
    msg = "logging level: {}".format(l.getEffectiveLevel())
    l.error(msg)

    # Read config
    l.debug("config file: '{}'", CONFIG_FILE)
    with open(CONFIG_FILE) as f:
        config = Config(yaml.safe_load(f))
    l.debug("config: {!s}", config)

    if not config.telegram.token:
        l.error("no token found in config")
        return 2

    bot = CodetalkIRCBot_Telegram(token=config.telegram.token)
    l.info("Me: {}", bot.update_bot_info().wait())

    bot.poll_loop(config.telegram.sleep or 1)


if __name__ == '__main__':
    sys.exit(main())

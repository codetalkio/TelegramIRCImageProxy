import logging
import os

import yaml


l = logging.getLogger(__name__)


def _replace_with_type(type_, replace_type, data):
    if isinstance(data, type_) and not isinstance(data, replace_type):
        return replace_type(data)
    return data


class Config(dict):

    def __init__(self, items=None):
        if items is not None:
            if hasattr(items, 'items'):
                items = list(items.items())
            for i, (k, v) in enumerate(items):
                items[i] = (k, _replace_with_type(dict, self.__class__, v))
            super().__init__(items)
        else:
            super().__init__()

    def __getattr__(self, key):
        if key in self:
            return self[key]
        else:
            l.warn("AttrDict: did not find key '{}' in {}", key, self.keys())

            if l.getEffectiveLevel() <= logging.INFO:
                import inspect
                stack = inspect.stack(1)[1:]
                l.info("-- AttrDict stack --")
                for info in reversed(stack):
                    l.info('  File "{0[1]}", line {0[2]}, in {0[3]} -- {1}',
                           info, info[4][-1].strip())
                l.info("-- AttrDict stack -- end")

            return self.__class__()  # return empty 'Config' as default

    def update(self, other=None):
        if not other:
            return
        other = _replace_with_type(dict, self.__class__, other)
        return super().update(other)


def read_file(filename, consider_user_config=True):
    l.debug("reading config file: '{}'", filename)
    with open(filename) as f:
        config = Config(yaml.safe_load(f))
    l.debug("config: {!s}", config)

    if consider_user_config and config.user_config:
        if os.path.exists(config.user_config):
            with open(config.user_config) as f:
                user_config = Config(yaml.safe_load(f))
                l.debug("user_config: {!s}", user_config)
                config.update(user_config)

            l.debug("config with user_config: {!s}", config)
        else:
            l.warn("user_config file not found: '{}'", config.user_config)

    return config

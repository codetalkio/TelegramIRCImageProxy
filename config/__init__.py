import logging

import yaml


CONFIG_FILE = "config.yaml"

l = logging.getLogger(__name__)


def _replace_with_type(type_, replace_type, data):
    if isinstance(data, type_):
        return replace_type(data)
    return data


class Config(dict):

    def __init__(self, items=None):
        if items is not None:
            if hasattr(items, 'items'):
                items = list(items.items())
            for i, (k, v) in enumerate(items):
                items[i] = (k, _replace_with_type(dict, Config, v))
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


def read_file(filename=CONFIG_FILE):
    l.debug("reading config file: '{}'", CONFIG_FILE)
    with open(CONFIG_FILE) as f:
        config = Config(yaml.safe_load(f))
    l.debug("config: {!s}", config)
    return config

import json
import logging
import os


l = logging.getLogger(__name__)


class UserDatabase(object):
    def __init__(self, path):
        self.path = path
        self._user_cache = None

    @property
    def user_cache(self):
        if not self._user_cache:
            if not os.path.exists(self.path):
                self._user_cache = dict(name_map={}, blacklist=[])
            else:
                try:
                    with open(self.path, 'r') as f:
                        data = json.load(f)
                except Exception as e:
                    l.exception("Unable to read user cache; {}", e)

                # convert keys to integers because JSON can't do that
                blacklist = data.get('blacklist', {})
                self._user_cache = {int(k): v for k, v in blacklist.items()}

            l.debug("found {} mapped users", len(self._user_cache['name_map']))
            l.debug("found {} blacklisted users", len(self._user_cache['blacklist']))
        return self._user_cache

    def _write_cache(self):
        if self._user_cache:
            with open(self.path, 'w') as f:
                json.dump(self._user_cache, f, indent=4, sort_keys=True)

    @property
    def name_map(self):
        return self.user_cache['name_map']

    @property
    def blacklist(self):
        return self.user_cache['blacklist']

    def add_to_name_map(self, id_, name):
        self.name_map[id_] = name
        l.info("added to name_map: {}: {}", id_, name)
        self._write_cache()

    def add_to_blacklist(self, id_):
        self.name_map.append(id_)
        l.info("added to blacklist: {}", id_)
        self._write_cache()
        return True

    def remove_from_blacklist(self, id_):
        if id_ in self.name_map:
            self.name_map.remove(id_)
            l.info("removed from blacklist: {}", id_)
        else:
            l.info("attempted to remove {} from blacklist, but it wasn't there".format(id_))
        self._write_cache()
        return True

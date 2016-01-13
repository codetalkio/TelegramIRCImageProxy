__all__ = ('AuthHandler', 'ImageHandler')

import logging
from threading import Thread


l = logging.getLogger(__name__)


class BaseHandler(Thread):
    thread_num = 0

    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            'name',
            "{0.__name__}-{0.thread_num}".format(self.__class__)
        )
        super().__init__(*args, **kwargs)

    def run(self):
        try:
            self.run_()
        except:
            l.exception("error in {}", self.__class__.__name__)

    # @abstractmethod
    def run_(self):
        pass

# I though Python could handly cyclic imports,
# but it seems like that is not the case.
from .auth import AuthHandler
from .image import ImageHandler

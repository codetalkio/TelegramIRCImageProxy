from collections import namedtuple
import logging
import sqlite3


l = logging.getLogger(__name__)

ImageInfo = namedtuple(
    'ImageInfo',
    ['f_id', 'time', 'username', 'c_id', 'm_id', 'caption', 'ext',
     'remote_path', 'local_path', 'url', 'finished']
)


class ImageDatabase(object):
    def __init__(self, dbpath):
        self.db = sqlite3.connect(dbpath)
        # self.db.row_factory = sqlite3.Row

        self.create_table()

    def create_table(self):
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS images (
                f_id TEXT PRIMARY KEY,
                time INTEGER,
                username TEXT,
                c_id INTEGER,
                m_id INTEGER,
                caption TEXT,
                ext TEXT,
                remote_path TEXT,
                local_path TEXT,
                url TEXT,
                finished INTEGER
            )"""
        )

    def find_image(self, img):
        cursor = self.db.execute("SELECT * FROM images WHERE f_id = ?", (img.f_id,))
        row = cursor.fetchone()
        if row is None:
            return

        db_img = ImageInfo(*row)
        l.debug("found image in database: {}", db_img)
        return db_img

    def get_unfinished_images(self):
        results = [ImageInfo(*row)
                   for row in self.db.execute("SELECT * FROM images WHERE finished = 0")]

        l.debug("found {} unfinished images in database", len(results))
        return results

    def insert_image(self, img):
        self.db.execute(
            "INSERT INTO images VALUES (%s)"
            % ", ".join(("?",) * len(img)),
            img
        )
        self.db.commit()
        l.debug("inserted image into database: {}", img)

    def update_image(self, img):
        update_columns = ('remote_path', 'local_path', 'url', 'finished')
        self.db.execute(
            "UPDATE images SET %s WHERE f_id = :f_id"
            % ", ".join("{0}=:{0}".format(key) for key in update_columns),
            img._asdict()
        )
        self.db.commit()
        l.debug("updated image in database: {}", img)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

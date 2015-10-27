import base64
import math
import os
import re
import textwrap


def wrap(text):
    text = textwrap.dedent(text.strip("\n")).strip()  # dedent
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)      # replace single linebreaks
    text = re.sub(r"\n{2,}", "\n\n", text)            # reduce to max 2 linebreaks
    return text


def randomstr(minlen):
    rand = os.urandom(math.ceil(minlen / 8 * 6))
    return base64.b64encode(rand).decode('ascii')

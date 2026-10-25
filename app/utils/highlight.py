import base64
import hashlib
from functools import lru_cache

from bs4 import BeautifulSoup  # type: ignore
from pygments import highlight as phighlight  # type: ignore
from pygments.formatters import HtmlFormatter  # type: ignore
from pygments.lexers import get_lexer_by_name  # type: ignore
from pygments.lexers import guess_lexer  # type: ignore

from app.config import CODE_HIGHLIGHTING_THEME

_FORMATTER = HtmlFormatter(style=CODE_HIGHLIGHTING_THEME)

HIGHLIGHT_CSS = _FORMATTER.get_style_defs()
HIGHLIGHT_CSS_HASH = base64.b64encode(
    hashlib.sha256(HIGHLIGHT_CSS.encode()).digest()
).decode()


@lru_cache(256)
def highlight(html: str) -> str:
    soup = BeautifulSoup(html, "html5lib")
    for code in soup.find_all("code"):
        parent = code.parent
        if parent is None or parent.name != "pre":
            continue

        # Replace <br> tags with line breaks (Mastodon sends code like this)
        code_content = (
            code.encode_contents().decode().replace("<br>", "\n").replace("<br/>", "\n")
        )

        # If this comes from a microblog.pub instance we may have the language
        # in the class name
        if "data-microblogpub-lexer" in code.attrs:
            try:
                lexer = get_lexer_by_name(code.attrs["data-microblogpub-lexer"])
            except Exception:
                lexer = guess_lexer(code_content)

            # Replace the code with Pygment output
            code_content = (
                code_content.replace("&gt;", ">")
                .replace("&lt;", "<")
                .replace("&amp;", "&")
            )
            highlighted = BeautifulSoup(
                phighlight(code_content, lexer, _FORMATTER), "html5lib"
            ).body
            assert highlighted is not None  # html5lib always builds a <body>
            parent.replace_with(highlighted.contents[0])
        else:
            code.name = "div"
            # class is multi-valued, so bs4 hands back a list
            code["class"] = " ".join([*(code.get("class") or []), "highlight"])

    assert soup.body is not None
    return soup.body.encode_contents().decode()

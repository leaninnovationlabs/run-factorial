# ruff: noqa: E501 -- email markup keeps each element on one line, inline styles and all.
"""The branded HTML body every account email shares: the mark and wordmark, a card with one
action, and the link written out under its button.

Email clients are not browsers: Gmail drops `<style>` blocks for some readers and never shows
SVG, and Outlook lays out with Word. So the layout is nested tables with inline styles, the mark
is a PNG the deployment serves itself (`ui/public/email-mark.png`, so no install loads an image
from another), and the button carries a VML copy for Outlook. The colours are the landing site's
tokens (`landing/src/styles.css`). Every value that reaches the markup is escaped.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Final

__all__ = ["Email", "render"]

_GROUND: Final = "#eef1ef"
_PANEL: Final = "#ffffff"
_INK: Final = "#121a18"
_INK_2: Final = "#3e4a46"
_MUTED: Final = "#6b7672"
_LINE: Final = "#cdd5d1"
_ACCENT: Final = "#d9571c"
_SANS: Final = "'IBM Plex Sans','Helvetica Neue',Helvetica,Arial,sans-serif"
_DISPLAY: Final = "Archivo,'Helvetica Neue',Helvetica,Arial,sans-serif"
_MONO: Final = "'IBM Plex Mono',Menlo,Consolas,monospace"


@dataclass(frozen=True)
class Email:
    #: The line an inbox shows after the subject; never shown in the body.
    preview: str
    heading: str
    lead: str
    paragraphs: tuple[str, ...]
    button: str
    url: str
    note: str
    #: `<dashboard>/email-mark.png`; empty draws the wordmark alone.
    mark_url: str
    #: Where the footer's wordmark links: the deployment's dashboard.
    home_url: str


def render(email: Email) -> str:
    e = {
        key: escape(value, quote=True)
        for key, value in vars(email).items()
        if isinstance(value, str)
    }
    paragraphs = "".join(
        f'<p style="margin:0 0 14px;font-family:{_SANS};font-size:15px;line-height:1.6;'
        f'color:{_INK_2};">{escape(text)}</p>'
        for text in email.paragraphs
    )
    mark = (
        f'<img src="{e["mark_url"]}" width="28" height="28" alt="" '
        f'style="display:block;width:28px;height:28px;border:0;border-radius:3px;">'
        if email.mark_url
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{e["heading"]}</title>
<!--[if mso]><style>table,td,p,a{{font-family:Arial,Helvetica,sans-serif !important;}}</style><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:{_GROUND};-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;">{e["preview"]}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_GROUND};">
<tr><td align="center" style="padding:40px 16px 32px;">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px;">
<tr><td style="padding:0 4px 20px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="padding-right:10px;vertical-align:middle;">{mark}</td>
<td style="vertical-align:middle;font-family:{_DISPLAY};font-size:21px;font-weight:800;letter-spacing:-0.5px;color:{_INK};">Factorial</td>
</tr></table>
</td></tr>
<tr><td style="background-color:{_PANEL};border:1px solid {_LINE};border-radius:8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td style="height:4px;background-color:{_ACCENT};border-radius:8px 8px 0 0;font-size:0;line-height:0;">&nbsp;</td></tr>
<tr><td style="padding:36px 40px 12px;">
<h1 style="margin:0 0 6px;font-family:{_DISPLAY};font-size:24px;font-weight:800;letter-spacing:-0.4px;line-height:1.25;color:{_INK};">{e["heading"]}</h1>
<p style="margin:0 0 24px;font-family:{_SANS};font-size:15px;line-height:1.5;color:{_MUTED};">{e["lead"]}</p>
{paragraphs}
</td></tr>
<tr><td style="padding:8px 40px 28px;">
<!--[if mso]>
<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{e["url"]}" style="height:46px;v-text-anchor:middle;width:220px;" arcsize="10%" stroke="f" fillcolor="{_ACCENT}"><w:anchorlock/><center style="color:#ffffff;font-family:Arial,sans-serif;font-size:15px;font-weight:bold;">{e["button"]}</center></v:roundrect>
<![endif]-->
<!--[if !mso]><!-->
<a href="{e["url"]}" target="_blank" style="display:inline-block;background-color:{_ACCENT};color:#ffffff;font-family:{_SANS};font-size:15px;font-weight:600;line-height:1;text-decoration:none;padding:15px 26px;border-radius:5px;">{e["button"]}</a>
<!--<![endif]-->
</td></tr>
<tr><td style="padding:0 40px 32px;">
<p style="margin:0 0 6px;font-family:{_SANS};font-size:13px;line-height:1.5;color:{_MUTED};">If the button does not open, copy this link into the browser:</p>
<p style="margin:0;font-family:{_MONO};font-size:12px;line-height:1.5;word-break:break-all;"><a href="{e["url"]}" style="color:{_ACCENT};text-decoration:underline;">{e["url"]}</a></p>
</td></tr>
<tr><td style="padding:20px 40px 28px;border-top:1px solid {_LINE};">
<p style="margin:0;font-family:{_SANS};font-size:13px;line-height:1.6;color:{_MUTED};">{e["note"]}</p>
</td></tr>
</table>
</td></tr>
<tr><td style="padding:20px 4px 0;font-family:{_SANS};font-size:12px;line-height:1.6;color:{_MUTED};">
An automated email from <a href="{e["home_url"]}" style="color:{_MUTED};text-decoration:underline;">Factorial</a>. Replies are not read.
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

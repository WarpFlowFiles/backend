"""HTML email in the site's style.

Email clients ignore most CSS, so the layout is tables with inline styles.
The logo is an inline attachment referenced by cid, not a remote image.
"""

from html import escape
from pathlib import Path

from app.config import format_size
from app.services.messages import OutgoingMessage, link_note, plural

LOGO_CID = "warpflow-logo"
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "email-logo.png"

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
TEXT = "#111111"
MUTED = "#6b6b6b"
SURFACE = "#f2f2f2"
CARD = "#eeeeee"


def files_label(count: int) -> str:
    return plural(count, "файл", "файла", "файлов")


def _multiline(text: str) -> str:
    return escape(text).replace("\n", "<br>")


def _extension(name: str) -> str:
    ext = name.rsplit(".", 1)[1] if "." in name else ""
    return escape(ext[:4].upper()) or "•"


def _file_rows(message: OutgoingMessage) -> str:
    """Each file with its own download button, so nothing has to be fetched from the site first."""
    rows = []
    for i, f in enumerate(message.files):
        border = f"border-top:1px solid {SURFACE};" if i else ""
        url = escape(message.file_url(f))
        rows.append(
            f"""
            <tr>
              <td width="52" style="padding:12px 0;{border}">
                <div style="width:44px;height:44px;line-height:44px;border-radius:14px;background:{CARD};
                            text-align:center;font:700 11px/44px {FONT};color:{TEXT};letter-spacing:.3px">{_extension(f.name)}</div>
              </td>
              <td style="padding:12px 12px;{border}font:500 15px/1.35 {FONT};color:{TEXT};word-break:break-word">
                {escape(f.name)}<br><span style="color:{MUTED};font-size:13px">{format_size(f.size)}</span>
              </td>
              <td width="1" align="right" style="padding:12px 0;{border}white-space:nowrap">
                <a href="{url}" target="_blank" download
                   style="display:inline-block;padding:10px 18px;border-radius:99px;background:#1e1e1e;
                          font:600 14px {FONT};color:#ffffff;text-decoration:none">Скачать</a>
              </td>
            </tr>"""
        )
    return "".join(rows)


def _download_block(message: OutgoingMessage) -> str:
    link = escape(message.link)
    code = f"{message.code[:3]} {message.code[3:]}"
    return f"""
      <tr><td align="center" style="padding:28px 0 0">
        <table role="presentation" cellpadding="0" cellspacing="0"><tr>
          <td align="center" bgcolor="#1e1e1e" style="border-radius:99px">
            <a href="{link}" target="_blank"
               style="display:inline-block;padding:17px 44px;border-radius:99px;font:600 17px {FONT};
                      color:#ffffff;text-decoration:none">Открыть на сайте</a>
          </td>
        </tr></table>
      </td></tr>
      <tr><td align="center" style="padding:24px 0 0;font:500 14px {FONT};color:{MUTED}">
        или введите код на сайте
      </td></tr>
      <tr><td align="center" style="padding:6px 0 0;font:700 32px {FONT};color:{TEXT};letter-spacing:4px">
        {code}
      </td></tr>
      <tr><td align="center" style="padding:6px 0 0;font:500 13px {FONT};color:{MUTED}">
        {escape(link_note(message))}
      </td></tr>
      <tr><td align="center" style="padding:20px 0 0;font:400 12px/1.5 {FONT};color:{MUTED};word-break:break-all">
        Если кнопки не работают, откройте ссылку:<br>
        <a href="{link}" target="_blank" style="color:{MUTED}">{link}</a>
      </td></tr>"""


def render_email(message: OutgoingMessage) -> str:
    """The email body: the text, and the files as download buttons (never attachments)."""
    has_files = bool(message.files)
    title = "Вам отправили файлы" if has_files else "Вам отправили сообщение"
    preheader = message.text[:120] if message.text else f"{title} через WarpFlow"

    sections = []
    if message.text:
        sections.append(
            f"""
      <tr><td style="padding:24px 0 0">
        <div style="padding:18px 20px;border-radius:20px;background:{SURFACE};
                    font:400 16px/1.5 {FONT};color:{TEXT};word-break:break-word">{_multiline(message.text)}</div>
      </td></tr>"""
        )
    if has_files:
        sections.append(
            f"""
      <tr><td style="padding:28px 0 4px;font:600 13px {FONT};color:{MUTED};text-transform:uppercase;letter-spacing:.6px">
        {files_label(len(message.files))} · {format_size(message.total_size)}
      </td></tr>
      <tr><td>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{_file_rows(message)}
        </table>
      </td></tr>"""
        )
        sections.append(_download_block(message))

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light only">
  <title>{escape(title)}</title>
</head>
<body style="margin:0;padding:0;background:{SURFACE}">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{SURFACE}">
    <tr><td align="center" style="padding:32px 12px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">
        <tr><td style="padding:0 8px 18px">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="padding-right:12px"><img src="cid:{LOGO_CID}" width="44" height="44" alt=""
                 style="display:block;border:0;border-radius:12px"></td>
            <td style="font:700 24px {FONT};color:{TEXT};letter-spacing:-.3px">WarpFlow</td>
          </tr></table>
        </td></tr>
        <tr><td bgcolor="#ffffff" style="border-radius:28px;padding:36px 32px 34px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            <tr><td style="font:700 26px/1.25 {FONT};color:{TEXT}">{title}</td></tr>
            <tr><td style="padding:8px 0 0;font:500 16px {FONT};color:{MUTED}">
              От: <span style="color:{TEXT};font-weight:600">{escape(message.from_name)}</span>
            </td></tr>
            {"".join(sections)}
          </table>
        </td></tr>
        <tr><td align="center" style="padding:22px 16px 0;font:400 13px/1.6 {FONT};color:{MUTED}">
          Отправлено через WarpFlow — быстрая передача файлов<br>
          Если вы не ждали это письмо, просто удалите его.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

"""IMAP message-parsing edge cases: multipart bodies, encoded headers,
attachments that shouldn't be mistaken for the reply text."""
from email.message import EmailMessage as MimeMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.services.imap_listener import _decode, _extract_body


def test_extract_body_simple_plain_text():
    msg = MimeMessage()
    msg.set_content("Sounds good, let's talk Tuesday.")
    assert _extract_body(msg).strip() == "Sounds good, let's talk Tuesday."


def test_extract_body_multipart_prefers_plain_text_over_html():
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("<p>Sounds <b>good</b></p>", "html"))
    msg.attach(MIMEText("Sounds good", "plain"))
    assert "Sounds good" in _extract_body(msg)
    assert "<p>" not in _extract_body(msg)


def test_extract_body_multipart_with_attachment_ignores_attachment():
    msg = MIMEMultipart()
    msg.attach(MIMEText("Please see attached", "plain"))
    attachment = MIMEText("not the real reply body", "plain")
    attachment.add_header("Content-Disposition", "attachment", filename="notes.txt")
    msg.attach(attachment)
    body = _extract_body(msg)
    assert "Please see attached" in body
    assert "not the real reply body" not in body


def test_extract_body_multipart_no_plain_part_returns_empty():
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("<p>HTML only</p>", "html"))
    assert _extract_body(msg) == ""


def test_extract_body_handles_non_utf8_charset():
    msg = MimeMessage()
    msg.set_content("café résumé", charset="latin-1")
    assert "caf" in _extract_body(msg)


def test_decode_plain_ascii_subject_passthrough():
    assert _decode("Re: quick question") == "Re: quick question"


def test_decode_mime_encoded_word_subject():
    # RFC 2047 encoded-word subject, as many mail clients send for non-ASCII text
    encoded = "=?UTF-8?B?UsOpOiBxdWljayBxdWVzdGlvbg==?="
    assert _decode(encoded) == "Ré: quick question"


def test_decode_none_returns_empty_string():
    assert _decode(None) == ""


def test_decode_empty_string_returns_empty_string():
    assert _decode("") == ""

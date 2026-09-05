"""Rendering a message body that a stranger wrote.

Everything in this package exists to take bytes an attacker chose and turn
them into something safe to put on the page. The split is deliberate:

    css_sanitize    tinycss2 — stylesheets and style attributes
    html_sanitize   nh3 — tags, attributes, URL policy
    quote_trim      where the reply ends and the quoted history begins
    plain_text      escape, linkify and depth-colour a text/plain body
    frame_document  the sandboxed document and the CSP that contains it
    image_policy    whether a remote image may load, and under whose URL
    fetch_guard     the SSRF guard in front of the image proxy
    dark            whether a message may be restyled for the dark theme

No module here trusts its input, and none of them is the only thing
standing between a hostile message and the reader: the sanitisers, the
sandboxed frame, and the Content-Security-Policy on that frame each
assume the other two might fail.
"""

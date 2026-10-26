"""M4: bypasses of the plain-text check. Everything is NFKC-normalised, stripped of combining
marks and folded for look-alike characters before the checks run, and the location, link and
markup detectors are loose enough to catch the usual evasions. The M3 list of ordinary facts
is the false-positive guard."""
from __future__ import annotations

import pytest
from test_fix2_m3_text_false_positives import LEGITIMATE

from app.textsafety import plain_text_problem

BYPASSES = {
    # coordinates
    "fullwidth digits": "４３．６５３２，－７９．８３３２",
    "unicode minus": "43.6532, −79.3832",
    "decimal comma": "43,6532 -79,3832",
    "hemisphere letters": "43.6532 N, 79.3832 W",
    "degree markers": "43.6532°N 79.3832°W",
    "semicolon": "43.65;-79.38",
    "within 20 chars": "at 43.6532 then maybe -79.3832",
    "combining marks": "4́3.6532, -79.3832",
    "dms": "43°39′11″N 79°23′00″W",
    "dms ascii": "43°39'11\"N",
    "lat token": "lat 43 lon -79",
    "latitude token": "latitude: 43.6",
    "lng token": "lng=-79.38",
    # links and schemes
    "scheme upper": "HTTPS://X.EXAMPLE",
    "bare domain": "evil.ly/x",
    "short link": "goo.gl/abc",
    "bitly": "bit.ly/3x",
    "dot com": "visit example.com tonight",
    "dot ca": "see cafe.ca",
    "email": "write a@b.ca",
    "mailto": "mailto:a@b.c",
    "tel": "tel:+14165550123",
    "javascript": "javascript:alert(1)",
    "data uri": "data:text/html,x",
    "fullwidth scheme": "ｊａｖａｓｃｒｉｐｔ:alert(1)",
    "cyrillic homoglyph scheme": "javаscript:alert(1)",
    "cyrillic homoglyph domain": "gооgle.com",
    "combining domain": "g̈oogle.com",
    "ideographic stop": "example。com",
    # markup
    "ascii markup": "<script>x</script>",
    "fullwidth brackets": "＜script＞",
    "angle quotes": "〈b〉",
    "entity lt": "&lt;b&gt;",
    "entity numeric": "&#60;b&#62;",
    "percent": "%3Cscript%3E",
    # addresses
    "name then number": "Yonge Street 123",
    "number street abbr": "123 Queen St W",
    "french rue": "45 Rue Saint-Denis",
    "french route": "12 route Principale",
    "capital court": "5 Elm Court",
    "lowercase street": "123 queen street",
    "drive": "88 Maple Drive",
    "king": "1 King Street West",
    # postal codes
    "postal": "M5V 2T6",
    "postal compact": "m5v2t6",
    "fsa with city": "Toronto M5V",
    "fsa with comma": "in Toronto, M5V",
    "fsa with province": "ON M5V",
}


@pytest.mark.parametrize("name", list(BYPASSES))
def test_each_bypass_is_caught(name) -> None:
    assert plain_text_problem(BYPASSES[name]), f"{name}: {BYPASSES[name]!r} was accepted"


@pytest.mark.parametrize("text", LEGITIMATE)
def test_the_legitimate_facts_are_not_caught(text) -> None:
    assert not plain_text_problem(text, allow_joiners=True), text


@pytest.mark.parametrize("text", [
    "$4.50, $3.25", "4.5 stars and 2.5 km", "20°C and sunny",
    "3.5 hours, 2.5 of them driving", "at 6 pm", "section 12 of the museum",
])
def test_numbers_that_are_not_coordinates_pass(text) -> None:
    assert not plain_text_problem(text), text

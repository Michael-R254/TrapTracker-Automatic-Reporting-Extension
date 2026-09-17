"""What each sanitiser policy actually does — recorded, not remembered.

There are TWO policies now, and the split is the security model:

* the **document** policy guards everything reaching the page through the report
  markdown, model-authored text included, and permits **no SVG at all**;
* the **chart** policy guards the agent's own computed charts, which never travel
  in the document — they are held aside and injected after the document has been
  sanitised, so this policy is reached only by holding a per-render nonce.

These pins were written at Stage 0 against a single permissive policy. They are
restructured here rather than edited, because "update the tests to match the new
behaviour" is where a guard quietly becomes a rubber stamp. The three properties
Stage 0 separated are kept, and asserted against BOTH policies, so the difference
between them is now itself under test: the document may not render SVG, the chart
policy may, and neither may execute or fetch anything.

Every pin keeps a demonstration that it CAN fail. That falsifiability is what made
them worth having: a test asserting `<script>` is stripped passes just as happily
against a sanitiser that strips everything, or against one that was never called.
"""

from __future__ import annotations

import nh3
import pytest

from ttr.web.app import (_ALLOWED_ATTRS, _ALLOWED_TAGS, _CHART_ATTRS, _CHART_TAGS,
                         _SVG_TAGS, _URL_SCHEMES)


def document(html: str, *, tags=None, attributes=None, url_schemes=None) -> str:
    """Sanitise as the DOCUMENT is sanitised — model text included."""
    return nh3.clean(html,
                     tags=_ALLOWED_TAGS if tags is None else tags,
                     attributes=_ALLOWED_ATTRS if attributes is None else attributes,
                     url_schemes=_URL_SCHEMES if url_schemes is None else url_schemes)


def chart(html: str, *, tags=None, attributes=None, url_schemes=None) -> str:
    """Sanitise as a HELD-ASIDE CHART is sanitised — generator output only."""
    return nh3.clean(html,
                     tags=_CHART_TAGS if tags is None else tags,
                     attributes=_CHART_ATTRS if attributes is None else attributes,
                     url_schemes=_URL_SCHEMES if url_schemes is None else url_schemes)


CHART_SHAPED = ('<svg role="img" width="400" height="60">'
                '<rect fill="#b00020" width="400" height="60"></rect>'
                '<text x="10" y="38" fill="#fff" font-size="28">9,999 events</text>'
                '</svg>')

EXECUTION_VECTORS = [
    ("script",        '<svg><script>alert(1)</script></svg>',            "alert(1)"),
    ("onload",        '<svg><rect onload="alert(1)" width="9"/></svg>',  "onload"),
    ("onclick",       '<svg><rect onclick="alert(1)" width="9"/></svg>', "onclick"),
    ("svg onload",    '<svg onload="alert(1)"><rect width="9"/></svg>',  "onload"),
    ("foreignObject", '<svg><foreignObject><script>alert(1)</script></foreignObject></svg>',
                      "alert(1)"),
    ("javascript:",   '<img src="javascript:alert(1)">',                 "javascript:"),
    ("animate",       '<svg><rect width="9"><animate attributeName="width" to="99"/></rect></svg>',
                      "animate"),
]

FETCH_VECTORS = [
    ("svg <image href>", '<svg><image href="https://evil.test/t.png" width="9"/></svg>'),
    ("svg <use href>",   '<svg><use href="https://evil.test/x#y"/></svg>'),
    ("svg <a href>",     '<svg><a href="https://evil.test/x"><text>t</text></a></svg>'),
    ("xlink:href",       '<svg><a xlink:href="https://evil.test/x"><text>t</text></a></svg>'),
    ("external img",     '<img src="https://evil.test/track.png">'),
    ("<style> block",    '<style>body{background:url(https://evil.test/x)}</style>'),
    ("style attribute",  '<svg><rect style="background:url(https://evil.test/x)" width="9"/></svg>'),
    ("iframe",           '<iframe src="https://evil.test"></iframe>'),
]


# =========================================================================== #
# THE DOCUMENT POLICY — what model-authored text can produce. Nothing.
# =========================================================================== #
def test_the_document_policy_permits_no_svg_at_all():
    """The stage's whole point, and the reason the 2026-08-15 xfail now passes.

    Chart-shaped markup is not sanitised into harmlessness — there is no tag left
    for it to become. Note what actually happens to the SUBTREE: `<svg>` puts the
    parser into foreign content, so removing the tags takes their text with them
    rather than unwrapping it. The fabricated figure disappears entirely.
    """
    out = document(CHART_SHAPED)

    assert "<svg" not in out and "<rect" not in out and "<text" not in out
    assert "9,999 events" not in out, "the whole subtree goes, text included"


def test_the_two_model_paths_are_neutralised_differently_and_both_safely():
    """Worth pinning together, because the difference is easy to misread.

    Narrative-LLM prose reaches the sanitiser as markup, so chart-shaped markup
    in it is DELETED, subtree and all, while the prose around it survives. A VLM
    description is HTML-escaped upstream at the interpolation (Stage 1), so it
    reaches the sanitiser as text and is DISPLAYED — which is what that section
    is for: it quotes what the model said as evidence.

    Neither can render an element. One disappears, one is shown; the safety
    property is identical and only the disclosure differs.
    """
    narrative = document('<p>Peaked sharply. <svg><text>4242</text></svg> Steady after.</p>')
    assert "4242" not in narrative
    assert "Peaked sharply." in narrative and "Steady after." in narrative

    described = document('<p>&lt;svg&gt;&lt;text&gt;4242&lt;/text&gt;&lt;/svg&gt;</p>')
    assert "&lt;svg&gt;" in described, "escaped model text stays visible as text"
    assert "<svg" not in described


@pytest.mark.parametrize("tag", sorted(_SVG_TAGS))
def test_no_svg_tag_is_in_the_document_policy(tag):
    assert tag not in _ALLOWED_TAGS, f"{tag} is renderable from model text"


def test_that_the_document_svg_pin_can_fail():
    """Put the tags back and the same markup renders — so the pin is real."""
    assert "<rect" not in document(CHART_SHAPED)
    assert "<rect" in document(CHART_SHAPED, tags=_ALLOWED_TAGS | _SVG_TAGS,
                               attributes=_CHART_ATTRS)


@pytest.mark.parametrize("name,markup,banned", EXECUTION_VECTORS)
def test_the_document_policy_executes_nothing(name, markup, banned):
    assert banned not in document(markup), f"{name} survived the document policy"


@pytest.mark.parametrize("name,markup", FETCH_VECTORS)
def test_the_document_policy_fetches_nothing(name, markup):
    assert "evil.test" not in document(markup), f"{name} left a remote reference"


def test_that_the_document_fetch_pin_can_fail():
    pixel = '<img src="https://evil.test/track.png">'
    assert "evil.test" not in document(pixel)
    assert "evil.test" in document(pixel, url_schemes={"data", "https"})


def test_a_data_uri_thumbnail_is_still_permitted_in_the_document():
    """Narrowing removed SVG, not the figure/thumbnail vocabulary."""
    assert "data:image/gif" in document('<img src="data:image/gif;base64,R0lGOD">')
    assert '<div class="x">' in document('<div class="x">caption</div>')


# =========================================================================== #
# THE CHART POLICY — privileged, and reached only by holding the nonce.
# =========================================================================== #
def test_the_chart_policy_still_renders_a_chart():
    out = chart(CHART_SHAPED)

    assert "<svg" in out and "<rect" in out and "<text" in out
    assert 'fill="#b00020"' in out and 'width="400"' in out


def test_that_the_chart_rendering_pin_can_fail():
    assert "<rect" in chart(CHART_SHAPED)
    assert "<rect" not in chart(CHART_SHAPED, tags=_CHART_TAGS - _SVG_TAGS)


@pytest.mark.parametrize("name,markup,banned", EXECUTION_VECTORS)
def test_the_chart_policy_executes_nothing_either(name, markup, banned):
    """Privileged is not unchecked. Trusted markup is still sanitised."""
    assert banned not in chart(markup), f"{name} survived the chart policy"


@pytest.mark.parametrize("name,markup", FETCH_VECTORS)
def test_the_chart_policy_fetches_nothing_either(name, markup):
    assert "evil.test" not in chart(markup), f"{name} left a remote reference"


def test_script_content_is_removed_not_merely_unwrapped():
    """A stripped TAG is not a stripped payload — the distinction still matters.

    `<a href>` is unwrapped and its text kept; `<script>` has its CONTENT deleted.
    Asserted under the chart policy, the only one that still has SVG tags for the
    contrast to be visible in.
    """
    assert chart('<svg><script>alert(1)</script></svg>') == "<svg></svg>"
    assert chart('<svg><a href="https://x.test"><text>click</text></a></svg>') == (
        "<svg><text>click</text></svg>"), "contrast: text kept, link dropped"


def test_that_the_chart_execution_pin_can_fail():
    hostile = '<svg><rect onload="alert(1)" width="9"/></svg>'
    loosened = {**_CHART_ATTRS, "rect": set(_CHART_ATTRS["rect"]) | {"onload"}}

    assert "onload" not in chart(hostile)
    assert "onload" in chart(hostile, attributes=loosened)


def test_that_the_chart_image_element_pin_can_fail():
    markup = '<svg><image href="https://evil.test/t.png" width="9"/></svg>'
    assert "evil.test" not in chart(markup)
    assert "evil.test" in chart(
        markup,
        tags=_CHART_TAGS | {"image"},
        attributes={**_CHART_ATTRS, "image": {"href", "width"}},
        url_schemes={"data", "https"})


def test_script_cannot_be_allowlisted_even_deliberately():
    """Stronger than either policy: the library refuses.

    `script` is in ammonia's `clean_content_tags` default and it panics rather
    than allow a tag whose content it is also told to delete. The one guarantee
    here that a careless edit cannot undo.
    """
    with pytest.raises(BaseException) as excinfo:
        chart("<svg><script>alert(1)</script></svg>", tags=_CHART_TAGS | {"script"})
    assert "clean_content_tags" in str(excinfo.value)


# =========================================================================== #
# The one permitted URL-shaped value — DEFUSED by the split, not just moved.
# =========================================================================== #
def test_fill_url_to_a_remote_paint_server_survives_the_chart_policy():
    """Still permitted, and no longer reachable by anyone but our own generators.

    `fill` is not a URL-typed attribute to ammonia, so `_URL_SCHEMES` is never
    consulted for it and `fill="url(https://…)"` passes through unvalidated. It is
    inert only because current browsers decline to resolve EXTERNAL paint servers,
    resolving `url(#local)` fragments alone — a property of browsers, not of this
    sanitiser, and so a control that could stop holding without anyone touching
    this repository.

    ITS RISK PROFILE CHANGED WITH THIS STAGE. While one permissive policy governed
    the whole document, an outsider could conceivably have set this value: VLM text
    is email-derived, and a description carrying `<rect fill="url(...)">` would have
    rendered. Now `fill` exists in no policy that model-authored content reaches;
    the only writer is the agent's own chart code, which emits colours it computes.
    The unvalidated attribute is still unvalidated — it is simply no longer
    attacker-writable. Defused, not merely relocated.
    """
    out = chart('<svg><rect fill="url(https://evil.test/paint)" width="9"/></svg>')
    assert 'fill="url(https://evil.test/paint)"' in out, (
        "if this starts being stripped, the note above is obsolete and should go — "
        "a stale caveat is worse than none")


def test_the_document_policy_cannot_carry_a_fill_at_all():
    """The half of that finding this stage actually closes."""
    out = document('<svg><rect fill="url(https://evil.test/paint)" width="9"/></svg>')

    assert "evil.test" not in out, "model text can no longer place a paint-server URL"
    assert "<rect" not in out


def test_that_the_fill_url_pin_can_fail():
    """It survives because `fill` is allowlisted on `rect`, nothing subtler."""
    markup = '<svg><rect fill="url(https://evil.test/paint)" width="9"/></svg>'
    loosened = {**_CHART_ATTRS, "rect": set(_CHART_ATTRS["rect"]) - {"fill"}}

    assert "evil.test" in chart(markup)
    assert "evil.test" not in chart(markup, attributes=loosened)


# =========================================================================== #
# The relationship between the two policies, asserted directly.
# =========================================================================== #
def test_the_chart_policy_is_the_document_policy_plus_svg_and_nothing_else():
    """Expressed as a derivation in the source; pinned as one here.

    If the two ever diverge on anything but SVG, the difference is unintended:
    the chart policy exists to add chart vocabulary, not to be a second, laxer
    document policy that accumulates exceptions.
    """
    assert _CHART_TAGS - _ALLOWED_TAGS == _SVG_TAGS
    assert _ALLOWED_TAGS - _CHART_TAGS == set()
    assert set(_CHART_ATTRS) - set(_ALLOWED_ATTRS) == _SVG_TAGS - {"title"}
    for tag, attrs in _ALLOWED_ATTRS.items():
        assert _CHART_ATTRS[tag] == attrs, f"{tag} differs between the policies"

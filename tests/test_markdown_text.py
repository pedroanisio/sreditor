from jinja2 import Environment

from sreditor.web.markdown_text import add_markdown_filters, markdown_html, markdown_plain


def test_markdown_html_emphasis_and_list_render_as_html() -> None:
    rendered = markdown_html("## Lead\n\n**bold** and *italic*\n\n- one\n- two")
    assert "<h2>Lead</h2>" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<em>italic</em>" in rendered
    assert "<li>one</li>" in rendered


def test_markdown_html_raw_html_is_escaped() -> None:
    rendered = markdown_html('<script>alert(1)</script> <b onclick="x">hi</b>')
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<b " not in rendered


def test_markdown_html_https_link_stays_a_link() -> None:
    assert '<a href="https://example.org">site</a>' in markdown_html("[site](https://example.org)")


def test_markdown_html_mailto_link_stays_a_link() -> None:
    assert 'href="mailto:a@example.org"' in markdown_html("[mail](mailto:a@example.org)")


def test_markdown_html_javascript_link_is_not_a_link() -> None:
    assert "<a" not in markdown_html("[click](javascript:alert(1))")


def test_markdown_html_relative_link_is_not_a_link() -> None:
    assert "<a" not in markdown_html("[admin](/app/account)")


def test_markdown_html_image_is_not_drawn() -> None:
    assert "<img" not in markdown_html("![logo](https://example.org/logo.png)")


def test_markdown_plain_markup_is_removed() -> None:
    assert markdown_plain("**Strike** at the *port*") == "Strike at the port"


def test_markdown_filter_in_autoescaped_template_renders_html() -> None:
    jinja = Environment(autoescape=True)
    add_markdown_filters(jinja)
    page = jinja.from_string("{{ text | markdown }}|{{ text | markdown_plain }}")
    assert page.render(text="**a** <i>") == "<p><strong>a</strong> &lt;i&gt;</p>\n|a &lt;i&gt;"

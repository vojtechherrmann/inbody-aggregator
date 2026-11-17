"""Footer build label: short commit + Container Apps revision, as in the monorepo's weber."""

import config


def test_label_with_revision():
    assert config.build_label("0123456789abcdef", "vhgwc-inbody-web--0000012") == "0123456 · rev 12"


def test_label_without_revision_is_the_commit_only():
    assert config.build_label("0123456789abcdef", "") == "0123456"
    assert config.build_label("dev", "") == "dev"


def test_label_with_a_named_revision_suffix():
    assert config.build_label("abc", "vhgwc-inbody-web--fix20261116") == "abc · rev fix20261116"


def test_footer_shows_the_build_label(anon, monkeypatch):
    monkeypatch.setattr(config, "BUILD_LABEL", "feedbee · rev 7")
    page = anon.get("/").text
    assert "feedbee · rev 7" in page and "VojtaH" in page


def test_footer_links_to_the_public_repository(anon):
    page = anon.get("/").text
    assert (
        '<a href="https://github.com/vojtechherrmann/inbody-aggregator" rel="noopener">GitHub</a>'
        in page
    )


def test_footer_links_to_the_homepage(anon):
    page = anon.get("/").text
    assert '<a href="https://vojtah.cz" rel="noopener">Homepage</a>' in page

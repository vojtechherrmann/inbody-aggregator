"""The account box in the app bar (the same look as the start page vojtah.cz): the user's name
with a menu (Change password, Log out), the change-password dialog on every page, Login when
anonymous."""

from __future__ import annotations

import re


def _box(page: str) -> str:
    return re.search(r'<details class="menu usermenu">.*?</details>', page, re.S).group(0)


def test_signed_in_box_has_the_name_change_password_and_log_out(as_user):
    page = as_user("bara").get("/history").text
    box = _box(page)
    assert 'class="userbtn"' in box and '<span class="userbtn-name">bara</span>' in box
    assert 'class="usericon"' in box and 'class="chev"' in box
    assert 'href="/account/password" data-open-dialog="pw-dialog"' in box
    assert 'action="/logout"' in box and 'class="menuitem"' in box
    assert box.index("/account/password") < box.index("/logout")
    # the settings menu no longer carries the password link
    settings = re.search(r'<details class="menu settings">.*?</details>', page, re.S).group(0)
    assert "/account/password" not in settings


def test_the_dialog_is_on_every_page_but_the_password_page(as_user):
    client = as_user("bara")
    page = client.get("/history").text
    dialog = re.search(r'<dialog id="pw-dialog".*?</dialog>', page, re.S).group(0)
    assert 'action="/account/password"' in dialog and 'name="csrf_token"' in dialog
    for name, auto in (
        ("current_password", "current-password"),
        ("new_password", "new-password"),
        ("new_password_repeat", "new-password"),
    ):
        field = re.search(rf'<input[^>]*name="{name}"[^>]*>', dialog).group(0)
        assert (
            'type="password"' in field
            and f'autocomplete="{auto}"' in field
            and "value=" not in field
        )
    assert 'autocomplete="username"' in dialog and 'value="bara"' in dialog
    assert 'data-pw-rules-for="pwd-new"' in dialog and "data-close-dialog" in dialog
    own = client.get("/account/password").text
    assert 'id="pw-dialog"' not in own
    assert 'id="pw-new"' in own and 'href="/history"' in own  # the same form, Cancel = back


def test_log_out_from_the_menu_still_works(as_user):
    from conftest import csrf_of

    client = as_user("bara")
    resp = client.post("/logout", data={"csrf_token": csrf_of(client)})
    assert resp.status_code == 303
    assert client.get("/history").status_code in (303, 401)


def test_guest_box_has_log_out_only(anon):
    import data
    import db
    import security
    from conftest import csrf_of

    with db.engine().begin() as conn:
        data.create_user(conn, "guest", security.UNUSABLE_PASSWORD_HASH, guest=True)
    assert anon.post("/login/guest", data={"csrf_token": csrf_of(anon)}).status_code == 303
    page = anon.get("/history").text
    box = _box(page)
    assert "/logout" in box and "/account/password" not in box
    assert 'id="pw-dialog"' not in page


def test_anonymous_sees_login_except_on_the_login_page(anon):
    page = anon.get("/data").text
    assert re.search(r'<a class="userbtn" href="/login">', page)
    assert '<details class="menu usermenu">' not in page
    assert '<a class="userbtn" href="/login">' not in anon.get("/login").text

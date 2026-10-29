import sys
from pathlib import Path

import pytest

from habitquest_backend.auth import verify_token

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from local_auth import EDITOR, PARENT, REVIEWER, demo_settings, local_token  # noqa: E402


def test_parent_and_editor_tokens_verify_with_their_roles(settings):
    assert verify_token(local_token(settings, PARENT), settings) == PARENT
    for editor in (EDITOR, REVIEWER):
        assert verify_token(local_token(settings, editor, editor=True), settings, hq_role="editor") == editor
    with pytest.raises(Exception, match="Editor role required"):
        verify_token(local_token(settings, PARENT), settings, hq_role="editor")
    assert EDITOR != REVIEWER


def test_local_tokens_require_demo_mode(settings, monkeypatch):
    monkeypatch.delenv("LOCAL_DEMO_AUTH", raising=False)
    with pytest.raises(SystemExit, match="LOCAL_DEMO_AUTH=true"):
        demo_settings()
    with pytest.raises(ValueError, match="JWT_SECRET"):
        local_token(settings.model_copy(update={"jwt_secret": None}), PARENT)

from tool import Principal, invite_user

class FakeUsers:
    def __init__(self): self.rows = {}
    def find_by_email(self, t, email): return self.rows.get((t, email))
    def create_invite(self, t, email, name, langs, role, by, key):
        row = self.rows.setdefault((t, email), {"user_id": "usr_1", "status": "invited", "idempotency_key": key,
                                                "invite_id": "inv_1", "expires_at": "2026-11-26T00:00:00Z"})
        return row

ADMIN = Principal("usr_admin", "ten_1", "admin")
ARGS = {"email": " Aram@Example.AM ", "display_name": "Արամ Պետրոսյան",
        "preferred_languages": ["hy_AM", "ru"], "role": "member", "idempotency_key": "k1"}

def test_happy_path_normalises_and_picks_language():
    r = invite_user(ARGS, ADMIN, FakeUsers())
    assert not r["isError"]
    sc = r["structuredContent"]
    assert sc["email"] == "Aram@example.am" and sc["preferred_languages"] == ["hy-AM", "ru"]
    assert sc["email_language"] == "hy"

def test_retry_with_same_key_is_idempotent():
    users = FakeUsers()
    first, second = invite_user(ARGS, ADMIN, users), invite_user(ARGS, ADMIN, users)
    assert first["structuredContent"]["invite_id"] == second["structuredContent"]["invite_id"]

def test_conflict_with_new_key_tells_model_what_to_do():
    users = FakeUsers(); invite_user(ARGS, ADMIN, users)
    r = invite_user({**ARGS, "idempotency_key": "k2"}, ADMIN, users)
    assert r["isError"] and r["structuredContent"]["error"]["code"] == "ALREADY_MEMBER"
    assert "update_user_role" in r["content"][0]["text"]

def test_admin_cannot_mint_admins_and_authz_runs_before_validation():
    r = invite_user({**ARGS, "role": "admin", "email": "garbage"}, ADMIN, FakeUsers())
    assert r["structuredContent"]["error"]["code"] == "FORBIDDEN_ROLE"

def test_validation_error_is_tool_error_with_field():
    r = invite_user({**ARGS, "preferred_languages": ["Armenian"]}, ADMIN, FakeUsers())
    err = r["structuredContent"]["error"]
    assert r["isError"] and err["field"] == "preferred_languages" and "BCP 47" in err["message"]

import logging
from datetime import datetime, timezone
from pathlib import Path
from requests.adapters import HTTPAdapter
from instagrapi import Client
from config import settings

logger = logging.getLogger(__name__)

class InstagramSessionError(RuntimeError): pass
class InstagramVerificationError(RuntimeError): pass
class InstagramProfileNotFound(RuntimeError): pass

# instagrapi calls requests.Session.get()/post() internally with no `timeout=` at all (confirmed
# by reading its own _send_private_request source). Plain `requests` has no timeout by default in
# that case -- if the network stalls for any reason (a flaky connection, a firewall silently
# dropping packets instead of resetting, a captive/corporate proxy, etc.) the call hangs
# indefinitely: no exception, no log line, nothing -- exactly "scheduler starts and just sits
# there forever with zero errors" reported on a fresh machine. This adapter forces every request
# through instagrapi's own sessions to have a real ceiling, turning a silent infinite hang into an
# actual, loud, loggable timeout error instead.
_DEFAULT_REQUEST_TIMEOUT = 30  # seconds

class _TimeoutHTTPAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _DEFAULT_REQUEST_TIMEOUT
        return super().send(request, **kwargs)

class InstagramActionClient:
    """Single wrapper around Instagrapi. Workers should not call Instagrapi directly."""
    def __init__(self):
        self.client = Client()
        for session in (self.client.private, self.client.public):
            session.mount("https://", _TimeoutHTTPAdapter())
            session.mount("http://", _TimeoutHTTPAdapter())
        self._logged_in = False

    def login(self):
        if self._logged_in:
            return
        session_path = Path(settings.instagram_session_file)
        try:
            session_valid = False
            if session_path.exists():
                logger.info("Loading saved Instagram session (device fingerprint) from %s", session_path)
                self.client.load_settings(session_path)
                try:
                    # Cheap authenticated call to check the loaded session actually works, without
                    # ever touching the password-login endpoint if it doesn't have to.
                    self.client.account_info()
                    session_valid = True
                    logger.info("Saved session is still valid; no login request needed")
                except Exception as exc:
                    logger.warning("Saved session failed verification (%s); will re-authenticate", exc)

            if not session_valid:
                if settings.instagram_sessionid:
                    # Reuse an already-authenticated browser session instead of the private-API
                    # password-login endpoint, which some accounts get flagged/rejected on
                    # (BadPassword / "rejects the proxy/IP, device fingerprint, or login context")
                    # even with a correct password. Never touches the password endpoint at all.
                    logger.info("Refreshing Instagram session via browser sessionid for @%s", settings.instagram_username or "(unknown)")
                    ok = self.client.login_by_sessionid(settings.instagram_sessionid)
                    if not ok:
                        raise InstagramSessionError("login_by_sessionid returned False")
                else:
                    if not settings.instagram_username or not settings.instagram_password:
                        raise InstagramSessionError("INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD missing")
                    if not session_path.exists():
                        # Client() just generated a random virtual device (uuid/android device id/phone
                        # model). Persist it *before* attempting login so a failed/retried login reuses
                        # the same fake device instead of presenting Instagram with a brand-new "phone"
                        # on every attempt -- repeated logins from different device fingerprints in a
                        # short window is itself a signal Instagram's risk system uses to reject a login.
                        session_path.parent.mkdir(parents=True, exist_ok=True)
                        self.client.dump_settings(session_path)
                        logger.info("No saved session found; persisted a new device fingerprint to %s", session_path)
                    logger.info("Logging in to Instagram as @%s (password endpoint)", settings.instagram_username)
                    ok = self.client.login(settings.instagram_username, settings.instagram_password)
                    if not ok:
                        raise InstagramSessionError("Instagram login returned False")

            session_path.parent.mkdir(parents=True, exist_ok=True)
            self.client.dump_settings(session_path)
            self._logged_in = True
            # Log which account is ACTUALLY authenticated, every time -- not just on a fresh
            # password/sessionid login. A pre-existing session_file (e.g. copied over along with
            # the project folder, or left behind from testing under a different account) is tried
            # FIRST, before ever looking at INSTAGRAM_SESSIONID/USERNAME in .env -- if that old
            # file is still a valid session, it silently wins, and everything this process does
            # (follows, DMs, and critically the "already have a conversation with this lead"
            # check, which reads the authenticated account's own DM inbox) happens as THAT
            # account, not whoever .env is actually configured for. This log line, plus the
            # mismatch check below, is what would have caught that immediately instead of it only
            # surfacing indirectly as "leads I already DMed from another account got filtered".
            actual_username = getattr(self.client, "username", None)
            logger.info("Instagram session active as @%s (session file: %s)", actual_username, session_path)
            if settings.instagram_username and actual_username and \
               actual_username.lower() != settings.instagram_username.lower():
                logger.error(
                    "MISMATCH: logged in as @%s but .env INSTAGRAM_USERNAME is @%s. This almost "
                    "always means a stale %s from a different account/setup is sitting in this "
                    "folder and is being reused instead of the credentials in .env. Delete that "
                    "file and re-run to log in as the correct account.",
                    actual_username, settings.instagram_username, session_path,
                )
        except InstagramSessionError:
            raise
        except Exception as exc:
            logger.error("Instagram login failed: %s", exc, exc_info=True)
            raise InstagramSessionError(
                "Instagram session could not be restored or validated. Outward actions are paused. "
                "Run: python fetch_login_cookie.py -- then click Resume."
            ) from exc

    def own_user_id(self):
        self.login()
        value = getattr(self.client, "user_id", None)
        return str(value) if value is not None else None

    def user_id(self, username: str) -> str:
        self.login()
        return str(self.client.user_id_from_username(username))

    def relationship(self, username: str) -> dict:
        self.login()
        uid = self.user_id(username)
        # Deliberately NOT using instagrapi's own user_friendship_v1() wrapper here: it validates
        # Instagram's raw response against a strict pydantic model, and on some
        # instagrapi/pydantic version combinations (seen on a fresh install -- newer instagrapi +
        # newer pydantic than this project was originally tested against) that model requires
        # fields (is_blocking_reel, is_muting_reel) that Instagram's actual API response doesn't
        # always include. That raises a hard ValidationError on every single relationship check,
        # which gets caught by profile_processor's per-lead error handling and correctly quarantines
        # each row to MANUAL_REVIEW rather than crashing -- but since it fails for every lead, the
        # net effect looks like the automation is "stuck": nothing ever reaches follow/message,
        # even though it IS actually progressing through the list one MANUAL_REVIEW row at a time.
        # We only ever use a handful of plain fields below, all present directly in Instagram's
        # raw JSON regardless of that unrelated model mismatch -- so call the same private
        # endpoint instagrapi itself uses internally and read the raw dict ourselves, instead of
        # going through its fragile strict-validated wrapper.
        raw = self.client.private_request(
            f"friendships/show/{uid}/", params={"is_external_deeplink_profile_view": "false"}
        )
        if not raw or raw.get("status") != "ok":
            raise InstagramVerificationError(f"No friendship response for {username}")
        result = {
            "user_id": uid,
            "following": bool(raw.get("following", False)),
            "outgoing_request": bool(raw.get("outgoing_request", False)),
            "incoming_request": bool(raw.get("incoming_request", False)),
            "is_private": bool(raw.get("is_private", False)),
            # Does the TARGET follow OUR account back -- the reciprocal signal Follow-Back Timing
            # V2 is built on. Same raw response, no extra API call.
            "followed_by": bool(raw.get("followed_by", False)),
        }
        logger.debug("Relationship for @%s: %s", username, result)
        return result

    def get_profile(self, username: str) -> dict:
        """Read-only profile lookup via Instagrapi (replaces the former HikerAPI dependency)."""
        self.login()
        logger.info("Fetching profile for @%s via Instagram", username)
        user = None
        try:
            # Read the raw private endpoint directly rather than going through instagrapi's own
            # user_info_by_username_v1(), which builds a strict pydantic User model out of the
            # response -- the same class of version-fragility that broke relationship() (see the
            # comment there). We only need a handful of plain fields below, all present in the
            # raw JSON regardless of what other fields a given instagrapi/pydantic version might
            # additionally require of that model.
            raw = self.client.private_request(f"users/{username}/usernameinfo/")
            user = raw.get("user") if isinstance(raw, dict) else None
        except Exception as exc:
            name = exc.__class__.__name__
            if "NotFound" in name or "UserNotFound" in name:
                logger.warning("Profile @%s not found on Instagram (private endpoint)", username)
                raise InstagramProfileNotFound(username) from exc
            # Private endpoint failed for some other reason (deactivated/renamed account, a
            # challenge page instead of JSON, etc.) -- fall back to instagrapi's own public
            # GQL-based lookup as a genuinely different code path, not just a re-validation of
            # the same broken response.
            logger.debug("Private profile lookup for @%s failed (%s), falling back to public lookup", username, exc)
            try:
                fallback = self.client.user_info_by_username(username)
                user = fallback.dict() if hasattr(fallback, "dict") else None
            except Exception as exc2:
                name2 = exc2.__class__.__name__
                if "NotFound" in name2 or "UserNotFound" in name2:
                    logger.warning("Profile @%s not found on Instagram (public fallback)", username)
                    raise InstagramProfileNotFound(username) from exc2
                if isinstance(exc2, KeyError) and exc2.args and exc2.args[0] == "data":
                    # instagrapi's own public GQL fallback is itself fragile: when the public
                    # profile endpoint doesn't return the expected shape, it throws a raw
                    # KeyError('data') instead of a clean UserNotFound. Treat it the same way --
                    # this profile can't be resolved right now, not a systemic failure that
                    # should pause the whole automation.
                    logger.warning("Profile @%s could not be resolved (public-fallback parse failure); treating as not found", username)
                    raise InstagramProfileNotFound(username) from exc2
                logger.error("Profile lookup failed for @%s: %s", username, exc2, exc_info=True)
                raise
        if not user:
            raise InstagramProfileNotFound(username)
        profile = {
            "username": user.get("username", username) or username,
            "user_id": str(user.get("pk", "") or ""),
            "full_name": user.get("full_name", "") or "",
            "biography": user.get("biography", "") or "",
            "is_private": bool(user.get("is_private", False)),
            "is_verified": bool(user.get("is_verified", False)),
            "city": user.get("city_name", None) or None,
            "raw": user,
        }
        logger.debug("Profile @%s: private=%s verified=%s city=%s", username, profile["is_private"], profile["is_verified"], profile["city"])
        return profile

    def follow(self, username: str) -> bool:
        self.login()
        logger.info("Sending follow request to @%s", username)
        result = bool(self.client.user_follow(self.user_id(username)))
        logger.info("Follow request to @%s returned %s", username, result)
        return result

    def unfollow(self, username: str) -> bool:
        self.login()
        logger.info("Unfollowing @%s", username)
        result = bool(self.client.user_unfollow(self.user_id(username)))
        logger.info("Unfollow @%s returned %s", username, result)
        return result

    def send_message(self, username: str, text: str):
        self.login()
        logger.info("Sending DM to @%s (%d chars)", username, len(text))
        result = self.client.direct_send(text, user_ids=[self.user_id(username)])
        logger.info("DM send to @%s returned id=%s", username, getattr(result, "id", None) or getattr(result, "pk", None))
        return result

    def direct_threads(self, amount: int = 50):
        self.login()
        return self.client.direct_threads(amount=amount, thread_message_limit=30)

    def find_thread_for_username(self, username: str, amount: int = 100):
        target = username.lower().lstrip("@")
        for thread in self.direct_threads(amount=amount):
            for user in getattr(thread, "users", []) or []:
                if (getattr(user, "username", "") or "").lower() == target:
                    return thread
        return None

    def has_previous_conversation(self, username: str) -> bool:
        thread = self.find_thread_for_username(username)
        return bool(thread and (getattr(thread, "messages", None) or []))

    @staticmethod
    def _as_datetime(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            # Instagram timestamps may be seconds, milliseconds, or microseconds.
            v = float(value)
            if v > 1e14: v /= 1e6
            elif v > 1e11: v /= 1e3
            try: return datetime.fromtimestamp(v, tz=timezone.utc)
            except Exception: return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _target_seen_at(self, thread, username):
        raw = getattr(thread, "last_seen_at", None)
        if raw is None:
            return None, hasattr(thread, "last_seen_at")
        target_id = None
        target = username.lower().lstrip("@")
        for u in getattr(thread, "users", []) or []:
            if (getattr(u, "username", "") or "").lower() == target:
                target_id = str(getattr(u, "pk", "") or getattr(u, "id", "") or "")
                break
        candidate = raw
        if isinstance(raw, dict):
            candidate = raw.get(target_id) or raw.get(int(target_id)) if target_id and target_id.isdigit() else raw.get(target_id)
            if candidate is None:
                return None, True
        for attr in ("timestamp", "seen_at", "time"):
            if hasattr(candidate, attr):
                candidate = getattr(candidate, attr)
                break
        return self._as_datetime(candidate), True

    def conversation_snapshot(self, username: str, since=None) -> dict:
        """Best-effort inbox state. Replies are only counted after `since` when supplied."""
        self.login()
        thread = self.find_thread_for_username(username)
        if thread is None:
            return {"exists": False, "reply": False, "seen_status": "UNKNOWN", "last_seen_at": None, "thread_id": None}
        messages = list(getattr(thread, "messages", []) or [])
        own_id = self.own_user_id()
        since_dt = self._as_datetime(since)
        reply = False
        for m in messages:
            msg_uid = str(getattr(m, "user_id", "") or "")
            if not msg_uid or (own_id and msg_uid == own_id):
                continue
            msg_dt = self._as_datetime(getattr(m, "timestamp", None))
            if since_dt is None or (msg_dt and msg_dt > since_dt):
                reply = True
                break
        seen_at, seen_field_exists = self._target_seen_at(thread, username)
        if seen_at:
            seen_status = "SEEN"
        elif seen_field_exists:
            seen_status = "NOT_SEEN"
        else:
            seen_status = "UNKNOWN"
        return {
            "exists": True,
            "reply": reply,
            "seen_status": seen_status,
            "last_seen_at": seen_at.isoformat() if seen_at else None,
            "thread_id": str(getattr(thread, "id", "") or getattr(thread, "pk", "") or ""),
        }

    def verify_follow(self, username: str) -> bool:
        rel = self.relationship(username)
        return bool(rel.get("following") or rel.get("outgoing_request"))

    def verify_unfollow(self, username: str) -> bool:
        rel = self.relationship(username)
        return not bool(rel.get("following") or rel.get("outgoing_request"))

    def verify_message_sent(self, username: str, text: str, since=None) -> bool:
        self.login()
        thread = self.find_thread_for_username(username)
        if thread is None:
            return False
        own_id = self.own_user_id()
        since_dt = self._as_datetime(since)
        for msg in getattr(thread, "messages", []) or []:
            msg_text = getattr(msg, "text", None)
            msg_uid = str(getattr(msg, "user_id", "") or "")
            msg_dt = self._as_datetime(getattr(msg, "timestamp", None))
            if msg_text != text:
                continue
            if own_id and msg_uid and msg_uid != own_id:
                continue
            if since_dt and (not msg_dt or msg_dt < since_dt):
                continue
            return True
        return False

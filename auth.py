"""
Google OAuth + JWT session management + usage-limit enforcement for WebSnap.

Environment variables required:
  GOOGLE_CLIENT_ID      — from Google Cloud Console
  GOOGLE_CLIENT_SECRET  — from Google Cloud Console
  SECRET_KEY            — random secret for signing JWTs (generate once, keep stable)

Optional:
  FREE_TIER_LIMIT       — int, default 5
  STRIPE_SECRET_KEY     — Stripe secret (leave unset until ready)
  STRIPE_PRICE_ID       — Stripe price ID for the Pro plan
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.orm import Session

from database import UsageRecord, User, get_db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FREE_TIER_LIMIT = int(os.getenv("FREE_TIER_LIMIT", "5"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-please-change-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30

# ---------------------------------------------------------------------------
# Google OAuth client (authlib)
# ---------------------------------------------------------------------------

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_session_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_session_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_current_user(
    session_token: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Returns the logged-in User or None (does NOT raise)."""
    if not session_token:
        return None
    user_id = decode_session_token(session_token)
    if not user_id:
        return None
    return db.get(User, user_id)


def require_auth(user: Optional[User] = Depends(get_current_user)) -> User:
    """Returns the logged-in User or raises HTTP 401."""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required. Please sign in with Google.")
    return user


# ---------------------------------------------------------------------------
# Usage helpers
# ---------------------------------------------------------------------------


def get_usage(user: User, db: Session) -> dict:
    used = db.query(UsageRecord).filter(UsageRecord.user_id == user.id).count()
    limit = FREE_TIER_LIMIT if user.tier == "free" else None
    remaining = max(0, limit - used) if limit is not None else None
    return {"used": used, "limit": limit, "remaining": remaining, "tier": user.tier}


def check_usage_limit(count: int, user: User, db: Session) -> None:
    """Raises HTTP 402 if user has insufficient remaining captures."""
    if user.tier == "pro":
        return
    usage = get_usage(user, db)
    if usage["remaining"] is not None and usage["remaining"] < count:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "usage_limit_reached",
                "used": usage["used"],
                "limit": usage["limit"],
                "remaining": usage["remaining"],
                "requested": count,
            },
        )


# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/auth/google")
async def login_google(request: Request):
    """Redirect the browser to Google's OAuth consent screen."""
    redirect_uri = str(request.url_for("auth_google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback", name="auth_google_callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_db)):
    """Receive the authorization code from Google, upsert the user, set a session cookie."""
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/?auth_error=oauth_failed")

    userinfo = token.get("userinfo") or {}
    google_id = userinfo.get("sub")
    if not google_id:
        return RedirectResponse(url="/?auth_error=no_sub")

    # Upsert user
    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = User(
            google_id=google_id,
            email=userinfo.get("email", ""),
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.name = userinfo.get("name", user.name)
        user.picture = userinfo.get("picture", user.picture)
        db.commit()

    session_token = create_session_token(user.id)
    response = RedirectResponse(url="/")
    response.set_cookie(
        "session_token",
        session_token,
        httponly=True,
        max_age=86400 * TOKEN_EXPIRE_DAYS,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    )
    return response


@router.get("/auth/me")
async def auth_me(
    user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return current auth state + usage info (called by the frontend on load)."""
    if not user:
        return JSONResponse({"authenticated": False})
    usage = get_usage(user, db)
    return {
        "authenticated": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "picture": user.picture,
            "tier": user.tier,
        },
        "usage": usage,
    }


@router.post("/auth/logout")
async def auth_logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("session_token")
    return response


# ---------------------------------------------------------------------------
# Stripe checkout placeholder
# ---------------------------------------------------------------------------


@router.post("/checkout/create")
async def create_checkout(request: Request, user: User = Depends(require_auth)):
    """
    Stripe Checkout placeholder.

    To activate:
      1. pip install stripe
      2. Set STRIPE_SECRET_KEY and STRIPE_PRICE_ID in .env
      3. Uncomment the Stripe block below and remove the placeholder response.
    """
    stripe_key = os.getenv("STRIPE_SECRET_KEY")
    price_id = os.getenv("STRIPE_PRICE_ID")

    if stripe_key and price_id:
        import stripe  # noqa: PLC0415  (lazy import — only needed when configured)

        stripe.api_key = stripe_key
        try:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="subscription",
                customer_email=user.email,
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=str(request.base_url) + "?upgraded=1",
                cancel_url=str(request.base_url),
            )
            return JSONResponse({"configured": True, "url": session.url})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Stripe error: {e}")

    # Stripe not yet configured — return a helpful message to the frontend
    return JSONResponse(
        {
            "configured": False,
            "message": (
                "Payments not yet configured. "
                "Add STRIPE_SECRET_KEY and STRIPE_PRICE_ID to your .env file."
            ),
        }
    )

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from passlib.context import CryptContext
from datetime import datetime, date
import httpx
import re
import os
from starlette.middleware.sessions import SessionMiddleware

# --- Config ---
RADARR_URL = os.getenv("RADARR_URL", "http://radarr:7878")
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")
SONARR_URL = os.getenv("SONARR_URL", "http://sonarr:8989")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme-use-a-long-random-string")
RADARR_QUALITY_PROFILE_ID = int(os.getenv("RADARR_QUALITY_PROFILE_ID", "1"))
SONARR_QUALITY_PROFILE_ID = int(os.getenv("SONARR_QUALITY_PROFILE_ID", "1"))
RADARR_ROOT_FOLDER = os.getenv("RADARR_ROOT_FOLDER", "/movies")
SONARR_ROOT_FOLDER = os.getenv("SONARR_ROOT_FOLDER", "/tv")
QBIT_USERNAME = os.getenv("QBIT_USERNAME", "admin")
QBIT_PASSWORD = os.getenv("QBIT_PASSWORD", "adminadmin")

MAX_LOGIN_ATTEMPTS = 5

# --- Database ---
DATABASE_URL = "sqlite:////config/zoop_portal.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    is_admin = Column(Boolean, default=False)
    is_approved = Column(Boolean, default=False)
    is_disabled = Column(Boolean, default=False)
    high_contrast = Column(Boolean, default=False)
    # Per-user auto-approve settings
    auto_approve = Column(Boolean, default=False)
    auto_approve_daily_limit = Column(Integer, default=5)
    # Login security
    failed_login_attempts = Column(Integer, default=0)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    requests = relationship("MediaRequest", back_populates="user")


class MediaRequest(Base):
    __tablename__ = "requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    title = Column(String)
    media_type = Column(String)
    link = Column(String)
    imdb_id = Column(String, nullable=True)
    status = Column(String, default="pending")
    radarr_sonarr_id = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    seasons = Column(Text, nullable=True)  # JSON: "all", "new", or "[1,2,3]"
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = relationship("User", back_populates="requests")


class SiteSettings(Base):
    __tablename__ = "site_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, index=True)
    value = Column(String)


Base.metadata.create_all(bind=engine)

# --- App ---
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


# --- DB Helper ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Auth Helpers ---
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def validate_password(password: str):
    errors = []
    if len(password) < 12:
        errors.append("at least 12 characters")
    if not re.search(r'[A-Z]', password):
        errors.append("at least 1 uppercase letter")
    if not re.search(r'[a-z]', password):
        errors.append("at least 1 lowercase letter")
    if not re.search(r'[0-9]', password):
        errors.append("at least 1 number")
    if not re.search(r'[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]', password):
        errors.append("at least 1 symbol")
    return errors


def get_admin(request: Request, db: Session):
    user_id = request.session.get("user_id")
    if not user_id:
        return None, False
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_admin:
        return user, False
    return user, True


def ensure_admin_exists(db: Session):
    admin = db.query(User).filter(User.is_admin == True).first()
    if not admin:
        admin_username = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "admin")
        admin = User(
            username=admin_username,
            password_hash=hash_password(admin_password),
            is_admin=True,
            is_approved=True,
            is_disabled=False,
            high_contrast=False,
            failed_login_attempts=0,
        )
        db.add(admin)
        db.commit()


# --- Settings Helpers ---
def get_setting(db: Session, key: str, default: str = None) -> str:
    s = db.query(SiteSettings).filter(SiteSettings.key == key).first()
    return s.value if s else default


def set_setting(db: Session, key: str, value: str):
    s = db.query(SiteSettings).filter(SiteSettings.key == key).first()
    if s:
        s.value = value
    else:
        db.add(SiteSettings(key=key, value=value))
    db.commit()


def get_user_auto_approve_count_today(db: Session, user_id: int) -> int:
    today_start = datetime.combine(date.today(), datetime.min.time())
    return db.query(MediaRequest).filter(
        MediaRequest.user_id == user_id,
        MediaRequest.status == "approved",
        MediaRequest.updated_at >= today_start,
        MediaRequest.notes == "auto-approved"
    ).count()


# --- IMDB / Media Helpers ---
def extract_imdb_id(link: str):
    match = re.search(r'imdb\.com/title/(tt\d+)', link)
    return match.group(1) if match else None


async def lookup_movie(imdb_id: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{RADARR_URL}/api/v3/movie/lookup",
            params={"term": f"imdb:{imdb_id}"},
            headers={"X-Api-Key": RADARR_API_KEY}
        )
        results = r.json()
        return results[0] if results else None


async def lookup_show(imdb_id: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{SONARR_URL}/api/v3/series/lookup",
            params={"term": f"imdb:{imdb_id}"},
            headers={"X-Api-Key": SONARR_API_KEY}
        )
        results = r.json()
        return results[0] if results else None


async def detect_media_type(imdb_id: str):
    try:
        show = await lookup_show(imdb_id)
        if show:
            return "show"
    except Exception:
        pass
    try:
        movie = await lookup_movie(imdb_id)
        if movie:
            return "movie"
    except Exception:
        pass
    return None


async def add_to_radarr(movie_data: dict):
    async with httpx.AsyncClient() as client:
        payload = {
            "title": movie_data["title"],
            "tmdbId": movie_data["tmdbId"],
            "year": movie_data.get("year"),
            "qualityProfileId": RADARR_QUALITY_PROFILE_ID,
            "rootFolderPath": RADARR_ROOT_FOLDER,
            "monitored": True,
            "addOptions": {"searchForMovie": True}
        }
        r = await client.post(
            f"{RADARR_URL}/api/v3/movie",
            json=payload,
            headers={"X-Api-Key": RADARR_API_KEY}
        )
        if r.status_code in (200, 201):
            return r.json()
        elif r.status_code == 400:
            data = r.json()
            if isinstance(data, list) and any("already" in str(e).lower() for e in data):
                return {"alreadyExists": True}
        return None


async def add_to_sonarr(show_data: dict, seasons_selection: str = "all"):
    import json
    all_seasons = show_data.get("seasons", [])

    # Parse which season numbers to monitor
    if seasons_selection == "all":
        monitored_nums = [s["seasonNumber"] for s in all_seasons if s["seasonNumber"] > 0]
        search_on_add = True
    elif seasons_selection == "new":
        monitored_nums = []
        search_on_add = False
    else:
        try:
            monitored_nums = json.loads(seasons_selection)
        except Exception:
            monitored_nums = [s["seasonNumber"] for s in all_seasons if s["seasonNumber"] > 0]
        search_on_add = False

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: Add series — let Sonarr do its thing, search=False so nothing downloads yet
        payload = {
            "title": show_data["title"],
            "tvdbId": show_data["tvdbId"],
            "year": show_data.get("year"),
            "qualityProfileId": SONARR_QUALITY_PROFILE_ID,
            "rootFolderPath": SONARR_ROOT_FOLDER,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {
                "searchForMissingEpisodes": False
            }
        }
        r = await client.post(
            f"{SONARR_URL}/api/v3/series",
            json=payload,
            headers={"X-Api-Key": SONARR_API_KEY}
        )

        if r.status_code not in (200, 201):
            if r.status_code == 400:
                data = r.json()
                if isinstance(data, list) and any("already" in str(e).lower() for e in data):
                    return {"alreadyExists": True}
            return None

        result = r.json()
        series_id = result.get("id")
        if not series_id:
            return result

        # Wait for Sonarr to finish processing the newly added series
        import asyncio
        await asyncio.sleep(1)

        # Step 2: Fetch the full series object fresh, then PUT with correct monitoring
        get_r = await client.get(
            f"{SONARR_URL}/api/v3/series/{series_id}",
            headers={"X-Api-Key": SONARR_API_KEY}
        )
        if get_r.status_code != 200:
            return result

        full_series = get_r.json()
        full_series["monitored"] = True
        for s in full_series.get("seasons", []):
            num = s["seasonNumber"]
            if num == 0:
                s["monitored"] = False
            else:
                s["monitored"] = num in monitored_nums

        put_r = await client.put(
            f"{SONARR_URL}/api/v3/series/{series_id}",
            json=full_series,
            headers={"X-Api-Key": SONARR_API_KEY}
        )

        if put_r.status_code not in (200, 201, 202):
            return result

        # Wait for PUT to be processed before triggering search
        await asyncio.sleep(1)

        # Step 3: Refresh series metadata first, then search
        if monitored_nums:
            # Refresh so Sonarr has full episode data before searching
            await client.post(
                f"{SONARR_URL}/api/v3/command",
                json={"name": "RefreshSeries", "seriesId": series_id},
                headers={"X-Api-Key": SONARR_API_KEY}
            )
            # Wait for refresh to complete
            await asyncio.sleep(3)

            if seasons_selection == "all":
                await client.post(
                    f"{SONARR_URL}/api/v3/command",
                    json={"name": "SeriesSearch", "seriesId": series_id},
                    headers={"X-Api-Key": SONARR_API_KEY}
                )
            else:
                for season_num in monitored_nums:
                    await client.post(
                        f"{SONARR_URL}/api/v3/command",
                        json={"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_num},
                        headers={"X-Api-Key": SONARR_API_KEY}
                    )

        return put_r.json()


# --- qBittorrent Helper ---
async def get_qbit_cookie():
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://qbittorrent:8080/api/v2/auth/login",
                data={"username": QBIT_USERNAME, "password": QBIT_PASSWORD}
            )
            return f"SID={r.cookies.get('SID', '')}"
    except Exception:
        return ""


# --- Startup ---
@app.on_event("startup")
async def startup():
    db = SessionLocal()
    ensure_admin_exists(db)
    db.close()


# --- Auth Routes ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()

    # Unknown user
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

    # Already disabled
    if user.is_disabled:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Your account has been disabled. Please contact an admin."})

    # Wrong password
    if not verify_password(password, user.password_hash):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
            user.is_disabled = True
            db.commit()
            return templates.TemplateResponse("login.html", {"request": request, "error": f"Account locked after {MAX_LOGIN_ATTEMPTS} failed attempts. Please contact an admin."})
        remaining = MAX_LOGIN_ATTEMPTS - user.failed_login_attempts
        db.commit()
        return templates.TemplateResponse("login.html", {"request": request, "error": f"Invalid username or password. {remaining} attempt{'s' if remaining != 1 else ''} remaining."})

    # Not yet approved
    if not user.is_approved:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Your account is pending admin approval"})

    # Success — reset counter and record login time
    user.failed_login_attempts = 0
    user.last_login = datetime.utcnow()
    db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = None, success: str = None):
    return templates.TemplateResponse("register.html", {"request": request, "error": error, "success": success})


@app.post("/register")
async def register(request: Request, username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...), db: Session = Depends(get_db)):
    if password != confirm_password:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Passwords do not match"})
    errors = validate_password(password)
    if errors:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Password must contain: " + ", ".join(errors)})
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Username already taken"})
    user = User(username=username, password_hash=hash_password(password), failed_login_attempts=0)
    db.add(user)
    db.commit()
    return templates.TemplateResponse("register.html", {"request": request, "success": "Account created! Please wait for admin approval before logging in."})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


# --- User Preferences ---
@app.post("/preferences/contrast")
async def toggle_contrast(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.high_contrast = not user.high_contrast
        db.commit()
    referer = request.headers.get("referer", "/dashboard")
    return RedirectResponse(referer, status_code=302)


# --- Dashboard ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db), success: str = None, error: str = None):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/login")
    requests_list = db.query(MediaRequest).order_by(MediaRequest.created_at.desc()).all()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "requests": requests_list,
        "success": success,
        "error": error
    })


# --- Submit Request ---
@app.post("/request")
async def submit_request(request: Request, link: str = Form(...), seasons: str = Form(default="all"), db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")
    user = db.query(User).filter(User.id == user_id).first()

    imdb_id = extract_imdb_id(link)
    if not imdb_id:
        return RedirectResponse("/dashboard?error=Could+not+extract+IMDB+ID.+Please+use+an+IMDB+link.", status_code=302)

    media_type = await detect_media_type(imdb_id)
    if not media_type:
        return RedirectResponse("/dashboard?error=Could+not+find+this+title.+Please+check+the+link.", status_code=302)

    title = "Unknown Title"
    media_data = None
    try:
        media_data = await lookup_show(imdb_id) if media_type == "show" else await lookup_movie(imdb_id)
        if media_data:
            title = f"{media_data['title']} ({media_data.get('year', '')})"
    except Exception:
        pass

    existing = db.query(MediaRequest).filter(MediaRequest.imdb_id == imdb_id).first()
    if existing:
        return RedirectResponse("/dashboard?error=This+title+has+already+been+requested.", status_code=302)

    # Check if already exists in Radarr or Sonarr
    try:
        if media_type == "movie":
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{RADARR_URL}/api/v3/movie",
                    headers={"X-Api-Key": RADARR_API_KEY}
                )
                if r.status_code == 200:
                    existing_movies = r.json()
                    for m in existing_movies:
                        if str(m.get("imdbId", "")) == imdb_id:
                            return RedirectResponse("/dashboard?error=This+movie+is+already+in+the+library.", status_code=302)
        elif media_type == "show":
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{SONARR_URL}/api/v3/series",
                    headers={"X-Api-Key": SONARR_API_KEY}
                )
                if r.status_code == 200:
                    existing_shows = r.json()
                    for s in existing_shows:
                        if str(s.get("imdbId", "")) == imdb_id:
                            return RedirectResponse("/dashboard?error=This+show+is+already+in+the+library.", status_code=302)
    except Exception:
        pass

    # Check per-user auto-approve
    approved_today = get_user_auto_approve_count_today(db, user.id)
    should_auto_approve = (
        user.auto_approve and
        approved_today < user.auto_approve_daily_limit and
        media_data is not None
    )

    status = "pending"
    notes = None

    if should_auto_approve:
        try:
            if media_type == "movie":
                await add_to_radarr(media_data)
            else:
                await add_to_sonarr(media_data, seasons)
            status = "approved"
            notes = "auto-approved"
        except Exception:
            status = "pending"

    media_request = MediaRequest(
        user_id=user.id,
        title=title,
        media_type=media_type,
        link=link,
        imdb_id=imdb_id,
        status=status,
        notes=notes,
        seasons=seasons if media_type == "show" else None,
        updated_at=datetime.utcnow()
    )
    db.add(media_request)
    db.commit()

    if status == "approved":
        return RedirectResponse("/dashboard?success=Request+auto-approved+and+sent+to+download!", status_code=302)
    return RedirectResponse("/dashboard?success=Request+submitted+successfully!", status_code=302)


# --- Lookup API (for season picker) ---
@app.get("/api/lookup")
async def api_lookup(request: Request, imdb_id: str):
    if not request.session.get("user_id"):
        return {"error": "Unauthorized"}
    try:
        show = await lookup_show(imdb_id)
        if show:
            seasons = []
            for s in show.get("seasons", []):
                if s["seasonNumber"] == 0:
                    continue
                stats = s.get("statistics") or {}
                ep_count = stats.get("totalEpisodeCount") or stats.get("episodeCount") or 0
                seasons.append({"number": s["seasonNumber"], "episode_count": ep_count})
            return {"type": "show", "title": show["title"], "year": show.get("year"), "seasons": seasons}
        movie = await lookup_movie(imdb_id)
        if movie:
            return {"type": "movie", "title": movie["title"], "year": movie.get("year")}
        return {"error": "Title not found"}
    except Exception as e:
        return {"error": str(e)}


# --- Update existing series seasons ---
@app.post("/api/update-series")
async def update_series(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Unauthorized"}

    body = await request.json()
    imdb_id = body.get("imdb_id")
    request_id = body.get("request_id")
    seasons_selection = body.get("seasons", "all")
    monitor_new = body.get("monitor_new", True)
    search_new = body.get("search_new", [])

    if not imdb_id:
        return {"error": "No IMDB ID provided"}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Find the series in Sonarr
            r = await client.get(
                f"{SONARR_URL}/api/v3/series",
                headers={"X-Api-Key": SONARR_API_KEY}
            )
            if r.status_code != 200:
                return {"error": "Could not reach Sonarr"}

            all_series = r.json()
            show = next((s for s in all_series if str(s.get("imdbId", "")) == imdb_id), None)

            if not show:
                return {"error": "Series not found in Sonarr"}

            series_id = show["id"]

            # Parse season selection
            import json as json_lib
            if seasons_selection == "all":
                monitored_nums = [s["seasonNumber"] for s in show["seasons"] if s["seasonNumber"] > 0]
            elif seasons_selection == "new":
                monitored_nums = []
            else:
                try:
                    monitored_nums = json_lib.loads(seasons_selection)
                except Exception:
                    monitored_nums = []

            # Update season monitoring
            for s in show["seasons"]:
                num = s["seasonNumber"]
                if num == 0:
                    s["monitored"] = False
                else:
                    s["monitored"] = num in monitored_nums

            # Never unmonitor the series itself — keep it True so seasons stay unlocked
            show["monitored"] = True

            put_r = await client.put(
                f"{SONARR_URL}/api/v3/series/{series_id}",
                json=show,
                headers={"X-Api-Key": SONARR_API_KEY}
            )

            if put_r.status_code not in (200, 201, 202):
                return {"error": f"Sonarr update failed: {put_r.status_code}"}

            # Wait for Sonarr to process the PUT before searching
            import asyncio
            await asyncio.sleep(2)

            # Trigger search only for newly added seasons
            if search_new:
                # Refresh series metadata first
                await client.post(
                    f"{SONARR_URL}/api/v3/command",
                    json={"name": "RefreshSeries", "seriesId": series_id},
                    headers={"X-Api-Key": SONARR_API_KEY}
                )
                await asyncio.sleep(3)

                for season_num in search_new:
                    await client.post(
                        f"{SONARR_URL}/api/v3/command",
                        json={"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_num},
                        headers={"X-Api-Key": SONARR_API_KEY}
                    )

            return {"success": True, "series_id": series_id}

    except Exception as e:
        return {"error": str(e)}
    finally:
        # Update seasons in DB regardless of Sonarr result
        if request_id:
            try:
                media_req = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
                if media_req:
                    media_req.seasons = seasons_selection
                    db.commit()
            except Exception:
                pass


# --- Get series status from Sonarr ---
@app.get("/api/series-status")
async def series_status(request: Request, imdb_id: str):
    if not request.session.get("user_id"):
        return {"error": "Unauthorized"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{SONARR_URL}/api/v3/series",
                headers={"X-Api-Key": SONARR_API_KEY}
            )
            if r.status_code != 200:
                return {"error": "Could not reach Sonarr"}
            all_series = r.json()
            show = next((s for s in all_series if str(s.get("imdbId", "")) == imdb_id), None)
            if not show:
                return {"found": False, "debug": f"No series found with imdbId={imdb_id}. Total series: {len(all_series)}"}
            seasons = [
                {"number": s["seasonNumber"], "monitored": s["monitored"]}
                for s in show.get("seasons", [])
                if s["seasonNumber"] > 0
            ]
            return {
                "found": True,
                "series_id": show["id"],
                "monitored": show["monitored"],
                "seasons": seasons
            }
    except Exception as e:
        return {"error": str(e)}


# --- Notifications API ---
@app.get("/api/notifications")
async def get_notifications(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"error": "Unauthorized"}
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"error": "Unauthorized"}

    if user.is_disabled:
        request.session.clear()
        return {"kicked": True}

    if not user.is_admin:
        return {"kicked": False, "pending_users": 0, "pending_requests": 0}

    pending_users = db.query(User).filter(User.is_approved == False).count()
    pending_requests = db.query(MediaRequest).filter(MediaRequest.status == "pending").count()
    return {
        "kicked": False,
        "pending_users": pending_users,
        "pending_requests": pending_requests
    }


# --- What's New ---
@app.get("/whats-new", response_class=HTMLResponse)
async def whats_new(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("whats_new.html", {"request": request, "user": user})


# --- Downloads ---
@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("downloads.html", {"request": request, "user": user})


@app.get("/api/downloads")
async def get_downloads(request: Request):
    if not request.session.get("user_id"):
        return {"error": "Unauthorized"}
    try:
        cookie = await get_qbit_cookie()
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "http://qbittorrent:8080/api/v2/torrents/info",
                headers={"Cookie": cookie}
            )
            if r.status_code == 200:
                return {"torrents": r.json()}
            return {"torrents": []}
    except Exception as e:
        return {"error": str(e), "torrents": []}


# --- Admin Routes ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: Session = Depends(get_db)):
    user, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    pending_users = db.query(User).filter(User.is_approved == False).all()
    all_users = db.query(User).filter(User.id != user.id).all()
    all_requests = db.query(MediaRequest).order_by(MediaRequest.created_at.desc()).all()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user,
        "pending_users": pending_users,
        "all_users": all_users,
        "all_requests": all_requests,
        "max_attempts": MAX_LOGIN_ATTEMPTS,
    })


@app.post("/admin/approve-user/{user_id}")
async def approve_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        u.is_approved = True
        u.is_disabled = False
        u.failed_login_attempts = 0
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/toggle-admin/{user_id}")
async def toggle_admin(user_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        u.is_admin = not u.is_admin
        # Ensure promoted admins are approved
        if u.is_admin:
            u.is_approved = True
            u.is_disabled = False
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/self-settings")
async def admin_self_settings(
    request: Request,
    auto_approve: str = Form(default="false"),
    auto_approve_daily_limit: int = Form(default=5),
    db: Session = Depends(get_db)
):
    user, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    user.auto_approve = auto_approve == "on"
    user.auto_approve_daily_limit = auto_approve_daily_limit
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/reject-user/{user_id}")
async def reject_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/disable-user/{user_id}")
async def disable_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        u.is_disabled = True
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/enable-user/{user_id}")
async def enable_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        u.is_disabled = False
        u.failed_login_attempts = 0
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/delete-user/{user_id}")
async def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/reset-password/{user_id}")
async def reset_password(user_id: int, request: Request, new_password: str = Form(...), db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        errors = validate_password(new_password)
        if not errors:
            u.password_hash = hash_password(new_password)
            u.failed_login_attempts = 0
            db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/update-user/{user_id}")
async def update_user(
    user_id: int,
    request: Request,
    auto_approve: str = Form(default="false"),
    auto_approve_daily_limit: int = Form(default=5),
    db: Session = Depends(get_db)
):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        u.auto_approve = auto_approve == "on"
        u.auto_approve_daily_limit = auto_approve_daily_limit
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/batch-approve")
async def batch_approve(request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")

    body = await request.json()
    request_ids = body.get("request_ids", [])
    seasons = body.get("seasons", "all")

    results = []
    for request_id in request_ids:
        media_request = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
        if not media_request or media_request.status != "pending":
            continue
        try:
            if media_request.media_type == "movie" and media_request.imdb_id:
                movie_data = await lookup_movie(media_request.imdb_id)
                if movie_data:
                    await add_to_radarr(movie_data)
                    media_request.title = f"{movie_data['title']} ({movie_data.get('year', '')})"
            elif media_request.media_type == "show" and media_request.imdb_id:
                show_data = await lookup_show(media_request.imdb_id)
                if show_data:
                    await add_to_sonarr(show_data, seasons)
                    media_request.title = f"{show_data['title']} ({show_data.get('year', '')})"
            media_request.status = "approved"
            media_request.seasons = seasons if media_request.media_type == "show" else None
        except Exception as e:
            media_request.notes = str(e)
            media_request.status = "approved"
        media_request.updated_at = datetime.utcnow()
        results.append(request_id)

    db.commit()
    return {"approved": len(results)}


@app.post("/admin/batch-reject")
async def batch_reject(request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")

    body = await request.json()
    request_ids = body.get("request_ids", [])

    count = 0
    for request_id in request_ids:
        media_request = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
        if not media_request or media_request.status != "pending":
            continue
        media_request.status = "rejected"
        media_request.updated_at = datetime.utcnow()
        count += 1

    db.commit()
    return {"rejected": count}


@app.post("/admin/approve-request/{request_id}")
async def approve_request(request_id: int, request: Request, seasons_override: str = Form(default=None), db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    media_request = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
    if not media_request:
        return RedirectResponse("/admin")

    # Use override if provided, otherwise use user's selection
    seasons = seasons_override if seasons_override else (media_request.seasons or "all")
    if seasons_override:
        media_request.seasons = seasons_override

    try:
        if media_request.media_type == "movie" and media_request.imdb_id:
            movie_data = await lookup_movie(media_request.imdb_id)
            if movie_data:
                await add_to_radarr(movie_data)
                media_request.title = f"{movie_data['title']} ({movie_data.get('year', '')})"
        elif media_request.media_type == "show" and media_request.imdb_id:
            show_data = await lookup_show(media_request.imdb_id)
            if show_data:
                await add_to_sonarr(show_data, seasons)
                media_request.title = f"{show_data['title']} ({show_data.get('year', '')})"
    except Exception as e:
        media_request.notes = str(e)
    media_request.status = "approved"
    media_request.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/reject-request/{request_id}")
async def reject_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    media_request = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
    if media_request:
        media_request.status = "rejected"
        media_request.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/delete-request/{request_id}")
async def delete_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    _, admin = get_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard")
    media_request = db.query(MediaRequest).filter(MediaRequest.id == request_id).first()
    if media_request:
        db.delete(media_request)
        db.commit()
    return RedirectResponse("/admin", status_code=302)

import base64
import io
import json
import math
import os
import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import barcode
import bcrypt
from barcode.writer import SVGWriter
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from pydantic import BaseModel
import qrcode
from reportlab.graphics import renderPDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from svglib.svglib import svg2rlg

# ==========================================
# 1. CONFIGURATION & CORE SETTINGS
# ==========================================
SECRET_KEY = os.getenv("SECRET_KEY", "labelforge_commercial_secret_2026_x995")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 30  # 30 days token expiry

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@lableforge.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Chopraji995#")

PAYMENT_CONFIG = {
    "upi_id": os.getenv("PAYMENT_UPI_ID", "merchant.labelforge@hdfcbank"),
    "payee_name": os.getenv("PAYMENT_PAYEE_NAME", "LabelForge Technologies"),
    "bank_name": "HDFC Bank (Commercial Settlement)",
    "account_number": "50200098765432",
    "ifsc_code": "HDFC0000123",
    "branch": "Commercial Business Center",
    "currency_symbol": "₹"
}

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./labelforge.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def hash_password(password: str) -> str:
    pwd_bytes = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        pwd_bytes = plain_password.encode('utf-8')[:72]
        hash_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(pwd_bytes, hash_bytes)
    except Exception:
        return False


# ==========================================
# 2. DATABASE MODELS
# ==========================================
class Business(Base):
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    gstin = Column(String(30), default="")
    phone = Column(String(30), default="")
    plan = Column(String(50), default="free")
    sku_prefix = Column(String(20), default="PRD")
    sku_padding = Column(Integer, default=6)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="business", cascade="all, delete-orphan")
    products = relationship("Product", back_populates="business", cascade="all, delete-orphan")
    categories = relationship("Category", back_populates="business", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True)
    full_name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    phone = Column(String(30), default="")
    role = Column(String(20), default="owner")
    is_suspended = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    business = relationship("Business", back_populates="users")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String(255), nullable=False)
    sku = Column(String(100), nullable=False)
    barcode = Column(String(100), nullable=False, unique=True, index=True)
    barcode_type = Column(String(50), default="ean13")
    category = Column(String(100), default="General")
    mrp = Column(Float, default=0.0)
    selling_price = Column(Float, default=0.0)
    batch_number = Column(String(50), default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    business = relationship("Business", back_populates="products")


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    business = relationship("Business", back_populates="categories")


class SystemSetting(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=False)


Base.metadata.create_all(bind=engine)


def auto_migrate_schema():
    if "sqlite" not in DATABASE_URL:
        return
    with engine.connect() as conn:
        try:
            res = conn.execute(text("PRAGMA table_info(businesses)"))
            existing_cols = {row[1] for row in res.fetchall()}
            if "sku_prefix" not in existing_cols:
                conn.execute(text("ALTER TABLE businesses ADD COLUMN sku_prefix VARCHAR(20) DEFAULT 'PRD'"))
            if "sku_padding" not in existing_cols:
                conn.execute(text("ALTER TABLE businesses ADD COLUMN sku_padding INTEGER DEFAULT 6"))
            conn.commit()
        except Exception as e:
            print(f"[Migration Notice] {e}")


auto_migrate_schema()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_defaults():
    db = SessionLocal()
    try:
        if not db.query(SystemSetting).filter(SystemSetting.key == "pricing_config").first():
            cfg = {"free_price": 0, "business_price": 799, "professional_price": 1999, "symbol": "₹"}
            db.add(SystemSetting(key="pricing_config", value=json.dumps(cfg)))
            db.commit()

        admin_user = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        if not admin_user:
            biz = Business(name="Platform Headquarters", plan="lifetime_unlimited", sku_prefix="PRD", sku_padding=6)
            db.add(biz)
            db.commit()
            db.refresh(biz)
            admin_user = User(
                business_id=biz.id,
                full_name="Super Administrator",
                email=ADMIN_EMAIL,
                hashed_password=hash_password(ADMIN_PASSWORD),
                role="superadmin"
            )
            db.add(admin_user)
            db.commit()
            db.refresh(admin_user)
        else:
            admin_user.hashed_password = hash_password(ADMIN_PASSWORD)
            admin_user.role = "superadmin"
            if admin_user.business:
                admin_user.business.plan = "lifetime_unlimited"
                if not admin_user.business.sku_prefix:
                    admin_user.business.sku_prefix = "PRD"
                if not admin_user.business.sku_padding:
                    admin_user.business.sku_padding = 6
            db.commit()
    finally:
        db.close()


init_defaults()


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    user = db.query(User).filter(User.email == email).first()
    if not user or user.is_suspended:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account disabled")
    return user


def require_superadmin(user: User = Depends(get_current_user)):
    is_super = (user.email == ADMIN_EMAIL or user.role == "superadmin")
    if not is_super:
        raise HTTPException(status_code=403, detail="SuperAdmin privileges required")
    return user


# ==========================================
# 3. BARCODE ENGINE
# ==========================================
class BarcodeEngine:
    @staticmethod
    def calc_ean13_check(d12: str) -> int:
        clean = re.sub(r'\D', '', str(d12)).zfill(12)[:12]
        odds = sum(int(x) for x in clean[0::2])
        evens = sum(int(x) for x in clean[1::2])
        total = odds + (evens * 3)
        mod = total % 10
        return 0 if mod == 0 else (10 - mod)

    @classmethod
    def generate_unique_ean13(cls, existing_barcodes: set) -> str:
        for _ in range(5000):
            ms = str(int(datetime.utcnow().timestamp() * 1000))[-7:]
            rnd = f"{random.randint(10, 99)}"
            base12 = f"890{rnd}{ms}"
            base12 = base12.ljust(12, "0")[:12]
            check = cls.calc_ean13_check(base12)
            code = f"{base12}{check}"
            if code not in existing_barcodes:
                return code

        fallback = f"890{random.randint(100000000, 999999999)}"
        return f"{fallback}{cls.calc_ean13_check(fallback)}"

    @classmethod
    def generate_svg(cls, symbology: str, value: str, write_text: bool = True, base_url: str = "") -> str:
        symbology = (symbology or "ean13").lower().strip()
        clean = (value or "").strip()

        if symbology in ("qrcode", "qr"):
            qr_content = f"{base_url.rstrip('/')}/verify/{clean}" if base_url else clean
            qr = qrcode.QRCode(box_size=8, border=2)
            qr.add_data(qr_content or "000000000000")
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="100%" height="100%" preserveAspectRatio="xMidYMid meet" style="display:block; margin:auto;"><image href="data:image/png;base64,{b64}" width="200" height="200"/></svg>'

        if symbology == "ean13":
            val_numeric = re.sub(r'\D', '', clean)
            if len(val_numeric) >= 12:
                base12 = val_numeric[:12]
            else:
                base12 = val_numeric.zfill(12)
            val_numeric = f"{base12}{cls.calc_ean13_check(base12)}"
        else:
            val_numeric = clean if clean else "00000000"

        b_class = barcode.get_barcode_class(symbology if symbology in ('ean13', 'code128', 'code39') else 'ean13')
        writer = SVGWriter()
        bc = b_class(val_numeric, writer=writer)
        buf = io.BytesIO()

        bc.write(buf, options={
            "write_text": write_text,
            "quiet_zone": 2.5,
            "module_width": 0.25,
            "module_height": 10.0,
            "font_size": 9,
            "text_distance": 2.5
        })
        raw_svg = buf.getvalue().decode('utf-8')

        # Add responsive attributes without distorting the internal coordinate grid
        if 'preserveAspectRatio' not in raw_svg:
            raw_svg = re.sub(
                r'<svg\b([^>]*)>',
                r'<svg\1 preserveAspectRatio="xMidYMid meet" style="display:block; margin:auto; max-width:100%; max-height:100%;">',
                raw_svg,
                count=1
            )
        return raw_svg
# ==========================================
# 4. REST APIS & FASTAPI
# ==========================================
app = FastAPI(title="LabelForge Pro Engine", version="8.9.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RegisterSchema(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = ""
    password: str
    business_name: str
    plan: Optional[str] = "free"


class LoginSchema(BaseModel):
    email: str
    password: str


class CleanProductCreate(BaseModel):
    name: str
    mrp: float = 0.0
    category: Optional[str] = "General"


class ProductUpdateSchema(BaseModel):
    name: str
    mrp: float
    category: str


class CategoryCreateSchema(BaseModel):
    name: str


class CompanySettingsSchema(BaseModel):
    business_name: str
    sku_prefix: str
    sku_padding: int


class ChangePasswordSchema(BaseModel):
    current_password: str
    new_password: str


class SheetPdfRequest(BaseModel):
    items: List[Dict[str, Any]]
    label_width_mm: float = 75.0
    label_height_mm: float = 50.0
    single_label: bool = False
    single_barcode: bool = False


class MockPaymentRequest(BaseModel):
    plan: str
    amount: float
    payment_mode: str
    bank_name: Optional[str] = None


class AdminUpdateCustomerPlanSchema(BaseModel):
    business_id: int
    new_plan: str


class AdminToggleUserSuspendSchema(BaseModel):
    user_id: int
    is_suspended: bool


class AdminUpdatePricingSchema(BaseModel):
    free_price: float
    business_price: float
    professional_price: float


# Category Endpoints
@app.get("/api/categories")
def get_categories(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.business_id:
        return []
    cats = db.query(Category).filter(Category.business_id == user.business_id).all()
    default_names = ["General", "Apparel", "Footwear", "Electronics", "Cosmetics", "FMCG"]
    if not cats:
        for name in default_names:
            db.add(Category(business_id=user.business_id, name=name))
        db.commit()
        cats = db.query(Category).filter(Category.business_id == user.business_id).all()
    return [{"id": c.id, "name": c.name} for c in cats]


@app.post("/api/categories")
def add_category(data: CategoryCreateSchema, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.business_id:
        raise HTTPException(status_code=400, detail="No business associated with user")
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name cannot be empty")
    existing = db.query(Category).filter(Category.business_id == user.business_id, Category.name.ilike(name)).first()
    if existing:
        return {"id": existing.id, "name": existing.name}
    new_cat = Category(business_id=user.business_id, name=name)
    db.add(new_cat)
    db.commit()
    db.refresh(new_cat)
    return {"id": new_cat.id, "name": new_cat.name}


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cat = db.query(Category).filter(Category.id == cat_id, Category.business_id == user.business_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    db.delete(cat)
    db.commit()
    return {"status": "success", "message": "Category removed"}


@app.get("/api/public/product/{barcode_num}")
def public_barcode_lookup(barcode_num: str, db: Session = Depends(get_db)):
    clean_code = re.sub(r'[\s-]', '', barcode_num.strip())
    prod = db.query(Product).filter(Product.barcode == clean_code).first()

    if not prod and len(clean_code) in (12, 13):
        prefix = clean_code[:12]
        prod = db.query(Product).filter(Product.barcode.startswith(prefix)).first()

    if not prod:
        raise HTTPException(status_code=404, detail=f"Barcode '{clean_code}' is not registered in this catalog.")

    biz_name = prod.business.name if prod.business else "Verified Brand"
    return {
        "status": "verified",
        "barcode": prod.barcode,
        "product_name": prod.name,
        "brand_name": biz_name,
        "mrp": prod.mrp,
        "formatted_mrp": f"₹{prod.mrp:.2f}",
        "sku": prod.sku,
        "category": prod.category,
        "batch_number": prod.batch_number or "B-REGULAR",
        "registered_at": prod.created_at.strftime("%Y-%m-%d")
    }


# Standalone Public Verification Page for Mobile Camera Scans
@app.get("/verify/{barcode_num}", response_class=HTMLResponse)
def verify_product_page(barcode_num: str, db: Session = Depends(get_db)):
    clean_code = re.sub(r'[\s-]', '', barcode_num.strip())
    prod = db.query(Product).filter(Product.barcode == clean_code).first()
    if not prod and len(clean_code) in (12, 13):
        prod = db.query(Product).filter(Product.barcode.startswith(clean_code[:12])).first()

    if not prod:
        return HTMLResponse(content=f"""
        <!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Verification Failed</title><script src="https://cdn.tailwindcss.com"></script></head>
        <body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">
          <div class="bg-white max-w-sm w-full p-8 rounded-3xl shadow-xl text-center border border-rose-200">
            <div class="w-16 h-16 bg-rose-100 text-rose-600 rounded-full flex items-center justify-center mx-auto text-2xl mb-4 font-bold">✕</div>
            <h1 class="text-xl font-extrabold text-slate-900">Unverified Barcode</h1>
            <p class="text-xs text-slate-500 mt-2 font-mono">{clean_code}</p>
            <p class="text-xs text-slate-600 mt-4 leading-relaxed">This item was not found in the verified LabelForge registry.</p>
          </div>
        </body></html>
        """, status_code=404)

    biz_name = prod.business.name if prod.business else "Verified Brand"
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Verified Product — {prod.name}</title>
      <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-50 min-h-screen flex items-center justify-center p-4">
      <div class="bg-white max-w-md w-full p-8 rounded-3xl shadow-2xl border border-slate-200 space-y-6">
        <div class="flex items-center justify-between pb-4 border-b border-slate-100">
          <span class="inline-flex items-center gap-1.5 px-3 py-1 bg-emerald-50 text-emerald-700 text-xs font-bold rounded-full">
            <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span> Authentic Product
          </span>
          <span class="text-xs font-bold text-slate-400 font-mono">GS1 Verified</span>
        </div>
        <div>
          <span class="text-[11px] font-bold text-slate-400 uppercase tracking-widest block">{biz_name}</span>
          <h1 class="text-2xl font-extrabold text-slate-900 mt-1">{prod.name}</h1>
          <div class="text-3xl font-black text-indigo-600 mt-3">₹{prod.mrp:.2f}</div>
        </div>
        <div class="grid grid-cols-2 gap-3 text-xs">
          <div class="p-3 bg-slate-50 rounded-2xl border border-slate-100">
            <span class="text-slate-400 block font-semibold text-[10px]">SKU CODE</span>
            <span class="font-mono font-bold text-slate-800 text-sm">{prod.sku}</span>
          </div>
          <div class="p-3 bg-slate-50 rounded-2xl border border-slate-100">
            <span class="text-slate-400 block font-semibold text-[10px]">CATEGORY</span>
            <span class="font-bold text-slate-800 text-sm">{prod.category}</span>
          </div>
          <div class="p-3 bg-slate-50 rounded-2xl border border-slate-100">
            <span class="text-slate-400 block font-semibold text-[10px]">BATCH</span>
            <span class="font-mono font-bold text-slate-800 text-sm">{prod.batch_number or "B-REG"}</span>
          </div>
          <div class="p-3 bg-slate-50 rounded-2xl border border-slate-100">
            <span class="text-slate-400 block font-semibold text-[10px]">BARCODE</span>
            <span class="font-mono font-bold text-slate-800 text-xs">{prod.barcode}</span>
          </div>
        </div>
        <div class="text-center pt-2">
          <span class="text-[10px] text-slate-400 font-medium">Secured by LabelForge GS1 Infrastructure</span>
        </div>
      </div>
    </body>
    </html>
    """)


@app.get("/api/config/payment")
def get_payment_details(amount: float = 799.0):
    amount_str = f"{amount:.2f}"
    txn_ref = f"ORD{int(datetime.utcnow().timestamp())}"
    payee_encoded = PAYMENT_CONFIG['payee_name'].replace(' ', '%20')
    upi_string = (
        f"upi://pay?pa={PAYMENT_CONFIG['upi_id']}"
        f"&pn={payee_encoded}"
        f"&am={amount_str}"
        f"&mam={amount_str}"
        f"&cu=INR"
        f"&tr={txn_ref}"
        f"&tn=Subscription%20Plan"
    )
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(upi_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "payment_info": PAYMENT_CONFIG,
        "upi_qr_base64": qr_b64,
        "upi_string": upi_string,
        "amount": amount
    }


@app.post("/api/billing/mock-pay")
def process_mock_payment(req: MockPaymentRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.business:
        raise HTTPException(status_code=400, detail="Business profile not found")

    target_plan = req.plan.lower()
    if target_plan not in ["free", "business", "professional"]:
        raise HTTPException(status_code=400, detail="Invalid plan selected")

    user.business.plan = target_plan
    db.commit()

    return {
        "status": "success",
        "message": f"Payment simulated successfully via {req.payment_mode.upper()}! Upgraded to {target_plan.capitalize()} Plan.",
        "new_plan": target_plan,
        "transaction_id": f"TXN-SIM-{int(datetime.utcnow().timestamp())}"
    }


@app.post("/api/auth/register")
def register(data: RegisterSchema, db: Session = Depends(get_db)):
    clean_email = data.email.strip().lower()
    if db.query(User).filter(User.email == clean_email).first():
        raise HTTPException(status_code=400, detail="Account with this email already exists")

    biz = Business(
        name=data.business_name.strip() or "My Retail Store",
        phone=data.phone or "",
        plan=data.plan or "free",
        sku_prefix="PRD",
        sku_padding=6
    )
    db.add(biz)
    db.commit()
    db.refresh(biz)

    for cat_name in ["General", "Apparel", "Footwear", "Electronics", "Cosmetics", "FMCG"]:
        db.add(Category(business_id=biz.id, name=cat_name))
    db.commit()

    user = User(
        business_id=biz.id,
        full_name=data.full_name.strip(),
        email=clean_email,
        phone=data.phone or "",
        hashed_password=hash_password(data.password),
        role="owner"
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": user.email, "role": user.role, "biz": biz.id})
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "business_name": biz.name,
            "sku_prefix": biz.sku_prefix,
            "sku_padding": biz.sku_padding,
            "plan": biz.plan,
            "is_admin": False
        }
    }


@app.post("/api/auth/login")
def login(data: LoginSchema, db: Session = Depends(get_db)):
    clean_email = data.email.strip().lower()
    user = db.query(User).filter(User.email == clean_email).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.is_suspended:
        raise HTTPException(status_code=403, detail="Account suspended. Contact support.")

    is_super = (user.email == ADMIN_EMAIL or user.role == "superadmin")
    biz_name = user.business.name if user.business else "Platform HQ"
    biz_plan = "lifetime_unlimited" if is_super else (user.business.plan if user.business else "free")
    sku_prefix = user.business.sku_prefix if user.business else "PRD"
    sku_padding = user.business.sku_padding if user.business else 6

    token = create_access_token({"sub": user.email, "role": user.role, "biz": user.business_id})
    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "business_name": biz_name,
            "sku_prefix": sku_prefix,
            "sku_padding": sku_padding,
            "plan": biz_plan,
            "is_admin": is_super
        }
    }


@app.get("/api/auth/me")
def get_current_user_profile(user: User = Depends(get_current_user)):
    is_super = (user.email == ADMIN_EMAIL or user.role == "superadmin")
    biz_name = user.business.name if user.business else "Platform HQ"
    biz_plan = "lifetime_unlimited" if is_super else (user.business.plan if user.business else "free")
    sku_prefix = user.business.sku_prefix if user.business else "PRD"
    sku_padding = user.business.sku_padding if user.business else 6
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "business_name": biz_name,
        "sku_prefix": sku_prefix,
        "sku_padding": sku_padding,
        "plan": biz_plan,
        "is_admin": is_super
    }


@app.get("/api/admin/overview")
def admin_get_overview(admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    all_users = db.query(User).order_by(User.created_at.desc()).all()
    all_businesses = db.query(Business).order_by(Business.created_at.desc()).all()
    all_products = db.query(Product).all()

    plan_counts = {"free": 0, "business": 0, "professional": 0, "lifetime_unlimited": 0}
    for b in all_businesses:
        p = (b.plan or "free").lower()
        plan_counts[p] = plan_counts.get(p, 0) + 1

    cfg_rec = db.query(SystemSetting).filter(SystemSetting.key == "pricing_config").first()
    pricing_config = {"free_price": 0, "business_price": 799, "professional_price": 1999}
    if cfg_rec and cfg_rec.value:
        try:
            parsed = json.loads(cfg_rec.value)
            pricing_config["business_price"] = float(parsed.get("business_price", 799))
            pricing_config["professional_price"] = float(parsed.get("professional_price", 1999))
        except Exception:
            pass

    customers_list = []
    for b in all_businesses:
        primary_user = next((u for u in b.users if u.role in ("owner", "superadmin")), None)
        if not primary_user and b.users:
            primary_user = b.users[0]

        prod_count = len(b.products)
        customers_list.append({
            "business_id": b.id,
            "business_name": b.name or "Untitled Business",
            "plan": b.plan or "free",
            "created_at": b.created_at.strftime("%Y-%m-%d"),
            "owner_id": primary_user.id if primary_user else None,
            "owner_name": primary_user.full_name if primary_user else "Registered User",
            "owner_email": primary_user.email if primary_user else "N/A",
            "is_suspended": primary_user.is_suspended if primary_user else False,
            "products_count": prod_count
        })

    mrr = (plan_counts.get("business", 0) * int(pricing_config["business_price"])) + (
                plan_counts.get("professional", 0) * int(pricing_config["professional_price"]))

    return {
        "stats": {
            "total_tenants": len(all_businesses),
            "total_users": len(all_users),
            "total_catalog_products": len(all_products),
            "estimated_mrr": mrr,
            "plans": plan_counts
        },
        "pricing_config": pricing_config,
        "customers": customers_list
    }


@app.put("/api/admin/customer/plan")
def admin_update_customer_plan(req: AdminUpdateCustomerPlanSchema, admin: User = Depends(require_superadmin),
                               db: Session = Depends(get_db)):
    biz = db.query(Business).filter(Business.id == req.business_id).first()
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found")
    biz.plan = req.new_plan.lower()
    db.commit()
    return {"status": "success", "message": f"Updated {biz.name} to {req.new_plan.upper()}"}


@app.put("/api/admin/user/suspend")
def admin_toggle_user_suspend(req: AdminToggleUserSuspendSchema, admin: User = Depends(require_superadmin),
                              db: Session = Depends(get_db)):
    target_user = db.query(User).filter(User.id == req.user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    if target_user.email == ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="Cannot suspend the platform SuperAdmin")

    target_user.is_suspended = req.is_suspended
    db.commit()
    return {"status": "success", "message": f"User {'suspended' if req.is_suspended else 'activated'} successfully"}


@app.delete("/api/admin/customer/{biz_id}")
def admin_delete_customer(biz_id: int, admin: User = Depends(require_superadmin), db: Session = Depends(get_db)):
    biz = db.query(Business).filter(Business.id == biz_id).first()
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found")
    if any(u.email == ADMIN_EMAIL for u in biz.users):
        raise HTTPException(status_code=400, detail="Cannot delete Platform Headquarters")

    db.delete(biz)
    db.commit()
    return {"status": "success", "message": "Business deleted successfully"}


@app.put("/api/admin/pricing")
def admin_update_pricing(req: AdminUpdatePricingSchema, admin: User = Depends(require_superadmin),
                         db: Session = Depends(get_db)):
    cfg_rec = db.query(SystemSetting).filter(SystemSetting.key == "pricing_config").first()
    new_cfg = {
        "free_price": req.free_price,
        "business_price": req.business_price,
        "professional_price": req.professional_price,
        "symbol": "₹"
    }
    if not cfg_rec:
        cfg_rec = SystemSetting(key="pricing_config", value=json.dumps(new_cfg))
        db.add(cfg_rec)
    else:
        cfg_rec.value = json.dumps(new_cfg)
    db.commit()
    return {"status": "success", "message": "Pricing configuration saved"}


@app.get("/api/products")
def get_products(query: Optional[str] = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.business_id:
        return []
    q = db.query(Product).filter(Product.business_id == user.business_id)
    if query and query.strip():
        term = f"%{query.strip().lower()}%"
        q = q.filter((Product.name.ilike(term)) | (Product.sku.ilike(term)) | (Product.barcode.ilike(term)))
    return q.order_by(Product.created_at.desc()).all()


@app.post("/api/products/auto")
def create_product_auto(data: CleanProductCreate, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    if not user.business_id:
        raise HTTPException(status_code=400, detail="User has no registered business profile")

    biz = user.business
    user_plan = biz.plan if biz else "free"
    is_super = (user.email == ADMIN_EMAIL or user.role == "superadmin" or user_plan == "lifetime_unlimited")
    total_count = db.query(Product).filter(Product.business_id == user.business_id).count()

    if not is_super:
        if user_plan == "free" and total_count >= 5:
            raise HTTPException(status_code=403,
                                detail="Free Plan limit reached (Max 5 products). Upgrade to Business for up to 100 products.")
        elif user_plan == "business" and total_count >= 100:
            raise HTTPException(status_code=403,
                                detail="Business Plan limit reached (Max 100 products). Upgrade to Enterprise for unlimited products.")

    prefix = biz.sku_prefix if biz and biz.sku_prefix else "PRD"
    padding = biz.sku_padding if biz and biz.sku_padding else 6

    sku_num = total_count + 1
    sku = f"{prefix}-{str(sku_num).zfill(padding)}"
    while db.query(Product).filter(Product.business_id == user.business_id, Product.sku == sku).first():
        sku_num += 1
        sku = f"{prefix}-{str(sku_num).zfill(padding)}"

    existing_barcodes = {p.barcode for p in db.query(Product.barcode).all()}
    barcode_val = BarcodeEngine.generate_unique_ean13(existing_barcodes)

    prod = Product(
        business_id=user.business_id,
        name=data.name.strip(),
        sku=sku,
        barcode=barcode_val,
        barcode_type="ean13",
        category=data.category or "General",
        mrp=data.mrp,
        selling_price=data.mrp,
        batch_number=f"B-{datetime.utcnow().strftime('%y%m%d')}",
    )
    db.add(prod)
    db.commit()
    db.refresh(prod)
    return prod


@app.put("/api/products/{prod_id}")
def update_product(prod_id: int, data: ProductUpdateSchema, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    prod = db.query(Product).filter(Product.id == prod_id, Product.business_id == user.business_id).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    prod.name = data.name.strip()
    prod.mrp = float(data.mrp)
    prod.selling_price = float(data.mrp)
    prod.category = data.category
    db.commit()
    db.refresh(prod)
    return prod


@app.delete("/api/products/{prod_id}")
def delete_product(prod_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    prod = db.query(Product).filter(Product.id == prod_id, Product.business_id == user.business_id).first()
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    db.delete(prod)
    db.commit()
    return {"status": "success", "message": "Product removed successfully"}


@app.put("/api/settings/company")
def update_company_settings(data: CompanySettingsSchema, user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    if not user.business:
        raise HTTPException(status_code=400, detail="Business not found")

    user.business.name = data.business_name.strip()
    user.business.sku_prefix = data.sku_prefix.strip().upper() or "PRD"
    user.business.sku_padding = max(3, min(data.sku_padding, 10))
    db.commit()

    return {
        "status": "success",
        "business_name": user.business.name,
        "sku_prefix": user.business.sku_prefix,
        "sku_padding": user.business.sku_padding
    }


@app.put("/api/settings/password")
def change_user_password(data: ChangePasswordSchema, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    if not verify_password(data.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password incorrect")

    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    user.hashed_password = hash_password(data.new_password)
    db.commit()
    return {"status": "success", "message": "Password changed successfully"}


@app.api_route("/api/barcodes/render", methods=["GET", "POST"])
def render_barcode(request: Request, symbology: str = "ean13", value: str = "", text: bool = True):
    try:
        base_url = str(request.base_url)
        svg_str = BarcodeEngine.generate_svg(symbology, value, text, base_url=base_url)
        return Response(content=svg_str, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==========================================
# 5. REPORTLAB PDF ENGINE
# ==========================================
@app.post("/api/labels/export-pdf")
def export_pdf_sheet(req: SheetPdfRequest, user: User = Depends(get_current_user)):
    user_plan = user.business.plan if user.business else "free"
    is_super = (user.email == ADMIN_EMAIL or user.role == "superadmin" or user_plan == "lifetime_unlimited")

    if not req.single_label and not req.single_barcode:
        if not is_super and user_plan == "free":
            raise HTTPException(status_code=403,
                                detail="Bulk printing is a Premium feature. Upgrade to Business or Enterprise.")
        if not is_super and user_plan == "business" and len(req.items) > 30:
            raise HTTPException(status_code=403, detail="Business Plan supports up to 30 labels/sheet.")

    buf = io.BytesIO()
    lw = req.label_width_mm * mm
    lh = req.label_height_mm * mm

    if req.single_barcode and len(req.items) > 0:
        c = canvas.Canvas(buf, pagesize=(lw, lh))
        itm = req.items[0]
        if itm.get("svg"):
            try:
                svg_buf = io.BytesIO(itm["svg"].encode('utf-8'))
                draw = svg2rlg(svg_buf)
                if draw and draw.width > 0 and draw.height > 0:
                    avail_w = lw - (6 * mm)
                    avail_h = lh - (6 * mm)
                    sc = min(avail_w / draw.width, avail_h / draw.height, 1.0)
                    draw.scale(sc, sc)
                    ox = (lw - (draw.width * sc)) / 2.0
                    oy = (lh - (draw.height * sc)) / 2.0
                    renderPDF.draw(draw, c, ox, oy)
            except Exception as e:
                print(f"[Barcode PDF Error] {e}")
        c.save()
        return Response(content=buf.getvalue(), media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=barcode_single.pdf"})

    if req.single_label and len(req.items) > 0:
        c = canvas.Canvas(buf, pagesize=(lw, lh))
        itm = req.items[0]
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.4)
        c.rect(1 * mm, 1 * mm, lw - (2 * mm), lh - (2 * mm))

        scale_factor = min(req.label_width_mm / 76.0, req.label_height_mm / 50.0)
        scale_factor = max(0.45, min(scale_factor, 1.4))

        top_cursor = lh - (3.5 * mm * scale_factor)
        center_x = lw / 2

        if itm.get("show_biz") and itm.get("biz"):
            c.setFillColorRGB(0.25, 0.25, 0.25)
            c.setFont("Helvetica-Bold", 7.5 * scale_factor)
            c.drawCentredString(center_x, top_cursor, str(itm.get("biz"))[:30].upper())
            top_cursor -= (4.0 * mm * scale_factor)

        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 8.5 * scale_factor)
        c.drawCentredString(center_x, top_cursor, str(itm.get("title", ""))[:28])
        top_cursor -= (3.8 * mm * scale_factor)

        pricing_text = ""
        if itm.get("show_mrp") and itm.get("mrp"):
            pricing_text += f"MRP: {itm.get('mrp')}"
        if pricing_text.strip():
            c.setFont("Helvetica-Bold", 7.5 * scale_factor)
            c.drawCentredString(center_x, top_cursor, pricing_text[:32])
            top_cursor -= (3.8 * mm * scale_factor)

        if itm.get("show_barcode") and itm.get("svg"):
            try:
                svg_buf = io.BytesIO(itm["svg"].encode('utf-8'))
                draw = svg2rlg(svg_buf)
                if draw and draw.width > 0 and draw.height > 0:
                    avail_w = lw - (8 * mm)
                    avail_h = max(10 * mm, top_cursor - (3 * mm))
                    sc = min(avail_w / draw.width, avail_h / draw.height, 1.0)
                    draw.scale(sc, sc)
                    # Center horizontally within the label width
                    ox = (lw - (draw.width * sc)) / 2.0
                    oy = 3 * mm + ((avail_h - (draw.height * sc)) / 2.0)
                    renderPDF.draw(draw, c, ox, oy)
            except Exception as e:
                print(f"[PDF Draw Error] {e}")
        c.save()
        return Response(content=buf.getvalue(), media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=single_label.pdf"})

    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    margin = 8 * mm
    gap = 2 * mm

    total_items = len(req.items)
    if total_items == 0:
        c.save()
        return Response(content=buf.getvalue(), media_type="application/pdf")

    best_scale = 0.01
    best_cols = 1
    best_rows = total_items
    avail_w = page_w - (2 * margin)
    avail_h = page_h - (2 * margin)

    for c_try in range(1, total_items + 1):
        r_try = math.ceil(total_items / c_try)
        w_req = c_try * lw + (c_try - 1) * gap
        h_req = r_try * lh + (r_try - 1) * gap

        scale_w = avail_w / w_req
        scale_h = avail_h / h_req
        s = min(scale_w, scale_h, 1.0)

        if s > best_scale:
            best_scale = s
            best_cols = c_try
            best_rows = r_try

    eff_lw = lw * best_scale
    eff_lh = lh * best_scale
    eff_gap = gap * best_scale

    total_grid_w = best_cols * eff_lw + (best_cols - 1) * eff_gap
    start_x = margin + (avail_w - total_grid_w) / 2
    start_y = page_h - margin

    for i, itm in enumerate(req.items):
        col_idx = i % best_cols
        row_idx = i // best_cols

        x = start_x + col_idx * (eff_lw + eff_gap)
        y = start_y - ((row_idx + 1) * eff_lh + row_idx * eff_gap)
        center_x = x + (eff_lw / 2)

        c.saveState()
        p = c.beginPath()
        p.rect(x, y, eff_lw, eff_lh)
        c.clipPath(p, stroke=0)

        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.3 * best_scale)
        c.rect(x, y, eff_lw, eff_lh)

        top_cursor = y + eff_lh - (2.5 * mm * best_scale)
        font_base = max(4.0, 7.0 * best_scale)

        if itm.get("show_biz") and itm.get("biz"):
            c.setFillColorRGB(0.3, 0.3, 0.3)
            c.setFont("Helvetica-Bold", font_base * 0.9)
            c.drawCentredString(center_x, top_cursor, str(itm.get("biz"))[:24].upper())
            top_cursor -= (3.2 * mm * best_scale)

        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", font_base)
        c.drawCentredString(center_x, top_cursor, str(itm.get("title", ""))[:22])
        top_cursor -= (2.8 * mm * best_scale)

        pricing_text = ""
        if itm.get("show_mrp") and itm.get("mrp"):
            pricing_text += f"MRP: {itm.get('mrp')}"
        if pricing_text.strip():
            c.setFont("Helvetica-Bold", font_base * 0.85)
            c.drawCentredString(center_x, top_cursor, pricing_text[:24])
            top_cursor -= (2.8 * mm * best_scale)

        if itm.get("show_barcode") and itm.get("svg"):
            try:
                svg_buf = io.BytesIO(itm["svg"].encode('utf-8'))
                draw = svg2rlg(svg_buf)
                if draw:
                    barcode_max_w = eff_lw - (4 * mm * best_scale)
                    barcode_max_h = max(3 * mm * best_scale, (top_cursor - y - (1.5 * mm * best_scale)))
                    sc = min(barcode_max_w / draw.width, barcode_max_h / draw.height)
                    draw.scale(sc, sc)
                    ox = x + (eff_lw - (draw.width * sc)) / 2
                    renderPDF.draw(draw, c, ox, y + 1.2 * mm * best_scale)
            except Exception:
                pass

        c.restoreState()

    c.save()
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=labels_single_sheet_a4.pdf"}
    )


# ==========================================
# 6. ENHANCED SPA INTERFACE
# ==========================================
SPA_HTML = """<!DOCTYPE html>
<html lang="en" class="scroll-smooth">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LabelForge — Enterprise Retail Barcode & SaaS Suite</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/html5-qrcode"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #f8fafc; color: #0f172a; margin: 0; padding: 0; }
    .mono { font-family: 'JetBrains Mono', monospace; }
    .barcode-svg-container svg { width: 100% !important; height: 100% !important; max-height: 100% !important; display: block; margin: 0 auto; }
    @keyframes scanline { 0% { transform: translateY(0); } 50% { transform: translateY(180px); } 100% { transform: translateY(0); } }
    .scanner-laser { animation: scanline 2.2s infinite ease-in-out; }
    .glow-indigo { box-shadow: 0 0 50px -10px rgba(99, 102, 241, 0.25); }
    .hero-pattern { background-image: radial-gradient(rgba(99, 102, 241, 0.12) 1px, transparent 1px); background-size: 24px 24px; }
  </style>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen antialiased selection:bg-indigo-600 selection:text-white">

  <div id="errorBoundary" class="hidden p-4 bg-rose-50 text-rose-800 border-b border-rose-200 text-xs font-mono font-bold"></div>
  <div id="appRoot"></div>

  <!-- ADD CATEGORY QUICK POPUP MODAL -->
  <div id="quickCategoryModal" class="hidden fixed inset-0 z-50 bg-slate-950/60 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-white w-full max-w-sm rounded-3xl p-6 shadow-2xl border border-slate-200">
      <div class="flex items-center justify-between pb-3 border-b border-slate-100 mb-4">
        <h3 class="font-bold text-slate-900 text-sm">Add New Category</h3>
        <button onclick="closeQuickCategoryModal()" class="text-slate-400 hover:text-slate-600 text-xl font-bold">&times;</button>
      </div>
      <form onsubmit="handleQuickAddCategory(event)" class="space-y-4">
        <div>
          <label class="block text-xs font-semibold text-slate-700 mb-1">Category Name</label>
          <input id="quickCategoryInput" type="text" required placeholder="e.g. Traditional Wear, Footwear" class="w-full px-3.5 py-2.5 border border-slate-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none">
        </div>
        <div class="pt-2 flex justify-end gap-2">
          <button type="button" onclick="closeQuickCategoryModal()" class="px-4 py-2 border border-slate-200 rounded-xl text-xs font-bold text-slate-600 hover:bg-slate-50">Cancel</button>
          <button type="submit" class="px-5 py-2 bg-indigo-600 text-white rounded-xl text-xs font-bold hover:bg-indigo-700">Add Category</button>
        </div>
      </form>
    </div>
  </div>

  <!-- EDIT PRODUCT MODAL -->
  <div id="editProductModal" class="hidden fixed inset-0 z-50 bg-slate-950/60 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-white w-full max-w-md rounded-3xl p-6 shadow-2xl border border-slate-200">
      <div class="flex items-center justify-between pb-4 border-b border-slate-100 mb-4">
        <h3 class="font-bold text-slate-900 text-base">Edit Catalog Product</h3>
        <button onclick="closeEditModal()" class="text-slate-400 hover:text-slate-600 text-xl font-bold">&times;</button>
      </div>
      <form onsubmit="saveEditedProduct(event)" class="space-y-4">
        <input type="hidden" id="editProdId">
        <div>
          <label class="block text-xs font-semibold text-slate-700 mb-1">Product Title</label>
          <input id="editProdName" type="text" required class="w-full px-3.5 py-2.5 border border-slate-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500">
        </div>
        <div>
          <label class="block text-xs font-semibold text-slate-700 mb-1">MRP (₹)</label>
          <input id="editProdMrp" type="number" step="0.01" required class="w-full px-3.5 py-2.5 border border-slate-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500">
        </div>
        <div>
          <label class="block text-xs font-semibold text-slate-700 mb-1">Category</label>
          <select id="editProdCat" class="w-full px-3.5 py-2.5 border border-slate-200 rounded-xl text-sm">
          </select>
        </div>
        <div>
          <label class="block text-xs font-semibold text-slate-400 mb-1">Assigned Barcode (Locked)</label>
          <input id="editProdBarcode" type="text" disabled readonly class="w-full px-3.5 py-2.5 border border-slate-200 bg-slate-100 font-mono text-xs rounded-xl text-slate-500">
        </div>
        <div class="pt-2 flex justify-end gap-2">
          <button type="button" onclick="closeEditModal()" class="px-4 py-2 border border-slate-200 rounded-xl text-xs font-bold text-slate-600 hover:bg-slate-50">Cancel</button>
          <button type="submit" class="px-5 py-2 bg-indigo-600 text-white rounded-xl text-xs font-bold hover:bg-indigo-700">Save Changes</button>
        </div>
      </form>
    </div>
  </div>

  <script>
  
  // ==========================================
    // HARDWARE BARCODE SCANNER LISTENER (HID)
    // ==========================================
    let barcodeBuffer = '';
    let lastKeyTime = 0;

    window.addEventListener('keydown', (e) => {
      // Only capture automated hardware scans if on the 'scanner' page
      if (state.view !== 'scanner') return;

      const currentTime = new Date().getTime();
      const timeDiff = currentTime - lastKeyTime;
      lastKeyTime = currentTime;

      // Handle the 'Enter' suffix transmitted by the scanner
      if (e.key === 'Enter') {
        if (barcodeBuffer.length >= 8) {
          e.preventDefault();
          state.scannerInput = barcodeBuffer.trim();
          barcodeBuffer = '';
          const inputEl = document.getElementById('scanInput');
          if (inputEl) inputEl.value = state.scannerInput;
          performScanLookup();
        }
        return;
      }

      // Barcode scanners type keystrokes in under 40 milliseconds
      if (timeDiff < 50) {
        if (e.key.length === 1) {
          barcodeBuffer += e.key;
        }
      } else {
        // Reset buffer if standard slow human typing
        barcodeBuffer = e.key.length === 1 ? e.key : '';
      }
    });
  
    window.onerror = function(msg, url, line) {
      const errBox = document.getElementById('errorBoundary');
      if (errBox) {
        errBox.classList.remove('hidden');
        errBox.innerText = "Error: " + msg + " (Line " + line + ")";
      }
    };

    const DEFAULT_ADMIN_EMAIL = "admin@lableforge.com";
    const DEFAULT_ADMIN_PASS = "Chopraji995#";

    let state = {
      view: 'landing',
      navHistory: [],
      token: null,
      user: null,
      authMode: 'login',
      authData: { full_name: '', email: '', phone: '', password: '', business_name: '', plan: 'free' },
      addProductDraft: { name: '', mrp: '', category: 'General' },
      products: [],
      categories: ["General", "Apparel", "Footwear", "Electronics", "Cosmetics", "FMCG"],
      paymentData: null,
      selectedPlanForCheckout: null,
      activePaymentTab: 'upi',
      selectedBank: 'HDFC Bank',
      selectedProductId: null,
      dimWidth: 76,
      dimHeight: 50,
      options: { showBiz: true, showMrp: true, showBarcode: true, showSku: true, showBatch: true },
      currentSvg: '',
      barcodeSymbology: 'ean13',
      barcodeVal: '',
      barcodeSvg: '',
      bulkCount: 16,
      scannerInput: '',
      scannerResult: null,
      scannerError: '',
      html5QrCodeInstance: null,
      isScanning: false,
      calcVolume: 1000,
      adminOverview: null
    };

    try {
      state.token = localStorage.getItem('token') || null;
      state.user = JSON.parse(localStorage.getItem('user') || 'null');
    } catch(e) {
      localStorage.clear();
      state.token = null;
      state.user = null;
    }

    function ensureProductSelected() {
      if (state.products && state.products.length > 0) {
        if (!state.selectedProductId || !state.products.find(p => p.id === state.selectedProductId)) {
          state.selectedProductId = state.products[0].id;
          state.barcodeVal = state.products[0].barcode;
        }
      }
    }

    function navigate(viewName, isBackAction = false) {
      if (!isBackAction && state.view !== viewName) {
        state.navHistory.push(state.view);
      }
      state.view = viewName;
      if (state.isScanning && viewName !== 'scanner') {
        stopLiveCameraScanner();
      }

      if (viewName === 'barcodes' || viewName === 'labels') {
        ensureProductSelected();
      }

      if (viewName === 'admin_hq') {
        loadAdminOverview();
      }

      render();
      window.scrollTo(0, 0);
    }

    function goBack() {
      if (state.navHistory.length > 0) {
        const prev = state.navHistory.pop();
        navigate(prev, true);
      } else {
        navigate(isSuperAdmin() ? 'admin_hq' : 'dashboard', true);
      }
    }

    function isSuperAdmin() {
      if (!state.user) return false;
      return state.user.is_admin || state.user.email === DEFAULT_ADMIN_EMAIL || state.user.role === 'superadmin';
    }

    function getUserPlan() {
      if (!state.user) return 'free';
      if (isSuperAdmin()) return 'lifetime_unlimited';
      return (state.user.plan || 'free').toLowerCase();
    }

    async function apiRequest(endpoint, method = 'GET', body = null) {
      const headers = { 'Content-Type': 'application/json' };
      if (state.token) {
        headers['Authorization'] = 'Bearer ' + state.token;
      }
      const options = { method, headers };
      if (body) {
        options.body = JSON.stringify(body);
      }
      const res = await fetch(endpoint, options);
      if (res.status === 401) {
        logout();
        throw new Error("Session expired. Please sign in again.");
      }
      if (!res.ok) {
        let errDetail = 'Request failed';
        try {
          const errData = await res.json();
          errDetail = errData.detail || JSON.stringify(errData);
        } catch(_) {
          errDetail = await res.text();
        }
        throw new Error(errDetail);
      }
      return res.json();
    }

    async function loadCategories() {
      if (!state.token) return;
      try {
        const cats = await apiRequest('/api/categories');
        if (cats && cats.length > 0) {
          state.categories = cats;
        }
      } catch (err) {
        console.warn("Categories fetch note:", err);
      }
    }

    async function refreshUserProfile() {
      if (!state.token) return;
      try {
        const profile = await apiRequest('/api/auth/me');
        state.user = profile;
        localStorage.setItem('user', JSON.stringify(profile));
      } catch (err) {
        console.warn("Profile sync error:", err);
      }
    }

    async function loadProducts() {
      if (!state.token) return;
      try {
        const prods = await apiRequest('/api/products');
        state.products = prods || [];
        ensureProductSelected();
      } catch (err) {
        console.error("Failed to load products:", err);
      }
    }

    async function loadAdminOverview() {
      if (!isSuperAdmin()) return;
      try {
        const overview = await apiRequest('/api/admin/overview');
        state.adminOverview = overview;
        render();
      } catch (err) {
        alert("Admin load failed: " + err.message);
      }
    }

    function logout() {
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      state.token = null;
      state.user = null;
      state.products = [];
      state.selectedProductId = null;
      state.navHistory = [];
      navigate('landing');
    }

    async function handleLogin(e) {
      e.preventDefault();
      const email = document.getElementById('logEmail').value.trim();
      const password = document.getElementById('logPass').value;
      try {
        const data = await apiRequest('/api/auth/login', 'POST', { email, password });
        localStorage.setItem('token', data.token);
        localStorage.setItem('user', JSON.stringify(data.user));
        state.token = data.token;
        state.user = data.user;
        await loadCategories();
        await loadProducts();
        navigate(isSuperAdmin() ? 'admin_hq' : 'dashboard');
      } catch (err) {
        alert("Login failed: " + err.message);
      }
    }

    async function handleRegister(e) {
      e.preventDefault();
      try {
        const data = await apiRequest('/api/auth/register', 'POST', state.authData);
        localStorage.setItem('token', data.token);
        localStorage.setItem('user', JSON.stringify(data.user));
        state.token = data.token;
        state.user = data.user;
        await loadCategories();
        await loadProducts();
        navigate('dashboard');
      } catch (err) {
        alert("Registration failed: " + err.message);
      }
    }

    async function handleAddProductForm(e) {
      e.preventDefault();
      const name = state.addProductDraft.name.trim();
      const mrp = parseFloat(state.addProductDraft.mrp) || 0;
      const category = state.addProductDraft.category;

      try {
        const newP = await apiRequest('/api/products/auto', 'POST', { name, mrp, category });
        state.addProductDraft = { name: '', mrp: '', category: 'General' };
        await loadProducts();
        state.selectedProductId = newP.id;
        state.barcodeVal = newP.barcode;
        navigate('manage_catalog');
      } catch (err) {
        alert("Failed to add product: " + err.message);
      }
    }

    function openQuickCategoryModal() {
      const modal = document.getElementById('quickCategoryModal');
      const input = document.getElementById('quickCategoryInput');
      if (modal) {
        modal.classList.remove('hidden');
        if (input) {
          input.value = '';
          input.focus();
        }
      }
    }

    function closeQuickCategoryModal() {
      const modal = document.getElementById('quickCategoryModal');
      if (modal) modal.classList.add('hidden');
    }

    async function handleQuickAddCategory(e) {
      e.preventDefault();
      const input = document.getElementById('quickCategoryInput');
      const name = input ? input.value.trim() : '';
      if (!name) return;

      try {
        const added = await apiRequest('/api/categories', 'POST', { name });
        closeQuickCategoryModal();
        await loadCategories();
        state.addProductDraft.category = added.name;
        render();
      } catch (err) {
        alert("Could not add category: " + err.message);
      }
    }

    function openEditModal(prodId) {
      const p = state.products.find(x => x.id === prodId);
      if (!p) return;
      document.getElementById('editProdId').value = p.id;
      document.getElementById('editProdName').value = p.name;
      document.getElementById('editProdMrp').value = p.mrp;

      const catSelect = document.getElementById('editProdCat');
      catSelect.innerHTML = (state.categories || []).map(c => {
        const val = typeof c === 'string' ? c : c.name;
        return `<option value="${val}" ${p.category === val ? 'selected' : ''}>${val}</option>`;
      }).join('');

      document.getElementById('editProdBarcode').value = p.barcode;
      document.getElementById('editProductModal').classList.remove('hidden');
    }

    function closeEditModal() {
      document.getElementById('editProductModal').classList.add('hidden');
    }

    async function saveEditedProduct(e) {
      e.preventDefault();
      const id = parseInt(document.getElementById('editProdId').value);
      const name = document.getElementById('editProdName').value;
      const mrp = parseFloat(document.getElementById('editProdMrp').value) || 0;
      const category = document.getElementById('editProdCat').value;

      try {
        await apiRequest(`/api/products/${id}`, 'PUT', { name, mrp, category });
        closeEditModal();
        await loadProducts();
        render();
      } catch (err) {
        alert("Save failed: " + err.message);
      }
    }

    async function deleteProductPrompt(id, name) {
      if (!confirm(`Are you sure you want to remove "${name}" from the catalog?`)) return;
      try {
        await apiRequest(`/api/products/${id}`, 'DELETE');
        await loadProducts();
        ensureProductSelected();
        render();
      } catch (err) {
        alert("Delete failed: " + err.message);
      }
    }

    async function handleAddCategory(e) {
      e.preventDefault();
      const input = document.getElementById('newCategoryNameInput');
      const name = input.value.trim();
      if (!name) return;

      try {
        await apiRequest('/api/categories', 'POST', { name });
        input.value = '';
        await loadCategories();
        render();
      } catch (err) {
        alert("Could not add category: " + err.message);
      }
    }

    async function handleDeleteCategory(catId, catName) {
      if (!confirm(`Delete category "${catName}"?`)) return;
      try {
        await apiRequest(`/api/categories/${catId}`, 'DELETE');
        await loadCategories();
        render();
      } catch (err) {
        alert("Delete failed: " + err.message);
      }
    }

    async function handleCompanySettingsSave(e) {
      e.preventDefault();
      const business_name = document.getElementById('setBizName').value;
      const sku_prefix = document.getElementById('setSkuPrefix').value;
      const sku_padding = parseInt(document.getElementById('setSkuPadding').value) || 6;
      try {
        const res = await apiRequest('/api/settings/company', 'PUT', { business_name, sku_prefix, sku_padding });
        if (state.user) {
          state.user.business_name = res.business_name;
          state.user.sku_prefix = res.sku_prefix;
          state.user.sku_padding = res.sku_padding;
          localStorage.setItem('user', JSON.stringify(state.user));
        }
        alert("Company settings saved!");
        render();
      } catch (err) {
        alert("Save failed: " + err.message);
      }
    }

    async function handlePasswordChange(e) {
      e.preventDefault();
      const current_password = document.getElementById('curPassword').value;
      const new_password = document.getElementById('newPassword').value;
      const confirm_password = document.getElementById('confirmPassword').value;

      if (new_password !== confirm_password) {
        alert("New passwords do not match.");
        return;
      }
      try {
        await apiRequest('/api/settings/password', 'PUT', { current_password, new_password });
        alert("Password updated successfully.");
        document.getElementById('curPassword').value = '';
        document.getElementById('newPassword').value = '';
        document.getElementById('confirmPassword').value = '';
      } catch (err) {
        alert("Error: " + err.message);
      }
    }

    function selectBarcodeProduct(prodId) {
      state.selectedProductId = parseInt(prodId);
      const p = state.products.find(x => x.id === state.selectedProductId);
      if (p) {
        state.barcodeVal = p.barcode;
      }
      renderBarcodeStudio();
    }

    async function renderBarcodeStudio() {
      ensureProductSelected();
      const p = state.products.find(x => x.id === state.selectedProductId);
      if (!p) {
        state.barcodeSvg = '';
        const box = document.getElementById('bcStudioBox');
        if (box) box.innerHTML = '<div class="text-slate-400 text-xs text-center">No product selected</div>';
        return;
      }
      const val = p.barcode;
      state.barcodeVal = val;
      const symbology = state.barcodeSymbology || 'ean13';

      try {
        const res = await fetch(`/api/barcodes/render?symbology=${symbology}&value=${encodeURIComponent(val)}&text=true`);
        if (!res.ok) {
          const errDetail = await res.text();
          throw new Error(errDetail || "Barcode generation error");
        }
        state.barcodeSvg = await res.text();
        const box = document.getElementById('bcStudioBox');
        if (box) box.innerHTML = state.barcodeSvg;
        const errEl = document.getElementById('bcStudioErr');
        if (errEl) errEl.innerText = '';
      } catch (e) {
        const errEl = document.getElementById('bcStudioErr');
        if (errEl) errEl.innerText = e.message;
      }
    }

    function selectProduct(prodId) {
      state.selectedProductId = parseInt(prodId);
      updateLabelPreviewDOM();
    }

    function updateLabelDimensionsFromInput(type, val) {
      const num = parseFloat(val) || 10;
      if (type === 'w') state.dimWidth = num;
      if (type === 'h') state.dimHeight = num;
      updateLabelPreviewDOM();
    }

    function setPresetDimensions(w, h) {
      state.dimWidth = w;
      state.dimHeight = h;
      const wEl = document.getElementById('dimWidthInput');
      const hEl = document.getElementById('dimHeightInput');
      if (wEl) wEl.value = w;
      if (hEl) hEl.value = h;
      updateLabelPreviewDOM();
    }

    async function updateLabelPreviewDOM() {
      ensureProductSelected();
      const p = state.products.find(x => x.id === state.selectedProductId);
      const container = document.getElementById('labelPreviewSlot');
      const dimDisplay = document.getElementById('targetDimDisplay');
      if (dimDisplay) dimDisplay.innerText = `Target: ${state.dimWidth}mm × ${state.dimHeight}mm`;

      if (!p) {
        if (container) container.innerHTML = '<div class="text-slate-400 text-xs text-center">No product selected</div>';
        return;
      }

      try {
        const res = await fetch(`/api/barcodes/render?symbology=ean13&value=${encodeURIComponent(p.barcode)}&text=true`);
        if (res.ok) {
          state.currentSvg = await res.text();
        } else {
          state.currentSvg = '';
        }
      } catch(e) {
        state.currentSvg = '';
      }

      if (container) {
        container.innerHTML = generateDynamicLabelMarkup(p, false);
      }
    }

    function generateDynamicLabelMarkup(p, isPrintMode) {
      if (!p) return '<div class="text-slate-400 text-xs text-center">No product selected</div>';
      const bizName = (state.user && state.user.business_name) ? state.user.business_name : 'LabelForge Pro';

      const pxW = Math.max(120, state.dimWidth * 3.77);
      const pxH = Math.max(80, state.dimHeight * 3.77);

      return `
        <div style="width: ${pxW}px; height: ${pxH}px;" class="bg-white border-2 border-slate-300 rounded-xl p-3 shadow-sm flex flex-col justify-between items-center text-center overflow-hidden select-none mx-auto">
          <div class="w-full flex flex-col items-center">
            ${state.options.showBiz ? `<div class="text-[10px] font-extrabold text-slate-400 uppercase tracking-widest leading-tight truncate w-full">${bizName}</div>` : ''}
            <div class="text-xs font-bold text-slate-900 leading-tight mt-0.5 truncate w-full">${p.name}</div>
            ${state.options.showMrp ? `<div class="text-xs font-extrabold text-indigo-700 leading-tight mt-0.5 w-full">MRP: ₹${p.mrp.toFixed(2)}</div>` : ''}
            <div class="flex items-center justify-center gap-2 text-[9px] text-slate-500 font-mono mt-0.5 w-full">
              ${state.options.showSku ? `<span>SKU: ${p.sku}</span>` : ''}
              ${state.options.showBatch ? `<span>• BATCH: ${p.batch_number || 'B-REG'}</span>` : ''}
            </div>
          </div>
          ${state.options.showBarcode && state.currentSvg ? `
            <div class="w-full flex-1 max-h-[50%] flex items-center justify-center barcode-svg-container mt-1 mx-auto">
              ${state.currentSvg}
            </div>
          ` : ''}
        </div>
      `;
    }

    async function downloadBarcodePdf() {
      ensureProductSelected();
      const p = state.products.find(x => x.id === state.selectedProductId);
      if (!p) return alert("Select a product first or add a product to your catalog.");
      if (!state.barcodeSvg) await renderBarcodeStudio();

      const payload = {
        items: [{ svg: state.barcodeSvg }],
        label_width_mm: 75.0,
        label_height_mm: 35.0,
        single_barcode: true
      };

      const res = await fetch('/api/labels/export-pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + state.token },
        body: JSON.stringify(payload)
      });
      if (!res.ok) return alert("PDF generation failed");
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `barcode_${p.sku}.pdf`;
      a.click();
    }

    async function downloadLabelPdf() {
      ensureProductSelected();
      const p = state.products.find(x => x.id === state.selectedProductId);
      if (!p) return alert("Select a product first or add a product to your catalog.");

      const payload = {
        items: [{
          title: p.name,
          mrp: `₹${p.mrp.toFixed(2)}`,
          biz: state.user ? state.user.business_name : 'LabelForge',
          sku: p.sku,
          batch: p.batch_number,
          svg: state.currentSvg,
          show_biz: state.options.showBiz,
          show_mrp: state.options.showMrp,
          show_barcode: state.options.showBarcode
        }],
        label_width_mm: state.dimWidth,
        label_height_mm: state.dimHeight,
        single_label: true
      };

      const res = await fetch('/api/labels/export-pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + state.token },
        body: JSON.stringify(payload)
      });
      if (!res.ok) return alert("Single label PDF failed");
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `label_${p.sku}.pdf`;
      a.click();
    }

    async function downloadBulkSingleSheet() {
      ensureProductSelected();
      const p = state.products.find(x => x.id === state.selectedProductId);
      if (!p) return alert("Select a product first or add a product to your catalog.");
      const count = parseInt(state.bulkCount) || 16;

      const singleItem = {
        title: p.name,
        mrp: `₹${p.mrp.toFixed(2)}`,
        biz: state.user ? state.user.business_name : 'LabelForge',
        sku: p.sku,
        batch: p.batch_number,
        svg: state.currentSvg,
        show_biz: state.options.showBiz,
        show_mrp: state.options.showMrp,
        show_barcode: state.options.showBarcode
      };

      const items = Array(count).fill(singleItem);
      const payload = {
        items,
        label_width_mm: state.dimWidth,
        label_height_mm: state.dimHeight,
        single_label: false
      };

      const res = await fetch('/api/labels/export-pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + state.token },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return alert(err.detail || "Bulk sheet failed");
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `bulk_sheet_${p.sku}_x${count}.pdf`;
      a.click();
    }

    async function performScanLookup() {
      let val = state.scannerInput.trim();
      if (!val) return;
      if (val.includes('/verify/')) {
        val = val.split('/verify/')[1].split('?')[0].split('#')[0];
      }
      state.scannerError = '';
      state.scannerResult = null;

      try {
        const res = await fetch(`/api/public/product/${encodeURIComponent(val)}`);
        if (!res.ok) throw new Error(`Barcode ${val} not found in catalog.`);
        state.scannerResult = await res.json();
      } catch (err) {
        state.scannerError = err.message;
      }
      render();
    }

    function startLiveCameraScanner() {
      if (state.isScanning) {
        stopLiveCameraScanner();
        return;
      }
      const readerEl = document.getElementById('reader');
      if (!readerEl) return;

      state.isScanning = true;
      render();

      setTimeout(() => {
        try {
          state.html5QrCodeInstance = new Html5Qrcode("reader");
          state.html5QrCodeInstance.start(
            { facingMode: "environment" },
            { fps: 10, qrbox: { width: 250, height: 150 } },
            (decodedText) => {
              state.scannerInput = decodedText;
              stopLiveCameraScanner();
              performScanLookup();
            },
            (err) => {}
          ).catch((e) => {
            state.isScanning = false;
            state.scannerError = "Camera access denied or unavailable: " + e;
            render();
          });
        } catch (e) {
          state.isScanning = false;
          state.scannerError = "Scanner initialization failed: " + e;
          render();
        }
      }, 100);
    }

    function stopLiveCameraScanner() {
      if (state.html5QrCodeInstance && state.isScanning) {
        state.html5QrCodeInstance.stop().then(() => {
          state.html5QrCodeInstance.clear();
          state.html5QrCodeInstance = null;
          state.isScanning = false;
          render();
        }).catch(() => {
          state.isScanning = false;
          render();
        });
      } else {
        state.isScanning = false;
      }
    }

    async function startUpgrade(planKey, price) {
      if (!state.token) {
        state.authMode = 'login';
        navigate('auth');
        return;
      }
      state.selectedPlanForCheckout = {
        key: planKey,
        title: planKey === 'business' ? 'Business Professional' : 'Enterprise HQ',
        price: price
      };
      try {
        state.paymentData = await apiRequest(`/api/config/payment?amount=${price}`);
      } catch(_) {
        state.paymentData = null;
      }
      navigate('checkout');
    }

    async function triggerMockPayment() {
      if (!state.selectedPlanForCheckout) return;
      try {
        const res = await apiRequest('/api/billing/mock-pay', 'POST', {
          plan: state.selectedPlanForCheckout.key,
          amount: state.selectedPlanForCheckout.price,
          payment_mode: state.activePaymentTab,
          bank_name: state.selectedBank
        });
        alert(res.message);
        await refreshUserProfile();
        navigate('dashboard');
      } catch(err) {
        alert("Payment simulation failed: " + err.message);
      }
    }

    async function adminChangeCustomerPlan(bizId, newPlan) {
      try {
        await apiRequest('/api/admin/customer/plan', 'PUT', { business_id: bizId, new_plan: newPlan });
        await refreshUserProfile();
        await loadAdminOverview();
      } catch(err) {
        alert("Failed to update plan: " + err.message);
      }
    }

    async function adminToggleSuspend(userId, currentSuspended) {
      try {
        await apiRequest('/api/admin/user/suspend', 'PUT', { user_id: userId, is_suspended: !currentSuspended });
        await loadAdminOverview();
      } catch(err) {
        alert("Toggle suspend failed: " + err.message);
      }
    }

    async function adminDeleteTenant(bizId, bizName) {
      if (!confirm(`Are you sure you want to permanently delete "${bizName}" and all associated products?`)) return;
      try {
        await apiRequest(`/api/admin/customer/${bizId}`, 'DELETE');
        await loadAdminOverview();
      } catch(err) {
        alert("Delete failed: " + err.message);
      }
    }

    async function adminSavePricing(e) {
      e.preventDefault();
      const bPrice = parseFloat(document.getElementById('admBizPrice').value) || 799;
      const pPrice = parseFloat(document.getElementById('admProPrice').value) || 1999;
      try {
        await apiRequest('/api/admin/pricing', 'PUT', { free_price: 0, business_price: bPrice, professional_price: pPrice });
        alert("Platform pricing updated!");
        await loadAdminOverview();
      } catch(err) {
        alert("Pricing save failed: " + err.message);
      }
    }

    let catChartInstance = null;
    let volChartInstance = null;
    let landingScanChart = null;
    let landingCostChart = null;

    function initDashboardCharts() {
      const pieCanvas = document.getElementById('categoryPieChart');
      const barCanvas = document.getElementById('volumeBarChart');
      if (!pieCanvas || !barCanvas) return;

      const catCounts = {};
      state.products.forEach(p => {
        catCounts[p.category] = (catCounts[p.category] || 0) + 1;
      });
      const catLabels = Object.keys(catCounts);
      const catData = Object.values(catCounts);

      if (catChartInstance) { catChartInstance.destroy(); catChartInstance = null; }
      if (volChartInstance) { volChartInstance.destroy(); volChartInstance = null; }

      catChartInstance = new Chart(pieCanvas, {
        type: 'doughnut',
        data: {
          labels: catLabels.length ? catLabels : ['No Products'],
          datasets: [{
            data: catData.length ? catData : [1],
            backgroundColor: ['#6366f1', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#94a3b8']
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom' } }
        }
      });

      volChartInstance = new Chart(barCanvas, {
        type: 'bar',
        data: {
          labels: state.products.slice(0, 7).map(p => p.sku) || ['No Data'],
          datasets: [{
            label: 'Product MRP (₹)',
            data: state.products.slice(0, 7).map(p => p.mrp) || [0],
            backgroundColor: '#4f46e5',
            borderRadius: 6
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true } }
        }
      });
    }

    function initLandingCharts() {
      const scanEl = document.getElementById('landingScanEfficiencyChart');
      const costEl = document.getElementById('landingCostSavingsChart');
      if (!scanEl || !costEl) return;

      if (landingScanChart) { landingScanChart.destroy(); landingScanChart = null; }
      if (landingCostChart) { landingCostChart.destroy(); landingCostChart = null; }

      landingScanChart = new Chart(scanEl, {
        type: 'line',
        data: {
          labels: ['10mm', '20mm', '30mm', '40mm', '50mm', '76mm'],
          datasets: [
            {
              label: 'LabelForge GS1 Vector (%)',
              data: [99.2, 99.7, 99.9, 100, 100, 100],
              borderColor: '#4f46e5',
              backgroundColor: 'rgba(79, 70, 229, 0.1)',
              tension: 0.35,
              fill: true,
              borderWidth: 3
            },
            {
              label: 'Rasterized PNG/Bitmaps (%)',
              data: [54.0, 68.2, 81.0, 87.5, 91.2, 93.0],
              borderColor: '#f43f5e',
              borderDash: [5, 5],
              tension: 0.35,
              borderWidth: 2
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: 'top' },
            tooltip: { mode: 'index', intersect: false }
          },
          scales: {
            y: { min: 40, max: 105, title: { display: true, text: 'First-Pass Optical Scan Rate (%)' } },
            x: { title: { display: true, text: 'Print Dimension Width' } }
          }
        }
      });

      landingCostChart = new Chart(costEl, {
        type: 'bar',
        data: {
          labels: ['Roll Waste', 'Thermal Ink', 'Defective Tags', 'Operator Hours'],
          datasets: [
            {
              label: 'Conventional Legacy Pipeline',
              data: [2400, 1800, 3100, 4200],
              backgroundColor: '#cbd5e1',
              borderRadius: 6
            },
            {
              label: 'LabelForge Vector Engine',
              data: [420, 310, 0, 850],
              backgroundColor: '#10b981',
              borderRadius: 6
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'top' } },
          scales: { y: { beginAtZero: true, title: { display: true, text: 'Quarterly Cost Expense (₹)' } } }
        }
      });
    }

    function updateCalcVolume(val) {
      state.calcVolume = parseInt(val) || 500;
      const volDisplay = document.getElementById('calcVolDisplay');
      const savDisplay = document.getElementById('calcSavDisplay');
      const timeDisplay = document.getElementById('calcTimeDisplay');
      if (volDisplay) volDisplay.innerText = state.calcVolume.toLocaleString('en-IN') + ' units / month';

      const monthlySavings = Math.round(state.calcVolume * 4.9);
      const hoursSaved = Math.round((state.calcVolume / 100) * 1.5);
      if (savDisplay) savDisplay.innerText = '₹' + monthlySavings.toLocaleString('en-IN');
      if (timeDisplay) timeDisplay.innerText = hoursSaved + ' hrs / mo';
    }

    function renderPublicNavbar() {
      const isLoggedIn = !!state.token;
      return `
        <header class="sticky top-0 z-50 bg-white/90 backdrop-blur-md border-b border-slate-200">
          <div class="max-w-7xl mx-auto px-4 sm:px-6 h-18 py-3 flex items-center justify-between">
            <div class="flex items-center gap-3 cursor-pointer" onclick="navigate('landing')">
              <div class="h-10 w-10 rounded-2xl bg-gradient-to-tr from-indigo-600 via-indigo-700 to-violet-600 flex items-center justify-center text-white shadow-md shadow-indigo-300 font-extrabold text-base">LF</div>
              <div>
                <span class="font-extrabold text-xl text-slate-900 tracking-tight leading-none block">Label<span class="text-indigo-600">Forge</span></span>
                <span class="text-[9px] uppercase tracking-widest text-slate-400 font-bold">Enterprise GS1 Engine</span>
              </div>
            </div>

            <nav class="hidden lg:flex items-center gap-8 text-xs uppercase tracking-widest font-bold text-slate-600">
              <a href="#features" class="hover:text-indigo-600 transition">Features</a>
              <a href="#analytics" class="hover:text-indigo-600 transition">Benchmarking</a>
              <a href="#calculator" class="hover:text-indigo-600 transition">ROI Calculator</a>
              <a href="#plans" onclick="navigate('plans')" class="hover:text-indigo-600 transition">Commercial Plans</a>
              <a href="#founder" class="hover:text-indigo-600 transition text-indigo-700">About Founder</a>
            </nav>

            <div class="flex items-center gap-3">
              ${isLoggedIn ? `
                <button onclick="navigate('${isSuperAdmin() ? 'admin_hq' : 'dashboard'}')" class="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-bold shadow-md shadow-indigo-200 transition flex items-center gap-2">
                  <span>Enter Workspace</span> &rarr;
                </button>
              ` : `
                <button onclick="navigate('auth'); state.authMode='login'; render();" class="px-4 py-2 text-xs font-bold text-slate-700 hover:text-slate-950 transition">Sign In</button>
                <button onclick="navigate('auth'); state.authMode='register'; render();" class="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-bold shadow-md shadow-indigo-200 transition">Create Account</button>
              `}
            </div>
          </div>
        </header>
      `;
    }

    function renderLanding() {
      const isLoggedIn = !!state.token;
      return `
        ${renderPublicNavbar()}
        <div class="overflow-x-hidden">
          <section class="relative pt-24 pb-28 border-b border-slate-200 bg-white hero-pattern">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 relative z-10 text-center">
              <div class="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-indigo-50 border border-indigo-200/80 text-xs font-bold text-indigo-800 mb-8 shadow-sm">
                <span class="w-2 h-2 rounded-full bg-indigo-600 animate-pulse"></span>
                Official GS1 Compliant EAN-13 Vector Barcode Synthesizer & Multi-Tenant SaaS
              </div>
              <h1 class="text-4xl sm:text-7xl font-extrabold text-slate-900 tracking-tight leading-[1.08] max-w-5xl mx-auto">
                Deterministic Barcodes. <br class="hidden sm:inline" />
                <span class="text-transparent bg-clip-text bg-gradient-to-r from-indigo-600 via-violet-600 to-indigo-800">Zero Scan Failures at Checkout.</span>
              </h1>
              <p class="mt-8 text-base sm:text-xl text-slate-600 max-w-3xl mx-auto leading-relaxed">
                Transform blurry, stretched labels into pixel-perfect GS1 vector PDF assets. Built with dynamic checksum integrity, automatic Indian retail prefixes, quiet zone safeguarding, and intelligent single-sheet packing.
              </p>

              <div class="mt-12 flex flex-wrap items-center justify-center gap-4">
                ${isLoggedIn ? `
                  <button onclick="navigate('${isSuperAdmin() ? 'admin_hq' : 'dashboard'}')" class="px-9 py-4 rounded-2xl bg-indigo-600 hover:bg-indigo-700 text-white font-extrabold text-sm shadow-xl shadow-indigo-200 transition flex items-center gap-3">
                    <span>Open ${isSuperAdmin() ? 'SuperAdmin Control Hub' : 'Store Dashboard'}</span>
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
                  </button>
                ` : `
                  <button onclick="navigate('auth'); state.authMode='register'; render();" class="px-9 py-4 rounded-2xl bg-indigo-600 hover:bg-indigo-700 text-white font-extrabold text-sm shadow-xl shadow-indigo-200 transition">
                    Start Free (5 Products Included)
                  </button>
                  <button onclick="navigate('auth'); state.authMode='login'; render();" class="px-9 py-4 rounded-2xl bg-slate-100 hover:bg-slate-200 text-slate-900 font-extrabold text-sm transition">
                    Sign In to Portal
                  </button>
                `}
              </div>

              <div class="mt-16 max-w-4xl mx-auto rounded-3xl bg-slate-900 p-4 sm:p-8 shadow-2xl border border-slate-800 text-left glow-indigo">
                <div class="flex items-center justify-between pb-4 border-b border-slate-800 text-xs">
                  <div class="flex items-center gap-2">
                    <span class="w-3 h-3 rounded-full bg-rose-500"></span>
                    <span class="w-3 h-3 rounded-full bg-amber-500"></span>
                    <span class="w-3 h-3 rounded-full bg-emerald-500"></span>
                    <span class="ml-2 font-mono text-slate-400">engine_output // ean13_vector.pdf</span>
                  </div>
                  <span class="px-2.5 py-1 rounded bg-indigo-950 text-indigo-300 font-mono text-[10px] font-bold">100% Vector Math</span>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6 pt-6 items-center">
                  <div class="space-y-4">
                    <div class="p-4 bg-slate-800/80 rounded-2xl border border-slate-700">
                      <span class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Country Code Prefix</span>
                      <div class="font-mono text-sm font-bold text-white mt-1">890 (India National GS1 Standard)</div>
                    </div>
                    <div class="p-4 bg-slate-800/80 rounded-2xl border border-slate-700">
                      <span class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Quiet Zone Preservation</span>
                      <div class="font-mono text-sm font-bold text-emerald-400 mt-1">Strict 4.0mm Dynamic Bounds</div>
                    </div>
                    <div class="p-4 bg-slate-800/80 rounded-2xl border border-slate-700">
                      <span class="text-[10px] font-bold uppercase tracking-wider text-slate-400">Checksum Algorithm</span>
                      <div class="font-mono text-sm font-bold text-indigo-400 mt-1">Mod-10 Odd/Even Sum Synchronized</div>
                    </div>
                  </div>
                  <div class="bg-white p-6 rounded-2xl shadow-inner flex flex-col items-center justify-center min-h-[220px]">
                    <div class="text-[10px] font-bold tracking-widest text-slate-400 uppercase">Live Sample Generated</div>
                    <div class="w-64 h-32 barcode-svg-container my-3 mx-auto flex items-center justify-center">
                      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 45" width="100%" height="100%" style="display:block; margin:auto;">
                        <rect x="5" y="5" width="2" height="30" fill="#000"/>
                        <rect x="9" y="5" width="1" height="30" fill="#000"/>
                        <rect x="13" y="5" width="3" height="30" fill="#000"/>
                        <rect x="18" y="5" width="1" height="30" fill="#000"/>
                        <rect x="22" y="5" width="4" height="30" fill="#000"/>
                        <rect x="28" y="5" width="2" height="30" fill="#000"/>
                        <rect x="33" y="5" width="1" height="30" fill="#000"/>
                        <rect x="37" y="5" width="3" height="30" fill="#000"/>
                        <rect x="42" y="5" width="2" height="30" fill="#000"/>
                        <rect x="47" y="5" width="1" height="30" fill="#000"/>
                        <rect x="51" y="5" width="2" height="30" fill="#000"/>
                        <rect x="56" y="5" width="3" height="30" fill="#000"/>
                        <rect x="62" y="5" width="1" height="30" fill="#000"/>
                        <rect x="66" y="5" width="4" height="30" fill="#000"/>
                        <rect x="73" y="5" width="2" height="30" fill="#000"/>
                        <rect x="78" y="5" width="1" height="30" fill="#000"/>
                        <rect x="82" y="5" width="3" height="30" fill="#000"/>
                        <rect x="88" y="5" width="2" height="30" fill="#000"/>
                        <rect x="93" y="5" width="2" height="30" fill="#000"/>
                        <text x="50" y="42" font-family="monospace" font-size="6" text-anchor="middle" font-weight="bold">8 904321 009852</text>
                      </svg>
                    </div>
                    <span class="text-[11px] font-mono text-indigo-700 font-bold text-center">100% Vector Scalability (0 DPI Loss)</span>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section id="analytics" class="py-24 bg-slate-50 border-b border-slate-200">
            <div class="max-w-7xl mx-auto px-4 sm:px-6">
              <div class="text-center max-w-3xl mx-auto mb-16">
                <span class="text-xs uppercase font-extrabold tracking-widest text-indigo-600 block mb-2">Performance Analytics</span>
                <h2 class="text-3xl sm:text-5xl font-extrabold text-slate-900 tracking-tight">Engineered for Flawless Scans</h2>
              </div>

              <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                <div class="bg-white p-8 rounded-3xl border border-slate-200 shadow-sm flex flex-col justify-between">
                  <div>
                    <span class="text-xs uppercase tracking-wider font-extrabold text-indigo-600">Scan Reliability Analysis</span>
                    <h3 class="text-lg font-bold text-slate-900 mt-1">Optical Scan Read Rate vs. Label Dimension</h3>
                  </div>
                  <div class="h-72 w-full relative">
                    <canvas id="landingScanEfficiencyChart"></canvas>
                  </div>
                </div>

                <div class="bg-white p-8 rounded-3xl border border-slate-200 shadow-sm flex flex-col justify-between">
                  <div>
                    <span class="text-xs uppercase tracking-wider font-extrabold text-emerald-600">Material Efficiency</span>
                    <h3 class="text-lg font-bold text-slate-900 mt-1">Cost Reductions: Legacy vs. LabelForge</h3>
                  </div>
                  <div class="h-72 w-full relative">
                    <canvas id="landingCostSavingsChart"></canvas>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section id="calculator" class="py-24 bg-white border-b border-slate-200">
            <div class="max-w-5xl mx-auto px-4 sm:px-6">
              <div class="bg-slate-950 text-white rounded-3xl p-8 sm:p-14 shadow-2xl border border-slate-800">
                <div class="max-w-2xl mb-10">
                  <span class="text-xs uppercase tracking-widest text-indigo-400 font-extrabold">Instant Savings Estimator</span>
                  <h2 class="text-3xl sm:text-4xl font-extrabold mt-2">Calculate Your Monthly Operational ROI</h2>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-3 gap-8 items-center">
                  <div class="md:col-span-2 space-y-6">
                    <div>
                      <div class="flex justify-between items-center mb-2">
                        <label class="text-xs uppercase tracking-wider text-slate-400 font-bold">Monthly Product Label Volume</label>
                        <span id="calcVolDisplay" class="font-mono text-base font-extrabold text-indigo-400">1,000 units / month</span>
                      </div>
                      <input type="range" min="100" max="10000" step="100" value="1000" oninput="updateCalcVolume(this.value)" class="w-full h-2.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
                    </div>

                    <div class="grid grid-cols-2 gap-4 pt-2">
                      <div class="p-4 bg-slate-900/90 rounded-2xl border border-slate-800">
                        <span class="text-[10px] uppercase font-bold text-slate-400 block">Paper Roll Waste</span>
                        <span class="text-emerald-400 font-extrabold text-lg mt-1 block">Reduced by 74%</span>
                      </div>
                      <div class="p-4 bg-slate-900/90 rounded-2xl border border-slate-800">
                        <span class="text-[10px] uppercase font-bold text-slate-400 block">Operator Hours Saved</span>
                        <span id="calcTimeDisplay" class="text-indigo-400 font-extrabold text-lg mt-1 block">15 hrs / mo</span>
                      </div>
                    </div>
                  </div>

                  <div class="p-6 bg-gradient-to-b from-indigo-900/40 to-slate-900 rounded-2xl border border-indigo-500/30 text-center">
                    <span class="text-xs uppercase font-extrabold tracking-wider text-indigo-300">Net Estimated Savings</span>
                    <div id="calcSavDisplay" class="text-4xl font-extrabold text-white my-3">₹4,900</div>
                    <button onclick="navigate('plans')" class="w-full py-3 bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-xs rounded-xl shadow-lg transition">
                      Claim Your Plan &rarr;
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <!-- FOUNDER & LEADERSHIP SECTION -->
          <section id="founder" class="py-24 bg-white border-b border-slate-200">
            <div class="max-w-6xl mx-auto px-4 sm:px-6">
              <div class="text-center max-w-2xl mx-auto mb-12">
                <span class="text-xs uppercase font-extrabold tracking-widest text-indigo-600 block mb-2">Executive Leadership</span>
                <h2 class="text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">The Vision Behind LabelForge</h2>
              </div>

              <div class="grid grid-cols-1 lg:grid-cols-2 gap-8 items-stretch">
                <!-- SIDHARTH CHOPRA (CEO) -->
                <div class="bg-gradient-to-br from-indigo-50 via-slate-50 to-white rounded-3xl border border-indigo-100 p-8 shadow-sm flex flex-col justify-between">
                  <div>
                    <div class="flex items-center gap-4 mb-6">
                      <div class="w-16 h-16 rounded-2xl bg-gradient-to-tr from-indigo-600 to-violet-600 text-white flex-shrink-0 flex items-center justify-center font-extrabold text-2xl shadow-lg shadow-indigo-200">
                        SC
                      </div>
                      <div>
                        <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-indigo-100 text-indigo-800 text-[10px] font-extrabold uppercase">
                          Executive Leadership
                        </span>
                        <h3 class="text-xl font-extrabold text-slate-900 mt-1">Sidharth Chopra</h3>
                        <p class="text-xs font-semibold text-indigo-600">Chief Executive Officer (CEO)</p>
                      </div>
                    </div>
                    <p class="text-xs text-slate-700 leading-relaxed">
                      Sidharth Chopra is the dynamic 14-year-old CEO driving LabelForge’s product architecture and technical innovation. Combining core software engineering with automated retail workflows, Sidharth engineered the high-precision vector engine to eliminate barcode read failures and bring frictionless catalog labeling to modern digital commerce.
                    </p>
                  </div>
                  <div class="pt-6 border-t border-slate-200/60 mt-6 text-[11px] font-mono text-slate-400">
                    Chief Executive Officer // LabelForge Pro Suite
                  </div>
                </div>

                <!-- SUNIL CHOPRA (FOUNDER) -->
                <div class="bg-gradient-to-br from-slate-50 via-indigo-50/40 to-white rounded-3xl border border-slate-200 p-8 shadow-sm flex flex-col justify-between">
                  <div>
                    <div class="flex items-center gap-4 mb-6">
                      <div class="w-16 h-16 rounded-2xl bg-gradient-to-tr from-slate-800 to-slate-950 text-white flex-shrink-0 flex items-center justify-center font-extrabold text-2xl shadow-lg shadow-slate-300">
                        SC
                      </div>
                      <div>
                        <span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-100 text-emerald-800 text-[10px] font-extrabold uppercase">
                          Founder & Strategist
                        </span>
                        <h3 class="text-xl font-extrabold text-slate-900 mt-1">Sunil Chopra</h3>
                        <p class="text-xs font-semibold text-emerald-600">Founder</p>
                      </div>
                    </div>
                    <p class="text-xs text-slate-700 leading-relaxed">
                      Sunil Chopra is the Founder of LabelForge, providing strategic direction, industry insight, and commercial guidance. With deep expertise across trade, business operations, and enterprise execution, Sunil spearheads the company’s mission to build scalable retail infrastructure and empower businesses with dependable GS1 standard technology.
                    </p>
                  </div>
                  <div class="pt-6 border-t border-slate-200/60 mt-6 text-[11px] font-mono text-slate-400">
                    Founder // Strategic Operations & Commercial Growth
                  </div>
                </div>
              </div>
            </div>
          </section>

          <footer class="bg-slate-950 text-slate-400 py-16 text-xs">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 flex flex-col sm:flex-row justify-between items-center gap-6">
              <div class="flex items-center gap-3">
                <div class="h-8 w-8 rounded-xl bg-indigo-600 text-white flex items-center justify-center font-extrabold text-sm">LF</div>
                <span class="text-white font-extrabold text-base">LabelForge Technologies</span>
              </div>
              <p class="text-center sm:text-right">&copy; 2026 LabelForge Pro Suite. Engineered for Modern E-Commerce & Retail.</p>
            </div>
          </footer>
        </div>
      `;
    }

    function renderAppShell(contentHtml) {
      const plan = getUserPlan();
      const isSuper = isSuperAdmin();
      const storeName = (state.user && state.user.business_name) ? state.user.business_name : 'Retail Store';
      const navItems = [
        ...(isSuper ? [{ key: 'admin_hq', label: 'SaaS Admin HQ', icon: '🛡️' }] : []),
        { key: 'dashboard', label: 'Dashboard', icon: '📊' },
        { key: 'manage_catalog', label: 'Manage Catalog', icon: '📦' },
        { key: 'add_product', label: 'Add Product', icon: '➕' },
        { key: 'barcodes', label: 'Barcode Generator', icon: '🏷️' },
        { key: 'labels', label: 'Label Designer', icon: '📐' },
        { key: 'scanner', label: 'Scan & Verify', icon: '📷' },
        { key: 'settings', label: 'Settings', icon: '⚙️️' }
      ];

      return `
        <div class="flex h-screen overflow-hidden bg-slate-50">
          <aside class="w-64 bg-slate-900 text-slate-300 flex flex-col flex-shrink-0 border-r border-slate-800 z-30">
            <!-- CLICKING HERE NAVIGATES WITHIN DASHBOARD / CONTROL HUB - NEVER TO LANDING PAGE -->
            <div class="p-5 flex items-center gap-3 border-b border-slate-800 cursor-pointer" onclick="navigate('${isSuper ? 'admin_hq' : 'dashboard'}')">
              <div class="h-10 w-10 rounded-2xl ${isSuper ? 'bg-amber-500' : 'bg-indigo-600'} flex items-center justify-center text-white font-extrabold shadow-md text-sm">
                ${isSuper ? 'HQ' : 'LF'}
              </div>
              <div>
                <span class="font-extrabold text-lg text-white tracking-tight">Label<span class="${isSuper ? 'text-amber-400' : 'text-indigo-400'}">Forge</span></span>
                <span class="block text-[10px] text-slate-400 font-semibold tracking-wide uppercase">${isSuper ? 'SaaS Control Hub' : 'Workspace Pro'}</span>
              </div>
            </div>

            <nav class="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
              ${navItems.map(item => `
                <button onclick="navigate('${item.key}')" class="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-xs font-semibold transition ${state.view === item.key ? (item.key === 'admin_hq' ? 'bg-amber-600 text-white shadow-md shadow-amber-600/30' : 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30') : (item.key === 'admin_hq' ? 'text-amber-400 hover:bg-slate-800' : 'hover:bg-slate-800/80 hover:text-white')}">
                  <span class="text-base">${item.icon}</span>
                  <span>${item.label}</span>
                </button>
              `).join('')}
            </nav>

            <div class="p-4 border-t border-slate-800 bg-slate-950/40">
              <div class="flex items-center justify-between">
                <div>
                  <div class="text-xs font-bold text-white truncate max-w-[110px]" title="${storeName}">${storeName}</div>
                  <div class="text-[10px] text-emerald-400 font-semibold uppercase mt-0.5">${isSuper ? '★ SaaS Master Admin' : plan + ' Plan'}</div>
                </div>
                <button onclick="logout()" class="px-2.5 py-1.5 rounded-xl bg-slate-800/90 hover:bg-rose-950/60 hover:text-rose-400 text-slate-400 text-xs font-semibold flex items-center gap-1.5 transition" title="Sign Out">
                  <span>🚪</span>
                  <span>Log Out</span>
                </button>
              </div>
              ${!isSuper ? `
                <div class="mt-3">
                  <button onclick="navigate('plans')" class="w-full py-2 rounded-xl bg-indigo-600/80 hover:bg-indigo-600 text-white text-[11px] font-bold transition shadow-sm">
                    ⚡ Upgrade Plan
                  </button>
                </div>
              ` : `
                <div class="mt-3">
                  <button onclick="navigate('admin_hq')" class="w-full py-2 rounded-xl bg-amber-600/90 hover:bg-amber-600 text-white text-[11px] font-bold transition shadow-sm">
                    🛡 Manage All Customers
                  </button>
                </div>
              `}
            </div>
          </aside>

          <div class="flex-1 flex flex-col min-w-0 overflow-y-auto">
            <header class="h-16 bg-white border-b border-slate-200 px-6 flex items-center justify-between flex-shrink-0">
              <div class="flex items-center gap-3">
                <div class="h-4 w-[1px] bg-slate-200 mx-1"></div>
                <span class="text-xs font-bold text-slate-400 uppercase tracking-widest">Section /</span>
                <span class="text-sm font-bold text-slate-800 capitalize">${state.view.replace('_', ' ')}</span>
              </div>
              <div class="flex items-center gap-3">
                ${isSuper ? `
                  <button onclick="navigate('admin_hq')" class="px-3.5 py-1.5 bg-amber-500 hover:bg-amber-600 text-white rounded-xl text-xs font-bold shadow-sm transition flex items-center gap-1">
                    <span>🛡️</span> SaaS Customers
                  </button>
                ` : ''}
              </div>
            </header>

            <main class="p-6 md:p-8 flex-1">
              ${contentHtml}
            </main>
          </div>
        </div>
      `;
    }

    function renderDashboard() {
      const totalProds = state.products.length;
      const totalInventoryVal = state.products.reduce((acc, p) => acc + (p.mrp || 0), 0);
      const uniqueCats = new Set(state.products.map(p => p.category)).size;

      return `
        <div class="max-w-7xl mx-auto space-y-8">
          <div>
            <h1 class="text-2xl font-bold text-slate-900">Commercial Dashboard</h1>
            <p class="text-xs text-slate-500 mt-1">Overview of catalog status, barcode distribution, and print metrics.</p>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-4 gap-6">
            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm cursor-pointer hover:border-indigo-300 transition" onclick="navigate('manage_catalog')">
              <span class="text-slate-400 text-xs font-semibold uppercase">Total Products</span>
              <div class="text-3xl font-extrabold text-slate-900 mt-2">${totalProds}</div>
              <div class="text-xs text-indigo-600 font-semibold mt-3">View full catalog &rarr;</div>
            </div>
            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm">
              <span class="text-slate-400 text-xs font-semibold uppercase">Catalog MRP Value</span>
              <div class="text-3xl font-extrabold text-emerald-600 mt-2">₹${totalInventoryVal.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
              <div class="text-xs text-slate-400 mt-3 font-medium">Accumulated shelf value</div>
            </div>
            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm">
              <span class="text-slate-400 text-xs font-semibold uppercase">Categories</span>
              <div class="text-3xl font-extrabold text-slate-900 mt-2">${uniqueCats} Active</div>
              <div class="text-xs text-slate-400 mt-3 font-medium">Segmented merchandise</div>
            </div>
            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm cursor-pointer hover:border-emerald-300 transition" onclick="navigate('scanner')">
              <span class="text-slate-400 text-xs font-semibold uppercase">Scanner System</span>
              <div class="text-3xl font-extrabold text-indigo-600 mt-2">Active</div>
              <div class="text-xs text-emerald-600 font-semibold mt-3">Open live camera scanner &rarr;</div>
            </div>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
            <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm">
              <h3 class="text-sm font-bold text-slate-900 mb-1">Catalog Category Distribution</h3>
              <p class="text-xs text-slate-400 mb-4">Breakdown of inventory by registered classification.</p>
              <div class="h-64 relative">
                <canvas id="categoryPieChart"></canvas>
              </div>
            </div>

            <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm">
              <h3 class="text-sm font-bold text-slate-900 mb-1">Catalog Item Distribution</h3>
              <p class="text-xs text-slate-400 mb-4">Breakdown of products registered and ready.</p>
              <div class="h-64 relative">
                <canvas id="volumeBarChart"></canvas>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function renderManageCatalog() {
      return `
        <div class="max-w-7xl mx-auto space-y-6">
          <div class="flex items-center justify-between">
            <div>
              <h1 class="text-2xl font-bold text-slate-900">Manage Catalog</h1>
              <p class="text-xs text-slate-500 mt-0.5">Edit, delete, and manage all registered products and locked EAN barcodes.</p>
            </div>
          </div>

          <div class="bg-white rounded-3xl border border-slate-200 overflow-hidden shadow-sm">
            <div class="overflow-x-auto">
              <table class="w-full text-left text-sm">
                <thead class="bg-slate-50 border-b border-slate-200 text-xs text-slate-500 uppercase font-semibold">
                  <tr>
                    <th class="px-6 py-4">SKU Code</th>
                    <th class="px-6 py-4">Product Name</th>
                    <th class="px-6 py-4">Barcode No.</th>
                    <th class="px-6 py-4">Category</th>
                    <th class="px-6 py-4">MRP</th>
                    <th class="px-6 py-4 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100">
                  ${state.products.length === 0 ? `
                    <tr><td colspan="6" class="px-6 py-12 text-center text-slate-400 text-xs font-medium">Your catalog is currently empty. Click "+ Add Product" to register your first item.</td></tr>
                  ` : state.products.map(p => `
                    <tr class="hover:bg-slate-50/70 transition">
                      <td class="px-6 py-4 font-mono text-xs font-semibold text-slate-600">${p.sku}</td>
                      <td class="px-6 py-4 font-bold text-slate-900">${p.name}</td>
                      <td class="px-6 py-4 font-mono text-xs text-indigo-700 font-bold tracking-wider">${p.barcode}</td>
                      <td class="px-6 py-4 text-xs font-semibold text-slate-600"><span class="px-2.5 py-1 bg-slate-100 rounded-lg">${p.category}</span></td>
                      <td class="px-6 py-4 font-semibold text-slate-900">₹${p.mrp.toFixed(2)}</td>
                      <td class="px-6 py-4 text-right space-x-2">
                        <button onclick="openEditModal(${p.id})" class="px-3 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-bold rounded-lg transition">Edit</button>
                        <button onclick="deleteProductPrompt(${p.id}, '${p.name}')" class="px-3 py-1 bg-rose-50 hover:bg-rose-100 text-rose-600 text-xs font-bold rounded-lg transition">Delete</button>
                      </td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      `;
    }

    function renderAddProduct() {
      const bizPrefix = state.user && state.user.sku_prefix ? state.user.sku_prefix : 'PRD';
      const bizPadding = state.user && state.user.sku_padding ? state.user.sku_padding : 6;
      const nextSku = `${bizPrefix}-${(state.products.length + 1).toString().padStart(bizPadding, '0')}`;

      const catOptions = (state.categories || []).map(c => {
        const val = typeof c === 'string' ? c : c.name;
        return `<option value="${val}" ${state.addProductDraft.category === val ? 'selected' : ''}>${val}</option>`;
      }).join('');

      return `
        <div class="max-w-2xl mx-auto space-y-6">
          <div>
            <h1 class="text-2xl font-bold text-slate-900">Add New Product</h1>
            <p class="text-xs text-slate-500 mt-0.5">The platform automatically generates a unique 13-digit EAN barcode and structured SKU.</p>
          </div>

          <div class="bg-white p-8 rounded-3xl border border-slate-200 shadow-sm">
            <form onsubmit="handleAddProductForm(event)" class="space-y-5">
              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1.5">Product Name</label>
                <input id="newProdName" type="text" required value="${state.addProductDraft.name}" oninput="state.addProductDraft.name = this.value" placeholder="e.g. Pure Cotton Kurti Pant Set" class="w-full px-4 py-3 border border-slate-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none">
              </div>

              <div class="grid grid-cols-2 gap-4">
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1.5">MRP (₹)</label>
                  <input id="newProdMrp" type="number" step="0.01" required value="${state.addProductDraft.mrp}" oninput="state.addProductDraft.mrp = this.value" placeholder="1499.00" class="w-full px-4 py-3 border border-slate-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none">
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1.5">Category</label>
                  <div class="flex gap-2">
                    <select id="newProdCat" onchange="state.addProductDraft.category = this.value" class="flex-1 px-4 py-3 border border-slate-200 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none bg-white">
                      ${catOptions}
                    </select>
                    <button type="button" onclick="openQuickCategoryModal()" class="px-3.5 py-2 border border-slate-200 rounded-xl text-sm font-bold text-indigo-600 hover:bg-indigo-50 transition" title="Add New Category Directly">
                      +
                    </button>
                  </div>
                </div>
              </div>

              <div class="p-4 bg-slate-50 rounded-2xl border border-slate-200 space-y-2">
                <span class="block text-xs font-bold text-slate-700 uppercase tracking-wider">Automated System Assignments</span>
                <div class="flex items-center justify-between text-xs text-slate-600">
                  <span>Assigned SKU Format:</span>
                  <span class="font-mono font-bold text-indigo-700">${nextSku} (Auto-calculated)</span>
                </div>
                <div class="flex items-center justify-between text-xs text-slate-600">
                  <span>Assigned Barcode:</span>
                  <span class="font-mono font-bold text-emerald-700">13-Digit GS1 EAN Unique (Locked on Save)</span>
                </div>
              </div>

              <div class="pt-3">
                <button type="submit" class="w-full py-3.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold shadow-lg shadow-indigo-100 transition">
                  Save Product to Catalog
                </button>
              </div>
            </form>
          </div>
        </div>
      `;
    }

    function renderBarcodeStudioView() {
      ensureProductSelected();
      const p = state.products.find(x => x.id === state.selectedProductId);

      return `
        <div class="max-w-7xl mx-auto space-y-6">
          <div class="flex items-center justify-between">
            <div>
              <h1 class="text-2xl font-bold text-slate-900">Barcode Generator</h1>
              <p class="text-xs text-slate-500">Unique, locked barcodes with multi-symbology rendering (EAN-13, Code-128, QR Code).</p>
            </div>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
            <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm space-y-5">
              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1.5">Select Catalog Product</label>
                ${state.products.length === 0 ? `
                  <div class="p-3 bg-slate-50 border border-slate-200 rounded-xl text-xs text-slate-500 text-center">
                    No products added yet. <button onclick="navigate('add_product')" class="text-indigo-600 font-bold underline">Add a product first</button>
                  </div>
                ` : `
                  <select onchange="selectBarcodeProduct(this.value)" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none">
                    ${state.products.map(pr => `<option value="${pr.id}" ${state.selectedProductId === pr.id ? 'selected' : ''}>${pr.name} (${pr.barcode})</option>`).join('')}
                  </select>
                `}
              </div>

              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1.5">System Barcode No. (Locked)</label>
                <input id="lockedBarcodeVal" type="text" value="${p ? p.barcode : 'NO PRODUCT REGISTERED'}" disabled readonly class="w-full px-3.5 py-2.5 font-mono text-center rounded-xl border border-slate-200 text-sm bg-slate-100 text-slate-500 font-bold tracking-widest cursor-not-allowed">
                <span class="block text-[11px] text-slate-400 mt-1 text-center">Generated exclusively by the platform. Cannot be edited.</span>
              </div>

              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1.5">Barcode Type</label>
                <select onchange="state.barcodeSymbology = this.value; renderBarcodeStudio();" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm bg-white font-medium focus:ring-2 focus:ring-indigo-500 focus:outline-none">
                  <option value="ean13" ${state.barcodeSymbology === 'ean13' ? 'selected' : ''}>EAN-13 (Standard Retail Scan)</option>
                  <option value="code128" ${state.barcodeSymbology === 'code128' ? 'selected' : ''}>Code-128 (Logistics & Packaging)</option>
                  <option value="qrcode" ${state.barcodeSymbology === 'qrcode' ? 'selected' : ''}>QR Code (Direct Mobile Camera Verification URL)</option>
                </select>
              </div>

              <div id="bcStudioErr" class="text-xs text-rose-500 font-semibold text-center"></div>

              <div class="pt-4 border-t border-slate-100">
                <button onclick="downloadBarcodePdf()" class="w-full py-3.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold shadow-md transition flex items-center justify-center gap-2">
                  Download Barcode Vector PDF
                </button>
              </div>
            </div>

            <div class="lg:col-span-2 bg-white p-8 rounded-3xl border border-slate-200 shadow-sm flex flex-col items-center justify-center min-h-[380px] relative text-center">
              <div class="p-8 bg-white rounded-3xl border-2 border-slate-200 shadow-sm flex items-center justify-center barcode-svg-container mx-auto" style="width: 340px; height: 170px;">
                <div id="bcStudioBox" class="w-full h-full flex items-center justify-center text-center">
                  ${state.barcodeSvg || '<div class="text-slate-400 text-xs text-center">No product selected yet. Add a product first.</div>'}
                </div>
              </div>

              <div class="mt-6 text-center w-full">
                <div class="text-xs font-bold text-slate-800">${p ? p.name : 'No Product Selected'}</div>
                <div class="text-[11px] font-mono text-indigo-600 mt-0.5">${p ? 'Encoded: ' + (state.barcodeVal || p.barcode) : 'Catalog Empty'}</div>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function renderLabelDesigner() {
      ensureProductSelected();
      const p = state.products.find(x => x.id === state.selectedProductId);
      const plan = getUserPlan();

      return `
        <div class="max-w-7xl mx-auto space-y-6">
          <div class="flex items-center justify-between">
            <div>
              <h1 class="text-2xl font-bold text-slate-900">Label Designer</h1>
              <p class="text-xs text-slate-500">Auto-scaling elements that resize proportionally with label dimension reductions.</p>
            </div>
          </div>

          <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
            <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm space-y-5">
              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1.5">Select Product</label>
                ${state.products.length === 0 ? `
                  <div class="p-3 bg-slate-50 border border-slate-200 rounded-xl text-xs text-slate-500 text-center">
                    No products added yet. <button onclick="navigate('add_product')" class="text-indigo-600 font-bold underline">Add a product first</button>
                  </div>
                ` : `
                  <select onchange="selectProduct(this.value)" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none">
                    ${state.products.map(pr => `<option value="${pr.id}" ${state.selectedProductId === pr.id ? 'selected' : ''}>${pr.name}</option>`).join('')}
                  </select>
                `}
              </div>

              <div class="p-4 bg-slate-50 rounded-2xl border border-slate-200 space-y-3">
                <span class="block text-xs font-bold text-slate-700 uppercase tracking-wider">Physical Dimensions (mm)</span>
                <div class="grid grid-cols-2 gap-3">
                  <div>
                    <label class="block text-[11px] font-semibold text-slate-500 mb-1">Width (mm)</label>
                    <input id="dimWidthInput" type="number" min="25" max="150" value="${state.dimWidth}" oninput="updateLabelDimensionsFromInput('w', this.value)" class="w-full px-3 py-2 rounded-xl border border-slate-200 text-sm font-semibold focus:ring-2 focus:ring-indigo-500">
                  </div>
                  <div>
                    <label class="block text-[11px] font-semibold text-slate-500 mb-1">Height (mm)</label>
                    <input id="dimHeightInput" type="number" min="15" max="120" value="${state.dimHeight}" oninput="updateLabelDimensionsFromInput('h', this.value)" class="w-full px-3 py-2 rounded-xl border border-slate-200 text-sm font-semibold focus:ring-2 focus:ring-indigo-500">
                  </div>
                </div>
                <div class="flex gap-2 pt-1 justify-center">
                  <button type="button" onclick="setPresetDimensions(76, 50)" class="px-2.5 py-1 text-[11px] font-semibold rounded-lg bg-white border border-slate-200 text-slate-600 hover:bg-slate-100">76×50mm</button>
                  <button type="button" onclick="setPresetDimensions(50, 30)" class="px-2.5 py-1 text-[11px] font-semibold rounded-lg bg-white border border-slate-200 text-slate-600 hover:bg-slate-100">50×30mm</button>
                  <button type="button" onclick="setPresetDimensions(40, 25)" class="px-2.5 py-1 text-[11px] font-semibold rounded-lg bg-white border border-slate-200 text-slate-600 hover:bg-slate-100">40×25mm</button>
                </div>
              </div>

              <div class="pt-2">
                <span class="block text-xs font-bold text-slate-700 mb-2 uppercase tracking-wider">Visible Elements</span>
                <div class="space-y-2 text-xs text-slate-600">
                  <label class="flex items-center gap-2"><input type="checkbox" ${state.options.showBiz ? 'checked' : ''} onchange="state.options.showBiz=this.checked; updateLabelPreviewDOM();" class="rounded text-indigo-600"> Brand / Business Name</label>
                  <label class="flex items-center gap-2"><input type="checkbox" ${state.options.showMrp ? 'checked' : ''} onchange="state.options.showMrp=this.checked; updateLabelPreviewDOM();" class="rounded text-indigo-600"> MRP & Retail Pricing</label>
                  <label class="flex items-center gap-2"><input type="checkbox" ${state.options.showSku ? 'checked' : ''} onchange="state.options.showSku=this.checked; updateLabelPreviewDOM();" class="rounded text-indigo-600"> SKU Code</label>
                  <label class="flex items-center gap-2"><input type="checkbox" ${state.options.showBatch ? 'checked' : ''} onchange="state.options.showBatch=this.checked; updateLabelPreviewDOM();" class="rounded text-indigo-600"> Batch Identifier</label>
                  <label class="flex items-center gap-2"><input type="checkbox" ${state.options.showBarcode ? 'checked' : ''} onchange="state.options.showBarcode=this.checked; updateLabelPreviewDOM();" class="rounded text-indigo-600"> High-Scan Barcode</label>
                </div>
              </div>

              <div class="pt-4 border-t border-slate-100">
                <button onclick="downloadLabelPdf()" class="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold shadow-md transition">
                  Download Single Label PDF
                </button>
              </div>

              <div class="pt-4 border-t border-slate-100 bg-slate-50 -mx-6 sm:-mx-8 -mb-6 sm:-mb-8 p-6 rounded-b-3xl">
                <div class="flex items-center justify-between mb-2">
                  <span class="text-xs font-bold text-slate-800">Bulk Sheet Auto-Fit</span>
                  <span class="text-[10px] ${plan === 'free' ? 'text-amber-600 font-bold' : 'text-indigo-600 font-semibold'} uppercase">
                    ${plan === 'free' ? 'PRO FEATURE' : 'AUTO-SCALED'}
                  </span>
                </div>
                <div class="flex gap-2">
                  <input type="number" min="1" max="60" value="${state.bulkCount}" oninput="state.bulkCount=this.value" class="w-20 px-3 py-2 rounded-xl border border-slate-200 text-sm font-semibold">
                  <button onclick="downloadBulkSingleSheet()" class="flex-1 py-2 bg-slate-900 hover:bg-black text-white text-xs font-bold rounded-xl transition">
                    Download Sheet PDF
                  </button>
                </div>
              </div>
            </div>

            <div class="lg:col-span-2 bg-white p-8 rounded-3xl border border-slate-200 shadow-sm flex flex-col items-center justify-center min-h-[440px] relative text-center">
              <div class="p-4 transition-all w-full flex items-center justify-center" id="labelPreviewSlot">
                ${generateDynamicLabelMarkup(p, false)}
              </div>

              <div class="flex items-center justify-center gap-4 text-xs text-slate-400 mt-6 font-mono text-center">
                <span id="targetDimDisplay">Target: ${state.dimWidth}mm × ${state.dimHeight}mm</span>
                <span>•</span>
                <span class="text-emerald-600 font-semibold">Centered Proportional Scaler</span>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function renderScanner() {
      return `
        <div class="max-w-3xl mx-auto space-y-6">
          <div class="text-center">
            <h1 class="text-2xl font-bold text-slate-900">Scan & Verify Barcode</h1>
            <p class="text-xs text-slate-500 mt-1">Scan using webcam or input any 13-digit EAN code / Verification URL to look up inventory data.</p>
          </div>

          <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm space-y-6">
            <div class="relative bg-slate-950 rounded-3xl overflow-hidden min-h-[280px] flex flex-col items-center justify-center p-4 border border-slate-800">
              <div id="reader" class="w-full"></div>
              ${state.isScanning ? `
                <div class="absolute inset-x-8 top-10 h-0.5 bg-rose-500 shadow-[0_0_12px_#f43f5e] scanner-laser pointer-events-none"></div>
              ` : ''}

              <button onclick="startLiveCameraScanner()" class="mt-4 px-6 py-3 ${state.isScanning ? 'bg-rose-600 hover:bg-rose-700' : 'bg-indigo-600 hover:bg-indigo-700'} text-white rounded-xl text-xs font-bold shadow-lg transition flex items-center gap-2 z-10">
                ${state.isScanning ? 'Stop Camera' : 'Turn On Camera Scanner'}
              </button>
            </div>

            <div>
              <label class="block text-xs font-bold uppercase tracking-wider text-slate-600 mb-2">Manual 13-Digit Entry / Barcode Gun Input</label>
              <div class="flex gap-2">
                <input type="text" id="scanInput" autofocus value="${state.scannerInput}" oninput="state.scannerInput = this.value" onkeydown="if(event.key==='Enter') performScanLookup();" placeholder="Scan barcode with gun or type manually..." class="flex-1 px-4 py-3 border border-slate-200 rounded-xl text-sm font-mono font-bold focus:outline-none focus:ring-2 focus:ring-indigo-500">
                <button onclick="performScanLookup()" class="px-6 py-3 bg-slate-900 hover:bg-black text-white text-xs font-bold rounded-xl transition shadow-md">
                  Verify Code
                </button>
              </div>
            </div>

            ${state.scannerError ? `
              <div class="p-4 bg-rose-50 text-rose-700 border border-rose-200 rounded-2xl text-xs font-semibold flex items-center gap-2">
                <span>⚠️ ${state.scannerError}</span>
              </div>
            ` : ''}

            ${state.scannerResult ? `
              <div class="p-6 bg-emerald-50/60 border border-emerald-200 rounded-2xl space-y-4">
                <div class="flex items-center justify-between pb-3 border-b border-emerald-200/60">
                  <span class="text-xs uppercase tracking-wider font-extrabold text-emerald-800 bg-emerald-200/60 px-3 py-1 rounded-full flex items-center gap-1.5">
                    <span class="w-2 h-2 rounded-full bg-emerald-600"></span> Verified Catalog Match
                  </span>
                  <span class="font-mono text-base font-extrabold text-emerald-950">${state.scannerResult.barcode}</span>
                </div>

                <div class="text-center sm:text-left">
                  <div class="text-[11px] font-bold text-slate-500 uppercase tracking-widest">${state.scannerResult.brand_name}</div>
                  <h3 class="text-xl font-extrabold text-slate-900 mt-0.5">${state.scannerResult.product_name}</h3>
                </div>

                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                  <div class="p-3 bg-white rounded-xl border border-slate-200 text-center">
                    <span class="text-slate-400 block text-[10px] uppercase font-bold">MRP</span>
                    <span class="font-extrabold text-indigo-700 text-base">${state.scannerResult.formatted_mrp}</span>
                  </div>
                  <div class="p-3 bg-white rounded-xl border border-slate-200 text-center">
                    <span class="text-slate-400 block text-[10px] uppercase font-bold">SKU</span>
                    <span class="font-mono font-bold text-slate-800">${state.scannerResult.sku}</span>
                  </div>
                  <div class="p-3 bg-white rounded-xl border border-slate-200 text-center">
                    <span class="text-slate-400 block text-[10px] uppercase font-bold">Category</span>
                    <span class="font-bold text-slate-800">${state.scannerResult.category}</span>
                  </div>
                  <div class="p-3 bg-white rounded-xl border border-slate-200 text-center">
                    <span class="text-slate-400 block text-[10px] uppercase font-bold">Batch</span>
                    <span class="font-mono font-bold text-slate-800">${state.scannerResult.batch_number}</span>
                  </div>
                </div>
              </div>
            ` : ''}
          </div>
        </div>
      `;
    }

    function renderSettings() {
      const bizName = state.user && state.user.business_name ? state.user.business_name : 'LabelForge Studios';
      const prefix = state.user && state.user.sku_prefix ? state.user.sku_prefix : 'PRD';
      const padding = state.user && state.user.sku_padding ? state.user.sku_padding : 6;

      return `
        <div class="max-w-4xl mx-auto space-y-8">
          <div>
            <h1 class="text-2xl font-bold text-slate-900">Workspace Settings</h1>
            <p class="text-xs text-slate-500 mt-1">Configure company identity, product categories, SKU generator, and security.</p>
          </div>

          <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm space-y-5">
            <div>
              <h3 class="font-bold text-slate-900 text-sm">Product Categories</h3>
              <p class="text-xs text-slate-400 mt-0.5">Manage custom categories for your catalog tagging and labeling.</p>
            </div>

            <form onsubmit="handleAddCategory(event)" class="flex gap-2">
              <input id="newCategoryNameInput" type="text" placeholder="Enter new category (e.g. Traditional Wear, Toys)" required class="flex-1 px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
              <button type="submit" class="px-5 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold shadow-sm transition">
                + Add Category
              </button>
            </form>

            <div class="pt-2">
              <div class="flex flex-wrap gap-2">
                ${(state.categories || []).map(c => {
                  const catName = typeof c === 'string' ? c : c.name;
                  const catId = typeof c === 'object' ? c.id : null;
                  return `
                    <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-100 text-slate-800 text-xs font-bold border border-slate-200">
                      <span>${catName}</span>
                      ${catId ? `
                        <button type="button" onclick="handleDeleteCategory(${catId}, '${catName}')" class="text-slate-400 hover:text-rose-600 text-sm font-bold ml-1">&times;</button>
                      ` : ''}
                    </span>
                  `;
                }).join('')}
              </div>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
            <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm space-y-5">
              <div>
                <h3 class="font-bold text-slate-900 text-sm">Company & SKU Generator</h3>
              </div>

              <form onsubmit="handleCompanySettingsSave(event)" class="space-y-4">
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1">Company / Brand Name</label>
                  <input id="setBizName" type="text" value="${bizName}" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                </div>

                <div class="grid grid-cols-2 gap-3">
                  <div>
                    <label class="block text-xs font-semibold text-slate-700 mb-1">SKU Prefix</label>
                    <input id="setSkuPrefix" type="text" value="${prefix}" required placeholder="PRD" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm uppercase focus:ring-2 focus:ring-indigo-500">
                  </div>
                  <div>
                    <label class="block text-xs font-semibold text-slate-700 mb-1">SKU Digit Padding</label>
                    <input id="setSkuPadding" type="number" min="3" max="8" value="${padding}" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                  </div>
                </div>

                <div class="p-3 bg-slate-50 rounded-xl text-xs text-slate-500 font-mono text-center">
                  Sample Result: <span class="font-bold text-indigo-600">${prefix}-${'1'.padStart(padding, '0')}</span>
                </div>

                <button type="submit" class="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold shadow-md transition">
                  Save Company & SKU Setup
                </button>
              </form>
            </div>

            <div class="bg-white p-6 sm:p-8 rounded-3xl border border-slate-200 shadow-sm space-y-5">
              <div>
                <h3 class="font-bold text-slate-900 text-sm">Security & Password</h3>
                <p class="text-xs text-slate-400 mt-0.5">Update credentials for your user account.</p>
              </div>

              <form onsubmit="handlePasswordChange(event)" class="space-y-4">
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1">Current Password</label>
                  <input id="curPassword" type="password" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1">New Password</label>
                  <input id="newPassword" type="password" minlength="6" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1">Confirm New Password</label>
                  <input id="confirmPassword" type="password" minlength="6" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                </div>

                <button type="submit" class="w-full py-3 bg-slate-900 hover:bg-black text-white rounded-xl text-xs font-bold shadow-md transition">
                  Update Password
                </button>
              </form>
            </div>
          </div>
        </div>
      `;
    }

    function renderAuth() {
      const isLogin = state.authMode === 'login';
      return `
        <div class="min-h-screen flex flex-col items-center justify-center px-4 py-12 bg-slate-100">
          <!-- AUTH LOGO HEADER: CLICKING GOES DIRECTLY TO LANDING PAGE -->
          <div class="flex items-center gap-3 cursor-pointer mb-8" onclick="navigate('landing')">
            <div class="h-11 w-11 rounded-2xl bg-gradient-to-tr from-indigo-600 via-indigo-700 to-violet-600 flex items-center justify-center text-white shadow-lg shadow-indigo-300 font-extrabold text-lg">LF</div>
            <div>
              <span class="font-extrabold text-2xl text-slate-900 tracking-tight leading-none block">Label<span class="text-indigo-600">Forge</span></span>
              <span class="text-[9px] uppercase tracking-widest text-slate-400 font-bold">Enterprise GS1 Engine</span>
            </div>
          </div>

          <div class="w-full max-w-md bg-white p-8 rounded-3xl border border-slate-200 shadow-xl">
            <div class="text-center mb-6">
              <h2 class="text-2xl font-bold text-slate-900">${isLogin ? 'Sign In to Workspace' : 'Create Business Account'}</h2>
              <p class="text-xs text-slate-500 mt-1">Access your catalog, barcode generator & labels.</p>
            </div>

            ${isLogin ? `
              <form onsubmit="handleLogin(event)" class="space-y-4">
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1">Email</label>
                  <input id="logEmail" type="email" required value="${DEFAULT_ADMIN_EMAIL}" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                </div>
                <div>
                  <label class="block text-xs font-semibold text-slate-700 mb-1">Password</label>
                  <input id="logPass" type="password" required value="${DEFAULT_ADMIN_PASS}" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                </div>
                <button type="submit" class="w-full py-3 rounded-xl bg-indigo-600 text-white text-sm font-bold hover:bg-indigo-700 transition shadow-md shadow-indigo-100">Sign In</button>
              </form>
            ` : `
              <form onsubmit="handleRegister(event)" class="space-y-3">
                <input oninput="state.authData.full_name = this.value" placeholder="Full Name" type="text" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                <input oninput="state.authData.business_name = this.value" placeholder="Business / Brand Name" type="text" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                <input oninput="state.authData.email = this.value" placeholder="Email" type="email" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                <input oninput="state.authData.password = this.value" placeholder="Password" type="password" required class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-sm focus:ring-2 focus:ring-indigo-500">
                <button type="submit" class="w-full py-3 rounded-xl bg-indigo-600 text-white text-sm font-bold hover:bg-indigo-700 mt-2 transition shadow-md shadow-indigo-100">Register</button>
              </form>
            `}

            <div class="mt-6 text-center text-xs text-slate-500">
              ${isLogin ? `Don't have an account? <a href="javascript:void(0)" onclick="state.authMode='register'; render();" class="text-indigo-600 font-semibold">Sign Up</a>` : `Already registered? <a href="javascript:void(0)" onclick="state.authMode='login'; render();" class="text-indigo-600 font-semibold">Sign In</a>`}
            </div>
          </div>
        </div>
      `;
    }

    function renderPlans() {
      const currentPlan = getUserPlan();

      return `
        ${renderPublicNavbar()}
        <div class="max-w-7xl mx-auto px-4 sm:px-6 py-12">
          <div class="text-center max-w-2xl mx-auto mb-12">
            <h1 class="text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">Commercial Plans</h1>
            <p class="mt-3 text-slate-600 text-sm">Scale your barcode creation and single-sheet bulk printing.</p>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-3 gap-8 max-w-6xl mx-auto">
            <div class="bg-white p-8 rounded-3xl border-2 ${currentPlan === 'free' ? 'border-emerald-500 ring-2 ring-emerald-100 shadow-md' : 'border-slate-200'} flex flex-col justify-between">
              <div>
                <h3 class="font-bold text-lg text-slate-900">Starter Free</h3>
                <div class="mt-6 flex items-baseline">
                  <span class="text-4xl font-extrabold text-slate-900">₹0</span>
                  <span class="text-xs text-slate-500 font-semibold ml-1">/ forever</span>
                </div>
                <ul class="mt-6 space-y-3 text-xs text-slate-600">
                  <li class="flex items-center gap-2">✓ <strong>Max 5 Products</strong> in Catalog</li>
                  <li class="flex items-center gap-2">✓ High-Scan EAN-13 Barcodes</li>
                  <li class="flex items-center gap-2">✓ Single Label PDF Downloads</li>
                  <li class="flex items-center gap-2">✓ Live Optical Scanner Included</li>
                </ul>
              </div>
              <div class="mt-8">
                <button disabled class="w-full py-3 rounded-xl bg-slate-100 text-slate-500 text-xs font-bold uppercase cursor-default">
                  ${currentPlan === 'free' ? 'Active' : 'Free Tier'}
                </button>
              </div>
            </div>

            <div class="bg-indigo-900 text-white p-8 rounded-3xl shadow-xl flex flex-col justify-between border-2 ${currentPlan === 'business' ? 'border-amber-400 ring-4 ring-amber-300/30' : 'border-indigo-800'}">
              <div>
                <h3 class="font-bold text-lg text-white">Business Professional</h3>
                <div class="mt-6 flex items-baseline">
                  <span class="text-4xl font-extrabold text-white">₹799</span>
                  <span class="text-xs text-indigo-300 font-semibold ml-1">/ month</span>
                </div>
                <ul class="mt-6 space-y-3 text-xs text-indigo-100">
                  <li class="flex items-center gap-2">✓ <strong>Up to 100 Products</strong> in Catalog</li>
                  <li class="flex items-center gap-2">✓ <strong>Bulk Single-Sheet Optimizer</strong> (Up to 30/sheet)</li>
                  <li class="flex items-center gap-2">✓ Code-128, EAN-13 & QR Studio</li>
                </ul>
              </div>
              <div class="mt-8">
                ${currentPlan === 'business' ? `
                  <button disabled class="w-full py-3 rounded-xl bg-indigo-800 text-indigo-300 text-xs font-bold uppercase cursor-default">Active</button>
                ` : `
                  <button onclick="startUpgrade('business', 799)" class="w-full py-3 rounded-xl bg-white hover:bg-indigo-50 text-indigo-900 text-xs font-bold shadow-lg transition">
                    Upgrade to Business (₹799)
                  </button>
                `}
              </div>
            </div>

            <div class="bg-white p-8 rounded-3xl border-2 ${currentPlan === 'professional' ? 'border-emerald-500 ring-2 ring-emerald-100' : 'border-slate-200'} flex flex-col justify-between shadow-sm">
              <div>
                <h3 class="font-bold text-lg text-slate-900">Enterprise HQ</h3>
                <div class="mt-6 flex items-baseline">
                  <span class="text-4xl font-extrabold text-slate-900">₹1,999</span>
                  <span class="text-xs text-slate-500 font-semibold ml-1">/ month</span>
                </div>
                <ul class="mt-6 space-y-3 text-xs text-slate-600">
                  <li class="flex items-center gap-2">✓ <strong>Unlimited Products</strong></li>
                  <li class="flex items-center gap-2">✓ <strong>Unlimited Multi-Fit Sheets</strong></li>
                  <li class="flex items-center gap-2">✓ Dedicated Priority Support</li>
                </ul>
              </div>
              <div class="mt-8">
                ${currentPlan === 'professional' ? `
                  <button disabled class="w-full py-3 rounded-xl bg-slate-100 text-slate-500 text-xs font-bold uppercase cursor-default">Active</button>
                ` : `
                  <button onclick="startUpgrade('professional', 1999)" class="w-full py-3 rounded-xl bg-slate-900 hover:bg-black text-white text-xs font-bold transition shadow-md">
                    Upgrade to Enterprise (₹1,999)
                  </button>
                `}
              </div>
            </div>
          </div>
        </div>
      `;
    }

    function renderCheckout() {
      const plan = state.selectedPlanForCheckout || { key: 'business', title: 'Business Professional', price: 799 };
      const qrBase64 = state.paymentData ? state.paymentData.upi_qr_base64 : '';

      return `
        <div class="max-w-4xl mx-auto px-4 sm:px-6 py-10">
          <div class="mb-6 flex items-center justify-between">
            <button onclick="navigate('plans')" class="text-xs font-bold text-slate-500 hover:text-slate-800 flex items-center gap-1">
              &larr; Back to Plans
            </button>
            <div class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-emerald-50 text-emerald-700 text-xs font-semibold">
              <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
              Secure Mock Payment Sandbox Active
            </div>
          </div>

          <div class="bg-white rounded-3xl border border-slate-200 shadow-xl overflow-hidden grid grid-cols-1 md:grid-cols-3">
            <div class="p-8 bg-slate-900 text-white flex flex-col justify-between">
              <div>
                <span class="text-xs uppercase tracking-widest text-indigo-400 font-bold">Order Summary</span>
                <h2 class="text-xl font-bold mt-1 text-white">${plan.title}</h2>
                <div class="mt-6 text-3xl font-extrabold text-white">₹${plan.price.toFixed(2)}</div>
                <p class="text-xs text-slate-400 mt-1">Instant simulated activation</p>
              </div>

              <div class="text-[11px] text-slate-400 bg-slate-800/60 p-3 rounded-xl mt-8">
                ℹ <strong>Sandbox Mode:</strong> Click confirmation below to upgrade entitlements instantly.
              </div>
            </div>

            <div class="md:col-span-2 p-8">
              <div class="flex border-b border-slate-200 mb-6">
                <button onclick="state.activePaymentTab = 'upi'; render();" class="pb-3 px-4 text-xs font-bold uppercase tracking-wider border-b-2 transition ${state.activePaymentTab === 'upi' ? 'border-indigo-600 text-indigo-600' : 'border-transparent text-slate-400 hover:text-slate-700'}">
                  UPI & QR Code (Amount Locked)
                </button>
                <button onclick="state.activePaymentTab = 'netbanking'; render();" class="pb-3 px-4 text-xs font-bold uppercase tracking-wider border-b-2 transition ${state.activePaymentTab === 'netbanking' ? 'border-indigo-600 text-indigo-600' : 'border-transparent text-slate-400 hover:text-slate-700'}">
                  Net Banking
                </button>
              </div>

              ${state.activePaymentTab === 'upi' ? `
                <div class="flex flex-col items-center text-center">
                  <div class="p-4 bg-white border-2 border-dashed border-slate-300 rounded-2xl shadow-sm mb-4">
                    ${qrBase64 ? `
                      <img src="data:image/png;base64,${qrBase64}" class="w-44 h-44 object-contain mx-auto" alt="UPI QR">
                    ` : `
                      <div class="w-44 h-44 flex items-center justify-center text-xs text-slate-400">Loading Locked QR...</div>
                    `}
                  </div>
                  <div class="text-xs text-slate-500 mb-4">
                    Amount is pre-filled & locked to <strong class="text-slate-900">₹${plan.price}</strong> on scan.
                  </div>
                  <button onclick="triggerMockPayment()" class="w-full py-3.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-bold shadow-lg shadow-indigo-100 transition">
                    Simulate Successful QR Payment (₹${plan.price})
                  </button>
                </div>
              ` : `
                <div class="space-y-4">
                  <div class="grid grid-cols-2 gap-3">
                    ${['HDFC Bank', 'ICICI Bank', 'State Bank of India', 'Axis Bank', 'Kotak Mahindra Bank', 'Punjab National Bank'].map(b => `
                      <label class="p-3 border rounded-xl flex items-center gap-2 cursor-pointer transition text-xs font-semibold ${state.selectedBank === b ? 'border-indigo-600 bg-indigo-50/50 text-indigo-900' : 'border-slate-200 text-slate-700'}">
                        <input type="radio" name="bank_choice" ${state.selectedBank === b ? 'checked' : ''} onchange="state.selectedBank = '${b}'; render();" class="text-indigo-600">
                        ${b}
                      </label>
                    `).join('')}
                  </div>
                  <div class="pt-4">
                    <button onclick="triggerMockPayment()" class="w-full py-3.5 rounded-xl bg-slate-900 hover:bg-black text-white text-xs font-bold shadow-lg transition">
                      Authorize Net Banking Transfer
                    </button>
                  </div>
                </div>
              `}
            </div>
          </div>
        </div>
      `;
    }

    function renderAdminHQ() {
      const data = state.adminOverview;
      if (!data) {
        return `
          <div class="p-16 flex flex-col items-center justify-center space-y-4 text-center">
            <div class="w-8 h-8 border-4 border-indigo-600 border-t-transparent rounded-full animate-spin"></div>
            <div class="text-sm font-semibold text-slate-600">Fetching SaaS Master Data...</div>
            <button onclick="loadAdminOverview()" class="px-4 py-2 bg-slate-900 text-white rounded-xl text-xs font-bold hover:bg-black">Retry</button>
          </div>
        `;
      }

      const st = data.stats || { total_tenants: 0, total_users: 0, total_catalog_products: 0, estimated_mrr: 0, plans: {} };
      const cfg = data.pricing_config || { business_price: 799, professional_price: 1999 };
      const customers = data.customers || [];

      return `
        <div class="max-w-7xl mx-auto space-y-8">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div>
              <div class="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-amber-50 border border-amber-200 text-xs font-bold text-amber-800 mb-2">
                <span class="w-2 h-2 rounded-full bg-amber-500 animate-pulse"></span>
                Master Control Center & Multi-Tenant Management
              </div>
              <h1 class="text-2xl font-extrabold text-slate-900">SaaS Administration & Tenants</h1>
              <p class="text-xs text-slate-500 mt-0.5">Manage all registered businesses, upgrade customer plans, ban accounts, and update platform pricing.</p>
            </div>
            <button onclick="loadAdminOverview()" class="px-4 py-2 rounded-xl bg-slate-900 hover:bg-black text-white text-xs font-bold transition flex items-center gap-2">
              🔄 Refresh Analytics
            </button>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-4 gap-6">
            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm">
              <span class="text-slate-400 text-xs font-semibold uppercase">Total Businesses</span>
              <div class="text-3xl font-extrabold text-slate-900 mt-2">${st.total_tenants}</div>
              <div class="text-xs text-slate-400 mt-2 font-medium">${st.total_users} Total Active User Logins</div>
            </div>

            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm">
              <span class="text-slate-400 text-xs font-semibold uppercase">Estimated MRR</span>
              <div class="text-3xl font-extrabold text-emerald-600 mt-2">₹${(st.estimated_mrr || 0).toLocaleString('en-IN')}</div>
              <div class="text-xs text-slate-400 mt-2 font-medium">Monthly recurring subscription value</div>
            </div>

            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm">
              <span class="text-slate-400 text-xs font-semibold uppercase">Total Hosted Catalog</span>
              <div class="text-3xl font-extrabold text-indigo-600 mt-2">${st.total_catalog_products} SKUs</div>
              <div class="text-xs text-slate-400 mt-2 font-medium">100% GS1 EAN-13 Indexed</div>
            </div>

            <div class="p-6 rounded-3xl bg-white border border-slate-200 shadow-sm">
              <span class="text-slate-400 text-xs font-semibold uppercase">Plan Breakdown</span>
              <div class="text-xs space-y-1 mt-2 font-bold text-slate-700">
                <div class="flex justify-between"><span>Free:</span> <span class="text-slate-900">${st.plans.free || 0}</span></div>
                <div class="flex justify-between"><span>Business:</span> <span class="text-indigo-600">${st.plans.business || 0}</span></div>
                <div class="flex justify-between"><span>Enterprise:</span> <span class="text-emerald-600">${st.plans.professional || 0}</span></div>
              </div>
            </div>
          </div>

          <div class="bg-white rounded-3xl border border-slate-200 shadow-sm overflow-hidden space-y-4 p-6 sm:p-8">
            <div class="flex items-center justify-between pb-4 border-b border-slate-100">
              <div>
                <h3 class="font-extrabold text-base text-slate-900">Registered SaaS Customers (${customers.length})</h3>
                <p class="text-xs text-slate-500 mt-0.5">Change client plans on the fly or restrict compromised accounts.</p>
              </div>
            </div>

            <div class="overflow-x-auto">
              <table class="w-full text-left text-sm">
                <thead class="bg-slate-50 border-b border-slate-200 text-xs text-slate-500 uppercase font-semibold">
                  <tr>
                    <th class="px-4 py-3">Business / Brand</th>
                    <th class="px-4 py-3">Owner Contact</th>
                    <th class="px-4 py-3">Current Plan</th>
                    <th class="px-4 py-3">Catalog Size</th>
                    <th class="px-4 py-3">Status</th>
                    <th class="px-4 py-3 text-right">Admin Controls</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100">
                  ${customers.map(c => `
                    <tr class="hover:bg-slate-50/70 transition">
                      <td class="px-4 py-4">
                        <div class="font-extrabold text-slate-900">${c.business_name}</div>
                        <div class="text-[11px] text-slate-400">Joined ${c.created_at}</div>
                      </td>
                      <td class="px-4 py-4">
                        <div class="font-semibold text-slate-800 text-xs">${c.owner_name}</div>
                        <div class="text-xs text-slate-500 font-mono">${c.owner_email}</div>
                      </td>
                      <td class="px-4 py-4">
                        <select onchange="adminChangeCustomerPlan(${c.business_id}, this.value)" class="text-xs font-bold rounded-lg px-2.5 py-1.5 border border-slate-200 bg-white focus:ring-2 focus:ring-amber-500">
                          <option value="free" ${c.plan === 'free' ? 'selected' : ''}>Free Tier</option>
                          <option value="business" ${c.plan === 'business' ? 'selected' : ''}>Business (₹799)</option>
                          <option value="professional" ${c.plan === 'professional' ? 'selected' : ''}>Enterprise (₹1,999)</option>
                          <option value="lifetime_unlimited" ${c.plan === 'lifetime_unlimited' ? 'selected' : ''}>Lifetime Unlimited</option>
                        </select>
                      </td>
                      <td class="px-4 py-4">
                        <span class="font-mono text-xs font-bold text-slate-700 bg-slate-100 px-2 py-0.5 rounded">${c.products_count} items</span>
                      </td>
                      <td class="px-4 py-4">
                        <span class="px-2.5 py-1 rounded-full text-[11px] font-extrabold ${c.is_suspended ? 'bg-rose-100 text-rose-700' : 'bg-emerald-100 text-emerald-700'}">
                          ${c.is_suspended ? 'Banned / Suspended' : 'Active'}
                        </span>
                      </td>
                      <td class="px-4 py-4 text-right space-x-2">
                        ${c.owner_email !== DEFAULT_ADMIN_EMAIL ? `
                          <button onclick="adminToggleSuspend(${c.owner_id}, ${c.is_suspended})" class="px-3 py-1 rounded-lg text-xs font-bold transition ${c.is_suspended ? 'bg-emerald-50 text-emerald-700 hover:bg-emerald-100' : 'bg-amber-50 text-amber-700 hover:bg-amber-100'}">
                            ${c.is_suspended ? 'Reactivate' : 'Suspend'}
                          </button>
                          <button onclick="adminDeleteTenant(${c.business_id}, '${c.business_name}')" class="px-3 py-1 rounded-lg bg-rose-50 hover:bg-rose-100 text-rose-600 text-xs font-bold transition">
                            Delete
                          </button>
                        ` : `
                          <span class="text-[11px] font-bold text-amber-600 bg-amber-50 px-2 py-1 rounded-md">Root Tenant</span>
                        `}
                      </td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <div class="bg-white rounded-3xl border border-slate-200 shadow-sm p-6 sm:p-8 space-y-4">
            <div>
              <h3 class="font-extrabold text-base text-slate-900">SaaS Plan Pricing Settings</h3>
              <p class="text-xs text-slate-500 mt-0.5">Control commercial pricing shown on the public landing page & checkout simulator.</p>
            </div>

            <form onsubmit="adminSavePricing(event)" class="grid grid-cols-1 sm:grid-cols-3 gap-6 pt-2">
              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1">Starter Free Tier (₹)</label>
                <input type="number" disabled value="0" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 bg-slate-100 text-slate-500 font-bold text-sm cursor-not-allowed text-center">
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1">Business Plan Price (₹/mo)</label>
                <input id="admBizPrice" type="number" required value="${cfg.business_price || 799}" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 font-bold text-sm focus:ring-2 focus:ring-amber-500 text-center">
              </div>
              <div>
                <label class="block text-xs font-semibold text-slate-700 mb-1">Enterprise HQ Price (₹/mo)</label>
                <input id="admProPrice" type="number" required value="${cfg.professional_price || 1999}" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 font-bold text-sm focus:ring-2 focus:ring-amber-500 text-center">
              </div>
              <div class="sm:col-span-3 flex justify-end">
                <button type="submit" class="px-6 py-3 rounded-xl bg-amber-500 hover:bg-amber-600 text-white font-bold text-xs shadow-md transition">
                  Update Platform Pricing
                </button>
              </div>
            </form>
          </div>
        </div>
      `;
    }

    // ==========================================
    // DISPATCHER
    // ==========================================
    function render() {
      const root = document.getElementById('appRoot');
      if (!root) return;

      if (state.view === 'landing') {
        root.innerHTML = renderLanding();
        setTimeout(initLandingCharts, 50);
        return;
      }

      if (!state.token) {
        if (state.view === 'auth') {
          root.innerHTML = renderAuth();
        } else if (state.view === 'plans') {
          root.innerHTML = renderPlans();
        } else {
          state.view = 'landing';
          root.innerHTML = renderLanding();
          setTimeout(initLandingCharts, 50);
        }
        return;
      }

      let contentHtml = '';
      if (state.view === 'admin_hq') contentHtml = renderAdminHQ();
      else if (state.view === 'dashboard') contentHtml = renderDashboard();
      else if (state.view === 'manage_catalog') contentHtml = renderManageCatalog();
      else if (state.view === 'add_product') contentHtml = renderAddProduct();
      else if (state.view === 'barcodes') contentHtml = renderBarcodeStudioView();
      else if (state.view === 'labels') contentHtml = renderLabelDesigner();
      else if (state.view === 'scanner') contentHtml = renderScanner();
      else if (state.view === 'settings') contentHtml = renderSettings();
      else if (state.view === 'plans') contentHtml = renderPlans();
      else if (state.view === 'checkout') contentHtml = renderCheckout();
      else contentHtml = renderDashboard();

      root.innerHTML = renderAppShell(contentHtml);

      if (state.view === 'dashboard') {
        setTimeout(initDashboardCharts, 50);
      }
      if (state.view === 'barcodes') {
        setTimeout(renderBarcodeStudio, 50);
      }
      if (state.view === 'labels') {
        setTimeout(updateLabelPreviewDOM, 50);
      }
    }

    function initApp() {
      try {
        state.view = 'landing';
        render();

        if (state.token && state.user) {
          Promise.all([loadCategories(), loadProducts()]).then(() => {
            ensureProductSelected();
          }).catch(err => console.warn(err));
        }
      } catch (err) {
        console.error("Critical rendering error:", err);
        const errBox = document.getElementById('errorBoundary');
        if (errBox) {
          errBox.classList.remove('hidden');
          errBox.innerText = "Fatal Error: " + err.message;
        }
      }
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initApp);
    } else {
      initApp();
    }
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index_spa():
    return HTMLResponse(content=SPA_HTML)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="127.0.0.1", port=port)

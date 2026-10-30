import os
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import (
    create_engine, String, Integer, DateTime, ForeignKey, Numeric, Text,
    UniqueConstraint, select, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session, sessionmaker
from passlib.context import CryptContext

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./warehouse_dev.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="staff")
    active: Mapped[int] = mapped_column(Integer, default=1)

class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)

class Part(Base):
    __tablename__ = "parts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(180), index=True)
    category: Mapped[str] = mapped_column(String(120), default="")
    brand_model: Mapped[str] = mapped_column(String(180), default="")
    unit: Mapped[str] = mapped_column(String(40), default="قطعة")
    min_stock: Mapped[int] = mapped_column(Integer, default=0)
    purchase_price: Mapped[Decimal] = mapped_column(Numeric(12,2), default=0)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(12,2), default=0)
    location_note: Mapped[str] = mapped_column(String(120), default="")

class Stock(Base):
    __tablename__ = "stocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("part_id", "warehouse_id"),)

class Movement(Base):
    __tablename__ = "movements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    movement_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

class Audit(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(120))
    details: Mapped[str] = mapped_column(Text, default="")

Base.metadata.create_all(engine)
templates = Jinja2Templates(directory="app/templates")
app = FastAPI(title="إدارة مخازن قطع الغيار")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "dev-only-change-me"))

def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def current_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(User, uid)
    return user if user and user.active else None

def seed():
    with SessionLocal() as db:
        if not db.scalar(select(func.count(User.id))):
            username = os.getenv("ADMIN_USERNAME", "admin")
            password = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
            db.add(User(username=username, password_hash=pwd.hash(password), role="admin"))
        if not db.scalar(select(func.count(Warehouse.id))):
            names = [x.strip() for x in os.getenv("WAREHOUSE_NAMES", "المخزن الرئيسي,فرع 2,فرع 3").split(",") if x.strip()]
            for name in names[:3]:
                db.add(Warehouse(name=name))
        db.commit()
seed()

def page(request, db, template, **context):
    user = current_user(request, db)
    return templates.TemplateResponse(template, {"request": request, "user": user, **context})

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=303)
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    parts_count = db.scalar(select(func.count(Part.id))) or 0
    total_qty = db.scalar(select(func.coalesce(func.sum(Stock.quantity), 0))) or 0
    low_count = db.scalar(select(func.count()).select_from(Stock).join(Part).where(Stock.quantity <= Part.min_stock)) or 0
    recent = db.scalars(select(Movement).order_by(Movement.created_at.desc()).limit(10)).all()
    return page(request, db, "dashboard.html", warehouses=warehouses, parts_count=parts_count,
                total_qty=total_qty, low_count=low_count, recent=recent)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(db_dep)):
    return page(request, db, "login.html", error=None)

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(db_dep)):
    user = db.scalar(select(User).where(User.username == username, User.active == 1))
    if not user or not pwd.verify(password, user.password_hash):
        return page(request, db, "login.html", error="اسم المستخدم أو كلمة المرور غير صحيحة")
    request.session["user_id"] = user.id
    db.add(Audit(user_id=user.id, action="تسجيل دخول", details="دخول ناجح"))
    db.commit()
    return RedirectResponse("/", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/parts", response_class=HTMLResponse)
def parts_page(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db): return RedirectResponse("/login", status_code=303)
    parts = db.scalars(select(Part).order_by(Part.name)).all()
    return page(request, db, "parts.html", parts=parts, message=None)

@app.post("/parts")
def add_part(request: Request, code: str = Form(...), name: str = Form(...),
             category: str = Form(""), brand_model: str = Form(""), unit: str = Form("قطعة"),
             min_stock: int = Form(0), purchase_price: float = Form(0), sale_price: float = Form(0),
             location_note: str = Form(""), db: Session = Depends(db_dep)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    if user.role != "admin": raise HTTPException(403, "غير مصرح")
    if db.scalar(select(Part).where(Part.code == code.strip())):
        raise HTTPException(400, "كود الصنف مستخدم بالفعل")
    part = Part(code=code.strip(), name=name.strip(), category=category, brand_model=brand_model,
                unit=unit, min_stock=max(0,min_stock), purchase_price=purchase_price,
                sale_price=sale_price, location_note=location_note)
    db.add(part)
    db.flush()
    for wh in db.scalars(select(Warehouse)).all():
        db.add(Stock(part_id=part.id, warehouse_id=wh.id, quantity=0))
    db.add(Audit(user_id=user.id, action="إضافة صنف", details=f"{code} - {name}"))
    db.commit()
    return RedirectResponse("/parts", status_code=303)

@app.get("/stock", response_class=HTMLResponse)
def stock_page(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db): return RedirectResponse("/login", status_code=303)
    rows = db.execute(select(Part, Stock, Warehouse).join(Stock, Stock.part_id==Part.id)
                      .join(Warehouse, Warehouse.id==Stock.warehouse_id).order_by(Part.name, Warehouse.id)).all()
    return page(request, db, "stock.html", rows=rows)

@app.get("/movement", response_class=HTMLResponse)
def movement_page(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db): return RedirectResponse("/login", status_code=303)
    parts = db.scalars(select(Part).order_by(Part.name)).all()
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    return page(request, db, "movement.html", parts=parts, warehouses=warehouses, error=None)

@app.post("/movement")
def add_movement(request: Request, part_id: int = Form(...), warehouse_id: int = Form(...),
                 movement_type: str = Form(...), quantity: int = Form(...), note: str = Form(""),
                 db: Session = Depends(db_dep)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    if movement_type not in ("إضافة", "صرف") or quantity <= 0:
        raise HTTPException(400, "بيانات الحركة غير صحيحة")
    stock = db.scalar(select(Stock).where(Stock.part_id==part_id, Stock.warehouse_id==warehouse_id).with_for_update())
    if not stock:
        raise HTTPException(404, "رصيد الصنف غير موجود")
    if movement_type == "صرف" and stock.quantity < quantity:
        raise HTTPException(400, f"الرصيد غير كافٍ. المتاح: {stock.quantity}")
    stock.quantity += quantity if movement_type == "إضافة" else -quantity
    db.add(Movement(part_id=part_id, warehouse_id=warehouse_id, movement_type=movement_type,
                    quantity=quantity, note=note, user_id=user.id))
    db.add(Audit(user_id=user.id, action=movement_type, details=f"صنف {part_id}، مخزن {warehouse_id}، كمية {quantity}"))
    db.commit()
    return RedirectResponse("/stock", status_code=303)

@app.get("/transfers", response_class=HTMLResponse)
def transfers_page(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db): return RedirectResponse("/login", status_code=303)
    parts = db.scalars(select(Part).order_by(Part.name)).all()
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    return page(request, db, "transfers.html", parts=parts, warehouses=warehouses)

@app.post("/transfers")
def transfer(request: Request, part_id: int = Form(...), from_id: int = Form(...),
             to_id: int = Form(...), quantity: int = Form(...), note: str = Form(""),
             db: Session = Depends(db_dep)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    if from_id == to_id or quantity <= 0: raise HTTPException(400, "حدد مخزنين مختلفين وكمية صحيحة")
    source = db.scalar(select(Stock).where(Stock.part_id==part_id, Stock.warehouse_id==from_id).with_for_update())
    dest = db.scalar(select(Stock).where(Stock.part_id==part_id, Stock.warehouse_id==to_id).with_for_update())
    if not source or not dest or source.quantity < quantity: raise HTTPException(400, "الرصيد في مخزن المصدر غير كافٍ")
    source.quantity -= quantity
    dest.quantity += quantity
    db.add(Movement(part_id=part_id, warehouse_id=from_id, movement_type="تحويل صادر", quantity=quantity, note=f"إلى مخزن {to_id}. {note}", user_id=user.id))
    db.add(Movement(part_id=part_id, warehouse_id=to_id, movement_type="تحويل وارد", quantity=quantity, note=f"من مخزن {from_id}. {note}", user_id=user.id))
    db.add(Audit(user_id=user.id, action="تحويل بين المخازن", details=f"صنف {part_id}، من {from_id} إلى {to_id}، كمية {quantity}"))
    db.commit()
    return RedirectResponse("/stock", status_code=303)

@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db): return RedirectResponse("/login", status_code=303)
    rows = db.execute(select(Part, Stock, Warehouse).join(Stock, Stock.part_id==Part.id)
                      .join(Warehouse, Warehouse.id==Stock.warehouse_id).where(Stock.quantity <= Part.min_stock)
                      .order_by(Part.name)).all()
    return page(request, db, "reports.html", rows=rows)

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(db_dep)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    if user.role != "admin": raise HTTPException(403, "للمدير فقط")
    users = db.scalars(select(User).order_by(User.id)).all()
    return page(request, db, "users.html", users=users, error=None)

@app.post("/users")
def add_user(request: Request, username: str = Form(...), password: str = Form(...),
             role: str = Form("staff"), db: Session = Depends(db_dep)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    if user.role != "admin": raise HTTPException(403, "للمدير فقط")
    if len(password) < 10: raise HTTPException(400, "كلمة المرور يجب أن تكون 10 أحرف على الأقل")
    if role not in ("admin", "staff"): raise HTTPException(400, "دور غير صحيح")
    if db.scalar(select(User).where(User.username==username)): raise HTTPException(400, "اسم المستخدم موجود")
    db.add(User(username=username, password_hash=pwd.hash(password), role=role))
    db.add(Audit(user_id=user.id, action="إضافة مستخدم", details=username))
    db.commit()
    return RedirectResponse("/users", status_code=303)

@app.get("/history", response_class=HTMLResponse)
def history(request: Request, db: Session = Depends(db_dep)):
    if not current_user(request, db): return RedirectResponse("/login", status_code=303)
    rows = db.execute(select(Movement, Part, Warehouse, User)
                      .join(Part, Part.id==Movement.part_id)
                      .join(Warehouse, Warehouse.id==Movement.warehouse_id)
                      .join(User, User.id==Movement.user_id)
                      .order_by(Movement.created_at.desc()).limit(300)).all()
    return page(request, db, "history.html", rows=rows)

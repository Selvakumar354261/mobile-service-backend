from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta, timezone
from typing import Optional
import os
import hashlib
import hmac
import secrets
import jwt

app = FastAPI(title="Mobile Service Tracker")

# ---- Database connection ----
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://apple@localhost/mobile_service_db")
engine = create_engine(DATABASE_URL)

# ---- Auth ----
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30

bearer_scheme = HTTPBearer()

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"

def verify_password(password: str, stored: str) -> bool:
    salt, _, expected = stored.partition("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hmac.compare_digest(digest.hex(), expected)

def create_access_token(username: str, role: str) -> str:
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"username": payload["sub"], "role": payload["role"]}

def require_admin(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

# ---- Pydantic models (request/response shapes) ----
class CustomerCreate(BaseModel):
    name: str
    mobile_number: str
    address: Optional[str] = None

class DeviceCreate(BaseModel):
    customer_id: int
    brand: str
    model: str
    imei_number: Optional[str] = None
    lock_type: Optional[str] = None
    lock_value: Optional[str] = None

class ServiceCreate(BaseModel):
    device_id: int
    issue_description: str
    estimated_cost: Optional[float] = None

class StatusUpdate(BaseModel):
    status: str
    final_cost: Optional[float] = None
    spare_part_used: Optional[str] = None
    notes: Optional[str] = None

class LoginRequest(BaseModel):
    username: str
    password: str

@app.get("/")
def root():
    return {"message": "Mobile Service Tracker API running"}

# ---- Login ----
@app.post("/login")
def login(credentials: LoginRequest):
    with engine.connect() as conn:
        user = conn.execute(
            text("SELECT username, password_hash, role FROM users WHERE username = :username"),
            {"username": credentials.username}
        ).fetchone()

    if not user or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_access_token(user.username, user.role)
    return {"access_token": token, "token_type": "bearer", "role": user.role, "username": user.username}

# ---- Create a customer ----
@app.post("/customers")
def create_customer(customer: CustomerCreate, current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        result = conn.execute(
            text("""INSERT INTO customers (name, mobile_number, address)
                     VALUES (:name, :mobile_number, :address)
                     RETURNING customer_id"""),
            {"name": customer.name, "mobile_number": customer.mobile_number, "address": customer.address}
        )
        conn.commit()
        new_id = result.fetchone()[0]
    return {"customer_id": new_id, "message": "Customer created"}

# ---- List all customers ----
@app.get("/customers")
def list_customers(current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).fetchall()
    return [dict(c._mapping) for c in customers]

# ---- Search customer + all their devices + service history by mobile number ----
@app.get("/customers/search/{mobile_number}")
def search_customer(mobile_number: str, current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        customer = conn.execute(
            text("SELECT * FROM customers WHERE mobile_number = :mobile"),
            {"mobile": mobile_number}
        ).fetchone()

        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        devices = conn.execute(
            text("SELECT * FROM devices WHERE customer_id = :cid"),
            {"cid": customer.customer_id}
        ).fetchall()

        device_list = []
        for d in devices:
            services = conn.execute(
                text("SELECT * FROM service_requests WHERE device_id = :did ORDER BY received_date DESC"),
                {"did": d.device_id}
            ).fetchall()
            device_list.append({
                "device_id": d.device_id,
                "brand": d.brand,
                "model": d.model,
                "imei_number": d.imei_number,
                "lock_type": d.lock_type,
                "lock_value": d.lock_value,
                "services": [dict(s._mapping) for s in services]
            })

    return {
        "customer": dict(customer._mapping),
        "devices": device_list
    }

# ---- Update customer ----
@app.put("/customers/{customer_id}")
def update_customer(customer_id: int, customer: CustomerCreate, current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        result = conn.execute(
            text("""UPDATE customers SET name=:name, mobile_number=:mobile_number, address=:address
                     WHERE customer_id=:cid"""),
            {"name": customer.name, "mobile_number": customer.mobile_number,
             "address": customer.address, "cid": customer_id}
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Customer not found")
    return {"message": "Customer updated"}

# ---- Delete customer (and their devices + service requests) ----
@app.delete("/customers/{customer_id}")
def delete_customer(customer_id: int, current_user: dict = Depends(require_admin)):
    with engine.connect() as conn:
        device_ids = conn.execute(
            text("SELECT device_id FROM devices WHERE customer_id=:cid"),
            {"cid": customer_id}
        ).fetchall()
        for d in device_ids:
            conn.execute(text("DELETE FROM service_requests WHERE device_id=:did"), {"did": d.device_id})
        conn.execute(text("DELETE FROM devices WHERE customer_id=:cid"), {"cid": customer_id})
        result = conn.execute(text("DELETE FROM customers WHERE customer_id=:cid"), {"cid": customer_id})
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Customer not found")
    return {"message": "Customer deleted"}

# ---- Add a device for a customer ----
@app.post("/devices")
def create_device(device: DeviceCreate, current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        result = conn.execute(
            text("""INSERT INTO devices (customer_id, brand, model, imei_number, lock_type, lock_value)
                     VALUES (:customer_id, :brand, :model, :imei_number, :lock_type, :lock_value)
                     RETURNING device_id"""),
            {"customer_id": device.customer_id, "brand": device.brand,
             "model": device.model, "imei_number": device.imei_number,
             "lock_type": device.lock_type, "lock_value": device.lock_value}
        )
        conn.commit()
        new_id = result.fetchone()[0]
    return {"device_id": new_id, "message": "Device added"}

# ---- Create a service request (device comes in for repair) ----
@app.post("/service-requests")
def create_service(service: ServiceCreate, current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        result = conn.execute(
            text("""INSERT INTO service_requests (device_id, issue_description, estimated_cost)
                     VALUES (:device_id, :issue_description, :estimated_cost)
                     RETURNING service_id"""),
            {"device_id": service.device_id, "issue_description": service.issue_description,
             "estimated_cost": service.estimated_cost}
        )
        conn.commit()
        new_id = result.fetchone()[0]
    return {"service_id": new_id, "message": "Service request created"}

# ---- Update service status (received -> in_progress -> ready -> delivered) ----
# COALESCE ensures an empty field in the update dialog does NOT wipe out
# a previously saved final_cost / spare_part_used / notes value.
@app.put("/service-requests/{service_id}/status")
def update_status(service_id: int, update: StatusUpdate, current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        completed = datetime.now() if update.status == "delivered" else None
        result = conn.execute(
            text("""UPDATE service_requests
                     SET status = :status,
                         final_cost = COALESCE(:final_cost, final_cost),
                         spare_part_used = COALESCE(:spare_part_used, spare_part_used),
                         notes = COALESCE(:notes, notes),
                         completed_date = COALESCE(:completed, completed_date)
                     WHERE service_id = :sid"""),
            {"status": update.status, "final_cost": update.final_cost,
             "spare_part_used": update.spare_part_used,
             "notes": update.notes, "completed": completed, "sid": service_id}
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Service request not found")
    return {"message": "Status updated"}

# ---- Pending service requests (not delivered), newest first ----
@app.get("/service-requests/pending")
def pending_services(current_user: dict = Depends(get_current_user)):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""SELECT sr.service_id, sr.issue_description, sr.status, sr.estimated_cost,
                            sr.spare_part_used, sr.final_cost, sr.received_date,
                            d.device_id, d.brand, d.model, d.lock_type, d.lock_value,
                            c.customer_id, c.name, c.mobile_number
                     FROM service_requests sr
                     JOIN devices d ON sr.device_id = d.device_id
                     JOIN customers c ON d.customer_id = c.customer_id
                     WHERE sr.status != 'delivered'
                     ORDER BY sr.received_date DESC""")
        ).fetchall()
    return [dict(r._mapping) for r in rows]
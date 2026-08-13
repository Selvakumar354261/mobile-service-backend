from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from datetime import datetime
from typing import Optional
import os
import time

app = FastAPI(title="Mobile Service Tracker")

# ---- Database connection ----
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://apple@localhost/mobile_service_db")
engine = create_engine(DATABASE_URL)

# No migration tooling in this project — creating the table here (idempotent)
# keeps local dev and Railway in sync without a manual DB step on deploy.
# Runs on FastAPI startup (not at import time) with retries: on Railway, a
# connection attempted the instant the module is imported can race the
# platform injecting DATABASE_URL, which previously crashed the whole
# container instead of just failing one request.
@app.on_event("startup")
def ensure_schema():
    last_error = None
    for attempt in range(5):
        try:
            with engine.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS status_history (
                        history_id SERIAL PRIMARY KEY,
                        service_id INTEGER NOT NULL REFERENCES service_requests(service_id) ON DELETE CASCADE,
                        status TEXT NOT NULL,
                        final_cost NUMERIC,
                        spare_part_used TEXT,
                        notes TEXT
                    )
                """))
                conn.commit()
            return
        except OperationalError as e:
            last_error = e
            time.sleep(1)
    raise last_error

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

@app.get("/")
def root():
    return {"message": "Mobile Service Tracker API running"}

# ---- Create a customer ----
@app.post("/customers")
def create_customer(customer: CustomerCreate):
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
def list_customers():
    with engine.connect() as conn:
        customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).fetchall()
    return [dict(c._mapping) for c in customers]

# ---- Search customer + all their devices + service history by mobile number ----
@app.get("/customers/search/{mobile_number}")
def search_customer(mobile_number: str):
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
def update_customer(customer_id: int, customer: CustomerCreate):
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
def delete_customer(customer_id: int):
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
def create_device(device: DeviceCreate):
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
def create_service(service: ServiceCreate):
    with engine.connect() as conn:
        result = conn.execute(
            text("""INSERT INTO service_requests (device_id, issue_description, estimated_cost)
                     VALUES (:device_id, :issue_description, :estimated_cost)
                     RETURNING service_id, status"""),
            {"device_id": service.device_id, "issue_description": service.issue_description,
             "estimated_cost": service.estimated_cost}
        )
        new_id, initial_status = result.fetchone()

        # First entry of the history timeline — the status a service starts
        # in when it's received, not something set via the /status endpoint.
        conn.execute(
            text("""INSERT INTO status_history (service_id, status)
                     VALUES (:service_id, :status)"""),
            {"service_id": new_id, "status": initial_status}
        )
        conn.commit()
    return {"service_id": new_id, "message": "Service request created"}

# ---- Update service status (received -> in_progress -> ready -> delivered) ----
# COALESCE ensures an empty field in the update dialog does NOT wipe out
# a previously saved final_cost / spare_part_used / notes value. Every call
# also appends a status_history row so changes over time can be shown as a
# timeline, not just the current snapshot.
@app.put("/service-requests/{service_id}/status")
def update_status(service_id: int, update: StatusUpdate):
    with engine.connect() as conn:
        completed = datetime.now() if update.status == "delivered" else None
        result = conn.execute(
            text("""UPDATE service_requests
                     SET status = :status,
                         final_cost = COALESCE(:final_cost, final_cost),
                         spare_part_used = COALESCE(:spare_part_used, spare_part_used),
                         notes = COALESCE(:notes, notes),
                         completed_date = COALESCE(:completed, completed_date)
                     WHERE service_id = :sid
                     RETURNING status, final_cost, spare_part_used, notes"""),
            {"status": update.status, "final_cost": update.final_cost,
             "spare_part_used": update.spare_part_used,
             "notes": update.notes, "completed": completed, "sid": service_id}
        )
        row = result.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Service request not found")

        conn.execute(
            text("""INSERT INTO status_history (service_id, status, final_cost, spare_part_used, notes)
                     VALUES (:service_id, :status, :final_cost, :spare_part_used, :notes)"""),
            {"service_id": service_id, "status": row.status, "final_cost": row.final_cost,
             "spare_part_used": row.spare_part_used, "notes": row.notes}
        )
        conn.commit()
    return {"message": "Status updated"}

# ---- Status change timeline for a service request, oldest first ----
@app.get("/service-requests/{service_id}/history")
def service_history(service_id: int):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""SELECT history_id, status, final_cost, spare_part_used, notes
                     FROM status_history
                     WHERE service_id = :sid
                     ORDER BY history_id ASC"""),
            {"sid": service_id}
        ).fetchall()
    return [dict(r._mapping) for r in rows]

# ---- Pending service requests (not delivered), newest first ----
@app.get("/service-requests/pending")
def pending_services():
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
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from datetime import datetime
from typing import Optional

app = FastAPI(title="Mobile Service Tracker")

# ---- Database connection ----
DATABASE_URL = "postgresql://apple@localhost/mobile_service_db"
engine = create_engine(DATABASE_URL)

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
            text("""INSERT INTO devices (customer_id, brand, model, imei_number)
                     VALUES (:customer_id, :brand, :model, :imei_number)
                     RETURNING device_id"""),
            {"customer_id": device.customer_id, "brand": device.brand,
             "model": device.model, "imei_number": device.imei_number}
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
                     WHERE service_id = :sid"""),
            {"status": update.status, "final_cost": update.final_cost,
             "spare_part_used": update.spare_part_used,
             "notes": update.notes, "completed": completed, "sid": service_id}
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Service request not found")
    return {"message": "Status updated"}

# ---- Pending service requests (not delivered) ----
@app.get("/service-requests/pending")
def pending_services():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""SELECT sr.service_id, sr.issue_description, sr.status, sr.estimated_cost,
                            sr.spare_part_used, sr.final_cost, sr.received_date,
                            d.device_id, d.brand, d.model,
                            c.customer_id, c.name, c.mobile_number
                     FROM service_requests sr
                     JOIN devices d ON sr.device_id = d.device_id
                     JOIN customers c ON d.customer_id = c.customer_id
                     WHERE sr.status != 'delivered'
                     ORDER BY sr.received_date ASC""")
        ).fetchall()
    return [dict(r._mapping) for r in rows]
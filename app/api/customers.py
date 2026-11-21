"""Customer Management, Consent Collection, and Opt-Out Administrative Endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.compliance import is_24h_service_window_open
from app.core.security import get_current_admin, record_audit_log, require_roles
from app.database.connection import get_db
from app.models.customer import Customer
from app.models.system import AdminUser
from app.schemas.customer import (
    ConsentRecordResponse,
    ConsentUpdateRequest,
    CustomerCreate,
    CustomerHistoryResponse,
    CustomerResponse,
    CustomerUpdate,
    OptOutRequest,
)
from app.services.consent_service import ConsentService
from app.services.customer_service import CustomerService

router = APIRouter(prefix="/api/customers", tags=["Customers & Consent"])


def _to_customer_response(customer: Customer) -> CustomerResponse:
    resp = CustomerResponse.model_validate(customer)
    resp.service_window_open = is_24h_service_window_open(customer)
    return resp


@router.get(
    "",
    response_model=list[CustomerResponse],
    summary="List and search customers",
)
def list_customers(
    search: Optional[str] = Query(default=None, description="Search by E.164 phone number or customer name"),
    marketing_consent: Optional[bool] = Query(default=None),
    utility_consent: Optional[bool] = Query(default=None),
    opted_out: Optional[bool] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> list[CustomerResponse]:
    customers = CustomerService.list_customers(
        db=db,
        search=search,
        marketing_consent=marketing_consent,
        utility_consent=utility_consent,
        opted_out=opted_out,
        skip=skip,
        limit=limit,
    )
    return [_to_customer_response(c) for c in customers]


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new customer (importing a phone number never grants automatic opt-in)",
)
def create_customer(
    payload: CustomerCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager", "support_agent")),
) -> CustomerResponse:
    try:
        customer = CustomerService.create_customer(
            db=db,
            payload=payload,
            actor=current_admin.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="create_customer",
        resource_type="customer",
        resource_id=str(customer.id),
        details={
            "marketing_consent": customer.marketing_consent,
            "utility_consent": customer.utility_consent,
        },
        ip_address=request.client.host if request.client else None,
    )
    return _to_customer_response(customer)


@router.get(
    "/export",
    summary="Export permitted, opted-in customer records",
)
def export_permitted_customers(
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    records = CustomerService.export_permitted_customers(db)
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="export_permitted_customers",
        resource_type="customer",
        details={"exported_count": len(records)},
        ip_address=request.client.host if request.client else None,
    )
    return {"count": len(records), "customers": records}


@router.get(
    "/{customer_id}",
    response_model=CustomerHistoryResponse,
    summary="View customer profile, 24h window status, consent audit trail, and conversation history",
)
def get_customer_detail(
    customer_id: int,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> CustomerHistoryResponse:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

    history = CustomerService.get_customer_history(db, customer)
    return CustomerHistoryResponse(
        customer=_to_customer_response(customer),
        consent_history=[ConsentRecordResponse.model_validate(r) for r in history["consent_history"]],
        messages=history["messages"],
        conversations=history["conversations"],
    )


@router.patch(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Update customer profile details",
)
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager", "support_agent")),
) -> CustomerResponse:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

    updated = CustomerService.update_customer(db, customer, payload)
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="update_customer",
        resource_type="customer",
        resource_id=str(customer.id),
        ip_address=request.client.host if request.client else None,
    )
    return _to_customer_response(updated)


@router.delete(
    "/{customer_id}",
    summary="Anonymize customer personal data and revoke consent",
)
def anonymize_customer(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

    anonymized = CustomerService.anonymize_customer(db, customer, actor=current_admin.username)
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="anonymize_customer",
        resource_type="customer",
        resource_id=str(customer_id),
        ip_address=request.client.host if request.client else None,
    )
    return {
        "customer_id": anonymized.id,
        "status": "anonymized",
        "is_anonymized": anonymized.is_anonymized,
    }


@router.post(
    "/{customer_id}/consent",
    response_model=CustomerResponse,
    summary="Record explicit customer WhatsApp opt-in consent with audit evidence",
)
def record_customer_consent(
    customer_id: int,
    payload: ConsentUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager", "support_agent")),
) -> CustomerResponse:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

    try:
        updated = ConsentService.record_consent(
            db=db,
            customer=customer,
            marketing_consent=payload.marketing_consent,
            utility_consent=payload.utility_consent,
            collection_method=payload.collection_method,
            source=payload.source,
            evidence=payload.evidence,
            actor=current_admin.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="update_customer_consent",
        resource_type="customer",
        resource_id=str(customer.id),
        details={
            "marketing_consent": updated.marketing_consent,
            "utility_consent": updated.utility_consent,
            "collection_method": payload.collection_method,
            "source": payload.source,
        },
        ip_address=request.client.host if request.client else None,
    )
    return _to_customer_response(updated)


@router.post(
    "/{customer_id}/opt-out",
    summary="Immediately opt out customer and cancel any queued or scheduled messages",
)
def opt_out_customer(
    customer_id: int,
    payload: Optional[OptOutRequest] = None,
    request: Request = None,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager", "support_agent")),
) -> dict:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

    req = payload or OptOutRequest()
    result = ConsentService.process_opt_out(
        db=db,
        customer=customer,
        categories=req.categories,
        reason=req.reason or "Administrator recorded opt-out",
        source=req.source,
        actor=current_admin.username,
    )

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="opt_out_customer",
        resource_type="customer",
        resource_id=str(customer.id),
        details=result,
        ip_address=request.client.host if request and request.client else None,
    )
    return result
